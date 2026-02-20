"""
Indicators blueprint -- indicator library list, create, edit, import,
tooltip APIs (both param and indicator).
"""

import logging
from pathlib import Path

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from scripts.strategy_db import (
    get_all_param_tooltips_dict as db_get_all_param_tooltips_dict,
    get_param_tooltip as db_get_param_tooltip,
    upsert_param_tooltip as db_upsert_param_tooltip,
    delete_param_tooltip as db_delete_param_tooltip,
    get_all_indicator_tooltips_dict as db_get_all_indicator_tooltips_dict,
    get_indicator_tooltip as db_get_indicator_tooltip,
    upsert_indicator_tooltip as db_upsert_indicator_tooltip,
    delete_indicator_tooltip as db_delete_indicator_tooltip,
)

logger = logging.getLogger(__name__)

indicators_bp = Blueprint("indicators_bp", __name__)


# ---------------------------------------------------------------------------
# Indicator Library routes
# ---------------------------------------------------------------------------


@indicators_bp.route("/indicators")
def indicator_list():
    """Indicator library list page."""
    from scripts.indicator_library import list_indicators

    indicators = list_indicators()

    show_import = False
    ext_dir = Path(r"C:\Users\prestondinova\Documents\indicators")
    ext_file = Path(r"C:\Users\prestondinova\Documents\market_sessions.afl")
    if ext_dir.exists() or ext_file.exists():
        show_import = True

    return render_template(
        "indicator_list.html",
        indicators=indicators,
        show_import=show_import,
    )


@indicators_bp.route("/indicators/create", methods=["GET", "POST"])
def indicator_create():
    """Create a new indicator file."""
    if request.method == "GET":
        return render_template(
            "indicator_editor.html",
            filename="",
            content="",
            meta=None,
        )

    from scripts.indicator_library import save_indicator

    filename = request.form.get("filename", "").strip()
    content = request.form.get("afl_content", "")

    if not filename:
        flash("Filename is required.", "danger")
        return render_template(
            "indicator_editor.html",
            filename=filename,
            content=content,
            meta=None,
        )

    if not filename.endswith(".afl"):
        filename += ".afl"

    ok, msg = save_indicator(filename, content)
    if ok:
        flash(f"Indicator '{filename}' created.", "success")
        return redirect(url_for("indicators_bp.indicator_edit", filename=filename))
    else:
        flash(f"Error: {msg}", "danger")
        return render_template(
            "indicator_editor.html",
            filename=filename,
            content=content,
            meta=None,
        )


@indicators_bp.route("/indicators/<filename>")
def indicator_edit(filename: str):
    """Indicator editor page -- view/edit a single indicator AFL file."""
    from scripts.indicator_library import read_indicator

    content, meta = read_indicator(filename)
    if meta is None:
        flash(f"Indicator '{filename}' not found.", "danger")
        return redirect(url_for("indicators_bp.indicator_list"))
    return render_template(
        "indicator_editor.html",
        filename=filename,
        content=content,
        meta=meta,
    )


@indicators_bp.route("/indicators/<filename>/save", methods=["POST"])
def indicator_save(filename: str):
    """Save indicator content."""
    from scripts.indicator_library import save_indicator

    content = request.form.get("afl_content", "")
    ok, msg = save_indicator(filename, content)
    if ok:
        flash(f"Indicator '{filename}' saved.", "success")
    else:
        flash(f"Error saving: {msg}", "danger")
    return redirect(url_for("indicators_bp.indicator_edit", filename=filename))


@indicators_bp.route("/indicators/<filename>/delete", methods=["POST"])
def indicator_delete(filename: str):
    """Delete an indicator file."""
    from scripts.indicator_library import delete_indicator

    ok, msg = delete_indicator(filename)
    if ok:
        flash(f"Indicator '{filename}' deleted.", "success")
    else:
        flash(f"Error: {msg}", "danger")
    return redirect(url_for("indicators_bp.indicator_list"))


@indicators_bp.route("/indicators/import", methods=["POST"])
def indicator_import():
    """Import indicators from external directories."""
    from scripts.indicator_library import import_indicators

    source_paths = []
    ext_dir = Path(r"C:\Users\prestondinova\Documents\indicators")
    ext_file = Path(r"C:\Users\prestondinova\Documents\market_sessions.afl")
    if ext_dir.exists():
        source_paths.append(ext_dir)
    if ext_file.exists():
        source_paths.append(ext_file)

    if not source_paths:
        flash("No external indicator sources found.", "warning")
        return redirect(url_for("indicators_bp.indicator_list"))

    results = import_indicators(source_paths)
    imported = sum(1 for _, ok, _ in results if ok)
    skipped = sum(1 for _, ok, msg in results if not ok and "skip" in msg.lower())
    flash(f"Imported {imported} indicator(s), {skipped} skipped (already exist).", "success")
    return redirect(url_for("indicators_bp.indicator_list"))


# ---------------------------------------------------------------------------
# Indicator Library API routes
# ---------------------------------------------------------------------------


@indicators_bp.route("/api/indicators/library")
def api_indicator_library():
    """JSON API: list all indicators with parsed metadata."""
    from scripts.indicator_library import list_indicators
    from dataclasses import asdict

    indicators = list_indicators()
    return jsonify([asdict(ind) for ind in indicators])


@indicators_bp.route("/api/indicators/library/<filename>")
def api_indicator_detail(filename: str):
    """JSON API: single indicator content + metadata."""
    from scripts.indicator_library import read_indicator
    from dataclasses import asdict

    content, meta = read_indicator(filename)
    if meta is None:
        return jsonify({"error": f"Indicator '{filename}' not found"}), 404
    result = asdict(meta)
    result["content"] = content
    return jsonify(result)


@indicators_bp.route("/api/indicators/generate-include", methods=["POST"])
def api_generate_include():
    """JSON API: generate #include AFL block from a configuration."""
    from scripts.indicator_library import generate_include_block

    data = request.get_json(silent=True) or {}
    indicators = data.get("indicators", [])

    if not indicators:
        return jsonify({"error": "No indicators specified"}), 400

    try:
        afl_block = generate_include_block(indicators)
        return jsonify({"afl_block": afl_block, "warnings": []})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


# ---------------------------------------------------------------------------
# Param tooltips API
# ---------------------------------------------------------------------------


@indicators_bp.route("/api/param-tooltips")
def api_param_tooltips():
    """Return all parameter tooltips as a dict keyed by parameter name."""
    return jsonify({"tooltips": db_get_all_param_tooltips_dict()})


@indicators_bp.route("/api/param-tooltips/<name>")
def api_param_tooltip_get(name: str):
    """Return a single parameter tooltip by name."""
    tip = db_get_param_tooltip(name)
    if tip is None:
        return jsonify({"error": f"No tooltip for '{name}'"}), 404
    return jsonify(tip)


@indicators_bp.route("/api/param-tooltips/<name>", methods=["PUT"])
def api_param_tooltip_upsert(name: str):
    """Create or update a parameter tooltip."""
    data = request.get_json(silent=True) or {}
    db_upsert_param_tooltip(
        name=name,
        indicator=data.get("indicator", ""),
        math=data.get("math", ""),
        param=data.get("param", ""),
        typical=data.get("typical", ""),
        guidance=data.get("guidance", ""),
    )
    return jsonify({"ok": True, "name": name})


@indicators_bp.route("/api/param-tooltips/<name>", methods=["DELETE"])
def api_param_tooltip_delete(name: str):
    """Delete a parameter tooltip."""
    deleted = db_delete_param_tooltip(name)
    if not deleted:
        return jsonify({"error": f"No tooltip for '{name}'"}), 404
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Indicator tooltips API
# ---------------------------------------------------------------------------


@indicators_bp.route("/api/indicator-tooltips")
def api_indicator_tooltips():
    """Return all indicator tooltips as a dict keyed by keyword."""
    return jsonify({"tooltips": db_get_all_indicator_tooltips_dict()})


@indicators_bp.route("/api/indicator-tooltips/<keyword>")
def api_indicator_tooltip_get(keyword: str):
    """Return a single indicator tooltip by keyword."""
    tip = db_get_indicator_tooltip(keyword)
    if tip is None:
        return jsonify({"error": f"No tooltip for '{keyword}'"}), 404
    return jsonify(tip)


@indicators_bp.route("/api/indicator-tooltips/<keyword>", methods=["PUT"])
def api_indicator_tooltip_upsert(keyword: str):
    """Create or update an indicator tooltip."""
    data = request.get_json(silent=True) or {}
    db_upsert_indicator_tooltip(
        keyword=keyword,
        name=data.get("name", ""),
        description=data.get("description", ""),
        math=data.get("math", ""),
        usage=data.get("usage", ""),
        key_params=data.get("key_params", ""),
    )
    return jsonify({"ok": True, "keyword": keyword})


@indicators_bp.route("/api/indicator-tooltips/<keyword>", methods=["DELETE"])
def api_indicator_tooltip_delete(keyword: str):
    """Delete an indicator tooltip."""
    deleted = db_delete_indicator_tooltip(keyword)
    if not deleted:
        return jsonify({"error": f"No tooltip for '{keyword}'"}), 404
    return jsonify({"ok": True})
