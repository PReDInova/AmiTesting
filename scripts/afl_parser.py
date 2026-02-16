"""
AFL indicator parser -- Sprint 4.

Extracts indicator function calls (MA, EMA, BBandTop/BBandBot, etc.) from
AmiBroker Formula Language source code so the dashboard can auto-configure
chart overlays to match the strategy that produced a backtest run.
"""

import math
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AFL function pattern -> indicator config mapping
# ---------------------------------------------------------------------------

_AFL_INDICATOR_PATTERNS = [
    # MA(array, period) -> SMA
    {
        "pattern": re.compile(r"\bMA\s*\(\s*\w+\s*,\s*(\d+)\s*\)", re.IGNORECASE),
        "type": "sma",
        "param_extractor": lambda m: {"period": int(m.group(1))},
    },
    # EMA(array, period) -> EMA
    {
        "pattern": re.compile(r"\bEMA\s*\(\s*\w+\s*,\s*(\d+)\s*\)", re.IGNORECASE),
        "type": "ema",
        "param_extractor": lambda m: {"period": int(m.group(1))},
    },
    # BBandTop(array, period, width)  -> Bollinger Bands
    {
        "pattern": re.compile(
            r"\bBBandTop\s*\(\s*\w+\s*,\s*(\d+)\s*,\s*([0-9.]+)\s*\)",
            re.IGNORECASE,
        ),
        "type": "bbands",
        "param_extractor": lambda m: {"period": int(m.group(1)),
                                       "std_dev": float(m.group(2))},
    },
    # BBandBot(array, period, width) -> same bbands type (will be deduped)
    {
        "pattern": re.compile(
            r"\bBBandBot\s*\(\s*\w+\s*,\s*(\d+)\s*,\s*([0-9.]+)\s*\)",
            re.IGNORECASE,
        ),
        "type": "bbands",
        "param_extractor": lambda m: {"period": int(m.group(1)),
                                       "std_dev": float(m.group(2))},
    },
]


def _strip_comments(afl: str) -> str:
    """Remove AFL single-line and block comments."""
    result = re.sub(r"/\*.*?\*/", "", afl, flags=re.DOTALL)
    result = re.sub(r"//[^\n]*", "", result)
    return result


def parse_afl_indicators(afl_content: str) -> list[dict]:
    """Parse AFL source and extract indicator configurations.

    Returns a deduplicated list of ``{"type": str, "params": dict}`` dicts.
    """
    stripped = _strip_comments(afl_content)
    seen: set[tuple] = set()
    indicators: list[dict] = []

    for spec in _AFL_INDICATOR_PATTERNS:
        for match in spec["pattern"].finditer(stripped):
            params = spec["param_extractor"](match)
            key = (spec["type"], tuple(sorted(params.items())))
            if key not in seen:
                seen.add(key)
                indicators.append({"type": spec["type"], "params": params})

    logger.info("Parsed %d indicators from AFL content.", len(indicators))
    return indicators


# ---------------------------------------------------------------------------
# #include -> indicator type mapping
# ---------------------------------------------------------------------------

_INCLUDE_TO_INDICATOR = {
    "tema.afl": "tema",
    "vwap_clouds.afl": "vwap",
    "stdev_exit.afl": "stdev_bands",
    "adx.afl": "adx",
    "consolidation_zones.afl": "donchian",
    "range_bound.afl": "donchian",
    "derivative_lookback.afl": "derivative",
}

# Files to skip (not chart indicators or too complex)
_INCLUDE_SKIP = {
    "market_sessions.afl",
    "consolidation_normwidth.afl",
}

# Inline AFL function patterns -> indicator type
_INLINE_PATTERNS = [
    (re.compile(r"\bBBandTop\s*\(", re.IGNORECASE), "bbands"),
    (re.compile(r"\bBBandBot\s*\(", re.IGNORECASE), "bbands"),
    (re.compile(r"\bRSI\s*\(", re.IGNORECASE), "rsi"),
    (re.compile(r"\bStochK\s*\(", re.IGNORECASE), "stochastic"),
    (re.compile(r"\bStochD\s*\(", re.IGNORECASE), "stochastic"),
    (re.compile(r"\bEMA\s*\(", re.IGNORECASE), "ema"),
    (re.compile(r"\bMA\s*\(", re.IGNORECASE), "sma"),
    (re.compile(r"\bATR\s*\(", re.IGNORECASE), "atr"),
]

# Indicator display properties
_INDICATOR_OVERLAY = {
    "tema": True, "vwap": True, "bbands": True, "donchian": True,
    "sma": True, "ema": True, "stdev_bands": True,
    "adx": False, "rsi": False, "stochastic": False, "atr": False,
    "derivative": False,
}

_INDICATOR_COLOR = {
    "tema": "#FF6D00",
    "adx": "#2979FF",
    "vwap": "#7C4DFF",
    "stdev_bands": "#78909C",
    "rsi": "#00BFA5",
    "stochastic": "#F50057",
    "donchian": "#00C853",
    "bbands": "#2979FF",
    "sma": "#FF6D00",
    "ema": "#00E676",
    "atr": "#FF9100",
    "derivative": "#FF5722",
}

# Param name keyword rules -> (indicator_type, param_key)
# Each rule: (name_keywords, indicator_type, param_key)
# name_keywords is a list of keyword groups -- all groups must match,
# where each group is a tuple of alternatives (any one matches).
_PARAM_RULES = [
    # TEMA
    ([("tema",), ("length",)], "tema", "period"),
    ([("smoothing",), ("length",)], "tema", "period"),
    # ADX
    ([("adx",), ("period",)], "adx", "period"),
    # Stochastic -- order matters: check %d before %k since %k rule uses "period" fallback
    ([("stoch",), ("%d",)], "stochastic", "d_period"),
    ([("stoch",), ("smooth",)], "stochastic", "smooth"),
    ([("stoch",), ("%k", "period")], "stochastic", "k_period"),
    # Bollinger Bands
    ([("bb",), ("period",)], "bbands", "period"),
    ([("bb",), ("std",)], "bbands", "std_dev"),
    # RSI
    ([("rsi",), ("period",)], "rsi", "period"),
    # Donchian
    ([("donchian",), ("period",)], "donchian", "period"),
    # StdDev Bands
    ([("stddev", "stdev"), ("lookback", "bars")], "stdev_bands", "lookback"),
    ([("stddev", "stdev"), ("mult",)], "stdev_bands", "multiplier"),
    # ATR
    ([("atr",), ("period",)], "atr", "period"),
    # Derivative
    ([("deriv",), ("lookback",)], "derivative", "lookback"),
]


def _match_param_rule(param_name: str) -> tuple[str, str] | None:
    """Match a Param display name against _PARAM_RULES.

    Returns ``(indicator_type, param_key)`` or ``None``.
    """
    name_lower = param_name.lower()
    for keyword_groups, ind_type, param_key in _PARAM_RULES:
        if all(
            any(kw in name_lower for kw in group)
            for group in keyword_groups
        ):
            return ind_type, param_key
    return None


def extract_strategy_indicators(afl_content: str) -> list[dict]:
    """Analyze a strategy's AFL code and return indicator configurations.

    Combines three sources of information:
    1. ``#include_once`` directives that reference indicator AFL files
    2. Inline AFL function calls (``MA(``, ``EMA(``, ``BBandTop(`` etc.)
    3. ``Param()`` / ``Optimize()`` calls matched by keyword rules

    Returns a list of indicator config dicts suitable for
    :func:`scripts.indicators.compute_indicators`::

        [{"type": str, "params": dict, "param_mapping": dict,
          "overlay": bool, "color": str}, ...]
    """
    stripped = _strip_comments(afl_content)

    # Collect detected indicator types (set for dedup)
    detected_types: set[str] = set()

    # --- 1. Parse #include_once directives ---
    include_re = re.compile(r'#include_once\s+"[^"]*?([^/\\]+\.afl)"', re.IGNORECASE)
    for m in include_re.finditer(stripped):
        filename = m.group(1).lower()
        if filename in _INCLUDE_SKIP:
            continue
        ind_type = _INCLUDE_TO_INDICATOR.get(filename)
        if ind_type:
            detected_types.add(ind_type)

    # --- 2. Detect inline indicator patterns ---
    for pattern, ind_type in _INLINE_PATTERNS:
        if pattern.search(stripped):
            detected_types.add(ind_type)

    # Special: HHV + LLV together -> donchian
    if (re.search(r"\bHHV\s*\(", stripped, re.IGNORECASE)
            and re.search(r"\bLLV\s*\(", stripped, re.IGNORECASE)):
        detected_types.add("donchian")

    # Special: Wilders + plusDI/plusDM -> adx (inline ADX calc)
    if (re.search(r"\bWilders?\b", stripped, re.IGNORECASE)
            and re.search(r"\bplusDI\b|\bplusDM\b", stripped, re.IGNORECASE)):
        detected_types.add("adx")

    # --- 3. Map Param() names to indicator parameters ---
    params = parse_afl_params(afl_content)

    # Build param mappings per indicator type:
    # ind_type -> {"param_key": value, ...} and {"param_key": "AFL Param Name"}
    ind_params: dict[str, dict] = {}
    ind_param_mapping: dict[str, dict] = {}

    for p in params:
        result = _match_param_rule(p["name"])
        if result is None:
            continue
        ind_type, param_key = result
        # A matched param also implies the indicator is present
        detected_types.add(ind_type)
        ind_params.setdefault(ind_type, {})[param_key] = p["default"]
        ind_param_mapping.setdefault(ind_type, {})[param_key] = p["name"]

    # --- 4. Build output list ---
    indicators: list[dict] = []
    for ind_type in sorted(detected_types):
        indicators.append({
            "type": ind_type,
            "params": ind_params.get(ind_type, {}),
            "param_mapping": ind_param_mapping.get(ind_type, {}),
            "overlay": _INDICATOR_OVERLAY.get(ind_type, True),
            "color": _INDICATOR_COLOR.get(ind_type, "#FFFFFF"),
        })

    logger.info(
        "Extracted %d strategy indicators from AFL content.", len(indicators)
    )
    return indicators


# AmiBroker TimeFrameSet() constant -> seconds
_TIMEFRAME_MAP = {
    "in1Minute": 60,
    "in5Minute": 300,
    "in10Minute": 600,
    "in15Minute": 900,
    "in1Hour": 3600,
    "inDaily": 86400,
}


def parse_afl_timeframe(afl_content: str) -> Optional[int]:
    """Extract the base timeframe from ``TimeFrameSet()`` if present.

    Returns the interval in seconds, or ``None`` if no timeframe is set.
    """
    stripped = _strip_comments(afl_content)
    match = re.search(r"\bTimeFrameSet\s*\(\s*(\w+)\s*\)", stripped)
    if match:
        return _TIMEFRAME_MAP.get(match.group(1))
    return None


# ---------------------------------------------------------------------------
# Param / Optimize parsing and modification
# ---------------------------------------------------------------------------

# Matches both Param("name", default, min, max, step) and
# Optimize("name", default, min, max, step).
_PARAM_RE = re.compile(
    r'\b(Param|Optimize)\s*\(\s*"([^"]+)"\s*,'
    r"\s*([0-9.eE+-]+)\s*,"   # default
    r"\s*([0-9.eE+-]+)\s*,"   # min
    r"\s*([0-9.eE+-]+)\s*,"   # max
    r"\s*([0-9.eE+-]+)\s*\)",  # step
    re.IGNORECASE,
)


def parse_afl_params(afl_content: str) -> list[dict]:
    """Extract ``Param()`` and ``Optimize()`` calls from AFL source.

    Comments are stripped before scanning so commented-out calls are ignored.
    Returns a list ordered by appearance in source::

        [{"name": str, "default": float, "min": float,
          "max": float, "step": float, "type": "param"|"optimize"}, ...]
    """
    stripped = _strip_comments(afl_content)
    results: list[dict] = []
    for m in _PARAM_RE.finditer(stripped):
        func_name = m.group(1)  # "Param" or "Optimize"
        results.append(
            {
                "name": m.group(2),
                "default": float(m.group(3)),
                "min": float(m.group(4)),
                "max": float(m.group(5)),
                "step": float(m.group(6)),
                "type": "optimize" if func_name.lower() == "optimize" else "param",
            }
        )
    logger.info("Parsed %d Param/Optimize calls from AFL content.", len(results))
    return results


def _fmt_num(value: float) -> str:
    """Format a numeric value: integers without decimal, floats as-is."""
    return str(int(value)) if value == int(value) else str(value)


def modify_afl_params(
    afl_content: str,
    overrides: dict | None = None,
    optimize_names: set | None = None,
    min_overrides: dict | None = None,
    max_overrides: dict | None = None,
    step_overrides: dict | None = None,
) -> str:
    """Return *afl_content* with targeted Param/Optimize modifications.

    * ``overrides``  -- ``{"param_name": new_default}`` replaces the default
      value inside the matching ``Param()`` or ``Optimize()`` call.
    * ``optimize_names`` -- set of param names that should use ``Optimize()``
      instead of ``Param()``.  Names **not** in the set stay as-is.
    * ``min_overrides`` -- ``{"param_name": new_min}`` replaces the min value.
    * ``max_overrides`` -- ``{"param_name": new_max}`` replaces the max value.
    * ``step_overrides`` -- ``{"param_name": new_step}`` replaces the step value.

    The original source (including comments, whitespace, and formatting) is
    preserved except for the specific tokens that are being replaced.
    """
    overrides = overrides or {}
    optimize_names = optimize_names or set()
    min_overrides = min_overrides or {}
    max_overrides = max_overrides or {}
    step_overrides = step_overrides or {}

    def _replacer(m: re.Match) -> str:
        func = m.group(1)          # Param or Optimize
        name = m.group(2)          # parameter name
        default = m.group(3)       # default value (string)
        min_val = m.group(4)
        max_val = m.group(5)
        step_val = m.group(6)

        # Apply default override
        if name in overrides:
            default = _fmt_num(overrides[name])

        # Apply min/max/step overrides
        if name in min_overrides:
            min_val = _fmt_num(min_overrides[name])
        if name in max_overrides:
            max_val = _fmt_num(max_overrides[name])
        if name in step_overrides:
            step_val = _fmt_num(step_overrides[name])

        # Apply Param -> Optimize conversion
        if name in optimize_names:
            func = "Optimize"

        return f'{func}("{name}", {default}, {min_val}, {max_val}, {step_val})'

    return _PARAM_RE.sub(_replacer, afl_content)


# ---------------------------------------------------------------------------
# Code Map: description ↔ AFL code alignment
# ---------------------------------------------------------------------------

_CODE_MAP_CATEGORIES = [
    {
        "id": "entry_long",
        "label": "Entry Long",
        "color": "#c8e6c9",
        "border": "#4caf50",
    },
    {
        "id": "entry_short",
        "label": "Entry Short",
        "color": "#ffcdd2",
        "border": "#ef5350",
    },
    {
        "id": "exit",
        "label": "Exit / Stops",
        "color": "#bbdefb",
        "border": "#2196f3",
    },
    {
        "id": "session",
        "label": "Session Filter",
        "color": "#fff9c4",
        "border": "#ffc107",
    },
    {
        "id": "indicators",
        "label": "Indicators",
        "color": "#e1bee7",
        "border": "#9c27b0",
    },
    {
        "id": "params",
        "label": "Parameters",
        "color": "#b2ebf2",
        "border": "#00bcd4",
    },
]


def build_code_map(description: str, afl_content: str) -> list[dict]:
    """Build a color-coded mapping between strategy description sections
    and AFL code lines.

    Returns a list of mapping entries, each with:

    * ``id``, ``label``, ``color``, ``border`` -- category metadata
    * ``desc_ranges`` -- list of ``{"start": int, "end": int}`` character
      positions in *description*
    * ``code_lines`` -- list of 1-indexed line numbers in *afl_content*
    """
    if not description or not afl_content:
        return []

    afl_lines = afl_content.splitlines()
    result = []

    for cat in _CODE_MAP_CATEGORIES:
        desc_ranges = _cm_desc_ranges(cat["id"], description)
        code_lines = _cm_code_lines(cat["id"], afl_lines)

        if desc_ranges or code_lines:
            result.append({
                "id": cat["id"],
                "label": cat["label"],
                "color": cat["color"],
                "border": cat["border"],
                "desc_ranges": desc_ranges,
                "code_lines": code_lines,
            })

    return result


def _cm_desc_ranges(cat_id: str, description: str) -> list[dict]:
    """Find character ranges in *description* belonging to *cat_id*."""
    if cat_id == "entry_long":
        return _cm_find_section(description, r"Entry\s+Long\s*:")
    if cat_id == "entry_short":
        return _cm_find_section(description, r"Entry\s+Short\s*:")
    if cat_id == "exit":
        return _cm_find_section(description, r"Exit\s*:")
    if cat_id == "session":
        ranges = []
        for m in re.finditer(
            r"(?:Asian|London|US|European)\s+[Ss]ession\s*(?:\([^)]*\))?",
            description,
        ):
            ranges.append({"start": m.start(), "end": m.end()})
        return ranges
    # "params" and "indicators" only highlight code -- no description ranges
    return []


_SECTION_LABELS = r"Entry\s+(?:Long|Short)\s*:|Exit\s*:|Designed\s+for"


def _cm_find_section(text: str, label_pattern: str) -> list[dict]:
    """Find a labeled section (e.g. ``Entry Long: ...``) spanning until the
    next section label or paragraph break."""
    ranges = []
    for m in re.finditer(label_pattern, text, re.IGNORECASE):
        start = m.start()
        rest = text[m.end():]
        end_match = re.search(
            r"\n\s*\n|\n\s*(?:" + _SECTION_LABELS + ")",
            rest,
            re.IGNORECASE,
        )
        end = m.end() + end_match.start() if end_match else len(text)
        # Trim trailing whitespace
        while end > start and text[end - 1] in " \n\r\t":
            end -= 1
        if end > start:
            ranges.append({"start": start, "end": end})
    return ranges


# ---------------------------------------------------------------------------
# Optimization progress tracking
# ---------------------------------------------------------------------------

_OPTIMIZE_RE = re.compile(
    r'\bOptimize\s*\(\s*"[^"]*"\s*,'
    r"\s*[0-9.eE+-]+\s*,"   # default
    r"\s*([0-9.eE+-]+)\s*,"  # min
    r"\s*([0-9.eE+-]+)\s*,"  # max
    r"\s*([0-9.eE+-]+)\s*\)",  # step
    re.IGNORECASE,
)

_PROGRESS_AFL_TEMPLATE = r"""// ==== AmiTesting Optimization Progress Tracker ====
// Status("action"): 1=backtest, 2=scan, 3=explore, 4=optimize
if (Status("action") == 4)
{{
    if (BarIndex() == BarCount - 1)
    {{
        _optCombo = Nz(StaticVarGet("__ami_opt_combo"));
        _optCombo = _optCombo + 1;
        StaticVarSet("__ami_opt_combo", _optCombo);
        _pf = fopen("{progress_file}", "w");
        if (_pf)
        {{
            fputs(NumToStr(_optCombo, 1.0, False), _pf);
            fclose(_pf);
        }}
    }}
}}
// ==== End Progress Tracker ====
"""


def calculate_optimization_combos(afl_content: str) -> int:
    """Compute the total number of optimization combos from Optimize() calls.

    For each ``Optimize("name", default, min, max, step)`` call, the number
    of values is ``floor((max - min) / step) + 1``.  The total combos is the
    product of all individual counts.

    Returns 0 if no ``Optimize()`` calls are found.
    """
    stripped = _strip_comments(afl_content)
    counts: list[int] = []
    for m in _OPTIMIZE_RE.finditer(stripped):
        mn = float(m.group(1))
        mx = float(m.group(2))
        step = float(m.group(3))
        if step <= 0:
            continue
        n = math.floor((mx - mn) / step) + 1
        if n > 0:
            counts.append(n)

    if not counts:
        return 0

    total = 1
    for c in counts:
        total *= c
    return total


def inject_progress_tracker(afl_content: str, progress_file_path: str) -> str:
    """Prepend the optimization progress tracker AFL block to *afl_content*.

    The injected code writes the current combo counter to *progress_file_path*
    on every optimization pass (last bar only).  It is inert during normal
    backtests because of the ``Status("action") == actionOptimize`` guard.

    Back-slashes in the file path are doubled for AFL string escaping.
    """
    # AFL strings use backslash escaping, so double them for Windows paths
    escaped_path = progress_file_path.replace("\\", "\\\\")
    tracker = _PROGRESS_AFL_TEMPLATE.format(progress_file=escaped_path)
    return tracker + "\n" + afl_content


def _cm_code_lines(cat_id: str, afl_lines: list[str]) -> list[int]:
    """Find 1-indexed line numbers in AFL code for *cat_id*."""
    lines = []
    for i, line in enumerate(afl_lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue

        matched = False
        if cat_id == "entry_long":
            # Match the signal definition but not TimeFrameExpand re-assignments
            matched = bool(
                re.search(r"\bbuySignal\s*=", stripped)
                and not re.search(r"TimeFrameExpand", stripped)
            )
        elif cat_id == "entry_short":
            matched = bool(
                re.search(r"\bshortSignal\s*=", stripped)
                and not re.search(r"TimeFrameExpand", stripped)
            )
        elif cat_id == "exit":
            # Strip inline comments before matching to avoid false positives
            # like `Sell = 0;  // exits handled by ApplyStop`
            code_part = stripped.split("//")[0]
            matched = bool(
                re.search(r"\bApplyStop\b|\bSetPositionSize\b", code_part)
            )
        elif cat_id == "session":
            # Only match lines that *define* the session filter, not lines
            # that merely reference the variable in a larger expression.
            matched = bool(
                re.search(
                    r"\basianSession\s*=|\blondonSession\s*=|\busSession\s*="
                    r"|\bTimeNum\s*\(\)",
                    stripped,
                )
            )
        elif cat_id == "indicators":
            matched = bool(re.search(r"#include_once", stripped))
        elif cat_id == "params":
            matched = bool(re.search(r"\bParam\s*\(|\bOptimize\s*\(", stripped))

        if matched:
            lines.append(i + 1)
    return lines
