"""Diagnostic: test AmiBroker COM AnalysisDocs.Open() with timeout."""
import sys
import time
import threading
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import win32com.client
import pythoncom

APX_PATH = str(Path(__file__).resolve().parent.parent / "apx" / "gcz25_test.apx")
EXE = r"C:\Program Files (x86)\AmiBroker\Broker.exe"
DB = r"C:\Program Files (x86)\AmiBroker\Databases\GoldAsia"


def test_com():
    pythoncom.CoInitialize()
    print("[1] Launching AmiBroker...")
    subprocess.Popen([EXE])
    time.sleep(3)

    print("[2] Connecting via COM...")
    ab = win32com.client.Dispatch("Broker.Application")
    ab.Visible = 1
    print("[3] Connected OK. Loading database...")
    ab.LoadDatabase(DB)
    print("[4] Database loaded. Attempting AnalysisDocs.Open()...")
    print(f"    APX: {APX_PATH}")

    # Run the Open call in a thread with timeout
    result = [None]
    error = [None]

    def open_apx():
        try:
            pythoncom.CoInitialize()
            ab2 = win32com.client.Dispatch("Broker.Application")
            doc = ab2.AnalysisDocs.Open(APX_PATH)
            result[0] = doc
            print(f"[5] AnalysisDocs.Open() returned: {doc}")
            if doc is not None:
                print("[6] Running backtest (mode=2)...")
                doc.Run(2)
                elapsed = 0
                while doc.IsBusy:
                    time.sleep(0.5)
                    elapsed += 0.5
                    if elapsed > 120:
                        print("[!] Backtest timed out after 120s")
                        break
                print(f"[7] Backtest finished in {elapsed:.1f}s")
                # Export
                out_dir = Path(__file__).resolve().parent.parent / "results" / "diag_test"
                out_dir.mkdir(parents=True, exist_ok=True)
                csv_path = str(out_dir / "results.csv")
                doc.Export(csv_path)
                print(f"[8] Exported CSV to {csv_path}")
                if Path(csv_path).exists():
                    size = Path(csv_path).stat().st_size
                    print(f"    CSV size: {size} bytes")
                    # Show first few lines
                    with open(csv_path, 'r', encoding='utf-8', errors='replace') as f:
                        for i, line in enumerate(f):
                            if i < 5:
                                print(f"    {line.rstrip()}")
                doc.Close()
                print("[9] Document closed.")
        except Exception as e:
            error[0] = e
            print(f"[!] Error: {e}")

    t = threading.Thread(target=open_apx, daemon=True)
    t.start()
    t.join(timeout=30)

    if t.is_alive():
        print("[!] AnalysisDocs.Open() HUNG for 30 seconds!")
        print("    AmiBroker is likely showing a dialog box.")
        print("    Check the AmiBroker window for any popups/errors.")
        # Try to quit
        try:
            ab.Quit()
        except:
            pass
        sys.exit(1)
    elif error[0]:
        print(f"[!] COM error: {error[0]}")
        try:
            ab.Quit()
        except:
            pass
        sys.exit(1)
    else:
        print("[OK] All steps completed successfully.")
        try:
            ab.Quit()
        except:
            pass


if __name__ == "__main__":
    test_com()
