# Sprint 3: Strategy Builder — Completed

## Objective
Add the ability to create new trading strategies by pasting AFL code, run them through AmiBroker's OLE interface, see errors clearly, fix manually, and iterate. Each strategy is stored independently with its own AFL, APX, results, and version history.

## What Was Built

### Multi-Strategy Architecture (`scripts/strategy_manager.py`)
The project was previously hardcoded to a single strategy (`afl/ma_crossover.afl`). Sprint 3 introduces a `strategies/` directory where each strategy gets its own subdirectory:

```
strategies/
  manifest.json                           # Index of all strategies
  rsi_mean_reversion_20260207_150000/     # Example strategy
    strategy.json                         # Metadata (name, description, status, paths)
    strategy.afl                          # AFL code
    strategy.apx                          # Generated APX (gitignored)
    results.csv                           # Backtest results (gitignored)
    results.html                          # HTML report (gitignored)
    versions/                             # AFL version snapshots
```

**StrategyManager functions:**
- `create_strategy()` — creates directory + manifest entry from name, description, and AFL code
- `get_strategy()` / `list_strategies()` — read strategy metadata
- `save_strategy_afl()` / `get_strategy_afl()` — AFL file I/O
- `build_strategy_apx()` — builds APX from strategy's AFL using the shared template
- `update_strategy_status()` — transitions: draft → tested → approved
- `save_strategy_version()` / `get_strategy_versions()` — per-strategy version snapshots
- `delete_strategy()` — removes directory and manifest entry

### OLE Backtest Parameterization (`scripts/ole_backtest.py`)
`run_backtest()` and `run_full_test()` now accept optional `results_html_path` and `results_csv_path` parameters, allowing each strategy to export to its own results directory. Fully backward-compatible — omitting these params uses the original global paths.

### Dashboard: Strategy Builder Page (`/strategy-builder`)
New page accessible from the navbar. Two sections:
1. **Create New Strategy** — name input, description input, CodeMirror editor for pasting AFL code, "Create Strategy" button
2. **Your Strategies** — grid of strategy cards showing name, status badge (Draft/Tested/Approved), source, creation date, with "View & Edit" and delete buttons

### Dashboard: Strategy Detail Page (`/strategies/<id>`)
Two-column workbench layout:
- **Left column** — CodeMirror AFL editor with save, save-version, and run-backtest buttons. Version history panel below.
- **Right column** — Build/run progress with log entries. On success: equity curve (trade-based and time-based toggle), 8 metric cards (total trades, wins, losses, win rate, total profit, avg profit, max drawdown, long/short split), and full trade log table. On failure: error message with expandable build log.

Auto-refreshes every 3 seconds while a backtest is running.

### Background Thread Execution
Strategy backtests run in a daemon background thread with thread-safe state tracking (`_builder_state` + `_builder_lock`). The pipeline:
1. Build APX from AFL using `build_strategy_apx()`
2. Run backtest via subprocess calling OLE automation with strategy-specific paths
3. Validate results: CSV exists, parses correctly, has >0 trades
4. Update strategy status to "tested" on success, show error on failure

Concurrent execution prevention: checks both `_builder_lock` and `_backtest_lock` before starting.

### UI Updates
- **Navbar**: Added "Strategy Builder" link with `bi-plus-circle` icon
- **Dashboard home**: Added "Build Strategy" quick action card (4-column layout)
- **Footer**: Updated to Sprint 3

## Files Changed

| File | Change |
|------|--------|
| `config/settings.py` | Added `STRATEGIES_DIR` path constant |
| `scripts/strategy_manager.py` | **New** — multi-strategy CRUD layer (253 lines) |
| `scripts/ole_backtest.py` | Added `results_html_path`/`results_csv_path` params to `run_backtest()` and `run_full_test()` |
| `dashboard/app.py` | Added `_builder_state`, `_run_strategy_backtest_background()`, 8 new routes for strategy CRUD + backtest |
| `dashboard/templates/strategy_builder.html` | **New** — strategy creation page with CodeMirror editor |
| `dashboard/templates/strategy_detail.html` | **New** — strategy detail/edit/run workbench |
| `dashboard/templates/base.html` | Added Strategy Builder nav link, updated footer |
| `dashboard/templates/index.html` | Added Build Strategy action card, 4-column layout |
| `tests/test_strategy_manager.py` | **New** — 20 tests for strategy manager |
| `strategies/.gitkeep` | **New** — directory placeholder |
| `.gitignore` | Added strategy builder generated file patterns |

## New Routes

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/strategy-builder` | Strategy builder page |
| POST | `/strategy-builder/create` | Create new strategy from pasted AFL |
| GET | `/strategies/<id>` | Strategy detail/edit page |
| POST | `/strategies/<id>/save` | Save edited AFL |
| POST | `/strategies/<id>/run` | Run backtest for strategy |
| POST | `/strategies/<id>/delete` | Delete a strategy |
| GET | `/api/strategy-builder/status` | JSON polling for build progress |
| GET | `/api/strategies/<id>/equity-curve` | JSON equity curve data |

## Test Results
**64 tests passing** (44 existing + 20 new strategy manager tests)

## How to Use
1. Navigate to **Strategy Builder** in the navbar
2. Enter a strategy name and paste your AFL code into the editor
3. Click **Create Strategy**
4. On the detail page, click **Run Backtest** to test through AmiBroker OLE
5. If errors occur, they're displayed clearly — edit the AFL and re-run
6. Use **Save Version** to snapshot iterations
7. View equity curves, metrics, and trade logs for successful backtests
