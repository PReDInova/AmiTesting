"""
Generate the exact same APX that analyze_bar would create, then
compare FormulaContent vs snapshot file byte-by-byte to find the mismatch.
"""
import sys
import uuid
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import APX_DIR, APX_TEMPLATE
from scripts.strategy_db import get_strategy, get_latest_version
from scripts.apx_builder import build_apx
from scripts.ole_bar_analyzer import (
    _generate_exploration_afl,
    _replace_params_with_values,
    _unix_to_datenum_timenum,
    _extract_variables_from_conditions,
)
from scripts.signal_evaluator import (
    _strip_comments,
    _extract_var_defs,
    _resolve_signal_expr,
    _split_and,
)


def main():
    strategy_id = "c0e490d0-590e-4e14-8e51-84df2268e235"
    bar_time = 1762907222
    params = {"StdDev Multiplier": 1.0, "TEMA Length": 21.0, "Min Fade Move USD": 3.0}

    # --- Load AFL (same as API endpoint) ---
    version = get_latest_version(strategy_id)
    afl_content = version.get("afl_content", "")
    print(f"AFL: {len(afl_content)} chars")

    # --- Parse (same as analyze_bar) ---
    stripped = _strip_comments(afl_content)
    var_defs = _extract_var_defs(stripped)
    buy_expr = _resolve_signal_expr(stripped, "Buy", var_defs)
    short_expr = _resolve_signal_expr(stripped, "Short", var_defs)
    buy_conditions = _split_and(buy_expr) if buy_expr else []
    short_conditions = _split_and(short_expr) if short_expr else []
    all_conditions = buy_conditions + short_conditions
    key_variables = _extract_variables_from_conditions(all_conditions)

    datenum, timenum = _unix_to_datenum_timenum(bar_time)

    # --- Generate exploration AFL (same as analyze_bar) ---
    explore_afl = _generate_exploration_afl(
        afl_content, buy_conditions, short_conditions,
        key_variables, datenum, timenum, params,
        buy_expr, short_expr,
    )
    print(f"Exploration AFL: {len(explore_afl)} chars")

    # --- Write files (same as analyze_bar) ---
    run_uuid = uuid.uuid4().hex[:12]
    temp_afl_path = APX_DIR / f"strategy_{run_uuid}.afl"
    temp_apx_path = APX_DIR / f"explore_analyze_{run_uuid}.apx"

    APX_DIR.mkdir(parents=True, exist_ok=True)
    afl_crlf = explore_afl.replace("\r\n", "\n").replace("\n", "\r\n")
    temp_afl_path.write_bytes(afl_crlf.encode("iso-8859-1"))
    print(f"Wrote AFL: {temp_afl_path} ({temp_afl_path.stat().st_size} bytes)")

    # Save the AFL bytes BEFORE build_apx reads/overwrites it
    original_afl_bytes = temp_afl_path.read_bytes()

    # --- Build APX (same as analyze_bar) ---
    periodicity = 0 if "TimeFrameSet(" in explore_afl else None
    build_apx(
        afl_path=str(temp_afl_path),
        output_apx_path=str(temp_apx_path),
        template_apx_path=str(APX_TEMPLATE),
        run_id=run_uuid,
        periodicity=periodicity,
    )
    print(f"Built APX: {temp_apx_path}")

    # --- Now compare ---
    # Read the snapshot file (which build_apx wrote)
    snap_bytes = temp_afl_path.read_bytes()
    print(f"\nSnapshot file: {len(snap_bytes)} bytes")
    print(f"Original AFL:  {len(original_afl_bytes)} bytes")
    print(f"Original == Snapshot: {original_afl_bytes == snap_bytes}")

    # Read the APX and extract FormulaContent
    apx_data = temp_apx_path.read_bytes()
    fc_open = b"<FormulaContent>"
    fc_close = b"</FormulaContent>"

    if fc_open not in apx_data:
        print("\nERROR: No <FormulaContent> in APX!")
        return

    fc_start = apx_data.find(fc_open) + len(fc_open)
    fc_end = apx_data.find(fc_close)
    fc_bytes = apx_data[fc_start:fc_end]
    print(f"FormulaContent: {len(fc_bytes)} bytes in APX")

    # Simulate AmiBroker decoding:
    # 1) XML unescape
    decoded = fc_bytes
    decoded = decoded.replace(b"&amp;", b"&")
    decoded = decoded.replace(b"&lt;", b"<")
    decoded = decoded.replace(b"&gt;", b">")
    # 2) literal \r\n -> CRLF
    decoded = decoded.replace(b"\\r\\n", b"\r\n")

    print(f"FC decoded: {len(decoded)} bytes")
    print(f"\nSnapshot == FC decoded: {snap_bytes == decoded}")

    if snap_bytes != decoded:
        for i in range(min(len(snap_bytes), len(decoded))):
            if snap_bytes[i] != decoded[i]:
                print(f"\nFIRST DIFF at byte {i}:")
                print(f"  snap byte: 0x{snap_bytes[i]:02X} ({chr(snap_bytes[i]) if 32 <= snap_bytes[i] < 127 else '?'})")
                print(f"  fc byte:   0x{decoded[i]:02X} ({chr(decoded[i]) if 32 <= decoded[i] < 127 else '?'})")
                ctx = 40
                print(f"  snap context: {snap_bytes[max(0,i-ctx):i+ctx]}")
                print(f"  fc context:   {decoded[max(0,i-ctx):i+ctx]}")
                break
        if len(snap_bytes) != len(decoded):
            print(f"\nSize difference: snap={len(snap_bytes)}, fc decoded={len(decoded)}")
    else:
        print("\nBYTE-FOR-BYTE MATCH! FormulaContent should match file.")
        print("The mismatch dialog must be caused by something else.")

    # Also check: does FormulaPath match the snapshot location?
    fp_open = b"<FormulaPath>"
    fp_close = b"</FormulaPath>"
    fp_start = apx_data.find(fp_open) + len(fp_open)
    fp_end = apx_data.find(fp_close)
    fp_value = apx_data[fp_start:fp_end].decode("iso-8859-1")
    print(f"\nFormulaPath in APX: {fp_value}")
    # Normalize for comparison
    fp_normalized = fp_value.replace("\\\\", "\\")
    snap_actual = str(temp_afl_path.resolve())
    print(f"Actual snap path:   {snap_actual}")
    print(f"Path match: {fp_normalized == snap_actual}")

    # Don't clean up — leave files for manual inspection
    print(f"\nFiles preserved for inspection:")
    print(f"  APX: {temp_apx_path}")
    print(f"  AFL: {temp_afl_path}")


if __name__ == "__main__":
    main()
