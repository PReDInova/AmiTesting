"""
ProjectX WebSocket data feed adapter.

Runs the async ProjectX realtime client on a background thread and
places completed OHLCV bars into a thread-safe queue for the COM
thread to consume.

Also supports a polling mode that fetches bars via REST API at a
regular interval, which is simpler and more robust for signal scanning.
"""

import asyncio
import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Re-export BarData and FeedStatus from bar_aggregator
from scripts.bar_aggregator import BarData, FeedStatus


class ProjectXFeed:
    """Bridges ProjectX async SDK to a synchronous queue.

    Supports two modes:
    - **polling** (default): Periodically fetches recent bars via REST API.
      Simpler, more robust, no WebSocket dependency.
    - **realtime**: Uses TradingSuite WebSocket for live tick streaming.
      Lower latency but more complex.

    Usage::

        feed = ProjectXFeed(symbols=["NQH6"], interval=1)
        feed.start()
        while True:
            bar = feed.bar_queue.get()  # blocks until bar available
            process(bar)
        feed.stop()
    """

    def __init__(
        self,
        symbols: list[str],
        interval: int = 1,
        unit: int = 2,
        initial_days: int = 2,
        poll_seconds: int = 60,
        bar_queue: queue.Queue | None = None,
        status_queue: queue.Queue | None = None,
        ami_symbol: str = "NQ",
    ):
        self.symbols = symbols
        self.interval = interval
        self.unit = unit
        self.initial_days = initial_days
        self.poll_seconds = poll_seconds
        self.bar_queue: queue.Queue[BarData] = bar_queue or queue.Queue()
        self.status_queue: queue.Queue[FeedStatus] = status_queue or queue.Queue()
        self.ami_symbol = ami_symbol

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_bar_time: Optional[datetime] = None

    def start(self) -> None:
        """Start the background polling thread."""
        load_dotenv()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="ProjectXFeed")
        self._thread.start()
        logger.info("ProjectXFeed started (symbols=%s, interval=%d, "
                     "poll=%ds).", self.symbols, self.interval,
                     self.poll_seconds)

    def stop(self) -> None:
        """Signal the feed to stop and wait for the thread to exit."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("ProjectXFeed stopped.")

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run_loop(self) -> None:
        """Thread target: create event loop and run async polling."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_poll_loop())
        except Exception as exc:
            logger.exception("ProjectXFeed thread crashed: %s", exc)
            self.status_queue.put(FeedStatus(
                connected=False,
                message=f"Feed thread crashed: {exc}",
            ))
        finally:
            loop.close()

    async def _async_poll_loop(self) -> None:
        """Async polling loop: fetch bars periodically via REST API."""
        from project_x_py import ProjectX

        self.status_queue.put(FeedStatus(
            connected=False, message="Connecting to ProjectX..."))

        retry_delay = 5
        while not self._stop_event.is_set():
            try:
                async with ProjectX.from_env() as client:
                    await client.authenticate()
                    self.status_queue.put(FeedStatus(
                        connected=True,
                        message=f"Authenticated. Account: "
                                f"{client.account_info.name}",
                    ))

                    # Initial backfill
                    await self._fetch_and_enqueue(client, days=self.initial_days)

                    # Polling loop
                    while not self._stop_event.is_set():
                        await asyncio.sleep(self.poll_seconds)
                        if self._stop_event.is_set():
                            break
                        await self._fetch_and_enqueue(client, days=1)

            except Exception as exc:
                logger.error("ProjectX connection error: %s", exc)
                self.status_queue.put(FeedStatus(
                    connected=False,
                    message=f"Connection error: {exc}. "
                            f"Retrying in {retry_delay}s...",
                ))
                # Wait with stop check
                for _ in range(retry_delay):
                    if self._stop_event.is_set():
                        return
                    await asyncio.sleep(1)
                retry_delay = min(retry_delay * 2, 60)

    async def _fetch_and_enqueue(self, client, days: int = 1) -> None:
        """Fetch recent bars and put new ones on the queue."""
        for symbol in self.symbols:
            try:
                df = await client.get_bars(
                    symbol,
                    days=days,
                    interval=self.interval,
                    unit=self.unit,
                )

                if df is None or len(df) == 0:
                    logger.debug("No bars returned for %s", symbol)
                    continue

                new_count = 0
                for row in df.iter_rows(named=True):
                    ts = row["timestamp"]
                    # Convert to naive datetime if timezone-aware
                    if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
                        ts = ts.replace(tzinfo=None)

                    # Skip bars we've already seen
                    if self._last_bar_time and ts <= self._last_bar_time:
                        continue

                    bar = BarData(
                        symbol=self.ami_symbol,
                        timestamp=ts,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("volume", 0)),
                        interval=self.interval * (60 if self.unit == 2 else 1),
                    )
                    self.bar_queue.put(bar)
                    new_count += 1

                if new_count > 0:
                    # Update last bar time to the most recent bar
                    last_ts = df["timestamp"][-1]
                    if hasattr(last_ts, "tzinfo") and last_ts.tzinfo:
                        last_ts = last_ts.replace(tzinfo=None)
                    self._last_bar_time = last_ts
                    logger.info("Fetched %d new bar(s) for %s (latest: %s)",
                                new_count, symbol,
                                self._last_bar_time.strftime("%H:%M:%S"))

            except Exception as exc:
                logger.error("Failed to fetch bars for %s: %s", symbol, exc)
