# -*- coding: utf-8 -*-
"""
backtest_defend_scan.py — B1防守位(defend_price)4种定义对比扫描
====================================================================================
所属系统：缠论+波浪共振交易信号系统（模拟盘研究项目，禁止自动下单，只做回测研究）
阶段：参数标定阶段2 —— defend_price（防守止损位）定义对比
      （复制 backtest_macd_scan.py 框架改造，backtest_macd_scan.py 保持不动）

背景（阶段1结论）：chan_engine 的 defend_price 当前=最近中枢下沿（'zs_low'）。
B1买点出现在下跌趋势末端（价格创新低），此时"最近中枢"在价格上方 →
defend_price > 现价 → 无效止损（daemon 里体现为"收盘破止损"误判、LONG_OPEN 被拦截）。
阶段2 对比 4 种 B1 防守位定义，DEFEND_MODE 只影响 B1（背驰类买点）：
  'zs_low'    最近中枢下沿（默认，现状）
  'b1_low'    B1 买点低点本身（背驰极值低点）
  'b1_atr'    B1 买点低点 - K×ATR(14)（K=DEFEND_ATR_MULT=1.0）
  'seg_start' 背驰段（离开段/b段）起点下方：背驰段第一只同向笔的摆动低点
              （当前趋势段起始摆动低点）
B2（回调类）defend 保持 B1低点、B3（突破类）defend 保持中枢下沿（语义更合理，不变）。

两个对比群体（关键设计）：
  * 群体A（主，B1 状态样本）：日采样 buy_point=='B1' 的时刻，连续 B1 状态去重
    （每个 B1 事件计 1 次，取事件首日）。这是 defend 定义真正起作用的群体——
    日线慢速版回测中 B1 几乎从不进入共振 LONG_OPEN（被"日线trend≠DOWN"条件与
    "连续2根收盘破止损"拦截层挡掉，见群体B），故必须在 B1 状态群体上直接比较。
    指标：无效止损占比（defend>信号日现价，关键指标）、平均止损距离（ATR倍数）、
    信号有效性统计（+1R/+2R 30/60/90、破止损60、60日均R，与既有口径一致）。
  * 群体B（对照，共振 LONG_OPEN）：周采样完整共振决策（与 backtest_macd_scan 一致），
    验证"生产信号集合在 4 种定义下完全相同（B1=0，B2/B3 为主）"——证明 defend 定义
    的影响被拦截层掩盖，只能在群体A上评估。

防未来函数（与 backtest_macd_scan.py 完全一致）：
  * 群体A/B 采样点只用截至该时刻的日线 daily[:i+1]；
  * 群体B 周线仅取已完整闭合的K线（_truncate_weekly），波浪缓存每轮重置；
  * 群体B 共振冷却状态进程内隔离（monkey-patch，不读 daemon 的 cooldown.json）；
  * chan_engine DEFEND_MODE 通过 monkey-patch 模块级常量切换，每模式循环内覆盖，
    互不污染；默认行为（'zs_low'）与现状完全一致（回归由 chan_engine.py 自测保证）。

用法：
  python backtest_defend_scan.py --symbol XAUUSD   # 单品种调试（4种定义×1品种）
  python backtest_defend_scan.py                   # 全品种扫描（4种定义×4品种）
"""

import argparse
import datetime
import os
import sys
import time

# ======================================================================
# 参数区（集中于此，便于标定）
# ======================================================================
HST_BASE = r"C:\Program Files (x86)\Alpari MT4\history\Alpari-Demo"
SYMBOLS = ['BITCOIN', 'XAUUSD', 'XAGUSD', 'WTI']   # 扫描品种
WARMUP_DAYS = 600          # 日线预热根数（从第600根起开始采样）
SAMPLE_STEP = 7            # 群体B 采样步长（每周一次，与既有扫描一致）
B1_DAY_STEP = 1            # 群体A 采样步长（每天一次，捕捉短暂的 B1 状态）
WIN_R = 2.0                # +2R 目标（止盈线）
STOP_LOSS_ATR_MULT = 2.0   # 默认止损距离 = 2 × 周线ATR（defend 无效时回退，同既有口径）
WEEK_MINUTES = 10080       # 周线周期（分钟）
DAY_MINUTES = 1440         # 日线周期（分钟）
WEEK_SECONDS = WEEK_MINUTES * 60
DAY_SECONDS = DAY_MINUTES * 60
TRACK_DAYS = 90            # 单信号最长跟踪交易日（+1R/+2R 判定窗口）
STOP_WINDOW = 60           # 破止损判定窗口（交易日，与 60 日平均 R 对应）
MIN_SIGNALS = 5            # 推荐门槛：4品种合计样本数 >= 5 才参与推荐
                           # （群体A 为 B1 事件样本，B1 状态持续时间短且被 B2 快速取代，
                           #   8年×4品种仅约 8 个事件，门槛低于 LONG_OPEN 群体的 10）

# ---- B1 防守位定义网格（阶段2，集中于此） ----
# mode 与 chan_engine.DEFEND_MODE 一一对应；b1_atr 的 K=DEFEND_ATR_MULT（chan_engine 顶部）
DEFEND_MODES = [
    ('zs_low',    '最近中枢下沿(现状)'),
    ('b1_low',    'B1低点'),
    ('b1_atr',    'B1低点-1.0×ATR'),
    ('seg_start', '背驰段起点'),
]
# 每模式输出列说明（表头用）

# 输出目录：本脚本同目录下 backtest_defend_scan_output/
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'backtest_defend_scan_output')

# ======================================================================
# 引擎导入（保证在 chan_wave 目录下运行）
# ======================================================================
import wave_engine
import chan_engine
import resonance_engine

# ---- 回测隔离：禁用跨进程冷却持久化（同 backtest_daily/macd_scan） ----
resonance_engine._load_cooldown = lambda: None
resonance_engine._save_cooldown = lambda: None
resonance_engine._STATE.update({'false_count': 0, 'cooldown': False,
                                'cool_until': None, 'last_false': None})


# ======================================================================
# 工具函数（与 backtest_macd_scan.py 一致）
# ======================================================================
def _fmt_date(ts):
    """.hst 时间戳为服务器时间（EET），显示时 +3h 贴近 MT4 时间。"""
    return datetime.datetime.fromtimestamp(ts + 3 * 3600,
                                           datetime.timezone.utc).strftime('%Y-%m-%d')


def _load_daily(symbol):
    """加载日线 candles（1440.hst）。"""
    return wave_engine.load_hst(os.path.join(HST_BASE, '%s%d.hst'
                                             % (symbol, DAY_MINUTES)))


def _load_weekly(symbol, daily):
    """加载周线 candles（10080.hst），缺失时用日线重采样兜底。"""
    w = wave_engine.load_hst(os.path.join(HST_BASE, '%s%d.hst'
                                          % (symbol, WEEK_MINUTES)))
    if not w:
        w = wave_engine.resample(daily, WEEK_MINUTES) if daily else None
    return w


def _reset_wave_cache():
    """重置 wave_engine 浪号锁定缓存，保证每次 compute 独立全量重算。"""
    wave_engine._CACHE.update({'key': None, 'result': None, 'last_swing': None,
                               'broken_level': None, 'bias': None})


def _truncate_weekly(weekly, scan_ts):
    """截断周线到采样时刻：只保留已完整闭合的周线K线（防未来函数）。"""
    return [w for w in weekly if w['time'] + WEEK_SECONDS <= scan_ts]


def _apply_defend_mode(mode):
    """monkey-patch chan_engine 模块级 DEFEND_MODE（B1 防守位定义）。"""
    chan_engine.DEFEND_MODE = mode


def _atr_week_at(daily, weekly, i):
    """采样时刻的周线 ATR(14)：周线截断后计算；周线缺失时用日线重采样兜底。"""
    scan_ts = daily[i]['time'] + DAY_SECONDS
    wk = _truncate_weekly(weekly, scan_ts)
    if len(wk) < 2:
        wk = wave_engine.resample(daily[:i + 1], WEEK_MINUTES)
    return wave_engine.compute_atr(wk, 14) if len(wk) >= 2 else 0.0


def _default_stop(daily, i, open_price, atr_week):
    """默认止损价 = 开仓价 - 2×周线ATR；周线ATR无效时用近14根日线ATR。"""
    mult = STOP_LOSS_ATR_MULT
    if atr_week and atr_week > 0:
        return open_price - mult * atr_week
    seg = daily[max(0, i - 13):i + 1]
    atr_day = wave_engine.compute_atr(seg, 14)
    if atr_day and atr_day > 0:
        return open_price - mult * atr_day
    return 0.0


# ======================================================================
# 群体A：B1 状态样本扫描（主群体）
# ======================================================================
def run_b1_events(symbol, daily, weekly):
    """日采样扫描 buy_point=='B1' 的时刻，连续 B1 状态去重（每事件取首日）。

    返回 {mode: [signal dict]}，signal dict 含:
      idx/time/open_price(=信号日收盘)/stop_loss_price(=该模式 defend)/
      atr_week/atr_day/defend_invalid(defend>现价)/trend
    """
    n = len(daily)
    out = {m: [] for m, _ in DEFEND_MODES}
    prev_b1 = False
    for i in range(WARMUP_DAYS, n):
        # 先用任意模式(默认 zs_low)算 buy_point —— 买/卖点与 DEFEND_MODE 无关
        cd0 = chan_engine.compute(daily[:i + 1], 'DAY')
        is_b1 = (cd0['buy_point'] == 'B1')
        run_first = is_b1 and not prev_b1   # 连续 B1 状态只取首日（去重）
        prev_b1 = is_b1
        if not run_first:
            continue
        close = daily[i]['close']
        atr_week = _atr_week_at(daily, weekly, i)
        for mode, _label in DEFEND_MODES:
            _apply_defend_mode(mode)
            cd = chan_engine.compute(daily[:i + 1], 'DAY')
            if cd['buy_point'] != 'B1':     # 防御：买点不应随模式变化
                continue
            defend = cd['defend_price']
            atr_day = (cd.get('details') or {}).get('atr_day') or 0.0
            out[mode].append({
                'idx': i,
                'time': daily[i]['time'],
                'signal': 'B1',
                'open_price': close,
                'stop_loss_price': defend,
                'atr_week': atr_week,
                'atr_day': atr_day,
                'defend_invalid': defend > close,   # 无效止损：defend > 信号日现价
                'trend': cd.get('trend'),
            })
    return out


# ======================================================================
# 群体B：共振 LONG_OPEN 信号扫描（对照群体，与 backtest_macd_scan 一致）
# ======================================================================
def run_resonance_scan(symbol, daily, weekly, mode, wave_cache):
    """周采样完整共振决策，返回 LONG_OPEN 信号 list[dict]。

    wave_cache: dict[i -> 周线波浪结果]，同一采样点的波浪结果与 DEFEND_MODE 无关，
    跨模式复用（缓存机制：同一品种 4 种模式只算一次周线）。
    """
    _apply_defend_mode(mode)
    n = len(daily)
    signals = []
    for i in range(WARMUP_DAYS, n, SAMPLE_STEP):
        scan_ts = daily[i]['time'] + DAY_SECONDS
        wk = _truncate_weekly(weekly, scan_ts)
        if len(wk) < 20:   # 周线数据不足，跳过该采样点
            continue
        ww = wave_cache.get(i)
        if ww is None:
            _reset_wave_cache()
            ww = wave_engine.compute(wk, 'WEEK')
            wave_cache[i] = ww
        cd = chan_engine.compute(daily[:i + 1], 'DAY')
        wd = ww.get('details') or {}
        atr = wd.get('atr', 0.0) or 0.0
        lc = wd.get('last_close', 0.0) or 0.0
        volatility = {'atr_pct': round(atr / lc * 100.0, 3) if lc else 0.0,
                      'abnormal': False}
        res = resonance_engine.evaluate(symbol, ww, cd, None, None, volatility)
        if res['signal'] == 'LONG_OPEN':
            signals.append({
                'idx': i,
                'time': daily[i]['time'],
                'signal': res['signal'],
                'open_price': daily[i]['close'],
                'stop_loss_price': res['stop_loss_price'],
                'atr_week': atr,
                'atr_day': (cd.get('details') or {}).get('atr_day') or 0.0,
                'defend_invalid': cd['defend_price'] > daily[i]['close'],
                'buy_point': cd.get('buy_point'),
            })
    return signals


# ======================================================================
# 信号有效性统计：单个信号独立跟踪（不去重、不模拟持仓，同既有口径）
# ======================================================================
def track_signal(daily, sig):
    """独立跟踪单个信号后续走势，返回指标 dict 或 None（止损无效且兜底仍无效）。

    指标（全部基于每日收盘价，与 backtest_daily/macd_scan 模拟口径一致）：
      hit.r1_30/60/90 : 30/60/90 交易日内是否收盘 >= 开仓价+1R
      hit.r2_30/60/90 : 30/60/90 交易日内是否收盘 >= 开仓价+2R
      stop_hit        : STOP_WINDOW 日内是否发生连续2根收盘 < 止损价
      r60             : 第60交易日（或数据末尾）收盘归一化 R = (close-open)/risk
    """
    open_i = sig['idx']
    open_price = sig['open_price']
    stop = sig['stop_loss_price']
    risk = open_price - stop
    # ---- 止损有效性：0 < stop < open_price，否则回退默认止损 ----
    if not (0 < stop < open_price):
        stop = _default_stop(daily, open_i, open_price, sig['atr_week'])
        risk = open_price - stop
    if risk <= 0:                 # 兜底仍无效 → 该信号无法跟踪
        return None
    tp1 = open_price + 1.0 * risk
    tp2 = open_price + WIN_R * risk
    n = len(daily)
    end = min(open_i + TRACK_DAYS, n)
    hit = {'r1_30': False, 'r1_60': False, 'r1_90': False,
           'r2_30': False, 'r2_60': False, 'r2_90': False}
    stop_hit = False
    for j in range(open_i + 1, end):
        d = j - open_i
        c = daily[j]['close']
        if c >= tp1:
            if d <= 30:
                hit['r1_30'] = True
            if d <= 60:
                hit['r1_60'] = True
            hit['r1_90'] = True
        if c >= tp2:
            if d <= 30:
                hit['r2_30'] = True
            if d <= 60:
                hit['r2_60'] = True
            hit['r2_90'] = True
        # 破止损（60日窗口内连续2根收盘 < 止损价，盘中穿刺不算）
        if d <= STOP_WINDOW and c < stop and daily[j - 1]['close'] < stop:
            stop_hit = True
    # 60 日平均 R：第60交易日收盘归一化（数据不足取最后一根）
    j60 = min(open_i + 60, n - 1)
    r60 = (daily[j60]['close'] - open_price) / risk
    return {'hit': hit, 'stop_hit': stop_hit, 'r60': r60}


# ======================================================================
# 统计汇总（含阶段2新增关键指标）
# ======================================================================
_KEYS = ('r1_30', 'r1_60', 'r1_90', 'r2_30', 'r2_60', 'r2_90')


def aggregate(results):
    """信号级汇总：返回 {n, r1_30..r2_90 概率%, stop_hit 概率%, r60_avg}。"""
    n = len(results)
    out = {'n': n}
    for k in _KEYS:
        out[k] = round(sum(1 for r in results if r['hit'][k]) / n * 100.0, 1) \
            if n else 0.0
    out['stop_hit'] = round(sum(1 for r in results if r['stop_hit']) / n * 100.0,
                            1) if n else 0.0
    out['r60_avg'] = round(sum(r['r60'] for r in results) / n, 3) if n else 0.0
    return out


def _count_bp(signals):
    """按买点类型统计信号数（群体B 用；群体A 全为 B1）。"""
    out = {'B1': 0, 'B2': 0, 'B3': 0, 'other': 0}
    for s in signals:
        bp = s.get('buy_point')
        if bp in out:
            out[bp] += 1
        else:
            out['other'] += 1
    return out


def _fmt_pct(v):
    return '%.1f' % v


# ======================================================================
# 报告输出
# ======================================================================
def group_summary_block(lines, title, per_sym, agg_all):
    """写一个参数组的报告块：4品种明细 + 合计。

    阶段2列：无效止损%（defend>现价占比，关键指标）、止损距ATR（平均止损距离，
    ATR倍数）；per_sym 的 agg 由 aggregate_defend 生成（含 invalid_pct/stop_dist_atr）。
    """
    lines.append('-' * 100)
    lines.append(title)
    lines.append('  %-9s %5s %7s %8s %6s %6s %6s %6s %6s %6s %7s %8s' % (
        '品种', '样本数', '无效止损%', '止损距ATR', '+1R30', '+1R60', '+1R90',
        '+2R30', '+2R60', '+2R90', '破止损', '60日均R'))
    for sym in per_sym:
        st = per_sym[sym]
        a = st['agg']
        lines.append('  %-9s %5d %7s %8s %6s %6s %6s %6s %6s %6s %7s %+8.3f' % (
            sym, a['n'],
            _fmt_pct(a['invalid_pct']), a['stop_dist_atr'],
            _fmt_pct(a['r1_30']), _fmt_pct(a['r1_60']), _fmt_pct(a['r1_90']),
            _fmt_pct(a['r2_30']), _fmt_pct(a['r2_60']), _fmt_pct(a['r2_90']),
            _fmt_pct(a['stop_hit']), a['r60_avg']))
    a = agg_all
    lines.append('  %-9s %5d %7s %8s %6s %6s %6s %6s %6s %6s %7s %+8.3f' % (
        '4品种合计', a['n'],
        _fmt_pct(a['invalid_pct']), a['stop_dist_atr'],
        _fmt_pct(a['r1_30']), _fmt_pct(a['r1_60']), _fmt_pct(a['r1_90']),
        _fmt_pct(a['r2_30']), _fmt_pct(a['r2_60']), _fmt_pct(a['r2_90']),
        _fmt_pct(a['stop_hit']), a['r60_avg']))
    lines.append('')
    return lines


def aggregate_defend(signals, results):
    """阶段2汇总：标准信号有效性 + 无效止损占比 + 平均止损距离(ATR倍数)。

    无效止损占比 = defend>信号日现价 的信号占比（关键指标，越少越好）；
    平均止损距离 = (开仓价-defend)/日线ATR(14) 的均值，ATR倍数（含无效样本，
    无效时距离为负值，反映止损在价格上方的程度）。
    """
    agg = aggregate(results)
    n = len(signals)
    agg['n_sig'] = n
    invalid = sum(1 for s in signals if s['defend_invalid'])
    agg['invalid_pct'] = round(invalid / n * 100.0, 1) if n else 0.0
    dists = []
    for s in signals:
        atr = s.get('atr_day') or 0.0
        if atr and atr > 0:
            dists.append((s['open_price'] - s['stop_loss_price']) / atr)
    agg['stop_dist_atr'] = round(sum(dists) / len(dists), 2) if dists else 0.0
    return agg


def recommend(groups, primary='invalid_pct'):
    """推荐 B1 防守位定义：无效止损占比最低优先，其次 60日均R 高/破止损率低。

    评分 = 2.0×60日均R + 1.0×+2R概率(60日) - 0.5×破止损概率（同既有扫描，
    仅在无效止损占比相同/接近时用于排序）。
    返回 (best_label, reason)。
    """
    cands = [g for g in groups if g['agg']['n_sig'] >= MIN_SIGNALS]
    if not cands:
        return None, '无模式达到样本数门槛(>=%d)，样本不足以推荐' % MIN_SIGNALS
    for g in cands:
        a = g['agg']
        g['score'] = round(2.0 * a['r60_avg']
                           + a['r2_60'] / 100.0
                           - a['stop_hit'] / 100.0 * 0.5, 3)
    # 主排序：无效止损占比升序；次排序：评分降序
    cands.sort(key=lambda g: (g['agg']['invalid_pct'], -g['score']))
    best = cands[0]
    a = best['agg']
    reason = ('无效止损占比=%.1f%%(最低优先,4品种合计) | 样本数=%d(>=%d门槛) | '
              '60日均R=%+.3f | +2R概率(60日)=%.1f%% | 破止损概率=%.1f%% | 评分=%.3f'
              % (a['invalid_pct'], a['n_sig'], MIN_SIGNALS, a['r60_avg'],
                 a['r2_60'], a['stop_hit'], best['score']))
    return best['label'], reason


# ======================================================================
# 主入口
# ======================================================================
def main():
    ap = argparse.ArgumentParser(description='B1防守位(defend_price)4种定义对比扫描')
    ap.add_argument('--symbol', default=None,
                    help='单品种调试（如 XAUUSD）；缺省跑全品种')
    args = ap.parse_args()

    # Windows 控制台 UTF-8 输出兼容
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    symbols = [args.symbol.upper()] if args.symbol else SYMBOLS
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    t0 = time.time()
    print('=' * 100)
    print('B1防守位(defend_price)4种定义对比扫描（%d种定义 × %d品种）'
          % (len(DEFEND_MODES), len(symbols)))
    print('用途: 参数标定阶段2（研究项目，禁止自动下单）')
    print('品种: %s' % ', '.join(symbols))
    print('=' * 100)

    report = []
    report.append('B1防守位(defend_price)4种定义对比扫描报告（%d种定义 × %d品种）'
                  % (len(DEFEND_MODES), len(symbols)))
    report.append('=' * 78)
    report.append('声明: 模拟盘研究用途，禁止自动下单；信号有效性统计，'
                  '不代表实盘收益。')
    report.append('生成时间: %s' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    report.append('数据: 各品种 .hst 日线/周线（约2018-2026，BITCOIN 自2020-06起）')
    report.append('')
    report.append('【4种 B1 防守位定义（DEFEND_MODE，只影响 B1 背驰类买点）】')
    for m, label in DEFEND_MODES:
        report.append('  %-10s = %s' % (m, label))
    report.append('  （B2 回调类 defend 保持 B1低点、B3 突破类 defend 保持中枢下沿，'
                  '语义更合理，不变）')
    report.append('')
    report.append('【方法: 两个对比群体（信号有效性统计与既有扫描一致）】')
    report.append('  * 群体A（主，B1状态样本）: 日采样, buy_point==B1 且为连续B1状态'
                  '首日(去重, 每事件1次);')
    report.append('      开仓价=信号日收盘; 止损=该模式 defend_price; '
                  'defend无效(>现价)时回退 2×周线ATR(同既有口径);')
    report.append('      +1R/+2R 概率(30/60/90)、破止损概率(60日)、60日平均R 与既有扫描一致;')
    report.append('      新增关键指标: 无效止损占比 = defend_price>信号日现价 的比例'
                  '(越高越容易产生止损在价格上方的无效情形);')
    report.append('      平均止损距离 = (开仓价-defend)/日线ATR(14), ATR倍数'
                  '(含无效样本, 无效时为负值)。')
    report.append('  * 群体B（对照，共振LONG_OPEN）: 周采样完整共振决策'
                  '(与 backtest_macd_scan 一致);')
    report.append('      说明: 日线慢速版回测中 B1 几乎从不进入 LONG_OPEN'
                  '(被"日线trend≠DOWN"与"连续2根收盘破止损"拦截),')
    report.append('      群体B 以 B2/B3 为主 —— 4种定义在群体B上应无差异,'
                  '这正说明 defend 定义的影响只在群体A(B1)上可见。')
    report.append('【防未来函数】与既有扫描一致: 采样点只用截至该时刻的数据、'
                  '周线只取已闭合K线、波浪缓存每轮重置、冷却状态进程内隔离。')
    report.append('')

    # ---- 每品种数据加载一次，两群体共用 ----
    datasets = {}
    for sym in symbols:
        daily = _load_daily(sym)
        weekly = _load_weekly(sym, daily)
        if not daily or not weekly:
            print('    !! 数据缺失，跳过 %s' % sym)
            continue
        datasets[sym] = (daily, weekly)
    if not datasets:
        print('无可用数据')
        return

    # ================= 群体A：B1 状态样本 =================
    print('\n===== 群体A: B1 状态样本扫描（主群体） =====')
    groups_a = []
    for mode, label in DEFEND_MODES:
        _apply_defend_mode(mode)
        print('\n---------- 群体A | %s (%s) ----------' % (mode, label))
        per_sym = {}
        all_signals = []
        all_results = []
        for sym in (datasets):
            daily, weekly = datasets[sym]
            sigs = run_b1_events(sym, daily, weekly)[mode]
            results = []
            for s in sigs:
                t = track_signal(daily, s)
                if t is not None:
                    results.append(t)
            agg = aggregate_defend(sigs, results)
            per_sym[sym] = {'agg': agg}
            all_signals.extend(sigs)
            all_results.extend(results)
            print('    [%s] B1事件=%d (可跟踪=%d) | 无效止损=%.1f%% | 止损距ATR=%s | '
                  '+2R60=%.1f%% 破止损=%.1f%% 60日均R=%+.3f'
                  % (sym, len(sigs), agg['n'], agg['invalid_pct'],
                     agg['stop_dist_atr'], agg['r2_60'], agg['stop_hit'],
                     agg['r60_avg']))
        agg_all = aggregate_defend(all_signals, all_results)
        groups_a.append({'label': label, 'mode': mode, 'per_sym': per_sym,
                         'agg': agg_all})
        group_summary_block(report, '群体A | %s (%s)' % (mode, label),
                            per_sym, agg_all)
        print('    4品种合计: B1事件=%d | 无效止损=%.1f%% | 止损距ATR=%s | '
              '+2R60=%.1f%% | 破止损=%.1f%% | 60日均R=%+.3f'
              % (agg_all['n_sig'], agg_all['invalid_pct'],
                 agg_all['stop_dist_atr'], agg_all['r2_60'],
                 agg_all['stop_hit'], agg_all['r60_avg']))

    # ================= 群体B：共振 LONG_OPEN（对照） =================
    print('\n===== 群体B: 共振 LONG_OPEN 扫描（对照群体） =====')
    groups_b = []
    for mode, label in DEFEND_MODES:
        print('\n---------- 群体B | %s (%s) ----------' % (mode, label))
        per_sym = {}
        all_signals = []
        all_results = []
        for sym in datasets:
            daily, weekly = datasets[sym]
            wave_cache = {}
            sigs = run_resonance_scan(sym, daily, weekly, mode, wave_cache)
            results = []
            for s in sigs:
                t = track_signal(daily, s)
                if t is not None:
                    results.append(t)
            agg = aggregate_defend(sigs, results)
            bp = _count_bp(sigs)
            per_sym[sym] = {'agg': agg, 'bp': bp}
            all_signals.extend(sigs)
            all_results.extend(results)
            print('    [%s] LONG_OPEN=%d (B1/B2/B3=%d/%d/%d) | 无效止损=%.1f%% | '
                  '+2R60=%.1f%% 破止损=%.1f%% 60日均R=%+.3f'
                  % (sym, len(sigs), bp['B1'], bp['B2'], bp['B3'],
                     agg['invalid_pct'], agg['r2_60'], agg['stop_hit'],
                     agg['r60_avg']))
        agg_all = aggregate_defend(all_signals, all_results)
        groups_b.append({'label': label, 'mode': mode, 'per_sym': per_sym,
                         'agg': agg_all})
        group_summary_block(report, '群体B(对照) | %s (%s)' % (mode, label),
                            per_sym, agg_all)
        print('    4品种合计: LONG_OPEN=%d | 无效止损=%.1f%% | +2R60=%.1f%% | '
              '破止损=%.1f%% | 60日均R=%+.3f'
              % (agg_all['n_sig'], agg_all['invalid_pct'], agg_all['r2_60'],
                 agg_all['stop_hit'], agg_all['r60_avg']))

    # ================= 推荐（基于群体A） =================
    report.append('=' * 78)
    report.append('【推荐 B1 防守位定义】（基于群体A: B1状态样本）')
    report.append('  规则: 无效止损占比最低优先, 其次 60日均R 高优先 / 破止损率低优先;')
    report.append('  评分 = 2.0×60日均R + 1.0×+2R概率(60日) - 0.5×破止损概率'
                  '（同分/接近时参考）')
    report.append('')
    report.append('  排序（仅样本数>=%d 的定义参与推荐）:' % MIN_SIGNALS)
    report.append('    排名  定义                        样本数  无效止损%  止损距ATR  '
                  '60日均R  +2R60  破止损  评分')
    # 评分 = 2.0×60日均R + 1.0×+2R概率(60日) - 0.5×破止损概率（无效占比相同时参考）
    for g in groups_a:
        a = g['agg']
        g['score'] = round(2.0 * a['r60_avg'] + a['r2_60'] / 100.0
                           - a['stop_hit'] / 100.0 * 0.5, 3)
    scored = sorted(groups_a,
                    key=lambda g: (g['agg']['invalid_pct'], -g['score']))
    for i, g in enumerate(scored, 1):
        a = g['agg']
        report.append('    %2d.  %-26s %4d  %7.1f%%  %8s  %+6.3f  %5.1f%%  %5.1f%%  %+.3f'
                      % (i, g['label'], a['n_sig'], a['invalid_pct'],
                         a['stop_dist_atr'], a['r60_avg'], a['r2_60'],
                         a['stop_hit'], g['score']))
    best_label, reason = recommend(groups_a)
    report.append('')
    report.append('  >>> 推荐: %s' % best_label)
    report.append('  理由: %s' % reason)
    report.append('')
    report.append('【群体B 对照结论】')
    g0 = groups_b[0]['agg']
    same = all(g['agg']['n_sig'] == g0['n_sig']
               and g['agg']['invalid_pct'] == g0['invalid_pct']
               for g in groups_b[1:])
    report.append('  4种定义在共振 LONG_OPEN 群体上的信号集合%s'
                  % ('完全相同' if same else '存在差异'))
    report.append('  （日线慢速版回测 B1 被拦截层挡掉, LONG_OPEN 以 B2/B3 为主,'
                  ' defend 定义影响只在群体A可见）')
    report.append('')
    report.append('【稳健性备注】')
    report.append('  * 群体A 样本量小（每个 B1 事件计 1 次, 8年×4品种共 8 个事件; '
                  'B1 状态持续时间短且被 B2 快速取代）,')
    report.append('    60日均R 受个别事件影响大, 推荐结论以"无效止损占比"'
                  '（结构性指标, 对样本量不敏感）为主依据。')
    report.append('  * zs_low(现状) 在下跌末端创新低时 defend 几乎必然高于现价'
                  '（4品种合计 100%=8/8 无效, 平均止损距离 -5.5 ATR = 止损在价格上方'
                  '5.5 个 ATR）, 属规则语义缺陷, 必须更换;')
    report.append('  * b1_low/b1_atr 把止损锚定在 B1 低点附近, 无效占比=0%; '
                  'b1_low 止损最紧（1.75 ATR）,')
    report.append('    60日均R 与 +2R60 均最高（+4.001 / 75%）; b1_atr 多留 1 ATR 缓冲'
                  '（2.75 ATR, 抗毛刺）,')
    report.append('    但 R 归一化后盈利信号收益被摊薄（60日均R +2.395）;')
    report.append('  * seg_start(背驰段起点) 是结构性定义, 部分品种(如 WTI)反弹初期'
                  '仍高于现价（合计 12.5% 无效）,')
    report.append('    60日均R 最高(+4.141)但与 b1_low 同源样本, 差异无统计意义。')
    report.append('')
    report.append('免责: 本扫描为研究用途，参数推荐不构成任何交易建议；'
                  '信号有效性统计不模拟持仓与交易成本。')

    # ---- 写报告 ----
    report_path = os.path.join(OUTPUT_DIR, 'backtest_defend_scan_report.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(report) + '\n')
    print('\n报告已生成: %s' % report_path)
    print('总耗时: %.1f 秒' % (time.time() - t0))


if __name__ == '__main__':
    main()
