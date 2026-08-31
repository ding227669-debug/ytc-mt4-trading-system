# -*- coding: utf-8 -*-
"""Watch BITCOIN for YTC compressed signal: pullback to 77568 support + M15/M5 reversal.
Exits with 0 when signal found, or after timeout.
"""
import os, datetime, time, sys

FILES = r'C:\Program Files (x86)\Alpari MT4\MQL4\Files'
SUPPORT = 77567.80          # H4-L key level (42 touches)
TOL = 0.002                 # 0.2% zone
TIMEOUT = 4 * 3600          # 4 hours max
INTERVAL = 15               # seconds

def read_kline(tf):
    p = os.path.join(FILES, f'market_kline_BITCOIN_{tf}.txt')
    if not os.path.exists(p): return None
    bars = []
    with open(p) as f:
        for line in f:
            parts = line.split(',')
            if len(parts) < 5: continue
            try:
                ts = datetime.datetime.strptime(parts[0], '%Y.%m.%d %H:%M').timestamp()
                bars.append((int(ts), float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4].strip())))
            except (ValueError, IndexError):
                continue
    bars.sort()
    return bars

def bull_sig(b0, b1):
    o, h, l, c = b1[1], b1[2], b1[3], b1[4]
    p_o, p_h, p_l, p_c = b0[1], b0[2], b0[3], b0[4]
    body = c - o
    if body > 0 and p_c < p_o and c >= p_o and o <= p_c: return 'ENGULF'
    lower = min(o, c) - l
    upper = h - max(o, c)
    if lower >= 2 * abs(body) and upper <= lower: return 'PINBAR'
    if p_h >= h and p_l <= l and c > p_h: return 'IB-BO'
    return None

print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] watching BITCOIN support {SUPPORT:.2f} ...', flush=True)
start = time.time()
while time.time() - start < TIMEOUT:
    for tf in ('M5', 'M15'):
        bars = read_kline(tf)
        if not bars or len(bars) < 3: continue
        b0, b1 = bars[-2], bars[-1]
        price = b1[4]
        # price near support zone
        if b1[3] <= SUPPORT * (1 + TOL) and b1[2] >= SUPPORT * (1 - TOL):
            sig = bull_sig(b0, b1)
            if sig:
                print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] >>> SIGNAL: BUY {sig} on {tf} @ {price:.2f} (bar close {b1[0]})', flush=True)
                sys.exit(0)
        # also print probe every ~2 min
        t = int(time.time())
        if t % 120 < INTERVAL and tf == 'M5':
            print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] probe: price={price:.2f} (support {SUPPORT:.2f}), no signal yet', flush=True)
    time.sleep(INTERVAL)

print('TIMEOUT: no signal in 4h', flush=True)
sys.exit(1)
