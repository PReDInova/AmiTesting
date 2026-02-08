"""
Strategy lifecycle management for multi-strategy support.

Manages the strategies/ directory structure, manifest.json,
and per-strategy metadata. Each strategy gets its own subdirectory
with AFL, APX, results, and version history.
"""

import json
import logging
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import STRATEGIES_DIR, APX_TEMPLATE, AMIBROKER_DB_PATH
from scripts.apx_builder import build_apx

logger = logging.getLogger(__name__)

MANIFEST_PATH: Path = STRATEGIES_DIR / "manifest.json"


def _ensure_manifest() -> None:
    """Create strategies/ dir and manifest.json if they don't exist."""
    STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)
    if not MANIFEST_PATH.exists():
        MANIFEST_PATH.write_text(json.dumps({"strategies": []}, indent=2),
                                  encoding="utf-8")


def _read_manifest() -> dict:
    """Read and return manifest.json contents."""
    _ensure_manifest()
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _write_manifest(data: dict) -> None:
    """Write data to manifest.json."""
    _ensure_manifest()
    MANIFEST_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

def generate_strategy_id(name: str) -> str:
    """Create a filesystem-safe ID from name + timestamp.

    Example: 'RSI Mmentum' -> 'rsi_momentum_20260207_143022'
    """
    safe = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40]
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{safe}_{ts}"


def create_strategy(name: str, description: str = "",
                    source: str = "manual", afl_content: str = "") -> str:
    """Create a new strategy directory and manifest entry.

    Returns the strategy_id.
    """
    strategy_id = generate_strategy_id(name)
    strategy_dir = STRATEGIES_DIR / strategy_id
    strategy_dir.mkdir(parents=True, exist_ok=True)
    (strategy_dir / "versions").mkdir(exist_ok=True)

    metadata = {
        "id": strategy_id,
        "name": name,
        "description": description,
        "source": source,
        "status": "draft",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_tested_at": None,
        "afl_path": str(strategy_dir / "strategy.afl"),
        "apx_path": str(strategy_dir / "strategy.apx"),
        "results_csv_path": str(strategy_dir / "results.csv"),
        "results_html_path": str(strategy_dir / "results.html"),
    }

    # Write strategy.json
    (strategy_dir / "strategy.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )

    # Write initial AFL if provided
    if afl_content:
        (strategy_dir / "strategy.afl").write_text(afl_content, encoding="utf-8")

    # Update manifest
    manifest = _read_manifest()
    manifest["strategies"].append({
        "id": strategy_id,
        "name": name,
        "source": source,
        "status": "draft",
        "created_at": metadata["created_at"],
    })
    _write_manifest(manifest)

    logger.info("Created strategy '%s' (id=%s)", name, strategy_id)
    return strategy_id

def get_strategy(strategy_id: str) -> dict:
    """Load strategy.json for a given strategy."""
    strategy_json = STRATEGIES_DIR / strategy_id / "strategy.json"
    if not strategy_json.exists():
        raise FileNotFoundError(f"Strategy not found: {strategy_id}")
    return json.loads(strategy_json.read_text(encoding="utf-8"))


def list_strategies() -> list:
    """Return all strategies from manifest.json, newest first."""
    manifest = _read_manifest()
    strategies = manifest.get("strategies", [])
    # Enrich with data from individual strategy.json files
    enriched = []
    for entry in strategies:
        try:
            full = get_strategy(entry["id"])
            enriched.append(full)
        except FileNotFoundError:
            enriched.append(entry)
    enriched.sort(key=lambda s: s.get("created_at", ""), reverse=True)
    return enriched


def update_strategy_status(strategy_id: str, status: str) -> None:
    """Update status in both strategy.json and manifest.json."""
    # Update strategy.json
    meta = get_strategy(strategy_id)
    meta["status"] = status
    if status == "tested":
        meta["last_tested_at"] = datetime.now(timezone.utc).isoformat()
    save_strategy_metadata(strategy_id, meta)

    # Update manifest entry
    manifest = _read_manifest()
    for entry in manifest["strategies"]:
        if entry["id"] == strategy_id:
            entry["status"] = status
            break
    _write_manifest(manifest)
    logger.info("Strategy '%s' status updated to '%s'", strategy_id, status)

def save_strategy_metadata(strategy_id: str, metadata: dict) -> None:
    """Write strategy.json into the strategy directory."""
    strategy_json = STRATEGIES_DIR / strategy_id / "strategy.json"
    strategy_json.write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def get_strategy_afl(strategy_id: str) -> str:
    """Read the AFL file for a strategy."""
    meta = get_strategy(strategy_id)
    afl_path = Path(meta["afl_path"])
    if not afl_path.exists():
        return ""
    return afl_path.read_text(encoding="utf-8")


def save_strategy_afl(strategy_id: str, content: str) -> tuple:
    """Write AFL content for a strategy. Returns (success, message)."""
    try:
        meta = get_strategy(strategy_id)
        afl_path = Path(meta["afl_path"])
        afl_path.write_text(content, encoding="utf-8")
        logger.info("Saved AFL for strategy '%s'", strategy_id)
        return True, "AFL saved successfully"
    except Exception as exc:
        logger.error("Failed to save AFL for '%s': %s", strategy_id, exc)
        return False, str(exc)


def build_strategy_apx(strategy_id: str) -> tuple:
    """Build the APX file for a specific strategy.

    Returns (success, message).
    """
    try:
        meta = get_strategy(strategy_id)
        afl_path = meta["afl_path"]
        apx_path = meta["apx_path"]

        result = build_apx(
            afl_path=afl_path,
            output_apx_path=apx_path,
            template_apx_path=str(APX_TEMPLATE),
        )
        logger.info("Built APX for strategy '%s': %s", strategy_id, result)
        return True, result
    except Exception as exc:
        logger.error("APX build failed for '%s': %s", strategy_id, exc)
        return False, str(exc)

def get_strategy_versions(strategy_id: str) -> list:
    """List AFL version snapshots for a strategy, newest first."""
    versions_dir = STRATEGIES_DIR / strategy_id / "versions"
    if not versions_dir.exists():
        return []
    versions = []
    for f in sorted(versions_dir.glob("*.afl"), reverse=True):
        versions.append({
            "name": f.stem,
            "filename": f.name,
            "filepath": str(f),
            "size_kb": round(f.stat().st_size / 1024, 1),
            "modified": datetime.fromtimestamp(
                f.stat().st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M"),
        })
    return versions


def save_strategy_version(strategy_id: str, content: str,
                          label: str = "") -> tuple:
    """Save a versioned snapshot of AFL content.

    Returns (success, version_filename).
    """
    try:
        versions_dir = STRATEGIES_DIR / strategy_id / "versions"
        versions_dir.mkdir(parents=True, exist_ok=True)

        existing = list(versions_dir.glob("v*.afl"))
        next_num = len(existing) + 1
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        safe_label = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:30]
        if safe_label:
            filename = f"v{next_num:03d}_{ts}_{safe_label}.afl"
        else:
            filename = f"v{next_num:03d}_{ts}.afl"

        (versions_dir / filename).write_text(content, encoding="utf-8")
        logger.info("Saved version '%s' for strategy '%s'", filename, strategy_id)
        return True, filename
    except Exception as exc:
        logger.error("Failed to save version for '%s': %s", strategy_id, exc)
        return False, str(exc)


def delete_strategy(strategy_id: str) -> None:
    """Remove strategy directory and manifest entry."""
    strategy_dir = STRATEGIES_DIR / strategy_id
    if strategy_dir.exists():
        shutil.rmtree(strategy_dir)

    manifest = _read_manifest()
    manifest["strategies"] = [
        s for s in manifest["strategies"] if s["id"] != strategy_id
    ]
    _write_manifest(manifest)
    logger.info("Deleted strategy '%s'", strategy_id)
