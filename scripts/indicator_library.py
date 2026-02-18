"""
AFL Indicator Library -- file-based indicator management and AFL include generation.

Indicators are AFL files stored in the project's indicators/ directory. Each file
is parsed on read to extract metadata: parameters (Param/ParamToggle with typeof/
IsEmpty guards), required inputs, and output variables.
"""

import logging
import re
import shutil
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class IndicatorParam:
    """A single AFL parameter extracted from an indicator file."""

    var_name: str
    display_name: str
    default: str
    min_val: str = ""
    max_val: str = ""
    step: str = ""
    param_type: str = "Param"       # "Param" or "ParamToggle"
    toggle_options: str = ""         # For ParamToggle: "No|Yes"
    has_guard: bool = True           # Wrapped in typeof/IsEmpty guard


@dataclass
class IndicatorInput:
    """A required input variable that must be defined before #include."""

    var_name: str
    description: str
    optional: bool = False


@dataclass
class IndicatorMeta:
    """Full metadata for an indicator AFL file."""

    filename: str
    filepath: str
    display_name: str
    description: str = ""
    indicator_type: str = "include"  # "include" or "standalone"
    params: list = field(default_factory=list)
    required_inputs: list = field(default_factory=list)
    output_vars: list = field(default_factory=list)
    has_plots: bool = False
    size_kb: float = 0.0
    modified_date: str = ""


# ---------------------------------------------------------------------------
# Regex patterns for AFL parsing
# ---------------------------------------------------------------------------

# Pattern A: typeof guard (single-line or with braces)
# if( typeof( varName ) == "undefined" )  varName = Param( "label", default, min, max, step );
_RE_TYPEOF_PARAM = re.compile(
    r'if\s*\(\s*typeof\s*\(\s*(\w+)\s*\)\s*==\s*"undefined"\s*\)'
    r'[\s\n{]*'
    r'\1\s*=\s*Param\s*\(\s*"([^"]+)"\s*,\s*([^)]+)\)',
    re.MULTILINE,
)

# Pattern A2: typeof guard with ParamToggle
_RE_TYPEOF_TOGGLE = re.compile(
    r'if\s*\(\s*typeof\s*\(\s*(\w+)\s*\)\s*==\s*"undefined"\s*\)'
    r'[\s\n{]*'
    r'\1\s*=\s*ParamToggle\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*(\d+)\s*\)',
    re.MULTILINE,
)

# Pattern B: IsEmpty guard (single-line or with braces)
# if( IsEmpty( varName ) )  { varName = Param( "label", default, min, max, step ); }
_RE_ISEMPTY_PARAM = re.compile(
    r'if\s*\(\s*IsEmpty\s*\(\s*(\w+)\s*\)\s*\)'
    r'[\s\n{]*'
    r'\1\s*=\s*Param\s*\(\s*"([^"]+)"\s*,\s*([^)]+)\)',
    re.MULTILINE,
)

# Pattern B2: IsEmpty guard with ParamToggle
_RE_ISEMPTY_TOGGLE = re.compile(
    r'if\s*\(\s*IsEmpty\s*\(\s*(\w+)\s*\)\s*\)'
    r'[\s\n{]*'
    r'\1\s*=\s*ParamToggle\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*(\d+)\s*\)',
    re.MULTILINE,
)

# Pattern C: Bare Param (no guard)
_RE_BARE_PARAM = re.compile(
    r'(\w+)\s*=\s*Param\s*\(\s*"([^"]+)"\s*,\s*([^)]+)\)',
)

# Pattern D: Bare ParamToggle (no guard)
_RE_BARE_TOGGLE = re.compile(
    r'(\w+)\s*=\s*ParamToggle\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*(\d+)\s*\)',
)

# INPUTS comment header
_RE_INPUTS_BLOCK = re.compile(
    r'//\s*INPUTS[^:]*:\s*\n((?:\s*//\s*-\s*\w+[^\n]*\n)+)',
    re.IGNORECASE | re.MULTILINE,
)
_RE_INPUT_LINE = re.compile(r'//\s*-\s*(\w+)\s*:\s*(.*)')

# OUTPUTS comment header
_RE_OUTPUTS_BLOCK = re.compile(
    r'//\s*OUTPUTS[^:]*:\s*\n((?:\s*//\s*-\s*\w+[^\n]*\n)+)',
    re.IGNORECASE | re.MULTILINE,
)
_RE_OUTPUT_LINE = re.compile(r'//\s*-\s*(\w+)\s*:\s*(.*)')

# Display name from header (first line of === block or first non-empty comment)
_RE_HEADER_NAME = re.compile(
    r'//\s*={3,}\s*\n//\s*(.+?)\s*\n',
    re.MULTILINE,
)

# Description from header (lines between name and next === or INPUTS/OUTPUTS)
_RE_HEADER_DESC = re.compile(
    r'//\s*={3,}\s*\n//\s*.+?\s*\n//\s*={3,}\s*\n((?://[^\n]*\n)*)',
    re.MULTILINE,
)

# Plot() detection
_RE_PLOT = re.compile(r'\bPlot\s*\(', re.IGNORECASE)

# Title assignment detection
_RE_TITLE = re.compile(r'\bTitle\s*=', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def get_indicators_dir() -> Path:
    """Return the indicators directory path, creating it if needed."""
    from config.settings import INDICATORS_DIR

    INDICATORS_DIR.mkdir(parents=True, exist_ok=True)
    return INDICATORS_DIR


# ---------------------------------------------------------------------------
# AFL parsing
# ---------------------------------------------------------------------------


def _strip_afl_comments(content: str) -> str:
    """Remove AFL block comments (/* ... */). Preserves // line comments."""
    return re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)


def _parse_param_args(args_str: str) -> tuple:
    """Parse the trailing args from a Param() call: 'default, min, max, step'.

    Returns (default, min_val, max_val, step) as strings.
    """
    parts = [p.strip().rstrip(";").rstrip("}").strip() for p in args_str.split(",")]
    default = parts[0] if len(parts) > 0 else ""
    min_val = parts[1] if len(parts) > 1 else ""
    max_val = parts[2] if len(parts) > 2 else ""
    step = parts[3] if len(parts) > 3 else ""
    return default, min_val, max_val, step


def _parse_params(content: str) -> list:
    """Extract Param() and ParamToggle() declarations with their guards."""
    params = []
    seen_vars = set()

    # Guarded Param (typeof)
    for m in _RE_TYPEOF_PARAM.finditer(content):
        var = m.group(1)
        if var in seen_vars:
            continue
        seen_vars.add(var)
        default, min_val, max_val, step = _parse_param_args(m.group(3))
        params.append(IndicatorParam(
            var_name=var,
            display_name=m.group(2),
            default=default,
            min_val=min_val,
            max_val=max_val,
            step=step,
            param_type="Param",
            has_guard=True,
        ))

    # Guarded ParamToggle (typeof)
    for m in _RE_TYPEOF_TOGGLE.finditer(content):
        var = m.group(1)
        if var in seen_vars:
            continue
        seen_vars.add(var)
        params.append(IndicatorParam(
            var_name=var,
            display_name=m.group(2),
            default=m.group(4),
            param_type="ParamToggle",
            toggle_options=m.group(3),
            has_guard=True,
        ))

    # Guarded Param (IsEmpty)
    for m in _RE_ISEMPTY_PARAM.finditer(content):
        var = m.group(1)
        if var in seen_vars:
            continue
        seen_vars.add(var)
        default, min_val, max_val, step = _parse_param_args(m.group(3))
        params.append(IndicatorParam(
            var_name=var,
            display_name=m.group(2),
            default=default,
            min_val=min_val,
            max_val=max_val,
            step=step,
            param_type="Param",
            has_guard=True,
        ))

    # Guarded ParamToggle (IsEmpty)
    for m in _RE_ISEMPTY_TOGGLE.finditer(content):
        var = m.group(1)
        if var in seen_vars:
            continue
        seen_vars.add(var)
        params.append(IndicatorParam(
            var_name=var,
            display_name=m.group(2),
            default=m.group(4),
            param_type="ParamToggle",
            toggle_options=m.group(3),
            has_guard=True,
        ))

    # Bare Param (no guard) -- only if not already captured
    for m in _RE_BARE_PARAM.finditer(content):
        var = m.group(1)
        if var in seen_vars:
            continue
        seen_vars.add(var)
        default, min_val, max_val, step = _parse_param_args(m.group(3))
        params.append(IndicatorParam(
            var_name=var,
            display_name=m.group(2),
            default=default,
            min_val=min_val,
            max_val=max_val,
            step=step,
            param_type="Param",
            has_guard=False,
        ))

    # Bare ParamToggle (no guard) -- only if not already captured
    for m in _RE_BARE_TOGGLE.finditer(content):
        var = m.group(1)
        if var in seen_vars:
            continue
        seen_vars.add(var)
        params.append(IndicatorParam(
            var_name=var,
            display_name=m.group(2),
            default=m.group(4),
            param_type="ParamToggle",
            toggle_options=m.group(3),
            has_guard=False,
        ))

    return params


def _parse_required_inputs(content: str) -> list:
    """Extract required inputs from INPUTS comment header."""
    inputs = []
    match = _RE_INPUTS_BLOCK.search(content)
    if not match:
        return inputs

    block = match.group(1)
    for line_match in _RE_INPUT_LINE.finditer(block):
        var_name = line_match.group(1)
        desc = line_match.group(2).strip()
        optional = "(optional)" in desc.lower()
        inputs.append(IndicatorInput(
            var_name=var_name,
            description=desc,
            optional=optional,
        ))

    return inputs


def _parse_output_vars(content: str) -> list:
    """Extract output variable names from OUTPUTS comment header."""
    outputs = []
    match = _RE_OUTPUTS_BLOCK.search(content)
    if not match:
        return outputs

    block = match.group(1)
    for line_match in _RE_OUTPUT_LINE.finditer(block):
        outputs.append(line_match.group(1))

    return outputs


def _parse_display_name(content: str, filename: str) -> str:
    """Extract display name from header comment or derive from filename."""
    match = _RE_HEADER_NAME.search(content)
    if match:
        name = match.group(1).strip()
        # Remove trailing version info like "- v2"
        name = re.sub(r'\s*[-–]\s*v\d+\s*$', '', name)
        return name

    # Derive from filename: "consolidation_zones.afl" -> "Consolidation Zones"
    stem = Path(filename).stem
    return stem.replace("_", " ").title()


def _parse_description(content: str) -> str:
    """Extract description from header comment block."""
    match = _RE_HEADER_DESC.search(content)
    if not match:
        return ""

    lines = match.group(1).strip().split("\n")
    desc_lines = []
    for line in lines:
        # Strip leading "// " and skip empty comment lines
        cleaned = re.sub(r'^\s*//\s?', '', line).strip()
        if not cleaned:
            continue
        # Stop at section headers (INPUTS, OUTPUTS, PARAMETERS, DEFINITION, etc.)
        if re.match(r'^[A-Z]{3,}', cleaned):
            break
        desc_lines.append(cleaned)

    return " ".join(desc_lines)


def _detect_indicator_type(content: str, params: list) -> tuple:
    """Detect if indicator is 'include' or 'standalone'.

    Returns (indicator_type, has_plots).
    """
    has_plots = bool(_RE_PLOT.search(content))
    has_title = bool(_RE_TITLE.search(content))
    has_guards = any(p.has_guard for p in params)

    # Standalone: has Plot() calls AND no guarded params
    if has_plots and not has_guards:
        return "standalone", has_plots

    # Include: has guards OR no Plot() calls
    return "include", has_plots


def parse_indicator_metadata(
    content: str,
    filename: str = "",
    filepath: str = "",
) -> IndicatorMeta:
    """Parse AFL content and extract all indicator metadata."""
    params = _parse_params(content)
    required_inputs = _parse_required_inputs(content)
    output_vars = _parse_output_vars(content)
    display_name = _parse_display_name(content, filename)
    description = _parse_description(content)
    indicator_type, has_plots = _detect_indicator_type(content, params)

    return IndicatorMeta(
        filename=filename,
        filepath=filepath,
        display_name=display_name,
        description=description,
        indicator_type=indicator_type,
        params=params,
        required_inputs=required_inputs,
        output_vars=output_vars,
        has_plots=has_plots,
    )


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------


def list_indicators(indicators_dir: Path = None) -> list:
    """List all .afl files in the indicators directory with parsed metadata."""
    ind_dir = indicators_dir or get_indicators_dir()
    indicators = []

    if not ind_dir.exists():
        return indicators

    for afl_file in sorted(ind_dir.glob("*.afl")):
        if not afl_file.is_file():
            continue
        try:
            content = afl_file.read_text(encoding="utf-8")
            meta = parse_indicator_metadata(
                content,
                filename=afl_file.name,
                filepath=str(afl_file),
            )
            stat = afl_file.stat()
            meta.size_kb = round(stat.st_size / 1024, 1)
            meta.modified_date = datetime.fromtimestamp(stat.st_mtime).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            indicators.append(meta)
        except Exception as exc:
            logger.warning("Failed to parse indicator %s: %s", afl_file.name, exc)

    return indicators


def read_indicator(
    filename: str,
    indicators_dir: Path = None,
) -> tuple:
    """Read an indicator file and return (content, metadata).

    Returns ("", None) if file not found.
    """
    ind_dir = indicators_dir or get_indicators_dir()
    filepath = ind_dir / Path(filename).name  # sanitize

    if not filepath.exists() or not filepath.is_file():
        return "", None

    try:
        content = filepath.read_text(encoding="utf-8")
    except Exception as exc:
        logger.error("Failed to read indicator %s: %s", filename, exc)
        return "", None

    meta = parse_indicator_metadata(content, filename=filepath.name, filepath=str(filepath))
    stat = filepath.stat()
    meta.size_kb = round(stat.st_size / 1024, 1)
    meta.modified_date = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")

    return content, meta


def save_indicator(
    filename: str,
    content: str,
    indicators_dir: Path = None,
) -> tuple:
    """Write content to an indicator file. Returns (success, message).

    Rejects path traversal attempts.
    """
    ind_dir = indicators_dir or get_indicators_dir()

    # Reject path traversal attempts
    if ".." in filename or "/" in filename or "\\" in filename:
        return False, "Invalid filename: path traversal not allowed"

    safe_name = Path(filename).name
    if not safe_name:
        return False, "Invalid filename"

    if not safe_name.endswith(".afl"):
        safe_name += ".afl"

    filepath = ind_dir / safe_name

    try:
        ind_dir.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding="utf-8")
        logger.info("Indicator saved: %s (%d chars)", filepath, len(content))
        return True, f"Saved {safe_name}"
    except Exception as exc:
        logger.error("Failed to save indicator %s: %s", safe_name, exc)
        return False, str(exc)


def delete_indicator(
    filename: str,
    indicators_dir: Path = None,
) -> tuple:
    """Delete an indicator file. Returns (success, message)."""
    ind_dir = indicators_dir or get_indicators_dir()
    safe_name = Path(filename).name
    filepath = ind_dir / safe_name

    if not filepath.exists():
        return False, f"Indicator '{safe_name}' not found"

    try:
        filepath.unlink()
        logger.info("Indicator deleted: %s", safe_name)
        return True, f"Deleted {safe_name}"
    except Exception as exc:
        logger.error("Failed to delete indicator %s: %s", safe_name, exc)
        return False, str(exc)


# ---------------------------------------------------------------------------
# Include block generation
# ---------------------------------------------------------------------------


def generate_include_block(
    indicators: list,
    indicators_dir: Path = None,
) -> str:
    """Generate AFL preamble with parameter assignments and #include_once statements.

    Parameters
    ----------
    indicators : list[dict]
        Each dict: {"filename": str, "params": {"var_name": "value", ...}}
        params includes all values to assign before the #include.
    indicators_dir : Path, optional
        Override for the indicators directory path.

    Returns
    -------
    str
        AFL code block with comments, parameter assignments, and #include_once.
    """
    if not indicators:
        return ""

    ind_dir = indicators_dir or get_indicators_dir()
    lines = ["// ---- Indicator Includes (auto-generated) ----"]

    for ind_cfg in indicators:
        filename = ind_cfg.get("filename", "")
        param_overrides = ind_cfg.get("params", {})

        filepath = ind_dir / Path(filename).name
        if not filepath.exists():
            raise FileNotFoundError(f"Indicator file not found: {filename}")

        # Read metadata to get display name
        content = filepath.read_text(encoding="utf-8")
        meta = parse_indicator_metadata(content, filename=filepath.name)

        lines.append("")
        lines.append(f"// {meta.display_name}")

        # Emit parameter assignments
        for var_name, value in param_overrides.items():
            if value != "":
                lines.append(f"{var_name} = {value};")

        # Emit #include_once with forward slashes to avoid \t, \n, etc.
        # being interpreted as escape sequences by editors/linters.
        abs_path = str(filepath.resolve()).replace("\\", "/")
        lines.append(f'#include_once "{abs_path}"')

    lines.append("")
    lines.append("// ---- End Indicator Includes ----")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Import from external locations
# ---------------------------------------------------------------------------


def import_indicators(
    source_paths: list,
    indicators_dir: Path = None,
    overwrite: bool = False,
) -> list:
    """Copy indicator files from external paths into the project's indicators/ dir.

    Parameters
    ----------
    source_paths : list[Path]
        Files or directories to import from.
    indicators_dir : Path, optional
        Override for the target directory.
    overwrite : bool
        If True, overwrite existing files. Default False (skip existing).

    Returns
    -------
    list[tuple[str, bool, str]]
        (filename, success, message) for each file processed.
    """
    ind_dir = indicators_dir or get_indicators_dir()
    ind_dir.mkdir(parents=True, exist_ok=True)
    results = []

    files_to_copy = []
    for src in source_paths:
        src = Path(src)
        if src.is_file() and src.suffix.lower() == ".afl":
            files_to_copy.append(src)
        elif src.is_dir():
            files_to_copy.extend(sorted(src.glob("*.afl")))

    for src_file in files_to_copy:
        dest = ind_dir / src_file.name
        if dest.exists() and not overwrite:
            results.append((src_file.name, False, "Already exists (skipped)"))
            continue
        try:
            shutil.copy2(src_file, dest)
            results.append((src_file.name, True, "Imported"))
            logger.info("Imported indicator: %s -> %s", src_file, dest)
        except Exception as exc:
            results.append((src_file.name, False, str(exc)))
            logger.error("Failed to import %s: %s", src_file.name, exc)

    return results
