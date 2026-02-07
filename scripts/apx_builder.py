"""
APX Builder — Generates AmiBroker Analysis Project (.apx) files.

Reads a base APX XML template, injects AFL formula content into the
<FormulaContent> element, and writes the resulting .apx file to disk.
"""

import sys
import logging
import xml.etree.ElementTree as ET
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


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------


def build_apx(
    afl_path: str,
    output_apx_path: str,
    template_apx_path: str = None,
) -> str:
    """Build an AmiBroker .apx project file from a template and AFL source.

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

    # Parse the APX template -----------------------------------------------
    if not template_apx_path.exists():
        raise FileNotFoundError(f"APX template not found: {template_apx_path}")
    tree = ET.parse(template_apx_path)
    root = tree.getroot()

    # Inject AFL into <FormulaContent> -------------------------------------
    formula_content_elem = root.find(".//FormulaContent")
    if formula_content_elem is None:
        raise ValueError(
            "Template XML is missing the <FormulaContent> element."
        )
    formula_content_elem.text = afl_content
    logger.info("Injected AFL content into <FormulaContent>.")

    # Write output APX file ------------------------------------------------
    output_apx_path.parent.mkdir(parents=True, exist_ok=True)

    tree.write(
        str(output_apx_path),
        encoding="ISO-8859-1",
        xml_declaration=True,
    )
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
