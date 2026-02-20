# HOW TO: Trade Intelligence & Insights (Section D)

This guide covers all the Trade Intelligence features added to the AmiTesting dashboard.

---

## Navigation

All new pages are accessible from the **top navigation bar**:

| Nav Link | URL | Description |
|----------|-----|-------------|
| **Live** | `/live` | Live signal scanner with proximity dashboard |
| **Trades** | `/trades` | Trade journal with filtering and P&L details |
| **Analytics** | `/analytics` | Signal accuracy + P&L attribution analytics |
| **Replay** | `/replay` | Step through recorded live sessions |

---

## D1: Persistent Trade Journal

### What it does
Every signal detected and every trade placed during a live session is automatically persisted to the SQLite database. Previously, this data only lived in memory and was lost when the session ended.

### How to use
1. **Start a live session** from the **Live** page (`/live`)
2. Signals and trades are automatically recorded as they occur
3. After the session ends, view them on the **Trades** page (`/trades`)

### Trades Page (`/trades`)
- Lists all trades across all live sessions, newest first
- Filter by session, strategy, or symbol
- Each trade shows: signal type, symbol, signal price, fill price, P&L, status, elapsed time
- Click on a trade row to see full indicator values at the time of the signal

### API Endpoints
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/trades` | GET | List trades with filters (`session_id`, `strategy_id`, `symbol`, `limit`) |
| `/api/signals` | GET | List signals with filters (`session_id`, `strategy_id`, `signal_type`, `limit`) |
| `/api/trades/daily-pnl` | GET | Get daily realized P&L (`date`, `strategy_id`) |

---

## D2: Proximity-to-Signal Dashboard

### What it does
Shows how close each AFL condition is to triggering a Buy or Short signal in real-time. Parses `Param()` calls and Buy/Short condition expressions from your AFL strategy file.

### How to use
1. Start a live session from **Live** (`/live`)
2. Once the scanner runs and indicator data appears, scroll to the **"Proximity to Signal"** panel
3. Progress bars show each condition's proximity:
   - **Green** = condition is met
   - **Yellow** = condition is close to triggering (>80%)
   - **Gray** = condition is far from threshold
4. Buy conditions are on the left, Short conditions on the right

### What gets parsed
The system extracts:
- `Param("label", default, min, max, step)` calls from AFL
- `Buy = expr1 AND expr2;` and `Short = expr1 AND expr2;` conditions
- Comparisons like `ADXvalue > adxThreshold`

### API Endpoint
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/live/proximity` | GET | Get proximity data for the active session |

---

## D3: Signal Accuracy Tracking

### What it does
Analyzes signal quality by tracking:
- Total signals, traded signals, deduped signals
- Signal breakdown by type (Buy/Short) and by hour of day
- Profitability: joins signals to subsequent trades to compute win rate, avg P&L, total P&L per signal type

### How to use
1. Navigate to **Analytics** (`/analytics`)
2. The **Signal Accuracy** section shows:
   - Overall totals (total signals, traded, deduped)
   - By-type breakdown with count and traded percentage
   - Profitability table: wins, losses, win rate, total P&L, avg P&L per signal type
   - Signal activity by hour (visual bar chart across 24 hours)
3. Use the **Strategy** dropdown to filter by a specific strategy
4. Use the **Date Range** dropdown (7d, 30d, 90d, All) to adjust the time window

### API Endpoint
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/signals/accuracy` | GET | Get signal accuracy metrics (`strategy_id`, `days`) |

---

## D4: P&L Attribution

### What it does
Breaks down realized P&L by multiple dimensions to identify which strategies, symbols, times of day, and days of the week are most profitable.

### How to use
1. Navigate to **Analytics** (`/analytics`)
2. The **P&L Attribution** section at the top shows four tables:
   - **By Strategy** — P&L for each strategy name
   - **By Symbol** — P&L for each traded symbol
   - **By Hour** — P&L by hour of day (identifies best trading hours)
   - **By Weekday** — P&L by day of week (0=Sun through 6=Sat)
3. Each table shows: group key, trade count, wins, total P&L, avg P&L
4. Visual PnL bars show relative magnitude (green for profit, red for loss)
5. Summary cards at the top show: Total P&L, Total Trades, Win Rate, Total Signals

### API Endpoints
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/trades/pnl` | GET | Get P&L attribution (`days`, `group_by`: strategy/symbol/hour/weekday) |
| `/api/trades/daily-pnl` | GET | Get daily total P&L |

---

## D5: Signal Replay

### What it does
Lets you step through a recorded live session event by event, seeing exactly what happened at each signal point. Includes:
- Chronological timeline of all signals and trades
- Indicator snapshots at each event
- Condition re-evaluation (compare actual vs. hypothetical signals)
- Match/mismatch indicators showing if conditions agree with actual signals

### How to use
1. Navigate to **Replay** (`/replay`)
2. Select a recorded live session from the dropdown
3. Click **Load Session for Replay**
4. Use the replay controls:
   - **Step** — advance one event
   - **5x** — advance five events
   - **Step back** (skip-start icon) — go back one event
   - **Reset** (skip-backward icon) — go to start
   - **End** (skip-end icon) — jump to last event
5. The **Current Event** panel shows:
   - Signal type and badge
   - Price, symbol, timestamp
   - Trade details (fill price, P&L, status) for trade events
   - Indicator snapshot at that point in time
6. The **Condition Proximity** panel shows how close each AFL condition was to triggering at that event
7. The **Event Timeline** on the right shows all events with color coding:
   - Green border = Buy signal
   - Red border = Short signal
   - Blue border = Trade
   - Check/X icons show match/mismatch with re-evaluated conditions
8. Click any event in the timeline to jump directly to it

### Summary metrics at the top show:
- Total events, signals, trades
- Total P&L across all trades
- Match Rate (if AFL re-evaluation is available)

### API Endpoints
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/replay/start` | POST | Start replay (`session_id`, optional `param_overrides`) |
| `/api/replay/step` | POST | Step forward/back (`num_bars`, `direction`: forward/back/jump/reset/end) |
| `/api/replay/events` | GET | Get all loaded events |
| `/api/replay/summary` | GET | Get replay summary stats |

---

## Quick Reference: All New Files

| File | Purpose |
|------|---------|
| `scripts/trade_replay.py` | Signal replay engine (TradeReplay class) |
| `dashboard/routes/trades.py` | Trade journal, analytics, signal accuracy routes |
| `dashboard/routes/live.py` | Live session + replay routes |
| `dashboard/templates/analytics.html` | Analytics dashboard template |
| `dashboard/templates/replay.html` | Signal replay template |
| `dashboard/templates/trades.html` | Trade journal template (pre-existing) |
| `tests/test_trade_intelligence.py` | 60 tests covering all D1-D5 features |

## Quick Reference: Database Tables

| Table | Purpose |
|-------|---------|
| `live_sessions` | Persistent session metadata (strategy, account, counters) |
| `live_trades` | Every trade placed (signal price, fill price, P&L, indicators) |
| `live_signals` | Every signal detected (type, price, indicators, was_traded, was_deduped) |
