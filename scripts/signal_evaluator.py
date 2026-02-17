"""
AFL signal parsing utilities.

Provides helpers for extracting Buy/Short signal expressions and variable
definitions from AFL source code.  These are used by :mod:`ole_bar_analyzer`
for the single-bar "Why no trade?" analysis feature.

Actual signal computation is handled by AmiBroker's native AFL engine via
OLE Exploration (see :func:`ole_bar_analyzer.compute_signals_via_exploration`).
"""

import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AFL parsing helpers
# ---------------------------------------------------------------------------

def _strip_comments(afl: str) -> str:
    """Remove AFL block and line comments."""
    result = re.sub(r"/\*.*?\*/", "", afl, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", result)


def _extract_var_defs(afl_stripped: str) -> dict[str, str]:
    """Extract ``varName = expr;`` assignments from stripped AFL.

    Returns a dict mapping variable names (original case) to their
    right-hand-side expression strings.  When a variable is assigned
    multiple times (e.g. first as a signal expression, then wrapped
    in ``TimeFrameExpand``), the **non-TimeFrame** definition is
    preferred so signal conditions can be resolved.
    """
    defs: dict[str, str] = {}
    # Track all definitions per variable to pick the best one
    all_defs: dict[str, list[str]] = {}
    skip = {"buy", "sell", "short", "cover",
            "setoption", "setpositionsize",
            "applystop", "settradedelays"}

    for m in re.finditer(r'\b(\w+)\s*=\s*(.+?)\s*;', afl_stripped,
                         flags=re.DOTALL):
        var_name = m.group(1)
        expr = re.sub(r'\s+', ' ', m.group(2).strip())
        if var_name.lower() in skip:
            continue
        all_defs.setdefault(var_name, []).append(expr)

    for var_name, exprs in all_defs.items():
        # Prefer the first definition that is NOT a TimeFrameExpand wrapper
        chosen = exprs[0]
        for e in exprs:
            if not re.match(r'TimeFrameExpand\s*\(', e, re.IGNORECASE):
                chosen = e
                break
        defs[var_name] = chosen

    return defs


def _extract_param_var_map(afl_stripped: str) -> dict[str, str]:
    """Map AFL variable names to their Param/Optimize display names.

    For ``adxThreshold = Param("ADX Threshold", 25, ...)``,
    returns ``{"adxThreshold": "ADX Threshold"}``.
    """
    result: dict[str, str] = {}
    pattern = re.compile(
        r'(\w+)\s*=\s*(?:Param|Optimize)\s*\(\s*"([^"]+)"',
        re.IGNORECASE,
    )
    for m in pattern.finditer(afl_stripped):
        result[m.group(1)] = m.group(2)
    return result


def _resolve_signal_expr(afl_stripped: str, signal: str,
                         var_defs: dict[str, str]) -> Optional[str]:
    """Find the expression assigned to Buy or Short (before ExRem).

    Resolves indirection through variable assignments and
    ``TimeFrameExpand()`` wrappers so the actual signal conditions
    are returned.
    """
    # Match the first assignment that isn't ExRem or literal 0
    pattern = re.compile(
        rf'\b{signal}\s*=\s*(?!ExRem\b|0\s*;)(.+?)\s*;',
        re.IGNORECASE | re.DOTALL,
    )
    m = pattern.search(afl_stripped)
    if m is None:
        return None

    expr = m.group(1).strip()
    expr = re.sub(r'\s+', ' ', expr)

    # Unwrap TimeFrameExpand(varName, ...) -> resolve varName
    tfe_m = re.match(r'TimeFrameExpand\s*\(\s*(\w+)\s*,', expr, re.IGNORECASE)
    if tfe_m:
        inner_var = tfe_m.group(1)
        resolved = var_defs.get(inner_var)
        if resolved:
            return re.sub(r'\s+', ' ', resolved)
        # Fall through to plain variable resolution

    # If expr is just a variable name, resolve it
    if re.fullmatch(r'\w+', expr):
        resolved = var_defs.get(expr)
        if resolved:
            return re.sub(r'\s+', ' ', resolved)

    return expr


def _split_and(expr: str) -> list[str]:
    """Split an expression on ``AND`` (case-insensitive)."""
    parts = re.split(r'\s+AND\s+', expr, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]
