# Live Trading System Architecture

## Overview

The live trading system streams real-time market data from TopStep (via ProjectX WebSocket), injects it into AmiBroker via COM/OLE, periodically scans for AFL strategy signals using AmiBroker's Exploration engine, and optionally executes trades back into TopStep as market orders.

Everything runs inside a single Python process. Three threads divide the work based on what each subsystem needs: COM/OLE calls must happen on a single thread (the "COM thread"), while the two ProjectX async clients each get their own thread with their own `asyncio` event loop.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Flask Web Server  (http://localhost:5000)                               │
│                                                                          │
│  GET  /live                → Serves the live dashboard HTML              │
│  POST /api/live/start      → Creates & starts the orchestrator           │
│  GET  /api/live/status     → Returns current session state (JSON)        │
│  POST /api/live/stop       → Graceful shutdown                           │
│  POST /api/live/kill       → Emergency kill switch (trades only)         │
│  GET  /api/live/accounts   → Fetches available ProjectX accounts         │
└────────────────────────────┬─────────────────────────────────────────────┘
                             │
                             │ spawns daemon thread
                             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  COM / Main Thread   (LiveAlertOrchestrator)                             │
│  pythoncom.CoInitialize()                                                │
│                                                                          │
│  ┌─────────────────┐  ┌─────────────────┐  ┌──────────────────┐         │
│  │ QuoteInjector   │  │ SignalScanner   │  │ AlertDispatcher  │         │
│  │ (AmiBroker COM) │  │ (AmiBroker OLE) │  │ (log/sound/      │         │
│  │                 │  │                 │  │  desktop/webhook) │         │
│  └────────┬────────┘  └────────┬────────┘  └──────────────────┘         │
│           │                    │                                          │
│  Main Loop (while running):                                              │
│    1. Drain bar_queue → inject bars via COM → RefreshAll()               │
│    2. Process feed status messages                                       │
│    3. Every scan_interval seconds:                                       │
│       scanner.scan()  →  build APX  →  OLE Exploration  →  parse CSV    │
│    4. Dispatch alerts for new signals                                     │
│    5. Submit trade for LATEST signal only                                 │
│    6. Drain trade results from executor                                   │
│    7. sleep(0.5s)                                                        │
└──────┬────────────────────────────────────┬──────────────────────────────┘
       │                                    │
  bar_queue (Queue)                    trade_queue (Queue)
  feed_status_queue (Queue)            result_queue (Queue)
       │                                    │
       ▼                                    ▼
┌──────────────────────┐         ┌──────────────────────────┐
│  ProjectX Feed       │         │  Trade Executor          │
│  (Background Thread) │         │  (Background Thread)     │
│                      │         │                          │
│  Own asyncio loop    │         │  Own asyncio loop        │
│  TradingSuite        │         │  Own ProjectX client     │
│  (WebSocket/SignalR) │         │  Own OrderManager        │
│                      │         │                          │
│  Streams live bars   │         │  Receives TradeRequests  │
│  via WebSocket       │         │  Places market orders    │
│                      │         │  Confirms fills via      │
│  Puts BarData on     │         │  position checks         │
│  bar_queue           │         │  Returns TradeResults    │
└──────────────────────┘         └──────────────────────────┘
       │                                    │
       ▼                                    ▼
  ProjectX WebSocket                 ProjectX REST API
  (TopStep SignalR)                  (TopStep/CME)
```

---

## Thread Model

| Thread | Name | Purpose | External I/O |
|--------|------|---------|-------------|
| Flask | (main process) | Serves web UI, handles API requests | HTTP |
| COM Thread | `LiveAlertOrchestrator` | All AmiBroker COM/OLE, bar injection, signal scanning, alert dispatch | AmiBroker COM |
| Feed Thread | `ProjectXFeed` | WebSocket streaming for market data bars via TradingSuite | ProjectX WebSocket (SignalR) |
| Executor Thread | `TradeExecutor` | Async order placement and fill confirmation | ProjectX REST API |

**Why separate threads?**
- AmiBroker COM requires a single-threaded COM apartment — all OLE calls must happen on the same thread that called `CoInitialize()`.
- ProjectX is an async SDK (`async/await`). Each background thread creates its own `asyncio` event loop so async calls don't block the COM thread.
- The Feed and Executor each get their own ProjectX client because they authenticate independently and may be doing concurrent API calls.

**Inter-thread communication is strictly queue-based** — no shared mutable state, no locks:

| Queue | Direction | Payload |
|-------|-----------|---------|
| `bar_queue` | Feed → COM Thread | `BarData` (OHLCV bar) |
| `feed_status_queue` | Feed → COM Thread | `FeedStatus` (connected/disconnected) |
| `_trade_queue` | COM Thread → Executor | `TradeRequest` |
| `_result_queue` | Executor → COM Thread | `TradeResult` |

---

## Startup Sequence

### 1. User clicks "Launch Session" in the dashboard

The browser sends `POST /api/live/start` with:

```json
{
  "strategy_id": "T03_-_Live_Pipeline_Test__2-Min_Cycle_",
  "account_id": 19092348,
  "account_name": "PRAC-V2-296866-99132796",
  "symbol": "NQH6",
  "ami_symbol": "NQ",
  "scan_interval": 120,
  "alert_channels": ["log", "desktop", "sound"],
  "trade_enabled": true,
  "trade_size": 3,
  "trade_timeout": 30
}
```

### 2. Flask creates the orchestrator

```python
# dashboard/app.py
orchestrator = LiveAlertOrchestrator(
    symbols=["NQH6"],
    ami_symbol="NQ",
    scan_interval=120,
    strategy_afl_path="strategies/T03_Live_Pipeline_Test.afl",
    alert_channels=["log", "desktop", "sound"],
    account_id=19092348,
    status_callback=_live_status_callback,
    trade_enabled=True,
    trade_symbol="NQH6",
    trade_size=3,
    trade_timeout=30,
)
```

### 3. Flask spawns the COM thread

```python
thread = orchestrator.start_background()  # daemon=True
```

The orchestrator's `start()` method blocks on this thread. It:

1. Calls `pythoncom.CoInitialize()` — registers this thread as a COM apartment
2. Creates `QuoteInjector` — connects to AmiBroker via `win32com.client.Dispatch("Broker.Application")`
3. Uses AmiBroker's currently-loaded database (no `LoadDatabase()` call for live streaming)
4. Creates `SignalScanner` — receives the existing `ab` COM object (shares it with the injector)
5. Creates `AlertDispatcher` — configures log, desktop, sound channels
6. Creates `ProjectXFeed` — starts its own background thread
7. Creates `TradeExecutor` — starts its own background thread
8. Enters `_main_loop()`

### 4. Feed thread connects WebSocket and backfills

On its own thread, `ProjectXFeed`:

1. Creates a new `asyncio` event loop
2. Maps the requested `(unit, interval)` to a TradingSuite timeframe string (e.g., `(2, 1)` → `"1min"`)
3. Creates `TradingSuite.create("NQH6", timeframes=["1min"])` which:
   - Authenticates with the ProjectX API
   - Establishes WebSocket connections (SignalR) to the TopStepX User Hub and Market Hub
   - Loads historical bars (default 5 days)
   - Subscribes to real-time market data
4. Registers a `NEW_BAR` event handler via `suite.on(EventType.NEW_BAR, callback)`
5. Backfills historical bars from `suite.data.get_data("1min", bars=5000)` into `bar_queue`
6. Streams live bars — the `NEW_BAR` callback fires when each bar closes, putting a `BarData` on `bar_queue`

**Reconnection:** TradingSuite handles WebSocket reconnection automatically with exponential backoff (1, 3, 5, 5, 5... seconds). If the initial `TradingSuite.create()` fails, the feed retries with its own backoff (5, 10, 20, 40, 60s cap).

**Timeframe support:** 14 fixed timeframes are supported: `1sec`, `5sec`, `10sec`, `15sec`, `30sec`, `1min`, `5min`, `15min`, `30min`, `1hr`, `4hr`, `1day`, `1week`, `1month`. For non-standard intervals (e.g., 2-minute), the feed falls back to `1min` and lets AmiBroker aggregate via `TimeFrameSet()`.

### 5. Executor thread authenticates and waits

On its own thread, `TradeExecutor`:

1. Creates a new `asyncio` event loop
2. Creates its own `ProjectX.from_env()` client
3. Authenticates
4. Selects the account
5. Resolves the contract: `await client.search_instruments("NQH6")` → `"CON.F.US.ENQ.H26"`
6. Creates `OrderManager(client, event_bus)`
7. Enters processing loop: blocks on `_trade_queue.get(timeout=1.0)`, waiting for trade requests

### 6. Main loop starts processing

Once bars begin arriving on `bar_queue`, the COM thread's main loop:

1. **Drains bars** — calls `injector.inject_bar()` for each, which uses COM to add OHLCV data to AmiBroker
2. **Refreshes AmiBroker** — `ab.RefreshAll()` so charts update
3. **Waits for scan interval** — e.g., 120 seconds
4. **Runs a scan** — `scanner.scan()` (see Signal Scanning below)

---

## Data Flow: ProjectX → AmiBroker

```
ProjectX WebSocket (SignalR)         AmiBroker
  Market Hub                          COM (OLE)
       │                                 │
       │  Live ticks via WebSocket       │
       ▼                                 │
  TradingSuite / RealtimeDataManager     │
       │                                 │
       │  NEW_BAR event fires            │
       │  when bar closes                │
       ▼                                 │
  ProjectXFeed._handle_new_bar()         │
       │                                 │
       │  BarData objects                │
       │  (queue.Queue)                  │
       ▼                                 │
  Orchestrator._main_loop()              │
       │                                 │
       │  injector.inject_bar()  ────────▶  ab.Stocks("NQ")
       │                                    qt = stock.Quotations.Add(date)
       │                                    qt.Open = bar.open
       │                                    qt.High = bar.high
       │                                    qt.Low  = bar.low
       │                                    qt.Close = bar.close
       │                                    qt.Volume = bar.volume
       │
       │  injector.refresh_all() ────────▶  ab.RefreshAll()
```

**Key details:**
- The symbol mapping is configurable: ProjectX uses contract symbols like `"NQH6"` (with expiry), AmiBroker uses generic symbols like `"NQ"`.
- Bars arrive the instant they close — no polling delay.
- `inject_bar()` converts Python `datetime` to COM date format via `pythoncom.MakeTime()`.
- Duplicate bars are skipped (tracked by `(symbol, com_date)` set).
- COM calls are wrapped in retry logic (3 attempts with exponential backoff) to handle transient RPC failures.

---

## Signal Scanning: AmiBroker OLE Exploration

Every `scan_interval` seconds, the orchestrator calls `scanner.scan()`. This is a blocking operation on the COM thread.

### Step 1: Generate Exploration AFL

The scanner takes the strategy AFL (e.g., `T03_Live_Pipeline_Test.afl`) and transforms it into an exploration-compatible version:

```
Original Strategy AFL
        │
        ▼
  _expand_includes()          ← Inline all #include_once files
        │
        ▼
  _process_isempty_guards()   ← Handle IsEmpty() checks
        │
        ▼
  _replace_params_with_values()  ← Replace Param() calls with defaults
        │
        ▼
  _strip_trading_directives() ← Remove Plot(), ApplyStop(),
        │                        SetPositionSize(), SetTradeDelays()
        ▼
  Append exploration block:

    _recentBars = BarIndex() >= (BarCount - 5);
    Filter = (Buy OR Sell OR Short OR Cover)
             AND _recentBars
             AND Name() == "NQ";

    AddColumn(Buy, "Buy", 1.0);
    AddColumn(Sell, "Sell", 1.0);
    AddColumn(Short, "Short", 1.0);
    AddColumn(Cover, "Cover", 1.0);
    AddColumn(Close, "Close", 1.4);
    AddColumn(Open, "Open", 1.4);
    AddColumn(High, "High", 1.4);
    AddColumn(Low, "Low", 1.4);
    AddColumn(Volume, "Volume", 1.0);
```

### Step 2: Build APX File

The `apx_builder.build_apx()` function creates an AmiBroker Project (`.apx`) XML file:

- Writes a snapshot of the AFL to `apx/live_scan_<id>.afl`
- Embeds the full AFL source in `<FormulaContent>` (XML-escaped, literal `\r\n`)
- Sets `<FormulaPath>` to the snapshot file
- Sets `<Periodicity>` to match the bar interval (e.g., 5 = 1-minute)
- Sets `<ApplyTo>` to 0 (all symbols) — the AFL's `Name() == "NQ"` filter restricts output
- Writes to `apx/live_scan_<id>.apx`

### Step 3: Run Exploration via OLE

```python
# Open the project file in AmiBroker's Analysis window
doc = ab.AnalysisDocs.Open("apx/live_scan_abc123.apx")

# Run in Exploration mode (mode=1)
doc.Run(1)

# Poll until complete (max 30 seconds)
while doc.IsBusy:
    time.sleep(0.3)

# Export results to CSV
doc.Export("apx/live_scan_abc123.csv")

# Close the analysis document
doc.Close()
```

A `_DialogAutoDismisser` thread runs during the exploration to auto-close any AmiBroker popup dialogs (e.g., "Formula is different") that would block the OLE call.

### Step 4: Parse CSV Results

AmiBroker exports the exploration results as CSV:

```csv
"Date/Time","Buy","Sell","Short","Cover","Close","Open","High","Low","Volume"
"02/18/2026 11:55:00 PM",1,0,0,0,24997.75,24996.50,24998.00,24995.25,1234
"02/18/2026 11:57:00 PM",0,1,0,0,24995.50,24997.00,24997.50,24994.75,987
"02/18/2026 11:59:00 PM",1,0,0,0,24996.50,24995.00,24997.00,24994.50,1100
```

The parser creates `Signal` objects for each row where Buy, Sell, Short, or Cover is > 0.

### Step 5: Deduplication

Signals are deduped by `"{signal_type}_{timestamp}"`. Across scans, the `_alerted_signals` set prevents the same bar's signal from being alerted twice.

### Step 6: Cleanup

All temporary files (`live_scan_*.afl`, `live_scan_*.apx`, `live_scan_*.csv`) are deleted after each scan.

---

## Alert Dispatch

For each new signal, the dispatcher fires alerts on the configured channels:

| Channel | How it works |
|---------|-------------|
| `log` | `logger.warning("SIGNAL: Buy NQ @ 24997.75 ...")` |
| `desktop` | Spawns a daemon thread that calls `ctypes.windll.user32.MessageBoxW()` |
| `sound` | `winsound.Beep(freq, duration)` — 800Hz for Buy, 400Hz for Short |
| `webhook` | Spawns a daemon thread that `POST`s JSON to the configured URL |

Deduplication: the dispatcher suppresses duplicate `(signal_type, symbol)` pairs within a 5-minute window.

---

## Trade Execution

### Signal → Trade Mapping

When the scanner returns multiple signals from the lookback window (e.g., Buy at 11:55, Sell at 11:57, Buy at 11:59), **only the signal with the latest bar timestamp is traded**. All signals are alerted, but only one trade is submitted per scan. This prevents contradictory orders (Buy + Sell) from hitting the exchange within milliseconds.

```python
# Only trade the most recent signal
latest_sig = max(signals, key=lambda s: s.timestamp)
trade_executor.submit_trade(TradeRequest(
    signal_type=latest_sig.signal_type,  # e.g. "Buy"
    symbol="NQH6",
    size=3,
    price=latest_sig.close_price,
    ...
))
```

### Order Placement

The `TradeExecutor` maps signal types to order sides:

| Signal | OrderSide | Effect |
|--------|-----------|--------|
| Buy | 0 (BUY) | Opens/adds to long position |
| Cover | 0 (BUY) | Closes short position |
| Sell | 1 (SELL) | Closes long position |
| Short | 1 (SELL) | Opens/adds to short position |

Orders are placed as **market orders** for immediate execution:

```python
resp = await order_mgr.place_market_order(
    contract_id="CON.F.US.ENQ.H26",
    side=0,       # BUY
    size=3,
    account_id=19092348,
)
# resp.success = True, resp.orderId = 2481610495
```

### Fill Detection

The ProjectX SDK's `get_order_by_id()` only searches **open** orders (`POST /Order/searchOpen`). Market orders on liquid instruments (like NQ) fill instantly and leave the open-orders list, so `get_order_by_id()` always returns `None` for filled market orders.

**Solution: Position-based fill confirmation.**

```
1. Snapshot position BEFORE placing order
      → e.g., size=0, avgPrice=0.00

2. Place market order
      → success=True, orderId=2481610495

3. Poll loop:
   a. get_order_by_id(orderId)
      → None (order already filled and left open queue)

   b. search_open_positions(account_id)
      → size=3, avgPrice=24998.50

   c. Position changed (0 → 3) → FILLED @ 24998.50

4. Return TradeResult(
      success=True,
      fill_price=24998.50,
      status="filled",
      elapsed_seconds=0.09,
      executed_at=datetime.now()   ← actual clock time
   )
```

### Timeout & Cancellation

If no fill is detected within `trade_timeout` seconds (default 30):

1. The executor calls `order_mgr.cancel_order(order_id, account_id)`
2. Returns `TradeResult(status="timeout")`

### Kill Switch

`POST /api/live/kill` sets `executor._enabled = False`. All subsequent trade requests are rejected with `status="disabled"` without placing any orders. The feed, scanning, and alerts continue normally.

---

## Dashboard Status Updates

The orchestrator communicates with the Flask dashboard through a `status_callback` function. Events flow one-way from the orchestrator to a shared `_live_state` dict that the `/api/live/status` endpoint reads.

| Event Type | When | Data |
|------------|------|------|
| `started` | Session begins | symbols, scan_interval |
| `bar_injected` | After bar injection | count |
| `feed_status` | Feed connects/disconnects | connected, message |
| `scan_complete` | After each scan | scan_num, signals_found, new_alerts |
| `alert` | New signal detected | signal_type, symbol, price, timestamp, strategy |
| `trade` | Trade result received | signal_type, fill_price, status, elapsed, executed_at |
| `error` | Any error | message |
| `stopped` | Session ends | bars_injected, scans_run, alerts_dispatched |

The dashboard polls `/api/live/status` every 2 seconds via JavaScript `setInterval()` and updates the UI metrics, alert history table, and trade history table.

---

## File Map

```
AmiTesting/
├── config/
│   ├── settings.py              # Paths: AFL_DIR, APX_DIR, DB path, COM name
│   └── live_settings.py         # Live defaults: symbols, intervals, trade config
│
├── scripts/
│   ├── live_signal_alert.py     # LiveAlertOrchestrator — main coordinator
│   ├── projectx_feed.py         # ProjectXFeed — WebSocket streaming via TradingSuite
│   ├── quote_injector.py        # QuoteInjector — AmiBroker COM bar injection
│   ├── signal_scanner.py        # SignalScanner — OLE Exploration runner
│   ├── alert_dispatcher.py      # AlertDispatcher — log/desktop/sound/webhook
│   ├── trade_executor.py        # TradeExecutor — background async order mgmt
│   ├── bar_aggregator.py        # BarData / FeedStatus dataclasses
│   ├── apx_builder.py           # APX XML file generator
│   ├── ole_bar_analyzer.py      # AFL preprocessing utilities
│   └── test_trade_flow.py       # One-shot trade verification script
│
├── dashboard/
│   ├── app.py                   # Flask routes, live state management
│   └── templates/
│       └── live_dashboard.html  # Setup form, running metrics, trade history
│
├── strategies/                  # AFL strategy files
├── afl/                         # AFL includes
├── apx/                         # Generated APX/AFL temp files (cleaned up)
└── .env                         # PROJECT_X_API_KEY, PROJECT_X_USERNAME
```

---

## External Dependencies

| System | Protocol | Purpose |
|--------|----------|---------|
| AmiBroker | COM/OLE (`win32com`) | Bar injection, Exploration scans |
| ProjectX (TopStep) | WebSocket (SignalR) | Real-time market data streaming |
| ProjectX (TopStep) | HTTPS REST | Order placement, account info, position queries |
| Windows OS | Win32 API | Desktop notifications, sound alerts |
| `.env` file | File I/O | ProjectX API credentials |
