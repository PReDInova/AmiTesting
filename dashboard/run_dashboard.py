"""Launch the AmiTesting results dashboard."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard.app import app

if __name__ == "__main__":
    print("Starting AmiTesting Dashboard at http://127.0.0.1:5000")
    app.run(debug=True, port=5000)
