"""Diagnostic: Compute NQ derivatives in Python to verify zero-crossings."""
import win32com.client
import numpy as np

ab = win32com.client.Dispatch("Broker.Application")
stks = ab.Stocks
nq = stks("NQ")
q = nq.Quotations
n = q.Count
print(f"NQ: {n} bars, last close={q(n-1).Close}")

# Load last 5000 bars of close prices and times
bars = min(5000, n)
closes = np.zeros(bars)
times = np.zeros(bars, dtype=int)
for i in range(bars):
    idx = n - bars + i
    bar = q(idx)
    closes[i] = bar.Close
    # Extract time as HHMMSS
    dt = bar.Date
    times[i] = dt.hour * 10000 + dt.minute * 100 + dt.second

print(f"Loaded {bars} bars, price range: {closes.min():.1f} - {closes.max():.1f}")
print(f"Time range: {times[0]} - {times[-1]}")

# Compute TEMA(8)
length = 8
alpha = 2.0 / (length + 1)
ema1 = np.zeros(bars)
ema2 = np.zeros(bars)
ema3 = np.zeros(bars)
ema1[0] = ema2[0] = ema3[0] = closes[0]
for i in range(1, bars):
    ema1[i] = alpha * closes[i] + (1 - alpha) * ema1[i-1]
    ema2[i] = alpha * ema1[i] + (1 - alpha) * ema2[i-1]
    ema3[i] = alpha * ema2[i] + (1 - alpha) * ema3[i-1]
tema = 3 * ema1 - 3 * ema2 + ema3

# Compute derivatives
lookback = 5
first_deriv = np.zeros(bars)
second_deriv = np.zeros(bars)
for i in range(lookback, bars):
    first_deriv[i] = (tema[i] - tema[i - lookback]) / lookback
for i in range(lookback * 2, bars):
    second_deriv[i] = first_deriv[i] - first_deriv[i - lookback]

# Count zero crossings of first derivative
cross_up = 0
cross_down = 0
cross_up_with_sep = 0
cross_down_with_sep = 0
cross_in_window = 0
min_deriv_sep = 1.0

for i in range(lookback + 1, bars):
    # Cross up: prev <= 0 and current > 0
    if first_deriv[i-1] <= 0 and first_deriv[i] > 0:
        cross_up += 1
        sep = abs(first_deriv[i] - second_deriv[i])
        if sep >= min_deriv_sep:
            cross_up_with_sep += 1
            tema_rising = tema[i] > tema[i-1]
            in_window = 153000 <= times[i] <= 190000
            if tema_rising and in_window:
                cross_in_window += 1
    # Cross down: prev >= 0 and current < 0
    if first_deriv[i-1] >= 0 and first_deriv[i] < 0:
        cross_down += 1
        sep = abs(first_deriv[i] - second_deriv[i])
        if sep >= min_deriv_sep:
            cross_down_with_sep += 1
            tema_falling = tema[i] < tema[i-1]
            in_window = 153000 <= times[i] <= 190000
            if tema_falling and in_window:
                cross_in_window += 1

print(f"\n--- Derivative Statistics (last {bars} bars) ---")
print(f"First deriv range: {first_deriv[lookback:].min():.4f} to {first_deriv[lookback:].max():.4f}")
print(f"Second deriv range: {second_deriv[lookback*2:].min():.4f} to {second_deriv[lookback*2:].max():.4f}")
print(f"\nZero crossings (up): {cross_up}")
print(f"Zero crossings (down): {cross_down}")
print(f"With separation >= {min_deriv_sep} (up): {cross_up_with_sep}")
print(f"With separation >= {min_deriv_sep} (down): {cross_down_with_sep}")
print(f"Full signal (+ TEMA dir + time window): {cross_in_window}")

# Show a few example zero crossings
print(f"\n--- Sample zero-crossing events ---")
count = 0
for i in range(lookback + 1, bars):
    if count >= 10:
        break
    crossed = False
    direction = ""
    if first_deriv[i-1] <= 0 and first_deriv[i] > 0:
        crossed = True
        direction = "UP"
    elif first_deriv[i-1] >= 0 and first_deriv[i] < 0:
        crossed = True
        direction = "DOWN"

    if crossed:
        sep = abs(first_deriv[i] - second_deriv[i])
        in_win = "YES" if 153000 <= times[i] <= 190000 else "NO"
        tema_dir = "rising" if tema[i] > tema[i-1] else "falling"
        print(f"  Bar {i}: time={times[i]} cross={direction} 1st={first_deriv[i]:.4f} 2nd={second_deriv[i]:.4f} sep={sep:.4f} window={in_win} tema={tema_dir} price={closes[i]:.1f}")
        count += 1
