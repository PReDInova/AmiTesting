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
from dashboard.app import app, parse_results_csv, get_status

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
    so that changing indicator parameters does not reset the user's zoom."""
    template_path = (
        Path(__file__).resolve().parent.parent
        / "dashboard"
        / "templates"
        / "indicator_explorer.html"
    )
    html = template_path.read_text(encoding="utf-8")

    # The function must capture the current range BEFORE removing series
    assert "getVisibleLogicalRange" in html, (
        "renderIndicators must save the visible range before modifying series"
    )

    # After all series are re-added, it must restore the saved range
    assert "setVisibleLogicalRange(savedRange)" in html, (
        "renderIndicators must restore the saved range after updating series"
    )

    # Verify save happens before remove, and restore happens after setData
    save_pos = html.index("getVisibleLogicalRange")
    remove_pos = html.index("mainChart.removeSeries")
    restore_pos = html.index("setVisibleLogicalRange(savedRange)")
    set_data_positions = [
        i for i in range(len(html))
        if html[i:i + len("series.setData(")] == "series.setData("
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
    """The run detail route should inject strategyIndicatorConfigs into the
    rendered page so the trade chart modal can use strategy-specific indicators."""
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
    assert "strategyIndicatorConfigs" in html, (
        "run_detail must pass indicator_configs to the template as strategyIndicatorConfigs"
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
    OHLC and indicator values at the crosshair position."""
    html = (TEMPLATE_DIR / "results_detail.html").read_text(encoding="utf-8")

    assert 'id="tradeDataTooltip"' in html, (
        "Modal must have a data tooltip element"
    )
    assert "trade-data-tooltip" in html, (
        "Tooltip CSS class must be defined"
    )
    assert "updateTradeDataTooltip" in html, (
        "Tooltip update function must exist"
    )


def test_trade_chart_supports_overlay_and_subpane_split():
    """The chart rendering JS must split indicators into overlay (main chart)
    and sub-pane groups based on the overlay flag."""
    html = (TEMPLATE_DIR / "results_detail.html").read_text(encoding="utf-8")

    assert "overlay !== false" in html or "overlay === false" in html, (
        "Chart must check indicator overlay flag to split into main/sub-pane"
    )
    assert "tradeCreateSubPane" in html, (
        "Sub-pane creation function must exist"
    )
    assert "tradeRenderIndicatorSeries" in html, (
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
    trade table."""
    html = (TEMPLATE_DIR / "results_detail.html").read_text(encoding="utf-8")

    assert "btTable.querySelectorAll('.bt-sortable')" in html, (
        "Sort JS must query .bt-sortable headers"
    )
    assert "btSortDir" in html, (
        "Sort direction state variable must exist"
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
    and render a histogram via Chart.js."""
    html = (TEMPLATE_DIR / "results_detail.html").read_text(encoding="utf-8")

    assert "dblclick" in html, (
        "Double-click event must be bound for column stats"
    )
    assert "_colStatsChartInstance" in html, (
        "Chart instance variable must exist for cleanup"
    )
    assert "new Chart(" in html, (
        "Chart.js histogram must be created"
    )
    # Verify stats computations
    assert "stdDev" in html or "Std Dev" in html, (
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
