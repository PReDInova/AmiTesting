"""
End-to-end test of the analyze_bar flow with dialog monitoring.
Reproduces exactly what the API endpoint does, with a background
thread that watches for and captures any AmiBroker dialog windows.
"""
import sys
import time
import threading
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import win32gui
import win32con

# ---------------------------------------------------------------------------
# Dialog monitor — runs in background, captures and dismisses dialogs
# ---------------------------------------------------------------------------
dialog_log = []
monitor_active = True


def _get_dialog_info(hwnd):
    """Get dialog window text and child button/static text."""
    title = win32gui.GetWindowText(hwnd)
    children = []
    def enum_child(ch, _):
        cls = win32gui.GetClassName(ch)
        txt = win32gui.GetWindowText(ch)
        if txt:
            children.append((cls, txt))
        return True
    try:
        win32gui.EnumChildWindows(hwnd, enum_child, None)
    except Exception:
        pass
    return title, children


def _dialog_monitor():
    """Poll for AmiBroker dialog windows and auto-dismiss them."""
    while monitor_active:
        try:
            dialogs = []
            def find_dlg(hwnd, lst):
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                cls = win32gui.GetClassName(hwnd)
                title = win32gui.GetWindowText(hwnd)
                if cls == '#32770' and ('AmiBroker' in title or 'Choose action' in title):
                    lst.append(hwnd)
                return True
            win32gui.EnumWindows(find_dlg, dialogs)

            for hwnd in dialogs:
                title, children = _get_dialog_info(hwnd)
                child_texts = [txt for _, txt in children]
                entry = {
                    "time": time.time(),
                    "title": title,
                    "children": child_texts,
                    "hwnd": hwnd,
                }
                dialog_log.append(entry)
                print(f"\n*** DIALOG DETECTED: \"{title}\" ***", flush=True)
                for ct in child_texts:
                    if len(ct) > 80:
                        print(f"    {ct[:80]}...", flush=True)
                    else:
                        print(f"    {ct}", flush=True)

                # Auto-dismiss: click "Keep existing file" or "OK"
                def click_dismiss(ch, _):
                    txt = win32gui.GetWindowText(ch)
                    cls = win32gui.GetClassName(ch)
                    if cls == 'Button' and txt in ('OK', 'Keep existing file'):
                        print(f"    -> Clicking \"{txt}\"", flush=True)
                        win32gui.PostMessage(ch, win32con.BM_CLICK, 0, 0)
                    return True
                try:
                    win32gui.EnumChildWindows(hwnd, click_dismiss, None)
                except Exception:
                    # Fallback: send WM_CLOSE
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)

        except Exception:
            pass
        time.sleep(0.3)


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------
def main():
    global monitor_active

    from scripts.strategy_db import get_strategy, get_latest_version

    strategy_id = "c0e490d0-590e-4e14-8e51-84df2268e235"
    bar_time = 1762907222
    params = {"StdDev Multiplier": 1.0, "TEMA Length": 21.0, "Min Fade Move USD": 3.0}

    # Load strategy AFL (same as API endpoint)
    strategy = get_strategy(strategy_id)
    if not strategy:
        print("ERROR: Strategy not found")
        return
    version = get_latest_version(strategy_id)
    afl_content = version.get("afl_content", "") if version else ""
    if not afl_content:
        print("ERROR: No AFL content")
        return

    print(f"Strategy: {strategy['name']}", flush=True)
    print(f"AFL: {len(afl_content)} chars", flush=True)
    print(f"Bar time: {bar_time}", flush=True)
    print(f"Params: {params}", flush=True)
    print(flush=True)

    # Start dialog monitor
    monitor_thread = threading.Thread(target=_dialog_monitor, daemon=True)
    monitor_thread.start()
    print("Dialog monitor started", flush=True)

    # Run analyze_bar (same as API endpoint)
    from scripts.ole_bar_analyzer import analyze_bar

    print("Calling analyze_bar()...", flush=True)
    try:
        result = analyze_bar(
            afl_content=afl_content,
            target_unix_ts=bar_time,
            strategy_id=strategy_id,
            param_values=params,
        )
        print(flush=True)
        print("=== RESULT ===", flush=True)
        if result.get("error"):
            print(f"Error: {result['error']}", flush=True)
        else:
            print(f"Bar OHLCV: {result.get('bar_ohlcv')}", flush=True)
            buy = result.get("buy", {})
            short = result.get("short", {})
            print(f"Buy overall: {buy.get('overall')}", flush=True)
            for c in buy.get("conditions", []):
                print(f"  {'PASS' if c['passed'] else 'FAIL'}: {c['text']}", flush=True)
            print(f"Short overall: {short.get('overall')}", flush=True)
            for c in short.get("conditions", []):
                print(f"  {'PASS' if c['passed'] else 'FAIL'}: {c['text']}", flush=True)
            print(f"Variables: {result.get('variable_values', {})}", flush=True)
            print(f"Elapsed: {result.get('elapsed_ms')}ms", flush=True)
    except Exception as exc:
        print(f"EXCEPTION: {exc}", flush=True)
        import traceback
        traceback.print_exc()

    # Stop monitor
    monitor_active = False
    time.sleep(0.5)

    # Report dialogs
    print(flush=True)
    if dialog_log:
        print(f"=== {len(dialog_log)} DIALOG(S) DETECTED ===", flush=True)
        for entry in dialog_log:
            print(f"  Title: {entry['title']}", flush=True)
            for ct in entry['children']:
                print(f"    {ct[:100]}", flush=True)
    else:
        print("=== NO DIALOGS DETECTED ===", flush=True)


if __name__ == "__main__":
    main()
