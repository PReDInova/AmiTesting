"""
AmiBroker OLE Quote Injector.

Adds new OHLCV bars to an AmiBroker database via COM automation.
Must be called from a thread that has called pythoncom.CoInitialize().

Follows the COM patterns established in ole_stock_data.py and
ole_backtest.py.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import pythoncom
import win32com.client

logger = logging.getLogger(__name__)

# OLE Automation Date epoch (same as ole_stock_data.py)
_OLE_EPOCH = datetime(1899, 12, 30)


def _datetime_to_com_date(dt: datetime) -> float:
    """Convert a Python datetime to an OLE Automation Date float.

    Inverse of ole_stock_data._com_date_to_datetime().
    """
    # Strip timezone info for consistent calculation
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    delta = dt - _OLE_EPOCH
    return delta.total_seconds() / 86400.0


class QuoteInjector:
    """Injects OHLCV bars into AmiBroker via COM OLE.

    Maintains a persistent COM connection to AmiBroker and provides
    methods to add individual quotation bars.

    Must be used from a COM-initialized thread.

    Parameters
    ----------
    com_dispatch_name : str
        COM ProgID (default: "Broker.Application").
    db_path : str or None
        Path to the AmiBroker database to load.  If ``None``, the
        injector uses whichever database AmiBroker already has open
        (typical for live streaming).
    """

    def __init__(self, com_dispatch_name: str, db_path: str = None):
        self.com_dispatch_name = com_dispatch_name
        self.db_path = db_path
        self.ab = None
        self._com_initialized = False
        self._injected_timestamps: set[float] = set()

    def connect(self) -> bool:
        """Attach to a running AmiBroker instance.

        If ``db_path`` was provided, loads that database.  Otherwise
        uses whichever database AmiBroker already has open.

        Returns True on success, False on failure.
        """
        try:
            if not self._com_initialized:
                pythoncom.CoInitialize()
                self._com_initialized = True

            logger.info("Connecting to AmiBroker via COM (%s) ...",
                        self.com_dispatch_name)
            self.ab = win32com.client.Dispatch(self.com_dispatch_name)

            if self.db_path:
                logger.info("Loading database: %s", self.db_path)
                self.ab.LoadDatabase(self.db_path)
            else:
                logger.info("Using AmiBroker's currently-loaded database.")

            logger.info("QuoteInjector connected to AmiBroker.")
            return True
        except Exception as exc:
            logger.error("QuoteInjector connect failed: %s", exc)
            self.ab = None
            return False

    def inject_bar(self, symbol: str, timestamp: datetime,
                   open_: float, high: float, low: float,
                   close: float, volume: float) -> bool:
        """Add a single OHLCV bar to the AmiBroker database.

        Uses ab.Stocks(symbol).Quotations.Add(date) then sets
        OHLCV properties on the returned quotation object.

        Returns True on success, False on failure.
        """
        if self.ab is None:
            logger.error("Not connected to AmiBroker.")
            return False

        com_date = _datetime_to_com_date(timestamp)

        # Skip if we already injected this exact timestamp
        dedup_key = (symbol, round(com_date, 8))
        if dedup_key in self._injected_timestamps:
            logger.debug("Skipping duplicate bar: %s %s", symbol, timestamp)
            return True

        def _do_inject():
            stock = self.ab.Stocks(symbol)
            if stock is None:
                logger.error("Symbol '%s' not found in AmiBroker database.",
                             symbol)
                return False

            quotes = stock.Quotations
            qt = quotes.Add(com_date)
            if qt is None:
                logger.error("Quotations.Add() returned None for %s at %s",
                             symbol, timestamp)
                return False

            qt.Open = open_
            qt.High = high
            qt.Low = low
            qt.Close = close
            qt.Volume = volume

            self._injected_timestamps.add(dedup_key)
            return True

        return self._retry_com_call(_do_inject)

    def refresh_all(self) -> bool:
        """Call ab.RefreshAll() to force AmiBroker to reload data.

        Should be called after injecting bars so charts update.
        """
        if self.ab is None:
            return False
        try:
            self.ab.RefreshAll()
            return True
        except Exception as exc:
            logger.error("RefreshAll failed: %s", exc)
            return False

    def disconnect(self) -> None:
        """Release the COM reference (does NOT quit AmiBroker)."""
        self.ab = None
        if self._com_initialized:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
            self._com_initialized = False
        logger.info("QuoteInjector disconnected.")

    def _retry_com_call(self, fn, max_retries: int = 3,
                        delay: float = 1.0) -> bool:
        """Retry a COM call with backoff on transient failures.

        Follows the retry pattern from ole_stock_data.py.
        Handles RPC_E_SERVERFAULT (-2147417851).
        """
        for attempt in range(max_retries):
            try:
                return fn()
            except Exception as exc:
                err_str = str(exc)
                if "-2147417851" in err_str or "RPC" in err_str:
                    logger.warning("COM transient error (attempt %d/%d): %s",
                                   attempt + 1, max_retries, exc)
                    time.sleep(delay * (attempt + 1))
                    # Re-dispatch COM proxy
                    try:
                        self.ab = win32com.client.Dispatch(
                            self.com_dispatch_name)
                    except Exception:
                        pass
                else:
                    logger.error("COM call failed: %s", exc)
                    return False
        logger.error("COM call failed after %d retries.", max_retries)
        return False
