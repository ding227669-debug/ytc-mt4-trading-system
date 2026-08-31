# -*- coding: utf-8 -*-
"""YTC live daemon: scan all 4 symbols every 30s, notify on signal.
Prints '>>> SIGNAL ...' when a setup fires (watch_patterns can catch it).
Usage: python scan_live_daemon.py
"""
import struct, os, datetime, time, sys

BASE = r'C:\Program Files (x86)\Alpari MT4\history\Alpari-Demo'
FILES = r'C:\Program Files (x86)\Alpari MT4\MQL4\Files'
SYMS = ['XAUUSD', 'XAGUSD', 'WTI', 'BITCOIN']
ROUND = {'BITCOIN': 2000, 'XAUUSD': 50, 'XAGUSD': 1, 'WTI': 5}

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

def read_kline(sym, tf):
    p = os.path.join(FILES, f'market_kline_{sym}_{tf}.txt')
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

_cache = {}
def scan_once():
    signals = []
    for sym in SYMS:
        d1 = read_hst(f'{sym}1440.hst')
        h4f = read_hst(f'{sym}240.hst')
        h1 = read_hst(f'{sym}60.hst')
        if h4f is None and h1:
            h4f = resample(h1, 14400, h1[0][0] - h1[0][0] % 14400)
        m15 = read_kline(sym, 'M15')
        m5 = read_kline(sym, 'M5')
        if not d1 or not h4f or not m15: continue

        d1_closes = [b[4] for b in d1]
        d1_ma = sum(d1_closes[-200:]) / 200
        d1_swh, d1_swl = fractals(d1, k=2)
        d1_s = classify(d1_swh, d1_swl, d1[-1][0])
        d1_above = d1_closes[-1] > d1_ma
        d1_dir = 'BULL' if (d1_above and d1_s in ('BULL', 'BULLISH-BIAS')) else ('BEAR' if (not d1_above and d1_s in ('BEAR', 'BEARISH-BIAS')) else 'CHOP')

        h4_closes = [b[4] for b in h4f]
        h4_ma = sum(h4_closes[-200:]) / 200
        h4_swh, h4_swl = fractals(h4f, k=2)
        h4_s = classify(h4_swh, h4_swl, h4f[-1][0])
        h4_above = h4_closes[-1] > h4_ma
        h4_dir = 'BULL' if (h4_above and h4_s in ('BULL', 'BULLISH-BIAS')) else ('BEAR' if (not h4_above and h4_s in ('BEAR', 'BEARISH-BIAS')) else 'CHOP')

        price = m15[-1][4]
        now = m15[-1][0]
        levels = {}
        for t, v in d1_swh:
            if now - 30*86400 <= t <= now: levels[('D1-H', t, v)] = v
        for t, v in d1_swl:
            if now - 30*86400 <= t <= now: levels[('D1-L', t, v)] = v
        for t, v in h4_swh:
            if now - 14*86400 <= t <= now: levels[('H4-H', t, v)] = v
        for t, v in h4_swl:
            if now - 14*86400 <= t <= now: levels[('H4-L', t, v)] = v
        base = int(price // ROUND[sym]) * ROUND[sym]
        for r in [base - 2*ROUND[sym], base - ROUND[sym], base, base + ROUND[sym], base + 2*ROUND[sym]]:
            levels[('RND', 0, float(r))] = float(r)

        tol = 0.002
        near = []
        for (key, lv) in sorted(levels.items(), key=lambda x: x[1]):
            for b in m15[-2:]:
                if b[3] <= lv * (1 + tol) and b[2] >= lv * (1 - tol):
                    near.append((key, lv))
                    break

        for b0, b1 in [(m15[-2], m15[-1])]:
            for (key, lv) in near:
                if lv <= b1[4] and d1_dir == 'BULL':
                    s = bull_sig(b0, b1)
                    if s: signals.append((sym, 'BUY', s, lv, key[0], 'M15'))
                if lv >= b1[4] and d1_dir == 'BEAR':
                    s = bear_sig(b0, b1)
                    if s: signals.append((sym, 'SELL', s, lv, key[0], 'M15'))
        if m5 and len(m5) >= 3:
            for b0, b1 in [(m5[-2], m5[-1])]:
                for (key, lv) in near:
                    if lv <= b1[4] and d1_dir == 'BULL':
                        s = bull_sig(b0, b1)
                        if s: signals.append((sym, 'BUY', s + '!M5', lv, key[0], 'M5'))
                    if lv >= b1[4] and d1_dir == 'BEAR':
                        s = bear_sig(b0, b1)
                        if s: signals.append((sym, 'SELL', s + '!M5', lv, key[0], 'M5'))
    return signals

print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] daemon started: scanning {len(SYMS)} symbols every 30s', flush=True)
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'notify_cache.json')
notified = {}
try:
    import json as _json
    with open(CACHE) as f:
        notified = _json.load(f)
except Exception:
    notified = {}

def holdings():
    try:
        import json as _json, urllib.request
        with urllib.request.urlopen('http://127.0.0.1:8080/api/positions', timeout=10) as r:
            data = _json.loads(r.read().decode())
        return {str(p.get('Symbol')): str(p.get('Type')) for p in data.get('positions', []) if p.get('Ticket')}
    except Exception:
        return {}

while True:
    try:
        sigs = scan_once()
        now_s = datetime.datetime.now().strftime('%H:%M:%S')
        t_now = time.time()
        holds = holdings()
        if sigs:
            new_sigs = []
            for s in sigs:
                sym, d, sig, lv, kind, tf = s
                # skip signal if already holding same direction on same symbol
                if sym in holds:
                    hold_dir = holds[sym]
                    if (d == 'BUY' and hold_dir == 'BUY') or (d == 'SELL' and hold_dir == 'SELL'):
                        continue
                key = f'{sym}|{d}|{sig}|{lv:.0f}|{tf}'
                if key in notified and t_now - notified[key] < 1800:
                    continue   # already notified within 30 min
                notified[key] = t_now
                new_sigs.append(s)
            try:
                import json as _json
                with open(CACHE, 'w') as f:
                    _json.dump(notified, f)
            except Exception:
                pass
            for s in new_sigs:
                line = f'[{now_s}] >>> SIGNAL: {s[0]} {s[1]} {s[2]} at level {s[3]:.2f} ({s[4]}) tf={s[5]} price={read_kline(s[0], "M15")[-1][4]:.2f}'
                print(line, flush=True)
                try:
                    import subprocess
                    popup_msg = f'{s[0]} {s[1]} 信号: {s[2]}\n关键位: {s[3]:.2f} ({s[4]})\n周期: {s[5]}  现价: {read_kline(s[0], "M15")[-1][4]:.2f}'
                    subprocess.Popen(['python', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'notify_popup.py'),
                                      'YTC 信号提醒', popup_msg, '3'])
                except Exception:
                    pass
        else:
            t = int(time.time())
            if t % 600 < 30:   # probe every ~10 min
                print(f'[{now_s}] probe: no signal (4 symbols checked)', flush=True)
    except Exception as e:
        print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] error: {e}', flush=True)
    time.sleep(30)
