"""
Tests for scripts.ole_stock_data -- OHLCV data fetching from AmiBroker.

All tests are fully mocked; no real AmiBroker installation is required.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure the project root is importable
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# OLE Automation Date epoch
_OLE_EPOCH = datetime(1899, 12, 30)


def _datetime_to_com_date(dt: datetime) -> float:
    """Convert a Python datetime to an OLE Automation Date (float)."""
    return (dt - _OLE_EPOCH).total_seconds() / 86400


def _make_mock_quotation(dt: datetime, o: float, h: float, l: float, c: float, v: float = 100):
    """Build a MagicMock that looks like a single AmiBroker Quotation bar."""
    q = MagicMock()
    q.Date = _datetime_to_com_date(dt)
    q.Open = o
    q.High = h
    q.Low = l
    q.Close = c
    q.Volume = v
    return q


def _make_mock_stock(symbol: str, quotations: list):
    """Build a MagicMock that looks like an AmiBroker Stock object."""
    stock = MagicMock()
    stock.Quotations.Count = len(quotations)
    stock.Quotations.side_effect = lambda i: quotations[i]
    return stock


# ---------------------------------------------------------------------------
# COM date conversion tests
# ---------------------------------------------------------------------------

class TestComDateConversion:
    """Verify OLE Automation Date to Python datetime conversion."""

    def test_known_date(self):
        from scripts.ole_stock_data import _com_date_to_datetime

        # Jan 1, 2025 midnight => (2025-01-01 - 1899-12-30) = 45_658 days
        dt = _com_date_to_datetime(45658.0)
        assert dt.year == 2025
        assert dt.month == 1
        assert dt.day == 1
        assert dt.hour == 0

    def test_fractional_date(self):
        from scripts.ole_stock_data import _com_date_to_datetime

        # Half a day = noon
        dt = _com_date_to_datetime(45658.5)
        assert dt.hour == 12
        assert dt.minute == 0

    def test_round_trip(self):
        from scripts.ole_stock_data import _com_date_to_datetime

        original = datetime(2025, 7, 21, 1, 14, 50)
        com_date = _datetime_to_com_date(original)
        result = _com_date_to_datetime(com_date)
        # Allow 1-second tolerance due to float precision
        assert abs((result - original).total_seconds()) < 1


# ---------------------------------------------------------------------------
# StockDataFetcher tests
# ---------------------------------------------------------------------------

class TestStockDataFetcher:
    """Tests for the StockDataFetcher class."""

    @patch("win32com.client.Dispatch")
    def test_connect_success(self, mock_dispatch):
        mock_dispatch.return_value = MagicMock()
        from scripts.ole_stock_data import StockDataFetcher

        fetcher = StockDataFetcher()
        result = fetcher.connect()
        assert result is True
        assert fetcher.ab is not None

    @patch("win32com.client.Dispatch")
    def test_connect_failure(self, mock_dispatch):
        mock_dispatch.side_effect = Exception("COM not running")
        from scripts.ole_stock_data import StockDataFetcher

        fetcher = StockDataFetcher()
        result = fetcher.connect()
        assert result is False
        assert fetcher.ab is None

    def test_disconnect_clears_reference(self):
        from scripts.ole_stock_data import StockDataFetcher

        fetcher = StockDataFetcher()
        fetcher.ab = MagicMock()
        fetcher.disconnect()
        assert fetcher.ab is None

    @patch("win32com.client.Dispatch")
    def test_fetch_ohlcv_success(self, mock_dispatch):
        """Verify fetch_ohlcv returns aggregated 1-minute bars."""
        from scripts.ole_stock_data import StockDataFetcher

        # Build 3 ticks within the same minute
        base_dt = datetime(2025, 7, 21, 1, 14, 0)
        ticks = [
            _make_mock_quotation(base_dt + timedelta(seconds=0),  3427.0, 3427.5, 3426.5, 3427.2, 10),
            _make_mock_quotation(base_dt + timedelta(seconds=20), 3427.2, 3428.0, 3427.0, 3427.8, 15),
            _make_mock_quotation(base_dt + timedelta(seconds=40), 3427.8, 3428.2, 3427.5, 3428.0, 20),
        ]
        stock = _make_mock_stock("GCZ5", ticks)

        mock_app = MagicMock()
        mock_app.Stocks.return_value = stock
        mock_dispatch.return_value = mock_app

        fetcher = StockDataFetcher()
        fetcher.connect()
        fetcher.load_database()

        result = fetcher.fetch_ohlcv(
            "GCZ5",
            base_dt - timedelta(minutes=1),
            base_dt + timedelta(minutes=1),
        )

        assert result["error"] is None
        assert len(result["data"]) == 1  # All 3 ticks aggregate into 1 bar

        bar = result["data"][0]
        assert bar["open"] == 3427.0
        assert bar["high"] == 3428.2
        assert bar["low"] == 3426.5
        assert bar["close"] == 3428.0
        assert bar["volume"] == 45  # 10 + 15 + 20

    def test_fetch_ohlcv_not_connected(self):
        from scripts.ole_stock_data import StockDataFetcher

        fetcher = StockDataFetcher()
        result = fetcher.fetch_ohlcv("GCZ5", datetime.now(), datetime.now())
        assert result["error"] is not None
        assert "Not connected" in result["error"]

    @patch("win32com.client.Dispatch")
    def test_fetch_ohlcv_symbol_not_found(self, mock_dispatch):
        from scripts.ole_stock_data import StockDataFetcher

        mock_app = MagicMock()
        mock_app.Stocks.return_value = None
        mock_dispatch.return_value = mock_app

        fetcher = StockDataFetcher()
        fetcher.connect()
        fetcher.load_database()

        result = fetcher.fetch_ohlcv("BOGUS", datetime.now(), datetime.now())
        assert result["error"] is not None
        assert "not found" in result["error"]

    @patch("win32com.client.Dispatch")
    def test_fetch_ohlcv_empty_quotations(self, mock_dispatch):
        from scripts.ole_stock_data import StockDataFetcher

        stock = MagicMock()
        stock.Quotations.Count = 0

        mock_app = MagicMock()
        mock_app.Stocks.return_value = stock
        mock_dispatch.return_value = mock_app

        fetcher = StockDataFetcher()
        fetcher.connect()
        fetcher.load_database()

        result = fetcher.fetch_ohlcv("GCZ5", datetime.now(), datetime.now())
        assert result["error"] is not None
        assert "No quotation" in result["error"]


# ---------------------------------------------------------------------------
# Cache tests
# ---------------------------------------------------------------------------

class TestOhlcvCache:
    """Tests for get_ohlcv_cached file-based caching."""

    @patch("scripts.ole_stock_data.StockDataFetcher")
    def test_cache_miss_fetches_from_com(self, MockFetcher, tmp_path):
        """On cache miss, data should be fetched via COM and cached."""
        from scripts.ole_stock_data import get_ohlcv_cached
        import scripts.ole_stock_data as mod

        # Redirect cache to temp dir
        original_cache = mod.CACHE_DIR
        mod.CACHE_DIR = tmp_path

        try:
            mock_instance = MockFetcher.return_value
            mock_instance.connect.return_value = True
            mock_instance.fetch_ohlcv.return_value = {
                "data": [{"time": 1753056890, "open": 3427.0, "high": 3428.0,
                           "low": 3426.0, "close": 3427.5, "volume": 100}],
                "error": None,
            }

            start = datetime(2025, 7, 21, 1, 0, 0)
            end = datetime(2025, 7, 21, 2, 0, 0)

            result = get_ohlcv_cached("GCZ5", start, end, padding_before=5, padding_after=5)

            assert result["error"] is None
            assert len(result["data"]) == 1
            mock_instance.connect.assert_called_once()

            # Verify cache file was written
            cache_file = tmp_path / "GCZ5.json"
            assert cache_file.exists()
        finally:
            mod.CACHE_DIR = original_cache

    @patch("scripts.ole_stock_data.StockDataFetcher")
    def test_cache_hit_skips_com(self, MockFetcher, tmp_path):
        """When cache is fresh and covers the range, COM should not be called."""
        from scripts.ole_stock_data import get_ohlcv_cached
        import scripts.ole_stock_data as mod

        original_cache = mod.CACHE_DIR
        mod.CACHE_DIR = tmp_path

        try:
            start = datetime(2025, 7, 21, 1, 0, 0)
            end = datetime(2025, 7, 21, 2, 0, 0)

            # Pre-populate cache
            cache_data = {
                "symbol": "GCZ5",
                "fetched_at": datetime.now().isoformat(),
                "window_start": (start - timedelta(minutes=60)).isoformat(),
                "window_end": (end + timedelta(minutes=60)).isoformat(),
                "bars": [
                    {"time": 1753056890, "open": 3427.0, "high": 3428.0,
                     "low": 3426.0, "close": 3427.5, "volume": 100},
                ],
            }
            cache_file = tmp_path / "GCZ5.json"
            cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

            result = get_ohlcv_cached("GCZ5", start, end, padding_before=5, padding_after=5)

            assert result["error"] is None
            # COM should NOT have been called
            MockFetcher.return_value.connect.assert_not_called()
        finally:
            mod.CACHE_DIR = original_cache

    @patch("scripts.ole_stock_data.StockDataFetcher")
    def test_amibroker_not_running(self, MockFetcher, tmp_path):
        """When AmiBroker is not running, return a friendly error."""
        from scripts.ole_stock_data import get_ohlcv_cached
        import scripts.ole_stock_data as mod

        original_cache = mod.CACHE_DIR
        mod.CACHE_DIR = tmp_path

        try:
            mock_instance = MockFetcher.return_value
            mock_instance.connect.return_value = False

            start = datetime(2025, 7, 21, 1, 0, 0)
            end = datetime(2025, 7, 21, 2, 0, 0)

            result = get_ohlcv_cached("GCZ5", start, end, padding_before=5, padding_after=5)

            assert result["error"] is not None
            assert "not running" in result["error"].lower()
            assert result["data"] == []
        finally:
            mod.CACHE_DIR = original_cache
