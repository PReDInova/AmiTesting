"""
APX Builder — Generates AmiBroker Analysis Project (.apx) files.

Reads a base APX XML template, writes a uniquely-named AFL snapshot for
<FormulaPath>, populates <FormulaContent> with XML-escaped AFL, sets the
symbol, and produces the final .apx file.

FormulaContent uses AmiBroker's custom encoding (literal \\r\\n for newlines)
plus standard XML entity escaping (&lt; &gt; &amp;) so that AFL comparison
operators don't break the XML parser.  The encoded content is derived from
the snapshot file's exact bytes so that AmiBroker sees a perfect match
between FormulaContent and FormulaPath — preventing the "formula is
different" dialog that corrupts COM automation.

Uses raw string operations (not ElementTree) to preserve the exact byte
format of the template — AmiBroker is strict about XML formatting, line
endings, and quote style.
"""

import sys
import uuid
import logging
from datetime import datetime, date
from dateutil.relativedelta import relativedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import (
    APX_TEMPLATE,
    APX_OUTPUT,
    AFL_STRATEGY_FILE,
    GCZ25_SYMBOL,
    setup_logging,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------


def _compute_date_range(date_range: str, dataset_start: str, dataset_end: str) -> tuple:
    """Compute from_date and to_date from a date range code and dataset bounds.

    The date_range code can be a simple period ('1m', '3m', '6m', '1y') which
    measures back from the dataset end, or a compound code like '1m@6m' meaning
    "1 month of data starting 6 months from the dataset start".

    Parameters
    ----------
    date_range : str
        Period code.  Simple: '1m', '3m', '6m', '1y'.
        Compound: '<duration>@<offset>' e.g. '1m@6m' = 1 month starting 6
        months after dataset start.
    dataset_start : str
        First available date in the dataset (YYYY-MM-DD).
    dataset_end : str
        Last available date in the dataset (YYYY-MM-DD).

    Returns
    -------
    tuple[str, str]
        (from_date, to_date) as 'YYYY-MM-DD' strings.
    """
    ds_start = datetime.strptime(dataset_start, "%Y-%m-%d").date()
    ds_end = datetime.strptime(dataset_end, "%Y-%m-%d").date()

    period_map = {"0m": 0, "1m": 1, "3m": 3, "6m": 6, "9m": 9, "1y": 12}

    if "@" in date_range:
        # Compound: duration@offset  e.g. "1m@6m"
        duration_code, offset_code = date_range.split("@", 1)
        duration_months = period_map.get(duration_code, 1)
        offset_months = period_map.get(offset_code, 0)
        from_dt = ds_start + relativedelta(months=offset_months)
        to_dt = from_dt + relativedelta(months=duration_months)
        # Clamp to dataset bounds
        to_dt = min(to_dt, ds_end)
        from_dt = min(from_dt, ds_end)
    else:
        # Simple: measure back from dataset end
        months = period_map.get(date_range, 12)
        to_dt = ds_end
        from_dt = ds_end - relativedelta(months=months)
        # Clamp to dataset start
        from_dt = max(from_dt, ds_start)

    return from_dt.strftime("%Y-%m-%d"), to_dt.strftime("%Y-%m-%d")


def _replace_xml_tag(output: bytes, open_tag: bytes, close_tag: bytes, value: str) -> bytes:
    """Replace the content between an XML open/close tag pair in raw bytes."""
    if open_tag in output and close_tag in output:
        start = output.find(open_tag) + len(open_tag)
        end = output.find(close_tag, start)
        output = output[:start] + value.encode("iso-8859-1") + output[end:]
    return output


def build_apx(
    afl_path: str,
    output_apx_path: str,
    template_apx_path: str = None,
    run_id: str = None,
    periodicity: int = None,
    symbol: str = None,
    date_range: str = None,
    dataset_start: str = None,
    dataset_end: str = None,
    **kwargs,
) -> str:
    """Build an AmiBroker .apx project file from a template and AFL source.

    Writes a uniquely-named snapshot file for <FormulaPath> and populates
    <FormulaContent> with the AFL (read back from the snapshot's exact bytes)
    so that the embedded content matches the file byte-for-byte after
    AmiBroker unescapes XML entities and converts literal ``\\r\\n`` to CRLF.

    Parameters
    ----------
    afl_path : str
        Path to the AFL formula file.
    output_apx_path : str
        Destination path for the generated .apx file.
    template_apx_path : str, optional
        Path to the base APX XML template.
    run_id : str, optional
        Unique identifier for this run (used in snapshot filename).
    periodicity : int, optional
        Override the backtest periodicity in the APX.  AmiBroker APX values:
        0=Tick, 5=1-min, 6=5-min, 7=15-min, 9=Hourly, 11=Daily, 12=Weekly.
        When ``None`` the template value is kept unchanged.
    date_range : str, optional
        Period code for the backtest window (e.g. '1m', '3m', '6m', '1y',
        or '1m@6m' for 1 month starting 6 months into the dataset).
        Requires ``dataset_start`` and ``dataset_end``.
    dataset_start : str, optional
        First available date in the dataset (YYYY-MM-DD).
    dataset_end : str, optional
        Last available date in the dataset (YYYY-MM-DD).

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

    # Read template as raw bytes to preserve exact format (CRLF, encoding) --
    if not template_apx_path.exists():
        raise FileNotFoundError(f"APX template not found: {template_apx_path}")
    template = template_apx_path.read_bytes()

    # --- Snapshot file (the file FormulaPath points to) --------------------
    # Each run gets its own AFL file so AmiBroker never compares against a
    # cached version from a previous run.  The snapshot is written to disk
    # so AmiBroker can load the formula from FormulaPath.
    if run_id is None:
        run_id = uuid.uuid4().hex[:12]
    snapshot_name = f"strategy_{run_id}.afl"
    output_apx_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path = output_apx_path.parent / snapshot_name

    afl_crlf = afl_content.replace("\r\n", "\n").replace("\n", "\r\n")
    snapshot_path.write_bytes(afl_crlf.encode("iso-8859-1"))
    logger.info("Wrote snapshot file: %s (%d bytes)",
                snapshot_path, snapshot_path.stat().st_size)

    # --- FormulaContent -----------------------------------------------------
    # By default, populate FormulaContent with the AFL encoded in AmiBroker's
    # format (literal \r\n for newlines, XML entity escaping).  This prevents
    # the "formula is different" dialog that corrupts COM automation.
    #
    # When populate_content=False (passed via kwargs), FormulaContent is cleared
    # and AmiBroker loads from FormulaPath instead.  The DialogHandler handles
    # the resulting dialog.
    populate_content = kwargs.get("populate_content", True)
    fc_open = b"<FormulaContent>"
    fc_close = b"</FormulaContent>"
    output = template

    if fc_open in output and fc_close in output:
        fc_start = output.find(fc_open) + len(fc_open)
        fc_end = output.find(fc_close)
        if populate_content:
            # Read the snapshot back and encode for FormulaContent.
            # AmiBroker stores newlines as literal \r\n (4-char sequence)
            # and uses standard XML entity escaping.
            snap_bytes = snapshot_path.read_bytes()
            snap_text = snap_bytes.decode("iso-8859-1")
            # Convert CRLF → literal \r\n
            fc_encoded = snap_text.replace("\r\n", "\\r\\n").replace("\r", "\\r\\n").replace("\n", "\\r\\n")
            # XML-escape special characters
            fc_encoded = fc_encoded.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            output = output[:fc_start] + fc_encoded.encode("iso-8859-1") + output[fc_end:]
            logger.info("FormulaContent populated (%d encoded chars)", len(fc_encoded))
        else:
            output = output[:fc_start] + output[fc_end:]
            logger.info("FormulaContent cleared (formula loaded from FormulaPath)")

    # --- Set FormulaPath to the snapshot -----------------------------------
    # AmiBroker APX format uses double backslashes in FormulaPath.
    fp_open = b"<FormulaPath>"
    fp_close = b"</FormulaPath>"
    if fp_open in output and fp_close in output:
        snap_abs = str(snapshot_path.resolve()).replace("\\", "\\\\")
        fp_start = output.find(fp_open) + len(fp_open)
        fp_end = output.find(fp_close)
        output = output[:fp_start] + snap_abs.encode("iso-8859-1") + output[fp_end:]
        logger.info("FormulaPath set to: %s", snap_abs)

    # --- Set Periodicity (base data resolution for the backtest) -------------
    if periodicity is not None:
        per_open = b"<Periodicity>"
        per_close = b"</Periodicity>"
        if per_open in output and per_close in output:
            per_val = str(periodicity).encode("iso-8859-1")
            per_start = output.find(per_open) + len(per_open)
            per_end = output.find(per_close)
            output = output[:per_start] + per_val + output[per_end:]
            logger.info("Periodicity set to: %d", periodicity)

    # --- Set Symbol / ApplyTo mode -------------------------------------------
    if symbol == "__ALL__":
        # "All Symbols" mode: set ApplyTo=0 so AmiBroker iterates every ticker
        output = output.replace(b"<ApplyTo>1</ApplyTo>", b"<ApplyTo>0</ApplyTo>")
        logger.info("ApplyTo set to 0 (All Symbols)")
        effective_symbol = ""
    else:
        effective_symbol = symbol or GCZ25_SYMBOL

    sym_open = b"<Symbol>"
    sym_close = b"</Symbol>"
    if sym_open in output and sym_close in output:
        sym_start = output.find(sym_open) + len(sym_open)
        sym_end = output.find(sym_close)
        output = output[:sym_start] + effective_symbol.encode("iso-8859-1") + output[sym_end:]
        logger.info("Symbol set to: %s", effective_symbol or "(all symbols)")

    # --- Set Date Range (backtest window) ------------------------------------
    if date_range and dataset_start and dataset_end:
        from_date, to_date = _compute_date_range(date_range, dataset_start, dataset_end)
        from_val = f"{from_date} 00:00:00"
        to_val = to_date

        # General section dates
        output = _replace_xml_tag(output, b"<FromDate>", b"</FromDate>", from_val)
        output = _replace_xml_tag(output, b"<ToDate>", b"</ToDate>", to_val)

        # BacktestSettings range dates
        output = _replace_xml_tag(output, b"<RangeFromDate>", b"</RangeFromDate>", from_val)
        output = _replace_xml_tag(output, b"<RangeToDate>", b"</RangeToDate>", to_val)

        # BacktestSettings backtest-specific range dates
        output = _replace_xml_tag(output, b"<BacktestRangeFromDate>", b"</BacktestRangeFromDate>", from_val)
        output = _replace_xml_tag(output, b"<BacktestRangeToDate>", b"</BacktestRangeToDate>", to_val)

        logger.info("Date range set: %s to %s (code=%s)", from_date, to_date, date_range)

    # Write output as raw bytes (no text-mode conversion) -------------------
    output_apx_path.write_bytes(output)

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
