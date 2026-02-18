# Live Signal Alert System: ProjectX → AmiBroker → Trade Alerts

## Context

The project currently runs historical backtests through AmiBroker via OLE/COM automation. The goal is to extend this to **live trading signals**: stream real-time market data from ProjectX (CME futures via `project-x-py` SDK), inject it into AmiBroker, run existing AFL strategies against live bars, and fire alerts when Buy/Short signals trigger.

This enables using the exact same AFL strategies (e.g., D02 NQ Derivative TEMA Zero-Cross) for live signal detection without reimplementing indicator logic in Python.

---

## Architecture

```
ProjectX WebSocket (async thread)     Main Thread (COM)
─────────────────────────────────     ─────────────────
                                      pythoncom.CoInitialize()
ProjectXRealtimeClient                      │
  ↓ live bars                               │
  queue.Queue ──────────────────────→ QuoteInjector
                                        │ ab.Stocks(sym).Quotations.Add(date)
                                        │ ab.RefreshAll()
                                        ↓
                                      SignalScanner (every N seconds)
                                        │ build exploration AFL
                                        │ AnalysisDocs.Open(apx) → Run(1)
                                        │ Export CSV → parse Buy/Short
                                        ↓
                                      AlertDispatcher
                                        │ log / desktop toast / sound / webhook
                                        ↓
                                      Console + log output
```

**Threading model**: Async ProjectX feed runs on a background thread. All COM calls (injection, scanning) happen on the main thread. A `queue.Queue` bridges the two.

---

## New Files to Create

### 1. `config/live_settings.py` — Live mode configuration
- ProjectX symbols, bar interval, AmiBroker injection target
- Scan interval, lookback bars, strategy AFL path
- Alert channels, dedup window, sound/webhook settings
- Reconnection and COM retry parameters

### 2. `scripts/projectx_feed.py` — ProjectX data feed adapter
- `ProjectXFeed` class: wraps `project-x-py` async SDK
- Runs async event loop on background thread
- Authenticates via `ProjectX.from_env()` (reads `PROJECT_X_API_KEY`, `PROJECT_X_USERNAME`)
- Subscribes to live bar data via `ProjectXRealtimeDataManager`
- Puts completed `BarData(symbol, timestamp, O, H, L, C, V)` onto `queue.Queue`
- Posts `FeedStatus` messages for connectivity monitoring
- Handles reconnection with exponential backoff

### 3. `scripts/bar_aggregator.py` — Tick-to-bar fallback
- `BarAggregator` class: accumulates ticks into fixed-interval OHLCV bars
- Only needed if SDK's `ProjectXRealtimeDataManager` doesn't handle bar aggregation
- Emits `BarData` via callback on bar close

### 4. `scripts/quote_injector.py` — AmiBroker OLE quote injection
- `QuoteInjector` class: adds bars to AmiBroker via COM
- Reuses COM connection pattern from `scripts/ole_stock_data.py:StockDataFetcher`
- Reuses `_OLE_EPOCH` and date conversion (inverse of `_com_date_to_datetime` at `ole_stock_data.py:45`)
- Key COM calls: `ab.Stocks(symbol).Quotations.Add(com_date)` → set `.Open/.High/.Low/.Close/.Volume`
- `refresh_all()` calls `ab.RefreshAll()` after batch injection
- COM retry logic following `ole_stock_data.py` pattern (retry on `RPC_E_SERVERFAULT`)
- Duplicate bar detection before injection

### 5. `scripts/signal_scanner.py` — Periodic exploration-based signal detection
- `SignalScanner` class: runs AFL strategy as Exploration to detect signals
- Reuses these functions directly from `scripts/ole_bar_analyzer.py`:
  - `_strip_trading_directives` (line 408) — removes Plot/ApplyStop/SetPositionSize
  - `_replace_params_with_values` (line 368) — hardcodes Param() values
  - `_expand_includes` (line 268) — inlines `#include_once` directives
  - `_process_isempty_guards` (line 151) — handles IsEmpty() guards
  - `_DialogAutoDismisser` (line 64) — auto-dismiss AmiBroker dialogs
- Reuses `scripts/apx_builder.py:build_apx()` for APX generation
- Generates exploration AFL from strategy AFL by:
  - Stripping trading directives
  - Replacing Param() with hardcoded values
  - Adding `Filter = (Buy OR Short) AND BarIndex() >= (BarCount - N)`
  - Adding `AddColumn(Buy/Short/Close/indicators)`
- Runs exploration via OLE: `AnalysisDocs.Open(apx)` → `Run(1)` → `Export(csv)`
- Parses CSV for `Signal(type, symbol, timestamp, price)` objects
- Cleans up temp APX/AFL/CSV files after each scan

### 6. `scripts/alert_dispatcher.py` — Multi-channel alert routing
- `AlertDispatcher` class with pluggable channels:
  - **log**: Python logger at WARNING level
  - **desktop**: Windows toast notification (via `ctypes` user32 API)
  - **sound**: `winsound.Beep()` or `.PlaySound()` for .wav files
  - **webhook**: HTTP POST via `urllib.request` (non-blocking, daemon thread)
- Signal deduplication: sliding window (default 5 min) prevents duplicate alerts
- Alert history tracking for dashboard display

### 7. `scripts/live_signal_alert.py` — Main orchestrator & CLI entry point
- `LiveAlertOrchestrator` class: wires all components together
- Startup sequence:
  1. `pythoncom.CoInitialize()` on main thread
  2. Start `_DialogAutoDismisser`
  3. Connect `QuoteInjector` to AmiBroker
  4. Initialize `SignalScanner`
  5. Initialize `AlertDispatcher`
  6. Start `ProjectXFeed` (background async thread)
  7. Enter main loop
- Main loop (on COM thread):
  1. Drain bar queue → inject into AmiBroker → refresh
  2. Process feed status messages (log connectivity)
  3. If scan interval elapsed → run signal scan → dispatch alerts
  4. Sleep 100ms to avoid busy-wait
- Graceful shutdown on Ctrl+C (SIGINT)
- CLI args: `--symbol`, `--interval`, `--scan-interval`, `--strategy`, `--alerts`
- Usage: `python -m scripts.live_signal_alert --symbol NQ --interval 60`

## Existing Files to Modify

### `requirements.txt`
- Add: `project-x-py>=3.5.9`
- Add: `python-dotenv>=1.0.0` (for `.env` file support with ProjectX credentials)

### `config/settings.py`
- Add `LIVE_SETTINGS` dict with sensible defaults (or just import from `live_settings.py`)

---

## Implementation Phases

**Phase 1 — Config + Quote Injection** (test with synthetic data)
- Create `config/live_settings.py`
- Create `scripts/quote_injector.py`
- Verify bars appear in AmiBroker by injecting test data manually

**Phase 2 — ProjectX Feed** (test with live stream → console)
- `pip install project-x-py python-dotenv`
- Create `.env` with `PROJECT_X_API_KEY` and `PROJECT_X_USERNAME`
- Create `scripts/projectx_feed.py` + `scripts/bar_aggregator.py`
- Test: stream live NQ bars and print to console

**Phase 3 — Signal Scanner** (test against historical data already in AmiBroker)
- Create `scripts/signal_scanner.py`
- Test: run scan against existing AmiBroker data, verify Buy/Short detection

**Phase 4 — Alerts**
- Create `scripts/alert_dispatcher.py`
- Test: fire test alerts through each channel

**Phase 5 — Orchestrator** (end-to-end)
- Create `scripts/live_signal_alert.py`
- Wire everything together
- End-to-end test with live ProjectX feed → AmiBroker → signal alerts

---

## Verification Plan

1. **Unit test quote injection**: Inject 5 synthetic bars → read them back via `ole_stock_data.py` → verify OHLCV matches
2. **Unit test signal scanner**: Run scan against existing historical data → verify it finds known signals from past backtests
3. **Unit test alert dispatcher**: Fire test events → verify log output, sound plays, desktop notification appears
4. **Integration test**: Run `live_signal_alert.py` with `--symbol NQ --interval 60` during market hours → verify:
   - Bars stream in from ProjectX (check log)
   - Bars appear in AmiBroker (open AmiBroker chart, see new candles)
   - Signal scans run every 60s (check log)
   - Alerts fire when Buy/Short conditions are met
5. **Resilience test**: Kill and restart AmiBroker mid-stream → verify reconnection and recovery
