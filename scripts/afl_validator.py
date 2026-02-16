"""
AFL Formula Validator -- pre-flight and post-flight checks.

Pre-validation catches common AFL mistakes (missing variables, syntax issues)
*before* sending to AmiBroker, since the OLE interface does not report
formula errors -- it silently produces empty results instead.

Post-validation checks that a backtest actually produced meaningful output.

Known error patterns are loaded from ``afl_errors/amibroker_reserved.json``
which contains AmiBroker's reserved function names and valid constants.
"""

import json
import re
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Load AmiBroker reserved-word reference data
# ---------------------------------------------------------------------------

_RESERVED_REF_PATH = (
    Path(__file__).resolve().parent.parent / "afl_errors" / "amibroker_reserved.json"
)

_BUILTIN_FUNCTIONS: set[str] = set()
_VALID_COLORS: set[str] = set()
_INVALID_COLOR_MAP: dict[str, str] = {}
_INCLUDE_REQUIRED_VARS: dict[str, list[str]] = {}
_INCLUDE_OUTPUT_VARS: dict[str, list[str]] = {}

def _load_reserved_reference() -> None:
    """Load reserved words and valid constants from the JSON reference file."""
    global _BUILTIN_FUNCTIONS, _VALID_COLORS, _INVALID_COLOR_MAP, _INCLUDE_REQUIRED_VARS, _INCLUDE_OUTPUT_VARS
    if _BUILTIN_FUNCTIONS:
        return  # already loaded
    try:
        data = json.loads(_RESERVED_REF_PATH.read_text(encoding="utf-8"))
        _BUILTIN_FUNCTIONS = {f.lower() for f in data.get("builtin_functions", [])}
        _VALID_COLORS = {c.lower() for c in data.get("valid_colors", [])}
        _INVALID_COLOR_MAP = {
            k.lower(): v for k, v in data.get("common_invalid_colors", {}).items()
        }
        _INCLUDE_REQUIRED_VARS = {
            k.lower(): v
            for k, v in data.get("include_required_vars", {}).items()
        }
        _INCLUDE_OUTPUT_VARS = {
            k.lower(): [v.lower() for v in vals]
            for k, vals in data.get("include_output_vars", {}).items()
        }
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("Could not load reserved reference: %s", exc)


# ---------------------------------------------------------------------------
# Required trading variable patterns
# ---------------------------------------------------------------------------

# AmiBroker requires all four trading variables to be assigned.
# Error 702 occurs when Short/Cover are missing even for long-only strategies.
_REQUIRED_VARS = {
    "Buy":   re.compile(r"^\s*Buy\s*=", re.MULTILINE),
    "Sell":  re.compile(r"^\s*Sell\s*=", re.MULTILINE),
    "Short": re.compile(r"^\s*Short\s*=", re.MULTILINE),
    "Cover": re.compile(r"^\s*Cover\s*=", re.MULTILINE),
}

# Assignment pattern: identifier at start of line (or after ; ) followed by =
# Captures the variable name.  Negative lookahead excludes == comparisons.
_ASSIGNMENT_RE = re.compile(
    r"(?:^|;)\s*([A-Za-z_]\w*)\s*=(?!=)", re.MULTILINE
)

# Color identifier pattern: colorXxx used anywhere in code
_COLOR_RE = re.compile(r"\b(color[A-Za-z0-9_]+)\b", re.IGNORECASE)

# Include path pattern
_INCLUDE_RE = re.compile(
    r'#include_once\s+"([^"]+)"', re.IGNORECASE
)

# Backslash sequences that can be interpreted as escape characters
_DANGEROUS_ESCAPES = re.compile(r"\\[tnrabfv]")

# Built-in variables that are meant to be assigned to — do not flag these.
# Buy/Sell/Short/Cover are trading signals; Title sets the chart title bar.
_ASSIGNABLE_BUILTINS = {"buy", "sell", "short", "cover", "title"}


def validate_afl(afl_content: str) -> tuple[bool, list[str]]:
    """Check AFL source code for common errors.

    Parameters
    ----------
    afl_content : str
        The raw AFL formula text.

    Returns
    -------
    tuple of (bool, list[str])
        ``(True, [])`` when all checks pass.
        ``(False, ["error 1", "error 2", ...])`` when problems are found.
    """
    _load_reserved_reference()
    errors = []

    # Strip comments for analysis (keep original for variable checks)
    stripped = _strip_comments(afl_content)

    # --- Check: non-empty ---
    if not stripped.strip():
        return (False, ["AFL formula is empty."])

    # --- Check: required trading variables ---
    for var_name, pattern in _REQUIRED_VARS.items():
        if not pattern.search(afl_content):
            errors.append(
                f"Missing required variable assignment: {var_name}. "
                f"AmiBroker Error 702 will occur. "
                f"Add '{var_name} = 0;' for unused directions."
            )

    # --- Check: at least one semicolon (basic AFL syntax) ---
    if ";" not in stripped:
        errors.append(
            "No semicolons found. AFL statements must end with ';'."
        )

    # --- Check: unmatched parentheses ---
    open_count = stripped.count("(")
    close_count = stripped.count(")")
    if open_count != close_count:
        errors.append(
            f"Unmatched parentheses: {open_count} opening vs "
            f"{close_count} closing."
        )

    # --- Check: encoding safety (ISO-8859-1 compatibility) ---
    try:
        afl_content.encode("iso-8859-1")
    except UnicodeEncodeError as e:
        errors.append(
            f"AFL contains characters not supported by AmiBroker's "
            f"ISO-8859-1 encoding: {e}"
        )

    # --- Check: reserved function names used as variables (ERR-001) ---
    errors.extend(_check_reserved_variable_names(stripped))

    # --- Check: invalid color constants (ERR-002) ---
    errors.extend(_check_invalid_colors(stripped))

    # --- Check: dangerous #include_once paths (ERR-004) ---
    errors.extend(_check_include_paths(afl_content))

    # --- Check: uninitialized include input variables (ERR-007) ---
    errors.extend(_check_include_required_vars(afl_content))

    if errors:
        return (False, errors)
    return (True, [])


def validate_afl_file(afl_path: str) -> tuple[bool, list[str]]:
    """Validate an AFL file on disk.  Convenience wrapper around validate_afl."""
    path = Path(afl_path)
    if not path.exists():
        return (False, [f"AFL file not found: {afl_path}"])
    content = path.read_text(encoding="utf-8")
    return validate_afl(content)


# ---------------------------------------------------------------------------
# Post-backtest validation
# ---------------------------------------------------------------------------

def validate_backtest_results(
    csv_path: str,
    html_path: str = None,
) -> tuple[bool, list[str]]:
    """Check that a backtest produced meaningful results.

    Parameters
    ----------
    csv_path : str
        Path to the exported CSV results file.
    html_path : str, optional
        Path to the exported HTML results file.

    Returns
    -------
    tuple of (bool, list[str])
        ``(True, [])`` when results look valid.
        ``(False, ["warning 1", ...])`` when problems are detected.
    """
    warnings = []

    csv_file = Path(csv_path)
    if not csv_file.exists():
        return (False, [f"Results CSV not found: {csv_path}. "
                        "The backtest may have failed silently."])

    size = csv_file.stat().st_size
    if size == 0:
        return (False, ["Results CSV is empty (0 bytes). "
                        "The AFL formula may have errors that AmiBroker "
                        "did not report through OLE."])

    # Read and check for actual trade data
    content = csv_file.read_text(encoding="utf-8", errors="replace")
    lines = [l for l in content.strip().splitlines() if l.strip()]

    if len(lines) <= 1:
        warnings.append(
            "Results CSV has only a header row (0 trades). "
            "The Buy/Sell conditions may never have triggered, or the "
            "AFL formula may contain errors."
        )

    if html_path:
        html_file = Path(html_path)
        if not html_file.exists():
            warnings.append(f"Results HTML not found: {html_path}")
        elif html_file.stat().st_size == 0:
            warnings.append("Results HTML is empty (0 bytes).")

    if warnings:
        return (False, warnings)
    return (True, [])


# ---------------------------------------------------------------------------
# Known-error checks (loaded from afl_errors/ knowledge base)
# ---------------------------------------------------------------------------

def _check_reserved_variable_names(stripped: str) -> list[str]:
    """ERR-001: Flag assignments where LHS is a reserved function name.

    AFL is case-insensitive, so ``tema = 3 * ema1`` collides with the
    built-in ``TEMA()`` function and produces Error 31.
    """
    if not _BUILTIN_FUNCTIONS:
        return []
    errors = []
    seen = set()
    for m in _ASSIGNMENT_RE.finditer(stripped):
        var_name = m.group(1)
        var_lower = var_name.lower()
        if var_lower in _ASSIGNABLE_BUILTINS:
            continue
        if var_lower in _BUILTIN_FUNCTIONS and var_lower not in seen:
            seen.add(var_lower)
            errors.append(
                f"Reserved function name '{var_name}' used as variable "
                f"(AmiBroker Error 31). AFL is case-insensitive — "
                f"'{var_name}' collides with the built-in {var_name.upper()}() "
                f"function. Rename to '{var_name}Val' or similar."
            )
    return errors


def _check_invalid_colors(stripped: str) -> list[str]:
    """ERR-002: Flag color constants not in AmiBroker's valid set.

    Using an invalid color like ``colorCyan`` causes Error 29
    (variable used without being initialized).

    Skips color-prefixed built-in *functions* such as ``ColorBlend()``,
    ``ColorRGB()``, ``ColorHSB()`` — these are function calls, not
    color constants, and are identified by a trailing ``(``.
    """
    if not _VALID_COLORS:
        return []
    errors = []
    seen = set()
    for m in _COLOR_RE.finditer(stripped):
        color = m.group(1)
        color_lower = color.lower()
        if color_lower in _VALID_COLORS:
            continue
        if color_lower in seen:
            continue
        # Skip color-prefixed built-in functions (e.g. ColorBlend, ColorRGB)
        end_pos = m.end()
        rest = stripped[end_pos:].lstrip()
        if rest.startswith("("):
            continue
        if color_lower in _BUILTIN_FUNCTIONS:
            continue
        seen.add(color_lower)
        suggestion = _INVALID_COLOR_MAP.get(color_lower)
        msg = (
            f"Invalid color constant '{color}' "
            f"(AmiBroker Error 29 — uninitialized variable)."
        )
        if suggestion:
            msg += f" Use '{suggestion}' instead."
        errors.append(msg)
    return errors


def _check_include_required_vars(afl_content: str) -> list[str]:
    """ERR-007: Flag #include_once directives where required input vars are missing.

    Indicator include files expect certain variables to be set by the caller
    *before* the ``#include_once`` line.  If a required variable is never
    assigned before the include, AmiBroker will throw Error 29 at runtime
    (even if the include file has an ``IsEmpty()`` guard — that guard itself
    triggers the error on an undeclared variable).

    Also tracks output variables produced by each include, so chained
    dependencies work (e.g. tema.afl produces ``temas`` which
    derivative_lookback.afl requires).
    """
    if not _INCLUDE_REQUIRED_VARS:
        return []

    errors = []
    # Track variables provided by preceding #include_once directives
    provided_by_includes: set[str] = set()
    # Split into lines for positional analysis
    lines = afl_content.splitlines()

    for line_idx, line in enumerate(lines):
        inc_match = _INCLUDE_RE.search(line)
        if not inc_match:
            continue

        # Extract filename from the include path (may be an absolute path)
        inc_path = inc_match.group(1)
        # Normalise: take the last path component
        inc_filename = inc_path.replace("\\", "/").rsplit("/", 1)[-1].lower()

        required_vars = _INCLUDE_REQUIRED_VARS.get(inc_filename)
        if required_vars:
            # Scan all lines BEFORE this include for assignments to required vars
            preceding_text = "\n".join(lines[:line_idx])
            # Strip comments from preceding text so we don't match assignments
            # inside comments
            preceding_stripped = _strip_comments(preceding_text)

            for var in required_vars:
                # Skip if a preceding include provides this variable
                if var.lower() in provided_by_includes:
                    continue
                # Check if var is assigned (case-insensitive): var = ...
                assign_pattern = re.compile(
                    r"(?:^|;)\s*" + re.escape(var) + r"\s*=(?!=)",
                    re.MULTILINE | re.IGNORECASE,
                )
                if not assign_pattern.search(preceding_stripped):
                    errors.append(
                        f"Required variable '{var}' is not assigned before "
                        f"#include_once \"{inc_filename}\" (line {line_idx + 1}). "
                        f"AmiBroker Error 29 will occur. "
                        f"Add '{var} = Close;' (or appropriate value) before the include."
                    )

        # Register output variables produced by this include
        outputs = _INCLUDE_OUTPUT_VARS.get(inc_filename, [])
        provided_by_includes.update(outputs)

    return errors


def _check_include_paths(afl_content: str) -> list[str]:
    """ERR-004: Flag #include_once paths with dangerous backslash escapes.

    Linters or pre-commit hooks may interpret ``\\t`` as a TAB character,
    corrupting the path.  Recommend forward slashes instead.
    """
    errors = []
    for m in _INCLUDE_RE.finditer(afl_content):
        path = m.group(1)
        bad_escapes = _DANGEROUS_ESCAPES.findall(path)
        if bad_escapes:
            errors.append(
                f"#include_once path '{path}' contains backslash "
                f"sequences {bad_escapes} that may be interpreted as "
                f"escape characters. Use forward slashes instead."
            )
    return errors


# ---------------------------------------------------------------------------
# Auto-fix: correct known AFL errors in-place
# ---------------------------------------------------------------------------

def auto_fix_afl(afl_content: str) -> tuple[str, list[str]]:
    """Attempt to auto-correct known AFL errors.

    Parameters
    ----------
    afl_content : str
        The raw AFL formula text.

    Returns
    -------
    tuple of (str, list[str])
        The corrected AFL content and a list of changes that were made.
        If no changes were needed, returns the original content and an
        empty list.
    """
    _load_reserved_reference()
    fixed = afl_content
    changes: list[str] = []

    stripped = _strip_comments(fixed)

    # --- Fix: reserved function names used as variables (ERR-001) ---
    if _BUILTIN_FUNCTIONS:
        for m in _ASSIGNMENT_RE.finditer(stripped):
            var_name = m.group(1)
            var_lower = var_name.lower()
            if var_lower in _ASSIGNABLE_BUILTINS:
                continue
            if var_lower in _BUILTIN_FUNCTIONS:
                new_name = var_name + "Val"
                # Use word-boundary replacement to rename all occurrences
                # but preserve case of the original usage
                pattern = re.compile(
                    r"\b" + re.escape(var_name) + r"\b"
                    r"(?!\s*\()"  # negative lookahead: don't rename function calls
                )
                fixed = pattern.sub(new_name, fixed)
                changes.append(
                    f"Renamed variable '{var_name}' -> '{new_name}' "
                    f"(collides with built-in {var_name.upper()}() function)."
                )

    # --- Fix: invalid color constants (ERR-002) ---
    if _VALID_COLORS:
        stripped_fixed = _strip_comments(fixed)
        for m in _COLOR_RE.finditer(stripped_fixed):
            color = m.group(1)
            color_lower = color.lower()
            if color_lower in _VALID_COLORS:
                continue
            # Skip color-prefixed functions (ColorBlend, ColorRGB, etc.)
            end_pos = m.end()
            rest = stripped_fixed[end_pos:].lstrip()
            if rest.startswith("(") or color_lower in _BUILTIN_FUNCTIONS:
                continue
            replacement = _INVALID_COLOR_MAP.get(color_lower)
            if replacement:
                fixed = re.sub(
                    r"\b" + re.escape(color) + r"\b",
                    replacement,
                    fixed,
                )
                changes.append(
                    f"Replaced invalid color '{color}' -> '{replacement}'."
                )

    # --- Fix: backslash paths in #include_once (ERR-004) ---
    for m in _INCLUDE_RE.finditer(fixed):
        path = m.group(1)
        if _DANGEROUS_ESCAPES.search(path):
            new_path = path.replace("\\", "/")
            fixed = fixed.replace(
                f'#include_once "{path}"',
                f'#include_once "{new_path}"',
            )
            changes.append(
                f"Replaced backslashes with forward slashes in "
                f"#include_once path."
            )

    return (fixed, changes)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_comments(afl: str) -> str:
    """Remove single-line (//) and multi-line (/* */) comments from AFL."""
    # Remove multi-line comments first
    result = re.sub(r"/\*.*?\*/", "", afl, flags=re.DOTALL)
    # Remove single-line comments
    result = re.sub(r"//[^\n]*", "", result)
    return result
