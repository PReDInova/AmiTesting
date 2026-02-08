"""
APX Builder — Generates AmiBroker Analysis Project (.apx) files.

Reads a base APX XML template, injects AFL formula content into the
<FormulaContent> element via string substitution, and writes the resulting
.apx file to disk.  Uses raw string operations (not ElementTree) to preserve
the exact byte format of the template — AmiBroker is strict about XML
formatting, line endings, and quote style.
"""

import re
import sys
import logging
from pathlib import Path

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import (
    APX_TEMPLATE,
    APX_OUTPUT,
    AFL_STRATEGY_FILE,
    setup_logging,
)

logger = logging.getLogger(__name__)

# Regex to match <FormulaContent>...</FormulaContent> (including empty)
_FORMULA_RE = re.compile(
    r"(<FormulaContent>)(.*?)(</FormulaContent>)",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------


def build_apx(
    afl_path: str,
    output_apx_path: str,
    template_apx_path: str = None,
) -> str:
    """Build an AmiBroker .apx project file from a template and AFL source.

    Uses raw string substitution to preserve the template's exact byte
    format (line endings, encoding, XML declaration).  This avoids the
    format corruption that Python's ElementTree introduces on Windows
    (single-quoted declarations, self-closing tags, CRLF normalisation).

    Parameters
    ----------
    afl_path : str
        Path to the AFL formula file whose content will be embedded in the
        ``<FormulaContent>`` element of the APX XML.
    output_apx_path : str
        Destination path for the generated .apx file.
    template_apx_path : str, optional
        Path to the base APX XML template.  When *None*, the default
        ``APX_TEMPLATE`` from ``config.settings`` is used.

    Returns
    -------
    str
        The *output_apx_path* that was written to disk.
    """

    # Resolve paths --------------------------------------------------------
    afl_path = Path(afl_path)
    output_apx_path = Path(output_apx_path)
    template_apx_path = Path(template_apx_path) if template_apx_path else APX_TEMPLATE

    logger.info("AFL source      : %s", afl_path)
    logger.info("APX template    : %s", template_apx_path)
    logger.info("APX output      : %s", output_apx_path)

    # Read AFL content -----------------------------------------------------
    if not afl_path.exists():
        raise FileNotFoundError(f"AFL file not found: {afl_path}")
    afl_content = afl_path.read_text(encoding="utf-8")
    logger.info("Read %d characters from AFL file.", len(afl_content))

    # Read the template as raw bytes to preserve exact format ---------------
    if not template_apx_path.exists():
        raise FileNotFoundError(f"APX template not found: {template_apx_path}")
    template_raw = template_apx_path.read_text(encoding="iso-8859-1")

    # Verify the template has a <FormulaContent> element --------------------
    if not _FORMULA_RE.search(template_raw):
        raise ValueError(
            "Template XML is missing the <FormulaContent> element."
        )

    # Inject AFL into <FormulaContent> via string substitution.
    # AmiBroker expects raw AFL text (not XML-escaped) inside this element.
    output_content = _FORMULA_RE.sub(
        rf"\g<1>{afl_content}\g<3>",
        template_raw,
    )
    logger.info("Injected AFL content into <FormulaContent>.")

    # Write output APX file preserving the template's encoding --------------
    output_apx_path.parent.mkdir(parents=True, exist_ok=True)
    output_apx_path.write_text(output_content, encoding="iso-8859-1")

    logger.info("APX file written: %s", output_apx_path)

    return str(output_apx_path)


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    setup_logging()
    logger.info("=== APX Builder start ===")

    result = build_apx(
        afl_path=str(AFL_STRATEGY_FILE),
        output_apx_path=str(APX_OUTPUT),
        template_apx_path=str(APX_TEMPLATE),
    )

    logger.info("=== APX Builder complete -> %s ===", result)
