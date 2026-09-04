# -*- coding: utf-8 -*-
"""YTC 全自动交易 daemon — 4品种双策略, 信号→下单→移动止损 全自动。

策略:
- 压缩测试 (YTC-C): D1 方向明确 + M5/M15 反转信号在关键位附近 → 风险 1R (1%)
- 标准策略 (YTC-S): D1 方向明确 + H4 共振 + M15 反转信号 + 关键位触碰>=2次 → 风险 2-3R (2-3%)

纪律:
- SL = 关键位外侧 0.1% (软件端设), 不设 TP (移动止损止盈, monitor_trade.py 自动抬)
- 同品种已有持仓(任意方向) → 跳过新信号 (一单原则, 不锁仓)
- 周末 (服务器时间周六/周日) 不开新仓
- 信号 30 分钟冷却 + orders_log 防重复下单 (跨重启)

依赖: bridge 127.0.0.1:8080, EA 写 market_kline_* / symbol_specs.txt / account_info.txt
用法: python autotrade_daemon.py
"""
import struct, os, datetime, time, sys, json, urllib.request, subprocess

BASE = r'C:\Program Files (x86)\Alpari MT4\history\Alpari-Demo'
FILES = r'C:\Program Files (x86)\Alpari MT4\MQL4\Files'
SYMS = ['XAUUSD', 'XAGUSD', 'WTI', 'BITCOIN']
ROUND = {'BITCOIN': 2000, 'XAUUSD': 50, 'XAGUSD': 1, 'WTI': 5}
RISK_C = 1.0          # 压缩测试风险 % (1R)
RISK_S = 2.0          # 标准策略风险 % (2R)
RISK_S_STRONG = 3.0   # 标准策略 H4 强趋势时 % (3R)
SL_BUF = 0.001        # SL 缓冲: 关键位外侧 0.1%
TOUCH_MIN = 2         # 标准版关键位最少触碰次数 (400根H1内)
API = 'http://127.0.0.1:8080'
HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, 'autotrade_log.txt')
CACHE = os.path.join(HERE, 'notify_cache.json')
ORDERS = os.path.join(HERE, 'orders_log.json')


def log(s):
    line = f'[{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {s}'
    print(line, flush=True)
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


# ---------- 数据读取 (与 scan_live_daemon 一致) ----------
def read_hst(name):
    p = os.path.join(BASE, name)
    if not os.path.exists(p):
        return None
    sz = os.path.getsize(p)
    n = (sz - 148) // 60
    bars = []
    with open(p, 'rb') as f:
        f.seek(148)
        for i in range(n):
            rec = f.read(60)
            if len(rec) < 60:
                break
            t, o, h, l, c = struct.unpack('<qdddd', rec[:40])
            bars.append((t, o, h, l, c))
    bars.sort()
    return bars


def read_kline(sym, tf):
    p = os.path.join(FILES, f'market_kline_{sym}_{tf}.txt')
    if not os.path.exists(p):
        return None
    bars = []
    with open(p) as f:
        for line in f:
            parts = line.split(',')
            if len(parts) < 5:
                continue
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
            if cur:
                out.append(tuple(cur))
            cur = [bucket, o, h, l, c]
        else:
            cur[2] = max(cur[2], h)
            cur[3] = min(cur[3], l)
            cur[4] = c
    if cur:
        out.append(tuple(cur))
    return out


def fractals(bars, k=2):
    highs, lows = [], []
    for i in range(k, len(bars) - k):
        seg_h = [bars[j][2] for j in range(i - k, i + k + 1)]
        seg_l = [bars[j][3] for j in range(i - k, i + k + 1)]
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
    hh = len(vals_h) >= 2 and all(vals_h[i] > vals_h[i - 1] for i in range(1, len(vals_h)))
    hl = len(vals_l) >= 2 and all(vals_l[i] > vals_l[i - 1] for i in range(1, len(vals_l)))
    lh = len(vals_h) >= 2 and all(vals_h[i] < vals_h[i - 1] for i in range(1, len(vals_h)))
    ll = len(vals_l) >= 2 and all(vals_l[i] < vals_l[i - 1] for i in range(1, len(vals_l)))
    if hh and hl:
        return 'BULL'
    if lh and ll:
        return 'BEAR'
    if hh or hl:
        return 'BULLISH-BIAS'
    if lh or ll:
        return 'BEARISH-BIAS'
    return 'CHOP'


def bull_sig(b0, b1):
    o, h, l, c = b1[1], b1[2], b1[3], b1[4]
    p_o, p_h, p_l, p_c = b0[1], b0[2], b0[3], b0[4]
    body = c - o
    if body > 0 and p_c < p_o and c >= p_o and o <= p_c:
        return 'ENGULF'
    lower = min(o, c) - l
    upper = h - max(o, c)
    if lower >= 2 * abs(body) and upper <= lower:
        return 'PINBAR'
    if p_h >= h and p_l <= l and c > p_h:
        return 'IB-BO'
    return None


def bear_sig(b0, b1):
    o, h, l, c = b1[1], b1[2], b1[3], b1[4]
    p_o, p_h, p_l, p_c = b0[1], b0[2], b0[3], b0[4]
    body = c - o
    if body < 0 and p_c > p_o and c <= p_o and o >= p_c:
        return 'ENGULF'
    upper = h - max(o, c)
    lower = min(o, c) - l
    if upper >= 2 * abs(body) and lower <= upper:
        return 'PINBAR'
    if p_h >= h and p_l <= l and c < p_l:
        return 'IB-BO'
    return None


def count_touches(h1, lv, tol=0.002):
    """最近 400 根 H1 中触及 lv±tol 的K线数"""
    n = 0
    for b in h1[-400:]:
        if b[3] <= lv * (1 + tol) and b[2] >= lv * (1 - tol):
            n += 1
    return n


def scan_once():
    """返回信号列表: (sym, dir, sig, lv, kind, tf, strategy)"""
    signals = []
    for sym in SYMS:
        d1 = read_hst(f'{sym}1440.hst')
        h4f = read_hst(f'{sym}240.hst')
        h1 = read_hst(f'{sym}60.hst')
        if h4f is None and h1:
            h4f = resample(h1, 14400, h1[0][0] - h1[0][0] % 14400)
        m15 = read_kline(sym, 'M15')
        m5 = read_kline(sym, 'M5')
        if not d1 or not h4f or not m15:
            continue

        d1_closes = [b[4] for b in d1]
        d1_ma = sum(d1_closes[-200:]) / 200
        d1_swh, d1_swl = fractals(d1, k=2)
        d1_s = classify(d1_swh, d1_swl, d1[-1][0])
        d1_above = d1_closes[-1] > d1_ma
        d1_dir = 'BULL' if (d1_above and d1_s in ('BULL', 'BULLISH-BIAS')) else (
            'BEAR' if (not d1_above and d1_s in ('BEAR', 'BEARISH-BIAS')) else 'CHOP')

        h4_closes = [b[4] for b in h4f]
        h4_ma = sum(h4_closes[-200:]) / 200
        h4_swh, h4_swl = fractals(h4f, k=2)
        h4_s = classify(h4_swh, h4_swl, h4f[-1][0])
        h4_above = h4_closes[-1] > h4_ma
        h4_dir = 'BULL' if (h4_above and h4_s in ('BULL', 'BULLISH-BIAS')) else (
            'BEAR' if (not h4_above and h4_s in ('BEAR', 'BEARISH-BIAS')) else 'CHOP')

        price = m15[-1][4]
        now = m15[-1][0]
        levels = {}
        for t, v in d1_swh:
            if now - 30 * 86400 <= t <= now:
                levels[('D1-H', t, v)] = v
        for t, v in d1_swl:
            if now - 30 * 86400 <= t <= now:
                levels[('D1-L', t, v)] = v
        for t, v in h4_swh:
            if now - 14 * 86400 <= t <= now:
                levels[('H4-H', t, v)] = v
        for t, v in h4_swl:
            if now - 14 * 86400 <= t <= now:
                levels[('H4-L', t, v)] = v
        base = int(price // ROUND[sym]) * ROUND[sym]
        for r in [base - 2 * ROUND[sym], base - ROUND[sym], base, base + ROUND[sym], base + 2 * ROUND[sym]]:
            levels[('RND', 0, float(r))] = float(r)

        tol = 0.002
        near = []
        for (key, lv) in sorted(levels.items(), key=lambda x: x[1]):
            for b in m15[-2:]:
                if b[3] <= lv * (1 + tol) and b[2] >= lv * (1 - tol):
                    near.append((key, lv))
                    break

        def check(sig_dir, tf_bars, tf_name, b0, b1):
            for (key, lv) in near:
                if sig_dir == 'BUY' and lv <= b1[4] and d1_dir == 'BULL':
                    s = bull_sig(b0, b1)
                    if s:
                        strat = 'C'
                        if tf_name == 'M15' and h4_dir == d1_dir and h4_dir != 'CHOP' and h1 is not None:
                            if count_touches(h1, lv) >= TOUCH_MIN:
                                strat = 'S'
                        signals.append((sym, 'BUY', s, lv, key[0], tf_name, strat, b1[4]))
                if sig_dir == 'SELL' and lv >= b1[4] and d1_dir == 'BEAR':
                    s = bear_sig(b0, b1)
                    if s:
                        strat = 'C'
                        if tf_name == 'M15' and h4_dir == d1_dir and h4_dir != 'CHOP' and h1 is not None:
                            if count_touches(h1, lv) >= TOUCH_MIN:
                                strat = 'S'
                        signals.append((sym, 'SELL', s, lv, key[0], tf_name, strat, b1[4]))

        check('BUY', m15, 'M15', m15[-2], m15[-1])
        check('SELL', m15, 'M15', m15[-2], m15[-1])
        if m5 and len(m5) >= 3:
            check('BUY', m5, 'M5', m5[-2], m5[-1])
            check('SELL', m5, 'M5', m5[-2], m5[-1])
    return signals


# ---------- 账户/规格/持仓 ----------
def read_specs():
    """解析 symbol_specs.txt → {sym: {'per_point','min_lot','lot_step'}}"""
    out = {}
    p = os.path.join(FILES, 'symbol_specs.txt')
    if not os.path.exists(p):
        return out
    with open(p) as f:
        for line in f:
            if ':' not in line or 'Lotsize' not in line:
                continue
            name, rest = line.split(':', 1)
            d = {}
            for kv in rest.split():
                if '=' in kv:
                    k, v = kv.split('=')
                    try:
                        d[k] = float(v)
                    except ValueError:
                        pass
            if d.get('TickValue') and d.get('TickSize') and d.get('TickSize', 1) > 0:
                out[name.strip()] = {
                    'per_point': d['TickValue'] / d['TickSize'],
                    'min_lot': d.get('MinLot', 0.01),
                    'lot_step': d.get('LotStep', 0.01),
                }
    return out


def get_balance():
    """读账户余额 (EA 每秒重写文件, 读冲突时重试; 失败再走 bridge API)"""
    p = os.path.join(FILES, 'account_info.txt')
    for _ in range(8):
        try:
            with open(p) as f:
                for line in f:
                    if line.startswith('Balance='):
                        return float(line.split('=')[1].strip())
        except Exception:
            time.sleep(0.3)
    try:
        with urllib.request.urlopen(API + '/api/account', timeout=8) as r:
            data = json.loads(r.read().decode())
        return float(data.get('balance', 0.0))
    except Exception:
        return 0.0


def api_positions():
    try:
        with urllib.request.urlopen(API + '/api/positions', timeout=10) as r:
            data = json.loads(r.read().decode())
        return data.get('positions', [])
    except Exception:
        return []


def holdings():
    out = {}
    for p in api_positions():
        if p.get('Ticket'):
            out[str(p.get('Symbol'))] = str(p.get('Type'))
    return out


def is_weekend():
    """服务器时间(本地-5h) 周六/周日 → 不开新仓"""
    try:
        srv = datetime.datetime.utcnow() + datetime.timedelta(hours=3)
    except Exception:
        srv = datetime.datetime.now() - datetime.timedelta(hours=5)
    return srv.weekday() >= 5


def place_order(sym, op, lots, sl, comment):
    """下单并轮询确认 (order_result.txt 覆盖式; EA 明确失败立刻返回; 超时查 positions 兜底)"""
    pos_before = api_positions()
    tickets_before = {str(p.get('Ticket')) for p in pos_before if p.get('Ticket')}
    try:
        with open(os.path.join(FILES, 'order_result.txt')) as f:
            before = f.read()
    except Exception:
        before = ''
    payload = {'symbol': sym, 'operation': op, 'lots': lots, 'stop_loss': sl,
               'take_profit': 0, 'comment': comment}
    req = urllib.request.Request(API + '/api/order', data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read().decode())
    # 轮询 order_result.txt (最多 12s): 内容变化即解析, 无论成败
    for _ in range(12):
        time.sleep(1)
        try:
            with open(os.path.join(FILES, 'order_result.txt')) as f:
                cur = f.read()
        except Exception:
            continue
        if cur != before:
            import re
            if '"success": true' in cur and '"ticket"' in cur:
                m_t = re.search(r'"ticket":\s*(\d+)', cur)
                m_p = re.search(r'"price":\s*([\d.]+)', cur)
                return True, int(m_t.group(1)) if m_t else 0, float(m_p.group(1)) if m_p else 0.0, cur
            return False, 0, 0.0, cur   # EA 明确失败 (如 4109)
    # 超时未确认: 查 positions 是否有新增持仓 (下单可能成功但结果文件被覆盖)
    for p in api_positions():
        t = str(p.get('Ticket'))
        if p.get('Symbol') == sym and t and t not in tickets_before:
            return True, int(t), float(p.get('OpenPrice', 0.0)), 'confirmed via positions'
    return False, 0, 0.0, str(resp)[:200] + ' | timeout'


def start_monitor(ticket, open_p, sl, r_size):
    """启动移动止损监控 (带随机错峰延迟, 防 /api/modify 并发竞态)"""
    delay = int(time.time()) % 6
    time.sleep(delay)
    subprocess.Popen(['python', os.path.join(HERE, 'monitor_trade.py'),
                      str(ticket), f'{open_p:.2f}', f'{sl:.2f}', f'{r_size:.2f}'])


def notify(title, msg):
    try:
        subprocess.Popen(['python', os.path.join(HERE, 'notify_popup.py'), title, msg, '3'])
    except Exception:
        pass


# ---------- 主循环 ----------
def main():
    notified = {}
    try:
        with open(CACHE) as f:
            notified = json.load(f)
    except Exception:
        pass
    orders = {}
    try:
        with open(ORDERS) as f:
            orders = json.load(f)
    except Exception:
        pass
    fail_cooldown = {}

    log('autotrade daemon started: 4 symbols, dual strategy (C=1R / S=2-3R), full auto')
    specs = read_specs()
    for s in SYMS:
        if s not in specs:
            log(f'WARN: {s} 合约规格缺失 (symbol_specs.txt) — 该品种暂不交易, 等待 EA 重挂生成')
        else:
            log(f'  {s} spec: per_point={specs[s]["per_point"]:.4f} min_lot={specs[s]["min_lot"]} lot_step={specs[s]["lot_step"]}')

    while True:
        try:
            t_now = time.time()
            sigs = scan_once()
            holds = holdings()
            bal = get_balance()
            specs = read_specs()  # 每轮重读, 规格文件更新后自动生效
            placed_round = set()  # 本轮已下单 (sym, dir) — 防同轮多信号重复开仓
            # 下单失败冷却: 失败后该品种 10 分钟内不再尝试 (防骚扰服务器)
            for k in list(fail_cooldown.keys()):
                if t_now - fail_cooldown[k] > 600:
                    del fail_cooldown[k]

            # ---- 平仓事件检测: orders_log 中的 ticket 消失 → 平仓, 记录+通知+释放 ----
            try:
                with open(ORDERS) as f:
                    orders_now = json.load(f)
            except Exception:
                orders_now = {}
            open_tickets = {str(p.get('Ticket')) for p in api_positions() if p.get('Ticket')}
            for k, o in list(orders_now.items()):
                t = str(o.get('ticket'))
                if t and t not in open_tickets:
                    log(f'CLOSED: {o.get("sym")} {o.get("dir")} ticket={t} (SL或手动平仓, 开仓@{o.get("price")} SL={o.get("sl")})')
                    notify('YTC 持仓平仓', f'{o.get("sym")} {o.get("dir")} ticket={t}\n开仓@{o.get("price")} SL={o.get("sl")}')
                    del orders_now[k]
            try:
                with open(ORDERS, 'w') as f:
                    json.dump(orders_now, f, ensure_ascii=False, indent=1)
            except Exception:
                pass
            orders = orders_now

            for s in sigs:
                sym, d, sig, lv, kind, tf, strat, price = s
                # 同品种已有持仓(任意方向) → 跳过
                if sym in holds:
                    log(f'SKIP {sym} {d} {sig}@{lv:.0f} ({strat}): 已有持仓 {holds[sym]}')
                    continue
                if (sym, d) in placed_round:
                    log(f'SKIP {sym} {d} {sig}@{lv:.0f} ({strat}): 本轮已开仓, 防重复')
                    continue
                if sym not in specs:
                    log(f'SKIP {sym} {d} {sig}@{lv:.0f}: 无合约规格')
                    continue
                if is_weekend():
                    log(f'SKIP {sym} {d} {sig}@{lv:.0f}: 周末不开新仓')
                    continue
                # 下单失败冷却 (10 分钟)
                if sym in fail_cooldown:
                    log(f'SKIP {sym} {d} {sig}@{lv:.0f}: 下单失败冷却中 (4109 问题待解决)')
                    continue
                # 同方向 30 分钟冷却 (按 品种+方向, 防同方向多信号连续开仓)
                dir_key = f'{sym}|{d}'
                if dir_key in notified and t_now - notified[dir_key] < 1800:
                    continue
                # 追价过滤: 现价偏离关键位 > 0.3% → 信号已滞后, 放弃
                dev = abs(price - lv) / lv
                if dev > 0.003:
                    log(f'SKIP {sym} {d} {sig}@{lv:.0f} ({strat}): 追价 {dev*100:.2f}% > 0.3%, 放弃')
                    continue
                key = f'{sym}|{d}|{sig}|{lv:.0f}|{tf}'
                if key in notified and t_now - notified[key] < 1800:
                    continue
                if key in orders:
                    continue
                notified[key] = t_now
                notified[dir_key] = t_now

                # 仓位: 风险 = 1R(压缩) / 2R-3R(标准, H4强趋势3R)
                risk_pct = RISK_C if strat == 'C' else (RISK_S_STRONG if tf == 'M15' else RISK_S)
                # (标准版只由 M15 信号产生, 此处留兜底)
                sp = specs[sym]
                entry_ref = lv
                sl = entry_ref * (1 - SL_BUF) if d == 'BUY' else entry_ref * (1 + SL_BUF)
                # 手数按真实止损距离 (现价→SL) 计算
                sl_dist = abs(price - sl)
                risk_money = bal * risk_pct / 100.0
                lots = risk_money / (sl_dist * sp['per_point']) if sl_dist * sp['per_point'] > 0 else sp['min_lot']
                lots = int(lots / sp['lot_step']) * sp['lot_step']
                lots = max(lots, sp['min_lot'])

                comment = f'YTC-{strat}'
                ok, ticket, price, raw = place_order(sym, d, lots, round(sl, 2), comment)
                if ok and ticket:
                    placed_round.add((sym, d))
                    orders[key] = {'ticket': ticket, 'time': datetime.datetime.now().isoformat(),
                                   'sym': sym, 'dir': d, 'strat': strat, 'lots': lots,
                                   'sl': round(sl, 2), 'price': price}
                    with open(ORDERS, 'w') as f:
                        json.dump(orders, f, ensure_ascii=False, indent=1)
                    r_size = abs(price - sl)
                    start_monitor(ticket, price, sl, r_size)
                    log(f'>>> ORDER OK: {sym} {d} {lots}手 @{price} SL={sl:.2f} ({strat}, {risk_pct}%风险) ticket={ticket}')
                    notify(f'YTC 自动开仓', f'{sym} {d} {lots}手 @{price}\nSL={sl:.2f} ({strat}) ticket={ticket}')
                else:
                    fail_cooldown[sym] = time.time()
                    log(f'ORDER FAIL: {sym} {d} {lots}手 SL={sl:.2f} resp={raw}')
                    notify(f'YTC 下单失败', f'{sym} {d}\n{raw[:120]}')
            try:
                with open(CACHE, 'w') as f:
                    json.dump(notified, f)
            except Exception:
                pass
        except Exception as e:
            log(f'error: {e}')
        time.sleep(30)


if __name__ == '__main__':
    main()
