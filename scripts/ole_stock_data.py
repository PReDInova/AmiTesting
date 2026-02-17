"""
AmiBroker OLE Stock Data Access -- Sprint 3 / Sprint 4

Fetches OHLCV price data from an already-running AmiBroker instance via
COM/OLE Automation, aggregates quotation data into candlestick bars at a
configurable interval, and caches results to JSON files for fast lookups.
"""

import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pythoncom
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

# Pandas resample rule strings keyed by interval-in-seconds
_INTERVAL_TO_PANDAS = {
    60: "1min",
    300: "5min",
    600: "10min",
    3600: "1h",
    86400: "1D",
}


def _com_date_to_datetime(com_date) -> datetime:
    """Convert a COM date to a Python datetime.

    Handles both OLE Automation Date floats *and* ``pywintypes.datetime``
    objects (returned when COM is properly initialised via CoInitialize).
    """
    if isinstance(com_date, datetime):
        # pywintypes.datetime is a datetime subclass -- strip tzinfo so
        # comparisons with naive datetimes work consistently.
        return com_date.replace(tzinfo=None)
    return _OLE_EPOCH + timedelta(days=float(com_date))


class StockDataFetcher:
    """Read OHLCV quotation data from an already-running AmiBroker instance.

    Unlike :class:`OLEBacktester`, this class does **not** launch or quit
    AmiBroker -- it only attaches to an existing COM server.  This avoids
    surprise process launches and keeps a running AmiBroker session intact.
    """

    def __init__(self) -> None:
        self.ab = None
        self._com_initialized = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Attach to an already-running AmiBroker via COM.

        Returns True on success, False if AmiBroker is not running.
        """
        try:
            # COM must be initialised per-thread (Flask serves requests
            # on worker threads that don't inherit the main thread's COM
            # apartment).  CoInitialize is safe to call multiple times --
            # redundant calls are harmless (returns S_FALSE).
            pythoncom.CoInitialize()
            self._com_initialized = True

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
        if self._com_initialized:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
            self._com_initialized = False
        logger.info("COM reference released.")

    # ------------------------------------------------------------------
    # Data retrieval
    # ------------------------------------------------------------------

    def fetch_ohlcv(
        self,
        symbol: str,
        start_dt: datetime,
        end_dt: datetime,
        interval: int = 60,
    ) -> dict:
        """Fetch quotations and return OHLCV bars at the requested *interval*.

        Parameters
        ----------
        symbol : str
            Ticker symbol as it appears in the AmiBroker database.
        start_dt, end_dt : datetime
            Date/time window (inclusive) for the returned bars.
        interval : int
            Target bar size in seconds (60, 300, 600, 86400).  Defaults to 60.

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

        # --- Detect source interval and aggregate as needed ---
        source_interval = self.detect_data_interval(raw_ticks)
        logger.info("Detected source interval: %ds, target: %ds", source_interval, interval)

        if source_interval > 0 and source_interval >= interval:
            # Source already matches or exceeds target — format without
            # re-aggregation (can't disaggregate coarser bars).
            bars = self._format_bars(raw_ticks)
        else:
            # Source is finer-grained than target — aggregate up.
            bars = self._aggregate_bars(raw_ticks, interval)

        logger.info("Produced %d bars at %ds interval.", len(bars), interval)
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
    def detect_data_interval(raw_ticks: list[dict]) -> int:
        """Detect the base interval of raw quotation data in seconds.

        Examines the median time gap between consecutive data points.
        Returns the detected interval in seconds:
          - 0 means tick-level (irregular, sub-second or few-second gaps)
          - 60 means 1-minute bars
          - 300 means 5-minute bars
          - etc.
        """
        if len(raw_ticks) < 2:
            return 0

        diffs = []
        for i in range(1, min(len(raw_ticks), 100)):
            delta = (raw_ticks[i]["datetime"] - raw_ticks[i - 1]["datetime"]).total_seconds()
            if delta > 0:
                diffs.append(delta)

        if not diffs:
            return 0

        diffs.sort()
        median_gap = diffs[len(diffs) // 2]

        # Classify against known bar intervals (10% tolerance)
        for known in (60, 300, 600, 3600, 86400):
            if abs(median_gap - known) < known * 0.1:
                return known

        if median_gap < 30:
            return 0  # tick-level
        return int(round(median_gap / 60) * 60)  # round to nearest minute

    @staticmethod
    def _format_bars(raw_ticks: list[dict]) -> list[dict]:
        """Convert raw tick dicts to the output bar format without aggregation."""
        return [
            {
                "time": int(tick["datetime"].timestamp()),
                "open": round(tick["open"], 2),
                "high": round(tick["high"], 2),
                "low": round(tick["low"], 2),
                "close": round(tick["close"], 2),
                "volume": round(tick["volume"], 2),
            }
            for tick in raw_ticks
        ]

    @staticmethod
    def _aggregate_bars(raw_ticks: list[dict], target_interval: int = 60) -> list[dict]:
        """Aggregate raw data into OHLCV bars of the specified interval.

        Parameters
        ----------
        raw_ticks : list[dict]
            Raw data with 'datetime', 'open', 'high', 'low', 'close', 'volume'.
        target_interval : int
            Target bar size in seconds (60=1min, 300=5min, 600=10min, 86400=daily).

        Returns
        -------
        list[dict]
            Bars with 'time' (epoch seconds), 'open', 'high', 'low', 'close', 'volume'.
        """
        resample_rule = _INTERVAL_TO_PANDAS.get(target_interval, f"{target_interval}s")

        df = pd.DataFrame(raw_ticks)
        df.set_index("datetime", inplace=True)
        df.sort_index(inplace=True)

        ohlcv = df.resample(resample_rule).agg({
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

    # Backward-compatible alias
    @staticmethod
    def _aggregate_to_1min(raw_ticks: list[dict]) -> list[dict]:
        """Aggregate raw tick-level dicts into 1-minute OHLCV bars.

        .. deprecated:: Sprint 4
            Use :meth:`_aggregate_bars` with ``target_interval=60`` instead.
        """
        return StockDataFetcher._aggregate_bars(raw_ticks, 60)


def _get_latest_bars_once(symbol: str, num_bars: int = 500, interval: int = 60,
                          days: int | None = None,
                          end_date: str | None = None) -> dict:
    """Single-attempt fetch of recent bars (called by :func:`get_latest_bars`).

    Returns ``{"data": [...], "error": None, "data_range": {...}}`` on
    success, or ``{"data": [], "error": "message"}`` on failure.
    """
    fetcher = StockDataFetcher()
    if not fetcher.connect():
        return {
            "data": [],
            "error": "AmiBroker is not running. Please start AmiBroker.",
        }

    try:
        fetcher.load_database()
        stock = fetcher.ab.Stocks(symbol)
        if stock is None:
            return {"data": [], "error": f"Symbol '{symbol}' not found in database."}

        quotations = stock.Quotations
        count = quotations.Count
        if count == 0:
            return {"data": [], "error": f"No quotation data for '{symbol}'."}

        # First and last dates available in the database
        first_available_dt = _com_date_to_datetime(quotations(0).Date)
        last_available_dt = _com_date_to_datetime(quotations(count - 1).Date)

        # Determine the anchor date (end of the data window)
        if end_date is not None:
            try:
                anchor_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59
                )
            except ValueError:
                return {
                    "data": [],
                    "error": f"Invalid end_date format: '{end_date}'. Use YYYY-MM-DD.",
                }
            # Find the index at or just before the anchor date
            end_idx = fetcher._bisect_quotations(quotations, count, anchor_dt)
            # _bisect returns the first index AT or AFTER target.  If that
            # bar is past the anchor, step back.
            if end_idx < count:
                end_bar_dt = _com_date_to_datetime(quotations(end_idx).Date)
                if end_bar_dt > anchor_dt:
                    end_idx = max(0, end_idx - 1)
            # We want to include end_idx, so the range is [start_idx .. end_idx]
            end_idx_exclusive = end_idx + 1
        else:
            anchor_dt = last_available_dt
            end_idx_exclusive = count

        # Determine start index
        if days is not None:
            cutoff = anchor_dt - timedelta(days=days)
            start_idx = fetcher._bisect_quotations(
                quotations, count, cutoff
            )
            # Clamp to not go past end_idx_exclusive
            start_idx = min(start_idx, max(0, end_idx_exclusive - 1))
            logger.info(
                "Days filter: anchor=%s, cutoff=%s, start_idx=%d, end_idx=%d of %d",
                anchor_dt, cutoff, start_idx, end_idx_exclusive, count,
            )
        else:
            start_idx = max(0, end_idx_exclusive - num_bars)

        raw_ticks = []
        for i in range(start_idx, end_idx_exclusive):
            q = quotations(i)
            bar_dt = _com_date_to_datetime(q.Date)
            raw_ticks.append({
                "datetime": bar_dt,
                "open": float(q.Open),
                "high": float(q.High),
                "low": float(q.Low),
                "close": float(q.Close),
                "volume": float(q.Volume),
            })

        logger.info("Read %d raw ticks (%s) for %s.",
                     len(raw_ticks),
                     f"last {days} days" if days else f"last {num_bars}",
                     symbol)

        if not raw_ticks:
            return {
                "data": [],
                "error": None,
                "data_range": {
                    "first_date": None,
                    "last_date": None,
                    "first_available_date": first_available_dt.strftime("%Y-%m-%d"),
                    "last_available_date": last_available_dt.strftime("%Y-%m-%d"),
                },
            }

        # Detect source interval and aggregate if needed
        source_interval = fetcher.detect_data_interval(raw_ticks)
        if source_interval > 0 and source_interval >= interval:
            bars = fetcher._format_bars(raw_ticks)
        else:
            bars = fetcher._aggregate_bars(raw_ticks, interval)

        logger.info("Produced %d bars at %ds interval for explorer.", len(bars), interval)
        return {
            "data": bars,
            "error": None,
            "data_range": {
                "first_date": raw_ticks[0]["datetime"].strftime("%Y-%m-%d"),
                "last_date": raw_ticks[-1]["datetime"].strftime("%Y-%m-%d"),
                "first_available_date": first_available_dt.strftime("%Y-%m-%d"),
                "last_available_date": last_available_dt.strftime("%Y-%m-%d"),
            },
        }
    except Exception as exc:
        logger.error("Error fetching latest bars for %s: %s", symbol, exc)
        return {"data": [], "error": str(exc)}
    finally:
        fetcher.disconnect()


import time as _time

def get_latest_bars(symbol: str, num_bars: int = 500, interval: int = 60,
                    days: int | None = None,
                    end_date: str | None = None,
                    _max_retries: int = 3) -> dict:
    """Fetch recent bars for *symbol* from AmiBroker with automatic retry.

    AmiBroker's COM server occasionally throws ``RPC_E_SERVERFAULT``
    (``-2147417851``) when the OLE interface is momentarily busy (e.g.
    concurrent requests from Flask worker threads).  This wrapper retries
    the fetch up to *_max_retries* times with a short back-off delay.

    Parameters
    ----------
    symbol : str
        Ticker symbol in the AmiBroker database.
    num_bars : int
        Maximum number of raw quotations to read (used as a fallback
        when *days* is ``None``).
    interval : int
        Target bar size in seconds (60, 300, 600, 86400).
    days : int or None
        If provided, only return data from the most recent *days* calendar
        days.  This is much faster than reading thousands of quotations
        and is the recommended way to control the data window.  When
        ``None`` the function falls back to reading the last *num_bars*
        quotations.
    end_date : str or None
        Optional end date in ``YYYY-MM-DD`` format.  When provided, the
        data window ends at this date instead of at the last available
        quotation.  Combined with *days*, this lets users sample any
        historical period: ``[end_date - days .. end_date]``.  When
        ``None`` (default), the last quotation in the database is used.

    Returns ``{"data": [...], "error": None, "data_range": {...}}`` on
    success.  ``data_range`` contains ``first_date``, ``last_date``, and
    ``last_available_date`` (the very last date in the database).
    """
    last_result = None
    for attempt in range(1, _max_retries + 1):
        result = _get_latest_bars_once(
            symbol=symbol, num_bars=num_bars, interval=interval,
            days=days, end_date=end_date,
        )
        if not result.get("error"):
            return result

        last_result = result
        err = result["error"]

        # Retry only on transient COM server faults
        if "-2147417851" in str(err) or "server threw an exception" in str(err).lower():
            logger.warning(
                "COM server fault for %s (attempt %d/%d), retrying in %ds...",
                symbol, attempt, _max_retries, attempt,
            )
            _time.sleep(attempt)  # back off: 1s, 2s, 3s
            continue

        # Non-transient error — return immediately
        return result

    logger.error("All %d attempts failed for %s.", _max_retries, symbol)
    return last_result


# ======================================================================
# Cached wrapper
# ======================================================================

def get_ohlcv_cached(
    symbol: str,
    start_dt: datetime,
    end_dt: datetime,
    padding_before: int = None,
    padding_after: int = None,
    interval: int = 60,
) -> dict:
    """Fetch OHLCV bars at the requested *interval*, using a JSON file cache.

    *padding_before* / *padding_after* widen the requested window by that
    many minutes so the chart shows context around the trade.

    The cache always stores 1-minute bars.  Higher timeframes are
    re-aggregated from the cached data on the fly.

    Returns ``{"data": [...], "error": None}`` on success or
    ``{"data": [], "error": "message"}`` on failure.
    """
    if padding_before is None:
        padding_before = CHART_SETTINGS["bars_before_entry"]
    if padding_after is None:
        padding_after = CHART_SETTINGS["bars_after_exit"]

    padded_start = start_dt - timedelta(minutes=padding_before)
    padded_end = end_dt + timedelta(minutes=padding_after)

    # --- Check cache (always stored as 1-min bars) ---
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{symbol}.json"

    cached_bars = None
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
                    ps_ts = int(padded_start.timestamp())
                    pe_ts = int(padded_end.timestamp())
                    cached_bars = [
                        b for b in cache["bars"]
                        if ps_ts <= b["time"] <= pe_ts
                    ]

            if cached_bars is None:
                logger.info("Cache stale or incomplete for %s, re-fetching.", symbol)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("Cache file corrupt for %s, re-fetching: %s", symbol, exc)

    if cached_bars is None:
        # --- Fetch from AmiBroker (always at 1-min / native resolution) ---
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
            result = fetcher.fetch_ohlcv(symbol, padded_start, padded_end, interval=60)
        finally:
            fetcher.disconnect()

        if result["error"]:
            return result

        cached_bars = result["data"]

        # --- Write cache (1-min bars) ---
        try:
            cache_payload = {
                "symbol": symbol,
                "fetched_at": datetime.now().isoformat(),
                "window_start": padded_start.isoformat(),
                "window_end": padded_end.isoformat(),
                "bars": cached_bars,
            }
            cache_file.write_text(
                json.dumps(cache_payload, indent=2), encoding="utf-8"
            )
            logger.info("Cache written for %s (%d bars).", symbol, len(cached_bars))
        except Exception as exc:
            logger.warning("Failed to write cache for %s: %s", symbol, exc)

    # --- Re-aggregate to the requested interval if needed ---
    if interval > 60 and cached_bars:
        reformat = [
            {
                "datetime": datetime.fromtimestamp(b["time"]),
                "open": b["open"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "volume": b["volume"],
            }
            for b in cached_bars
        ]
        final_bars = StockDataFetcher._aggregate_bars(reformat, interval)
    else:
        final_bars = cached_bars

    return {"data": final_bars, "error": None}
