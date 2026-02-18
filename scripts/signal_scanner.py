"""
Periodic signal scanner using AmiBroker OLE Exploration.

Runs a lightweight Exploration AFL against the latest bars in AmiBroker
to detect Buy/Short signals. Reuses exploration patterns from
ole_bar_analyzer.py.
"""

import csv
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
    signal_type: str          # "Buy" or "Short"
    symbol: str
    timestamp: datetime       # Bar datetime where signal fired
    close_price: float
    strategy_name: str
    indicator_values: dict


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
    """

    def __init__(
        self,
        ab,
        strategy_afl_path: str,
        symbol: str = "NQ",
        lookback_bars: int = 5,
    ):
        self.ab = ab
        self.strategy_afl_path = strategy_afl_path
        self.symbol = symbol
        self.lookback_bars = lookback_bars
        self._alerted_signals: set[str] = set()  # dedup by "type_timestamp"

    def scan(self) -> list[Signal]:
        """Run one exploration scan and return detected signals.

        Steps:
        1. Generate the exploration AFL from the strategy AFL.
        2. Write it to a temp file, build APX.
        3. Open APX in AmiBroker, run Exploration (mode=1).
        4. Poll IsBusy until complete.
        5. Export CSV, parse for Buy/Short signals.
        6. Clean up temp files.
        7. Return only NEW signals (not previously alerted).
        """
        scan_id = uuid.uuid4().hex[:8]
        scan_afl_path = APX_DIR / f"live_scan_{scan_id}.afl"
        scan_apx_path = APX_DIR / f"live_scan_{scan_id}.apx"
        scan_csv_path = APX_DIR / f"live_scan_{scan_id}.csv"

        try:
            # 1. Generate exploration AFL
            scan_afl = self._generate_scan_afl()
            scan_afl_path.write_text(scan_afl, encoding="utf-8")

            # 2. Build APX
            build_apx(
                afl_path=str(scan_afl_path),
                output_apx_path=str(scan_apx_path),
                template_apx_path=str(APX_TEMPLATE),
                run_id=f"live_scan_{scan_id}",
                symbol=self.symbol,
                populate_content=False,
            )

            # 3. Run exploration via OLE
            all_signals = self._run_exploration(
                str(scan_apx_path), str(scan_csv_path))

            # 4. Filter to only NEW signals
            new_signals = []
            for sig in all_signals:
                key = f"{sig.signal_type}_{sig.timestamp.isoformat()}"
                if key not in self._alerted_signals:
                    self._alerted_signals.add(key)
                    new_signals.append(sig)

            return new_signals

        except Exception as exc:
            logger.error("Signal scan failed: %s", exc)
            return []

        finally:
            # 5. Clean up temp files
            for p in [scan_afl_path, scan_apx_path, scan_csv_path]:
                try:
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass
            # Also clean up the strategy snapshot AFL
            snapshot = APX_DIR / f"strategy_live_scan_{scan_id}.afl"
            try:
                if snapshot.exists():
                    snapshot.unlink()
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

        # Append exploration block
        explore_block = f"""

// ==== LIVE SIGNAL SCAN ====
_recentBars = BarIndex() >= (BarCount - {self.lookback_bars});
Filter = (Buy OR Short) AND _recentBars AND Name() == "{self.symbol}";

AddColumn(Buy, "Buy", 1.0);
AddColumn(Short, "Short", 1.0);
AddColumn(Close, "Close", 1.4);
AddColumn(Open, "Open", 1.4);
AddColumn(High, "High", 1.4);
AddColumn(Low, "Low", 1.4);
AddColumn(Volume, "Volume", 1.0);
// ==== END LIVE SIGNAL SCAN ====
"""
        afl += explore_block
        return afl

    def _run_exploration(self, apx_path: str,
                         csv_path: str) -> list[Signal]:
        """Run the exploration via OLE and parse results."""
        signals = []

        with _DialogAutoDismisser():
            try:
                # Open the APX
                count_before = self.ab.AnalysisDocs.Count
                doc = self.ab.AnalysisDocs.Open(apx_path)
                if doc is None:
                    # Try getting it by index
                    count_after = self.ab.AnalysisDocs.Count
                    if count_after > count_before:
                        doc = self.ab.AnalysisDocs.Item(count_after - 1)
                    else:
                        logger.error("Failed to open APX: %s", apx_path)
                        return signals

                # Run exploration
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

                # Export results
                doc.Export(csv_path)

                # Close the analysis document
                try:
                    doc.Close()
                except Exception:
                    pass

            except Exception as exc:
                logger.error("Exploration OLE error: %s", exc)
                return signals

        # Parse CSV
        signals = self._parse_scan_csv(csv_path)
        return signals

    def _parse_scan_csv(self, csv_path: str) -> list[Signal]:
        """Parse the exploration CSV and return Signal objects."""
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

            strategy_name = Path(self.strategy_afl_path).stem

            for row in reader:
                try:
                    # Parse date/time from AmiBroker CSV
                    # Format varies: "Date/Time" or separate "Date" and "Time"
                    dt_str = row.get("Date/Time", "")
                    if not dt_str:
                        dt_str = row.get("Date", "") + " " + row.get("Time", "")
                    dt_str = dt_str.strip()

                    if not dt_str:
                        continue

                    # Try common AmiBroker date formats
                    timestamp = None
                    for fmt in ["%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                                "%m/%d/%Y", "%Y-%m-%d"]:
                        try:
                            timestamp = datetime.strptime(dt_str, fmt)
                            break
                        except ValueError:
                            continue

                    if timestamp is None:
                        logger.debug("Could not parse date: %s", dt_str)
                        continue

                    # Extract signal values
                    buy_val = float(row.get("Buy", "0") or "0")
                    short_val = float(row.get("Short", "0") or "0")
                    close_val = float(row.get("Close", "0") or "0")

                    if buy_val > 0:
                        signals.append(Signal(
                            signal_type="Buy",
                            symbol=self.symbol,
                            timestamp=timestamp,
                            close_price=close_val,
                            strategy_name=strategy_name,
                            indicator_values={},
                        ))

                    if short_val > 0:
                        signals.append(Signal(
                            signal_type="Short",
                            symbol=self.symbol,
                            timestamp=timestamp,
                            close_price=close_val,
                            strategy_name=strategy_name,
                            indicator_values={},
                        ))

                except Exception as exc:
                    logger.debug("Error parsing CSV row: %s", exc)
                    continue

        except Exception as exc:
            logger.error("Failed to parse scan CSV: %s", exc)

        return signals
