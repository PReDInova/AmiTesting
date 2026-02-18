"""
Alert dispatcher for live signal alerts.

Supports multiple alert channels: logging, desktop notifications
(Windows toast), sound, and optional webhook POST.
"""

import json
import logging
import threading
import time
import urllib.request
import winsound
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AlertEvent:
    """An alert to be dispatched."""
    signal_type: str          # "Buy" or "Short"
    symbol: str
    timestamp: datetime
    price: float
    strategy_name: str
    indicator_values: dict = field(default_factory=dict)


class AlertDispatcher:
    """Routes alert events to configured channels.

    Parameters
    ----------
    channels : list[str]
        Active channels: "log", "desktop", "sound", "webhook".
    sound_file : str, optional
        Path to .wav file for sound alerts.
    webhook_url : str, optional
        URL for webhook POST alerts.
    dedup_window : int
        Seconds within which duplicate signals are suppressed.
    """

    def __init__(
        self,
        channels: list[str],
        sound_file: str = None,
        webhook_url: str = None,
        dedup_window: int = 300,
    ):
        self.channels = channels
        self.sound_file = sound_file
        self.webhook_url = webhook_url
        self.dedup_window = dedup_window
        self._recent_alerts: list[tuple[str, str, float]] = []
        self._alert_history: list[AlertEvent] = []

    def dispatch(self, event: AlertEvent) -> bool:
        """Dispatch an alert event through all configured channels.

        Returns True if the alert was dispatched (not deduplicated).
        """
        if self._is_duplicate(event):
            logger.debug("Suppressed duplicate alert: %s %s at %s",
                         event.signal_type, event.symbol, event.timestamp)
            return False

        self._alert_history.append(event)
        self._recent_alerts.append(
            (event.signal_type, event.symbol, time.time()))

        for channel in self.channels:
            try:
                handler = getattr(self, f"_alert_{channel}", None)
                if handler:
                    handler(event)
                else:
                    logger.warning("Unknown alert channel: %s", channel)
            except Exception as exc:
                logger.error("Alert channel '%s' failed: %s", channel, exc)

        return True

    def _is_duplicate(self, event: AlertEvent) -> bool:
        """Check if this signal was already alerted within the dedup window."""
        now = time.time()
        cutoff = now - self.dedup_window

        # Prune old entries
        self._recent_alerts = [
            (st, sym, ts) for st, sym, ts in self._recent_alerts
            if ts > cutoff
        ]

        # Check for match
        for sig_type, symbol, ts in self._recent_alerts:
            if sig_type == event.signal_type and symbol == event.symbol:
                return True
        return False

    def _alert_log(self, event: AlertEvent) -> None:
        """Write to Python logger at WARNING level for visibility."""
        indicator_str = ""
        if event.indicator_values:
            parts = [f"{k}={v:.4f}" for k, v in event.indicator_values.items()]
            indicator_str = f" [{', '.join(parts)}]"

        logger.warning(
            "SIGNAL ALERT: %s %s @ %.2f  (%s)  %s%s",
            event.signal_type.upper(),
            event.symbol,
            event.price,
            event.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            event.strategy_name,
            indicator_str,
        )

    def _alert_desktop(self, event: AlertEvent) -> None:
        """Show a Windows desktop notification.

        Uses ctypes to call the Windows MessageBox API in a daemon thread
        to avoid blocking the main loop.
        """
        import ctypes

        title = f"Trade Signal: {event.signal_type.upper()} {event.symbol}"
        msg = (f"{event.signal_type.upper()} {event.symbol} @ {event.price:.2f}\n"
               f"Time: {event.timestamp.strftime('%H:%M:%S')}\n"
               f"Strategy: {event.strategy_name}")

        def _show():
            try:
                # MB_OK | MB_ICONINFORMATION | MB_TOPMOST | MB_SETFOREGROUND
                flags = 0x00000000 | 0x00000040 | 0x00040000 | 0x00010000
                ctypes.windll.user32.MessageBoxW(0, msg, title, flags)
            except Exception as exc:
                logger.debug("Desktop notification failed: %s", exc)

        t = threading.Thread(target=_show, daemon=True)
        t.start()

    def _alert_sound(self, event: AlertEvent) -> None:
        """Play a sound alert using winsound."""
        try:
            if self.sound_file:
                winsound.PlaySound(self.sound_file,
                                   winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                # Different tones for Buy vs Short
                freq = 800 if event.signal_type == "Buy" else 600
                duration = 500  # ms
                # Run in thread to avoid blocking
                threading.Thread(
                    target=winsound.Beep, args=(freq, duration),
                    daemon=True
                ).start()
        except Exception as exc:
            logger.debug("Sound alert failed: %s", exc)

    def _alert_webhook(self, event: AlertEvent) -> None:
        """POST alert data to a webhook URL.

        Uses urllib.request to avoid adding requests as a dependency.
        Runs in a daemon thread to avoid blocking the scan loop.
        """
        if not self.webhook_url:
            return

        payload = json.dumps({
            "signal_type": event.signal_type,
            "symbol": event.symbol,
            "price": event.price,
            "timestamp": event.timestamp.isoformat(),
            "strategy": event.strategy_name,
            "indicators": event.indicator_values,
        }).encode("utf-8")

        def _post():
            try:
                req = urllib.request.Request(
                    self.webhook_url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    logger.debug("Webhook response: %d", resp.status)
            except Exception as exc:
                logger.error("Webhook POST failed: %s", exc)

        threading.Thread(target=_post, daemon=True).start()

    @property
    def alert_history(self) -> list[AlertEvent]:
        """Return the list of all dispatched alerts."""
        return list(self._alert_history)
