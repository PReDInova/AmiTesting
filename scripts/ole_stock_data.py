"""
AmiBroker OLE Stock Data Access -- Sprint 3

Fetches OHLCV price data from an already-running AmiBroker instance via
COM/OLE Automation, aggregates tick data into 1-minute candlestick bars,
and caches results to JSON files for fast subsequent lookups.
"""

import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import win32com.client

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import (
    AMIBROKER_DB_PATH,
    AMIBROKER_EXE,
    CACHE_DIR,
    CHART_SETTINGS,
)

logger = logging.getLogger(__name__)

# OLE Automation Date epoch: 30 December 1899
_OLE_EPOCH = datetime(1899, 12, 30)


def _com_date_to_datetime(com_date: float) -> datetime:
    """Convert an OLE Automation Date (float) to a Python datetime."""
    return _OLE_EPOCH + timedelta(days=float(com_date))


class StockDataFetcher:
    """Read OHLCV quotation data from an already-running AmiBroker instance.

    Unlike :class:`OLEBacktester`, this class does **not** launch or quit
    AmiBroker -- it only attaches to an existing COM server.  This avoids
    surprise process launches and keeps a running AmiBroker session intact.
    """

    def __init__(self) -> None:
        self.ab = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Attach to an already-running AmiBroker via COM.

        Returns True on success, False if AmiBroker is not running.
        """
        try:
            logger.info("Attaching to running AmiBroker via COM (%s) ...", AMIBROKER_EXE)
            self.ab = win32com.client.Dispatch(AMIBROKER_EXE)
            logger.info("Attached to AmiBroker successfully.")
            return True
        except Exception as exc:
            logger.error("Cannot attach to AmiBroker (is it running?): %s", exc)
            self.ab = None
            return False

    def load_database(self, db_path: str = None) -> bool:
        """Load an AmiBroker database (defaults to ``AMIBROKER_DB_PATH``)."""
        path = db_path or AMIBROKER_DB_PATH
        try:
            logger.info("Loading database: %s", path)
            self.ab.LoadDatabase(path)
            logger.info("Database loaded.")
            return True
        except Exception as exc:
            logger.error("Failed to load database '%s': %s", path, exc)
            return False

    def disconnect(self) -> None:
        """Release the COM reference (does **not** quit AmiBroker)."""
        self.ab = None
        logger.info("COM reference released.")

    # ------------------------------------------------------------------
    # Data retrieval
    # ------------------------------------------------------------------

    def fetch_ohlcv(
        self,
        symbol: str,
        start_dt: datetime,
        end_dt: datetime,
    ) -> dict:
        """Fetch raw tick quotations and aggregate to 1-minute OHLCV bars.

        Parameters
        ----------
        symbol : str
            Ticker symbol as it appears in the AmiBroker database.
        start_dt, end_dt : datetime
            Date/time window (inclusive) for the returned bars.

        Returns
        -------
        dict
            ``{"data": [<bar>, ...], "error": None}`` on success, or
            ``{"data": [], "error": "message"}`` on failure.
            Each bar is ``{"time": <epoch_sec>, "open", "high", "low",
            "close", "volume"}``.
        """
        if self.ab is None:
            return {"data": [], "error": "Not connected to AmiBroker."}

        try:
            stock = self.ab.Stocks(symbol)
        except Exception as exc:
            return {"data": [], "error": f"COM error accessing symbol '{symbol}': {exc}"}

        if stock is None:
            return {"data": [], "error": f"Symbol '{symbol}' not found in database."}

        try:
            quotations = stock.Quotations
            count = quotations.Count
        except Exception as exc:
            return {"data": [], "error": f"Cannot read quotations for '{symbol}': {exc}"}

        if count == 0:
            return {"data": [], "error": f"No quotation data for '{symbol}'."}

        logger.info("Reading %d quotations for %s ...", count, symbol)

        # --- Find the start index via binary search ---
        start_idx = self._bisect_quotations(quotations, count, start_dt)

        # --- Collect raw ticks within the window ---
        raw_ticks = []
        for i in range(start_idx, count):
            q = quotations(i)
            bar_dt = _com_date_to_datetime(q.Date)

            if bar_dt > end_dt:
                break
            if bar_dt < start_dt:
                continue

            raw_ticks.append({
                "datetime": bar_dt,
                "open": float(q.Open),
                "high": float(q.High),
                "low": float(q.Low),
                "close": float(q.Close),
                "volume": float(q.Volume),
            })

        logger.info("Collected %d raw ticks in window.", len(raw_ticks))

        if not raw_ticks:
            return {"data": [], "error": None}

        # --- Aggregate to 1-minute bars ---
        bars = self._aggregate_to_1min(raw_ticks)
        logger.info("Aggregated to %d one-minute bars.", len(bars))
        return {"data": bars, "error": None}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _bisect_quotations(quotations, count: int, target_dt: datetime) -> int:
        """Binary search for the first quotation at or after *target_dt*."""
        lo, hi = 0, count - 1
        while lo < hi:
            mid = (lo + hi) // 2
            mid_dt = _com_date_to_datetime(quotations(mid).Date)
            if mid_dt < target_dt:
                lo = mid + 1
            else:
                hi = mid
        return lo

    @staticmethod
    def _aggregate_to_1min(raw_ticks: list[dict]) -> list[dict]:
        """Aggregate raw tick-level dicts into 1-minute OHLCV bars.

        Returns a list of dicts with ``time`` (UTC epoch seconds), ``open``,
        ``high``, ``low``, ``close``, ``volume``.
        """
        df = pd.DataFrame(raw_ticks)
        df.set_index("datetime", inplace=True)
        df.sort_index(inplace=True)

        ohlcv = df.resample("1min").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna(subset=["open"])

        bars = []
        for dt, row in ohlcv.iterrows():
            bars.append({
                "time": int(dt.timestamp()),
                "open": round(row["open"], 2),
                "high": round(row["high"], 2),
                "low": round(row["low"], 2),
                "close": round(row["close"], 2),
                "volume": round(row["volume"], 2),
            })
        return bars


# ======================================================================
# Cached wrapper
# ======================================================================

def get_ohlcv_cached(
    symbol: str,
    start_dt: datetime,
    end_dt: datetime,
    padding_before: int = None,
    padding_after: int = None,
) -> dict:
    """Fetch 1-minute OHLCV bars, using a JSON file cache.

    *padding_before* / *padding_after* widen the requested window by that
    many minutes so the chart shows context around the trade.

    Returns ``{"data": [...], "error": None}`` on success or
    ``{"data": [], "error": "message"}`` on failure.
    """
    if padding_before is None:
        padding_before = CHART_SETTINGS["bars_before_entry"]
    if padding_after is None:
        padding_after = CHART_SETTINGS["bars_after_exit"]

    padded_start = start_dt - timedelta(minutes=padding_before)
    padded_end = end_dt + timedelta(minutes=padding_after)

    # --- Check cache ---
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{symbol}.json"

    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
            cache_age_hours = (
                datetime.now() - datetime.fromisoformat(cache["fetched_at"])
            ).total_seconds() / 3600
            max_age = CHART_SETTINGS["cache_max_age_hours"]

            if cache_age_hours < max_age:
                cached_start = datetime.fromisoformat(cache["window_start"])
                cached_end = datetime.fromisoformat(cache["window_end"])

                if cached_start <= padded_start and cached_end >= padded_end:
                    logger.info("Cache hit for %s (age %.1fh).", symbol, cache_age_hours)
                    # Filter cached bars to the padded window
                    ps_ts = int(padded_start.timestamp())
                    pe_ts = int(padded_end.timestamp())
                    filtered = [
                        b for b in cache["bars"]
                        if ps_ts <= b["time"] <= pe_ts
                    ]
                    return {"data": filtered, "error": None}

            logger.info("Cache stale or incomplete for %s, re-fetching.", symbol)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("Cache file corrupt for %s, re-fetching: %s", symbol, exc)

    # --- Fetch from AmiBroker ---
    fetcher = StockDataFetcher()
    if not fetcher.connect():
        return {
            "data": [],
            "error": (
                "AmiBroker is not running. "
                "Please start AmiBroker to view trade charts."
            ),
        }

    try:
        fetcher.load_database()
        result = fetcher.fetch_ohlcv(symbol, padded_start, padded_end)
    finally:
        fetcher.disconnect()

    if result["error"]:
        return result

    # --- Write cache ---
    try:
        cache_payload = {
            "symbol": symbol,
            "fetched_at": datetime.now().isoformat(),
            "window_start": padded_start.isoformat(),
            "window_end": padded_end.isoformat(),
            "bars": result["data"],
        }
        cache_file.write_text(
            json.dumps(cache_payload, indent=2), encoding="utf-8"
        )
        logger.info("Cache written for %s (%d bars).", symbol, len(result["data"]))
    except Exception as exc:
        logger.warning("Failed to write cache for %s: %s", symbol, exc)

    return result
