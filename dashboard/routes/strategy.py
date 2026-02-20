"""
Strategy blueprint -- CRUD, versions, optimization, exploration, signals,
bar analysis, builder, refine, diff, status, compare.
"""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

from flask import (
    Blueprint, flash, jsonify, redirect, render_template, request, url_for,
)

from config.settings import (
    AMIBROKER_DB_PATH, DEFAULT_SYMBOL, CHART_SETTINGS, PROJECT_ROOT,
)
from dashboard.state import _backtest_state, _backtest_lock
from dashboard.helpers import validate_afl_content, VALID_INTERVALS
from dashboard.routes.backtest import _run_backtest_background
from scripts.strategy_db import (
    get_strategy as db_get_strategy,
    list_strategies as db_list_strategies,
    create_strategy as db_create_strategy,
    get_strategy_summary,
    list_runs as db_list_runs,
    list_versions as db_list_versions,
    get_version as db_get_version,
    get_latest_version as db_get_latest_version,
    get_run_with_context,
    create_version as db_create_version,
    find_strategy_by_name as db_find_strategy_by_name,
)
from scripts.afl_reverser import reverse_afl

logger = logging.getLogger(__name__)

strategy_bp = Blueprint("strategy_bp", __name__)

# In-memory cache of OHLCV bars for the explorer
_explorer_bars_cache: dict = {}


# ---------------------------------------------------------------------------
# Strategy detail & CRUD
# ---------------------------------------------------------------------------


@strategy_bp.route("/strategy/<strategy_id>")
def strategy_detail(strategy_id: str):
    """Strategy detail page showing versions and runs."""
    strategy = db_get_strategy(strategy_id)
    if strategy is None:
        flash(f"Strategy '{strategy_id}' not found.", "danger")
        return redirect(url_for("backtest_bp.index"))

    versions = db_list_versions(strategy_id)
    runs = db_list_runs(strategy_id=strategy_id)

    try:
        from scripts.afl_parser import parse_afl_params
    except ImportError:
        parse_afl_params = lambda _: []

    params = []
    if versions:
        params = parse_afl_params(versions[0].get("afl_content", ""))

    version_map = {v["id"]: v for v in versions}
    for run in runs:
        run["version"] = version_map.get(run["version_id"])
        run_afl = run.get("afl_content", "")
        if run_afl:
            run["params"] = parse_afl_params(run_afl)
        else:
            run["params"] = []

    return render_template(
        "strategy_detail.html",
        strategy=strategy,
        versions=versions,
        runs=runs,
        params=params,
        db_configured=bool(AMIBROKER_DB_PATH),
        default_symbol=DEFAULT_SYMBOL,
    )


@strategy_bp.route("/strategy/create", methods=["POST"])
def strategy_create():
    """Create a new strategy."""
    name = request.form.get("name", "").strip()
    summary = request.form.get("summary", "").strip()
    symbol = request.form.get("symbol", "").strip()

    if not name:
        flash("Strategy name is required.", "danger")
        return redirect(url_for("backtest_bp.index"))

    strategy_id = db_create_strategy(name=name, summary=summary, symbol=symbol)
    flash(f"Strategy '{name}' created.", "success")
    return redirect(url_for("strategy_bp.strategy_detail", strategy_id=strategy_id))


@strategy_bp.route("/strategy/reverse", methods=["POST"])
def strategy_reverse():
    """Create a reversed copy of a strategy and run backtest."""
    run_id = request.form.get("run_id", "").strip()
    if not run_id:
        flash("No run specified for reversal.", "danger")
        return redirect(url_for("backtest_bp.index"))

    run = get_run_with_context(run_id)
    if run is None:
        flash(f"Run '{run_id}' not found.", "danger")
        return redirect(url_for("backtest_bp.index"))

    version = run.get("version")
    afl_content = run.get("afl_content") or (version.get("afl_content", "") if version else "")
    if not afl_content.strip():
        flash("No AFL content available to reverse.", "danger")
        return redirect(url_for("backtest_bp.run_detail", run_id=run_id))

    original_strategy = run.get("strategy") or {}
    original_name = original_strategy.get("name", "Unknown")
    reversed_name = f"{original_name}_reverse"

    reversed_afl = reverse_afl(afl_content)

    existing = db_find_strategy_by_name(reversed_name)
    if existing:
        strategy_id = existing["id"]
    else:
        strategy_id = db_create_strategy(
            name=reversed_name,
            summary=f"Reversed signals from: {original_name}",
            description=f"Auto-generated reversed strategy. Buy/Short and Sell/Cover signals swapped from {original_name}.",
            symbol=original_strategy.get("symbol", ""),
        )

    version_id = db_create_version(
        strategy_id=strategy_id,
        afl_content=reversed_afl,
        label="Reversed signals",
    )

    with _backtest_lock:
        if _backtest_state["running"]:
            flash(f"Reversed strategy '{reversed_name}' created but a backtest is already running.", "warning")
            return redirect(url_for("strategy_bp.strategy_detail", strategy_id=strategy_id))

    symbol = run.get("symbol") or None
    thread = threading.Thread(
        target=_run_backtest_background,
        args=(strategy_id, version_id, None, symbol),
        daemon=True,
    )
    thread.start()
    flash(f"Reversed strategy '{reversed_name}' created. Backtest started.", "info")
    return redirect(url_for("backtest_bp.backtest_status_page"))


# ---------------------------------------------------------------------------
# Strategy Builder
# ---------------------------------------------------------------------------


@strategy_bp.route("/strategy-builder")
def strategy_builder():
    """Strategy Builder page."""
    strategies = db_list_strategies()
    enriched = []
    for s in strategies:
        runs = db_list_runs(strategy_id=s["id"])
        s["status"] = "tested" if runs else "draft"
        s["source"] = "manual"
        enriched.append(s)
    return render_template("strategy_builder.html", strategies=enriched)


@strategy_bp.route("/strategy-builder/create", methods=["POST"])
def strategy_builder_create():
    """Create a new strategy with AFL code from the Strategy Builder form."""
    name = request.form.get("strategy_name", "").strip()
    description = request.form.get("description", "").strip()
    afl_content = request.form.get("afl_content", "").strip()

    if not name:
        flash("Strategy name is required.", "danger")
        return redirect(url_for("strategy_bp.strategy_builder"))

    if not afl_content:
        flash("AFL code cannot be empty.", "danger")
        return redirect(url_for("strategy_bp.strategy_builder"))

    afl_warnings = validate_afl_content(afl_content)
    for w in afl_warnings:
        flash(f"AFL warning: {w}", "warning")

    strategy_id = db_create_strategy(name=name, summary=description)
    db_create_version(
        strategy_id=strategy_id,
        afl_content=afl_content,
        label="v1",
    )

    flash(f"Strategy '{name}' created with initial version.", "success")
    return redirect(url_for("strategy_bp.strategy_detail", strategy_id=strategy_id))


@strategy_bp.route("/api/strategy-builder/generate", methods=["POST"])
def api_generate_afl():
    """API endpoint: Generate AFL code using Claude Code SDK."""
    data = request.get_json(silent=True) or {}

    strategy_name = data.get("strategy_name", "").strip()
    description = data.get("description", "").strip()
    symbol = data.get("symbol", "").strip()

    if not strategy_name and not description:
        return jsonify({"error": "Please provide a strategy name or description."}), 400

    from scripts.afl_generator import generate_afl

    result = generate_afl(
        strategy_name=strategy_name or "Untitled Strategy",
        description=description,
        symbol=symbol,
    )

    if result["error"]:
        return jsonify({"error": result["error"]}), 500

    return jsonify({
        "afl_code": result["afl_code"],
        "warnings": result["warnings"],
        "cost_usd": result["cost_usd"],
    })


# ---------------------------------------------------------------------------
# Versions & run-with-params
# ---------------------------------------------------------------------------


@strategy_bp.route("/strategy/<strategy_id>/version/create", methods=["POST"])
def version_create(strategy_id: str):
    """Create a new version for a strategy with AFL content."""
    afl_content = request.form.get("afl_content", "")
    label = request.form.get("label", "").strip()

    if not afl_content.strip():
        flash("AFL content cannot be empty.", "danger")
        return redirect(url_for("strategy_bp.strategy_detail", strategy_id=strategy_id))

    afl_warnings = validate_afl_content(afl_content)
    for warning in afl_warnings:
        flash(f"AFL warning: {warning}", "warning")

    version_id = db_create_version(
        strategy_id=strategy_id,
        afl_content=afl_content,
        label=label,
    )
    flash(f"Version created (v{label or 'new'}).", "success")
    return redirect(url_for("strategy_bp.strategy_detail", strategy_id=strategy_id))


@strategy_bp.route("/strategy/<strategy_id>/run-with-params", methods=["POST"])
def run_with_params(strategy_id: str):
    """Run a backtest with modified parameter values."""
    try:
        from scripts.afl_parser import parse_afl_params, modify_afl_params
    except ImportError:
        flash("AFL parser not available.", "danger")
        return redirect(url_for("strategy_bp.strategy_detail", strategy_id=strategy_id))

    version_id = request.form.get("version_id")
    run_mode = int(request.form.get("run_mode", "2"))
    symbol = request.form.get("symbol", "").strip() or None
    date_range = request.form.get("date_range", "").strip() or None

    version = db_get_version(version_id)
    if version is None:
        flash("Version not found.", "danger")
        return redirect(url_for("strategy_bp.strategy_detail", strategy_id=strategy_id))

    afl_content = version.get("afl_content", "")
    if not afl_content:
        flash("Version has no AFL content.", "danger")
        return redirect(url_for("strategy_bp.strategy_detail", strategy_id=strategy_id))

    current_params = parse_afl_params(afl_content)
    overrides = {}
    min_overrides = {}
    max_overrides = {}
    step_overrides = {}
    optimize_names = set()

    for p in current_params:
        form_key = f"param_{p['name']}"
        form_val = request.form.get(form_key)
        if form_val is not None:
            try:
                overrides[p["name"]] = float(form_val)
            except ValueError:
                pass

        opt_key = f"optimize_{p['name']}"
        if request.form.get(opt_key) == "on":
            optimize_names.add(p["name"])

            for prefix, target in [("min_", min_overrides), ("max_", max_overrides), ("step_", step_overrides)]:
                val = request.form.get(f"{prefix}{p['name']}")
                if val is not None:
                    try:
                        target[p["name"]] = float(val)
                    except ValueError:
                        pass

    modified_afl = modify_afl_params(
        afl_content, overrides=overrides, optimize_names=optimize_names,
        min_overrides=min_overrides, max_overrides=max_overrides,
        step_overrides=step_overrides,
    )

    mode_label = "Optimization" if run_mode == 4 else "Backtest"
    changed_params = [
        f"{k}={v}" for k, v in overrides.items()
        if current_params and any(p["name"] == k and p["default"] != v for p in current_params)
    ]
    label = f"Parameter {mode_label.lower()}"
    if changed_params:
        label += f": {', '.join(changed_params[:3])}"
        if len(changed_params) > 3:
            label += f" (+{len(changed_params) - 3} more)"

    new_version_id = db_create_version(
        strategy_id=strategy_id,
        afl_content=modified_afl,
        label=label,
    )

    thread = threading.Thread(
        target=_run_backtest_background,
        args=(strategy_id, new_version_id, run_mode, symbol, date_range),
        daemon=True,
    )
    thread.start()

    flash(f"{mode_label} started with modified parameters.", "info")
    return redirect(url_for("strategy_bp.strategy_detail", strategy_id=strategy_id))


# ---------------------------------------------------------------------------
# Optimization
# ---------------------------------------------------------------------------


@strategy_bp.route("/api/strategy/<strategy_id>/optimize", methods=["POST"])
def api_strategy_optimize(strategy_id: str):
    """Run an optimization backtest for a strategy."""
    try:
        from scripts.afl_parser import parse_afl_params, modify_afl_params
    except ImportError:
        return jsonify({"error": "AFL parser not available."}), 500

    strategy = db_get_strategy(strategy_id)
    if strategy is None:
        return jsonify({"error": "Strategy not found."}), 404

    data = request.get_json(silent=True) or {}
    params_to_optimize = data.get("params_to_optimize", [])
    min_overrides = data.get("min_overrides", {})
    max_overrides = data.get("max_overrides", {})
    step_overrides = data.get("step_overrides", {})

    version = db_get_latest_version(strategy_id)
    if version is None:
        return jsonify({"error": "No versions found for strategy."}), 404

    afl_content = version.get("afl_content", "")
    if not afl_content:
        return jsonify({"error": "Version has no AFL content."}), 400

    optimize_names = set(params_to_optimize)
    modified_afl = modify_afl_params(
        afl_content,
        optimize_names=optimize_names,
        min_overrides=min_overrides,
        max_overrides=max_overrides,
        step_overrides=step_overrides,
    )

    param_list = ", ".join(params_to_optimize[:3])
    if len(params_to_optimize) > 3:
        param_list += f" (+{len(params_to_optimize) - 3} more)"
    label = f"Optimization: {param_list}" if param_list else "Optimization run"

    new_version_id = db_create_version(
        strategy_id=strategy_id,
        afl_content=modified_afl,
        label=label,
    )

    thread = threading.Thread(
        target=_run_backtest_background,
        args=(strategy_id, new_version_id, 4),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "version_id": new_version_id,
        "status": "running",
        "params_optimized": params_to_optimize,
    })


@strategy_bp.route("/api/strategy/<strategy_id>/param-analysis")
def api_strategy_param_analysis(strategy_id: str):
    """Get parameter optimization suggestions for a strategy."""
    from scripts.param_advisor import analyze_strategy_params
    analysis = analyze_strategy_params(strategy_id)
    return jsonify(analysis)


# ---------------------------------------------------------------------------
# Indicator Explorer
# ---------------------------------------------------------------------------


@strategy_bp.route("/strategy/<strategy_id>/explore")
def strategy_explore(strategy_id: str):
    """Render the interactive indicator explorer page for a strategy."""
    from scripts.afl_parser import (
        parse_afl_params, extract_strategy_indicators, build_code_map,
    )

    strategy = db_get_strategy(strategy_id)
    if strategy is None:
        flash("Strategy not found.", "error")
        return redirect("/")

    version = db_get_latest_version(strategy_id)
    afl_content = version.get("afl_content", "") if version else ""

    params = parse_afl_params(afl_content) if afl_content else []
    indicator_configs = extract_strategy_indicators(afl_content) if afl_content else []
    description = strategy.get("description", "") if strategy else ""
    code_map = build_code_map(description, afl_content) if afl_content else []

    explore_symbol = request.args.get("symbol") or DEFAULT_SYMBOL

    return render_template(
        "indicator_explorer.html",
        strategy=strategy,
        params=params,
        indicator_configs=indicator_configs,
        symbol=explore_symbol,
        afl_content=afl_content,
        code_map=code_map,
        db_configured=bool(AMIBROKER_DB_PATH),
    )


@strategy_bp.route("/api/strategy/<strategy_id>/explorer-data")
def api_strategy_explorer_data(strategy_id: str):
    """Fetch OHLCV bars + computed indicators for the indicator explorer."""
    import time as _perf_time
    from flask import current_app
    _t_total_start = _perf_time.perf_counter()

    from scripts.ole_stock_data import get_latest_bars
    from scripts.indicators import compute_indicators
    from scripts.afl_parser import parse_afl_params, extract_strategy_indicators

    strategy = db_get_strategy(strategy_id)
    if strategy is None:
        return jsonify({"error": "Strategy not found"}), 404

    version = db_get_latest_version(strategy_id)
    afl_content = version.get("afl_content", "") if version else ""
    if not afl_content:
        return jsonify({"error": "No AFL content in latest version"}), 400

    try:
        interval = int(request.args.get("interval", "60"))
    except ValueError:
        interval = 60
    if interval not in VALID_INTERVALS:
        interval = 60

    default_days = CHART_SETTINGS.get("explorer_default_days", 5)
    try:
        days = int(request.args.get("days", str(default_days)))
    except ValueError:
        days = default_days
    days = max(1, min(days, 365))

    end_date = request.args.get("end_date")

    param_overrides = {}
    for key, val in request.args.items():
        if key.startswith("param_"):
            param_name = key[6:]
            try:
                param_overrides[param_name] = float(val)
            except ValueError:
                pass

    _t_afl_parse_start = _perf_time.perf_counter()
    indicator_configs = extract_strategy_indicators(afl_content)

    for cfg in indicator_configs:
        mapping = cfg.get("param_mapping", {})
        for ind_param, afl_param_name in mapping.items():
            if afl_param_name in param_overrides:
                cfg["params"][ind_param] = param_overrides[afl_param_name]
    _t_afl_parse_ms = (_perf_time.perf_counter() - _t_afl_parse_start) * 1000

    explore_symbol = request.args.get("symbol") or DEFAULT_SYMBOL

    cache_key = (strategy_id, explore_symbol, interval, days, end_date)
    cached = _explorer_bars_cache.get(cache_key)

    data_range = None
    _bar_source = "cache"
    _t_bars_start = _perf_time.perf_counter()
    if cached and (datetime.now() - cached["fetched_at"]).total_seconds() < 300:
        bars = cached["bars"]
        data_range = cached.get("data_range")
    else:
        _bar_source = "amiBroker_COM"
        result = get_latest_bars(
            symbol=explore_symbol,
            interval=interval,
            days=days,
            end_date=end_date,
        )
        if result.get("error"):
            return jsonify({"error": result["error"]}), 503
        bars = result.get("data", [])
        data_range = result.get("data_range")
        _explorer_bars_cache[cache_key] = {
            "bars": bars,
            "data_range": data_range,
            "fetched_at": datetime.now(),
        }
    _t_bars_ms = (_perf_time.perf_counter() - _t_bars_start) * 1000

    if not bars:
        return jsonify({
            "error": "No bar data available for this date range.",
            "data_range": data_range,
        }), 404

    _t_indicators_start = _perf_time.perf_counter()
    ind_configs_for_compute = [
        {"type": cfg["type"], "params": cfg["params"]}
        for cfg in indicator_configs
    ]
    computed = compute_indicators(bars, ind_configs_for_compute)

    for i, ind in enumerate(computed):
        if i < len(indicator_configs):
            ind["overlay"] = indicator_configs[i].get("overlay", True)
            ind["color"] = indicator_configs[i].get("color", "#FF6D00")
    _t_indicators_ms = (_perf_time.perf_counter() - _t_indicators_start) * 1000

    _t_total_ms = (_perf_time.perf_counter() - _t_total_start) * 1000
    _timing = {
        "total_ms": round(_t_total_ms, 1),
        "afl_parse_ms": round(_t_afl_parse_ms, 1),
        "bar_fetch_ms": round(_t_bars_ms, 1),
        "bar_source": _bar_source,
        "bar_count": len(bars),
        "indicator_compute_ms": round(_t_indicators_ms, 1),
        "indicator_count": len(computed),
    }
    current_app.logger.info("explorer-data timing: %s", _timing)

    return jsonify({
        "bars": bars,
        "indicators": computed,
        "indicator_configs": indicator_configs,
        "data_range": data_range,
        "_timing": _timing,
    })


@strategy_bp.route("/api/strategy/<strategy_id>/recalculate", methods=["POST"])
def api_strategy_recalculate(strategy_id: str):
    """Recalculate indicators with new parameter values (no OHLCV re-fetch)."""
    from scripts.indicators import compute_indicators
    from scripts.afl_parser import extract_strategy_indicators

    strategy = db_get_strategy(strategy_id)
    if strategy is None:
        return jsonify({"error": "Strategy not found"}), 404

    version = db_get_latest_version(strategy_id)
    afl_content = version.get("afl_content", "") if version else ""
    if not afl_content:
        return jsonify({"error": "No AFL content"}), 400

    body = request.get_json(silent=True) or {}
    param_overrides = body.get("params", {})
    interval = body.get("interval", 60)
    days = body.get("days", CHART_SETTINGS.get("explorer_default_days", 5))
    end_date = body.get("end_date")
    recalc_symbol = body.get("symbol") or DEFAULT_SYMBOL

    cache_key = (strategy_id, recalc_symbol, interval, days, end_date)
    cached = _explorer_bars_cache.get(cache_key)
    if not cached or not cached.get("bars"):
        for k, v in _explorer_bars_cache.items():
            if k[0] == strategy_id and k[1] == recalc_symbol and v.get("bars"):
                cached = v
                break
    if not cached or not cached.get("bars"):
        return jsonify({"error": "No cached bars. Load the explorer first."}), 400

    bars = cached["bars"]

    indicator_configs = extract_strategy_indicators(afl_content)
    for cfg in indicator_configs:
        mapping = cfg.get("param_mapping", {})
        for ind_param, afl_param_name in mapping.items():
            if afl_param_name in param_overrides:
                try:
                    cfg["params"][ind_param] = float(param_overrides[afl_param_name])
                except (ValueError, TypeError):
                    pass

    ind_configs_for_compute = [
        {"type": cfg["type"], "params": cfg["params"]}
        for cfg in indicator_configs
    ]
    computed = compute_indicators(bars, ind_configs_for_compute)

    for i, ind in enumerate(computed):
        if i < len(indicator_configs):
            ind["overlay"] = indicator_configs[i].get("overlay", True)
            ind["color"] = indicator_configs[i].get("color", "#FF6D00")

    return jsonify({"indicators": computed})


# ---------------------------------------------------------------------------
# Signal Computation via AmiBroker Exploration
# ---------------------------------------------------------------------------


@strategy_bp.route("/api/strategy/<strategy_id>/signals", methods=["POST"])
def api_strategy_signals(strategy_id: str):
    """Compute Buy/Short/Sell/Cover signals via AmiBroker OLE Exploration."""
    from flask import current_app
    from scripts.ole_bar_analyzer import compute_signals_via_exploration

    strategy = db_get_strategy(strategy_id)
    if strategy is None:
        return jsonify({"error": "Strategy not found"}), 404

    version = db_get_latest_version(strategy_id)
    afl_content = version.get("afl_content", "") if version else ""
    if not afl_content:
        return jsonify({"error": "No AFL content"}), 400

    body = request.get_json(silent=True) or {}
    param_overrides = body.get("params", {})
    interval = body.get("interval", 60)
    days = body.get("days", CHART_SETTINGS.get("explorer_default_days", 5))
    end_date = body.get("end_date")
    sig_symbol = body.get("symbol") or DEFAULT_SYMBOL

    pv = {}
    for k, v in param_overrides.items():
        try:
            pv[k] = float(v)
        except (ValueError, TypeError):
            pass

    result = compute_signals_via_exploration(
        afl_content=afl_content,
        param_values=pv,
        symbol=sig_symbol,
        interval=interval,
    )

    if result.get("error"):
        current_app.logger.warning("Signal computation error: %s", result["error"])
        return jsonify(result), 503

    cache_key = (strategy_id, sig_symbol, interval, days, end_date)
    cached = _explorer_bars_cache.get(cache_key)
    if cached and cached.get("bars"):
        bars = cached["bars"]
        min_time = bars[0]["time"]
        max_time = bars[-1]["time"]
        for key in ("buy", "short", "sell", "cover"):
            result[key] = [
                s for s in result[key]
                if min_time <= s["time"] <= max_time
            ]

    current_app.logger.info(
        "Signals for %s: %d Buy, %d Short, %d Sell, %d Cover (%dms)",
        strategy_id[:8],
        len(result.get("buy", [])), len(result.get("short", [])),
        len(result.get("sell", [])), len(result.get("cover", [])),
        result.get("elapsed_ms", 0),
    )

    return jsonify(result)


# ---------------------------------------------------------------------------
# Bar Analysis API
# ---------------------------------------------------------------------------


@strategy_bp.route("/api/strategy/<strategy_id>/analyze-bar", methods=["POST"])
def api_analyze_bar(strategy_id: str):
    """Analyze signal conditions at a specific bar using AmiBroker OLE."""
    from scripts.ole_bar_analyzer import analyze_bar

    strategy = db_get_strategy(strategy_id)
    if strategy is None:
        return jsonify({"error": "Strategy not found"}), 404

    version = db_get_latest_version(strategy_id)
    afl_content = version.get("afl_content", "") if version else ""
    if not afl_content:
        return jsonify({"error": "No AFL content"}), 400

    body = request.get_json(silent=True) or {}
    bar_time = body.get("bar_time")
    if bar_time is None:
        return jsonify({"error": "bar_time is required"}), 400

    param_overrides = body.get("params", {})

    pv = {}
    for k, v in param_overrides.items():
        try:
            pv[k] = float(v)
        except (ValueError, TypeError):
            pass

    try:
        result = analyze_bar(
            afl_content=afl_content,
            target_unix_ts=int(bar_time),
            strategy_id=strategy_id,
            param_values=pv,
        )
        return jsonify({"analysis": result})
    except Exception as exc:
        logger.exception("Bar analysis failed: %s", exc)
        return jsonify({"error": f"Analysis failed: {exc}"}), 500


# ---------------------------------------------------------------------------
# Strategy diff / status / deployable
# ---------------------------------------------------------------------------


@strategy_bp.route("/api/strategy/<strategy_id>/diff/<v1_id>/<v2_id>")
def api_strategy_diff(strategy_id, v1_id, v2_id):
    """Return a unified diff between two strategy versions."""
    import difflib
    from scripts.strategy_db import get_version

    v1 = get_version(v1_id)
    v2 = get_version(v2_id)

    if not v1 or not v2:
        return jsonify({"error": "Version not found"}), 404

    if v1.get("strategy_id") != strategy_id or v2.get("strategy_id") != strategy_id:
        return jsonify({"error": "Version does not belong to this strategy"}), 400

    afl1 = v1.get("afl_content", "").splitlines(keepends=True)
    afl2 = v2.get("afl_content", "").splitlines(keepends=True)

    diff = list(difflib.unified_diff(
        afl1, afl2,
        fromfile=f"v{v1.get('version_number', '?')} ({v1_id[:8]})",
        tofile=f"v{v2.get('version_number', '?')} ({v2_id[:8]})",
    ))

    return jsonify({
        "diff": "".join(diff),
        "v1_version": v1.get("version_number"),
        "v2_version": v2.get("version_number"),
        "v1_label": v1.get("label", ""),
        "v2_label": v2.get("label", ""),
        "has_changes": len(diff) > 0,
    })


@strategy_bp.route("/api/strategy/<strategy_id>/status", methods=["POST"])
def api_update_strategy_status(strategy_id):
    """Update a strategy's lifecycle status."""
    from scripts.strategy_db import update_strategy_status, STRATEGY_STATUSES

    data = request.get_json(silent=True) or {}
    status = data.get("status", "")

    if status not in STRATEGY_STATUSES:
        return jsonify({
            "error": f"Invalid status. Must be one of: {', '.join(STRATEGY_STATUSES)}"
        }), 400

    success = update_strategy_status(strategy_id, status)
    if success:
        return jsonify({"status": status, "message": f"Strategy status updated to '{status}'"})
    else:
        return jsonify({"error": "Strategy not found"}), 404


@strategy_bp.route("/api/strategies/deployable")
def api_deployable_strategies():
    """List strategies that are approved or live (eligible for deployment)."""
    from scripts.strategy_db import list_deployable_strategies
    strategies = list_deployable_strategies()
    return jsonify(strategies)


# ---------------------------------------------------------------------------
# Strategy comparison
# ---------------------------------------------------------------------------


@strategy_bp.route("/compare")
def compare_page():
    """Strategy comparison page for side-by-side metrics."""
    from scripts.strategy_db import list_strategies
    strategies = list_strategies()
    return render_template("compare.html", strategies=strategies)


@strategy_bp.route("/api/compare")
def api_compare():
    """Compare metrics across multiple backtest runs."""
    from scripts.strategy_db import get_run
    run_ids = request.args.getlist("run_ids")
    if not run_ids:
        return jsonify({"error": "No run_ids provided"}), 400

    runs = []
    for rid in run_ids:
        run = get_run(rid)
        if run:
            runs.append({
                "run_id": rid,
                "strategy_id": run.get("strategy_id"),
                "metrics": run.get("metrics", {}),
                "status": run.get("status"),
                "created_at": run.get("created_at"),
                "symbol": run.get("symbol", ""),
                "date_range": run.get("date_range", ""),
            })
    return jsonify(runs)


# ---------------------------------------------------------------------------
# Iterative AFL refinement endpoint
# ---------------------------------------------------------------------------


@strategy_bp.route("/api/strategy-builder/refine", methods=["POST"])
def api_refine_strategy():
    """Refine a strategy's AFL based on backtest results."""
    from scripts.afl_generator import refine_afl

    data = request.get_json(silent=True) or {}
    strategy_name = data.get("strategy_name", "Unnamed Strategy")
    description = data.get("description", "")
    current_afl = data.get("current_afl", "")
    backtest_results = data.get("backtest_results", {})
    iteration = int(data.get("iteration", 1))
    symbol = data.get("symbol", "")

    if not current_afl:
        return jsonify({"error": "current_afl is required"}), 400

    result = refine_afl(
        strategy_name=strategy_name,
        description=description,
        current_afl=current_afl,
        backtest_results=backtest_results,
        iteration=iteration,
        symbol=symbol,
    )

    return jsonify(result)
