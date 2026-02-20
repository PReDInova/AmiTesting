"""
Periodic signal scanner using AmiBroker OLE Exploration.

Runs a lightweight Exploration AFL against the latest bars in AmiBroker
to detect Buy/Short signals. Reuses exploration patterns from
ole_bar_analyzer.py.
"""

import csv
import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import pythoncom
import win32com.client

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import (
    AMIBROKER_EXE,
    AMIBROKER_DB_PATH,
    APX_DIR,
    APX_TEMPLATE,
)
from scripts.apx_builder import build_apx
from scripts.ole_bar_analyzer import (
    _strip_trading_directives,
    _replace_params_with_values,
    _expand_includes,
    _process_isempty_guards,
    _DialogAutoDismisser,
)

logger = logging.getLogger(__name__)

EXPLORATION_RUN_MODE = 1
EXPLORATION_POLL_INTERVAL = 0.3
EXPLORATION_MAX_WAIT = 30


@dataclass
class Signal:
    """A detected trading signal."""
    signal_type: str          # "Buy", "Sell", "Short", or "Cover"
    symbol: str
    timestamp: datetime       # Bar datetime where signal fired
    close_price: float
    strategy_name: str
    indicator_values: dict


def parse_afl_params(afl_content: str) -> dict[str, dict]:
    """Parse Param() calls from AFL content.

    Returns dict mapping variable name to param info:
    {
        "adxThreshold": {
            "label": "ADX Threshold",
            "default": 25.0,
            "min": 10.0,
            "max": 40.0,
            "step": 1.0,
        }
    }
    """
    pattern = re.compile(
        r'(\w+)\s*=\s*Param\(\s*"([^"]+)"\s*,\s*'
        r'([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*'
        r'(?:,\s*([\d.]+))?\s*\)',
        re.MULTILINE,
    )
    params = {}
    for m in pattern.finditer(afl_content):
        varname = m.group(1)
        params[varname] = {
            "label": m.group(2),
            "default": float(m.group(3)),
            "min": float(m.group(4)),
            "max": float(m.group(5)),
            "step": float(m.group(6)) if m.group(6) else 1.0,
        }
    return params


def parse_afl_conditions(afl_content: str) -> list[dict]:
    """Parse Buy/Short conditions from AFL to extract threshold comparisons.

    Looks for patterns like:
        Buy = expr1 AND expr2 AND ...
        Short = expr1 AND expr2 AND ...

    Where each expr might be a comparison like ``ADXvalue > adxThreshold``.

    Returns a list of condition dicts:
    [
        {
            "signal": "Buy",
            "lhs": "ADXvalue",
            "operator": ">",
            "rhs": "adxThreshold",
            "description": "ADXvalue > adxThreshold",
        }
    ]
    """
    conditions = []

    # Find Buy/Short assignment lines (handle multiline with AND)
    for signal_name in ("Buy", "Short"):
        # Match single-line and continuation patterns
        pattern = re.compile(
            rf'^\s*{signal_name}\s*=\s*(.+?);\s*$',
            re.MULTILINE,
        )
        for m in pattern.finditer(afl_content):
            expr = m.group(1).strip()
            # Split on AND
            parts = re.split(r'\bAND\b', expr, flags=re.IGNORECASE)
            for part in parts:
                part = part.strip()
                # Match comparisons: var > val, var < val, var >= val, etc.
                comp = re.match(
                    r'^(\w+)\s*(>=|<=|>|<|==)\s*(\w+(?:\.\d+)?)$',
                    part,
                )
                if comp:
                    conditions.append({
                        "signal": signal_name,
                        "lhs": comp.group(1),
                        "operator": comp.group(2),
                        "rhs": comp.group(3),
                        "description": part,
                    })
    return conditions


def compute_proximity(
    conditions: list[dict],
    params: dict[str, dict],
    indicator_values: dict[str, float],
) -> list[dict]:
    """Compute how close current indicator values are to triggering conditions.

    For each condition like ``ADXvalue > 25``, computes:
    - current value of the LHS indicator
    - threshold value (from params or literal)
    - proximity percentage (how close to triggering)

    Returns list of proximity dicts sorted by signal type.
    """
    results = []

    for cond in conditions:
        lhs_name = cond["lhs"]
        rhs_name = cond["rhs"]
        operator = cond["operator"]

        # Get current value from indicators
        current = indicator_values.get(lhs_name)
        if current is None:
            continue

        # Get threshold: try as param default, then as literal float
        threshold = None
        if rhs_name in params:
            threshold = params[rhs_name]["default"]
        else:
            try:
                threshold = float(rhs_name)
            except ValueError:
                # RHS is another variable — check indicators
                threshold = indicator_values.get(rhs_name)

        if threshold is None:
            continue

        # Compute proximity based on operator
        if operator in (">", ">="):
            # Current needs to be above threshold
            if threshold != 0:
                proximity_pct = min(100.0, max(0.0, (current / threshold) * 100))
            else:
                proximity_pct = 100.0 if current > 0 else 0.0
            met = current > threshold if operator == ">" else current >= threshold
            direction = "above"
        elif operator in ("<", "<="):
            # Current needs to be below threshold
            if threshold != 0:
                proximity_pct = min(100.0, max(0.0, (1 - (current - threshold) / abs(threshold)) * 100))
            else:
                proximity_pct = 100.0 if current < 0 else 0.0
            met = current < threshold if operator == "<" else current <= threshold
            direction = "below"
        else:
            proximity_pct = 100.0 if current == threshold else 0.0
            met = current == threshold
            direction = "equal"

        results.append({
            "signal": cond["signal"],
            "condition": cond["description"],
            "indicator": lhs_name,
            "current_value": round(current, 4),
            "threshold": round(threshold, 4),
            "operator": operator,
            "direction": direction,
            "proximity_pct": round(min(proximity_pct, 100.0), 1),
            "met": met,
        })

    return results


class SignalScanner:
    """Scans for Buy/Short signals by running AmiBroker Exploration.

    Uses an existing COM connection (shares the QuoteInjector's COM
    apartment on the main thread).

    Parameters
    ----------
    ab : COM object
        An already-connected AmiBroker COM Application object.
    strategy_afl_path : str
        Path to the strategy AFL file to scan.
    symbol : str
        Target symbol to scan.
    lookback_bars : int
        Number of recent bars to check for new signals.
    periodicity : int or None
        AmiBroker periodicity for the exploration APX.
        0=Tick, 5=1-min, 6=5-min, 7=15-min, 9=Hourly, 11=Daily.
        When None, the template default is used.
    """

    # Map (ProjectX unit, interval) to AmiBroker periodicity value.
    # Unit: 1=Second, 2=Minute, 3=Hour, 4=Day
    AMI_PERIODICITY = {
        (2, 1): 5,     # 1 Minute
        (2, 5): 6,     # 5 Minutes
        (2, 15): 7,    # 15 Minutes
        (3, 1): 9,     # 1 Hour (Hourly)
        (4, 1): 11,    # Daily
    }

    def __init__(
        self,
        ab,
        strategy_afl_path: str,
        symbol: str = "NQ",
        lookback_bars: int = 5,
        periodicity: int = None,
    ):
        self.ab = ab
        self.strategy_afl_path = strategy_afl_path
        self.symbol = symbol
        self.lookback_bars = lookback_bars
        self.periodicity = periodicity
        self._alerted_signals: set[str] = set()  # dedup by "type_timestamp"

        # Indicator values from the most recent scan
        self.latest_indicators: dict[str, float] = {}
        self.latest_indicator_time: str | None = None

        # Parsed AFL params and conditions (for proximity calculations)
        afl_content = Path(strategy_afl_path).read_text(encoding="utf-8")
        self.parsed_params: dict[str, dict] = parse_afl_params(afl_content)
        self.parsed_conditions: list[dict] = parse_afl_conditions(afl_content)

        # --- Cached exploration state (reduce per-scan overhead) ---
        self._last_afl_hash: str | None = None   # SHA-256 of generated AFL
        self._cached_afl_path: Path | None = None
        self._cached_apx_path: Path | None = None
        self._cached_csv_path: Path | None = None
        self._cached_doc = None                   # open AnalysisDoc COM ref

    def scan(self) -> list[Signal]:
        """Run one exploration scan and return detected signals.

        Steps:
        1. Generate the exploration AFL from the strategy AFL.
        2. Hash the AFL content; only regenerate APX when the hash changes.
        3. Open APX in AmiBroker (or reuse cached doc), run Exploration.
        4. Poll IsBusy until complete.
        5. Export CSV, parse for Buy/Short signals.
        6. Return only NEW signals (not previously alerted).

        AFL/APX files and the AnalysisDoc are cached between scans.
        Call ``close()`` to release resources when done.
        """
        # Reset indicator state for this scan
        self.latest_indicators = {}
        self.latest_indicator_time = None

        try:
            # 1. Generate exploration AFL
            scan_afl = self._generate_scan_afl()
            afl_hash = hashlib.sha256(scan_afl.encode("utf-8")).hexdigest()

            # 2. Check if AFL has changed since last scan
            if afl_hash != self._last_afl_hash:
                logger.debug("AFL content changed (hash=%s…); regenerating "
                             "APX files.", afl_hash[:12])
                # Close stale cached doc before rebuilding
                self._close_cached_doc()
                self._cleanup_cached_files()

                scan_id = uuid.uuid4().hex[:8]
                self._cached_afl_path = APX_DIR / f"live_scan_{scan_id}.afl"
                self._cached_apx_path = APX_DIR / f"live_scan_{scan_id}.apx"
                self._cached_csv_path = APX_DIR / f"live_scan_{scan_id}.csv"

                self._cached_afl_path.write_text(scan_afl, encoding="utf-8")

                # Build APX
                #   - populate_content=True (default) prevents "formula is
                #     different" dialog
                #   - symbol="__ALL__" forces ApplyTo=0 so the exploration
                #     scans all symbols; the AFL's Name() filter restricts
                #     output to the target symbol.
                #   - periodicity must match bar interval
                build_apx(
                    afl_path=str(self._cached_afl_path),
                    output_apx_path=str(self._cached_apx_path),
                    template_apx_path=str(APX_TEMPLATE),
                    run_id=f"live_scan_{scan_id}",
                    symbol="__ALL__",
                    periodicity=self.periodicity,
                )

                self._last_afl_hash = afl_hash
            else:
                logger.debug("AFL unchanged (hash=%s…); reusing cached APX.",
                             afl_hash[:12])

            # 3. Run exploration via OLE (reuses cached doc when possible)
            all_signals = self._run_exploration(
                str(self._cached_apx_path), str(self._cached_csv_path))

            # 4. Filter to only NEW signals
            new_signals = []
            for sig in all_signals:
                key = f"{sig.signal_type}_{sig.timestamp.isoformat()}"
                if key not in self._alerted_signals:
                    self._alerted_signals.add(key)
                    new_signals.append(sig)

            return new_signals

        except Exception as exc:
            logger.error("Signal scan failed: %s", exc, exc_info=True)
            # On error, invalidate cache so next scan starts fresh
            self._close_cached_doc()
            self._cleanup_cached_files()
            self._last_afl_hash = None
            return []

    def close(self):
        """Release cached COM doc and clean up temp files.

        Call this when the scanner is no longer needed (e.g. at shutdown).
        """
        self._close_cached_doc()
        self._cleanup_cached_files()
        self._last_afl_hash = None

    def get_proximity(self) -> list[dict]:
        """Compute proximity-to-signal for all parsed conditions.

        Uses the latest indicator values from the most recent scan
        and the parsed Param()/condition info from the AFL source.

        Returns a list of proximity dicts (see ``compute_proximity``).
        """
        return compute_proximity(
            self.parsed_conditions,
            self.parsed_params,
            self.latest_indicators,
        )

    def _close_cached_doc(self):
        """Close the cached AnalysisDoc COM reference if open."""
        if self._cached_doc is not None:
            try:
                self._cached_doc.Close()
                logger.debug("Closed cached AnalysisDoc.")
            except Exception:
                pass
            self._cached_doc = None

    def _cleanup_cached_files(self):
        """Remove cached AFL/APX/CSV files from disk."""
        for attr in ("_cached_afl_path", "_cached_apx_path",
                      "_cached_csv_path"):
            p = getattr(self, attr, None)
            if p is not None:
                try:
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass
                setattr(self, attr, None)
        # Clean up strategy snapshot AFL files generated by build_apx
        # (build_apx creates a "strategy_live_scan_<id>.afl" alongside the APX)
        for p in APX_DIR.glob("strategy_live_scan_*.afl"):
            try:
                p.unlink()
            except Exception:
                pass

    def _generate_scan_afl(self) -> str:
        """Generate exploration AFL from the strategy AFL.

        Adapts the strategy by:
        - Expanding #include_once directives
        - Processing IsEmpty guards
        - Stripping Plot/ApplyStop/SetPositionSize/SetTradeDelays
        - Adding a Filter for recent bars with signal
        - Adding AddColumn for Buy, Short, Close, and key indicators
        """
        afl_path = Path(self.strategy_afl_path)
        afl_content = afl_path.read_text(encoding="utf-8")

        # Expand includes so AFL is self-contained
        afl = _expand_includes(afl_content)

        # Process IsEmpty guards
        afl = _process_isempty_guards(afl)

        # Replace Param() with default values (use defaults since we
        # don't have slider values in live mode)
        afl = _replace_params_with_values(afl, {})

        # Strip trading directives
        afl = _strip_trading_directives(afl)

        # Also strip SetTradeDelays and ExRem (not needed for signal detection)
        afl = re.sub(r'^\s*SetTradeDelays\s*\(.*?\)\s*;.*$', '',
                     afl, flags=re.MULTILINE)

        # Keep Buy/Short/ExRem assignments as they are — we want the
        # final signal values

        # --- Auto-detect indicator variables for live display ---
        indicator_columns_afl = self._detect_indicator_columns(afl)

        # Append exploration block.
        # Filter includes the latest bar (even without a signal) so we
        # always have indicator values for the live display.
        explore_block = f"""

// ==== LIVE SIGNAL SCAN ====
_recentBars = BarIndex() >= (BarCount - {self.lookback_bars});
_isLatestBar = BarIndex() == BarCount - 1;
Filter = (((Buy OR Sell OR Short OR Cover) AND _recentBars) OR _isLatestBar) AND Name() == "{self.symbol}";

AddColumn(Buy, "Buy", 1.0);
AddColumn(Sell, "Sell", 1.0);
AddColumn(Short, "Short", 1.0);
AddColumn(Cover, "Cover", 1.0);
AddColumn(Close, "Close", 1.4);
AddColumn(Open, "Open", 1.4);
AddColumn(High, "High", 1.4);
AddColumn(Low, "Low", 1.4);
AddColumn(Volume, "Volume", 1.0);
AddColumn(BarsSince(Buy), "ind_BarsSinceBuy", 1.0);
AddColumn(BarsSince(Sell), "ind_BarsSinceSell", 1.0);
{indicator_columns_afl}// ==== END LIVE SIGNAL SCAN ====
"""
        afl += explore_block
        return afl

    # Common AFL indicator functions for auto-detection
    _INDICATOR_FUNCS = [
        "MA", "EMA", "WMA", "DEMA", "TEMA", "KAMA",
        "RSI", "RSIa",
        "MACD", "Signal",
        "ATR", "ATRa",
        "BBandTop", "BBandBot", "BBandMid",
        "StochK", "StochD",
        "CCI", "ADX", "PDI", "MDI",
        "MFI", "OBV", "ROC", "MOM",
        "SAR", "VWAP",
        "LinRegSlope", "LinearReg",
        "HHV", "LLV",
        "Wilders", "StDev",
    ]

    # Variables to skip (AFL reserved or non-indicator)
    _SKIP_VARS = {
        "buy", "sell", "short", "cover",
        "buyprice", "sellprice", "shortprice", "coverprice",
        "filter", "i", "j", "n", "result",
    }

    def _detect_indicator_columns(self, afl: str) -> str:
        """Detect indicator variables in AFL and generate AddColumn lines.

        Scans the processed AFL for assignments like ``varname = MA(...)``
        using known indicator function names.  Returns AFL AddColumn
        statements with an ``ind_`` prefix so the CSV parser can
        distinguish indicator columns from standard ones.
        """
        func_alternation = "|".join(self._INDICATOR_FUNCS)
        pattern = re.compile(
            rf'^\s*(\w+)\s*=\s*(?:{func_alternation})\s*\(',
            re.MULTILINE,
        )

        detected = []
        for m in pattern.finditer(afl):
            varname = m.group(1)
            if varname.lower() in self._SKIP_VARS:
                continue
            if varname not in detected:
                detected.append(varname)

        if not detected:
            return ""

        lines = []
        for varname in detected:
            lines.append(
                f'AddColumn({varname}, "ind_{varname}", 1.4);')

        logger.debug("Auto-detected %d indicator(s): %s",
                     len(detected), ", ".join(detected))
        return "\n".join(lines) + "\n"

    def _run_exploration(self, apx_path: str,
                         csv_path: str) -> list[Signal]:
        """Run the exploration via OLE and parse results.

        On the first call (or after AFL changes), opens the APX and caches
        the AnalysisDoc COM reference.  On subsequent calls with the same
        AFL, reuses the cached doc and simply calls ``doc.Run(1)`` again,
        avoiding the overhead of Open/Close on every scan cycle.
        """
        signals = []

        with _DialogAutoDismisser() as dismisser:
            try:
                doc = self._cached_doc

                # Open the APX only if we don't have a cached doc
                if doc is None:
                    logger.debug("Opening APX: %s", apx_path)
                    count_before = self.ab.AnalysisDocs.Count
                    doc = self.ab.AnalysisDocs.Open(apx_path)
                    if doc is None:
                        # Try getting it by index
                        count_after = self.ab.AnalysisDocs.Count
                        if count_after > count_before:
                            doc = self.ab.AnalysisDocs.Item(
                                count_after - 1)
                        else:
                            logger.error("Failed to open APX: %s",
                                         apx_path)
                            return signals
                    self._cached_doc = doc
                    logger.debug("Cached new AnalysisDoc for reuse.")
                else:
                    logger.debug("Reusing cached AnalysisDoc.")

                # Run exploration (works for both first and subsequent runs)
                logger.debug("Running exploration (mode=%d)...",
                             EXPLORATION_RUN_MODE)
                doc.Run(EXPLORATION_RUN_MODE)

                # Poll until done
                start_time = time.time()
                while doc.IsBusy:
                    if time.time() - start_time > EXPLORATION_MAX_WAIT:
                        logger.warning("Exploration timed out after %ds",
                                       EXPLORATION_MAX_WAIT)
                        try:
                            doc.Abort()
                        except Exception:
                            pass
                        break
                    time.sleep(EXPLORATION_POLL_INTERVAL)

                elapsed = time.time() - start_time
                logger.debug("Exploration finished in %.1fs", elapsed)

                # Export results (overwrite previous CSV)
                doc.Export(csv_path)
                csv_exists = Path(csv_path).exists()
                csv_size = (Path(csv_path).stat().st_size
                            if csv_exists else 0)
                logger.debug("Exported CSV: %s (exists=%s, size=%d bytes)",
                             csv_path, csv_exists, csv_size)

                if dismisser._dismissed > 0:
                    logger.warning("Auto-dismissed %d dialog(s) during scan",
                                   dismisser._dismissed)

                # NOTE: Do NOT close the doc here — it stays cached for
                # reuse on the next scan.  Call close() for cleanup.

            except Exception as exc:
                logger.error("Exploration OLE error: %s", exc, exc_info=True)
                # Invalidate cached doc on error so next scan reopens
                self._cached_doc = None
                return signals

        # Parse CSV
        signals = self._parse_scan_csv(csv_path)
        return signals

    def _parse_scan_csv(self, csv_path: str) -> list[Signal]:
        """Parse the exploration CSV and return Signal objects.

        Also extracts indicator values from the latest-timestamp row
        and stores them in ``self.latest_indicators``.
        """
        signals = []
        csv_file = Path(csv_path)

        if not csv_file.exists():
            logger.debug("No exploration CSV produced (no signals).")
            return signals

        try:
            content = csv_file.read_text(encoding="utf-8-sig")
            if not content.strip():
                return signals

            reader = csv.DictReader(content.strip().splitlines())

            # Identify indicator columns (prefixed with "ind_")
            fieldnames = reader.fieldnames or []
            indicator_cols = [f for f in fieldnames if f.startswith("ind_")]

            strategy_name = Path(self.strategy_afl_path).stem

            # Track the row with the latest timestamp for indicator values
            latest_ts = None
            latest_row = None
            row_index = 0

            for row in reader:
                try:
                    row_index += 1

                    # Parse date/time from AmiBroker CSV
                    # Format varies: "Date/Time" or separate "Date" and "Time"
                    dt_str = row.get("Date/Time", "")
                    if not dt_str:
                        dt_str = row.get("Date", "") + " " + row.get("Time", "")
                    dt_str = dt_str.strip()

                    # Try common AmiBroker date formats
                    # AmiBroker may use 12-hour clock with AM/PM
                    timestamp = None
                    if dt_str:
                        for fmt in ["%m/%d/%Y %I:%M:%S %p",
                                    "%m/%d/%Y %H:%M:%S",
                                    "%Y-%m-%d %H:%M:%S",
                                    "%m/%d/%Y %H:%M",
                                    "%m/%d/%Y", "%Y-%m-%d"]:
                            try:
                                timestamp = datetime.strptime(dt_str, fmt)
                                break
                            except ValueError:
                                continue

                    if timestamp is None and dt_str:
                        logger.debug("Could not parse date: %s", dt_str)

                    # Always track the latest row for indicator extraction
                    # even if date parsing fails (use row order as fallback)
                    if timestamp is not None:
                        if latest_ts is None or timestamp > latest_ts:
                            latest_ts = timestamp
                            latest_row = row
                    elif latest_row is None:
                        # No parseable date yet — use first available row
                        latest_row = row

                    # Extract signal values (requires valid timestamp)
                    if timestamp is None:
                        continue

                    buy_val = float(row.get("Buy", "0") or "0")
                    sell_val = float(row.get("Sell", "0") or "0")
                    short_val = float(row.get("Short", "0") or "0")
                    cover_val = float(row.get("Cover", "0") or "0")
                    close_val = float(row.get("Close", "0") or "0")

                    for sig_type, sig_val in [("Buy", buy_val),
                                              ("Sell", sell_val),
                                              ("Short", short_val),
                                              ("Cover", cover_val)]:
                        if sig_val > 0:
                            signals.append(Signal(
                                signal_type=sig_type,
                                symbol=self.symbol,
                                timestamp=timestamp,
                                close_price=close_val,
                                strategy_name=strategy_name,
                                indicator_values={},
                            ))

                except Exception as exc:
                    logger.debug("Error parsing CSV row: %s", exc)
                    continue

            # --- Extract indicator values from the latest bar ---
            if latest_row:
                indicators = {}

                # Always include Close price as baseline context
                for key in ["Close"]:
                    raw = latest_row.get(key, "")
                    try:
                        if raw:
                            indicators[key] = round(float(raw), 4)
                    except (ValueError, TypeError):
                        pass

                # Extract all ind_* columns (auto-detected + built-in)
                for col in indicator_cols:
                    raw = latest_row.get(col, "")
                    try:
                        val = float(raw) if raw else None
                        if val is not None:
                            # Strip "ind_" prefix for display
                            indicators[col[4:]] = round(val, 4)
                    except (ValueError, TypeError):
                        pass

                # If still empty (no ind_ columns parsed), fall back
                # to OHLCV so there's always something to show
                if len(indicators) <= 1:
                    for key in ["Open", "High", "Low", "Volume"]:
                        raw = latest_row.get(key, "")
                        try:
                            if raw:
                                indicators[key] = round(float(raw), 4)
                        except (ValueError, TypeError):
                            pass

                self.latest_indicators = indicators
                self.latest_indicator_time = (
                    latest_ts.isoformat() if latest_ts else None)

        except Exception as exc:
            logger.error("Failed to parse scan CSV: %s", exc)

        return signals
