"""
ProjectX WebSocket data feed adapter.

Streams live OHLCV bars from the ProjectX TradingSuite via WebSocket
(SignalR) and places completed bars into a thread-safe queue for the
COM thread to consume.

The TradingSuite handles authentication, WebSocket connections,
reconnection (exponential backoff), and bar construction from ticks.
"""

import asyncio
import json
import logging
import queue
import threading
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Monkey-patch signalrcore for resilience.
#
# signalrcore 1.0.0 has two crash points that kill the WebSocket
# connection on a single bad message:
#
#   1. websocket_client.prepare_data() calls data.decode('utf-8')
#      with no error handling.  Non-UTF-8 bytes raise UnicodeDecodeError.
#
#   2. json_hub_protocol.parse_messages() calls json.loads() with no
#      error handling.  Truncated/fragmented messages raise JSONDecodeError.
#
# Both exceptions propagate to base_socket_client.run() which sets
# self.running = False — killing the connection entirely.
#
# These patches make both methods resilient: bad bytes are replaced,
# bad messages are logged and skipped.
# ---------------------------------------------------------------------------
# Mutable counter for skipped SignalR fragments (used by patch below)
_bad_msg_counter = [0]


def _patch_signalrcore():
    # --- Patch 1: UTF-8 decode resilience ---
    try:
        from signalrcore.transport.websockets.websocket_client import (
            WebSocketClient,
        )

        def _resilient_prepare_data(self, data):
            if self.is_binary:
                return data
            return data.decode('utf-8', errors='replace')

        WebSocketClient.prepare_data = _resilient_prepare_data
        logger.debug("Patched signalrcore WebSocketClient.prepare_data "
                     "for UTF-8 resilience.")
    except ImportError:
        pass

    # --- Patch 2: JSON parse resilience ---
    try:
        from signalrcore.protocol.json_hub_protocol import JsonHubProtocol
    except ImportError:
        return

    def _resilient_parse(self, raw):
        self.logger.debug("Raw message incoming: ")
        self.logger.debug(raw)

        raw_messages = [
            record.replace(self.record_separator, "")
            for record in raw.split(self.record_separator)
            if record is not None and record != ""
            and record != self.record_separator
        ]

        result = []
        for raw_message in raw_messages:
            try:
                dict_message = json.loads(raw_message)
            except (json.JSONDecodeError, ValueError):
                # Fragments from large DOM/orderbook messages that
                # signalrcore splits across WebSocket frames.
                # Count silently; logged periodically below.
                _bad_msg_counter[0] += 1
                if _bad_msg_counter[0] <= 3 or _bad_msg_counter[0] % 500 == 0:
                    logger.debug(
                        "SignalR: skipped %d malformed fragment(s) "
                        "(DOM/orderbook frame splits)",
                        _bad_msg_counter[0])
                continue
            if len(dict_message.keys()) > 0:
                result.append(self.get_message(dict_message))
        return result

    JsonHubProtocol.parse_messages = _resilient_parse
    logger.debug("Patched signalrcore JsonHubProtocol.parse_messages "
                 "for JSON resilience.")

_patch_signalrcore()

# Re-export BarData and FeedStatus from bar_aggregator
from scripts.bar_aggregator import BarData, FeedStatus

# Map (unit, interval) to TradingSuite timeframe string.
# Unit: 1=Second, 2=Minute, 3=Hour, 4=Day
_TIMEFRAME_MAP = {
    (1, 1): "1sec",   (1, 5): "5sec",   (1, 10): "10sec",
    (1, 15): "15sec",  (1, 30): "30sec",
    (2, 1): "1min",   (2, 5): "5min",   (2, 15): "15min",
    (2, 30): "30min",
    (3, 1): "1hr",    (3, 4): "4hr",
    (4, 1): "1day",
}

# Interval in seconds for each TradingSuite timeframe
_TIMEFRAME_SECONDS = {
    "1sec": 1, "5sec": 5, "10sec": 10, "15sec": 15, "30sec": 30,
    "1min": 60, "5min": 300, "15min": 900, "30min": 1800,
    "1hr": 3600, "4hr": 14400, "1day": 86400,
    "1week": 604800, "1month": 2592000,
}


class ProjectXFeed:
    """Bridges ProjectX TradingSuite WebSocket to a synchronous queue.

    Connects to the ProjectX SignalR WebSocket, streams live ticks,
    and delivers completed OHLCV bars via ``bar_queue``.

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
        bar_queue: queue.Queue | None = None,
        status_queue: queue.Queue | None = None,
        ami_symbol: str = "NQ",
        account_id: int = None,
    ):
        self.symbols = symbols
        self.interval = interval
        self.unit = unit
        self.initial_days = initial_days
        self.bar_queue: queue.Queue[BarData] = bar_queue or queue.Queue()
        self.status_queue: queue.Queue[FeedStatus] = status_queue or queue.Queue()
        self.ami_symbol = ami_symbol
        self.account_id = account_id

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_bar_time: Optional[datetime] = None

        # Resolve timeframe from (unit, interval)
        self._timeframe = _TIMEFRAME_MAP.get((unit, interval))
        if self._timeframe is None:
            # Unsupported interval — fall back to next lower standard
            # timeframe and let AmiBroker aggregate via TimeFrameSet()
            self._timeframe = "1min"
            logger.warning(
                "Unsupported timeframe (unit=%d, interval=%d). "
                "Falling back to '%s' — AmiBroker will aggregate.",
                unit, interval, self._timeframe)

        self._bar_interval_seconds = _TIMEFRAME_SECONDS.get(
            self._timeframe, 60)

    def start(self) -> None:
        """Start the background WebSocket streaming thread."""
        load_dotenv()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="ProjectXFeed")
        self._thread.start()
        logger.info("ProjectXFeed started (symbols=%s, timeframe=%s).",
                     self.symbols, self._timeframe)

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
        """Thread target: create event loop and run async streaming."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_stream_loop())
        except Exception as exc:
            logger.exception("ProjectXFeed thread crashed: %s", exc)
            self.status_queue.put(FeedStatus(
                connected=False,
                message=f"Feed thread crashed: {exc}",
            ))
        finally:
            loop.close()

    async def _async_stream_loop(self) -> None:
        """Async streaming loop: connect via TradingSuite WebSocket."""
        from project_x_py import TradingSuite
        from project_x_py.event_bus import EventType

        self.status_queue.put(FeedStatus(
            connected=False, message="Connecting to ProjectX WebSocket..."))

        retry_delay = 5
        while not self._stop_event.is_set():
            suite = None
            # Track connection health via event callbacks
            connection_lost = asyncio.Event()

            try:
                # TradingSuite handles auth, WebSocket, and history.
                # Timeout prevents hanging if SignalR keeps getting closed
                # during the initial handshake.
                logger.info("Creating TradingSuite for %s (%s)...",
                            self.symbols[0], self._timeframe)
                try:
                    suite = await asyncio.wait_for(
                        TradingSuite.create(
                            instruments=self.symbols[0],
                            timeframes=[self._timeframe],
                        ),
                        timeout=30,
                    )
                except asyncio.TimeoutError:
                    raise ConnectionError(
                        f"TradingSuite.create() timed out after 30s — "
                        f"SignalR may be unable to connect for '{self.symbols[0]}'")

                # Register connection-health event handlers
                async def on_disconnect(event):
                    logger.warning("TradingSuite DISCONNECTED: %s",
                                   getattr(event, 'data', ''))
                    connection_lost.set()
                    self.status_queue.put(FeedStatus(
                        connected=False,
                        message="WebSocket disconnected."))

                async def on_reconnecting(event):
                    logger.info("TradingSuite reconnecting...")

                async def on_error(event):
                    logger.error("TradingSuite ERROR event: %s",
                                 getattr(event, 'data', ''))

                async def on_connected(event):
                    logger.info("TradingSuite CONNECTED event.")
                    connection_lost.clear()
                    self.status_queue.put(FeedStatus(
                        connected=True,
                        message=f"WebSocket reconnected. "
                                f"Streaming {self._timeframe} bars."))

                for etype, handler in [
                    (EventType.DISCONNECTED, on_disconnect),
                    (EventType.RECONNECTING, on_reconnecting),
                    (EventType.ERROR, on_error),
                    (EventType.CONNECTED, on_connected),
                ]:
                    try:
                        await suite.on(etype, handler)
                    except Exception:
                        pass  # older SDK may not support all event types

                self.status_queue.put(FeedStatus(
                    connected=True,
                    message=(f"WebSocket connected. "
                             f"Streaming {self._timeframe} bars."),
                ))
                logger.info("TradingSuite connected for %s (%s)",
                            self.symbols[0], self._timeframe)

                # Register NEW_BAR callback
                async def on_new_bar(event):
                    self._handle_new_bar(event)

                await suite.on(EventType.NEW_BAR, on_new_bar)

                # Backfill historical bars
                await self._backfill_history(suite)

                # Keep alive until stop requested or connection lost
                retry_delay = 5  # reset on successful connection
                health_check_interval = 30  # seconds between health checks
                ticks_since_health_check = 0
                while not self._stop_event.is_set():
                    await asyncio.sleep(1)
                    ticks_since_health_check += 1

                    # If a DISCONNECTED event fired, break to retry
                    if connection_lost.is_set():
                        logger.warning(
                            "Connection lost detected. Breaking to retry.")
                        break

                    # Periodic health check via realtime client
                    if ticks_since_health_check >= health_check_interval:
                        ticks_since_health_check = 0
                        try:
                            rt = getattr(suite, 'realtime', None)
                            if rt and hasattr(rt, 'user_connected'):
                                if not rt.user_connected and not rt.market_connected:
                                    logger.warning(
                                        "Health check: both hubs disconnected.")
                                    connection_lost.set()
                                    break
                        except Exception:
                            pass

            except Exception as exc:
                logger.error("ProjectX WebSocket error: %s", exc)
                self.status_queue.put(FeedStatus(
                    connected=False,
                    message=f"WebSocket error: {exc}. "
                            f"Retrying in {retry_delay}s...",
                ))
                for _ in range(retry_delay):
                    if self._stop_event.is_set():
                        return
                    await asyncio.sleep(1)
                retry_delay = min(retry_delay * 2, 60)

            finally:
                if suite is not None:
                    try:
                        await suite.disconnect()
                        logger.info("TradingSuite disconnected.")
                    except Exception as exc:
                        logger.debug("Disconnect error: %s", exc)

    def _handle_new_bar(self, event) -> None:
        """Process a NEW_BAR event from TradingSuite."""
        try:
            bar_data = event.data["data"]
            timeframe = event.data.get("timeframe", self._timeframe)

            ts = bar_data["timestamp"]
            if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)

            # Skip bars already seen (overlap with backfill)
            if self._last_bar_time and ts <= self._last_bar_time:
                return

            bar = BarData(
                symbol=self.ami_symbol,
                timestamp=ts,
                open=float(bar_data["open"]),
                high=float(bar_data["high"]),
                low=float(bar_data["low"]),
                close=float(bar_data["close"]),
                volume=float(bar_data.get("volume", 0)),
                interval=self._bar_interval_seconds,
            )
            self.bar_queue.put(bar)
            self._last_bar_time = ts

            logger.info("New %s bar: %s C=%.2f V=%.0f",
                        timeframe,
                        ts.strftime("%H:%M:%S"),
                        bar.close, bar.volume)

        except Exception as exc:
            logger.error("Error processing NEW_BAR event: %s", exc)

    async def _backfill_history(self, suite) -> None:
        """Load historical bars from TradingSuite and queue them."""
        try:
            hist = await suite.data.get_data(self._timeframe, bars=5000)
            if hist is None or len(hist) == 0:
                logger.info("No historical bars available for backfill.")
                return

            count = 0
            for row in hist.iter_rows(named=True):
                ts = row["timestamp"]
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
                    interval=self._bar_interval_seconds,
                )
                self.bar_queue.put(bar)
                count += 1

            if count > 0:
                last_ts = hist["timestamp"][-1]
                if hasattr(last_ts, "tzinfo") and last_ts.tzinfo is not None:
                    last_ts = last_ts.replace(tzinfo=None)
                self._last_bar_time = last_ts
                logger.info("Backfilled %d historical bars (latest: %s)",
                            count,
                            self._last_bar_time.strftime("%H:%M:%S"))

        except Exception as exc:
            logger.error("Historical backfill failed: %s", exc)
