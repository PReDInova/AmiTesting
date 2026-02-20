"""
Tests for the Flask results dashboard (dashboard/app.py).

Covers all routes (index, detail, stage, approve, reject, logs, API, download)
and the helper functions (get_result_files, parse_results_csv, get_status).
"""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

# Ensure project root is importable
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config.settings import RESULTS_DIR, LOGS_DIR
from dashboard.app import app, parse_results_csv, get_status, compute_equity_curve

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

STAGED_DIR: Path = RESULTS_DIR / "staged"

SAMPLE_CSV_CONTENT = (
    "Symbol,Trade,Date,Price,Profit,ProfitPct,Shares,EntryPrice,ExitPrice,MAE,MFE\n"
    "GCZ25,Long,2024-03-15,2180.50,1200.00,5.50,1,2180.50,2192.50,-350.00,1400.00\n"
    "GCZ25,Long,2024-06-10,2350.00,-450.00,-1.91,1,2350.00,2345.50,-600.00,200.00\n"
    "GCZ25,Long,2024-09-22,2620.00,800.00,3.05,1,2620.00,2628.00,-200.00,950.00\n"
    "GCZ25,Long,2025-01-08,2710.00,1500.00,5.54,1,2710.00,2725.00,-100.00,1700.00\n"
    "GCZ25,Long,2025-04-14,2790.00,-300.00,-1.08,1,2790.00,2787.00,-500.00,150.00\n"
)


@pytest.fixture
def client():
    """Create a Flask test client with TESTING enabled."""
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture
def sample_csv():
    """Write a sample CSV into RESULTS_DIR, yield the filename, then clean up.

    Cleans up:
    - The CSV file itself
    - Any sidecar .status.json file
    - Any copy in results/staged/
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = "test_backtest_results.csv"
    filepath = RESULTS_DIR / filename

    filepath.write_text(SAMPLE_CSV_CONTENT, encoding="utf-8")

    yield filename

    # --- Cleanup ---
    # Remove the CSV
    if filepath.exists():
        filepath.unlink()

    # Remove sidecar status file
    sidecar = filepath.parent / f"{filename}.status.json"
    if sidecar.exists():
        sidecar.unlink()

    # Remove staged copy
    staged_copy = STAGED_DIR / filename
    if staged_copy.exists():
        staged_copy.unlink()

    # Remove staged dir if empty
    if STAGED_DIR.exists() and not any(STAGED_DIR.iterdir()):
        STAGED_DIR.rmdir()


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------


def test_index_page(client):
    """GET / should return 200."""
    response = client.get("/")
    assert response.status_code == 200


def test_index_shows_strategies(client):
    """GET / should show the Strategies section (seeded DB has at least one)."""
    response = client.get("/")
    assert response.status_code == 200
    assert b"Strategies" in response.data
    # The seeded SMA Crossover strategy should appear
    assert b"SMA Crossover" in response.data


def test_index_with_results(client, sample_csv):
    """GET / should display the sample CSV filename when it exists."""
    response = client.get("/")
    assert response.status_code == 200
    assert sample_csv.encode() in response.data


def test_results_detail(client, sample_csv):
    """GET /results/<filename> should return 200 and show metrics."""
    response = client.get(f"/results/{sample_csv}")
    assert response.status_code == 200
    # The detail page should contain the filename
    assert sample_csv.encode() in response.data
    # The parsed metrics should show total trades info
    assert b"Total Trades" in response.data or b"total_trades" in response.data


def test_results_detail_not_found(client):
    """GET /results/nonexistent.csv should redirect (302) since the file
    does not exist. The app flashes an error and redirects to index."""
    response = client.get("/results/nonexistent.csv")
    # The app does a redirect (302) on missing files
    assert response.status_code == 302


def test_approve_result(client, sample_csv):
    """POST /results/<filename>/approve should write a .status.json sidecar
    with status 'approved'."""
    response = client.post(f"/results/{sample_csv}/approve", follow_redirects=True)
    assert response.status_code == 200

    sidecar = RESULTS_DIR / f"{sample_csv}.status.json"
    assert sidecar.exists(), "Sidecar .status.json was not created"

    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["status"] == "approved"
    assert "timestamp" in data
    assert data["reviewer"] == "user"


def test_reject_result(client, sample_csv):
    """POST /results/<filename>/reject should write a .status.json sidecar
    with status 'rejected'."""
    response = client.post(f"/results/{sample_csv}/reject", follow_redirects=True)
    assert response.status_code == 200

    sidecar = RESULTS_DIR / f"{sample_csv}.status.json"
    assert sidecar.exists(), "Sidecar .status.json was not created"

    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["status"] == "rejected"
    assert "timestamp" in data


def test_stage_result(client, sample_csv):
    """POST /results/<filename>/stage should copy the file to results/staged/."""
    response = client.post(f"/results/{sample_csv}/stage", follow_redirects=True)
    assert response.status_code == 200

    staged_copy = STAGED_DIR / sample_csv
    assert staged_copy.exists(), "File was not copied to results/staged/"

    # Verify the staged copy has the same content
    original = RESULTS_DIR / sample_csv
    assert staged_copy.read_text(encoding="utf-8") == original.read_text(encoding="utf-8")


def test_api_endpoint(client, sample_csv):
    """GET /api/results/<filename> should return JSON with trades and metrics."""
    response = client.get(f"/api/results/{sample_csv}")
    assert response.status_code == 200
    assert response.content_type == "application/json"

    data = response.get_json()
    assert data["filename"] == sample_csv
    assert data["error"] is None
    assert isinstance(data["trades"], list)
    assert len(data["trades"]) == 5
    assert isinstance(data["metrics"], dict)
    assert data["metrics"]["total_trades"] == 5
    assert "total_profit" in data["metrics"]
    assert "win_rate" in data["metrics"]
    assert data["status"] == "pending"


def test_api_endpoint_not_found(client):
    """GET /api/results/nonexistent.csv should return 404 JSON."""
    response = client.get("/api/results/nonexistent.csv")
    assert response.status_code == 404
    data = response.get_json()
    assert "error" in data


def test_logs_page(client):
    """GET /logs should return 200."""
    response = client.get("/logs")
    assert response.status_code == 200
    # Should contain the log heading
    assert b"ole_backtest.log" in response.data


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


def test_parse_results_csv_valid(sample_csv):
    """parse_results_csv should return trades and computed metrics for a
    valid CSV file."""
    filepath = RESULTS_DIR / sample_csv
    result = parse_results_csv(filepath)

    assert result["error"] is None
    assert len(result["trades"]) == 5
    assert "Symbol" in result["columns"]
    assert "Profit" in result["columns"]

    metrics = result["metrics"]
    assert metrics["total_trades"] == 5
    assert metrics["winning_trades"] == 3   # 1200, 800, 1500 are positive
    assert metrics["losing_trades"] == 2    # -450, -300 are negative
    assert metrics["win_rate"] == 60.0
    assert metrics["long_trades"] == 5
    assert metrics["short_trades"] == 0
    # Total profit: 1200 + (-450) + 800 + 1500 + (-300) = 2750
    assert metrics["total_profit"] == 2750.0


def test_parse_results_csv_missing_file():
    """parse_results_csv should return an error dict for a nonexistent file."""
    fake_path = RESULTS_DIR / "absolutely_nonexistent_file.csv"
    result = parse_results_csv(fake_path)

    assert result["error"] is not None
    assert "not found" in result["error"].lower() or "File not found" in result["error"]
    assert result["trades"] == []
    assert result["metrics"] == {}


def test_get_status_no_sidecar(sample_csv):
    """get_status should return 'pending' when no sidecar file exists."""
    filepath = RESULTS_DIR / sample_csv
    # Make sure there is no sidecar
    sidecar = filepath.parent / f"{sample_csv}.status.json"
    if sidecar.exists():
        sidecar.unlink()

    status = get_status(filepath)
    assert status == "pending"


# ---------------------------------------------------------------------------
# Indicator Explorer – zoom preservation tests
# ---------------------------------------------------------------------------


def test_explorer_preserves_zoom_on_param_change():
    """renderIndicators() should save and restore the visible logical range
    so that changing indicator parameters does not reset the user's zoom.

    The JS has been extracted to dashboard/static/js/indicator_explorer.js,
    with a small Jinja2 config partial at partials/indicator_explorer_js.html.
    """
    js_path = (
        Path(__file__).resolve().parent.parent
        / "dashboard"
        / "static"
        / "js"
        / "indicator_explorer.js"
    )
    js = js_path.read_text(encoding="utf-8")

    # The function must capture the current range BEFORE removing series
    assert "getVisibleLogicalRange" in js, (
        "renderIndicators must save the visible range before modifying series"
    )

    # After all series are re-added, it must restore the saved range
    assert "setVisibleLogicalRange(savedRange)" in js, (
        "renderIndicators must restore the saved range after updating series"
    )

    # Verify save happens before remove, and restore happens after setData
    save_pos = js.index("getVisibleLogicalRange")
    remove_pos = js.index("mainChart.removeSeries")
    restore_pos = js.index("setVisibleLogicalRange(savedRange)")
    set_data_positions = [
        i for i in range(len(js))
        if js[i:i + len("series.setData(")] == "series.setData("
    ]

    assert save_pos < remove_pos, (
        "Range must be saved before series are removed"
    )
    # Restore must come after at least one setData call
    assert any(pos < restore_pos for pos in set_data_positions), (
        "Range must be restored after indicator data is set"
    )


# ---------------------------------------------------------------------------
# Trade chart modal – strategy indicator tests
# ---------------------------------------------------------------------------

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "templates"


def test_run_detail_passes_indicator_configs(client):
    """The run detail route should inject indicator_configs into the rendered
    page so the trade chart modal can use strategy-specific indicators.

    After the JS refactoring, indicator_configs is passed through the
    RESULTS_CONFIG inline config object in partials/results_detail_js.html,
    which sets window.RESULTS_CONFIG.indicatorConfigs for the external JS."""
    from scripts.strategy_db import create_strategy, create_version, create_run, update_run

    afl = (
        '#include_once "indicators/tema.afl"\n'
        'Buy = Close > MA(Close, 20);\n'
        'Sell = Close < MA(Close, 20);\n'
    )
    sid = create_strategy("Test Indicator Passthrough")
    vid = create_version(sid, afl_content=afl, label="v1")
    rid = create_run(vid, sid, afl_content=afl, symbol="GCZ25")
    # Mark completed with a CSV so the page renders the trade table
    update_run(rid, status="completed", results_csv="results.csv")

    response = client.get(f"/run/{rid}")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert "indicator_configs" in html or "indicatorConfigs" in html, (
        "run_detail must pass indicator_configs to the template via RESULTS_CONFIG"
    )


def test_trade_modal_uses_strategy_indicators():
    """The trade chart modal must use strategy indicator toggles instead of
    hardcoded SMA/EMA/BBands toggles."""
    html = (TEMPLATE_DIR / "results_detail.html").read_text(encoding="utf-8")

    # Old hardcoded toggles should be gone
    assert 'id="indSMA"' not in html, (
        "Hardcoded SMA toggle should be removed"
    )
    assert 'id="indEMA"' not in html, (
        "Hardcoded EMA toggle should be removed"
    )
    assert 'id="indBBands"' not in html, (
        "Hardcoded BBands toggle should be removed"
    )
    assert "btnLoadPreset" not in html, (
        "Strategy Preset button should be removed (indicators load automatically)"
    )

    # Strategy indicator toggles should be generated from indicator_configs
    assert "indicator_configs" in html, (
        "Template must reference indicator_configs for toggle generation"
    )


def test_trade_modal_has_subpane_container():
    """The trade chart modal must have a sub-pane container for non-overlay
    indicators like RSI, ADX, Stochastic, and Derivative."""
    html = (TEMPLATE_DIR / "results_detail.html").read_text(encoding="utf-8")

    assert 'id="tradeSubPaneChart"' in html, (
        "Modal must have a sub-pane chart container"
    )
    assert 'id="tradeSubPaneCard"' in html, (
        "Modal must have a sub-pane card wrapper"
    )


def test_trade_modal_has_data_tooltip():
    """The trade chart modal must have a data tooltip element for displaying
    OHLC and indicator values at the crosshair position.

    After the JS refactoring, the tooltip DOM element stays in
    results_detail.html, while the updateTradeDataTooltip function is in
    dashboard/static/js/results_detail.js."""
    html = (TEMPLATE_DIR / "results_detail.html").read_text(encoding="utf-8")

    assert 'id="tradeDataTooltip"' in html, (
        "Modal must have a data tooltip element"
    )
    assert "trade-data-tooltip" in html, (
        "Tooltip CSS class must be defined"
    )

    js_path = (
        Path(__file__).resolve().parent.parent
        / "dashboard" / "static" / "js" / "results_detail.js"
    )
    js = js_path.read_text(encoding="utf-8")
    assert "updateTradeDataTooltip" in js, (
        "Tooltip update function must exist in external JS"
    )


def test_trade_chart_supports_overlay_and_subpane_split():
    """The chart rendering JS must split indicators into overlay (main chart)
    and sub-pane groups based on the overlay flag.

    After the JS refactoring, these functions live in
    dashboard/static/js/results_detail.js."""
    js_path = (
        Path(__file__).resolve().parent.parent
        / "dashboard" / "static" / "js" / "results_detail.js"
    )
    js = js_path.read_text(encoding="utf-8")

    assert "overlay !== false" in js or "overlay === false" in js, (
        "Chart must check indicator overlay flag to split into main/sub-pane"
    )
    assert "tradeCreateSubPane" in js, (
        "Sub-pane creation function must exist"
    )
    assert "tradeRenderIndicatorSeries" in js, (
        "Shared indicator series rendering function must exist"
    )


# ---------------------------------------------------------------------------
# Backtest trade table – sorting & column statistics tests
# ---------------------------------------------------------------------------

STRATEGIES_DIR = Path(__file__).resolve().parent.parent / "strategies"


def test_backtest_table_sortable_headers():
    """Backtest trade table headers must have the bt-sortable class and
    sort icon for click-to-sort functionality."""
    html = (TEMPLATE_DIR / "results_detail.html").read_text(encoding="utf-8")

    assert 'class="bt-sortable"' in html, (
        "Trade table headers must have bt-sortable class"
    )
    assert 'id="btTradeTable"' in html, (
        "Trade table must have btTradeTable id"
    )


def test_backtest_table_sort_js():
    """Sorting JS must exist for .bt-sortable elements in the backtest
    trade table.

    After the JS refactoring, this code lives in
    dashboard/static/js/results_detail.js."""
    js_path = (
        Path(__file__).resolve().parent.parent
        / "dashboard" / "static" / "js" / "results_detail.js"
    )
    js = js_path.read_text(encoding="utf-8")

    assert "btSortDir" in js, (
        "Sort direction state variable must exist"
    )
    assert ".bt-sortable" in js, (
        "Sort JS must reference .bt-sortable headers"
    )


def test_column_stats_modal_exists():
    """A column statistics modal must exist in the template for showing
    summary stats and a histogram when double-clicking a column header."""
    html = (TEMPLATE_DIR / "results_detail.html").read_text(encoding="utf-8")

    assert 'id="colStatsModal"' in html, (
        "Column statistics modal must exist"
    )
    assert 'id="colStatsChart"' in html, (
        "Histogram canvas must exist inside the stats modal"
    )
    assert 'id="colStatsBody"' in html, (
        "Stats table body must exist inside the stats modal"
    )


def test_column_stats_js():
    """Column statistics JS must compute mean, median, min, max, std dev
    and render a histogram via Chart.js.

    After the JS refactoring, column stats are triggered by .bt-stats-btn
    click (not dblclick) and the JS lives in results_detail.js. The stats
    button elements remain in the HTML template."""
    html = (TEMPLATE_DIR / "results_detail.html").read_text(encoding="utf-8")
    js_path = (
        Path(__file__).resolve().parent.parent
        / "dashboard" / "static" / "js" / "results_detail.js"
    )
    js = js_path.read_text(encoding="utf-8")

    # Stats buttons must exist in template
    assert "bt-stats-btn" in html, (
        "Stats button class must be present in template headers"
    )
    assert "_colStatsChartInstance" in js, (
        "Chart instance variable must exist for cleanup"
    )
    assert "new Chart(" in js, (
        "Chart.js histogram must be created"
    )
    # Verify stats computations
    assert "stdDev" in js or "Std Dev" in js, (
        "Standard deviation must be computed"
    )


def test_d02_cbt_code_present():
    """D02 AFL must contain Custom Backtest Procedure code with
    StaticVarSet, SetCustomBacktestProc, and AddCustomMetric calls."""
    afl_path = STRATEGIES_DIR / "D02_nq_deriv_tema_zerocross.afl"
    afl = afl_path.read_text(encoding="utf-8")

    assert 'SetCustomBacktestProc("")' in afl, (
        "D02 must enable inline CBT"
    )
    assert 'StaticVarSet("d02_fd_"' in afl, (
        "D02 must store firstDeriv as StaticVar"
    )
    assert 'StaticVarSet("d02_sd_"' in afl, (
        "D02 must store secondDeriv as StaticVar"
    )
    assert 'StaticVarSet("d02_ts_"' in afl, (
        "D02 must store temaSlope as StaticVar"
    )

    # Verify all 9 custom metric columns appear in the fputs CSV header
    assert "1stDeriv@Entry" in afl, "D02 must output 1stDeriv@Entry"
    assert "2ndDeriv@Entry" in afl, "D02 must output 2ndDeriv@Entry"
    assert "TEMASlope@Entry" in afl, "D02 must output TEMASlope@Entry"
    assert "1stDeriv@Exit" in afl, "D02 must output 1stDeriv@Exit"
    assert "2ndDeriv@Exit" in afl, "D02 must output 2ndDeriv@Exit"
    assert "1stDeriv Min" in afl, "D02 must output 1stDeriv Min"
    assert "1stDeriv Max" in afl, "D02 must output 1stDeriv Max"
    assert "2ndDeriv Min" in afl, "D02 must output 2ndDeriv Min"
    assert "2ndDeriv Max" in afl, "D02 must output 2ndDeriv Max"

    # Verify sidecar CSV approach
    assert 'StaticVarGetText("d02_metrics_path")' in afl, (
        "D02 must read the metrics output path from StaticVar"
    )
    assert "fopen(" in afl, "D02 must write sidecar CSV via fopen"
    assert "fputs(" in afl, "D02 must write rows via fputs"


# ---------------------------------------------------------------------------
# Symbol-filtering tests
#
# The symbol filter only activates for MULTI-symbol CSVs (e.g. __ALL__ runs).
# Single-symbol CSVs are always shown in full because AmiBroker may label
# trades with a different ticker than the one the user requested.
# ---------------------------------------------------------------------------

MULTI_SYMBOL_CSV = (
    "Symbol,Trade,Date,Price,Ex. date,Ex. Price,Profit,% Profit\n"
    "GC,Long,2024-03-15,2180.50,2024-03-16,2192.50,1200.00,5.50\n"
    "GC,Long,2024-06-10,2350.00,2024-06-11,2345.50,-450.00,-1.91\n"
    "NQ,Long,2024-03-15,17800.00,2024-03-16,17900.00,500.00,0.56\n"
    "NQ,Short,2024-06-10,18200.00,2024-06-11,18150.00,250.00,0.27\n"
    "NQ,Long,2024-09-22,19500.00,2024-09-23,19400.00,-400.00,-0.51\n"
)

# Single-symbol CSV that simulates what AmiBroker actually produces:
# run.symbol = "NQ" but AmiBroker labels every trade as "GC"
SINGLE_SYMBOL_CSV = (
    "Symbol,Trade,Date,Price,Ex. date,Ex. Price,Profit,% Profit\n"
    "GC,Long,2024-03-15,2180.50,2024-03-16,2192.50,1200.00,5.50\n"
    "GC,Long,2024-06-10,2350.00,2024-06-11,2345.50,-450.00,-1.91\n"
    "GC,Long,2024-09-22,2620.00,2024-09-23,2628.00,800.00,3.05\n"
)


@pytest.fixture
def multi_symbol_csv(tmp_path):
    """Write a multi-symbol CSV to a temp dir and return the filepath."""
    filepath = tmp_path / "multi_symbol_results.csv"
    filepath.write_text(MULTI_SYMBOL_CSV, encoding="utf-8")
    return filepath


@pytest.fixture
def single_symbol_csv(tmp_path):
    """Write a single-symbol CSV (all GC) to a temp dir."""
    filepath = tmp_path / "single_symbol_results.csv"
    filepath.write_text(SINGLE_SYMBOL_CSV, encoding="utf-8")
    return filepath


# ── Single-symbol CSV tests (the AmiBroker mismatch case) ──────────────

def test_single_symbol_csv_filter_mismatch_passes_through(single_symbol_csv):
    """A single-symbol CSV should show ALL trades even when the filter
    doesn't match (run.symbol='NQ' but CSV has only 'GC' trades)."""
    result = parse_results_csv(single_symbol_csv, symbol_filter="NQ")
    assert result["error"] is None
    assert len(result["trades"]) == 3
    assert result["metrics"]["total_trades"] == 3
    # Profits: 1200 + (-450) + 800 = 1550
    assert result["metrics"]["total_profit"] == 1550.0


def test_single_symbol_csv_filter_match_passes_through(single_symbol_csv):
    """A single-symbol CSV with a matching filter works normally."""
    result = parse_results_csv(single_symbol_csv, symbol_filter="GC")
    assert result["error"] is None
    assert len(result["trades"]) == 3


def test_single_symbol_equity_curve_mismatch(single_symbol_csv):
    """Equity curve for a single-symbol CSV should work even with
    a mismatched filter (run.symbol='NQ', CSV has 'GC')."""
    data = compute_equity_curve(single_symbol_csv, symbol_filter="NQ")
    assert data["error"] is None
    assert len(data["trade_view"]["equity"]) == 4  # Start + 3 trades
    assert data["trade_view"]["profits"] == [0, 1200.0, -450.0, 800.0]


# ── Multi-symbol CSV tests (the __ALL__ run case) ──────────────────────

def test_parse_results_csv_no_filter(multi_symbol_csv):
    """Without symbol_filter, all 5 trades are returned."""
    result = parse_results_csv(multi_symbol_csv)
    assert result["error"] is None
    assert len(result["trades"]) == 5
    assert result["metrics"]["total_trades"] == 5


def test_parse_results_csv_filter_gc(multi_symbol_csv):
    """symbol_filter='GC' on a multi-symbol CSV returns only GC trades."""
    result = parse_results_csv(multi_symbol_csv, symbol_filter="GC")
    assert result["error"] is None
    assert len(result["trades"]) == 2
    assert result["metrics"]["total_trades"] == 2
    assert all(t["Symbol"] == "GC" for t in result["trades"])
    # GC profits: 1200 + (-450) = 750
    assert result["metrics"]["total_profit"] == 750.0
    assert result["metrics"]["winning_trades"] == 1
    assert result["metrics"]["losing_trades"] == 1


def test_parse_results_csv_filter_nq(multi_symbol_csv):
    """symbol_filter='NQ' on a multi-symbol CSV returns only NQ trades."""
    result = parse_results_csv(multi_symbol_csv, symbol_filter="NQ")
    assert result["error"] is None
    assert len(result["trades"]) == 3
    assert result["metrics"]["total_trades"] == 3
    assert all(t["Symbol"] == "NQ" for t in result["trades"])
    # NQ profits: 500 + 250 + (-400) = 350
    assert result["metrics"]["total_profit"] == 350.0
    assert result["metrics"]["winning_trades"] == 2
    assert result["metrics"]["losing_trades"] == 1


def test_parse_results_csv_filter_missing_symbol(multi_symbol_csv):
    """symbol_filter for a symbol absent in a multi-symbol CSV returns error."""
    result = parse_results_csv(multi_symbol_csv, symbol_filter="ES")
    assert result["error"] is not None
    assert "ES" in result["error"]
    assert result["trades"] == []


def test_parse_results_csv_filter_case_insensitive(multi_symbol_csv):
    """symbol_filter should be case-insensitive on multi-symbol CSVs."""
    result = parse_results_csv(multi_symbol_csv, symbol_filter="gc")
    assert result["error"] is None
    assert len(result["trades"]) == 2


def test_parse_results_csv_filter_all_symbols(multi_symbol_csv):
    """symbol_filter='__ALL__' should return all trades (no filtering)."""
    result = parse_results_csv(multi_symbol_csv, symbol_filter="__ALL__")
    assert result["error"] is None
    assert len(result["trades"]) == 5


def test_equity_curve_filter_gc(multi_symbol_csv):
    """Equity curve with symbol_filter='GC' on multi-symbol CSV."""
    data = compute_equity_curve(multi_symbol_csv, symbol_filter="GC")
    assert data["error"] is None
    assert len(data["trade_view"]["equity"]) == 3  # Start + 2 GC trades
    assert data["trade_view"]["profits"] == [0, 1200.0, -450.0]


def test_equity_curve_filter_nq(multi_symbol_csv):
    """Equity curve with symbol_filter='NQ' on multi-symbol CSV."""
    data = compute_equity_curve(multi_symbol_csv, symbol_filter="NQ")
    assert data["error"] is None
    assert len(data["trade_view"]["equity"]) == 4  # Start + 3 NQ trades
    assert data["trade_view"]["profits"] == [0, 500.0, 250.0, -400.0]


def test_equity_curve_no_filter(multi_symbol_csv):
    """Equity curve without filter uses all 5 trades."""
    data = compute_equity_curve(multi_symbol_csv)
    assert data["error"] is None
    assert len(data["trade_view"]["equity"]) == 6  # Start + 5 trades


def test_equity_curve_filter_missing_symbol(multi_symbol_csv):
    """Equity curve with missing symbol on multi-symbol CSV returns error."""
    data = compute_equity_curve(multi_symbol_csv, symbol_filter="ES")
    assert data["error"] is not None
    assert "ES" in data["error"]


# ---------------------------------------------------------------------------
# __ALL__ run route tests — end-to-end Flask client tests verifying that
# the ?symbol= query parameter correctly filters per-symbol data within
# a single multi-symbol (__ALL__) run.
# ---------------------------------------------------------------------------


@pytest.fixture
def all_symbol_run(client, tmp_path):
    """Create an __ALL__ run backed by a multi-symbol CSV and return run info."""
    from scripts.strategy_db import create_strategy, create_version, create_run, update_run

    afl = 'Buy = Close > MA(Close, 20);\nSell = Close < MA(Close, 20);\n'
    sid = create_strategy("Test __ALL__ Symbol Run")
    vid = create_version(sid, afl_content=afl, label="v1")
    rid = create_run(vid, sid, afl_content=afl, symbol="__ALL__")

    # create_run auto-sets results_dir to "results/<run_id>"
    results_dir = RESULTS_DIR / rid
    results_dir.mkdir(parents=True, exist_ok=True)
    csv_path = results_dir / "results.csv"
    csv_path.write_text(MULTI_SYMBOL_CSV, encoding="utf-8")

    update_run(rid, status="completed", results_csv="results.csv")

    yield {"run_id": rid, "strategy_id": sid, "version_id": vid, "csv_path": csv_path, "results_dir": results_dir}

    # Cleanup
    if csv_path.exists():
        csv_path.unlink()
    if results_dir.exists():
        shutil.rmtree(results_dir, ignore_errors=True)


def test_all_run_no_filter_shows_all_trades(client, all_symbol_run):
    """Viewing an __ALL__ run without ?symbol= shows all trades."""
    rid = all_symbol_run["run_id"]
    response = client.get(f"/run/{rid}")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    # Should contain both GC and NQ trades (5 total from MULTI_SYMBOL_CSV)
    assert "GC" in html
    assert "NQ" in html


def test_all_run_filter_gc_shows_only_gc(client, all_symbol_run):
    """Viewing an __ALL__ run with ?symbol=GC shows only GC trades."""
    rid = all_symbol_run["run_id"]
    response = client.get(f"/run/{rid}?symbol=GC")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    # Trade log should have 2 GC trades, NOT 3 NQ trades
    # Check that the GC profit values appear
    assert "1200" in html  # GC profit
    assert "2180" in html  # GC price


def test_all_run_filter_nq_shows_only_nq(client, all_symbol_run):
    """Viewing an __ALL__ run with ?symbol=NQ shows only NQ trades."""
    rid = all_symbol_run["run_id"]
    response = client.get(f"/run/{rid}?symbol=NQ")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    # Trade log should have 3 NQ trades
    assert "17800" in html  # NQ price
    assert "500" in html  # NQ profit


def test_all_run_symbol_pills_include_csv_symbols(client, all_symbol_run):
    """__ALL__ run should show per-symbol pills extracted from the CSV."""
    rid = all_symbol_run["run_id"]
    response = client.get(f"/run/{rid}")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    # Should have pill links for GC and NQ with ?symbol= query params
    assert f"/run/{rid}?symbol=GC" in html, "Should have GC per-symbol pill link"
    assert f"/run/{rid}?symbol=NQ" in html, "Should have NQ per-symbol pill link"


def test_all_run_equity_curve_api_no_filter(client, all_symbol_run):
    """Equity curve API for __ALL__ run without ?symbol= returns all trades."""
    rid = all_symbol_run["run_id"]
    response = client.get(f"/api/run/{rid}/equity-curve")
    assert response.status_code == 200
    data = response.get_json()
    assert data["error"] is None
    # 5 trades + starting point = 6 equity points
    assert len(data["trade_view"]["equity"]) == 6


def test_all_run_equity_curve_api_filter_gc(client, all_symbol_run):
    """Equity curve API for __ALL__ run with ?symbol=GC returns only GC trades."""
    rid = all_symbol_run["run_id"]
    response = client.get(f"/api/run/{rid}/equity-curve?symbol=GC")
    assert response.status_code == 200
    data = response.get_json()
    assert data["error"] is None
    # 2 GC trades + starting point = 3 equity points
    assert len(data["trade_view"]["equity"]) == 3
    assert data["trade_view"]["profits"] == [0, 1200.0, -450.0]


def test_all_run_equity_curve_api_filter_nq(client, all_symbol_run):
    """Equity curve API for __ALL__ run with ?symbol=NQ returns only NQ trades."""
    rid = all_symbol_run["run_id"]
    response = client.get(f"/api/run/{rid}/equity-curve?symbol=NQ")
    assert response.status_code == 200
    data = response.get_json()
    assert data["error"] is None
    # 3 NQ trades + starting point = 4 equity points
    assert len(data["trade_view"]["equity"]) == 4
    assert data["trade_view"]["profits"] == [0, 500.0, 250.0, -400.0]


def test_all_run_equity_curve_url_includes_symbol(client, all_symbol_run):
    """When viewing __ALL__ run with ?symbol=GC, the equity curve JS URL
    should include the symbol parameter."""
    rid = all_symbol_run["run_id"]
    response = client.get(f"/run/{rid}?symbol=GC")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert f"/api/run/{rid}/equity-curve?symbol=GC" in html, (
        "Equity curve JS URL must include ?symbol= when viewing filtered __ALL__ run"
    )


def test_all_run_active_pill_highlights_selected_symbol(client, all_symbol_run):
    """The selected symbol pill should be highlighted (btn-primary) when
    viewing an __ALL__ run with ?symbol=GC."""
    rid = all_symbol_run["run_id"]
    response = client.get(f"/run/{rid}?symbol=GC")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    # GC pill should be btn-primary (active), NQ should be btn-outline-secondary
    gc_pill_idx = html.find(f"?symbol=GC")
    assert gc_pill_idx > 0, "GC pill link must exist"
    # The class attribute comes AFTER the href in the <a> tag, look forward
    gc_section = html[gc_pill_idx:gc_pill_idx + 200]
    assert "btn-primary" in gc_section, "GC pill should be highlighted as active"
    # NQ pill should NOT be highlighted
    nq_pill_idx = html.find(f"?symbol=NQ")
    assert nq_pill_idx > 0, "NQ pill link must exist"
    nq_section = html[nq_pill_idx:nq_pill_idx + 200]
    assert "btn-outline-secondary" in nq_section, "NQ pill should not be active"


def test_single_ticker_run_ignores_symbol_query_param(client):
    """A single-ticker (non-__ALL__) run should ignore ?symbol= query param."""
    from scripts.strategy_db import create_strategy, create_version, create_run, update_run

    afl = 'Buy = Close > MA(Close, 20);\nSell = Close < MA(Close, 20);\n'
    sid = create_strategy("Test Single Ticker Run")
    vid = create_version(sid, afl_content=afl, label="v1")
    rid = create_run(vid, sid, afl_content=afl, symbol="GC")

    results_dir = RESULTS_DIR / rid
    results_dir.mkdir(parents=True, exist_ok=True)
    csv_path = results_dir / "results.csv"
    csv_path.write_text(SINGLE_SYMBOL_CSV, encoding="utf-8")
    update_run(rid, status="completed", results_csv="results.csv")

    try:
        # Even with ?symbol=NQ, a GC run should still show all its trades
        response = client.get(f"/run/{rid}?symbol=NQ")
        assert response.status_code == 200
        html = response.data.decode("utf-8")
        # All 3 GC trades should be present (no filtering applied)
        assert "2180" in html
        assert "2350" in html
        assert "2620" in html
    finally:
        if csv_path.exists():
            csv_path.unlink()
        if results_dir.exists():
            shutil.rmtree(results_dir, ignore_errors=True)
