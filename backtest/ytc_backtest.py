# -*- coding: utf-8 -*-
"""YTC backtest for any symbol on Alpari MT4 .hst data.
Usage: python ytc_backtest.py <SYMBOL> <START YYYY-MM-DD> <END YYYY-MM-DD>
Signal TF: M15 if available (M1/M15 file), else H1 (H1 file).
"""
import struct, os, datetime, bisect, sys

BASE = r'C:\Program Files (x86)\Alpari MT4\history\Alpari-Demo'
SYM = sys.argv[1].upper() if len(sys.argv) > 1 else 'XAUUSD'
WEEK_START = datetime.datetime.strptime(sys.argv[2], '%Y-%m-%d').timestamp() if len(sys.argv) > 2 else datetime.datetime(2026, 8, 24).timestamp()
WEEK_END = datetime.datetime.strptime(sys.argv[3], '%Y-%m-%d').timestamp() if len(sys.argv) > 3 else datetime.datetime(2026, 8, 31).timestamp()

# round-number spacing per symbol (market convention)
ROUND_SPACING = {'BITCOIN': 2000, 'XAUUSD': 50, 'XAGUSD': 1, 'WTI': 5}.get(SYM, 100)

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

# ---------- data ----------
d1 = read_hst(f'{SYM}1440.hst')
h4f = read_hst(f'{SYM}240.hst')
h1 = read_hst(f'{SYM}60.hst')
m1 = read_hst(f'{SYM}1.hst')
m15f = read_hst(f'{SYM}15.hst')
if h4f is None and h1:
    h4f = resample(h1, 14400, h1[0][0] - h1[0][0] % 14400)   # H4 from H1 fallback
    print(f'NOTE: {SYM}240.hst missing, H4 resampled from H1 ({len(h4f)} bars)')

# signal TF: prefer M15 (from M1 resample or M15 file), fallback H1
sig = None
sig_tf = None
if m1:
    sig = resample(m1, 900, m1[0][0] - m1[0][0] % 900)
    if m15f:
        ts_set = {b[0] for b in sig}
        sig = sorted(sig + [b for b in m15f if b[0] not in ts_set])
    sig_tf = 'M15'
elif m15f:
    sig = m15f
    sig_tf = 'M15'
elif h1:
    sig = h1
    sig_tf = 'H1'
print(f'== YTC backtest: {SYM} {sys.argv[2]} ~ {sys.argv[3]} ==')
print(f'data: D1={len(d1) if d1 else 0} H4={len(h4f) if h4f else 0} H1={len(h1) if h1 else 0} M1={len(m1) if m1 else 0}')
print(f'signal TF: {sig_tf} ({len(sig)} bars, {datetime.datetime.fromtimestamp(sig[0][0])} ~ {datetime.datetime.fromtimestamp(sig[-1][0])})')

if not d1 or not h4f or not sig:
    print('MISSING DATA - cannot backtest'); sys.exit(1)

# ---------- D1 direction ----------
d1_closes = [b[4] for b in d1]
d1_ma = []
for i in range(len(d1_closes)):
    d1_ma.append((d1[i][0], sum(d1_closes[i-199:i+1]) / 200 if i >= 199 else None))
d1_swh, d1_swl = fractals(d1, k=2)
d1_swh_conf = [(t + 2*86400, v) for t, v in d1_swh]
d1_swl_conf = [(t + 2*86400, v) for t, v in d1_swl]

# ---------- H4 direction ----------
h4_closes = [b[4] for b in h4f]
h4_ma = []
for i in range(len(h4_closes)):
    h4_ma.append((h4f[i][0], sum(h4_closes[i-199:i+1]) / 200 if i >= 199 else None))
h4_swh, h4_swl = fractals(h4f, k=2)
h4_swh_conf = [(t + 8*3600, v) for t, v in h4_swh]
h4_swl_conf = [(t + 8*3600, v) for t, v in h4_swl]

def d1_dir(ts):
    idx = bisect.bisect_right([b[0] for b in d1], ts) - 1
    if idx < 0: return 'CHOP'
    ma = d1_ma[idx][1]
    if ma is None: return 'CHOP'
    above = d1_closes[idx] > ma
    s = classify(d1_swh_conf, d1_swl_conf, ts)
    if above and s in ('BULL', 'BULLISH-BIAS'): return 'BULL'
    if not above and s in ('BEAR', 'BEARISH-BIAS'): return 'BEAR'
    return 'CHOP'

def h4_dir(ts):
    idx = bisect.bisect_right([b[0] for b in h4f], ts) - 1
    if idx < 0: return 'CHOP'
    ma = h4_ma[idx][1]
    if ma is None: return 'CHOP'
    above = h4_closes[idx] > ma
    s = classify(h4_swh_conf, h4_swl_conf, ts)
    if above and s in ('BULL', 'BULLISH-BIAS'): return 'BULL'
    if not above and s in ('BEAR', 'BEARISH-BIAS'): return 'BEAR'
    return 'CHOP'

# ---------- key levels ----------
def candidate_levels(ts):
    lv = {}
    for t, v in d1_swh_conf:
        if ts - 30*86400 <= t <= ts: lv[('D1-H', t, v)] = v
    for t, v in d1_swl_conf:
        if ts - 30*86400 <= t <= ts: lv[('D1-L', t, v)] = v
    for t, v in h4_swh_conf:
        if ts - 14*86400 <= t <= ts: lv[('H4-H', t, v)] = v
    for t, v in h4_swl_conf:
        if ts - 14*86400 <= t <= ts: lv[('H4-L', t, v)] = v
    price = d1_closes[-1]
    base = int(price // ROUND_SPACING) * ROUND_SPACING
    for r in [base - 2*ROUND_SPACING, base - ROUND_SPACING, base, base + ROUND_SPACING, base + 2*ROUND_SPACING]:
        lv[('RND', 0, float(r))] = float(r)
    return lv

h1_times = [b[0] for b in h1] if h1 else []
touch_src = h1 if h1 else sig   # fallback: use signal TF bars for touch counting
touch_cache = {}
def touch_ts_list(level):
    tol = 0.001
    return [b[0] for b in touch_src if b[3] <= level * (1 + tol) and b[2] >= level * (1 - tol)]

def touch_count(level, ts):
    if level not in touch_cache:
        touch_cache[level] = touch_ts_list(level)
    lst = touch_cache[level]
    window = 400 * 3600 if h1 else 400 * 900   # 400 H1 bars or 400 M15 bars
    lo = bisect.bisect_left(lst, ts - window)
    hi = bisect.bisect_left(lst, ts)
    return hi - lo

# ---------- signal scan ----------
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

def bear_sig(b0, b1):
    o, h, l, c = b1[1], b1[2], b1[3], b1[4]
    p_o, p_h, p_l, p_c = b0[1], b0[2], b0[3], b0[4]
    body = c - o
    if body < 0 and p_c > p_o and c <= p_o and o >= p_c: return 'ENGULF'
    upper = h - max(o, c)
    lower = min(o, c) - l
    if upper >= 2 * abs(body) and lower <= upper: return 'PINBAR'
    if p_h >= h and p_l <= l and c < p_l: return 'IB-BO'
    return None

opportunities = []
for i in range(2, len(sig)):
    ts = sig[i][0]
    if ts < WEEK_START or ts > WEEK_END: continue
    dd, hd = d1_dir(ts), h4_dir(ts)
    allow_long = dd == 'BULL' and hd in ('BULL', 'BULLISH-BIAS')
    allow_short = dd == 'BEAR' and hd in ('BEAR', 'BEARISH-BIAS')
    if not allow_long and not allow_short: continue
    for (key, lv) in sorted(candidate_levels(ts).items(), key=lambda x: x[1]):
        kind, t0, _ = key
        need = 2 if kind.startswith('D1') else 3
        if touch_count(lv, ts) < need: continue
        tol = 0.002
        if not (sig[i][3] <= lv * (1 + tol) and sig[i][2] >= lv * (1 - tol)): continue
        sig_name = None
        if allow_long and lv <= sig[i][4]:
            sig_name = bull_sig(sig[i-1], sig[i])
            d = 'BUY'
        elif allow_short and lv >= sig[i][4]:
            sig_name = bear_sig(sig[i-1], sig[i])
            d = 'SELL'
        if sig_name:
            opportunities.append({'ts': ts, 'dir': d, 'level': lv, 'kind': kind,
                                  'touches': touch_count(lv, ts), 'sig': sig_name, 'close': sig[i][4]})

merged = []
for op in sorted(opportunities, key=lambda x: x['ts']):
    dup = False
    for m in merged:
        if m['dir'] == op['dir'] and abs(m['level'] - op['level']) / op['level'] < 0.002 and (op['ts'] - m['ts']) < 86400:
            dup = True; break
    if not dup: merged.append(op)

print(f'raw signals: {len(opportunities)}, merged opportunities: {len(merged)}\n')
for op in merged:
    print(f"{datetime.datetime.fromtimestamp(op['ts'])} {op['dir']:4s} @ {op['level']:.2f} "
          f"[{op['kind']}] touches={op['touches']} sig={op['sig']} close={op['close']:.2f}")

print('\n== Simulated execution (entry=signal close, SL=level*0.999, TP=+2R) ==')
total_r = 0.0
for op in merged:
    entry = op['close']
    sl = op['level'] * 0.999 if op['dir'] == 'BUY' else op['level'] * 1.001
    r = abs(entry - sl)
    tp = entry + 2 * r if op['dir'] == 'BUY' else entry - 2 * r
    status, exit_price, exit_ts = 'OPEN', None, None
    for b in sig:
        if b[0] <= op['ts']: continue
        if op['dir'] == 'BUY':
            if b[3] <= sl: status, exit_price, exit_ts = 'SL', sl, b[0]; break
            if b[2] >= tp: status, exit_price, exit_ts = 'TP', tp, b[0]; break
        else:
            if b[2] >= sl: status, exit_price, exit_ts = 'SL', sl, b[0]; break
            if b[3] <= tp: status, exit_price, exit_ts = 'TP', tp, b[0]; break
    last = sig[-1][4]
    if status == 'OPEN':
        pnl = (last - entry) if op['dir'] == 'BUY' else (entry - last)
        rr = pnl / r if r else 0
    else:
        pnl = (exit_price - entry) if op['dir'] == 'BUY' else (entry - exit_price)
        rr = pnl / r if r else 0
        print(f"{datetime.datetime.fromtimestamp(op['ts'])} {op['dir']} entry={entry:.2f} SL={sl:.2f} TP={tp:.2f} "
              f"=> {status} @ {exit_price:.2f} ({datetime.datetime.fromtimestamp(exit_ts).strftime('%m-%d %H:%M')}) pnl={rr:+.1f}R")
    if status == 'OPEN':
        print(f"{datetime.datetime.fromtimestamp(op['ts'])} {op['dir']} entry={entry:.2f} SL={sl:.2f} TP={tp:.2f} "
              f"=> {status} now={last:.2f} pnl={rr:+.1f}R")
    total_r += rr
print(f'\nTOTAL: {len(merged)} trades, net {total_r:+.1f}R')
