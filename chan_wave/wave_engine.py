# -*- coding: utf-8 -*-
"""
wave_engine.py — 艾略特波浪自动数浪引擎（周线级别）
====================================================
所属系统：缠论+波浪共振交易信号系统（模拟盘研究项目，禁止自动下单，只做信号计算）

职责：
  * 输入已闭合K线序列（时间升序，元素: time/open/high/low/close/vol），输出标准化 wave_result
  * 自动数浪：分形摆动点序列 -> 推动浪 1-2-3-4-5 + 调整浪 A-B-C
  * 硬铁律校验（违反即该标注无效）：
      R1 3浪不能是最短推动浪（严格比较 1/3/5 浪长度）
      R2 4浪回调不能进入1浪价格区间（上升浪: 4浪低点 > 1浪高点）
      R3 2浪不能破1浪起点（上升浪: 2浪低点 > 1浪起点）
  * 浪号锁定（防抖动）：摆动结构/关键位未变化时沿用上次标注，不逐根全量重数

输出契约（与 chan_engine 集成用，勿改字段名）：
  wave_result = {
    'level': 'WEEK',
    'wave_label': '3',        # None/'1'..'5'/'A'/'B'/'C'，当前处于哪一段
    'wave_status': 'RUNNING', # COMPLETE(当前浪已走完)/RUNNING(正在运行)/UNCERTAIN(无法可靠数浪)
    'bias': 'BULL',           # BULL/BEAR/NEUTRAL，大方向
    'wave_broken': False,     # 浪型结构是否被价格击穿破坏
    'details': {'swings': [...], 'label_checks': {...}, 'last_close': float,
                'atr': float, 'bars_count': int, 'last_range': float}
    # bars_count: 输入K线根数; last_range: 最近一根K线真实波幅(high-low)
    # —— 供 daemon.compute_volatility 的异常波动率开关(abnormal)判定使用
  }

禁止：本模块不含任何下单/交易功能。
"""

import os
import sys
import struct
import datetime

# ======================================================================
# 参数区（集中于此，便于后续标定 —— 4 品种交叉验证 + 前向验证调整）
# ======================================================================
SWING_N = 5             # 分形确认所需左右K线数（K线 i 为分形高点: 左右各N根中最高）
SWING_ATR_MULT = 1.0    # 摆动过滤系数：相邻摆动幅度 < ATR * 该系数 视为噪声剔除
ATR_PERIOD = 14         # ATR 周期（Wilder 平滑）
WAVE_WINDOW = 14        # 数浪时只分析最近 WAVE_WINDOW 个摆动点（聚焦当前结构）
MAX_SWINGS_KEEP = 60    # details.swings 最多保留的摆动点数
HST_BASE = r"C:\Program Files (x86)\Alpari MT4\history\Alpari-Demo"  # .hst 数据目录

# ======================================================================
# .hst 数据读取（格式与 backtest/fvg_backtest.py、autotrade_daemon.py 一致）
#   文件头 148 字节；每条记录 60 字节：
#     前 40 字节 = <qdddd  (time, open, high, low, close)
#     40-48 字节 = 成交量 (long long)
#     48-60 字节 = spread + 填充（忽略）
# ======================================================================
def load_hst(path):
    """读取 MT4 .hst 历史文件 -> candles 列表（时间升序，dict 元素）。"""
    if not os.path.exists(path):
        return None
    sz = os.path.getsize(path)
    n = (sz - 148) // 60
    bars = []
    with open(path, 'rb') as f:
        f.seek(148)
        for _ in range(n):
            rec = f.read(60)
            if len(rec) < 60:
                break
            t, o, h, l, c = struct.unpack('<qdddd', rec[:40])
            try:
                vol = struct.unpack('<q', rec[40:48])[0]
            except struct.error:
                vol = 0
            bars.append({'time': int(t), 'open': o, 'high': h, 'low': l,
                         'close': c, 'vol': float(vol)})
    bars.sort(key=lambda x: x['time'])
    return bars


def resample(candles, target_minutes):
    """低周期 -> 高周期聚合（自测用，如日线 -> 周线）。

    target_minutes: 目标周期分钟数（WEEK=10080）。
    """
    step = target_minutes * 60
    out = []
    for c in candles:
        bucket = c['time'] - c['time'] % step
        if out and out[-1]['time'] == bucket:
            out[-1]['high'] = max(out[-1]['high'], c['high'])
            out[-1]['low'] = min(out[-1]['low'], c['low'])
            out[-1]['close'] = c['close']
            out[-1]['vol'] += c['vol']
        else:
            out.append({'time': bucket, 'open': c['open'], 'high': c['high'],
                        'low': c['low'], 'close': c['close'], 'vol': c['vol']})
    return out


# ======================================================================
# 技术指标
# ======================================================================
def compute_atr(candles, period=ATR_PERIOD):
    """ATR（Wilder 平滑）。K线不足时退回简单平均波幅。"""
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        tr = max(c['high'] - c['low'],
                 abs(c['high'] - p['close']),
                 abs(c['low'] - p['close']))
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / len(trs)
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


# ======================================================================
# 摆动点（swing points）识别
# ======================================================================
def _merge_same_type(items, candles):
    """合并连续同类型摆动：高点取更高、低点取更低（保留更晚位置）。"""
    out = []
    for i, t in items:
        if out and out[-1][1] == t:
            pi = out[-1][0]
            if t == 'H' and candles[i]['high'] >= candles[pi]['high']:
                out[-1] = (i, t)
            elif t == 'L' and candles[i]['low'] <= candles[pi]['low']:
                out[-1] = (i, t)
        else:
            out.append((i, t))
    return out


def _filter_small_swings(items, candles, min_amp):
    """剔除幅度 < min_amp 的相邻摆动（保留更早确认的点，跳过过近的点）。"""
    out = []
    i = 0
    while i < len(items):
        j = i + 1
        while j < len(items):
            a, b = items[i], items[j]
            pa = candles[a[0]]['high'] if a[1] == 'H' else candles[a[0]]['low']
            pb = candles[b[0]]['high'] if b[1] == 'H' else candles[b[0]]['low']
            if abs(pb - pa) < min_amp:
                j += 1
            else:
                break
        out.append(items[i])
        i = j
    return out


def find_fractal_swings(candles, n=SWING_N, atr=None, atr_mult=SWING_ATR_MULT):
    """分形摆动点序列（升序、高低交替）。

    步骤：
      1) 分形：K线 i 的高点是 [i-n, i+n] 区间最高 -> 分形高点('H')；低点对称('L')
      2) 合并连续同类型摆动（高点取更高、低点取更低）
      3) ATR 幅度过滤：相邻摆动幅度 < ATR*atr_mult 视为噪声剔除，再合并一次
    返回 [{'idx','time','price','type'}, ...]
    """
    if atr is None:
        atr = compute_atr(candles, ATR_PERIOD)
    if atr <= 0:
        atr = 1e-9
    L = len(candles)
    # 1) 原始分形（最后确认的摆动点 idx <= L-1-n，保证左右各 n 根已闭合）
    raw = []
    for i in range(n, L - n):
        c = candles[i]
        is_high = True
        is_low = True
        for j in range(i - n, i + n + 1):
            if j == i:
                continue
            if candles[j]['high'] >= c['high']:
                is_high = False
            if candles[j]['low'] <= c['low']:
                is_low = False
            if not is_high and not is_low:
                break
        if is_high:
            raw.append((i, 'H'))
        if is_low:
            raw.append((i, 'L'))
    # 2) 合并同类型
    merged = _merge_same_type(raw, candles)
    # 3) ATR 幅度过滤
    merged = _filter_small_swings(merged, candles, atr * atr_mult)
    merged = _merge_same_type(merged, candles)
    return [{'idx': i,
             'time': candles[i]['time'],
             'price': candles[i]['high'] if t == 'H' else candles[i]['low'],
             'type': t}
            for i, t in merged]


# ======================================================================
# 数浪核心：铁律检查 + 标注
# ======================================================================
def _check_impulse(sw, i, up, last_close):
    """检查 sw[i:i+6] 是否构成合法完整5浪（0-1-2-3-4-5）。

    返回 {'pts','up','checks','extending'} 或 None（任一铁律违反）。
    """
    pts = sw[i:i + 6]
    if len(pts) < 6:
        return None
    expect = ['L', 'H', 'L', 'H', 'L', 'H'] if up else ['H', 'L', 'H', 'L', 'H', 'L']
    for p, e in zip(pts, expect):
        if p['type'] != e:
            return None
    p = [x['price'] for x in pts]
    # 价格单调结构（高低交替推进）
    if up:
        if not (p[1] > p[0] and p[2] < p[1] and p[3] > p[2] and p[4] < p[3] and p[5] > p[4]):
            return None
    else:
        if not (p[1] < p[0] and p[2] > p[1] and p[3] < p[2] and p[4] > p[3] and p[5] < p[4]):
            return None
    len1 = abs(p[1] - p[0])
    len3 = abs(p[3] - p[2])
    len5_full = abs(p[5] - p[4])
    is_last = (i + 5 == len(sw) - 1)      # 5浪终点是否最新摆动
    extending = (last_close > p[5]) if up else (last_close < p[5])
    checks = {'r1': None, 'r2': None, 'r3': None, 'note': None}
    if is_last and extending:
        # R1: 5浪延伸中，len5 未走完 -> 只严格检查 len3 > len1
        if len3 <= len1:
            return None
        checks['r1'] = True
        checks['note'] = 'wave5 extending, len5 not judged'
    else:
        # R1: 3浪不能是最短推动浪（严格比较 1/3/5 浪长度）
        if len3 <= len1 or len3 <= len5_full:
            return None
        checks['r1'] = True
    # R2: 4浪回调不能进入1浪价格区间
    if up:
        if not (p[4] > p[1]):
            return None
    else:
        if not (p[4] < p[1]):
            return None
    checks['r2'] = True
    # R3: 2浪不能破1浪起点
    if up:
        if not (p[2] > p[0]):
            return None
    else:
        if not (p[2] < p[0]):
            return None
    checks['r3'] = True
    return {'pts': pts, 'up': up, 'checks': checks, 'extending': extending}


def _check_partial(sw, i, up, last_close):
    """检查从 i 起的部分推动浪（已确认到 1-2 / 1-2-3 / 1-2-3-4）。

    返回 {'pts','up','npts','label','checks'} 或 None。
      npts: 已确认浪点数（1..4）；label: 当前所处浪。
    """
    # k = 摆动点数：5 -> 1-2-3-4 确认；4 -> 1-2-3；3 -> 1-2；2 -> 1
    for k in (5, 4, 3, 2):
        if i + k > len(sw):
            continue
        pts = sw[i:i + k]
        expect = (['L', 'H', 'L', 'H', 'L'][:k] if up else ['H', 'L', 'H', 'L', 'H'][:k])
        if any(p['type'] != e for p, e in zip(pts, expect)):
            continue
        p = [x['price'] for x in pts]
        # 价格单调结构
        ok_price = True
        for j in range(k - 1):
            if up:
                if j % 2 == 0:
                    if not (p[j + 1] > p[j]):
                        ok_price = False
                        break
                else:
                    if not (p[j + 1] < p[j]):
                        ok_price = False
                        break
            else:
                if j % 2 == 0:
                    if not (p[j + 1] < p[j]):
                        ok_price = False
                        break
                else:
                    if not (p[j + 1] > p[j]):
                        ok_price = False
                        break
        if not ok_price:
            continue
        # R3: 2浪不破1浪起点（k>=3 时已可检查）
        if k >= 3:
            if up:
                if not (p[2] > p[0]):
                    continue
            else:
                if not (p[2] < p[0]):
                    continue
        # R2: 4浪不进入1浪区间（k>=5 时已可检查）
        if k >= 5:
            if up:
                if not (p[4] > p[1]):
                    continue
            else:
                if not (p[4] < p[1]):
                    continue
        npts = k - 1
        # ---- 判定当前所处浪 ----
        if k == 5:      # 1-2-3-4 已确认
            if up:
                cur = '5' if last_close >= p[4] else '4'
            else:
                cur = '5' if last_close <= p[4] else '4'
            if cur == '4':   # 4浪进行中已破1浪顶 -> 候选失效
                if up and last_close < p[1]:
                    continue
                if (not up) and last_close > p[1]:
                    continue
        elif k == 4:    # 1-2-3 已确认，当前在 4 浪
            cur = '4'
            if up and last_close < p[1]:
                continue
            if (not up) and last_close > p[1]:
                continue
        elif k == 3:    # 1-2 已确认，当前在 3 浪
            cur = '3'
        else:           # k == 2，1 浪顶已确认
            if up:
                cur = '1' if last_close >= p[1] else '2'
            else:
                cur = '1' if last_close <= p[1] else '2'
            if cur == '2':   # 2浪进行中已破1浪起点 -> 候选失效
                if up and last_close < p[0]:
                    continue
                if (not up) and last_close > p[0]:
                    continue
        checks = {'r1': None, 'r2': (True if k >= 5 else None),
                  'r3': (True if k >= 3 else None), 'note': None}
        return {'pts': pts, 'up': up, 'npts': npts, 'label': cur, 'checks': checks}
    return None


def _check_abc(sw, e, up, last_close):
    """5浪终点在 sw[e]（up 时为高点），检查其后摆动是否为 A-B-C 调整浪。

    返回 {'pts','up','label','status','bias','checks'} 或 None。
    """
    p5 = sw[e]
    after = sw[e + 1:]
    if not after:
        return None
    a = after[0]
    # A 浪方向与 5 浪相反
    if up:
        if a['type'] != 'L' or a['price'] >= p5['price']:
            return None
    else:
        if a['type'] != 'H' or a['price'] <= p5['price']:
            return None
    n = len(after)
    # B 浪合法性：B 不破 A 起点（5浪终点）
    if n >= 2:
        b = after[1]
        if up:
            if b['type'] != 'H' or b['price'] > p5['price']:
                return None
        else:
            if b['type'] != 'L' or b['price'] < p5['price']:
                return None
    # C 浪类型检查
    if n >= 3:
        c = after[2]
        if up:
            if c['type'] != 'L':
                return None
        else:
            if c['type'] != 'H':
                return None
    # ---- 当前浪判定 ----
    if n == 1:
        label, status = 'A', 'RUNNING'
    elif n == 2:
        label, status = 'B', 'RUNNING'
    else:
        label, status = 'C', 'RUNNING'
        if n == 3:
            # C 浪结束判定：收盘反向突破 C 终点
            c = after[2]
            if up:
                if last_close > c['price']:
                    status = 'COMPLETE'
            else:
                if last_close < c['price']:
                    status = 'COMPLETE'
        else:
            # C 之后又有新摆动 -> C 已确认走完
            status = 'COMPLETE'
    bias = 'BEAR' if up else 'BULL'
    checks = {'r1': None, 'r2': None, 'r3': None,
              'abc_b_not_break_a': True, 'note': 'correction after wave5'}
    return {'pts': [p5] + after[:3], 'up': up, 'label': label,
            'status': status, 'bias': bias, 'checks': checks}


def _key_levels(label, pts, up):
    """当前浪防守位 + 1浪起点价（用于浪号锁定的破位检查）。

    防守位定义（破位 = 收盘反向击穿防守位）：
      * 推动浪 1/3/5：防守位 = 浪起点（1浪起点/2浪底/4浪底）
      * 回调浪 2/4：  防守位 = 前低/前高（2浪用1浪起点，4浪用2浪底）
        —— 回调浪本身必然跌破前一个推动浪顶/底，不能把浪起点当破位线
      * A/B/C：防守位 = 5浪终点 / A终点 / B终点
    上升结构: 收盘 < 防守位 视为击穿；下降结构对称。
    """
    prices = [p['price'] for p in pts]
    if label in ('A', 'B', 'C'):
        # pts = [5浪终点, A终点, B终点, (C终点)]
        idx = {'A': 0, 'B': 1, 'C': 2}[label]
        return {'broken_level': prices[idx], 'wave1_start': prices[0]}
    idx = {'1': 0, '2': 0, '3': 2, '4': 2, '5': 4}[label]
    return {'broken_level': prices[idx], 'wave1_start': prices[0]}


def _label_waves(swings, candles):
    """在摆动点序列上标注浪型（核心数浪逻辑）。

    返回 (label, status, bias, broken, checks, key_levels)
      label:      None/'1'..'5'/'A'/'B'/'C'
      status:     'RUNNING'/'COMPLETE'/'UNCERTAIN'
      bias:       'BULL'/'BEAR'/'NEUTRAL'
      broken:     bool，浪型是否被价格击穿破坏
      checks:     dict，铁律逐项检查结果
      key_levels: {'broken_level': 当前浪起点价, 'wave1_start': 1浪起点价} 或 None
    """
    last_close = candles[-1]['close']
    if len(swings) < 3:
        # 最新摆动不足以确定浪型 -> 宁可不给方向
        return None, 'UNCERTAIN', 'NEUTRAL', False, {}, None

    win = swings[-WAVE_WINDOW:]
    cands = []
    for up in (True, False):
        # 完整 5 浪（6 个摆动点）
        for i in range(len(win) - 5):
            r = _check_impulse(win, i, up, last_close)
            if r:
                cands.append({'end': i + 5, 'up': up, 'kind': 'imp', 'r': r})
        # 部分标注（1-2 / 1-2-3 / 1-2-3-4）
        for i in range(len(win) - 2):
            r = _check_partial(win, i, up, last_close)
            if r:
                cands.append({'end': i + r['npts'], 'up': up, 'kind': 'part', 'r': r})

    if not cands:
        # 标注候选为空 -> 无法可靠数浪
        return None, 'UNCERTAIN', 'NEUTRAL', False, {}, None

    # 优先取终点（end）最靠后的候选（最近结构优先）
    max_end = max(c['end'] for c in cands)
    top = [c for c in cands if c['end'] == max_end]

    # 方向冲突裁决：优先与最新摆动类型同方向的候选
    last_type = win[-1]['type']
    dir_pref = (last_type == 'H')      # 最新摆动是高点 -> 偏上升结构
    dir_cands = [c for c in top if c['up'] == dir_pref]
    if dir_cands:
        top = dir_cands
    if len({c['up'] for c in top}) > 1:
        # 同终点、方向互相矛盾 -> 多种标注互相矛盾，无法可靠数浪
        return None, 'UNCERTAIN', 'NEUTRAL', False, {}, None

    # 完整度排序：完整5浪 > 部分标注（确认浪数多者优先）
    top.sort(key=lambda c: (100 if c['kind'] == 'imp' else c['r']['npts']),
             reverse=True)
    best = top[0]
    r = best['r']
    up = best['up']
    bias_dir = 'BULL' if up else 'BEAR'

    if best['kind'] == 'imp':
        pts = r['pts']
        e = best['end']
        ext = (last_close > pts[-1]['price']) if up else (last_close < pts[-1]['price'])
        if ext:
            # 5浪仍在延伸（最新收盘创新高/低）-> 5浪 RUNNING
            label, status = '5', 'RUNNING'
            kl = _key_levels('5', pts, up)
        elif e < len(win) - 1:
            # 5浪终点后已有新摆动 -> 尝试 A-B-C 调整浪
            abc = _check_abc(win, e, up, last_close)
            if abc:
                label, status = abc['label'], abc['status']
                bias_dir = abc['bias']
                checks = abc['checks']
                kl = _key_levels(label, abc['pts'], up)
            else:
                label, status = '5', 'COMPLETE'
                kl = _key_levels('5', pts, up)
        else:
            # 5浪终点 = 最新摆动且未延伸 -> 5浪刚走完
            label, status = '5', 'COMPLETE'
            kl = _key_levels('5', pts, up)
    else:
        label, status = r['label'], 'RUNNING'
        kl = _key_levels(label, r['pts'], up)

    # ---- 破位判定：收盘击穿当前浪起点 ----
    broken = False
    if kl:
        if bias_dir == 'BULL':
            broken = last_close < kl['broken_level']
        elif bias_dir == 'BEAR':
            broken = last_close > kl['broken_level']

    return label, status, bias_dir, broken, r['checks'], kl


# ======================================================================
# 对外接口 compute + 状态缓存（浪号锁定）
# ======================================================================
# 模块级缓存：同一数据序列（首时间/末时间/长度唯一标识）增量更新，防抖动
_CACHE = {'key': None, 'result': None, 'last_swing': None,
          'broken_level': None, 'bias': None}


def _empty_result(level):
    """最小结果（数据不足/无法数浪时的兜底）。"""
    return {'level': level,
            'wave_label': None,
            'wave_status': 'UNCERTAIN',
            'bias': 'NEUTRAL',
            'wave_broken': False,
            'details': {'swings': [], 'label_checks': {},
                        'last_close': 0.0, 'atr': 0.0,
                        'bars_count': 0, 'last_range': 0.0}}


def compute(candles, level='WEEK'):
    """主入口：艾略特波浪数浪。

    candles: list[dict]，元素 {'time','open','high','low','close','vol'}，
             时间升序、已闭合K线。
    返回标准化 wave_result 字典（契约见模块 docstring）。
    """
    if not candles or len(candles) < SWING_N * 2 + 2:
        return _empty_result(level)

    key = (candles[0]['time'], candles[-1]['time'], len(candles),
           candles[-1]['close'])
    last_close = candles[-1]['close']

    # 幂等：完全相同的输入直接返回缓存结果
    if _CACHE.get('key') == key:
        return _CACHE['result']

    atr = compute_atr(candles, ATR_PERIOD)
    swings = find_fractal_swings(candles, SWING_N, atr, SWING_ATR_MULT)
    last_swing = swings[-1]['time'] if swings else None

    # ---- 增量路径（浪号锁定）：数据追加、摆动尾部未变、且未破关键位 -> 沿用上次标注 ----
    prev = _CACHE
    data_grew = (prev.get('key') is not None and
                 key[1] > prev['key'][1] and key[2] > prev['key'][2])
    if (data_grew and prev.get('last_swing') == last_swing and
            prev.get('broken_level') is not None and
            prev.get('bias') in ('BULL', 'BEAR')):
        lvl = prev['broken_level']
        if prev['bias'] == 'BULL':
            broke = last_close < lvl
        else:
            broke = last_close > lvl
        if not broke:
            # 结构未变、未破位 -> 沿用上次标注，仅刷新 details
            res = prev['result']
            res['details']['last_close'] = last_close
            res['details']['atr'] = atr
            res['details']['bars_count'] = len(candles)
            res['details']['last_range'] = candles[-1]['high'] - candles[-1]['low']
            res['details']['swings'] = swings[-MAX_SWINGS_KEEP:]
            _CACHE.update({'key': key, 'result': res, 'last_swing': last_swing,
                           'broken_level': lvl, 'bias': prev['bias']})
            return res

    # ---- 全量重算 ----
    label, status, bias, broken, checks, kl = _label_waves(swings, candles)
    details = {
        'swings': swings[-MAX_SWINGS_KEEP:],
        'label_checks': {
            'rule1_wave3_not_shortest': checks.get('r1'),
            'rule2_wave4_not_in_wave1': checks.get('r2'),
            'rule3_wave2_not_break_wave1': checks.get('r3'),
            'abc_b_not_break_a': checks.get('abc_b_not_break_a'),
            'note': checks.get('note'),
            'n_swings': len(swings),
        },
        'last_close': last_close,
        'atr': atr,
        'bars_count': len(candles),
        'last_range': candles[-1]['high'] - candles[-1]['low'],
        'last_swing_time': swings[-1]['time'] if swings else None,
    }
    res = {'level': level,
           'wave_label': label,
           'wave_status': status,
           'bias': bias,
           'wave_broken': broken,
           'details': details}
    _CACHE.update({'key': key, 'result': res, 'last_swing': last_swing,
                   'broken_level': (kl or {}).get('broken_level'),
                   'bias': bias})
    return res


# ======================================================================
# 自测（真实 .hst 数据）
# ======================================================================
def _fmt_time(ts):
    # .hst 时间戳为服务器时间（EET），显示时 +3h 贴近 MT4 时间
    return datetime.datetime.fromtimestamp(ts + 3 * 3600,
                                           datetime.timezone.utc).strftime('%Y-%m-%d')


def _load_weekly(symbol):
    """加载周线：优先 <SYMBOL>10080.hst，缺失则用 1440 日线重采样。"""
    p10080 = os.path.join(HST_BASE, '%s10080.hst' % symbol)
    if os.path.exists(p10080):
        return load_hst(p10080), '10080.hst(原生周线)'
    p1440 = os.path.join(HST_BASE, '%s1440.hst' % symbol)
    if os.path.exists(p1440):
        daily = load_hst(p1440)
        if daily:
            return resample(daily, 10080), '1440.hst 日线重采样 -> 周线'
    return None, None


def _run_selftest():
    """自测：读 BITCOIN / XAUUSD 周线，滚动验证防抖动 + 输出最终 wave_result。"""
    for sym in ('XAUUSD', 'BITCOIN'):
        candles, src = _load_weekly(sym)
        if not candles:
            print('[%s] !! 数据缺失，跳过' % sym)
            continue
        print('=' * 78)
        print('[%s] 周线 %d 根  (%s ~ %s)  来源: %s' %
              (sym, len(candles), _fmt_time(candles[0]['time']),
               _fmt_time(candles[-1]['time']), src))

        # ---- 滚动验证（最近12根，逐根喂入 compute，验证浪号锁定/防抖动）----
        print('  [滚动验证 最近12根] 时间        label status    bias   broken')
        start = len(candles) - 12
        for k in range(start, len(candles)):
            r = compute(candles[:k + 1])
            print('    %s  %-5s %-9s %-6s %s' %
                  (_fmt_time(candles[k]['time']),
                   str(r['wave_label']), r['wave_status'], r['bias'],
                   r['wave_broken']))

        # ---- 最终 wave_result ----
        res = compute(candles)
        d = res['details']
        print('  [最终 wave_result]')
        print('    wave_label=%s  wave_status=%s  bias=%s  wave_broken=%s' %
              (res['wave_label'], res['wave_status'], res['bias'],
               res['wave_broken']))
        print('    last_close=%.2f  atr=%.2f  摆动点数=%d' %
              (d['last_close'], d['atr'], len(d['swings'])))
        lc = d['label_checks']
        print('    label_checks: R1(3浪非最短)=%s  R2(4浪不进1浪)=%s  '
              'R3(2浪不破1浪起点)=%s  note=%s' %
              (lc.get('rule1_wave3_not_shortest'),
               lc.get('rule2_wave4_not_in_wave1'),
               lc.get('rule3_wave2_not_break_wave1'), lc.get('note')))
        print('    最近摆动(时间 价格 类型):')
        for s in d['swings'][-8:]:
            print('      %s  %10.2f  %s' % (_fmt_time(s['time']), s['price'], s['type']))
        print()


if __name__ == '__main__':
    # Windows 控制台中文/UTF-8 输出兼容
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    _run_selftest()
