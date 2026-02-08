# Sprint 2 Completion Report: APX Format Fix & Infrastructure

**Sprint Duration**: February 7, 2026
**Branch**: `fix/apx-format` (9 commits, 15 files changed, +1,504 / -213 lines)
**Status**: Complete
**Test Suite**: 80 tests passing (up from 44)

---

## What Prompted This Sprint

After Sprint 1 delivered the OLE automation pipeline, attempting to actually run a backtest through AmiBroker revealed that the generated `.apx` files were rejected. The root cause was that the `base.apx` template used a fabricated XML schema (`<AmiBrokerProject>`) that doesn't match AmiBroker's actual format (`<AmiBroker-Analysis>`). This sprint fixed the APX format through iterative debugging against a real AmiBroker-generated reference file, then expanded into infrastructure improvements (strategy database, AFL validation) that surfaced during the process.

---

## What Was Completed

### 1. APX Format Fix (3 commits)

**Problem**: AmiBroker error "Failed to open file... The file may have incorrect format"

**Root causes discovered** (by comparing our output byte-for-byte against an AmiBroker-generated reference file):

| # | Issue | Impact |
|---|-------|--------|
| 1 | Wrong XML schema: `<AmiBrokerProject SchemaVersion="1">` | AmiBroker expects `<AmiBroker-Analysis CompactMode="0">` with `<BacktestSettings>` (not `<Backtest>`) and ~200 settings elements |
| 2 | Empty `<FormulaPath>` | AmiBroker defaults to `Formulas\Imported\` and fails with "Can not open formula file" |
| 3 | Real CRLF bytes in `<FormulaContent>` | AmiBroker stores newlines as literal 4-char `\r\n` text, not as 0x0D 0x0A bytes |

**Files changed**:
- `apx/base.apx` — Replaced entirely with real AmiBroker schema (derived from reference file)
- `scripts/apx_builder.py` — Complete rewrite: ElementTree replaced with direct byte splicing to preserve exact template format. Now injects `FormulaPath` (absolute AFL path with doubled backslashes) and escapes `FormulaContent` newlines to literal `\r\n` text.
- `apx/gcz25_test.apx` — Rebuilt from corrected template

### 2. OLE Validation Step (1 commit)

**Problem**: No way to verify APX format before running a backtest

**Solution**: Added `validate_apx()` method to `OLEBacktester` that opens the APX via `ab.AnalysisDocs.Open()` and returns `(bool, reason)`. Integrated into `run_full_test()` as a gate before backtest execution.

**Files changed**:
- `scripts/ole_backtest.py` — Added `validate_apx()`, updated `run_full_test()` to validate first

### 3. AmiBroker Executable Path (1 commit)

**Problem**: Two AmiBroker installations existed (`C:\Program Files` and `C:\Program Files (x86)`). `Dispatch("Broker.Application")` was connecting to the wrong one via COM registry.

**Solution**: Added `AMIBROKER_EXE_PATH` setting and updated `connect()` to explicitly launch the correct executable via `subprocess.Popen` before COM dispatch.

**Files changed**:
- `config/settings.py` — Added `AMIBROKER_EXE_PATH`
- `scripts/ole_backtest.py` — Updated `connect()` to launch exe before dispatch

### 4. Tick Data SMA Strategy (2 commits)

**Problem**: Original MA(10)/MA(50) strategy was designed for daily bars. With tick data, it either produced no signals or Error 702 (missing `Short`/`Cover` variables).

**Solution**: Rewrote `ma_crossover.afl` to:
- Aggregate ticks into 1-minute bars via `TimeFrameSet(in1Minute)`
- Apply 10-min fast / 30-min slow SMA crossover
- Expand signals back to tick timeframe with `TimeFrameRestore()`
- Add `Short = 0; Cover = 0;` for long-only (fixes Error 702)

**Files changed**:
- `afl/ma_crossover.afl` — Rewritten for tick data
- `dashboard/app.py` — Updated strategy description to match

### 5. Strategy Database (1 commit)

**Problem**: Strategy metadata (name, description, parameters, symbol, risk notes) was hardcoded as a 50-line Python dict in `dashboard/app.py`.

**Solution**: SQLite database at `data/strategies.db` with full CRUD operations.

| Function | Purpose |
|----------|---------|
| `init_db()` | Creates table if not exists |
| `upsert_strategy()` | Insert or update by results_file key |
| `get_strategy()` | Fetch single strategy |
| `list_strategies()` | All strategies, most recent first |
| `delete_strategy()` | Remove by results_file |
| `get_strategy_info()` | Dashboard helper with default fallback |
| `seed_default_strategies()` | Auto-populates SMA crossover on first run |

**Schema**: `id`, `results_file` (unique key), `name`, `summary`, `description`, `parameters_json`, `symbol`, `risk_notes`, `afl_file`, `apx_file`, `created_at`, `updated_at`

**Files changed**:
- `scripts/strategy_db.py` — New module (231 lines)
- `dashboard/app.py` — Removed hardcoded dict, imports from strategy_db
- `.gitignore` — Added `data/*.db` patterns

### 6. AFL Validator (1 commit)

**Problem**: AmiBroker's OLE interface does not report AFL formula errors — it silently produces empty results. The Error 702 (missing Short/Cover) was only visible in the AmiBroker GUI, not through OLE.

**Solution**: Two-layer validation in `scripts/afl_validator.py`:

**Pre-validation** (before sending to AmiBroker):

| Check | Catches |
|-------|---------|
| Missing `Buy`/`Sell`/`Short`/`Cover` | Error 702 |
| Empty formula | Blank or comments-only AFL |
| No semicolons | AFL syntax requires `;` |
| Unmatched parentheses | Typos in nested function calls |
| Non-ISO-8859-1 characters | Unicode that AmiBroker can't handle |

**Post-validation** (after backtest):

| Check | Catches |
|-------|---------|
| CSV missing | Silent OLE failure |
| CSV empty (0 bytes) | AFL errors OLE didn't report |
| Header-only CSV (0 trades) | Signals never triggered |
| Missing/empty HTML | Partial export failure |

**Files changed**:
- `scripts/afl_validator.py` — New module (177 lines)
- `tests/test_afl_validator.py` — 22 tests

---

## Test Suite Summary

| Test File | Count | What It Validates |
|-----------|-------|-------------------|
| `test_settings.py` | 9 | Configuration paths, backtest params, COM name |
| `test_apx_builder.py` | 7 | APX generation with real AmiBroker-Analysis schema |
| `test_ole_backtest.py` | 10 | OLE connect/disconnect, database loading, backtest, timeout, validation step |
| `test_integration.py` | 4 | End-to-end pipeline with mocked OLE (2 Open/Close calls for validate + backtest) |
| `test_dashboard.py` | 14 | All dashboard routes, CSV parsing, status management |
| `test_strategy_db.py` | 14 | SQLite CRUD: insert, upsert, get, list, delete, seed, default fallback |
| `test_afl_validator.py` | 22 | Good scripts, missing vars, syntax errors, encoding, file validation, result validation |
| **Total** | **80** | |

---

## Commit History

```
27cc905 Add AFL validator and fix Error 702 (missing Short/Cover)
9e73673 Replace hardcoded strategy descriptions with SQLite database
167e8ca Update dashboard strategy description for tick data SMA crossover
3244c1a Update SMA crossover strategy for tick data
531709c Launch specific AmiBroker executable before COM dispatch
935e69c Fix FormulaPath and FormulaContent encoding in APX builder
07c16e7 Replace base.apx with real AmiBroker-Analysis schema
c42622e Rewrite APX builder to string substitution, add OLE validation step
0f7f8f4 Fix APX file format for AmiBroker compatibility
```

---

## What's Left to Build

### Immediate (should be done next)

1. **Wire AFL validator into the backtest pipeline** — `afl_validator.validate_afl_file()` should run before `build_apx()` in both the CLI entry point (`run.py`) and the dashboard's backtest route. `validate_backtest_results()` should run after `run_backtest()` completes. Currently the validator module exists but is not yet called from the pipeline.

2. **Wire AFL validator into the dashboard AFL editor** — When a user edits AFL in the CodeMirror editor and saves, run `validate_afl()` on the content and display errors/warnings before allowing a backtest to start.

3. **Verify live backtest end-to-end** — The APX format fix, COM registration (`/regserver`), and tick-data strategy are all in place but haven't been confirmed to produce actual trade results through the full OLE pipeline. This needs manual verification with AmiBroker running.

### Near-term (Sprint 3 scope)

4. **Strategy Builder UI** — The Sprint 3 branch (`sprint3-strategy-builder`) added a strategy builder page, multi-strategy support via `strategies/` directory, and per-strategy backtest routes. This work needs to be rebased onto the `fix/apx-format` changes (especially the new `strategy_db.py` which supersedes the old `strategy_manager.py`).

5. **Dashboard CRUD for strategies** — Add routes to create/edit/delete strategies through the UI, backed by `strategy_db.py`. The DB layer is ready; the UI routes are not.

6. **Per-strategy result storage** — Currently results are hardcoded to `results/results.csv`. Each strategy should store its own results (the `results_file` column in the DB supports this; the `ole_backtest.py` parameterized paths from Sprint 3 support this).

### Future direction

7. **Optimization runs** — AmiBroker supports parameter optimization via `analysis_doc.Run(4)`. The OLE infrastructure is ready; need AFL `Optimize()` calls and result parsing.

8. **Error feedback loop** — When `validate_backtest_results()` detects 0 trades, surface the issue in the dashboard with actionable suggestions (e.g., "Buy/Sell conditions never triggered — check if data covers the strategy's timeframe").

9. **Multi-symbol support** — The `<Symbol>` field in the APX template is currently empty. Populate it from the strategy DB so different strategies can target different instruments.

10. **Automated regression testing** — Add a CI-compatible test that builds an APX, validates it byte-for-byte against the reference format, and confirms the AFL validator catches known-bad scripts.
