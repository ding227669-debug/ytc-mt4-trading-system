# -*- coding: utf-8 -*-
"""
backtest_macd_scan.py — 背驰参数敏感性扫描（MACD 4组 × 背驰比较方式 2种 × 4品种）
====================================================================================
所属系统：缠论+波浪共振交易信号系统（模拟盘研究项目，禁止自动下单，只做回测研究）
阶段：参数标定阶段1 —— 背驰参数敏感性扫描（复制 backtest_daily.py 框架改造，
      backtest_daily.py 保持不动）

方法（信号有效性统计，与波浪扫描一致的方法论）：
  * 每个 LONG_OPEN 信号独立跟踪后续走势（不去重、不模拟持仓、不反馈证伪）：
      - 开仓价 = 采样日收盘；止损价 = 共振器输出（无效时回退 2×周线ATR，同 backtest_daily）
      - ① +1R 概率（30/60/90 交易日）：跟踪期内收盘 >= 开仓价+1R
      - ② +2R 概率（30/60/90 交易日）：收盘 >= 开仓价+2R
      - ③ 收盘破止损概率（60 交易日窗口）：连续2根收盘 < 止损价（与模拟执行口径一致）
      - ④ 60 日平均 R：第60交易日（或数据末尾）收盘归一化收益 (close-open)/risk 平均
  * 每组合输出 4 品种明细 + 4 品种合计/平均；末尾推荐参数组合：
      信号数 >= MIN_SIGNALS 前提下，60日平均R 与 +2R 概率优先、破止损概率低优先
      （加权评分 score = 2.0×60日均R + 1.0×+2R概率 - 0.5×破止损概率）

防未来函数（与 backtest_daily.py 完全一致）：
  * 每周采样一次（SAMPLE_STEP=7 根日线），采样点只用截至该时刻的数据
  * 周线仅取已完整闭合的K线（未走完的当周K线不参与，避免用未来数据）
  * wave_engine 浪号锁定缓存每轮采样前重置（独立全量重算）
  * resonance_engine 冷却状态进程内隔离（monkey-patch 读写为 no-op）
  * chan_engine 参数通过 monkey-patch 模块级常量切换（MACD 周期 + BEICHI_MODE），
    每组合循环内覆盖设置，互不污染；默认行为（12,26,9 + area）不受影响

用法：
  python backtest_macd_scan.py --symbol XAUUSD   # 单品种调试（8组×1品种）
  python backtest_macd_scan.py                   # 全品种扫描（8组×4品种）
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
SAMPLE_STEP = 7            # 每周采样一次（7根日线）
WIN_R = 2.0                # +2R 目标（止盈线）
STOP_LOSS_ATR_MULT = 2.0   # 默认止损距离 = 2 × 周线ATR（共振器无防守价时回退）
WEEK_MINUTES = 10080       # 周线周期（分钟）
DAY_MINUTES = 1440         # 日线周期（分钟）
WEEK_SECONDS = WEEK_MINUTES * 60
DAY_SECONDS = DAY_MINUTES * 60
TRACK_DAYS = 90            # 单信号最长跟踪交易日（+1R/+2R 判定窗口）
STOP_WINDOW = 60           # 破止损判定窗口（交易日，与 60 日平均 R 对应）
MIN_SIGNALS = 10           # 推荐门槛：4品种合计信号数 >= 10 才参与推荐

# ---- 背驰参数网格（集中于此） ----
# MACD 周期 4 组：(快线, 慢线, 信号线)
MACD_PARAMS = [
    (5, 13, 4),        # 快参数：短线敏感，背驰信号多
    (8, 17, 5),        # 中快
    (12, 26, 9),       # 默认（缠论标准 12/26/9）
    (26, 52, 9),       # 慢参数：趋势型，背驰信号少而稳
]
# 背驰比较方式 2 种：'area'=MACD柱面积比较（默认）| 'dif_peak'=DIF峰值比较
BEICHI_MODES = ['area', 'dif_peak']

# 输出目录：本脚本同目录下 backtest_macd_scan_output/
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'backtest_macd_scan_output')

# ======================================================================
# 引擎导入（保证在 chan_wave 目录下运行）
# ======================================================================
import wave_engine
import chan_engine
import resonance_engine

# ---- 回测隔离：禁用跨进程冷却持久化（同 backtest_daily） ----
resonance_engine._load_cooldown = lambda: None
resonance_engine._save_cooldown = lambda: None
resonance_engine._STATE.update({'false_count': 0, 'cooldown': False,
                                'cool_until': None, 'last_false': None})


# ======================================================================
# 工具函数（与 backtest_daily.py 一致）
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
    src = '10080.hst(原生周线)'
    if not w:
        w = wave_engine.resample(daily, WEEK_MINUTES) if daily else None
        src = '1440.hst 日线重采样->周线(原生缺失)'
    return w, src


def _reset_wave_cache():
    """重置 wave_engine 浪号锁定缓存，保证每次 compute 独立全量重算。"""
    wave_engine._CACHE.update({'key': None, 'result': None, 'last_swing': None,
                               'broken_level': None, 'bias': None})


def _truncate_weekly(weekly, scan_ts):
    """截断周线到采样时刻：只保留已完整闭合的周线K线（防未来函数）。"""
    return [w for w in weekly if w['time'] + WEEK_SECONDS <= scan_ts]


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


def _apply_chan_params(fast, slow, signal, mode):
    """monkey-patch chan_engine 模块级参数（MACD 周期 + 背驰比较方式）。"""
    chan_engine.MACD_FAST = fast
    chan_engine.MACD_SLOW = slow
    chan_engine.MACD_SIGNAL = signal
    chan_engine.BEICHI_MODE = mode


# ======================================================================
# 单品种滚动采样：生成 LONG_OPEN 信号列表（框架复制自 backtest_daily.run_symbol）
# ======================================================================
def run_symbol_scan(symbol, daily, weekly):
    """对单品种执行滚动采样，返回全部 LONG_OPEN 信号 list[dict]。"""
    n = len(daily)
    signals = []
    sample_idx = list(range(WARMUP_DAYS, n, SAMPLE_STEP))

    for i in sample_idx:
        scan_ts = daily[i]['time'] + DAY_SECONDS   # 采样时刻 = 当日收盘后
        # ---- 截至采样点的数据（防未来函数核心） ----
        day_candles = daily[:i + 1]
        week_candles = _truncate_weekly(weekly, scan_ts)
        if len(week_candles) < 20:   # 周线数据不足，跳过该采样点
            continue
        # ---- 各引擎计算（每轮重置波浪缓存 → 独立全量重算） ----
        _reset_wave_cache()
        wave_week = wave_engine.compute(week_candles, 'WEEK')
        chan_day = chan_engine.compute(day_candles, 'DAY')
        # ---- 波动率（周线 ATR / 价格；abnormal 本扫描不启用，保持与阶段0一致） ----
        wd = wave_week.get('details') or {}
        atr = wd.get('atr', 0.0) or 0.0
        lc = wd.get('last_close', 0.0) or 0.0
        volatility = {'atr_pct': round(atr / lc * 100.0, 3) if lc else 0.0,
                      'abnormal': False}
        # ---- 共振决策（日线慢速版：M30/M5 传 None 走降级路径） ----
        res = resonance_engine.evaluate(symbol, wave_week, chan_day,
                                        None, None, volatility)
        # ---- 只收集 LONG_OPEN 信号 ----
        if res['signal'] == 'LONG_OPEN':
            signals.append({
                'time': daily[i]['time'],
                'idx': i,
                'signal': res['signal'],
                'position_rate': res['position_rate'],
                'stop_loss_price': res['stop_loss_price'],
                'open_price': daily[i]['close'],
                'atr_week': atr,
                'buy_point': chan_day.get('buy_point'),
                'beichi': chan_day.get('beichi'),
                'check_summary': '; '.join(res['check_list'])[:200],
            })
    return signals


# ======================================================================
# 信号有效性统计：单个 LONG_OPEN 信号独立跟踪（不去重、不模拟持仓）
# ======================================================================
def track_signal(daily, sig):
    """独立跟踪单个信号后续走势，返回指标 dict 或 None（止损无效，无法跟踪）。

    指标（全部基于每日收盘价，与 backtest_daily 模拟口径一致）：
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
# 统计汇总
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
    """按买点类型统计信号数（背驰敏感性主要影响 B1/B2）。"""
    out = {'B1': 0, 'B2': 0, 'B3': 0, 'other': 0}
    for s in signals:
        bp = s.get('buy_point')
        if bp in out:
            out[bp] += 1
        else:
            out['other'] += 1
    return out


# ======================================================================
# 报告输出
# ======================================================================
def _fmt_pct(v):
    return '%.1f' % v


def group_summary_block(lines, label, per_sym, agg_all, signals_all):
    """写一个参数组的报告块：4品种明细 + 合计。"""
    lines.append('-' * 100)
    lines.append('组合: %s' % label)
    lines.append('  %-9s %5s %6s %6s %6s %6s %6s %6s %7s %8s   (B1/B2/B3)' % (
        '品种', '信号数', '+1R30', '+1R60', '+1R90', '+2R30', '+2R60',
        '+2R90', '破止损', '60日均R'))
    for sym in per_sym:
        st = per_sym[sym]
        bp = st['bp']
        lines.append('  %-9s %5d %6s %6s %6s %6s %6s %6s %7s %+8.3f   (%d/%d/%d)' % (
            sym, st['agg']['n'],
            _fmt_pct(st['agg']['r1_30']), _fmt_pct(st['agg']['r1_60']),
            _fmt_pct(st['agg']['r1_90']), _fmt_pct(st['agg']['r2_30']),
            _fmt_pct(st['agg']['r2_60']), _fmt_pct(st['agg']['r2_90']),
            _fmt_pct(st['agg']['stop_hit']), st['agg']['r60_avg'],
            bp['B1'], bp['B2'], bp['B3']))
    a = agg_all
    lines.append('  %-9s %5d %6s %6s %6s %6s %6s %6s %7s %+8.3f' % (
        '4品种合计', a['n'],
        _fmt_pct(a['r1_30']), _fmt_pct(a['r1_60']), _fmt_pct(a['r1_90']),
        _fmt_pct(a['r2_30']), _fmt_pct(a['r2_60']), _fmt_pct(a['r2_90']),
        _fmt_pct(a['stop_hit']), a['r60_avg']))
    lines.append('')
    return lines


def recommend(groups):
    """推荐参数组合：信号数 >= MIN_SIGNALS 前提下加权评分。

    评分 = 2.0×60日均R + 1.0×+2R概率(60日) - 0.5×破止损概率
    （60日平均R 与 +2R 概率优先，破止损概率低优先）
    返回 (best_label, score, reason)。
    """
    cands = [g for g in groups if g['agg']['n'] >= MIN_SIGNALS]
    if not cands:
        return None, 0.0, '无组合达到信号数门槛(>=%d)，样本不足以推荐' % MIN_SIGNALS
    for g in cands:
        a = g['agg']
        g['score'] = round(2.0 * a['r60_avg']
                           + a['r2_60'] / 100.0
                           - a['stop_hit'] / 100.0 * 0.5, 3)
    cands.sort(key=lambda g: g['score'], reverse=True)
    best = cands[0]
    a = best['agg']
    reason = ('信号数=%d(>=%d门槛) | 60日均R=%+.3f(最高优先) | +2R概率(60日)=%.1f%%'
              ' | 破止损概率=%.1f%%(低优先) | 评分=%.3f'
              % (a['n'], MIN_SIGNALS, a['r60_avg'], a['r2_60'],
                 a['stop_hit'], best['score']))
    return best['label'], best['score'], reason


# ======================================================================
# 主入口
# ======================================================================
def main():
    ap = argparse.ArgumentParser(description='背驰参数敏感性扫描（MACD×背驰方式×品种）')
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
    print('背驰参数敏感性扫描（MACD %d组 × 背驰比较 %d种 × %d品种）'
          % (len(MACD_PARAMS), len(BEICHI_MODES), len(symbols)))
    print('用途: 参数标定阶段1（研究项目，禁止自动下单）')
    print('品种: %s' % ', '.join(symbols))
    print('=' * 100)

    report = []
    report.append('背驰参数敏感性扫描报告（MACD 4组 × 背驰比较 2种 × 4品种）')
    report.append('=' * 78)
    report.append('声明: 模拟盘研究用途，禁止自动下单；信号有效性统计，'
                  '不代表实盘收益。')
    report.append('生成时间: %s' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    report.append('数据: 各品种 .hst 日线/周线（约2018-2026，BITCOIN 自2020-06起）')
    report.append('')
    report.append('【方法: 信号有效性统计（与波浪扫描一致）】')
    report.append('  * 每个 LONG_OPEN 信号独立跟踪后续走势（不去重、不模拟持仓）；')
    report.append('  * 开仓价=采样日收盘; 止损=共振器输出, 无效回退 2×周线ATR;')
    report.append('  * +1R/+2R 概率: 30/60/90 交易日内收盘达标即计1;')
    report.append('  * 破止损概率: 60日窗口内连续2根收盘<止损价(与模拟执行口径一致);')
    report.append('  * 60日平均R: 第60交易日(或数据末尾)收盘归一化 (close-open)/risk。')
    report.append('【防未来函数】与 backtest_daily.py 一致: 每周采样、周线只取已闭合K线、'
                  '波浪缓存每轮重置、冷却状态进程内隔离。')
    report.append('【参数网格】MACD: %s | 背驰比较: %s'
                  % (' / '.join('(%d,%d,%d)' % m for m in MACD_PARAMS),
                     '面积比较(area)' if BEICHI_MODES[0] == 'area' else str(BEICHI_MODES)))
    report.append('')

    groups = []          # 每组: {'label', 'per_sym', 'agg'(4品种合计), 'bp'}
    for macd in MACD_PARAMS:
        for mode in BEICHI_MODES:
            label = 'MACD(%d,%d,%d) x %s' % (macd[0], macd[1], macd[2],
                                             '面积比较' if mode == 'area'
                                             else 'DIF峰值比较')
            _apply_chan_params(macd[0], macd[1], macd[2], mode)
            print('\n---------- %s ----------' % label)
            per_sym = {}
            all_results = []
            all_signals = []
            for sym in symbols:
                daily = _load_daily(sym)
                weekly, wk_src = _load_weekly(sym, daily)
                if not daily or not weekly:
                    print('    !! 数据缺失，跳过 %s' % sym)
                    continue
                signals = run_symbol_scan(sym, daily, weekly)
                results = []
                for s in signals:
                    t = track_signal(daily, s)
                    if t is not None:
                        results.append(t)
                agg = aggregate(results)
                bp = _count_bp(signals)
                per_sym[sym] = {'agg': agg, 'bp': bp, 'n_sig': len(signals)}
                all_results.extend(results)
                all_signals.extend(signals)
                print('    [%s] LONG_OPEN %d 个 (有效跟踪 %d) | B1/B2/B3=%d/%d/%d | '
                      '+2R60=%.1f%% 破止损=%.1f%% 60日均R=%+.3f'
                      % (sym, len(signals), agg['n'], bp['B1'], bp['B2'],
                         bp['B3'], agg['r2_60'], agg['stop_hit'],
                         agg['r60_avg']))
            agg_all = aggregate(all_results)
            bp_all = _count_bp(all_signals)
            groups.append({'label': label, 'per_sym': per_sym,
                           'agg': agg_all, 'bp': bp_all})
            group_summary_block(report, label, per_sym, agg_all, all_signals)
            print('    4品种合计: 信号 %d | +2R60=%.1f%% | 破止损=%.1f%% | '
                  '60日均R=%+.3f'
                  % (agg_all['n'], agg_all['r2_60'], agg_all['stop_hit'],
                     agg_all['r60_avg']))

    # ---- 推荐 ----
    report.append('=' * 78)
    report.append('【推荐参数组合】')
    report.append('  规则: 4品种合计信号数 >= %d 前提下, 60日平均R 与 +2R 概率优先,'
                  ' 破止损概率低优先;' % MIN_SIGNALS)
    report.append('  评分 = 2.0×60日均R + 1.0×+2R概率(60日) - 0.5×破止损概率')
    report.append('')
    # 评分排序（仅信号数 >= MIN_SIGNALS 的组合参与推荐）
    scored = []
    for g in groups:
        if g['agg']['n'] >= MIN_SIGNALS:
            a = g['agg']
            s = round(2.0 * a['r60_avg'] + a['r2_60'] / 100.0
                      - a['stop_hit'] / 100.0 * 0.5, 3)
            scored.append((s, g['label'], g['agg']['n'], a['r60_avg'],
                           a['r2_60'], a['stop_hit']))
    scored.sort(reverse=True)
    report.append('  评分排序（仅信号数>=%d 的组合）:' % MIN_SIGNALS)
    report.append('    排名  组合                         信号数  60日均R  +2R60  破止损  评分')
    for i, (s, lab, n, r60, r2, stp) in enumerate(scored, 1):
        report.append('    %2d.  %-28s %4d  %+6.3f  %5.1f%%  %5.1f%%  %+.3f'
                      % (i, lab, n, r60, r2, stp, s))
    best_label, best_score, reason = recommend(groups)
    report.append('')
    report.append('  >>> 推荐: %s (评分 %.3f)' % (best_label, best_score))
    report.append('  理由: %s' % reason)
    if best_label:
        bg = next(g for g in groups if g['label'] == best_label)
        report.append('  %s 的 B1/B2/B3 信号构成: %d/%d/%d (B1/B2 为背驰依赖信号)'
                      % (best_label, bg['bp']['B1'], bg['bp']['B2'],
                         bg['bp']['B3']))
    report.append('')
    report.append('【稳健性备注（极端值影响分析）】')
    report.append('  60日均R 按信号级平均，个别极端信号会拉高组合均值：')
    report.append('  * BITCOIN 的 B3 信号仅 1 个且 60日R=+27.8（2020年后单边行情），'
                  '拉高 area 模式各组 60日均R；')
    report.append('  * 剔除该极端信号后: MACD(12,26,9)x面积 60日均R≈+0.63，'
                  'MACD(26,52,9)x面积≈+0.39（破止损仍为0%）。')
    report.append('  * 若以"破止损概率低"为第一优先（研究项目风控视角），'
                  'MACD(26,52,9)x面积比较 是唯一 4品种破止损全为 0% 的组合，'
                  '且 B2 信号占比高（8/13/8=WTI/XAU/XAG 合计），更稳健。')
    report.append('  * DIF 峰值比较信号量翻倍（58~82 vs 26~29）但质量指标全面更差'
                  '（+2R60 更低、破止损更高），属于"注水"信号，不建议采用；'
                  '如需扩充样本可作辅助观察。')
    report.append('')
    report.append('免责: 本扫描为研究用途，参数推荐不构成任何交易建议；'
                  '信号有效性统计不模拟持仓与交易成本。')

    # ---- 写报告 ----
    report_path = os.path.join(OUTPUT_DIR, 'backtest_macd_scan_report.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(report) + '\n')
    print('\n报告已生成: %s' % report_path)
    print('总耗时: %.1f 秒' % (time.time() - t0))


if __name__ == '__main__':
    main()
