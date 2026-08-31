# -*- coding: utf-8 -*-
"""Diagnose why XAUUSD week had no signals"""
import struct, os, datetime, bisect

BASE = r'C:\Program Files (x86)\Alpari MT4\history\Alpari-Demo'
import sys
SYM = sys.argv[1].upper() if len(sys.argv) > 1 else 'XAUUSD'

def read_hst(name):
    p = os.path.join(BASE, name)
    if not os.path.exists(p): return None
    sz = os.path.getsize(p)
    n = (sz - 148) // 60
    bars = []
    with open(p, 'rb') as f:
        f.seek(148)
        for i in range(n):
            rec = f.read(60)
            t, o, h, l, c = struct.unpack('<qdddd', rec[:40])
            bars.append((t, o, h, l, c))
    bars.sort()
    return bars

def resample(bars, secs, start_ts):
    out = []
    cur = None
    for t, o, h, l, c in bars:
        bucket = (t - start_ts) // secs * secs + start_ts
        if cur is None or bucket != cur[0]:
            if cur: out.append(tuple(cur))
            cur = [bucket, o, h, l, c]
        else:
            cur[2] = max(cur[2], h)
            cur[3] = min(cur[3], l)
            cur[4] = c
    if cur: out.append(tuple(cur))
    return out

def fractals(bars, k=2):
    highs, lows = [], []
    for i in range(k, len(bars) - k):
        seg_h = [bars[j][2] for j in range(i-k, i+k+1)]
        seg_l = [bars[j][3] for j in range(i-k, i+k+1)]
        if bars[i][2] == max(seg_h) and seg_h.count(bars[i][2]) == 1:
            highs.append((bars[i][0], bars[i][2]))
        if bars[i][3] == min(seg_l) and seg_l.count(bars[i][3]) == 1:
            lows.append((bars[i][0], bars[i][3]))
    return highs, lows

def classify(swings_high, swings_low, ts):
    hs = sorted([p for p in swings_high if p[0] <= ts], key=lambda x: x[0])[-4:]
    ls = sorted([p for p in swings_low if p[0] <= ts], key=lambda x: x[0])[-4:]
    vals_h = [p[1] for p in hs]
    vals_l = [p[1] for p in ls]
    hh = len(vals_h) >= 2 and all(vals_h[i] > vals_h[i-1] for i in range(1, len(vals_h)))
    hl = len(vals_l) >= 2 and all(vals_l[i] > vals_l[i-1] for i in range(1, len(vals_l)))
    lh = len(vals_h) >= 2 and all(vals_h[i] < vals_h[i-1] for i in range(1, len(vals_h)))
    ll = len(vals_l) >= 2 and all(vals_l[i] < vals_l[i-1] for i in range(1, len(vals_l)))
    if hh and hl: return 'BULL'
    if lh and ll: return 'BEAR'
    if hh or hl: return 'BULLISH-BIAS'
    if lh or ll: return 'BEARISH-BIAS'
    return 'CHOP'

d1 = read_hst(f'{SYM}1440.hst')
h4f = read_hst(f'{SYM}240.hst')
h1 = read_hst(f'{SYM}60.hst')
if h4f is None and h1:
    h4f = resample(h1, 14400, h1[0][0] - h1[0][0] % 14400)
    print(f'NOTE: H4 resampled from H1 ({len(h4f)} bars)')

d1_closes = [b[4] for b in d1]
d1_ma = []
for i in range(len(d1_closes)):
    d1_ma.append((d1[i][0], sum(d1_closes[i-199:i+1]) / 200 if i >= 199 else None))
d1_swh, d1_swl = fractals(d1, k=2)
d1_swh_conf = [(t + 2*86400, v) for t, v in d1_swh]
d1_swl_conf = [(t + 2*86400, v) for t, v in d1_swl]

h4_closes = [b[4] for b in h4f]
h4_ma = []
for i in range(len(h4_closes)):
    h4_ma.append((h4f[i][0], sum(h4_closes[i-199:i+1]) / 200 if i >= 199 else None))
h4_swh, h4_swl = fractals(h4f, k=2)
h4_swh_conf = [(t + 8*3600, v) for t, v in h4_swh]
h4_swl_conf = [(t + 8*3600, v) for t, v in h4_swl]

print('== daily D1 direction this week ==')
for b in d1[-10:]:
    if b[0] < datetime.datetime(2026,8,17).timestamp(): continue
    ma = d1_ma[[x[0] for x in d1].index(b[0])][1]
    s = classify(d1_swh_conf, d1_swl_conf, b[0])
    print(f"{datetime.datetime.fromtimestamp(b[0]).strftime('%m-%d')} close={b[4]:.2f} MA200={ma:.2f} above={'Y' if b[4]>ma else 'N'} struct={s}")

print('\n== H4 direction last 8 bars ==')
for b in h4f[-8:]:
    ma = h4_ma[[x[0] for x in h4f].index(b[0])][1]
    s = classify(h4_swh_conf, h4_swl_conf, b[0])
    print(f"{datetime.datetime.fromtimestamp(b[0]).strftime('%m-%d %H:%M')} close={b[4]:.2f} MA200={ma:.2f} above={'Y' if b[4]>ma else 'N'} struct={s}")

print('\n== key levels + touches (week 08-24~08-28) ==')
ts_end = datetime.datetime(2026,8,28,18).timestamp()
ts_start = datetime.datetime(2026,8,24).timestamp()
levels = {}
for t, v in d1_swh_conf:
    if ts_start - 30*86400 <= t <= ts_end: levels[('D1-H', t, v)] = v
for t, v in d1_swl_conf:
    if ts_start - 30*86400 <= t <= ts_end: levels[('D1-L', t, v)] = v
for t, v in h4_swh_conf:
    if ts_start - 14*86400 <= t <= ts_end: levels[('H4-H', t, v)] = v
for t, v in h4_swl_conf:
    if ts_start - 14*86400 <= t <= ts_end: levels[('H4-L', t, v)] = v
for name, lv in sorted(levels.items(), key=lambda x: x[1]):
    tol = 0.001
    cnt = sum(1 for b in h1 if ts_start - 400*3600 <= b[0] <= ts_end and b[3] <= lv*(1+tol) and b[2] >= lv*(1-tol))
    print(f"{name}: {lv:.2f} touches={cnt}")

print('\n== week price range ==')
wh = [b for b in h1 if ts_start <= b[0] <= ts_end]
print(f'H1 bars in week: {len(wh)}, high={max(b[2] for b in wh):.2f}, low={min(b[3] for b in wh):.2f}')
