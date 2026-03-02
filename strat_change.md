# Strategy Changes Log

## Overview

All 25 named strategies have been updated to support both **NQ** (Nasdaq E-mini Futures) and **GC** (Gold Futures). Previously, each strategy only targeted NQ via `Name() == "NQ"`. The StdDev-based exits used by 24 of 25 strategies auto-adapt to any symbol's price level, so no stop/target recalibration is required for GC. D02 is the exception (see below).

---

## Changes Applied to All 25 Strategies

### Symbol Filter
- **Before:** `isTargetSymbol = Name() == "NQ";`
- **After:** `isTargetSymbol = Name() == "NQ" OR Name() == "GC";`

### Symbol Filter Comment
- **Before:** `// ---- Symbol filter (NQ only) ----`
- **After:** `// ---- Symbol filter (NQ and GC futures) ----`

### Header Description
- Removed "for NQ (Nasdaq E-mini Futures)" from opening description lines
- Changed "Adapted for NQ (Nasdaq E-mini Futures) on native 1-minute data." to "Targets NQ and GC futures on native 1-minute data."

---

## Per-Strategy Changes

### A01 - TEMA + ADX Trend Filter
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- No title change needed

### A02 - TEMA + ADX Anti-Trend (Mean Reversion)
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- No title change needed

### A03 - Consolidation Zone Breakout
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- No title change needed

### A04 - Consolidation Zone Fade (False Breakout)
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- Description fix: "lower-volatility Asian session" -> "lower-volatility periods" (no session filter in code)
- No title change needed

### A05 - VWAP Band Bounce
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- No title change needed

### A06 - VWAP + TEMA Confluence Strategy
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- Title: removed trailing `| NQ`
- No title rename needed

### A07 - Range-Bound VWAP Mean Reversion
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references ("on NQ 1-minute data" -> "on 1-minute data")
- Inline comment: "simplified for NQ 1-min" -> "1-min"
- Title: removed trailing `| NQ`
- No title rename needed

### A08 - Derivative Zero-Cross Reversal
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- Title: removed trailing `| NQ`
- No title rename needed

### A09 - Derivative Zero-Cross + ADX Weakening
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- Title: removed trailing `| NQ`
- No title rename needed

### A10 - Full Stack: CZ Breakout + ADX + TEMA Strategy
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- Title: removed trailing `| NQ`
- No title rename needed

### B03 - Bollinger Band + RSI Mean Reversion
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- Description fix: removed "AND Asian session" from entry condition descriptions (no session filter in code)
- No title change needed

### B04 - Stochastic Reversal Trading
- Symbol filter: NQ only -> NQ and GC
- Header: removed "NQ" from "on NQ 1-minute data"
- No title change needed

### B06 - Session VWAP Mean Reversion Enhanced
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- No title change needed

### B07 - Donchian Channel Breakout
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- No title change needed

### B09 - ADX-Filtered EMA Crossover
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- No title change needed

### B13 - MACD Momentum + TEMA Crossover
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- No title change needed

### C01 - Consolidation + VWAP + ADX Triple Filter
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- No title change needed

### C02 - VWAP Bands + Derivative Peaks (Precision Reversal)
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- No title change needed

### C03 - Range-Bound Stochastic Reversal
- Symbol filter: NQ only -> NQ and GC
- Header: removed "NQ" from "for NQ 1-minute data"
- Inline comment: "simplified for NQ 1-min" -> "1-min"
- No title change needed

### C05 - TEMA Contrarian + VWAP Fade
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- Inline comments: removed "Asian" from Phase 2 comments ("fade the Asian bearish/bullish move" -> "fade the bearish/bullish move")
- Title: removed trailing `| NQ`
- No title rename needed

### C06 - ADX Regime Switch (Auto Trend/Range)
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- No title change needed

### C08 - VWAP Cloud Breakout + ADX Momentum
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- No title change needed

### C09 - Double Touch Derivative Reversal
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- Title: removed trailing `| NQ`
- No title rename needed

### D01 - Derivative Zero-Cross Reversal
- Symbol filter: NQ only -> NQ and GC
- Header: removed NQ-specific references
- No title change needed

### D02 - Derivative TEMA Zero-Cross (formerly "NQ Derivative TEMA Zero-Cross")
- Symbol filter: NQ only -> NQ and GC
- Header title: "D02 - NQ Derivative TEMA Zero-Cross" -> "D02 - Derivative TEMA Zero-Cross"
- Description: "NQ intraday strategy" -> "Intraday strategy"
- Design notes: "Designed for NQ (Nasdaq E-mini Futures)" -> "Targets NQ and GC futures"
- Symbol filter comments: "only NQ generates signals" -> "only NQ and GC generate signals"
- Title string: `D02 NQ Deriv TEMA Zero-Cross` -> `D02 Deriv TEMA Zero-Cross`
- **Note:** Filename (`D02_nq_deriv_tema_zerocross.afl`) not renamed to avoid breaking existing APX configurations. Consider renaming in a future cleanup pass.

---

## Title Change Suggestions

Only **D02** needed a title change (removing "NQ" from the display title). All other strategy titles are symbol-agnostic and require no changes. The `Name()` function in the Title string already dynamically shows the current symbol.

| Strategy | Current Title | Suggested Title | Action |
|----------|--------------|-----------------|--------|
| D02 | D02 NQ Deriv TEMA Zero-Cross | D02 Deriv TEMA Zero-Cross | **Changed** |
| All others | (no NQ in title) | (no change needed) | None |

---

## Compatibility Notes

### StdDev-Based Exits (24 strategies)
Strategies A01-A10, B03-B13, C01-C09, D01 all use `StDev(Close, sdBars)` for stop loss and profit target distances. Because StdDev is calculated from the symbol's own price data, these exits **automatically adapt** to GC's price level (~2,600-2,900) vs NQ's (~18,000-22,000). No parameter tuning is needed.

### Fixed-Point Exits (D02 only)
D02 uses fixed-point stops: `stopPoints = 30` and `targetPoints = 60`. These values are calibrated for NQ's typical 1-minute volatility. On GC (which moves ~1-5 points per minute vs NQ's ~5-20), these fixed stops may be too wide, resulting in fewer stop-outs but also fewer profit targets hit. **Consider creating a GC-specific variant of D02 with tighter stops (e.g., 5/10 pts) or converting to StdDev-based exits for symbol-adaptive behavior.**

### Time Filter (D02 only)
D02 has a time filter (`tradeStartTime = 153000`, `tradeEndTime = 190000`) designed for 1 hour after NY open through 2 hours before NY close (UTC times). GC trades during the same session hours, so this filter works for both symbols.

### VWAP Reset Time
Strategies using VWAP clouds reset at `sessionResetTime = 93000` (9:30 AM EST, US market open). This works for both NQ and GC as both trade the same US session.
