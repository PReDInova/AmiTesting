# Indicator Inventory for Trading Ideas

Complete catalog of every indicator referenced across all test ideas, clearly marking
what already exists and what needs to be built as AFL scripts.

---

## Existing Indicators (Already Built)

| Indicator | File | Description | Used By Tests |
|-----------|------|-------------|---------------|
| TEMA | `indicators/tema.afl` | Triple EMA smoothing, session-aware | A01-A10, B06, C01-C08 |
| ADX | `indicators/adx.afl` | Trend strength with +DI/-DI | A01, A02, A09, A10, B04, B07, B09, C01, C06, C08 |
| Consolidation Zones | `indicators/consolidation_zones.afl` | TEMA derivative flatness detection | A03, A04, A10, C01, C04, C05, C06 |
| Derivative Lookback | `indicators/derivative_lookback.afl` | Peak/trough detection via 1st/2nd derivatives | A08, A09, C02, C07 |
| Market Sessions | `indicators/market_sessions.afl` | Asian/London/NY time filter | ALL tests |
| Range-Bound | `indicators/range_bound.afl` | Rolling high/low channel consolidation detection | A07, C03, C06, C07 |
| StdDev Exit | `indicators/stdev_exit.afl` | Dynamic stop/target distances | Most tests |
| VWAP Clouds | `indicators/vwap_clouds.afl` | VWAP + 1/2/3 sigma deviation bands | A05-A07, B06, C01, C02, C05, C06, C08 |
| Consolidation Normwidth | `indicators/consolidation_normwidth.afl` | ATR for CZ analysis | (supporting) |

---

## AmiBroker Built-In Functions (No AFL Files Needed)

These are native AmiBroker functions that can be called directly inline within any
strategy AFL. They do NOT require separate indicator files.

| Function | AFL Syntax | Used By Tests |
|----------|-----------|---------------|
| RSI | `RSI(period)` | B03, B04, B08, B09, B10, B12, C03 |
| Bollinger Bands | `BBandTop(C, period, width)`, `BBandBot(C, period, width)` | B03, B05, C04 |
| EMA | `EMA(array, period)` | B01, B05, B09, B12 |
| MACD | `MACD(fast, slow)`, `Signal(fast, slow, signal)` | B05, B13, C05 |
| Stochastic | `StochK(kPeriod, dPeriod)`, `StochD(kPeriod, dPeriod, smooth)` | B04, C03 |
| Donchian | `HHV(High, period)`, `LLV(Low, period)` | B07 |
| SMA | `MA(array, period)` | supporting |
| ATR | `ATR(period)` | supporting (many) |
| Cross | `Cross(array1, array2)` | supporting (many) |
| Ref | `Ref(array, -n)` | supporting (many) |

**Note:** These unblock the majority of Section B and C strategies with zero new code.
Strategies just call them directly, e.g.:
```afl
myRSI = RSI(14);
Buy = Cross(30, myRSI);  // RSI crosses above 30 from below
```

---

## New Indicators Needed (Must Be Built)

### Priority 1 - Custom Logic Required

#### Keltner Channels
- **Used by:** B05, C04
- **Count:** 2 strategies
- **Why custom:** Not a built-in AmiBroker function; requires manual calculation
- **Description:** Volatility envelope based on EMA and ATR (vs. Bollinger's SMA and StdDev)
- **Required Features:**
  - EMA period for center line (15, 20, 25)
  - ATR multiplier (1.0, 1.5, 2.0)
  - ATR period (7, 10, 14)
  - Upper channel, lower channel, center line
- **AFL Implementation Notes:**
  - Center = EMA(Close, period)
  - Upper = Center + ATR(atr_period) * multiplier
  - Lower = Center - ATR(atr_period) * multiplier
  - Simple enough to inline, but a reusable file is cleaner for B05/C04

#### Pivot Points
- **Used by:** B08, C07
- **Count:** 2 strategies
- **Why custom:** Requires previous day's H/L/C via TimeFrame functions; not a single built-in call
- **Description:** Support/resistance levels calculated from previous day's H/L/C
- **Required Features:**
  - Standard pivots: P, R1, R2, R3, S1, S2, S3
  - Fibonacci pivots (optional)
  - Camarilla pivots (optional)
  - Daily reset (use previous day's data)
- **AFL Implementation Notes:**
  - Must reference previous day's TimeFrameGetPrice for H, L, C
  - Standard: P = (H+L+C)/3, R1 = 2P-L, S1 = 2P-H, R2 = P+(H-L), S2 = P-(H-L)

### Priority 2 - Session-Aware Utility Indicators

#### Asian Range Box Calculator
- **Used by:** B01, B02, B10, B11
- **Count:** 4 strategies (fundamental to many Asian ideas)
- **Description:** Records the high and low during the Asian session window for use by
  other strategies
- **Required Features:**
  - Configurable session start/end times
  - Asian High, Asian Low, Asian Open, Asian Close
  - Asian Range Width (High - Low)
  - Asian Midpoint ((High + Low) / 2)
  - Range validity filter (min/max range)
  - Carry forward values into London/NY session for comparison
- **AFL Implementation Notes:**
  - Loop through bars, track session boundaries
  - Use TimeNum() for time-based filtering
  - Similar pattern to existing market_sessions.afl

#### Divergence Detector
- **Used by:** B12
- **Count:** 1 strategy
- **Description:** Detects bullish/bearish divergence between price and an oscillator (RSI)
- **Required Features:**
  - Compare price swing highs/lows to oscillator swing highs/lows
  - Bullish divergence: lower price low + higher oscillator low
  - Bearish divergence: higher price high + lower oscillator high
  - Configurable lookback window
  - Can leverage existing `derivative_lookback.afl` for peak/trough detection
- **AFL Implementation Notes:**
  - Use existing peak/trough logic on both price and RSI arrays
  - Compare most recent peak/trough to previous one

#### Liquidity Sweep Detector
- **Used by:** B02
- **Count:** 1 strategy
- **Description:** Detects when price briefly exceeds a key level (stop hunt) then reverses
- **Required Features:**
  - Input: key level (e.g., Asian High/Low)
  - Sweep threshold: how far beyond the level price must go
  - Reversal confirmation: price returns inside the level within N bars
  - Output: sweep detected (boolean), sweep direction, sweep bar

#### Market Structure Shift (MSS) Detector
- **Used by:** B02
- **Count:** 1 strategy
- **Description:** Detects a change in market structure (higher high after down-move, or
  lower low after up-move) -- an ICT concept
- **Required Features:**
  - Track swing highs and swing lows
  - Bullish MSS: after a series of lower lows, a higher high is made
  - Bearish MSS: after a series of higher highs, a lower low is made
  - Configurable swing detection sensitivity

#### Rolling ATR Average Comparison
- **Used by:** B11
- **Count:** 1 strategy
- **Description:** Compares current session's ATR to a rolling N-day average of session ATR
- **Required Features:**
  - Calculate ATR for current Asian session
  - Compare to rolling average of past N days' Asian ATRs
  - Output: contraction ratio (current / average)
  - Flag low-volatility days (ratio < threshold)

#### Grid Order Manager
- **Used by:** (B14 - Grid Scalper, not currently in test list but noted in research)
- **Count:** 1 strategy
- **Description:** Places and manages a grid of limit orders within a defined range
- **Required Features:**
  - Configurable grid levels and spacing
  - Limit order simulation
  - Position tracking per grid level
  - Note: complex to implement in AmiBroker backtester; may need CBT (Custom Backtester)

---

## Indicator Dependency Map

```
Strategy Dependencies (what each strategy needs beyond market_sessions.afl):

A01: tema + adx + stdev_exit
A02: tema + adx + stdev_exit
A03: consolidation_zones + stdev_exit
A04: consolidation_zones + stdev_exit
A05: vwap_clouds + stdev_exit
A06: vwap_clouds + tema + stdev_exit
A07: range_bound + vwap_clouds + stdev_exit
A08: derivative_lookback + tema + stdev_exit
A09: derivative_lookback + tema + adx + stdev_exit
A10: consolidation_zones + adx + tema + stdev_exit

B01: asian_range_box (NEED) + EMA() (built-in)
B02: asian_range_box + liquidity_sweep + mss_detector (ALL NEED BUILDING)
B03: BBandTop/Bot() + RSI() (ALL BUILT-IN, READY NOW)
B04: StochK/D() + adx (ALL BUILT-IN/EXISTING, READY NOW)
B05: BBandTop/Bot() + keltner_channels (NEED) + MACD() (built-in)
B06: vwap_clouds + tema (ALL EXISTING, READY NOW)
B07: HHV/LLV() (built-in) + adx + stdev_exit (ALL EXISTING/BUILT-IN, READY NOW)
B08: pivot_points (NEED) + RSI() (built-in) + stdev_exit
B09: EMA() + RSI() (built-in) + adx + stdev_exit (ALL EXISTING/BUILT-IN, READY NOW)
B10: asian_range_box (NEED)
B11: rolling_atr_comparison (NEED) + stdev_exit
B12: RSI() (built-in) + divergence_detector (NEED) + derivative_lookback + EMA() (built-in)
B13: MACD() (built-in) + tema + stdev_exit (ALL EXISTING/BUILT-IN, READY NOW)

C01: consolidation_zones + vwap_clouds + adx + stdev_exit (ALL EXISTING, READY NOW)
C02: vwap_clouds + derivative_lookback + tema + stdev_exit (ALL EXISTING, READY NOW)
C03: range_bound + StochK/D() (built-in) + stdev_exit (ALL EXISTING/BUILT-IN, READY NOW)
C04: consolidation_zones + BBandTop/Bot() (built-in) + keltner_channels (NEED) + stdev_exit
C05: tema + vwap_clouds + stdev_exit (ALL EXISTING, READY NOW)
C06: adx + consolidation_zones + vwap_clouds + tema + stdev_exit (ALL EXISTING, READY NOW)
C07: derivative_lookback + tema + range_bound + pivot_points (NEED) + stdev_exit
C08: vwap_clouds + adx + tema + stdev_exit (ALL EXISTING, READY NOW)
```

---

## Build Priority Order for Custom Indicators

Only indicators that require custom AFL files (not built-in AmiBroker functions):

| Priority | Indicator | Strategies Unblocked | Complexity |
|----------|-----------|---------------------|------------|
| 1 | Asian Range Box | B01, B02, B10, B11 (4) | Medium |
| 2 | Keltner Channels | B05, C04 (2) | Low-Medium |
| 3 | Pivot Points | B08, C07 (2) | Medium |
| 4 | Rolling ATR Comparison | B11 (1) | Medium |
| 5 | Divergence Detector | B12 (1) | High |
| 6 | Liquidity Sweep Detector | B02 (1) | High |
| 7 | MSS Detector | B02 (1) | High |

---

## Additional Indicators Worth Considering

These weren't in the current test list but are commonly used for gold/Asian session
trading and could spawn additional test ideas:

| Indicator | Description | Potential Use |
|-----------|-------------|---------------|
| **ATR Bands** | Bands at N x ATR around a moving average | Alternative to Bollinger for volatility-based entries |
| **Ichimoku Cloud** | Multi-component trend system (Tenkan, Kijun, Senkou, Chikou) | Asian-origin indicator, natural fit for Asian session |
| **Parabolic SAR** | Trailing stop/reverse indicator | Alternative exit mechanism for trend strategies |
| **CCI (Commodity Channel Index)** | Cyclical oscillator measuring deviation from statistical mean | Gold-specific oscillator, good for commodities |
| **Williams %R** | Momentum oscillator similar to Stochastic but inverted | Alternative to Stochastic for range trading |
| **OBV (On Balance Volume)** | Cumulative volume-based trend indicator | Volume confirmation for breakouts |
| **MFI (Money Flow Index)** | Volume-weighted RSI | Better than RSI alone when volume data available |
| **Heikin-Ashi** | Smoothed candlestick calculation | Noise reduction for trend identification |
| **Supertrend** | ATR-based trend-following overlay | Simple trend filter, alternative to EMA |
| **ALMA (Arnaud Legoux MA)** | Gaussian-weighted moving average | Less lag than EMA, smoother than TEMA |
| **Hull MA** | Weighted MA designed to eliminate lag | Alternative smoothing to TEMA |
| **Chandelier Exit** | ATR-based trailing stop from highest high | Alternative exit strategy |
| **Average True Range Percent** | ATR as percentage of price | Normalizes volatility across price levels |
| **Session Volume Profile** | Volume at price levels during session | Identify high-volume nodes for S/R |
| **Order Flow Imbalance** | Bid/ask volume ratio analysis | Microstructure signal (if tick data available) |

---

## Summary

- **Existing custom indicators:** 9 (ready to use immediately)
- **AmiBroker built-in functions available:** 10+ (RSI, BB, EMA, MACD, Stochastic,
  Donchian/HHV/LLV, SMA, ATR, Cross, Ref -- no AFL files needed)
- **Custom indicators that must be built:** 7
  - Low-Medium complexity: 2 (Keltner Channels, Pivot Points)
  - Medium complexity: 2 (Asian Range Box, Rolling ATR Comparison)
  - High complexity: 3 (Divergence Detector, Liquidity Sweep, MSS Detector)
- **Additional indicator candidates for future ideas:** 15

### Strategies Ready to Build Right Now (no new indicators needed)

All Section A tests (A01-A10), plus:
- **B03** - BB + RSI Mean Reversion (built-in functions only)
- **B04** - Stochastic Range Trading (built-in + existing ADX)
- **B06** - Session VWAP Enhanced (existing indicators only)
- **B07** - Donchian Breakout (built-in HHV/LLV + existing ADX)
- **B09** - ADX-Filtered EMA Crossover (built-in + existing)
- **B13** - NY Momentum Carryover (built-in MACD + existing TEMA)
- **C01** - Triple Filter (all existing)
- **C02** - VWAP + Derivative Precision (all existing)
- **C03** - Range-Bound + Stochastic (built-in + existing)
- **C05** - TEMA Contrarian + VWAP + Fade (all existing)
- **C06** - ADX Regime Switch (all existing)
- **C08** - VWAP Cloud Breakout + ADX (all existing)

**Total: 22 strategies ready now, 9 strategies need 1+ custom indicator built first.**
