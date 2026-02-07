# Trading Agent System — Sprint 1: OLE Interface Verification

AI Agent System for Trading Strategy Development and Evaluation using AmiBroker.

## Sprint 1 Goal

Validate OLE Automation with AmiBroker by running a simple MA crossover backtest on GCZ25 (/GC gold futures).

## Project Structure

```
AmiTesting/
├── config/          # Configuration and settings
│   └── settings.py
├── afl/             # AFL strategy files
│   └── ma_crossover.afl
├── apx/             # AmiBroker Analysis Project files
│   ├── base.apx     # Template
│   └── gcz25_test.apx (generated)
├── scripts/         # Python scripts
│   ├── apx_builder.py
│   └── ole_backtest.py
├── results/         # Backtest output (HTML/CSV)
├── logs/            # Application logs
├── prd/             # Product requirements
├── run.py           # Main entry point
└── requirements.txt
```

## Setup

1. Ensure AmiBroker Professional is installed with GCZ25 database loaded
2. Install Python dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Configure the database path in `config/settings.py`:
   ```python
   AMIBROKER_DB_PATH = r"C:\Path\To\Your\GCZ25Database"
   ```

## Usage

Run the full Sprint 1 test:
```
python run.py
```

Or run steps individually:
```
python scripts/apx_builder.py   # Build the .apx file
python scripts/ole_backtest.py  # Run the OLE backtest
```

## Strategy

Simple Moving Average crossover on /GC (GCZ25):
- Buy when 10-period MA crosses above 50-period MA
- Sell when 50-period MA crosses above 10-period MA
- Daily bars, single position, no commissions

## Sprint 1 Success Criteria

- OLE methods (LoadDatabase, Run(2)) return expected values
- Backtest completes with exported trade list (CSV/HTML)
- No crashes or unhandled exceptions
