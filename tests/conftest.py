"""
Shared pytest fixtures for the AmiTesting test suite.

Provides temporary AFL files, APX templates, mock COM objects, and sample
CSV data used across multiple test modules.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure the project root is importable
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Temporary AFL file
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_afl_file(tmp_path):
    """Write a minimal AFL strategy to a temp file and return its path."""
    afl_content = (
        "// Test AFL Strategy\n"
        "Buy = Cross(MA(Close,10), MA(Close,50));\n"
        "Sell = Cross(MA(Close,50), MA(Close,10));\n"
        "SetPositionSize(1, spsShares);\n"
    )
    afl_file = tmp_path / "test_strategy.afl"
    afl_file.write_text(afl_content, encoding="utf-8")
    return afl_file


# ---------------------------------------------------------------------------
# APX template WITH FormulaContent element
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_apx_template(tmp_path):
    """Write a minimal valid APX XML template with a <FormulaContent> element."""
    xml_content = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
        '<AmiBroker-Analysis CompactMode="0">\n'
        "<General>\n"
        "<FormulaPath></FormulaPath>\n"
        "<FormulaContent></FormulaContent>\n"
        "</General>\n"
        "<BacktestSettings>\n"
        "<InitialEquity>100000</InitialEquity>\n"
        "<ReverseSignalForcesExit>1</ReverseSignalForcesExit>\n"
        "</BacktestSettings>\n"
        "</AmiBroker-Analysis>\n"
    )
    template_file = tmp_path / "base.apx"
    template_file.write_text(xml_content, encoding="utf-8")
    return template_file


# ---------------------------------------------------------------------------
# APX template WITHOUT FormulaContent element
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_apx_template_no_formula(tmp_path):
    """Write an APX XML template that is missing the <FormulaContent> element."""
    xml_content = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
        '<AmiBroker-Analysis CompactMode="0">\n'
        "<General>\n"
        "<FormulaPath></FormulaPath>\n"
        "</General>\n"
        "</AmiBroker-Analysis>\n"
    )
    template_file = tmp_path / "no_formula.apx"
    template_file.write_text(xml_content, encoding="utf-8")
    return template_file


# ---------------------------------------------------------------------------
# Mock COM application object (simulates AmiBroker OLE)
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_com_app():
    """Return a MagicMock that simulates the AmiBroker COM object.

    Structure:
        app.Visible        -- writable attribute
        app.LoadDatabase() -- callable
        app.AnalysisDocs.Open() -> analysis_doc mock
        app.Quit()         -- callable

    The analysis_doc mock exposes:
        .Run()    -- callable
        .IsBusy   -- property: True on first access, False on second
        .Export() -- callable
        .Close()  -- callable
    """
    app = MagicMock(name="AmiBrokerApp")

    # Build the analysis_doc mock
    analysis_doc = MagicMock(name="AnalysisDoc")

    # IsBusy starts True, then toggles to False on the second access
    busy_sequence = iter([True, False])
    type(analysis_doc).IsBusy = property(lambda self: next(busy_sequence, False))

    app.AnalysisDocs.Open.return_value = analysis_doc
    app.Visible = 0  # will be overwritten by connect()

    return app


# ---------------------------------------------------------------------------
# Sample results CSV
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_results_csv(tmp_path):
    """Write a sample backtest-results CSV with 5 /GC trades and return the path."""
    header = "Symbol,Trade,Date,Price,Profit,ProfitPct,Shares,EntryPrice,ExitPrice,MAE,MFE"
    rows = [
        "GCZ25,Long,2025-06-01,2350.00,500.00,2.13,1,2350.00,2400.00,-150.00,600.00",
        "GCZ25,Short,2025-06-15,2400.00,300.00,1.25,1,2400.00,2370.00,-100.00,450.00",
        "GCZ25,Long,2025-07-01,2370.00,-200.00,-0.84,1,2370.00,2350.00,-250.00,100.00",
        "GCZ25,Short,2025-07-15,2350.00,450.00,1.91,1,2350.00,2305.00,-80.00,500.00",
        "GCZ25,Long,2025-08-01,2305.00,700.00,3.04,1,2305.00,2375.00,-50.00,750.00",
    ]
    csv_content = header + "\n" + "\n".join(rows) + "\n"
    csv_file = tmp_path / "results.csv"
    csv_file.write_text(csv_content, encoding="utf-8")
    return csv_file
