# Gold Asian Session Trading Ideas

Master catalog of backtesting ideas for /GC (Gold Futures) focused on the Asian session
(6:00 PM - 3:00 AM EST / 23:00 - 08:00 UTC).

All ideas leverage the existing indicator library where possible. Each test has a unique ID
for tracking results across iterations.

---

## Section A: Existing Indicator Combinations

These tests use only indicators already built in the `indicators/` directory, combined in
new ways and filtered to the Asian session via `market_sessions.afl`.

### A01 - TEMA + ADX Trend Filter

**Concept:** Use TEMA crossover signals but only enter when ADX confirms a trend is present.
Avoids the whipsaw problem of pure crossover strategies during flat Asian hours.

- **Indicators:** `tema.afl`, `adx.afl`, `market_sessions.afl`
- **Timeframe:** 1-min bars (aggregated from tick)
- **Entry Long:** TEMA crosses above Close AND ADX > 25 AND +DI > -DI
- **Entry Short:** Close crosses above TEMA AND ADX > 25 AND -DI > +DI
- **Exit:** StdDev-based stops (`stdev_exit.afl`, multiplier=1.0, lookback=30)
- **Session Filter:** Asian session only
- **Parameters to Sweep:**
  - TEMA Length: [13, 21, 34, 55]
  - ADX Period: [10, 14, 20]
  - ADX Threshold: [20, 25, 30]
  - StdDev Multiplier: [0.75, 1.0, 1.5, 2.0]

### A02 - TEMA + ADX Anti-Trend (Mean Reversion)

**Concept:** Opposite of A01. When ADX is LOW (no trend), use TEMA crossovers as mean
reversion signals. Asian session's range-bound nature should favor this.

- **Indicators:** `tema.afl`, `adx.afl`, `market_sessions.afl`, `stdev_exit.afl`
- **Timeframe:** 1-min bars
- **Entry Long:** TEMA crosses above Close AND ADX < 20 (no trend)
- **Entry Short:** Close crosses above TEMA AND ADX < 20
- **Exit:** StdDev-based stops (multiplier=0.75 for tighter targets in ranges)
- **Session Filter:** Asian session only
- **Parameters to Sweep:**
  - TEMA Length: [13, 21, 34]
  - ADX Period: [14, 20]
  - ADX Threshold (max): [15, 20, 25]
  - StdDev Multiplier: [0.5, 0.75, 1.0]

### A03 - Consolidation Zone Breakout

**Concept:** Use the consolidation zone detector to find tight ranges during Asian hours,
then trade the breakout direction.

- **Indicators:** `consolidation_zones.afl`, `market_sessions.afl`, `stdev_exit.afl`
- **Timeframe:** 1-min bars
- **Entry Long:** `czBreakoutUp` signal fires during Asian session
- **Entry Short:** `czBreakoutDown` signal fires during Asian session
- **Exit:** StdDev-based stops with wider multiplier (breakout needs room)
- **Session Filter:** Asian session only
- **Parameters to Sweep:**
  - CZ TEMA Length: [13, 21, 34]
  - CZ Flat Threshold: [0.25, 0.50, 0.75, 1.0]
  - CZ Max Range (ticks): [40, 60, 80, 100]
  - Min Consol Bars: [3, 5, 10, 15]
  - StdDev Multiplier: [1.0, 1.5, 2.0]

### A04 - Consolidation Zone Fade (Mean Reversion)

**Concept:** Instead of trading breakouts, fade moves BACK to the consolidation zone
midpoint when price returns after a false breakout.

- **Indicators:** `consolidation_zones.afl`, `market_sessions.afl`, `stdev_exit.afl`
- **Timeframe:** 1-min bars
- **Entry Long:** Price broke below `czLow`, then crosses back above it (false breakdown)
- **Entry Short:** Price broke above `czHigh`, then crosses back below it (false breakout)
- **Target:** `czMid` (consolidation zone midpoint)
- **Stop:** Beyond the false breakout extreme + 1 StdDev
- **Session Filter:** Asian session only
- **Parameters to Sweep:**
  - CZ TEMA Length: [13, 21, 34]
  - CZ Max Range (ticks): [40, 60, 80]
  - Min Consol Bars: [5, 10, 15]
  - Breakout confirmation bars: [2, 3, 5]

### A05 - VWAP Band Bounce

**Concept:** During Asian session, trade bounces off VWAP standard deviation bands.
Asian ranges tend to respect VWAP bands as support/resistance.

- **Indicators:** `vwap_clouds.afl`, `market_sessions.afl`, `stdev_exit.afl`
- **Timeframe:** 1-min bars
- **Entry Long:** Price touches or crosses below VWAP -1 StdDev band (s1), then crosses back above it
- **Entry Short:** Price touches or crosses above VWAP +1 StdDev band (r1), then crosses back below it
- **Exit:** Take profit at VWAP midline, stop at -2 StdDev band
- **Session Filter:** Asian session only
- **Parameters to Sweep:**
  - Entry Band: [s1/r1 (1 sigma), s2/r2 (2 sigma)]
  - Take Profit: [VWAP, opposite 1-sigma band]
  - Stop Band: [next outer band, 1.5x ATR beyond entry band]

### A06 - VWAP + TEMA Confluence

**Concept:** Only enter trades when both TEMA direction and VWAP position agree.
Price above VWAP + TEMA bullish = long. Price below VWAP + TEMA bearish = short.

- **Indicators:** `vwap_clouds.afl`, `tema.afl`, `market_sessions.afl`, `stdev_exit.afl`
- **Timeframe:** 1-min bars
- **Entry Long:** Close > VWAP AND TEMA crosses above Close (contrarian TEMA buy)
- **Entry Short:** Close < VWAP AND Close crosses above TEMA (contrarian TEMA sell)
- **Exit:** StdDev-based stops
- **Session Filter:** Asian session only
- **Parameters to Sweep:**
  - TEMA Length: [13, 21, 34]
  - VWAP Band Filter: [None, must be within 1-sigma bands, must be within 2-sigma bands]
  - StdDev Multiplier: [0.75, 1.0, 1.5]

### A07 - Range-Bound Detector + VWAP Mean Reversion

**Concept:** Use the range-bound detector to confirm consolidation, then mean-revert
toward VWAP within the range.

- **Indicators:** `range_bound.afl`, `vwap_clouds.afl`, `market_sessions.afl`, `stdev_exit.afl`
- **Timeframe:** 1-min bars
- **Entry Long:** `isRangeBound` = true AND Close < VWAP AND Close near `rangeLow`
- **Entry Short:** `isRangeBound` = true AND Close > VWAP AND Close near `rangeHigh`
- **Exit:** VWAP midline or range midpoint, whichever is closer
- **Stop:** Beyond range boundary + 1 StdDev
- **Session Filter:** Asian session only
- **Parameters to Sweep:**
  - Range Period: [15, 20, 30]
  - Range Threshold (ATR mult): [1.0, 1.5, 2.0]
  - "Near boundary" definition: [within 25%, within 33% of range from edge]
  - Min Range Bars: [3, 5, 10]

### A08 - Derivative Peak/Trough Reversal

**Concept:** Use the derivative lookback indicator to detect peaks and troughs in TEMA,
entering counter-trend at detected turning points.

- **Indicators:** `derivative_lookback.afl`, `tema.afl`, `market_sessions.afl`, `stdev_exit.afl`
- **Timeframe:** 1-min bars
- **Entry Long:** `validTrough` detected (TEMA bottoming) during Asian session
- **Entry Short:** `validPeak` detected (TEMA topping) during Asian session
- **Exit:** StdDev-based stops, or exit when opposite signal fires
- **Session Filter:** Asian session only
- **Parameters to Sweep:**
  - TEMA Length: [13, 21, 34, 55]
  - Derivative Lookback: [5, 8, 13]
  - Min Separation: [5, 10, 15, 20]
  - StdDev Multiplier: [0.75, 1.0, 1.5]

### A09 - Derivative Peaks + ADX Confirmation

**Concept:** Combine derivative peak/trough detection with ADX. Only take trough (buy)
signals when ADX shows weakening downtrend, and peak (sell) signals when ADX shows
weakening uptrend.

- **Indicators:** `derivative_lookback.afl`, `tema.afl`, `adx.afl`, `market_sessions.afl`, `stdev_exit.afl`
- **Timeframe:** 1-min bars
- **Entry Long:** `validTrough` AND ADX declining (ADX < ADX[5 bars ago]) AND +DI starting to rise
- **Entry Short:** `validPeak` AND ADX declining AND -DI starting to rise
- **Exit:** StdDev-based stops
- **Session Filter:** Asian session only
- **Parameters to Sweep:**
  - TEMA Length: [13, 21, 34]
  - ADX Period: [10, 14, 20]
  - ADX decline lookback: [3, 5, 8]
  - Derivative Lookback: [5, 8, 13]

### A10 - Full Stack: Consolidation -> Breakout -> ADX -> TEMA Direction

**Concept:** Multi-filter approach. Wait for consolidation zone to form, then trade
the breakout only if ADX confirms trend initiation and TEMA agrees with direction.

- **Indicators:** `consolidation_zones.afl`, `adx.afl`, `tema.afl`, `market_sessions.afl`, `stdev_exit.afl`
- **Timeframe:** 1-min bars
- **Entry Long:** `czBreakoutUp` AND ADX > 20 AND ADX rising AND Close > TEMA
- **Entry Short:** `czBreakoutDown` AND ADX > 20 AND ADX rising AND Close < TEMA
- **Exit:** StdDev-based stops (wider for confirmed breakouts)
- **Session Filter:** Asian session only
- **Parameters to Sweep:**
  - CZ TEMA Length: [13, 21]
  - CZ Min Consol Bars: [5, 10]
  - ADX Period: [10, 14]
  - ADX Threshold: [15, 20, 25]
  - TEMA Length (separate from CZ): [13, 21, 34]
  - StdDev Multiplier: [1.5, 2.0, 2.5]

---

## Section B: Classic Asian Session Strategies

These require building new AFL indicators/strategies but are well-documented approaches
found in online trading literature.

### B01 - Asian Session Box Breakout (Classic)

**Concept:** Mark the high/low of the Asian session as a "box." Trade the breakout
when London session opens.

- **New Indicators Needed:** Asian range box calculator
- **Timeframe:** 15-min bars
- **Setup:** Record highest high and lowest low from 23:00-08:00 UTC
- **Filter:** Only trade if range is between 5 and 25 USD
- **Entry Long:** Price closes above Asian High + 0.50 buffer during London hours
- **Entry Short:** Price closes below Asian Low - 0.50 buffer during London hours
- **Stop:** Opposite end of the Asian range
- **Target:** 1.5-2x risk distance, or trail with ATR(14) x 2
- **Trend Filter:** Only longs above EMA(200) on H1, only shorts below
- **Parameters to Sweep:**
  - Asian range start/end: [2300-0800, 0000-0800, 2300-0700]
  - Entry buffer: [0.25, 0.50, 1.00] USD
  - Min range: [3, 5, 8] USD
  - Max range: [15, 20, 25, 30] USD
  - EMA trend filter: [100, 200, None]
  - R:R target: [1:1.5, 1:2, 1:3]

### B02 - ICT Asian Range Liquidity Sweep

**Concept:** Institutions sweep the Asian range high/low to grab stop-loss liquidity
before reversing. Trade the reversal after the sweep.

- **New Indicators Needed:** Liquidity sweep detector, market structure shift (MSS) detector
- **Timeframe:** 5-min bars
- **Setup:** Mark Asian range 23:00-05:00 UTC
- **Entry Long:** Price dips below Asian Low (sweep), then makes a higher high on M5 (MSS), enter on pullback
- **Entry Short:** Price spikes above Asian High (sweep), then makes a lower low (MSS), enter on pullback
- **Stop:** Below sweep low (longs) or above sweep high (shorts)
- **Target:** Opposite side of Asian range, or previous day high/low
- **Parameters to Sweep:**
  - Sweep threshold (how far beyond range): [0.50, 1.00, 2.00] USD
  - MSS confirmation candles: [2, 3, 5]
  - Max time after sweep for entry: [30, 60, 90] minutes
  - R:R minimum: [1:2, 1:3]

### B03 - Bollinger Band + RSI Mean Reversion

**Concept:** Fade moves to Bollinger Band extremes when RSI confirms overextension
during low-volatility Asian hours.

- **New Indicators Needed:** Bollinger Bands, RSI
- **Timeframe:** 15-min bars
- **Entry Long:** Close <= Lower BB AND RSI(14) < 30
- **Entry Short:** Close >= Upper BB AND RSI(14) > 70
- **Exit:** Middle BB (20-period SMA) for take profit, 1.5x ATR beyond entry BB for stop
- **Session Filter:** Asian session only (23:00-08:00 UTC)
- **Parameters to Sweep:**
  - BB Period: [15, 20, 30]
  - BB StdDev: [1.5, 2.0, 2.5]
  - RSI Period: [7, 14, 21]
  - RSI Overbought: [65, 70, 75, 80]
  - RSI Oversold: [20, 25, 30, 35]
  - ATR Stop Multiplier: [1.0, 1.5, 2.0]

### B04 - Stochastic Range Trading

**Concept:** Use Stochastic oscillator to identify overbought/oversold within the
Asian session range, filtered by ADX < 25 to confirm ranging conditions.

- **New Indicators Needed:** Stochastic Oscillator
- **Existing Indicators:** `adx.afl`, `market_sessions.afl`
- **Timeframe:** 15-min or 30-min bars
- **Entry Long:** Stoch %K crosses above %D below 20 AND price near range low AND ADX < 25
- **Entry Short:** Stoch %K crosses below %D above 80 AND price near range high AND ADX < 25
- **Exit:** Opposite end of range, or opposite Stochastic signal
- **Stop:** 2-3 USD beyond range boundary
- **Parameters to Sweep:**
  - Stoch %K: [5, 9, 14]
  - Stoch %D: [3, 5]
  - Stoch Smoothing: [3, 5]
  - ADX Threshold: [20, 25, 30]
  - Overbought: [75, 80, 85]
  - Oversold: [15, 20, 25]

### B05 - Bollinger-Keltner Squeeze Breakout

**Concept:** Detect when Bollinger Bands contract inside Keltner Channels (extreme
low volatility). These squeezes during Asian session precede explosive London open moves.

- **New Indicators Needed:** Keltner Channels, Bollinger Bands, MACD
- **Timeframe:** 15-min or 30-min bars
- **Squeeze Condition:** Upper BB < Upper KC AND Lower BB > Lower KC
- **Entry Long:** Squeeze fires (BB expands outside KC) AND MACD histogram > 0
- **Entry Short:** Squeeze fires AND MACD histogram < 0
- **Exit:** ATR trailing stop (7-period ATR x 3.0) or MACD histogram sign change
- **Optimal Timing:** Detect squeeze during Asian, trade breakout at London open
- **Parameters to Sweep:**
  - BB Period: [15, 20, 25]
  - BB StdDev: [1.5, 2.0, 2.5]
  - KC EMA: [15, 20, 25]
  - KC ATR Multiplier: [1.0, 1.5, 2.0]
  - KC ATR Period: [7, 10, 14]
  - MACD: [7/30/14, 12/26/9, 8/21/5]
  - ATR Trail Period: [5, 7, 10]
  - ATR Trail Multiplier: [2.0, 3.0, 4.0]

### B06 - Session VWAP Mean Reversion (Enhanced)

**Concept:** Extended version of A05 using existing VWAP. When price deviates
significantly from session VWAP, fade back toward it with EMA confirmation.

- **Existing Indicators:** `vwap_clouds.afl`, `tema.afl`, `market_sessions.afl`
- **Timeframe:** 5-min or 15-min bars
- **Entry Long:** Close < VWAP - 1 sigma (s1) AND TEMA turning up (derivative positive)
- **Entry Short:** Close > VWAP + 1 sigma (r1) AND TEMA turning down (derivative negative)
- **Exit:** Take profit at VWAP, stop at 2-sigma band
- **Filter:** VWAP slope is flat (not steeply trending)
- **Parameters to Sweep:**
  - Entry deviation: [1 sigma, 1.5 sigma, 2 sigma]
  - TEMA Length: [8, 13, 21]
  - VWAP slope flatness threshold: [0.10, 0.25, 0.50]
  - Take profit: [VWAP, opposite 0.5 sigma]

### B07 - Donchian Channel Breakout

**Concept:** Compute Donchian Channel over Asian session bars only, trade the breakout
during London hours.

- **New Indicators Needed:** Donchian Channel (Asian-session-only calculation)
- **Existing Indicators:** `adx.afl`, `market_sessions.afl`, `stdev_exit.afl`
- **Timeframe:** 15-min bars
- **Entry Long:** Price > Donchian High + 0.5x ATR during London hours AND ADX > 20
- **Entry Short:** Price < Donchian Low - 0.5x ATR during London hours AND ADX > 20
- **Stop:** Opposite Donchian boundary + 1x ATR
- **Target:** 2x risk, or 2x ATR trailing stop
- **Time Exit:** Close by 16:00 UTC
- **Parameters to Sweep:**
  - Donchian Period (Asian bars): [20, 32, 40]
  - ATR buffer multiplier: [0.25, 0.50, 1.0]
  - ADX threshold: [15, 20, 25]
  - ATR trailing multiplier: [1.5, 2.0, 3.0]

### B08 - Pivot Point Bounce

**Concept:** Trade bounces off daily pivot levels during Asian session.

- **New Indicators Needed:** Pivot Point calculator (Standard, Fibonacci, or Camarilla)
- **Existing Indicators:** `market_sessions.afl`, `stdev_exit.afl`
- **Timeframe:** 15-min or 30-min bars
- **Entry Long:** Price at S1/S2 pivot support AND RSI(14) < 40
- **Entry Short:** Price at R1/R2 pivot resistance AND RSI(14) > 60
- **Exit:** Next pivot level (e.g., long at S1, TP at Pivot)
- **Stop:** 1.5x ATR beyond the pivot level
- **Parameters to Sweep:**
  - Pivot Type: [Standard, Fibonacci, Camarilla]
  - RSI Period: [7, 14]
  - RSI thresholds: [30/70, 35/65, 40/60]
  - "At pivot" tolerance: [0.50, 1.00, 2.00] USD
  - ATR stop multiplier: [1.0, 1.5, 2.0]

### B09 - ADX-Filtered EMA Crossover

**Concept:** Standard EMA crossover but filtered by ADX to avoid signals during
dead-flat Asian periods.

- **Existing Indicators:** `adx.afl`, `market_sessions.afl`, `stdev_exit.afl`
- **New Indicators Needed:** EMA (or adapt TEMA)
- **Timeframe:** 15-min bars
- **Entry Long:** EMA(20) crosses above EMA(50) AND ADX > 25 AND RSI < 70
- **Entry Short:** EMA(20) crosses below EMA(50) AND ADX > 25 AND RSI > 30
- **Exit:** RSI extreme (70 for longs, 30 for shorts) or reverse crossover
- **Stop:** Recent swing low/high, minimum 1x ATR
- **Parameters to Sweep:**
  - Fast EMA: [10, 15, 20]
  - Slow EMA: [30, 40, 50]
  - ADX Period: [10, 14, 20]
  - ADX Threshold: [20, 25, 30]
  - RSI Period: [7, 14]

### B10 - Asian Session Fade into London Open

**Concept:** The Asian session often establishes a directional move that gets faded
at the London open. Trade the reversal.

- **Existing Indicators:** `market_sessions.afl`
- **Timeframe:** 15-min bars
- **Setup:** Measure Asian move: close at 08:00 UTC vs. open at 23:00 UTC
- **Entry Short (fade bullish Asian):** Asian close > Asian open by > 3 USD, enter short when price breaks below Asian close after 08:30 UTC
- **Entry Long (fade bearish Asian):** Asian close < Asian open by > 3 USD, enter long when price breaks above Asian close after 08:30 UTC
- **Target:** Midpoint of Asian range
- **Stop:** Beyond the Asian session extreme
- **Time Exit:** Close by 12:00 UTC
- **Parameters to Sweep:**
  - Min Asian move: [2, 3, 5, 8] USD
  - Entry delay after London open: [15, 30, 45] minutes
  - Target: [Asian midpoint, Asian open, 50% retracement, 61.8% retracement]
  - Time exit: [11:00, 12:00, 14:00] UTC

### B11 - ATR Volatility Contraction Breakout

**Concept:** When Asian ATR is unusually low vs. its recent average, position for
a volatility expansion breakout.

- **New Indicators Needed:** Rolling ATR average comparison
- **Existing Indicators:** `market_sessions.afl`, `stdev_exit.afl`
- **Timeframe:** 15-min bars
- **Setup:** Today's Asian ATR < 0.7x the 20-day average Asian ATR
- **Entry Long:** Price > Asian High + 1x ATR(14) during 08:00-12:00 UTC
- **Entry Short:** Price < Asian Low - 1x ATR(14) during 08:00-12:00 UTC
- **Target:** 3x ATR from entry
- **Stop:** 1.5x ATR from entry, trail after 1.5x ATR profit
- **Parameters to Sweep:**
  - ATR Period: [7, 10, 14]
  - Volatility lookback (days): [10, 15, 20, 30]
  - Contraction threshold: [0.5, 0.6, 0.7, 0.8]
  - Entry ATR buffer: [0.5, 1.0, 1.5]
  - TP ATR mult: [2.0, 3.0, 4.0]
  - SL ATR mult: [1.0, 1.5, 2.0]

### B12 - Multi-Timeframe RSI Divergence

**Concept:** Spot RSI divergence on lower timeframe within Asian range while
confirming with higher timeframe RSI direction.

- **New Indicators Needed:** RSI, divergence detector
- **Existing Indicators:** `derivative_lookback.afl` (for peak/trough detection), `market_sessions.afl`
- **Timeframe:** 5-min for entry, 1-hour for bias
- **Entry Long:** M5 bullish divergence (lower price low, higher RSI low) AND H1 RSI > 40 AND price above H1 EMA(50)
- **Entry Short:** M5 bearish divergence (higher price high, lower RSI high) AND H1 RSI < 60 AND price below H1 EMA(50)
- **Stop:** Below divergence swing low/high
- **Target:** Opposite end of Asian range, or 1.5x risk
- **Parameters to Sweep:**
  - RSI Period: [7, 14, 21]
  - Divergence lookback: [10, 15, 20] candles
  - H1 RSI bias thresholds: [35/65, 40/60, 45/55]
  - EMA bias period: [20, 50, 100]

### B13 - NY Momentum Carryover

**Concept:** Check if the previous NY session closed with strong momentum. If so,
the early Asian session may continue that momentum briefly.

- **New Indicators Needed:** MACD
- **Existing Indicators:** `tema.afl`, `market_sessions.afl`, `stdev_exit.afl`
- **Timeframe:** 15-min bars
- **Setup:** At Asian open, check H1 MACD state at NY close
- **Entry Long:** Bullish MACD (positive, increasing histogram) AND price pulls back to TEMA(20) in first 3 hours
- **Entry Short:** Bearish MACD (negative, decreasing histogram) AND price rallies to TEMA(20) in first 3 hours
- **Target:** 1.5x ATR from entry
- **Stop:** 1x ATR from entry
- **Time Exit:** Close by 02:00 UTC
- **Parameters to Sweep:**
  - MACD: [7/30/14, 12/26/9, 8/21/5]
  - TEMA pullback length: [13, 20, 21]
  - TP ATR mult: [1.0, 1.5, 2.0]
  - SL ATR mult: [0.75, 1.0, 1.5]
  - Max entry window: [2, 3, 4] hours after Asian open

---

## Section C: Advanced / Multi-Indicator Combinations

These combine multiple existing indicators with new ones for more sophisticated strategies.

### C01 - Consolidation + VWAP + ADX Triple Filter

**Concept:** Only trade when all three agree: consolidation detected (range confirmed),
VWAP position shows bias, ADX confirms regime. Highest conviction setup.

- **Indicators:** `consolidation_zones.afl`, `vwap_clouds.afl`, `adx.afl`, `market_sessions.afl`, `stdev_exit.afl`
- **Timeframe:** 1-min bars
- **Entry Long:** `czBreakoutUp` AND Close > VWAP AND ADX > 20 AND ADX rising
- **Entry Short:** `czBreakoutDown` AND Close < VWAP AND ADX > 20 AND ADX rising
- **Exit:** StdDev-based stops (wider: 2.0-2.5x multiplier)
- **Parameters to Sweep:**
  - CZ settings: [default, tight (flat threshold=0.25), loose (flat threshold=0.75)]
  - ADX threshold: [15, 20, 25]
  - ADX "rising" lookback: [3, 5, 8] bars
  - StdDev multiplier: [1.5, 2.0, 2.5]

### C02 - VWAP Bands + Derivative Peaks (Precision Reversal)

**Concept:** Enter mean-reversion trades at VWAP bands, but only when derivative
analysis confirms a turning point in TEMA.

- **Indicators:** `vwap_clouds.afl`, `derivative_lookback.afl`, `tema.afl`, `market_sessions.afl`, `stdev_exit.afl`
- **Timeframe:** 1-min bars
- **Entry Long:** Close < VWAP s1 (below -1 sigma) AND `validTrough` detected in TEMA
- **Entry Short:** Close > VWAP r1 (above +1 sigma) AND `validPeak` detected in TEMA
- **Exit:** VWAP midline, or StdDev stop
- **Parameters to Sweep:**
  - VWAP band: [s1/r1, s2/r2]
  - TEMA Length: [13, 21, 34]
  - Derivative Lookback: [5, 8, 13]
  - Min Separation: [5, 10, 15]

### C03 - Range-Bound Regime + Stochastic (To Build)

**Concept:** Use the range-bound detector to confirm ranging market, then apply
Stochastic oscillator for timing entries within the range.

- **Existing Indicators:** `range_bound.afl`, `market_sessions.afl`, `stdev_exit.afl`
- **New Indicators Needed:** Stochastic Oscillator
- **Timeframe:** 1-min or 5-min bars
- **Entry Long:** `isRangeBound` = true AND Stoch %K crosses above %D below 20 AND Close near `rangeLow`
- **Entry Short:** `isRangeBound` = true AND Stoch %K crosses below %D above 80 AND Close near `rangeHigh`
- **Exit:** `rangeMid` or opposite Stochastic signal
- **Parameters to Sweep:**
  - Range Period: [15, 20, 30]
  - Range Threshold: [1.0, 1.5, 2.0]
  - Stoch settings: [5/3/3, 9/3/3, 14/3/3]

### C04 - Consolidation Zone + Bollinger Squeeze Confirmation

**Concept:** Two independent consolidation measures must agree. Both the CZ detector
AND the BB-inside-KC squeeze must fire before entering.

- **Existing Indicators:** `consolidation_zones.afl`, `market_sessions.afl`, `stdev_exit.afl`
- **New Indicators Needed:** Bollinger Bands, Keltner Channels
- **Timeframe:** 1-min or 5-min bars
- **Entry Long:** `isConsolidating` AND BB inside KC (squeeze), then breakout up from both
- **Entry Short:** `isConsolidating` AND BB inside KC (squeeze), then breakout down from both
- **Exit:** ATR trailing stop
- **Parameters to Sweep:**
  - CZ settings: [default, aggressive]
  - BB Period: [15, 20]
  - BB StdDev: [1.5, 2.0]
  - KC EMA: [15, 20]
  - KC ATR mult: [1.0, 1.5]

### C05 - TEMA Contrarian + VWAP Zones + Session Fade

**Concept:** Enhanced version of the existing ma_crossover.afl strategy. Add VWAP
zone awareness and session fade logic for the Asian-to-London transition.

- **Existing Indicators:** All current indicators
- **Timeframe:** 1-min bars
- **Phase 1 (Asian):** Run TEMA contrarian strategy during Asian hours (existing logic)
- **Phase 2 (London fade):** At London open, if Asian session produced a directional move, fade it using VWAP as target
- **Exit:** StdDev-based or VWAP band
- **Parameters to Sweep:**
  - TEMA Length: [13, 21, 34]
  - Phase 1 StdDev mult: [0.75, 1.0, 1.5]
  - Phase 2 fade threshold: [2, 3, 5] USD minimum Asian move
  - Phase 2 target: [VWAP, Asian midpoint, VWAP s1/r1]

### C06 - ADX Regime Switch (Trend vs. Range Auto-Select)

**Concept:** Automatically switch between trend-following and mean-reversion based
on ADX reading. ADX > 25 = trend mode (breakout CZ), ADX < 20 = range mode
(fade to VWAP). In between = no trade.

- **Existing Indicators:** `adx.afl`, `consolidation_zones.afl`, `vwap_clouds.afl`, `tema.afl`, `market_sessions.afl`, `stdev_exit.afl`
- **Timeframe:** 1-min bars
- **Trend Mode (ADX > 25):** Use A03 (CZ Breakout) logic
- **Range Mode (ADX < 20):** Use A05 (VWAP Band Bounce) logic
- **Dead Zone (ADX 20-25):** No trades
- **Parameters to Sweep:**
  - ADX Period: [10, 14, 20]
  - Trend threshold: [22, 25, 28, 30]
  - Range threshold: [15, 18, 20, 22]
  - Individual strategy params from A03 and A05

### C07 - Derivative Peak/Trough + Range-Bound + Pivot Bounce

**Concept:** In range-bound conditions, combine derivative peak/trough timing with
pivot point levels for high-precision mean reversion entries.

- **Existing Indicators:** `derivative_lookback.afl`, `tema.afl`, `range_bound.afl`, `market_sessions.afl`, `stdev_exit.afl`
- **New Indicators Needed:** Pivot Points
- **Timeframe:** 1-min bars
- **Entry Long:** `isRangeBound` AND `validTrough` AND price within 1 USD of a pivot support level
- **Entry Short:** `isRangeBound` AND `validPeak` AND price within 1 USD of a pivot resistance level
- **Exit:** Next pivot level or `rangeMid`
- **Parameters to Sweep:**
  - TEMA Length: [13, 21]
  - Derivative Lookback: [5, 8]
  - Range Period: [15, 20]
  - Pivot tolerance: [0.50, 1.00, 2.00] USD

### C08 - VWAP Cloud Breakout with ADX Momentum

**Concept:** When price breaks decisively beyond VWAP 2-sigma bands during the
Asian session with ADX confirming momentum, ride the trend.

- **Existing Indicators:** `vwap_clouds.afl`, `adx.afl`, `tema.afl`, `market_sessions.afl`, `stdev_exit.afl`
- **Timeframe:** 1-min bars
- **Entry Long:** Close > VWAP r2 (above +2 sigma) AND ADX > 30 AND +DI > -DI AND Close > TEMA
- **Entry Short:** Close < VWAP s2 (below -2 sigma) AND ADX > 30 AND -DI > +DI AND Close < TEMA
- **Exit:** StdDev-based stops (wider for trend trades)
- **Parameters to Sweep:**
  - VWAP band: [r2/s2, r3/s3]
  - ADX threshold: [25, 30, 35]
  - TEMA Length: [13, 21, 34]
  - StdDev multiplier: [1.5, 2.0, 2.5, 3.0]

---

## Section D: Parameter Optimization Studies

These are not new strategies but systematic parameter sweeps of the most promising
ideas to find optimal settings.

### D01 - TEMA Length Sensitivity Study

**Test:** Run the existing TEMA Contrarian strategy (ma_crossover.afl) across TEMA
lengths [5, 8, 13, 21, 34, 55, 89] with Asian session filter. Measure win rate,
profit factor, and max drawdown for each.

### D02 - StdDev Exit Optimization

**Test:** For each strategy in Section A, sweep the StdDev exit parameters:
- Lookback: [10, 15, 20, 30, 50, 75, 100]
- Multiplier: [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
- Asymmetric stops: Test different multipliers for stop loss vs. take profit

### D03 - ADX Threshold Sensitivity

**Test:** For strategies using ADX, sweep the threshold from 10 to 40 in steps of 2.
Plot performance metrics vs. ADX threshold to find the optimal dividing line between
"trending" and "ranging."

### D04 - Session Time Window Optimization

**Test:** For all strategies, vary the Asian session definition:
- Start: [22:00, 22:30, 23:00, 23:30, 00:00] UTC
- End: [06:00, 07:00, 07:30, 08:00, 08:30, 09:00] UTC
- Test if excluding the first/last 30 minutes improves results

### D05 - Consolidation Zone Parameter Matrix

**Test:** Full matrix sweep of consolidation zone detector:
- CZ TEMA Length: [8, 13, 21, 34]
- Flat Threshold: [0.10, 0.25, 0.50, 0.75, 1.00]
- Max Range Ticks: [20, 40, 60, 80, 100, 120]
- Min Consol Bars: [2, 3, 5, 8, 10, 15, 20]

### D06 - VWAP Band Effectiveness Study

**Test:** For VWAP-based strategies, compare performance across:
- Entry band: [0.5 sigma, 1.0 sigma, 1.5 sigma, 2.0 sigma, 2.5 sigma, 3.0 sigma]
- Exit at: [VWAP, 0.5 sigma opposite, 1.0 sigma opposite]
- Session: Asian only vs. Asian + first 2 hours of London

### D07 - Day-of-Week Filter Study

**Test:** For the top 5 performing strategies, break down results by day of week.
- Monday: Gap effects from weekend?
- Tuesday-Thursday: Normal conditions
- Friday: Position squaring before weekend?
- Determine if excluding certain days improves overall metrics

### D08 - Bar Aggregation Study

**Test:** For tick-based strategies (Section A), test different bar aggregation periods:
- Tick count: [100, 250, 500, 1000]
- Time: [30s, 1min, 2min, 5min, 15min]
- Volume: [50, 100, 200 contracts per bar]
- Range bars: [0.50, 1.00, 2.00, 5.00] USD per bar

---

## Total Test Count Summary

| Section | Tests | Approx Parameter Combos |
|---------|-------|------------------------|
| A: Existing Indicator Combos | 10 | ~2,500 |
| B: Classic Asian Strategies | 13 | ~4,000 |
| C: Advanced Multi-Indicator | 8 | ~3,000 |
| D: Optimization Studies | 8 | ~5,000 |
| **Total** | **39** | **~14,500** |

---

## Priority Order (Suggested)

Start with strategies that require NO new indicators (Section A), then move to
strategies that need minimal new code:

1. **A01** - TEMA + ADX (simplest combination of existing tools)
2. **A03** - Consolidation Breakout (leverages most complex existing indicator)
3. **A05** - VWAP Band Bounce (natural for mean reversion)
4. **A08** - Derivative Peak/Trough Reversal (unique to this codebase)
5. **A10** - Full Stack (highest conviction, most filters)
6. **B01** - Asian Box Breakout (classic, well-documented)
7. **B03** - BB + RSI Mean Reversion (simple to implement)
8. **B10** - Asian Session Fade (contrarian, pairs well with existing logic)
9. **C01** - Triple Filter (best of Section A combined)
10. **C06** - ADX Regime Switch (adaptive approach)

---

## Notes

- All strategies should be tested with realistic transaction costs: $2.50/side for
  /GC futures + slippage of 1 tick ($0.10)
- Asian session gold spreads may widen; account for 2-5 tick spread vs. 1-2 ticks
  during London/NY
- Initial equity: $100,000 per the existing APX configuration
- Position sizing: Start with 1 contract, then test risk-based sizing (1-2% per trade)
- Minimum sample size: Require at least 30 trades before drawing conclusions
- Walk-forward testing: After optimization, validate with out-of-sample period
