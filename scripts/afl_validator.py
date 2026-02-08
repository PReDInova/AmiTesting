"""
AFL Formula Validator -- pre-flight and post-flight checks.

Pre-validation catches common AFL mistakes (missing variables, syntax issues)
*before* sending to AmiBroker, since the OLE interface does not report
formula errors -- it silently produces empty results instead.

Post-validation checks that a backtest actually produced meaningful output.
"""

import re
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

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

# Common AFL functions that indicate a valid formula
_KNOWN_FUNCTIONS = [
    "MA", "EMA", "RSI", "MACD", "ATR", "Cross", "Ref",
    "Plot", "SetPositionSize", "ExRem", "TimeFrameSet",
    "BBandTop", "BBandBot", "ADX", "StochK", "StochD",
]


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
# Helpers
# ---------------------------------------------------------------------------

def _strip_comments(afl: str) -> str:
    """Remove single-line (//) and multi-line (/* */) comments from AFL."""
    # Remove multi-line comments first
    result = re.sub(r"/\*.*?\*/", "", afl, flags=re.DOTALL)
    # Remove single-line comments
    result = re.sub(r"//[^\n]*", "", result)
    return result
