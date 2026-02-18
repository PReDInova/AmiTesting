"""
Parameter optimization advisor for AFL strategies.

Analyzes Param() definitions in strategy AFL code and suggests
optimization ranges, priorities, and configurations based on
the parameter type (period, threshold, multiplier, etc.).
"""

import logging
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.strategy_db import get_strategy, get_latest_version
from scripts.afl_parser import parse_afl_params

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parameter classification rules
# ---------------------------------------------------------------------------

# Each rule: (name_keywords, category, suggested_range, suggested_step, rationale, priority)
_PARAM_RULES = [
    # Period / Length params (moving averages, lookbacks)
    {
        "keywords": ["length", "period", "lookback", "bars"],
        "category": "period",
        "default_range": (5, 100),
        "default_step": 5,
        "rationale": "Period parameters control indicator responsiveness. Shorter periods react faster but produce more noise; longer periods are smoother but lag.",
        "priority": "high",
    },
    # Threshold params (ADX, RSI levels)
    {
        "keywords": ["threshold"],
        "category": "threshold",
        "default_range": (10, 40),
        "default_step": 5,
        "rationale": "Threshold values control trade entry sensitivity. Lower thresholds generate more signals; higher thresholds are more selective.",
        "priority": "high",
    },
    # Rise/momentum lookback
    {
        "keywords": ["rise"],
        "category": "lookback",
        "default_range": (2, 10),
        "default_step": 1,
        "rationale": "Rise lookback controls how many bars to check for directional momentum confirmation.",
        "priority": "medium",
    },
    # Multiplier params (StdDev, profit target, ATR)
    {
        "keywords": ["mult", "multiplier"],
        "category": "multiplier",
        "default_range": (0.5, 3.0),
        "default_step": 0.25,
        "rationale": "Multipliers scale volatility-based stops and targets. Higher values give trades more room but increase risk per trade.",
        "priority": "medium",
    },
    # ATR-specific params
    {
        "keywords": ["atr"],
        "category": "period",
        "default_range": (7, 28),
        "default_step": 7,
        "rationale": "ATR period controls the volatility measurement window. Common values: 7 (1 week), 14 (2 weeks), 21 (1 month) for daily bars.",
        "priority": "low",
    },
    # Donchian / channel params
    {
        "keywords": ["donchian", "channel"],
        "category": "period",
        "default_range": (15, 60),
        "default_step": 5,
        "rationale": "Channel period defines the lookback window for breakout boundaries. Wider channels produce fewer but higher-conviction signals.",
        "priority": "high",
    },
    # StdDev specific
    {
        "keywords": ["stdev", "stddev"],
        "category": "period",
        "default_range": (10, 60),
        "default_step": 5,
        "rationale": "StdDev lookback controls how many bars are used for volatility estimation. Shorter windows adapt faster; longer windows are more stable.",
        "priority": "low",
    },
]


def _classify_param(param: dict) -> dict:
    """Classify a parameter and generate optimization suggestion."""
    name_lower = param["name"].lower()

    # Try to match against rules
    for rule in _PARAM_RULES:
        if any(kw in name_lower for kw in rule["keywords"]):
            # Use the rule's defaults, but respect the param's existing range
            current_min = param["min"]
            current_max = param["max"]
            current_step = param["step"]

            suggested_min = max(rule["default_range"][0], current_min)
            suggested_max = min(rule["default_range"][1], current_max)

            # If the suggested range is too narrow, widen it
            if suggested_max <= suggested_min:
                suggested_min = rule["default_range"][0]
                suggested_max = rule["default_range"][1]

            # Suggest a coarser step for optimization (fewer combinations)
            suggested_step = rule["default_step"]
            if suggested_step < current_step:
                suggested_step = current_step

            return {
                "param_name": param["name"],
                "current_default": param["default"],
                "current_range": [current_min, current_max],
                "current_step": current_step,
                "suggested_range": [suggested_min, suggested_max],
                "suggested_step": suggested_step,
                "rationale": rule["rationale"],
                "priority": rule["priority"],
                "category": rule["category"],
            }

    # Default classification for unrecognized params
    return {
        "param_name": param["name"],
        "current_default": param["default"],
        "current_range": [param["min"], param["max"]],
        "current_step": param["step"],
        "suggested_range": [param["min"], param["max"]],
        "suggested_step": param["step"],
        "rationale": "No specific optimization guidance available for this parameter.",
        "priority": "low",
        "category": "other",
    }


def _estimate_combinations(suggestions: list, optimize_all: bool = False) -> dict:
    """Calculate estimated optimization combinations and runtime."""
    # Only count high + medium priority params by default
    params_to_opt = []
    for s in suggestions:
        if optimize_all or s["priority"] in ("high", "medium"):
            params_to_opt.append(s)

    if not params_to_opt:
        return {
            "params_to_optimize": [],
            "total_combinations": 0,
            "estimated_time_seconds": 0,
        }

    total_combos = 1
    for p in params_to_opt:
        range_size = p["suggested_range"][1] - p["suggested_range"][0]
        steps = max(1, int(range_size / p["suggested_step"]) + 1)
        total_combos *= steps

    # Rough estimate: ~2.5 seconds per combination for AmiBroker optimization
    est_seconds = total_combos * 2.5

    return {
        "params_to_optimize": [p["param_name"] for p in params_to_opt],
        "total_combinations": total_combos,
        "estimated_time_seconds": round(est_seconds),
    }


def analyze_strategy_params(strategy_id: str) -> dict:
    """Analyze a strategy's parameters and return optimization suggestions.

    Parameters
    ----------
    strategy_id : str
        UUID of the strategy to analyze.

    Returns
    -------
    dict
        Analysis results including parameter suggestions and optimization config.
    """
    strategy = get_strategy(strategy_id)
    if strategy is None:
        return {"error": f"Strategy not found: {strategy_id}"}

    version = get_latest_version(strategy_id)
    if version is None:
        return {"error": f"No versions found for strategy: {strategy_id}"}

    afl_content = version.get("afl_content", "")
    if not afl_content:
        return {"error": "No AFL content in latest version"}

    params = parse_afl_params(afl_content)
    if not params:
        return {
            "strategy_id": strategy_id,
            "strategy_name": strategy["name"],
            "params": [],
            "suggestions": [],
            "optimization_config": {
                "params_to_optimize": [],
                "total_combinations": 0,
                "estimated_time_seconds": 0,
            },
        }

    suggestions = [_classify_param(p) for p in params]

    # Sort by priority: high first, then medium, then low
    priority_order = {"high": 0, "medium": 1, "low": 2}
    suggestions.sort(key=lambda s: priority_order.get(s["priority"], 3))

    opt_config = _estimate_combinations(suggestions)

    return {
        "strategy_id": strategy_id,
        "strategy_name": strategy["name"],
        "version_id": version["id"],
        "version_number": version["version_number"],
        "params": params,
        "suggestions": suggestions,
        "optimization_config": opt_config,
    }


def get_all_strategy_analysis() -> list[dict]:
    """Analyze parameters for ALL strategies in the database.

    Returns a list of analysis dicts, one per strategy.
    Strategies with no parameters are included with empty suggestions.
    """
    from scripts.strategy_db import list_strategies

    results = []
    for strategy in list_strategies():
        analysis = analyze_strategy_params(strategy["id"])
        if "error" not in analysis:
            results.append(analysis)

    return results
