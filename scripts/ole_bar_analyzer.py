"""
OLE Bar Analyzer — "Why no trade here?" feature.

Generates a custom Exploration AFL that evaluates individual Buy/Short
sub-conditions at a specific bar, runs it through AmiBroker's OLE
Exploration mode (Run(1)), and parses the exported CSV to return
per-condition pass/fail status with actual numeric values.

This ensures all analysis uses AmiBroker's actual AFL engine — the same
source as backtests — for full confidence in results.
"""

import csv
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pythoncom
import win32com.client
import win32con
import win32gui

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import (
    AMIBROKER_EXE,
    AMIBROKER_DB_PATH,
    APX_DIR,
    APX_TEMPLATE,
    DEFAULT_SYMBOL,
)
from scripts.apx_builder import build_apx
from scripts.signal_evaluator import (
    _strip_comments,
    _extract_var_defs,
    _resolve_signal_expr,
    _split_and,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXPLORATION_RUN_MODE = 1       # AmiBroker Exploration mode
EXPLORATION_POLL_INTERVAL = 0.3
EXPLORATION_MAX_WAIT = 30      # seconds


# ---------------------------------------------------------------------------
# Dialog auto-dismiss
# ---------------------------------------------------------------------------


class _DialogAutoDismisser:
    """Background thread that auto-dismisses AmiBroker modal dialogs.

    When AmiBroker detects a mismatch between FormulaContent and the file
    at FormulaPath, it shows a "Choose action" dialog.  This thread monitors
    for that dialog and clicks "Keep existing file" so the correct AFL from
    the file on disk is used.  Also handles crash-recovery and other OK
    dialogs that would otherwise block OLE automation.
    """

    def __init__(self, poll_interval: float = 0.15):
        self._poll_interval = poll_interval
        self._running = False
        self._thread = None
        self._dismissed = 0

    def __enter__(self):
        self._running = True
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        if self._dismissed:
            logger.info("Dialog auto-dismiss: dismissed %d dialog(s)",
                        self._dismissed)

    def _monitor(self):
        while self._running:
            try:
                dialogs = []

                def _find(hwnd, lst):
                    if not win32gui.IsWindowVisible(hwnd):
                        return True
                    cls = win32gui.GetClassName(hwnd)
                    if cls == '#32770':          # standard dialog class
                        title = win32gui.GetWindowText(hwnd)
                        if 'AmiBroker' in title or 'Choose' in title:
                            lst.append(hwnd)
                    return True

                win32gui.EnumWindows(_find, dialogs)

                for hwnd in dialogs:
                    self._try_dismiss(hwnd)
            except Exception:
                pass
            time.sleep(self._poll_interval)

    def _try_dismiss(self, hwnd):
        buttons = []

        def _find_btn(child, lst):
            cls = win32gui.GetClassName(child)
            txt = win32gui.GetWindowText(child)
            if cls == 'Button' and txt:
                lst.append((child, txt))
            return True

        try:
            win32gui.EnumChildWindows(hwnd, _find_btn, buttons)
        except Exception:
            return

        # Prefer "Keep existing file", fall back to "OK"
        for target in ('Keep existing file', 'OK'):
            for child_hwnd, txt in buttons:
                if txt == target:
                    try:
                        win32gui.PostMessage(child_hwnd,
                                             win32con.BM_CLICK, 0, 0)
                        self._dismissed += 1
                        logger.debug("Auto-dismissed dialog: clicked '%s'",
                                     txt)
                    except Exception:
                        pass
                    return


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _process_isempty_guards(afl_content: str) -> str:
    """Process ``if( IsEmpty(var) ) { ... }`` guard blocks in expanded AFL.

    Include files use ``IsEmpty()`` guards to provide defaults when used
    standalone.  Inside ``TimeFrameSet``, the ``IsEmpty()`` call causes
    AmiBroker's Exploration to silently produce zero output.

    This function handles each guard according to context:

    * Variable **already assigned** earlier in the AFL → strip the guard
      entirely (the parent strategy set it; the guard is unnecessary).
    * Variable **not yet assigned** → convert the body to an unconditional
      default assignment, replacing ``Param()`` calls with their default
      values.  This preserves the intended default without ``IsEmpty()``.
    """
    lines = afl_content.split('\n')
    result: list[str] = []
    assigned_vars: set[str] = set()
    i = 0
    changed = False

    while i < len(lines):
        line = lines[i]
        stripped_line = line.strip()

        # Track variable assignments (skip comments and blank lines)
        if stripped_line and not stripped_line.startswith('//'):
            assign_m = re.match(r'(\w+)\s*=\s*[^=]', stripped_line)
            if assign_m and ';' in stripped_line:
                assigned_vars.add(assign_m.group(1))

        # Detect IsEmpty guard:  if( IsEmpty( varName ) )
        ie_match = re.match(
            r'\s*if\s*\(\s*IsEmpty\s*\(\s*(\w+)\s*\)\s*\)',
            line, re.IGNORECASE,
        )
        if ie_match:
            var_name = ie_match.group(1)
            changed = True

            # Collect the guard body (lines between { and })
            body_lines: list[str] = []
            brace_depth = 0

            # Check if { is on the same line as the if
            after_match = line[ie_match.end():]
            if '{' in after_match:
                brace_depth = 1
                i += 1
            else:
                # Skip ahead to the opening brace line
                i += 1
                while i < len(lines):
                    if '{' in lines[i]:
                        brace_depth = 1
                        i += 1
                        break
                    i += 1

            # Collect body until the closing brace
            while i < len(lines) and brace_depth > 0:
                cur = lines[i]
                if '}' in cur:
                    brace_depth -= 1
                    if brace_depth <= 0:
                        i += 1
                        break
                if '{' in cur:
                    brace_depth += 1
                body_lines.append(cur)
                i += 1

            if var_name in assigned_vars:
                # Variable already defined — strip the entire guard
                logger.debug("IsEmpty(%s): stripped (already assigned)",
                             var_name)
            else:
                # Variable NOT defined — keep body as unconditional default
                for bl in body_lines:
                    bl_stripped = bl.strip()
                    if not bl_stripped:
                        continue
                    # Replace Param("Name", default, ...) with just default
                    param_m = re.match(
                        r'(\w+)\s*=\s*Param\s*\(\s*"[^"]*"\s*,\s*'
                        r'([\d.eE+-]+)',
                        bl_stripped, re.IGNORECASE,
                    )
                    if param_m:
                        vname = param_m.group(1)
                        default_val = param_m.group(2)
                        result.append(f'{vname} = {default_val};')
                        assigned_vars.add(vname)
                        logger.debug(
                            "IsEmpty(%s): set default %s = %s",
                            var_name, vname, default_val,
                        )
                    else:
                        result.append(bl_stripped)
                        a_m = re.match(r'(\w+)\s*=', bl_stripped)
                        if a_m:
                            assigned_vars.add(a_m.group(1))
                        logger.debug(
                            "IsEmpty(%s): kept body: %s",
                            var_name, bl_stripped,
                        )
            continue

        result.append(line)
        i += 1

    if changed:
        logger.info("Processed IsEmpty guards (tracked %d assigned vars)",
                     len(assigned_vars))
    return '\n'.join(result)


def _expand_includes(afl_content: str) -> str:
    """Inline ``#include_once`` directives so the AFL is self-contained.

    AmiBroker shows a "Choose action" dialog when an APX formula contains
    ``#include_once`` because it expands the include and detects a difference
    between the expanded formula in memory and the file on disk.  Resolving
    includes before writing the APX eliminates the dialog entirely.
    """
    include_pattern = re.compile(
        r'^\s*#include_once\s+"([^"]+)"\s*$', re.MULTILINE)

    seen: set[str] = set()

    def _replace(m):
        inc_path = Path(m.group(1))
        # Normalize to avoid re-including the same file
        key = str(inc_path.resolve())
        if key in seen:
            return f"// (already included: {inc_path.name})"
        seen.add(key)
        if not inc_path.exists():
            logger.warning("Include file not found: %s", inc_path)
            return m.group(0)  # leave the directive as-is
        inc_text = inc_path.read_text(encoding="utf-8")
        # Strip non-ASCII chars (e.g. σ in comments) so the AFL can be
        # encoded as iso-8859-1 for AmiBroker's APX format.
        inc_text = inc_text.encode("ascii", errors="replace").decode("ascii")
        logger.debug("Expanded include: %s (%d chars)", inc_path.name,
                      len(inc_text))
        # Recursively expand nested includes
        inc_text = include_pattern.sub(_replace, inc_text)
        return f"// ---- inlined from {inc_path.name} ----\n{inc_text}\n// ---- end {inc_path.name} ----"

    return include_pattern.sub(_replace, afl_content)


def _unix_to_datenum_timenum(unix_ts: int) -> tuple[int, int]:
    """Convert Unix timestamp to AmiBroker DateNum and TimeNum.

    AmiBroker's ``DateNum()`` / ``TimeNum()`` reflect the **system local
    timezone** (matching the Windows timezone setting on the machine where
    AmiBroker runs).  The conversion must therefore use local time — not
    UTC — to produce values that match the AmiBroker Exploration output.

    DateNum format: 1YYMMDD for years 2000+ (e.g. 1260210 = Feb 10, 2026)
    TimeNum format: HHMMSS  (e.g. 204200 = 8:42:00 PM)
    """
    dt = datetime.fromtimestamp(unix_ts)
    year = dt.year
    if year >= 2000:
        datenum = 1000000 + (year - 2000) * 10000 + dt.month * 100 + dt.day
    else:
        datenum = (year - 1900) * 10000 + dt.month * 100 + dt.day
    timenum = dt.hour * 10000 + dt.minute * 100 + dt.second
    return datenum, timenum


def _extract_variables_from_conditions(conditions: list[str]) -> list[str]:
    """Extract unique variable/function names from condition strings.

    For conditions like 'Cross(temas, Close)', 'plusDI > minusDI',
    'asianSession', returns the variable tokens that can be added as
    AddColumn() calls for numeric value inspection.
    """
    variables = set()
    for cond in conditions:
        # Extract word tokens (variable names)
        tokens = re.findall(r'\b([a-zA-Z_]\w*)\b', cond)
        for tok in tokens:
            # Skip AFL built-in functions and keywords
            if tok.lower() in ('cross', 'ref', 'iif', 'and', 'or', 'not',
                               'abs', 'max', 'min', 'close', 'open',
                               'high', 'low', 'volume'):
                continue
            variables.add(tok)
    return sorted(variables)


def _cleanup_stale_explore_files(apx_dir: Path, max_age_hours: int = 1):
    """Remove leftover exploration files from previous runs.

    Files may persist if AmiBroker had them locked during cleanup.
    Only deletes files older than *max_age_hours* to avoid removing
    files from a currently-running analysis.
    """
    import time as _time

    cutoff = _time.time() - max_age_hours * 3600
    patterns = ["explore_analyze_*.afl", "explore_analyze_*.apx",
                "explore_analyze_*.csv", "_src_*.afl"]
    for pattern in patterns:
        for f in apx_dir.glob(pattern):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    logger.debug("Cleaned up stale explore file: %s", f.name)
            except Exception:
                pass


def _replace_params_with_values(afl_content: str,
                                param_values: dict[str, float]) -> str:
    """Replace Param() and Optimize() calls with current slider values.

    This ensures the Exploration uses the user's current parameter settings
    rather than the AFL defaults.
    """
    if not param_values:
        return afl_content

    def _replace_match(m):
        full_match = m.group(0)
        param_name = m.group(1)
        if param_name in param_values:
            val = param_values[param_name]
            # Use integer format if value is whole number
            if val == int(val):
                return str(int(val))
            return str(val)
        return full_match

    # Replace Param("Name", default, min, max, step) with value
    pattern = re.compile(
        r'Param\s*\(\s*"([^"]+)"\s*,'
        r'\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*\)',
        re.IGNORECASE,
    )
    result = pattern.sub(_replace_match, afl_content)

    # Also handle Optimize("Name", default, min, max, step)
    pattern_opt = re.compile(
        r'Optimize\s*\(\s*"([^"]+)"\s*,'
        r'\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*\)',
        re.IGNORECASE,
    )
    result = pattern_opt.sub(_replace_match, result)

    return result


def _strip_trading_directives(afl_content: str) -> str:
    """Remove trading directives that are not needed for Exploration.

    Strips: ApplyStop, SetPositionSize, Plot, PlotShapes, PlotOHLC, Title.
    Handles multi-line statements (e.g. Title = ... + ... + ...;) by
    continuing to skip lines until a semicolon terminates the statement.
    """
    directive_re = re.compile(
        r'^\s*(ApplyStop|SetPositionSize|Plot\s*\(|PlotShapes|'
        r'PlotOHLC|Title\s*=)', re.IGNORECASE)
    lines = afl_content.split('\n')
    filtered = []
    skipping = False
    for line in lines:
        if skipping:
            # Continue skipping until we find the terminating semicolon
            if ';' in line:
                skipping = False
            continue
        if directive_re.match(line.strip()):
            # Check if the statement ends on this line
            if ';' not in line:
                skipping = True
            continue
        filtered.append(line)
    return '\n'.join(filtered)


def _generate_exploration_afl(
    afl_content: str,
    buy_conditions: list[str],
    short_conditions: list[str],
    key_variables: list[str],
    datenum: int,
    timenum: int,
    param_values: dict[str, float],
    buy_expr: Optional[str],
    short_expr: Optional[str],
) -> str:
    """Generate the Exploration AFL with AddColumn() for each condition.

    For strategies with ``TimeFrameSet``/``TimeFrameRestore``:
      1. Injects ``_explDN``/``_explTN`` capture before TimeFrameRestore
         (so DateNum/TimeNum are in 1-minute resolution).
      2. Injects TimeFrameExpand for those vars after the last
         TimeFrameExpand line.
      3. Appends the Filter + AddColumn block at the end, referencing
         the expanded ``_explDN``/``_explTN``.

    For strategies without TimeFrameSet, appends everything at the end
    using raw ``DateNum()``/``TimeNum()``.
    """
    # Step 1: Replace Param() calls with current values
    afl = _replace_params_with_values(afl_content, param_values)

    # Step 2: Strip trading directives (Plot, ApplyStop, etc.)
    afl = _strip_trading_directives(afl)

    # Step 3: Detect TimeFrameSet / TimeFrameRestore
    tfr_pattern = re.compile(r'^(\s*TimeFrameRestore\s*\(\s*\)\s*;)',
                             re.IGNORECASE | re.MULTILINE)
    tfs_pattern = re.compile(r'TimeFrameSet\s*\(\s*(\w+)\s*\)',
                             re.IGNORECASE)
    tfr_match = tfr_pattern.search(afl)
    tfs_match = tfs_pattern.search(afl)
    uses_timeframe = tfr_match is not None and tfs_match is not None

    if uses_timeframe:
        tf_interval = tfs_match.group(1)  # e.g. "in1Minute"

        # 3a. Capture DateNum/TimeNum in the higher-resolution timeframe
        #     (before TimeFrameRestore) so they match the bars the user sees.
        capture_block = (
            '\n// ==== BAR ANALYSIS: capture DN/TN in 1-min timeframe ====\n'
            '_explDN = DateNum();\n'
            '_explTN = TimeNum();\n'
        )
        insert_pos = tfr_match.start()
        afl = afl[:insert_pos] + capture_block + afl[insert_pos:]

        # 3b. Expand _explDN/_explTN after the last TimeFrameExpand line.
        #     Re-search because the AFL string has shifted.
        tfe_pattern = re.compile(
            r'^(\s*\w+\s*=\s*TimeFrameExpand\s*\(.+?\)\s*;)',
            re.IGNORECASE | re.MULTILINE)
        tfe_matches = list(tfe_pattern.finditer(afl))
        if tfe_matches:
            last_tfe_end = tfe_matches[-1].end()
        else:
            # Fallback: insert after TimeFrameRestore
            tfr_match2 = tfr_pattern.search(afl)
            last_tfe_end = tfr_match2.end() if tfr_match2 else len(afl)

        expand_block = (
            f'\n_explDN = TimeFrameExpand(_explDN, {tf_interval});\n'
            f'_explTN = TimeFrameExpand(_explTN, {tf_interval});\n'
        )
        afl = afl[:last_tfe_end] + expand_block + afl[last_tfe_end:]

        # Filter uses the expanded 1-min DateNum/TimeNum
        dn_expr = '_explDN'
        tn_expr = '_explTN'
        logger.info("Injected _explDN/_explTN capture + expand for TimeFrameSet AFL")
    else:
        # No timeframe switching — use raw DateNum()/TimeNum()
        dn_expr = 'DateNum()'
        tn_expr = 'TimeNum()'

    # Step 4: Build the AddColumn block
    explore_lines = []
    explore_lines.append('')
    explore_lines.append('// ==== BAR ANALYSIS EXPLORATION ====')
    explore_lines.append(f'_exploreDate = {datenum};')
    explore_lines.append(f'_exploreTime = {timenum};')
    # Use a 60-second window so the filter matches even when the
    # frontend timestamp doesn't align exactly to a bar boundary.
    # TimeNum is HHMMSS, so +/- 100 gives a ~60-second window.
    explore_lines.append(f'Filter = {dn_expr} == _exploreDate '
                         f'AND {tn_expr} >= _exploreTime - 100 '
                         f'AND {tn_expr} <= _exploreTime + 100;')
    explore_lines.append('')

    # Individual Buy conditions
    for i, cond in enumerate(buy_conditions):
        col_name = f"buy_{i}"
        explore_lines.append(f'AddColumn({cond}, "{col_name}", 1.0);')

    # Overall buySignal (pre-ExRem)
    if buy_expr:
        explore_lines.append(f'AddColumn(({buy_expr}), "buySignal_all", 1.0);')

    explore_lines.append('')

    # Individual Short conditions
    for i, cond in enumerate(short_conditions):
        col_name = f"short_{i}"
        explore_lines.append(f'AddColumn({cond}, "{col_name}", 1.0);')

    # Overall shortSignal (pre-ExRem)
    if short_expr:
        explore_lines.append(f'AddColumn(({short_expr}), "shortSignal_all", 1.0);')

    explore_lines.append('')

    # Key variable values
    for var in key_variables:
        explore_lines.append(f'AddColumn({var}, "val_{var}", 1.4);')

    # Also add Close for reference
    explore_lines.append('AddColumn(Close, "val_Close", 1.4);')
    explore_lines.append('AddColumn(Open, "val_Open", 1.4);')
    explore_lines.append('AddColumn(High, "val_High", 1.4);')
    explore_lines.append('AddColumn(Low, "val_Low", 1.4);')
    explore_lines.append('AddColumn(Volume, "val_Volume", 1.0);')
    explore_lines.append('// ==== END BAR ANALYSIS ====')

    explore_block = '\n'.join(explore_lines)

    # Step 5: Append at the very end of the AFL.
    afl = afl + '\n' + explore_block + '\n'
    logger.info("Appended exploration block at end of AFL")

    return afl


def _parse_exploration_csv(
    csv_path: str,
    buy_conditions: list[str],
    short_conditions: list[str],
    key_variables: list[str],
    target_timenum: int = 0,
) -> Optional[dict]:
    """Parse the AmiBroker Exploration CSV export.

    Returns a dict with column values from the row closest to
    *target_timenum*, or None if no data row was found.
    """
    csv_file = Path(csv_path)
    if not csv_file.exists():
        logger.error("Exploration CSV not found: %s", csv_path)
        return None

    try:
        # AmiBroker CSV may have BOM or varied encoding
        content = csv_file.read_text(encoding='utf-8-sig')
        if not content.strip():
            logger.warning("Exploration CSV is empty: %s", csv_path)
            return None

        reader = csv.DictReader(content.strip().splitlines())
        rows = list(reader)
        if not rows:
            logger.warning("No data rows in exploration CSV: %s", csv_path)
            return None

        logger.info("Parsed exploration CSV: %d rows, %d columns",
                     len(rows), len(rows[0]))

        # When multiple rows match (time-window filter), pick the one
        # whose Date/Time column is closest to the target TimeNum.
        if len(rows) == 1 or target_timenum == 0:
            row = rows[0]
        else:
            def _row_timenum(r):
                dt_str = r.get("Date/Time", "")
                # AmiBroker CSV Date/Time: "MM/DD/YYYY HH:MM:SS" (24h)
                try:
                    parts = dt_str.strip().split()
                    if len(parts) >= 2:
                        h, m, s = (int(x) for x in parts[1].split(":"))
                        return h * 10000 + m * 100 + s
                except Exception:
                    pass
                return 0
            row = min(rows,
                      key=lambda r: abs(_row_timenum(r) - target_timenum))
            logger.info("Selected row with Date/Time=%s (closest to %d)",
                        row.get("Date/Time", "?"), target_timenum)

        result = {
            "buy_conditions": {},
            "short_conditions": {},
            "variable_values": {},
            "raw_row": dict(row),
        }

        # Extract buy condition values
        for i, cond in enumerate(buy_conditions):
            col_name = f"buy_{i}"
            for csv_col in row:
                if col_name in csv_col.lower().replace(' ', '_'):
                    try:
                        result["buy_conditions"][i] = float(row[csv_col])
                    except (ValueError, TypeError):
                        result["buy_conditions"][i] = None

        # Extract short condition values
        for i, cond in enumerate(short_conditions):
            col_name = f"short_{i}"
            for csv_col in row:
                if col_name in csv_col.lower().replace(' ', '_'):
                    try:
                        result["short_conditions"][i] = float(row[csv_col])
                    except (ValueError, TypeError):
                        result["short_conditions"][i] = None

        # Extract overall signals
        for csv_col in row:
            if 'buysignal_all' in csv_col.lower().replace(' ', '_'):
                try:
                    result["buySignal_all"] = float(row[csv_col])
                except (ValueError, TypeError):
                    pass
            if 'shortsignal_all' in csv_col.lower().replace(' ', '_'):
                try:
                    result["shortSignal_all"] = float(row[csv_col])
                except (ValueError, TypeError):
                    pass

        # Extract variable values
        for var in key_variables + ['Close', 'Open', 'High', 'Low', 'Volume']:
            search_key = f"val_{var}".lower()
            for csv_col in row:
                if search_key in csv_col.lower().replace(' ', '_'):
                    try:
                        result["variable_values"][var] = float(row[csv_col])
                    except (ValueError, TypeError):
                        result["variable_values"][var] = None

        return result

    except Exception as exc:
        logger.error("Failed to parse exploration CSV: %s", exc)
        return None


def _build_condition_details(
    cond_text: str,
    passed: bool,
    variable_values: dict[str, float],
) -> str:
    """Build a human-readable details string for a condition result."""
    # Cross(A, B)
    cross_m = re.match(r'Cross\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)',
                       cond_text, re.IGNORECASE)
    if cross_m:
        a_name = cross_m.group(1)
        b_name = cross_m.group(2)
        a_val = variable_values.get(a_name)
        b_val = variable_values.get(b_name)
        parts = []
        if a_val is not None:
            parts.append(f"{a_name} = {a_val:.2f}")
        if b_val is not None:
            parts.append(f"{b_name} = {b_val:.2f}")
        detail = ', '.join(parts) if parts else ''
        if passed:
            return f"{detail} (crossover detected)" if detail else "Crossover detected"
        return f"{detail} (no crossover)" if detail else "No crossover"

    # A op B comparison
    comp_m = re.match(r'(\w+)\s*(>=|<=|>|<|==)\s*(\w+)', cond_text)
    if comp_m:
        lhs_name = comp_m.group(1)
        op = comp_m.group(2)
        rhs_name = comp_m.group(3)
        lhs_val = variable_values.get(lhs_name)
        rhs_val = variable_values.get(rhs_name)
        parts = []
        if lhs_val is not None:
            parts.append(f"{lhs_name} = {lhs_val:.2f}")
        else:
            parts.append(lhs_name)
        parts.append(op)
        if rhs_val is not None:
            parts.append(f"{rhs_name} = {rhs_val:.2f}")
        else:
            parts.append(rhs_name)
        return ' '.join(parts)

    # Boolean variable
    if re.fullmatch(r'\w+', cond_text):
        val = variable_values.get(cond_text)
        if val is not None:
            return f"{cond_text} = {val:.1f} ({'True' if val > 0.5 else 'False'})"
        return cond_text

    return cond_text


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze_bar(
    afl_content: str,
    target_unix_ts: int,
    strategy_id: str,
    param_values: dict[str, float] = None,
) -> dict:
    """Analyze Buy/Short signal conditions at a specific bar using AmiBroker OLE.

    Generates a custom Exploration AFL, runs it through AmiBroker, and
    parses the results to show per-condition pass/fail with actual values.

    Parameters
    ----------
    afl_content : str
        Full AFL source code of the strategy.
    target_unix_ts : int
        Unix timestamp of the bar to analyze.
    strategy_id : str
        Strategy UUID (used for temp file naming).
    param_values : dict, optional
        Current parameter slider values (keyed by display name).

    Returns
    -------
    dict
        Analysis results with per-condition breakdown.
    """
    start_time = time.time()
    param_values = param_values or {}

    # ── Parse AFL to identify conditions ──
    stripped = _strip_comments(afl_content)
    var_defs = _extract_var_defs(stripped)

    buy_expr = _resolve_signal_expr(stripped, "Buy", var_defs)
    short_expr = _resolve_signal_expr(stripped, "Short", var_defs)

    buy_conditions = _split_and(buy_expr) if buy_expr else []
    short_conditions = _split_and(short_expr) if short_expr else []

    all_conditions = buy_conditions + short_conditions
    key_variables = _extract_variables_from_conditions(all_conditions)

    # Also add variables from var_defs that appear in conditions
    # (e.g., trendConfirmed is a variable that resolves to a comparison)
    for cond in all_conditions:
        if re.fullmatch(r'\w+', cond.strip()):
            var_name = cond.strip()
            if var_name in var_defs:
                # Add the variables from the resolved expression too
                resolved = var_defs[var_name]
                extra_vars = _extract_variables_from_conditions([resolved])
                for v in extra_vars:
                    if v not in key_variables:
                        key_variables.append(v)

    logger.info("Bar analysis: %d Buy conditions, %d Short conditions, "
                "%d key variables",
                len(buy_conditions), len(short_conditions), len(key_variables))

    # ── Convert timestamp to AmiBroker date/time ──
    datenum, timenum = _unix_to_datenum_timenum(target_unix_ts)
    target_dt = datetime.fromtimestamp(target_unix_ts)
    logger.info("Target bar: %s local (DateNum=%d, TimeNum=%d)",
                target_dt.strftime("%Y-%m-%d %H:%M:%S"), datenum, timenum)

    # ── Generate Exploration AFL ──
    explore_afl = _generate_exploration_afl(
        afl_content, buy_conditions, short_conditions,
        key_variables, datenum, timenum, param_values,
        buy_expr, short_expr,
    )

    # Inline #include_once directives so the AFL is self-contained.
    # AmiBroker shows a "Choose action" dialog for formulas with includes
    # because it expands them in memory and detects a content difference.
    explore_afl = _expand_includes(explore_afl)

    # Process IsEmpty() guards with full context.  Now that all includes
    # are inlined, we can see which variables the parent strategy already
    # set and strip only those guards.  Guards for variables the parent
    # did NOT set are converted to unconditional default assignments so
    # the variable is always defined.  (IsEmpty() inside TimeFrameSet
    # causes AmiBroker Exploration to silently produce zero output.)
    explore_afl = _process_isempty_guards(explore_afl)

    # ── Write temp files ──
    # Use a single AFL file that serves as both the source and the
    # FormulaPath target (snapshot).  Writing one file instead of two
    # eliminates encoding-related "formula mismatch" dialogs in AmiBroker.
    run_uuid = uuid.uuid4().hex[:12]
    temp_afl_path = APX_DIR / f"strategy_{run_uuid}.afl"
    temp_apx_path = APX_DIR / f"explore_analyze_{run_uuid}.apx"
    temp_csv_path = APX_DIR / f"explore_analyze_{run_uuid}.csv"

    try:
        # Clean up stale exploration files from previous runs that
        # failed to clean up (e.g. AmiBroker had files locked).
        _cleanup_stale_explore_files(APX_DIR)

        APX_DIR.mkdir(parents=True, exist_ok=True)

        # Write exploration AFL directly to the snapshot path (the file
        # that FormulaPath in the APX will reference).  build_apx reads
        # from this file and also re-writes it to guarantee CRLF encoding.
        afl_crlf = explore_afl.replace("\r\n", "\n").replace("\n", "\r\n")
        temp_afl_path.write_bytes(afl_crlf.encode("iso-8859-1"))
        logger.info("Wrote exploration AFL: %s (%d chars)",
                     temp_afl_path, len(explore_afl))

        # Build APX from template.
        # For strategies with TimeFrameSet(in1Minute), set the APX
        # periodicity to 1-minute (5) so that the base timeframe matches
        # the TimeFrameSet target.  This makes TimeFrameSet a no-op and
        # ensures DateNum()/TimeNum() are at 1-minute resolution for the
        # exploration Filter.  Using hourly periodicity (template default)
        # would cause the Filter to operate on hourly bars, missing the
        # specific minute-level bar the user clicked on.
        periodicity = None
        if "TimeFrameSet(" in explore_afl:
            periodicity = 5  # 1-minute
        build_apx(
            afl_path=str(temp_afl_path),
            output_apx_path=str(temp_apx_path),
            template_apx_path=str(APX_TEMPLATE),
            run_id=run_uuid,
            periodicity=periodicity,
        )
        logger.info("Built exploration APX: %s", temp_apx_path)

        # ── Run OLE Exploration ──
        # The _DialogAutoDismisser handles AmiBroker's "Choose action"
        # dialog (formula mismatch for AFL with <, >, &) and crash-
        # recovery dialogs, clicking "Keep existing file" / "OK" so the
        # correct AFL from the file on disk is used.
        with _DialogAutoDismisser():
            ab = None
            analysis_doc = None
            try:
                pythoncom.CoInitialize()
                logger.info("Connecting to AmiBroker for exploration...")
                ab = win32com.client.Dispatch(AMIBROKER_EXE)
                ab.LoadDatabase(AMIBROKER_DB_PATH)

                analysis_doc = ab.AnalysisDocs.Open(
                    str(temp_apx_path.resolve()))
                if analysis_doc is None:
                    return _error_result(
                        "AmiBroker could not open the exploration APX. "
                        "Check that AmiBroker is running.",
                        target_unix_ts, start_time,
                    )

                logger.info("Running Exploration (mode=%d)...",
                            EXPLORATION_RUN_MODE)
                analysis_doc.Run(EXPLORATION_RUN_MODE)

                # Poll until complete
                elapsed = 0.0
                while analysis_doc.IsBusy:
                    if elapsed >= EXPLORATION_MAX_WAIT:
                        return _error_result(
                            f"Exploration timed out after "
                            f"{EXPLORATION_MAX_WAIT}s",
                            target_unix_ts, start_time,
                        )
                    time.sleep(EXPLORATION_POLL_INTERVAL)
                    elapsed += EXPLORATION_POLL_INTERVAL

                logger.info("Exploration completed in %.1fs", elapsed)

                # Export CSV
                analysis_doc.Export(str(temp_csv_path.resolve()))
                logger.info("Exported exploration CSV: %s", temp_csv_path)

            except Exception as exc:
                logger.error("OLE Exploration failed: %s", exc)
                return _error_result(
                    f"AmiBroker OLE error: {exc}. Is AmiBroker running?",
                    target_unix_ts, start_time,
                )
            finally:
                if analysis_doc is not None:
                    try:
                        analysis_doc.Close()
                    except Exception:
                        pass
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

        # ── Parse CSV results ──
        csv_data = _parse_exploration_csv(
            str(temp_csv_path), buy_conditions, short_conditions,
            key_variables, target_timenum=timenum,
        )

        if csv_data is None:
            return _error_result(
                "Exploration returned no data for this bar. "
                "The bar may be outside the available data range.",
                target_unix_ts, start_time,
            )

        # ── Build response ──
        variable_values = csv_data.get("variable_values", {})
        elapsed_ms = int((time.time() - start_time) * 1000)

        # Build Buy condition results
        buy_result = {
            "expression": buy_expr or "",
            "overall": csv_data.get("buySignal_all", 0) > 0.5,
            "conditions": [],
        }
        for i, cond in enumerate(buy_conditions):
            val = csv_data["buy_conditions"].get(i)
            passed = val is not None and val > 0.5
            buy_result["conditions"].append({
                "text": cond,
                "passed": passed,
                "details": _build_condition_details(cond, passed, variable_values),
            })

        # Build Short condition results
        short_result = {
            "expression": short_expr or "",
            "overall": csv_data.get("shortSignal_all", 0) > 0.5,
            "conditions": [],
        }
        for i, cond in enumerate(short_conditions):
            val = csv_data["short_conditions"].get(i)
            passed = val is not None and val > 0.5
            short_result["conditions"].append({
                "text": cond,
                "passed": passed,
                "details": _build_condition_details(cond, passed, variable_values),
            })

        return {
            "bar_time": target_unix_ts,
            "bar_ohlcv": {
                "open": variable_values.get("Open"),
                "high": variable_values.get("High"),
                "low": variable_values.get("Low"),
                "close": variable_values.get("Close"),
                "volume": variable_values.get("Volume"),
            },
            "buy": buy_result,
            "short": short_result,
            "variable_values": {
                k: v for k, v in variable_values.items()
                if k not in ('Open', 'High', 'Low', 'Close', 'Volume')
            },
            "elapsed_ms": elapsed_ms,
            "error": None,
        }

    finally:
        # Clean up temp files (AFL, APX, CSV)
        for f in [temp_afl_path, temp_apx_path, temp_csv_path]:
            try:
                if f.exists():
                    f.unlink()
            except Exception:
                pass


def _error_result(message: str, bar_time: int, start_time: float) -> dict:
    """Build an error response dict."""
    return {
        "bar_time": bar_time,
        "bar_ohlcv": None,
        "buy": {"expression": "", "overall": False, "conditions": []},
        "short": {"expression": "", "overall": False, "conditions": []},
        "variable_values": {},
        "elapsed_ms": int((time.time() - start_time) * 1000),
        "error": message,
    }


# ---------------------------------------------------------------------------
# Signal computation via OLE Exploration
# ---------------------------------------------------------------------------

# Map chart interval (seconds) to AmiBroker APX periodicity values
_INTERVAL_TO_PERIODICITY = {
    60: 5,      # 1-minute
    300: 6,     # 5-minute
    600: 7,     # 15-minute (closest)
    3600: 9,    # Hourly
    86400: 11,  # Daily
}


def _generate_signal_exploration_afl(
    afl_content: str,
    param_values: dict[str, float],
    symbol: str = None,
) -> str:
    """Generate an Exploration AFL that outputs Buy/Short signals for all bars.

    Takes the full strategy AFL, replaces Param/Optimize with current slider
    values, strips visual directives, inlines includes, and appends a
    ``Filter = Buy OR Short`` exploration block so AmiBroker exports only
    the bars where entry signals fire.

    When *symbol* is provided, the Filter also includes ``Name() == "symbol"``
    so the exploration only processes the target symbol (used with ApplyTo=0
    to avoid dependence on AmiBroker's active chart window).
    """
    # 1. Replace Param/Optimize calls with current values
    afl = _replace_params_with_values(afl_content, param_values)

    # 2. Expand #include_once directives
    afl = _expand_includes(afl)

    # 3. Process IsEmpty() guards
    afl = _process_isempty_guards(afl)

    # 4. Strip visual directives (Plot, PlotShapes, Title)
    afl = _strip_trading_directives(afl)

    # 5. Detect TimeFrameSet and set up DateNum/TimeNum capture
    tfr_pattern = re.compile(r'^(\s*TimeFrameRestore\s*\(\s*\)\s*;)',
                             re.IGNORECASE | re.MULTILINE)
    tfs_pattern = re.compile(r'TimeFrameSet\s*\(\s*(\w+)\s*\)',
                             re.IGNORECASE)
    tfr_match = tfr_pattern.search(afl)
    tfs_match = tfs_pattern.search(afl)
    uses_timeframe = tfr_match is not None and tfs_match is not None

    if uses_timeframe:
        tf_interval = tfs_match.group(1)  # e.g. "in1Minute"

        # Capture DateNum/TimeNum before TimeFrameRestore (in higher-res TF)
        capture_block = (
            '\n// ==== SIGNAL EXPLORATION: capture DN/TN in computed timeframe ====\n'
            '_sigDN = DateNum();\n'
            '_sigTN = TimeNum();\n'
        )
        insert_pos = tfr_match.start()
        afl = afl[:insert_pos] + capture_block + afl[insert_pos:]

        # Expand _sigDN/_sigTN after the last TimeFrameExpand line
        tfe_pattern = re.compile(
            r'^(\s*\w+\s*=\s*TimeFrameExpand\s*\(.+?\)\s*;)',
            re.IGNORECASE | re.MULTILINE)
        tfe_matches = list(tfe_pattern.finditer(afl))
        if tfe_matches:
            last_tfe_end = tfe_matches[-1].end()
        else:
            tfr_match2 = tfr_pattern.search(afl)
            last_tfe_end = tfr_match2.end() if tfr_match2 else len(afl)

        expand_block = (
            f'\n_sigDN = TimeFrameExpand(_sigDN, {tf_interval});\n'
            f'_sigTN = TimeFrameExpand(_sigTN, {tf_interval});\n'
        )
        afl = afl[:last_tfe_end] + expand_block + afl[last_tfe_end:]

        dn_expr = '_sigDN'
        tn_expr = '_sigTN'
    else:
        dn_expr = 'DateNum()'
        tn_expr = 'TimeNum()'

    # 6. Append the signal exploration block
    # When a symbol is specified, add Name() == "symbol" to the Filter so
    # the exploration only processes the target ticker (ApplyTo=0 iterates
    # all symbols; the Name() check filters to just the one we want).
    if symbol:
        escaped = symbol.replace('"', '\\"')
        filter_expr = f'(Buy OR Short) AND Name() == "{escaped}"'
    else:
        filter_expr = 'Buy OR Short'

    explore_block = (
        '\n\n// ==== SIGNAL EXPLORATION ====\n'
        f'Filter = {filter_expr};\n'
        'AddColumn(Buy, "Buy", 1.0);\n'
        'AddColumn(Short, "Short", 1.0);\n'
        f'AddColumn({dn_expr}, "DN", 1.0);\n'
        f'AddColumn({tn_expr}, "TN", 1.0);\n'
        '// ==== END SIGNAL EXPLORATION ====\n'
    )
    afl += explore_block

    return afl


def _parse_signal_csv(csv_path: str) -> dict:
    """Parse the signal Exploration CSV and return Buy/Short/Sell/Cover timestamps.

    The CSV contains one row per bar where Buy or Short fired.
    Each row has columns: Symbol, Date/Time, Buy, Short, DN, TN.
    """
    csv_file = Path(csv_path)
    if not csv_file.exists():
        return {"buy": [], "short": [], "sell": [], "cover": [],
                "error": "Exploration CSV not found"}

    try:
        content = csv_file.read_text(encoding='utf-8-sig')
        if not content.strip():
            return {"buy": [], "short": [], "sell": [], "cover": [],
                    "error": None}

        reader = csv.DictReader(content.strip().splitlines())
        rows = list(reader)
        if not rows:
            return {"buy": [], "short": [], "sell": [], "cover": [],
                    "error": None}

        logger.info("Signal CSV: %d rows", len(rows))

        buy_signals = []
        short_signals = []

        for row in rows:
            # Parse Date/Time to Unix timestamp
            # AmiBroker CSV format: "MM/DD/YYYY HH:MM:SS" (24h)
            dt_str = row.get("Date/Time", "").strip()
            unix_ts = None
            if dt_str:
                try:
                    dt = datetime.strptime(dt_str, "%m/%d/%Y %H:%M:%S")
                    unix_ts = int(dt.timestamp())
                except ValueError:
                    # Try date-only format
                    try:
                        dt = datetime.strptime(dt_str, "%m/%d/%Y")
                        unix_ts = int(dt.timestamp())
                    except ValueError:
                        logger.warning("Cannot parse Date/Time: %s", dt_str)
                        continue

            if unix_ts is None:
                continue

            # Check Buy/Short values
            buy_val = 0
            short_val = 0
            for col_name, col_val in row.items():
                col_lower = col_name.strip().lower()
                if col_lower == "buy":
                    try:
                        buy_val = float(col_val)
                    except (ValueError, TypeError):
                        pass
                elif col_lower == "short":
                    try:
                        short_val = float(col_val)
                    except (ValueError, TypeError):
                        pass

            if buy_val > 0.5:
                buy_signals.append({"time": unix_ts})
            if short_val > 0.5:
                short_signals.append({"time": unix_ts})

        # Derive Sell/Cover from ExRem alternation
        # After ExRem, Buy and Short alternate. A Buy after Short = Cover,
        # a Short after Buy = Sell.
        sell_signals = []
        cover_signals = []

        # Merge and sort all signals by time
        all_signals = (
            [("buy", s["time"]) for s in buy_signals] +
            [("short", s["time"]) for s in short_signals]
        )
        all_signals.sort(key=lambda x: x[1])

        in_long = False
        in_short = False
        for sig_type, sig_time in all_signals:
            if sig_type == "buy":
                if in_short:
                    cover_signals.append({"time": sig_time})
                    in_short = False
                in_long = True
            elif sig_type == "short":
                if in_long:
                    sell_signals.append({"time": sig_time})
                    in_long = False
                in_short = True

        logger.info("Parsed signals: %d Buy, %d Short, %d Sell, %d Cover",
                     len(buy_signals), len(short_signals),
                     len(sell_signals), len(cover_signals))

        return {
            "buy": buy_signals,
            "short": short_signals,
            "sell": sell_signals,
            "cover": cover_signals,
            "error": None,
        }

    except Exception as exc:
        logger.error("Failed to parse signal CSV: %s", exc)
        return {"buy": [], "short": [], "sell": [], "cover": [],
                "error": str(exc)}


def compute_signals_via_exploration(
    afl_content: str,
    param_values: dict[str, float] = None,
    symbol: str = None,
    interval: int = 60,
) -> dict:
    """Compute Buy/Short/Sell/Cover signals using AmiBroker OLE Exploration.

    Runs the full strategy AFL through AmiBroker's Exploration engine with
    current parameter values, extracting signal timestamps from the results.
    This ensures 100% accuracy — all AFL constructs (#include, for loops,
    ApplyStop, ExRem, etc.) are evaluated by AmiBroker's native engine.

    Parameters
    ----------
    afl_content : str
        Full AFL source code of the strategy.
    param_values : dict, optional
        Current slider values keyed by display name.
    symbol : str, optional
        Ticker symbol (defaults to DEFAULT_SYMBOL).
    interval : int
        Chart interval in seconds (used to set APX periodicity).

    Returns
    -------
    dict
        ``buy``, ``short``, ``sell``, ``cover`` — lists of ``{"time": unix_ts}``.
        ``elapsed_ms`` — computation time in milliseconds.
        ``error`` — error message or None.
    """
    start_time = time.time()
    param_values = param_values or {}
    symbol = symbol or DEFAULT_SYMBOL

    # Generate the Exploration AFL with symbol filter baked into Filter expr.
    # The AFL Filter uses Name() == "symbol" so AmiBroker only processes
    # bars for the target ticker (ApplyTo=0 iterates all symbols).
    explore_afl = _generate_signal_exploration_afl(
        afl_content, param_values, symbol=symbol)

    # Determine periodicity
    periodicity = None
    if "TimeFrameSet(" in explore_afl:
        periodicity = 5  # 1-minute (match TimeFrameSet target)
    elif interval in _INTERVAL_TO_PERIODICITY:
        periodicity = _INTERVAL_TO_PERIODICITY[interval]

    # Write temp files
    run_uuid = uuid.uuid4().hex[:12]
    temp_afl_path = APX_DIR / f"strategy_{run_uuid}.afl"
    temp_apx_path = APX_DIR / f"signal_explore_{run_uuid}.apx"
    temp_csv_path = APX_DIR / f"signal_explore_{run_uuid}.csv"

    try:
        _cleanup_stale_explore_files(APX_DIR)
        APX_DIR.mkdir(parents=True, exist_ok=True)

        # Write AFL with CRLF encoding
        afl_crlf = explore_afl.replace("\r\n", "\n").replace("\n", "\r\n")
        temp_afl_path.write_bytes(afl_crlf.encode("iso-8859-1"))
        logger.info("Wrote signal exploration AFL: %s (%d chars)",
                     temp_afl_path, len(explore_afl))

        # Build APX with ApplyTo=0 (All Symbols).  The AFL Filter already
        # contains Name() == "symbol" so only the target ticker is processed.
        # Using "__ALL__" sets ApplyTo=0 in the APX, making the exploration
        # independent of whatever symbol happens to be active in AmiBroker's
        # chart window (ApplyTo=1 "Current" would use that instead).
        build_apx(
            afl_path=str(temp_afl_path),
            output_apx_path=str(temp_apx_path),
            template_apx_path=str(APX_TEMPLATE),
            run_id=run_uuid,
            periodicity=periodicity,
            symbol="__ALL__",
        )
        logger.info("Built signal exploration APX: %s (symbol=%s, periodicity=%s)",
                     temp_apx_path, symbol, periodicity)

        # Run OLE Exploration
        with _DialogAutoDismisser():
            ab = None
            analysis_doc = None
            try:
                pythoncom.CoInitialize()
                logger.info("Connecting to AmiBroker for signal exploration...")
                ab = win32com.client.Dispatch(AMIBROKER_EXE)
                ab.LoadDatabase(AMIBROKER_DB_PATH)

                analysis_doc = ab.AnalysisDocs.Open(
                    str(temp_apx_path.resolve()))
                if analysis_doc is None:
                    elapsed_ms = int((time.time() - start_time) * 1000)
                    return {
                        "buy": [], "short": [], "sell": [], "cover": [],
                        "elapsed_ms": elapsed_ms,
                        "error": "AmiBroker could not open the exploration APX.",
                    }

                logger.info("Running signal Exploration (mode=%d)...",
                            EXPLORATION_RUN_MODE)
                analysis_doc.Run(EXPLORATION_RUN_MODE)

                # Poll until complete
                elapsed = 0.0
                while analysis_doc.IsBusy:
                    if elapsed >= EXPLORATION_MAX_WAIT:
                        elapsed_ms = int((time.time() - start_time) * 1000)
                        return {
                            "buy": [], "short": [], "sell": [], "cover": [],
                            "elapsed_ms": elapsed_ms,
                            "error": f"Exploration timed out after {EXPLORATION_MAX_WAIT}s",
                        }
                    time.sleep(EXPLORATION_POLL_INTERVAL)
                    elapsed += EXPLORATION_POLL_INTERVAL

                logger.info("Signal Exploration completed in %.1fs", elapsed)

                # Export CSV
                analysis_doc.Export(str(temp_csv_path.resolve()))
                logger.info("Exported signal CSV: %s", temp_csv_path)

            except Exception as exc:
                logger.error("OLE signal Exploration failed: %s", exc)
                elapsed_ms = int((time.time() - start_time) * 1000)
                return {
                    "buy": [], "short": [], "sell": [], "cover": [],
                    "elapsed_ms": elapsed_ms,
                    "error": f"AmiBroker OLE error: {exc}. Is AmiBroker running?",
                }
            finally:
                if analysis_doc is not None:
                    try:
                        analysis_doc.Close()
                    except Exception:
                        pass
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

        # Parse CSV results
        result = _parse_signal_csv(str(temp_csv_path))
        result["elapsed_ms"] = int((time.time() - start_time) * 1000)

        logger.info("Signal computation complete: %d Buy, %d Short, "
                     "%d Sell, %d Cover (%.1fs)",
                     len(result["buy"]), len(result["short"]),
                     len(result["sell"]), len(result["cover"]),
                     result["elapsed_ms"] / 1000)

        return result

    finally:
        # Clean up temp files
        for f in [temp_afl_path, temp_apx_path, temp_csv_path]:
            try:
                if f.exists():
                    f.unlink()
            except Exception:
                pass
