"""
Educational tooltips for strategy parameters.

Each entry provides context a trader needs when adjusting a parameter
in the Indicator Explorer: what the indicator does, the math, the
specific role of this value, typical settings, and guidance on when
to change it.
"""

PARAM_INFO: dict[str, dict] = {

    # ─── TEMA ──────────────────────────────────────────────────────────

    "TEMA Length": {
        "indicator": "Triple Exponential Moving Average (TEMA)",
        "math": "TEMA = 3*EMA\u2081 \u2212 3*EMA\u2082 + EMA\u2083, where each EMA feeds into the next. This triple-smoothing removes more lag than a standard EMA of the same period.",
        "param": "The lookback period for each of the three nested EMAs. Larger values produce a smoother line that reacts more slowly to price changes.",
        "typical": "21 bars is the standard for intraday gold futures (roughly 20 minutes of 1-min bars). Short-term scalpers may use 8\u201313; swing traders 34\u201355.",
        "guidance": "Decrease to catch faster moves (more signals, more noise). Increase to filter out chop and only trade sustained trends. If you see too many whipsaws, increase the length.",
    },
    "CZ TEMA Length": {
        "indicator": "TEMA for Consolidation Zone detection",
        "math": "Same TEMA formula (3*EMA\u2081 \u2212 3*EMA\u2082 + EMA\u2083). Used here to measure the slope/flatness of price movement to identify consolidation zones.",
        "param": "Lookback period for the TEMA that defines the consolidation zone\u2019s trend reference. Shorter values detect tighter consolidations.",
        "typical": "21 bars. Similar to standard TEMA Length since both measure the same instrument at 1-minute resolution.",
        "guidance": "Decrease to detect shorter consolidation patterns. Increase if too many false consolidation zones are being identified (the TEMA needs to be \u2018flat\u2019 for longer).",
    },
    "Trend TEMA Length": {
        "indicator": "TEMA for higher-timeframe trend direction",
        "math": "Same TEMA calculation but with a longer period to capture the broader trend rather than short-term oscillations.",
        "param": "Lookback period for the trend-direction TEMA. A longer period here means the strategy only trades in the direction of a more established trend.",
        "typical": "34 bars. Roughly 1.6x the standard TEMA Length to provide a clear separation between the signal and trend timeframes.",
        "guidance": "Increase to require a stronger/longer trend before allowing entries. Decrease if the filter is too restrictive and missing valid setups.",
    },
    "TEMA Deriv Lookback": {
        "indicator": "TEMA derivative (rate of change)",
        "math": "Measures the slope of the TEMA over N bars: TEMA[0] \u2212 TEMA[\u2212N]. Positive = rising, negative = falling.",
        "param": "Number of bars over which to measure the TEMA\u2019s rate of change. Shorter = reacts faster to direction shifts.",
        "typical": "3 bars. A small window catches direction changes quickly for intraday scalping.",
        "guidance": "Increase to require a more sustained directional move before signaling. Decrease for faster reactions (but more false signals).",
    },

    # ─── ADX ───────────────────────────────────────────────────────────

    "ADX Period": {
        "indicator": "Average Directional Index (ADX)",
        "math": "ADX = Wilder-smoothed average of DX, where DX = |+DI \u2212 \u2212DI| / (+DI + \u2212DI) \u00d7 100. +DI and \u2212DI measure directional movement strength.",
        "param": "Wilder\u2019s smoothing period for the ADX calculation. Also used for +DI and \u2212DI. Larger values produce a smoother ADX that changes more slowly.",
        "typical": "14 bars (Wilder\u2019s original default). This is equivalent to roughly a 27-bar EMA due to Wilder\u2019s smoothing formula.",
        "guidance": "Decrease (10\u201312) for faster trend detection on short intraday timeframes. Increase (18\u201320) if ADX is too noisy and flip-flopping between trend/no-trend readings.",
    },
    "ADX Threshold": {
        "indicator": "ADX trend strength gate",
        "math": "The strategy only enters trades when ADX > this threshold, confirming a directional trend is present.",
        "param": "Minimum ADX reading required to allow entry signals. Below this value, the market is considered non-trending and entries are blocked.",
        "typical": "25 is the classic threshold. ADX < 20 = no trend, 20\u201325 = emerging trend, 25\u201350 = strong trend, > 50 = extremely strong (rare).",
        "guidance": "Lower (18\u201322) to catch trends earlier (more trades, some in weak trends). Raise (28\u201335) for higher-quality setups only. Very high values (>35) may produce very few signals.",
    },
    "ADX Max Threshold": {
        "indicator": "ADX ceiling for mean-reversion strategies",
        "math": "Entry is allowed only when ADX < this threshold, confirming the market is range-bound (not trending).",
        "param": "Maximum ADX reading permitted for entry. Above this value, a trend is developing and mean-reversion entries are suppressed.",
        "typical": "20. Mean-reversion strategies want flat, range-bound conditions where ADX is low.",
        "guidance": "Increase to allow entries in slightly trending markets (more signals). Decrease to require stricter ranging conditions (higher-quality mean-reversion setups).",
    },
    "ADX Decline Lookback": {
        "indicator": "ADX momentum (declining trend strength)",
        "math": "Checks if ADX[0] < ADX[\u2212N], meaning trend strength is fading over the last N bars.",
        "param": "Number of bars over which to measure whether ADX is declining. Confirms that the trend is losing steam before a mean-reversion entry.",
        "typical": "5 bars. A short window detects rapid trend exhaustion.",
        "guidance": "Increase to require a longer period of declining ADX (more conservative). Decrease for faster reaction to trend weakening.",
    },
    "ADX Rise Lookback": {
        "indicator": "ADX momentum (rising trend strength)",
        "math": "Checks if ADX[0] > ADX[\u2212N], confirming trend strength is increasing.",
        "param": "Number of bars over which to measure whether ADX is rising. Confirms that a trend is gaining strength before entry.",
        "typical": "5 bars. Catches the early acceleration phase of a new trend.",
        "guidance": "Increase to require more sustained ADX acceleration. Decrease for quicker confirmation of emerging trends.",
    },
    "Range ADX Threshold": {
        "indicator": "ADX gate for range-bound regime",
        "math": "Same as ADX Threshold but specifically tuned for a range-bound strategy mode.",
        "param": "When ADX is below this value, the strategy considers the market to be in a range and activates range-bound logic.",
        "typical": "20. Similar to ADX Max Threshold; below 20 is generally considered \u2018no trend\u2019.",
        "guidance": "Adjust together with Trend ADX Threshold to define clear regime boundaries. Leave a gap between them to avoid flip-flopping.",
    },
    "Trend ADX Threshold": {
        "indicator": "ADX gate for trending regime",
        "math": "Same as ADX Threshold but specifically tuned for the trending strategy mode.",
        "param": "When ADX exceeds this value, the strategy considers the market to be trending and activates trend-following logic.",
        "typical": "25. Should be higher than Range ADX Threshold to create a clear separation between regimes.",
        "guidance": "Raise to require stronger trends before switching to trend mode. Lower to catch trends earlier but risk false regime detection.",
    },

    # ─── StdDev Exit / Risk Management ────────────────────────────────

    "StdDev Lookback (bars)": {
        "indicator": "Standard Deviation exit bands",
        "math": "StdDev = \u221a(mean of squared deviations from the mean over N bars). The exit distance = StdDev \u00d7 multiplier, creating dynamic stops that adapt to current volatility.",
        "param": "Number of bars used to calculate the rolling standard deviation of price. This determines how much price history defines \u2018normal\u2019 volatility.",
        "typical": "30 bars (30 minutes at 1-min resolution). Provides a stable volatility estimate without being too slow to react to regime changes.",
        "guidance": "Decrease (10\u201320) for stops that adapt faster to volatility changes (tighter during calm, wider during spikes). Increase (50\u2013100) for more stable stop distances that don\u2019t react to short-term vol spikes.",
    },
    "StdDev Multiplier": {
        "indicator": "Standard Deviation exit band width",
        "math": "Exit distance = StdDev \u00d7 this multiplier. A multiplier of 1.0 means the stop is 1 standard deviation away from the current price.",
        "param": "How many standard deviations away the stop loss is placed. Higher = wider stops (more room to breathe). Lower = tighter stops (less risk per trade, more stop-outs).",
        "typical": "1.0 (one standard deviation). At this level, roughly 68% of normal price moves would stay within the stop distance.",
        "guidance": "Increase (1.5\u20132.5) to avoid being stopped out by normal volatility, at the cost of larger losses when stops are hit. Decrease (0.5\u20130.8) for tighter risk control in mean-reversion strategies where you expect small moves.",
    },
    "SD Multiplier": {
        "indicator": "Standard Deviation multiplier (alternate name)",
        "math": "Same as StdDev Multiplier: Exit distance = StdDev \u00d7 this value.",
        "param": "Controls the width of the volatility-based stop band. Functionally identical to \u2018StdDev Multiplier\u2019 but used in strategies with a different naming convention.",
        "typical": "1.0 standard deviation. Adjust based on strategy type: trend-following benefits from wider stops (1.5\u20132.0), mean-reversion from tighter (0.5\u20131.0).",
        "guidance": "Same guidance as StdDev Multiplier. Match to your strategy\u2019s expected move size.",
    },
    "Profit Target Mult": {
        "indicator": "Profit target as a multiple of stop distance",
        "math": "Profit target = exit distance (StdDev-based) \u00d7 this multiplier. A value of 1.0 means the profit target equals the stop loss distance (1:1 reward-to-risk).",
        "param": "The reward-to-risk ratio. 1.0 = symmetric (target = stop). 2.0 = target is 2\u00d7 the stop distance. Higher values mean fewer winners but larger profits per win.",
        "typical": "1.0 (symmetric stops). This is a neutral starting point; the strategy\u2019s edge comes from entry signal quality, not asymmetric R:R.",
        "guidance": "Increase (1.5\u20132.0) if the strategy catches directional moves that tend to run. Decrease (0.5\u20130.8) for mean-reversion strategies where quick, small profits are expected. Watch the win rate: higher mult \u2192 lower win rate.",
    },
    "Range SD Mult": {
        "indicator": "StdDev multiplier for range-bound regime",
        "math": "Same StdDev \u00d7 multiplier calculation, but tuned for range-bound conditions where moves are smaller.",
        "param": "A tighter multiplier used when the market is in a range. Since range-bound moves are smaller, stops should be proportionally tighter.",
        "typical": "0.8. Tighter than the default 1.0 because ranging markets have smaller price excursions.",
        "guidance": "Decrease if range-bound trades are being stopped out by normal noise. Increase if range-bound profits are too small relative to stop distance.",
    },
    "Trend SD Mult": {
        "indicator": "StdDev multiplier for trending regime",
        "math": "Same StdDev \u00d7 multiplier calculation, tuned for trending conditions where moves are larger.",
        "param": "A wider multiplier used when the market is trending. Trends produce larger swings that need more room.",
        "typical": "1.5. Wider than the range-bound multiplier to let trending moves develop without premature stops.",
        "guidance": "Increase if trend trades are being stopped out during normal pullbacks. Decrease if losing too much on failed trend entries.",
    },

    # ─── Bollinger Bands ──────────────────────────────────────────────

    "BB Period": {
        "indicator": "Bollinger Bands",
        "math": "Middle band = SMA(Close, N). Upper = Middle + K\u00d7StdDev. Lower = Middle \u2212 K\u00d7StdDev. The bands widen during volatile periods and narrow during quiet ones.",
        "param": "The SMA lookback period for the middle band. Also used as the lookback for the standard deviation calculation.",
        "typical": "20 bars (Bollinger\u2019s original default). This represents roughly 20 minutes of intraday data at 1-min resolution.",
        "guidance": "Decrease (10\u201315) for bands that react faster to price changes (tighter, more touches). Increase (25\u201340) for smoother bands that filter out short-term noise.",
    },
    "BB StdDev": {
        "indicator": "Bollinger Bands width",
        "math": "Upper band = SMA + (this value \u00d7 StdDev). Lower band = SMA \u2212 (this value \u00d7 StdDev).",
        "param": "Number of standard deviations for the upper and lower bands. Controls how far the bands sit from the moving average.",
        "typical": "2.0 standard deviations. At this level, roughly 95% of price action should stay within the bands under normal conditions.",
        "guidance": "Decrease (1.5) for more frequent band touches (more signals, more false ones). Increase (2.5\u20133.0) for rarer, higher-conviction touches at extreme levels.",
    },

    # ─── RSI ──────────────────────────────────────────────────────────

    "RSI Period": {
        "indicator": "Relative Strength Index (RSI)",
        "math": "RSI = 100 \u2212 100/(1 + RS), where RS = average gain / average loss over N bars using Wilder\u2019s smoothing. Oscillates 0\u2013100.",
        "param": "Wilder\u2019s smoothing period. Shorter periods make RSI more volatile and responsive; longer periods produce smoother readings.",
        "typical": "14 bars (Wilder\u2019s original default). For intraday gold futures, some traders use 7\u201310 for faster signals.",
        "guidance": "Decrease (7\u201310) for more responsive signals on fast intraday charts. Increase (20\u201325) for smoother readings that filter out noise. Must re-tune OB/OS levels when changing the period.",
    },
    "RSI Overbought": {
        "indicator": "RSI overbought threshold",
        "math": "When RSI > this level, the instrument is considered overbought. Mean-reversion strategies sell/short at this level.",
        "param": "The RSI reading above which the market is considered overextended to the upside.",
        "typical": "70 is the classic level. Some aggressive traders use 80 for higher-conviction signals.",
        "guidance": "Raise (75\u201380) for fewer, higher-quality overbought signals. Lower (60\u201365) for more frequent signals (but more false ones). Always adjust in tandem with Oversold.",
    },
    "RSI Oversold": {
        "indicator": "RSI oversold threshold",
        "math": "When RSI < this level, the instrument is considered oversold. Mean-reversion strategies buy at this level.",
        "param": "The RSI reading below which the market is considered overextended to the downside.",
        "typical": "30 is the classic level. Some traders use 20 for stricter oversold confirmation.",
        "guidance": "Lower (20\u201325) for fewer, higher-quality oversold signals. Raise (35\u201340) for more frequent entries. Keep symmetric with Overbought (e.g., 30/70 or 20/80).",
    },

    # ─── Stochastic ───────────────────────────────────────────────────

    "Stoch %K Period": {
        "indicator": "Stochastic Oscillator",
        "math": "%K = ((Close \u2212 Lowest Low over N) / (Highest High over N \u2212 Lowest Low over N)) \u00d7 100. Measures where the close sits within the recent range.",
        "param": "The lookback period for the highest high and lowest low. Defines the \u2018recent range\u2019 for the calculation.",
        "typical": "14 bars. Shorter (5\u20139) for fast-moving intraday charts; longer (14\u201321) for smoother signals.",
        "guidance": "Decrease for more reactive signals (useful for scalping). Increase for smoother %K that produces fewer crossover signals.",
    },
    "Stoch %D Period": {
        "indicator": "Stochastic %D (signal line)",
        "math": "%D = SMA(%K, N). It\u2019s a smoothed version of %K used for crossover signals.",
        "param": "The SMA period applied to smooth %K into %D. Crossovers of %K and %D generate trading signals.",
        "typical": "3 bars. This is the standard; larger values (5\u20137) produce fewer, more deliberate crossover signals.",
        "guidance": "Increase (5\u20137) to reduce false crossover signals. Decrease (2) for faster but noisier crossovers.",
    },
    "Stoch Smoothing": {
        "indicator": "Stochastic %K smoothing (Slow Stochastic)",
        "math": "Raw %K is smoothed by an SMA of this length before plotting. Smoothing of 1 = Fast Stochastic; 3 = Slow Stochastic.",
        "param": "Pre-smoothing applied to %K before the %D calculation. Turns the Fast Stochastic into the Slow Stochastic.",
        "typical": "3 (Slow Stochastic). The Slow variant is preferred for most trading as it removes the jitter of the Fast version.",
        "guidance": "Set to 1 for Fast Stochastic (more signals, more noise). Keep at 3 for the standard Slow Stochastic. Rarely set above 5.",
    },
    "Stoch Overbought": {
        "indicator": "Stochastic overbought level",
        "math": "When Stochastic > this level, the price is near the top of its recent range. Mean-reversion strategies look to sell/short.",
        "param": "The Stochastic reading above which the market is overbought.",
        "typical": "80 is standard. The scale is 0\u2013100, similar to RSI but measuring range position rather than momentum.",
        "guidance": "Raise (85\u201390) for fewer signals at more extreme levels. Lower (70\u201375) for more frequent entries.",
    },
    "Stoch Oversold": {
        "indicator": "Stochastic oversold level",
        "math": "When Stochastic < this level, the price is near the bottom of its recent range. Mean-reversion strategies look to buy.",
        "param": "The Stochastic reading below which the market is oversold.",
        "typical": "20 is standard. Symmetric with the overbought level.",
        "guidance": "Lower (10\u201315) for more extreme oversold signals. Raise (25\u201330) for more frequent entries. Keep symmetric with Overbought.",
    },

    # ─── VWAP ─────────────────────────────────────────────────────────

    "VWAP Sigma 1": {
        "indicator": "Volume Weighted Average Price (VWAP) bands",
        "math": "VWAP = \u03a3(Price \u00d7 Volume) / \u03a3(Volume), recalculated each session. Bands = VWAP \u00b1 (sigma \u00d7 StdDev of price from VWAP).",
        "param": "The first (innermost) standard deviation band around VWAP. This band contains the tightest normal price variation.",
        "typical": "1.0\u03c3. The 1-sigma band captures roughly 68% of price activity around VWAP.",
        "guidance": "Increase for wider inner bands (fewer touches). Decrease for tighter bands that are touched more frequently. This is the most commonly used entry band for VWAP bounce strategies.",
    },
    "VWAP Sigma 2": {
        "indicator": "VWAP second standard deviation band",
        "math": "Second band = VWAP \u00b1 (this value \u00d7 StdDev). Further from VWAP, representing a more significant price deviation.",
        "param": "The multiplier for the second (middle) VWAP band.",
        "typical": "2.0\u03c3. The 2-sigma band captures roughly 95% of price activity. Touches here are less frequent but more significant.",
        "guidance": "Adjust relative to Sigma 1. Keep a consistent ratio (e.g., 1.0/2.0/3.0). Narrowing the gap between bands creates tighter cloud zones.",
    },
    "VWAP Sigma 3": {
        "indicator": "VWAP third standard deviation band",
        "math": "Outermost band = VWAP \u00b1 (this value \u00d7 StdDev). Represents extreme price deviation from fair value.",
        "param": "The multiplier for the third (outermost) VWAP band.",
        "typical": "3.0\u03c3. Touches at the 3-sigma band are rare and often represent significant overextension.",
        "guidance": "Only adjust if using the outer bands for entries. A touch at 3-sigma is a high-conviction mean-reversion signal but occurs infrequently.",
    },
    "Entry Band Sigma": {
        "indicator": "VWAP band selection for entry",
        "math": "Selects which VWAP band (1\u03c3, 2\u03c3, or 3\u03c3) to use as the entry trigger.",
        "param": "An integer selector: 1 = trade off the 1-sigma band, 2 = 2-sigma, 3 = 3-sigma. Higher sigma bands produce fewer but higher-quality signals.",
        "typical": "1 (1-sigma band). This provides the most signals. Use 2 or 3 for stricter entry criteria.",
        "guidance": "Set to 1 for maximum trade frequency. Set to 2\u20133 for fewer, higher-conviction entries at more extreme price levels.",
    },
    "Breakout Band Sigma": {
        "indicator": "VWAP band for breakout detection",
        "math": "Price breaking through this VWAP band signals a directional breakout rather than a mean-reversion opportunity.",
        "param": "Which sigma band level constitutes a breakout. Price must convincingly clear this band to trigger a breakout entry.",
        "typical": "2 (2-sigma). A break beyond 2 standard deviations from VWAP suggests genuine directional momentum.",
        "guidance": "Decrease to 1 for more breakout signals (but more false breakouts). Increase to 3 for very rare, high-conviction breakouts only.",
    },
    "Entry Sigma": {
        "indicator": "VWAP sigma level for entry trigger",
        "math": "Same as Entry Band Sigma \u2014 selects the VWAP standard deviation band used for entry conditions.",
        "param": "Which sigma band to use as the entry level. Same function as \u2018Entry Band Sigma\u2019 with a shorter name.",
        "typical": "1 (innermost band). Provides the highest signal frequency.",
        "guidance": "Same as Entry Band Sigma guidance.",
    },
    "VWAP Entry Band": {
        "indicator": "VWAP band for entry (integer selector)",
        "math": "Selects the VWAP band level (1 or 2) used for entry triggers.",
        "param": "Integer selecting which VWAP band triggers entries. 1 = inner band (more signals), 2 = outer band (fewer, stronger signals).",
        "typical": "1. Start with the inner band and increase only if too many false signals.",
        "guidance": "Use 1 for normal conditions. Switch to 2 if the inner band produces too many whipsaw entries.",
    },

    # ─── EMA ──────────────────────────────────────────────────────────

    "EMA Fast": {
        "indicator": "Exponential Moving Average (fast line)",
        "math": "EMA = Price \u00d7 K + EMA_prev \u00d7 (1 \u2212 K), where K = 2/(N+1). The fast EMA reacts quickly to price changes.",
        "param": "Period for the fast (short-term) EMA in a dual-EMA crossover system.",
        "typical": "20 bars. Common fast periods range from 8 to 21 depending on the timeframe.",
        "guidance": "Decrease (8\u201312) for faster crossover signals (more whipsaws). Increase (20\u201330) for smoother signals. Keep well below the slow EMA to maintain clear separation.",
    },
    "EMA Slow": {
        "indicator": "Exponential Moving Average (slow line)",
        "math": "Same EMA formula but with a longer period. The slow EMA represents the longer-term trend direction.",
        "param": "Period for the slow (long-term) EMA. Crossover with the fast EMA generates buy/sell signals.",
        "typical": "50 bars. Common slow periods: 30, 50, or 100 bars.",
        "guidance": "Increase (75\u2013100) to trade only longer-duration trends. Decrease (30\u201340) for more responsive crossovers. The ratio of fast:slow matters more than absolute values (typically 1:2 to 1:3).",
    },

    # ─── MACD ─────────────────────────────────────────────────────────

    "MACD Fast": {
        "indicator": "Moving Average Convergence/Divergence (MACD)",
        "math": "MACD line = EMA(Close, fast) \u2212 EMA(Close, slow). Signal line = EMA(MACD line, signal period). Histogram = MACD \u2212 Signal.",
        "param": "The fast EMA period in the MACD calculation. The MACD line is the difference between the fast and slow EMAs.",
        "typical": "12 bars (Gerald Appel\u2019s original default). The 12/26/9 combination is the most widely used.",
        "guidance": "Decrease (8\u201310) for more responsive MACD signals on intraday charts. Increase (15) for smoother but slower signals. Always adjust in context with the slow period.",
    },
    "MACD Slow": {
        "indicator": "MACD slow EMA period",
        "math": "The longer EMA in the MACD calculation. MACD line = EMA(fast) \u2212 EMA(slow).",
        "param": "The slow EMA period. The gap between fast and slow determines the MACD\u2019s sensitivity.",
        "typical": "26 bars. The classic ratio is roughly 2:1 (slow:fast).",
        "guidance": "Decrease (20\u201322) for faster MACD reactions. Increase (30\u201335) for a smoother MACD that filters noise. Maintain a ratio of at least 1.5:1 with the fast period.",
    },
    "MACD Signal": {
        "indicator": "MACD signal line smoothing",
        "math": "Signal line = EMA(MACD line, N). Crossovers of the MACD line above/below the signal line generate trade signals.",
        "param": "EMA period applied to the MACD line to create the signal line. Smaller values make the signal line track the MACD more closely.",
        "typical": "9 bars. This smoothing period produces clear crossover signals without too much lag.",
        "guidance": "Decrease (5\u20137) for faster, more frequent crossover signals. Increase (12\u201315) for fewer, higher-quality signals. Very small values (3\u20134) make the signal line nearly identical to the MACD line.",
    },

    # ─── Donchian ─────────────────────────────────────────────────────

    "Donchian Period": {
        "indicator": "Donchian Channel",
        "math": "Upper = Highest High over N bars. Lower = Lowest Low over N bars. Middle = (Upper + Lower) / 2. A pure price-based channel.",
        "param": "The lookback period for the highest high and lowest low. Defines the width and reaction speed of the channel.",
        "typical": "32 bars. Richard Donchian originally used 20 for daily charts; 32 is common for intraday to capture a meaningful range.",
        "guidance": "Decrease (15\u201320) for tighter channels with more breakout signals. Increase (40\u201360) for wider channels that filter out minor breakouts. The channel should contain \u2018normal\u2019 price action for the asset.",
    },

    # ─── ATR ──────────────────────────────────────────────────────────

    "ATR Period": {
        "indicator": "Average True Range (ATR)",
        "math": "True Range = max(H\u2212L, |H\u2212C_prev|, |L\u2212C_prev|). ATR = Wilder\u2019s smoothed average of TR over N bars. Measures volatility in price units.",
        "param": "Wilder\u2019s smoothing period for ATR. Determines how quickly the ATR adapts to volatility changes.",
        "typical": "14 bars (Wilder\u2019s default). For intraday gold, 10\u201320 is standard.",
        "guidance": "Decrease (7\u201310) for ATR that reacts faster to volatility spikes. Increase (20\u201330) for more stable volatility readings. Used for position sizing and stop placement.",
    },
    "ATR Buffer Mult": {
        "indicator": "ATR-based buffer distance",
        "math": "Buffer = ATR \u00d7 this multiplier. Creates a volatility-scaled distance used for breakout confirmation or stop placement.",
        "param": "How many ATRs of buffer distance to require. For breakouts, price must move this far beyond a level to confirm the break.",
        "typical": "0.5 ATR. Half an ATR is a moderate buffer that filters noise without requiring excessive confirmation.",
        "guidance": "Increase (0.75\u20131.5) for stricter breakout confirmation (fewer false breaks). Decrease (0.2\u20130.3) for more sensitive breakout detection. Set to 0 for no buffer (pure level break).",
    },

    # ─── Consolidation Zone ───────────────────────────────────────────

    "CZ Flat Threshold": {
        "indicator": "Consolidation Zone detector",
        "math": "A zone is \u2018flat\u2019 when the TEMA slope (rate of change) is below this threshold, indicating sideways price movement.",
        "param": "Maximum absolute TEMA slope allowed for a bar to be considered part of a consolidation zone. Lower values require stricter flatness.",
        "typical": "0.5. This is measured in price-units-per-bar, so it depends on the instrument\u2019s price scale (gold \u2248 $2000\u2013$2800).",
        "guidance": "Decrease for stricter consolidation detection (only very flat zones). Increase to allow slightly drifting consolidation zones. Tune empirically by observing which zones are detected on the chart.",
    },
    "CZ Max Range (ticks)": {
        "indicator": "Maximum consolidation zone height",
        "math": "The zone\u2019s price range (high \u2212 low) must be \u2264 this value for it to qualify as a consolidation. Measured in ticks (minimum price increments).",
        "param": "The maximum allowed range of a consolidation zone in ticks. Zones wider than this are rejected as too volatile.",
        "typical": "80 ticks. For gold futures at $0.10/tick, this is $8.00 of range.",
        "guidance": "Decrease (40\u201360) to only detect very tight consolidation zones. Increase (100\u2013200) to allow wider ranging zones. Match to the instrument\u2019s typical intraday range.",
    },
    "CZ Min Consol Bars": {
        "indicator": "Minimum consolidation duration",
        "math": "The zone must persist for at least this many consecutive flat bars to be valid.",
        "param": "Minimum number of bars that must be \u2018flat\u2019 (below the slope threshold) to form a valid consolidation zone.",
        "typical": "5 bars. At 1-minute resolution, this is a 5-minute minimum consolidation.",
        "guidance": "Increase (10\u201320) to require longer consolidation periods (higher-quality breakout setups). Decrease (2\u20133) to detect shorter pauses in price action.",
    },

    # ─── Derivative / Momentum ────────────────────────────────────────

    "Deriv Lookback": {
        "indicator": "Derivative (rate of change) lookback",
        "math": "Derivative = indicator[0] \u2212 indicator[\u2212N]. Measures the slope/momentum of an indicator over N bars.",
        "param": "Number of bars over which to calculate the rate of change. Applied to indicators like TEMA to measure their acceleration or deceleration.",
        "typical": "8 bars. A moderate lookback that balances responsiveness with noise reduction.",
        "guidance": "Decrease (3\u20135) for faster momentum detection. Increase (13\u201321) for smoother momentum readings that confirm stronger directional moves.",
    },

    # ─── Miscellaneous Strategy Parameters ────────────────────────────

    "Min Fade Move USD": {
        "indicator": "Minimum price move for fade entry",
        "math": "The price must have moved at least this many dollars in one direction before a fade (counter-trend) entry is allowed.",
        "param": "Minimum directional move in USD required before fading the move. Prevents fading tiny, insignificant price wiggles.",
        "typical": "$3.00 for gold futures. This ensures there\u2019s a meaningful move to fade.",
        "guidance": "Increase ($5\u2013$10) to only fade larger moves (fewer but higher-conviction entries). Decrease ($1\u2013$2) for more frequent fade signals. Scale with the instrument\u2019s daily range.",
    },
    "Min Separation": {
        "indicator": "Minimum signal separation",
        "math": "After a signal fires, no new signal of the same type is allowed for at least N bars.",
        "param": "Minimum number of bars between consecutive signals. Prevents signal clustering where multiple entries pile up in the same area.",
        "typical": "10 bars (10 minutes at 1-min resolution). Prevents re-entry immediately after a stop-out.",
        "guidance": "Increase (15\u201330) if seeing too many rapid re-entries after stops. Decrease (3\u20135) if the filter is too restrictive and missing valid setups after fast moves.",
    },
    "Near Boundary %": {
        "indicator": "Proximity to zone boundary",
        "math": "Entry is allowed when price is within this percentage of a zone boundary (upper or lower). E.g., 25% means the top or bottom quarter of the zone.",
        "param": "How close to a consolidation zone\u2019s boundary the price must be to trigger an entry signal. Measured as a percentage of the zone\u2019s total height.",
        "typical": "25% (top or bottom quarter of the zone).",
        "guidance": "Decrease (10\u201315%) to require price to be very close to the boundary (fewer signals, stronger levels). Increase (30\u201350%) for more signals triggered further from the boundary edge.",
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# Indicator-level educational tooltips (linked from strategy descriptions)
# ═══════════════════════════════════════════════════════════════════════════

INDICATOR_INFO: dict[str, dict] = {

    "TEMA": {
        "name": "Triple Exponential Moving Average",
        "description": "A trend-following indicator that applies three layers of exponential smoothing to reduce lag while staying smooth. Responds faster than a standard EMA of the same period, making it popular for short-term momentum and crossover strategies.",
        "math": "TEMA = 3\u00d7EMA\u2081 \u2212 3\u00d7EMA\u2082 + EMA\u2083, where EMA\u2081 = EMA(Close, N), EMA\u2082 = EMA(EMA\u2081, N), EMA\u2083 = EMA(EMA\u2082, N).",
        "usage": "Used as a trend direction filter (price above/below TEMA) and for crossover signals (TEMA crossing Close). The derivative (slope) of TEMA can measure momentum strength.",
        "key_params": "TEMA Length (typically 21 for intraday). Shorter = faster reaction, more noise. Longer = smoother, more lag.",
    },

    "ADX": {
        "name": "Average Directional Index",
        "description": "Measures trend strength on a 0\u2013100 scale without indicating direction. High ADX means a strong trend (up or down); low ADX means the market is range-bound. Often used with +DI and \u2212DI directional indicators to determine which side is dominant.",
        "math": "ADX = Wilder-smoothed average of DX, where DX = |+DI \u2212 \u2212DI| / (+DI + \u2212DI) \u00d7 100. +DI measures upward movement, \u2212DI measures downward movement, each smoothed over N bars.",
        "usage": "ADX > 25 typically signals a tradeable trend (use trend-following entries). ADX < 20 signals a range-bound market (use mean-reversion entries). The crossover of +DI and \u2212DI confirms direction.",
        "key_params": "ADX Period (typically 14), ADX Threshold (typically 25 for trend gate).",
    },

    "+DI": {
        "name": "Plus Directional Indicator",
        "description": "The positive directional indicator, a component of the ADX system. Measures the strength of upward price movement over the lookback period. When +DI > \u2212DI, buyers are dominant.",
        "math": "+DM = max(High \u2212 PrevHigh, 0) when it exceeds \u2212DM, else 0. +DI = 100 \u00d7 Wilder-smooth(+DM, N) / Wilder-smooth(TR, N).",
        "usage": "Used alongside \u2212DI to confirm trend direction. +DI > \u2212DI is a bullish signal. The gap between +DI and \u2212DI indicates the strength of directional bias.",
        "key_params": "Shares the ADX Period parameter (typically 14).",
    },

    "-DI": {
        "name": "Minus Directional Indicator",
        "description": "The negative directional indicator, a component of the ADX system. Measures the strength of downward price movement. When \u2212DI > +DI, sellers are dominant.",
        "math": "\u2212DM = max(PrevLow \u2212 Low, 0) when it exceeds +DM, else 0. \u2212DI = 100 \u00d7 Wilder-smooth(\u2212DM, N) / Wilder-smooth(TR, N).",
        "usage": "Used alongside +DI to confirm trend direction. \u2212DI > +DI is a bearish signal.",
        "key_params": "Shares the ADX Period parameter (typically 14).",
    },

    "VWAP": {
        "name": "Volume Weighted Average Price",
        "description": "The average price weighted by volume, resetting each session. Acts as a dynamic support/resistance level. Institutional traders use VWAP as a benchmark \u2014 price above VWAP suggests bullish sentiment, below suggests bearish.",
        "math": "VWAP = \u03a3(Price \u00d7 Volume) / \u03a3(Volume), accumulated from the start of each trading session. Standard deviation bands are added at \u00b11\u03c3, \u00b12\u03c3, \u00b13\u03c3.",
        "usage": "Mean-reversion entries at outer bands (price touches 2\u03c3 band and reverses). Breakout confirmation when price moves beyond a VWAP band with momentum. The central VWAP line acts as a magnet for price.",
        "key_params": "Entry Band Sigma (which band triggers entries, 1\u20133), VWAP Sigma levels (band widths).",
    },

    "StdDev": {
        "name": "Standard Deviation Exit Bands",
        "description": "Rolling standard deviation of price used to set dynamic stop-loss and profit-target distances. Adapts exit distances to current volatility \u2014 wider stops in volatile markets, tighter in calm markets.",
        "math": "Band = Close \u00b1 (StdDev(Close, N) \u00d7 Multiplier). StdDev is calculated over a rolling lookback window.",
        "usage": "Applied as symmetric exits via ApplyStop: stop-loss and profit target both set at N standard deviations from entry price. Higher multiplier = wider stops, fewer stop-outs but larger losses when hit.",
        "key_params": "StdDev Lookback (typically 30 bars), StdDev Multiplier (typically 1.0\u20132.0).",
    },

    "RSI": {
        "name": "Relative Strength Index",
        "description": "A momentum oscillator on a 0\u2013100 scale measuring the speed and magnitude of recent price changes. Identifies overbought and oversold conditions.",
        "math": "RSI = 100 \u2212 100/(1 + RS), where RS = Wilder-smooth(gains, N) / Wilder-smooth(losses, N).",
        "usage": "RSI > 70 = overbought (potential sell signal). RSI < 30 = oversold (potential buy signal). Divergence between RSI and price can signal reversals. Often combined with other indicators as a filter.",
        "key_params": "RSI Period (typically 14), Overbought level (70), Oversold level (30).",
    },

    "Stochastic": {
        "name": "Stochastic Oscillator",
        "description": "A momentum indicator comparing a closing price to its price range over a lookback period. Oscillates between 0\u2013100 and identifies overbought/oversold conditions relative to recent price action.",
        "math": "%K = SMA((Close \u2212 Lowest Low) / (Highest High \u2212 Lowest Low) \u00d7 100, smoothing). %D = SMA(%K, D-period). This is the \u2018Slow Stochastic\u2019 variant.",
        "usage": "%K crossing above %D from below 20 = bullish. %K crossing below %D from above 80 = bearish. Works best in range-bound markets where overbought/oversold levels are meaningful.",
        "key_params": "%K Period (typically 14), %D Period (typically 3), Smoothing (typically 3).",
    },

    "Bollinger Bands": {
        "name": "Bollinger Bands",
        "description": "A volatility envelope placed above and below a moving average. The bands widen when volatility increases and narrow when it decreases. Approximately 95% of price action falls within \u00b12 standard deviation bands.",
        "math": "Middle = SMA(Close, N). Upper = Middle + K\u00d7StdDev(Close, N). Lower = Middle \u2212 K\u00d7StdDev(Close, N).",
        "usage": "Mean-reversion: buy when price touches the lower band, sell at the upper band. Breakout: trade in the direction of a band break after a squeeze (narrow bands). Band width indicates volatility regime.",
        "key_params": "BB Period (typically 20), BB StdDev multiplier (typically 2.0).",
    },

    "EMA": {
        "name": "Exponential Moving Average",
        "description": "A weighted moving average that gives more weight to recent prices. Reacts faster to price changes than a Simple Moving Average (SMA) of the same period. Commonly used in crossover systems.",
        "math": "EMA = Close \u00d7 \u03b1 + EMA_prev \u00d7 (1 \u2212 \u03b1), where \u03b1 = 2 / (N + 1).",
        "usage": "Fast/slow EMA crossovers generate trade signals. Price above EMA = bullish bias, below = bearish. Multiple EMAs create a \u2018ribbon\u2019 showing trend strength.",
        "key_params": "EMA Fast (short period, e.g. 8\u201312), EMA Slow (long period, e.g. 21\u201326).",
    },

    "SMA": {
        "name": "Simple Moving Average",
        "description": "The unweighted arithmetic mean of the last N closing prices. The most basic trend indicator \u2014 price above SMA is bullish, below is bearish.",
        "math": "SMA = (Close\u2081 + Close\u2082 + \u2026 + Close_N) / N.",
        "usage": "Trend direction filter and crossover signals. Commonly used periods: 20 (short-term), 50 (medium), 200 (long-term). Crossovers between fast and slow SMAs signal momentum shifts.",
        "key_params": "Period (number of bars to average).",
    },

    "MACD": {
        "name": "Moving Average Convergence/Divergence",
        "description": "A trend-following momentum indicator showing the relationship between two EMAs. The MACD line crossing the signal line generates trade signals; the histogram shows the distance between them.",
        "math": "MACD Line = EMA(Close, fast) \u2212 EMA(Close, slow). Signal Line = EMA(MACD Line, signal). Histogram = MACD \u2212 Signal.",
        "usage": "MACD crossing above Signal = bullish. MACD crossing below Signal = bearish. Zero-line crossovers confirm trend direction. Histogram divergence from price can signal reversals.",
        "key_params": "Fast period (typically 12), Slow period (typically 26), Signal period (typically 9).",
    },

    "Donchian": {
        "name": "Donchian Channel",
        "description": "A channel formed by the highest high and lowest low over N periods. Breakouts above the upper channel or below the lower channel signal potential trend starts. Used in the original Turtle Trading system.",
        "math": "Upper = Highest High over N bars. Lower = Lowest Low over N bars. Middle = (Upper + Lower) / 2.",
        "usage": "Buy on upper channel breakout, sell on lower channel breakout. The channel width indicates volatility. Also used to identify consolidation (narrow channel) and range boundaries.",
        "key_params": "Donchian Period (typically 20).",
    },

    "ATR": {
        "name": "Average True Range",
        "description": "Measures market volatility by decomposing the entire range of an asset price for the period. Does not indicate direction, only volatility. Higher ATR = more volatile market.",
        "math": "True Range = max(High \u2212 Low, |High \u2212 PrevClose|, |Low \u2212 PrevClose|). ATR = Wilder-smooth(TR, N).",
        "usage": "Position sizing (risk a fixed multiple of ATR). Dynamic stop placement (e.g., 2\u00d7ATR trailing stop). Volatility filter (avoid trading when ATR is too low/high).",
        "key_params": "ATR Period (typically 14), ATR Buffer Multiplier (for stop distance).",
    },

    "Consolidation Zone": {
        "name": "Consolidation Zone Detector",
        "description": "A custom indicator that identifies periods where price is moving sideways in a tight range. Detects when TEMA derivative is flat (low slope) and the price range is narrow, then tracks the zone boundaries for breakout/fade strategies.",
        "math": "Zone detected when: |TEMA slope| < flatness threshold AND (Highest High \u2212 Lowest Low) < max range over a minimum number of bars. Zone boundaries = highest high and lowest low during the consolidation.",
        "usage": "Breakout strategies enter when price exits the zone with momentum. Fade strategies enter when price breaks out but then crosses back inside (false breakout). Zone boundaries persist after the consolidation ends for fade entries.",
        "key_params": "CZ TEMA Length, CZ Flat Threshold, CZ Max Range, CZ Min Consol Bars.",
    },
}
