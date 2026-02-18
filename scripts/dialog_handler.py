"""
AmiBroker Dialog Auto-Handler

Runs a background thread that monitors for blocking AmiBroker dialog windows
and automatically dismisses them.  This solves the COM/OLE automation problem
where modal dialogs freeze AnalysisDocs.Open() and other COM calls.

Known dialogs:
  - "Choose action:" — "Existing formula is different than one stored in the
    project."  Clicking "Keep existing file" tells AmiBroker to use the file
    on disk (our snapshot), which is the desired behaviour.

Usage:
    handler = DialogHandler()
    handler.start()
    # ... do COM work ...
    handler.stop()

Or as a context manager:
    with DialogHandler() as handler:
        # ... do COM work ...
        print(handler.dismissed)  # list of dismissed dialogs
"""

import ctypes
import ctypes.wintypes
import logging
import threading
import time

import win32con
import win32gui

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Win32 helpers
# ---------------------------------------------------------------------------

EnumChildProc = ctypes.WINFUNCTYPE(
    ctypes.wintypes.BOOL,
    ctypes.wintypes.HWND,
    ctypes.wintypes.LPARAM,
)


def _get_child_windows(hwnd: int) -> list[dict]:
    """Return list of child windows with their text and class name."""
    children = []

    def callback(child_hwnd, _lparam):
        try:
            text = win32gui.GetWindowText(child_hwnd)
            cls = win32gui.GetClassName(child_hwnd)
            children.append({
                "hwnd": child_hwnd,
                "text": text,
                "class": cls,
                "visible": win32gui.IsWindowVisible(child_hwnd),
            })
        except Exception:
            pass
        return True  # continue enumeration

    try:
        win32gui.EnumChildWindows(hwnd, callback, 0)
    except Exception:
        pass

    return children


def _focus_window(hwnd: int) -> None:
    """Bring a window to the foreground so button clicks register."""
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass  # best-effort; click may still work without focus


def _click_button(hwnd: int) -> None:
    """Send a click message to a button window.

    Uses SendMessageW (synchronous).  This is safe when called from the
    same thread as the button's owner (e.g., via a timer callback in the
    main thread's message pump).  For cross-thread use, the DialogHandler
    class is only started AFTER blocking COM calls complete, so cross-thread
    corruption is avoided.
    """
    ctypes.windll.user32.SendMessageW(hwnd, win32con.BM_CLICK, 0, 0)


# ---------------------------------------------------------------------------
# Dialog rules
# ---------------------------------------------------------------------------

# Each rule is a dict with:
#   "name"        — human-readable label for logging
#   "match"       — function(title, children_texts) -> bool
#   "action"      — function(hwnd, children) -> str|None  (returns action taken or None)

def _match_formula_different(title: str, children_texts: list[str]) -> bool:
    """Match the 'Choose action' dialog (formula mismatch).

    The dialog title is "Choose action" and contains buttons
    "Keep existing file" / "Overwrite existing file".  The description
    text ("Existing formula is different...") is rendered in a DirectUIHWND
    which doesn't expose text via GetWindowText, so we match on title +
    button text instead.
    """
    title_lower = title.lower().strip()
    all_text = " ".join(children_texts).lower()
    return (
        title_lower == "choose action"
        and "keep existing file" in all_text
    ) or (
        "existing formula" in all_text
        and "different" in all_text
    ) or (
        "choose action" in all_text
    )


def _action_keep_existing(hwnd: int, children: list[dict]) -> str | None:
    """Click 'Keep existing file' button."""
    _focus_window(hwnd)
    for child in children:
        if child["class"] == "Button" and "keep" in child["text"].lower():
            _click_button(child["hwnd"])
            return f"Clicked '{child['text']}'"
    # Fallback: look for the first button (often the default/safe option)
    for child in children:
        if child["class"] == "Button" and child["visible"] and child["text"]:
            _click_button(child["hwnd"])
            return f"Clicked fallback button '{child['text']}'"
    return None


def _match_overwrite_dialog(title: str, children_texts: list[str]) -> bool:
    """Match generic overwrite/replace confirmation dialogs."""
    all_text = " ".join(children_texts).lower()
    return "overwrite" in all_text and ("file" in all_text or "formula" in all_text)


def _action_decline_overwrite(hwnd: int, children: list[dict]) -> str | None:
    """Click No/Cancel to decline overwriting."""
    _focus_window(hwnd)
    for child in children:
        if child["class"] == "Button":
            t = child["text"].lower()
            if t in ("no", "&no", "cancel", "&cancel"):
                _click_button(child["hwnd"])
                return f"Clicked '{child['text']}'"
    return None


def _match_optimization_warning(title: str, children_texts: list[str]) -> bool:
    """Match the 'extensive optimization' warning dialog."""
    all_text = " ".join(children_texts).lower()
    return "optimization" in all_text and ("extensive" in all_text or "combinations" in all_text)


def _action_accept_optimization(hwnd: int, children: list[dict]) -> str | None:
    """Click OK/Yes to proceed with optimization."""
    _focus_window(hwnd)
    for child in children:
        if child["class"] == "Button":
            t = child["text"].lower()
            if t in ("ok", "&ok", "yes", "&yes"):
                _click_button(child["hwnd"])
                return f"Clicked '{child['text']}'"
    return None


def _match_afl_error(title: str, children_texts: list[str]) -> bool:
    """Match the 'AFL Error' dialog."""
    return title.strip().lower() == "afl error"


def _action_close_afl_error(hwnd: int, children: list[dict]) -> str | None:
    """Click Close on the AFL Error dialog, logging any error text.

    The error text is typically in an Edit control; we try WM_GETTEXT
    since GetWindowText may return empty for multi-line edits.
    """
    _focus_window(hwnd)

    # Try to extract the error message from Edit controls
    error_text = ""
    for child in children:
        if child["class"] == "Edit":
            # Use WM_GETTEXT to read multi-line edit content
            buf_size = 4096
            buf = ctypes.create_unicode_buffer(buf_size)
            length = ctypes.windll.user32.SendMessageW(
                child["hwnd"], win32con.WM_GETTEXT, buf_size, buf
            )
            if length > 0:
                error_text = buf.value[:length]
                break

    if error_text:
        logger.error("AFL Error dialog content: %s", error_text.replace("\r\n", " | "))

    for child in children:
        if child["class"] == "Button" and "close" in child["text"].lower():
            _click_button(child["hwnd"])
            return f"Clicked '{child['text']}' (AFL error: {error_text[:100]})" if error_text else f"Clicked '{child['text']}'"
    # Fallback: any button
    for child in children:
        if child["class"] == "Button" and child["visible"] and child["text"]:
            _click_button(child["hwnd"])
            return f"Clicked fallback '{child['text']}'"
    return None


DIALOG_RULES = [
    {
        "name": "Formula Different (Choose Action)",
        "match": _match_formula_different,
        "action": _action_keep_existing,
    },
    {
        "name": "AFL Error",
        "match": _match_afl_error,
        "action": _action_close_afl_error,
    },
    {
        "name": "Overwrite Confirmation",
        "match": _match_overwrite_dialog,
        "action": _action_decline_overwrite,
    },
    {
        "name": "Extensive Optimization Warning",
        "match": _match_optimization_warning,
        "action": _action_accept_optimization,
    },
]


# ---------------------------------------------------------------------------
# DialogHandler
# ---------------------------------------------------------------------------


class DialogHandler:
    """Background thread that watches for and auto-dismisses AmiBroker dialogs.

    Parameters
    ----------
    poll_interval : float
        Seconds between scans (default 0.5).
    process_name : str
        Window class or title substring to scope scanning to AmiBroker windows.
    """

    def __init__(self, poll_interval: float = 0.5, process_name: str = "AmiBroker"):
        self._poll_interval = poll_interval
        self._process_name = process_name
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.dismissed: list[dict] = []  # log of dismissed dialogs

    # -- Context manager --

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    # -- Public API --

    def start(self) -> None:
        """Start the background dialog monitor thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self.dismissed.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="DialogHandler",
        )
        self._thread.start()
        logger.info("Dialog handler started (poll=%.1fs).", self._poll_interval)

    def stop(self) -> None:
        """Stop the background dialog monitor thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        if self.dismissed:
            logger.info(
                "Dialog handler stopped. Dismissed %d dialog(s): %s",
                len(self.dismissed),
                [d["rule"] for d in self.dismissed],
            )
        else:
            logger.info("Dialog handler stopped. No dialogs were dismissed.")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- Internal --

    def _monitor_loop(self) -> None:
        """Main polling loop — runs on the background thread."""
        while not self._stop_event.is_set():
            try:
                self._scan_once()
            except Exception as exc:
                logger.debug("Dialog scan error: %s", exc)
            self._stop_event.wait(self._poll_interval)

    def _scan_once(self) -> None:
        """Enumerate top-level windows and check for known dialogs."""
        dialog_hwnds = []

        def enum_callback(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return True
            try:
                title = win32gui.GetWindowText(hwnd)
                cls = win32gui.GetClassName(hwnd)
            except Exception:
                return True

            # Only look at AmiBroker-related windows.
            # Dialog boxes from AmiBroker typically have class "#32770" (standard
            # Windows dialog) or contain "AmiBroker" in the title.
            is_ami = self._process_name.lower() in title.lower()
            is_dialog = cls == "#32770"

            if is_ami or is_dialog:
                dialog_hwnds.append((hwnd, title, cls))
            return True

        win32gui.EnumWindows(enum_callback, 0)

        for hwnd, title, cls in dialog_hwnds:
            children = _get_child_windows(hwnd)
            children_texts = [c["text"] for c in children if c["text"]]

            for rule in DIALOG_RULES:
                try:
                    if rule["match"](title, children_texts):
                        action_result = rule["action"](hwnd, children)
                        if action_result:
                            record = {
                                "rule": rule["name"],
                                "title": title,
                                "action": action_result,
                                "time": time.time(),
                                "children_texts": children_texts,
                            }
                            self.dismissed.append(record)
                            logger.warning(
                                "Auto-dismissed dialog: [%s] title='%s' — %s",
                                rule["name"], title, action_result,
                            )
                            # Give the dialog time to close before scanning again
                            time.sleep(0.3)
                            break
                except Exception as exc:
                    logger.debug("Rule '%s' error: %s", rule["name"], exc)
