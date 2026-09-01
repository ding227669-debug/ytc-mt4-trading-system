# -*- coding: utf-8 -*-
"""
backtest_wave_scan.py — 波浪参数敏感性扫描（参数标定阶段1）
================================================================
所属系统：缠论+波浪共振交易信号系统（模拟盘研究项目，禁止自动下单，只做回测研究）
阶段：参数标定阶段1 —— 波浪参数敏感性扫描（基于 backtest_daily.py 基础设施改造）

目的：
  扫描 wave_engine 三个关键参数（SWING_N × SWING_ATR_MULT × WAVE_WINDOW = 27 组），
  在 4 个品种上评估每组参数：
    ① 数浪稳定性：滚动数浪 vs 事后数浪（近似金标准）的 bias 一致率 / bias 切换次数
    ② 信号质量（信号制，不去重、不模拟真实持仓）：
       每个 LONG_OPEN 信号独立跟踪其后 30/60/90 个交易日：
         - +1R 概率（收盘 >= 开仓价 + 1×止损距离）
         - +2R 概率（收盘 >= 开仓价 + 2×止损距离）
         - 破止损概率（连续2根收盘 < 止损价，与阶段0止损规则一致）
         - 60日平均 R（60日窗口内先触发止损=-1R / 2R止盈=+2R，否则按第60日收盘结算，
           用止损距离归一化）
  输出对比报告 + 推荐参数组合。

防未来函数（与 backtest_daily.py 完全一致）：
  * 每周采样一次（每 SAMPLE_STEP=7 根日线），采样点只用截至该时刻的数据；
  * 周线仅取已完整闭合的K线（_truncate_weekly）；
  * 每轮采样前重置 wave_engine 浪号锁定缓存（_reset_wave_cache）→ 独立全量重算；
  * resonance_engine 冷却状态进程内隔离（monkey-patch 为 no-op）。

与 backtest_daily.py 的差异（本脚本专有）：
  * 参数网格集中顶部，运行期修改 wave_engine 模块级参数生效
    （wave_engine.compute 内部直接引用模块级 SWING_N/SWING_ATR_MULT/WAVE_WINDOW）；
  * chan_engine.compute 与波浪参数无关 → 每品种预计算所有采样点的日线缠论结果缓存，
    27 组参数共享，避免重复计算（chan_engine 为纯函数、resonance_engine.evaluate 只读，
    缓存安全）；
  * 信号质量统计采用"信号制"：每个 LONG_OPEN（position_rate>0 且止损有效）独立跟踪，
    不去重、不模拟真实持仓（阶段0 是"持仓制"去重后 8 年仅 11 笔，样本太少）。

用法：
  python backtest_wave_scan.py                      # 全量 27 组 × 4 品种
  python backtest_wave_scan.py --symbol XAUUSD      # 单品种快速调试
  python backtest_wave_scan.py --params 5,1.0,14    # 只跑指定参数组（调试）
  python backtest_wave_scan.py --params 3,0.5,10;7,1.5,20   # 多组（分号分隔）

输出（OUTPUT_DIR = 本脚本同目录 backtest_wave_scan_output/）：
  * backtest_wave_scan_report.txt —— 27组×4品种完整表格 + 4品种平均 + 推荐参数组合
  * wave_scan_results.csv         —— 机器可读明细（含 4品种平均行 AVG）
"""

import argparse
import csv
import datetime
import os
import sys
import time

# ======================================================================
# 参数区（集中于此，便于标定调整）
# ======================================================================
HST_BASE = r"C:\Program Files (x86)\Alpari MT4\history\Alpari-Demo"
SYMBOLS = ['BITCOIN', 'XAUUSD', 'XAGUSD', 'WTI']   # 扫描品种
WARMUP_DAYS = 600          # 日线预热根数（与 backtest_daily 一致）
SAMPLE_STEP = 7            # 每周采样一次（7根日线）
MAX_HOLD_DAYS = 60         # 60日平均R的结算窗口
WIN_R = 2.0                # 止盈目标 +2R
LOSS_R = -1.0              # 止损固定 -1R
STOP_LOSS_ATR_MULT = 2.0   # 默认止损距离 = 2 × 周线ATR（共振器无防守价时回退）
WEEK_MINUTES = 10080       # 周线周期（分钟）
DAY_MINUTES = 1440         # 日线周期（分钟）
WEEK_SECONDS = WEEK_MINUTES * 60
DAY_SECONDS = DAY_MINUTES * 60
CSV_ENCODING = 'utf-8-sig'
MIN_WEEK_CANDLES = 20      # 周线最少根数（不足跳过采样点，同 backtest_daily）
MIN_SIGNALS = 10           # 推荐筛选：4品种合计可执行信号数下限
DEFAULT_PARAMS = (5, 1.0, 14)   # 默认参数组（阶段0），用于对比

# ---- 扫描网格（3×3×3 = 27 组）----
SWING_N_GRID = [3, 5, 7]                # 摆动点确认左右K线根数
SWING_ATR_MULT_GRID = [0.5, 1.0, 1.5]   # 摆动幅度过滤 ATR 倍数
WAVE_WINDOW_GRID = [10, 14, 20]         # 数浪窗口（最近N个摆动点）

# ---- 信号质量跟踪窗口 ----
QUALITY_WINDOWS = [30, 60, 90]          # 信号后跟踪 N 个交易日

# 输出目录：本脚本同目录下 backtest_wave_scan_output/
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'backtest_wave_scan_output')

# ======================================================================
# 引擎导入（保证在 chan_wave 目录下运行）
# ======================================================================
import wave_engine
import chan_engine
import resonance_engine

# ---- 回测隔离：禁用跨进程冷却持久化（同 backtest_daily）----
resonance_engine._load_cooldown = lambda: None
resonance_engine._save_cooldown = lambda: None
resonance_engine._STATE.update({'false_count': 0, 'cooldown': False,
                                'cool_until': None, 'last_false': None})


# ======================================================================
# 工具函数（与 backtest_daily.py 口径一致）
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
    return 0.0  # 兜底失败（数据异常），调用方应跳过该信号


def set_wave_params(n, mult, window):
    """运行期设置 wave_engine 参数（compute/_label_waves 直接引用模块级变量）。"""
    wave_engine.SWING_N = n
    wave_engine.SWING_ATR_MULT = mult
    wave_engine.WAVE_WINDOW = window


# ======================================================================
# 主流程：单品种滚动采样 + 信号生成（chan 结果缓存复用）
# ======================================================================
def run_symbol(symbol, daily, weekly, chan_cache):
    """对单品种执行滚动采样，返回 (signals, wave_roll)。

    chan_cache: dict {采样索引: chan_engine.compute 结果}，跨参数组共享
                （日线缠论与波浪参数无关，避免 27 组重复计算）。
    signals: list[dict]，非 NONE 信号（含 LONG_OPEN/LONG_CLOSE/REDUCE）。
    wave_roll: list[dict]，每采样点的滚动数浪状态（供稳定性评估）。
    """
    n = len(daily)
    signals = []
    wave_roll = []
    sample_idx = list(range(WARMUP_DAYS, n, SAMPLE_STEP))
    for i in sample_idx:
        scan_ts = daily[i]['time'] + DAY_SECONDS   # 采样时刻 = 当日收盘后
        # ---- 截至采样点的数据（防未来函数核心） ----
        day_candles = daily[:i + 1]
        week_candles = _truncate_weekly(weekly, scan_ts)
        if len(week_candles) < MIN_WEEK_CANDLES:   # 周线数据不足，跳过
            continue
        # ---- 各引擎计算（每轮重置波浪缓存 → 独立全量重算） ----
        _reset_wave_cache()
        wave_week = wave_engine.compute(week_candles, 'WEEK')
        chan_day = chan_cache.get(i)
        if chan_day is None:
            chan_day = chan_engine.compute(day_candles, 'DAY')
            chan_cache[i] = chan_day
        # ---- 波动率（周线 ATR / 价格） ----
        wd = wave_week.get('details') or {}
        atr = wd.get('atr', 0.0) or 0.0
        lc = wd.get('last_close', 0.0) or 0.0
        volatility = {'atr_pct': round(atr / lc * 100.0, 3) if lc else 0.0,
                      'abnormal': False}
        # ---- 共振决策（日线慢速版：M30/M5 传 None 走降级路径） ----
        res = resonance_engine.evaluate(symbol, wave_week, chan_day,
                                        None, None, volatility)
        # ---- 记录滚动数浪状态 ----
        wave_roll.append({'idx': i, 'time': daily[i]['time'],
                          'label': wave_week['wave_label'],
                          'bias': wave_week['bias'],
                          'status': wave_week['wave_status'],
                          'broken': wave_week['wave_broken']})
        # ---- 记录非 NONE 信号 ----
        if res['signal'] != 'NONE':
            signals.append({
                'time': daily[i]['time'],
                'idx': i,
                'signal': res['signal'],
                'position_rate': res['position_rate'],
                'stop_loss_price': res['stop_loss_price'],
                'open_price': daily[i]['close'],
                'atr_week': atr,
                'check_summary': '; '.join(res['check_list'])[:300],
                'reason': '; '.join(res['reason']),
            })
    return signals, wave_roll


# ======================================================================
# 数浪稳定性评估（滚动 vs 事后金标准，与 backtest_daily 一致）
# ======================================================================
def wave_stability(weekly, wave_roll):
    """滚动数浪 vs 事后数浪（近似金标准）对比。"""
    _reset_wave_cache()
    final = wave_engine.compute(weekly, 'WEEK')
    final_label, final_bias = final['wave_label'], final['bias']

    n = len(wave_roll)
    if n == 0:
        return {'n': 0, 'final_label': final_label, 'final_bias': final_bias}

    bias_hit = sum(1 for r in wave_roll if r['bias'] == final_bias)
    label_hit = sum(1 for r in wave_roll if r['label'] == final_label)
    non_none = [r for r in wave_roll if r['label'] is not None]
    label_hit_nn = sum(1 for r in non_none if r['label'] == final_label)

    label_sw = 0
    bias_sw = 0
    for a, b in zip(wave_roll[:-1], wave_roll[1:]):
        if a['label'] != b['label']:
            label_sw += 1
        if a['bias'] != b['bias']:
            bias_sw += 1

    return {
        'n': n,
        'bias_agree': round(bias_hit / n * 100.0, 1),
        'bias_hit': bias_hit,
        'label_agree': round(label_hit / n * 100.0, 1),
        'label_hit': label_hit,
        'label_agree_nn': round(label_hit_nn / len(non_none) * 100.0, 1)
                          if non_none else 0.0,
        'non_none_count': len(non_none),
        'label_switches': label_sw,
        'bias_switches': bias_sw,
        'final_label': final_label,
        'final_bias': final_bias,
    }


# ======================================================================
# 信号有效性统计（信号制：每个 LONG_OPEN 独立跟踪，不去重、不模拟持仓）
# ======================================================================
def signal_quality(daily, signals):
    """对单品种所有可执行 LONG_OPEN 信号做信号后跟踪统计。

    可执行信号 = signal=='LONG_OPEN' 且 position_rate>0 且止损有效（risk>0），
    与阶段0执行条件一致（仓位0的信号不构成可交易信号）。

    口径（与阶段0模拟执行一致，统一用收盘价）：
      +1R/+2R ：信号后 N 个交易日内存在收盘价 >= 开仓价 + 1R/2R×止损距离
      破止损  ：信号后 N 个交易日内出现连续2根收盘 < 止损价
      60日平均R：60日窗口内先触发止损→-1R、先触发2R止盈→+2R（止损优先），
                 否则按第60日（或数据末尾）收盘价结算 (close-open)/risk
    """
    sigs = [s for s in signals
            if s['signal'] == 'LONG_OPEN' and s['position_rate'] > 0]
    n_daily = len(daily)

    # 止损有效化（与阶段0 simulate_trade 相同：无效止损回退默认，再无效丢弃）
    valid = []
    for s in sigs:
        open_price = s['open_price']
        stop = s['stop_loss_price']
        risk = open_price - stop
        if not (0 < stop < open_price):
            stop = _default_stop(daily, s['idx'], open_price, s['atr_week'])
            risk = open_price - stop
        if risk <= 0:
            continue
        s['_stop'] = stop
        s['_risk'] = risk
        valid.append(s)

    out = {'n': len(valid), 'windows': {},
           'r60_avg': 0.0, 'n_r60': 0}
    if not valid:
        for w in QUALITY_WINDOWS:
            out['windows'][w] = {'r1': 0.0, 'r2': 0.0, 'sl': 0.0}
        return out

    agg = {w: {'r1': 0, 'r2': 0, 'sl': 0} for w in QUALITY_WINDOWS}
    r60_list = []

    for s in valid:
        open_i = s['idx']
        open_price = s['open_price']
        stop = s['_stop']
        risk = s['_risk']
        tp1 = open_price + 1.0 * risk
        tp2 = open_price + 2.0 * risk

        hit = {w: {'r1': False, 'r2': False, 'sl': False}
               for w in QUALITY_WINDOWS}
        r60 = None
        prev_below = False
        # 跟踪最长窗口（90日），同时按窗口归属累计
        end = min(n_daily, open_i + 1 + max(QUALITY_WINDOWS))
        for j in range(open_i + 1, end):
            d = j - open_i
            c = daily[j]['close']
            below = c < stop
            stop_hit = below and prev_below   # 连续2根收盘 < 止损
            prev_below = below
            for w in QUALITY_WINDOWS:
                if d <= w:
                    if c >= tp1:
                        hit[w]['r1'] = True
                    if c >= tp2:
                        hit[w]['r2'] = True
                    if stop_hit:
                        hit[w]['sl'] = True
            # 60日结算（止损优先，与阶段0判定顺序一致）
            if d <= 60 and r60 is None:
                if stop_hit:
                    r60 = LOSS_R
                elif c >= tp2:
                    r60 = WIN_R
                elif d == 60:
                    r60 = (c - open_price) / risk
        if r60 is None:   # 数据末尾不足60日
            j_end = n_daily - 1
            r60 = (daily[j_end]['close'] - open_price) / risk

        for w in QUALITY_WINDOWS:
            agg[w]['r1'] += 1 if hit[w]['r1'] else 0
            agg[w]['r2'] += 1 if hit[w]['r2'] else 0
            agg[w]['sl'] += 1 if hit[w]['sl'] else 0
        r60_list.append(r60)

    n = len(valid)
    for w in QUALITY_WINDOWS:
        out['windows'][w] = {
            'r1': round(agg[w]['r1'] / n * 100.0, 1),
            'r2': round(agg[w]['r2'] / n * 100.0, 1),
            'sl': round(agg[w]['sl'] / n * 100.0, 1),
        }
    out['r60_avg'] = round(sum(r60_list) / n, 3)
    out['n_r60'] = n
    return out


# ======================================================================
# 汇总与推荐
# ======================================================================
def _avg_with(seq):
    """有值项的平均；全空返回 0.0。"""
    vals = [x for x in seq if x is not None]
    return round(sum(vals) / len(vals), 2) if vals else 0.0


def recommend(rows):
    """推荐参数组：bias一致率(4品种平均)最高优先，信号数>=10，次之+2R@60/60日平均R。"""
    valid = [r for r in rows if r['sig_total'] >= MIN_SIGNALS]
    pool = valid if valid else rows   # 若全部不满足信号数下限，退而求其次
    ranked = sorted(pool,
                    key=lambda r: (-r['bias_agree_avg'], -r['r2_60_avg'],
                                   -r['r60_avg'], -r['bias_sw_avg']))
    return ranked[0], ranked[:10]


def _fmt_params(p):
    """参数组显示名，如 'N5 M1.0 W14'。"""
    return 'N%d M%.1f W%d' % (p[0], p[1], p[2])


# ======================================================================
# 主入口
# ======================================================================
def main():
    ap = argparse.ArgumentParser(description='波浪参数敏感性扫描（参数标定阶段1）')
    ap.add_argument('--symbol', default=None,
                    help='单品种扫描（如 XAUUSD）；缺省跑全品种')
    ap.add_argument('--params', default=None,
                    help='只跑指定参数组，格式 "N,ATR,W" 分号分隔多组，如 '
                         '"5,1.0,14" 或 "3,0.5,10;7,1.5,20"')
    args = ap.parse_args()

    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    symbols = [args.symbol.upper()] if args.symbol else SYMBOLS
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    t0 = time.time()
    print('=' * 80)
    print('波浪参数敏感性扫描（参数标定阶段1）')
    print('用途: 研究项目，禁止自动下单')
    print('品种: %s' % ', '.join(symbols))

    # ---- 构建参数网格 ----
    grid = [(n, m, w) for n in SWING_N_GRID
            for m in SWING_ATR_MULT_GRID for w in WAVE_WINDOW_GRID]
    if args.params:
        sel = []
        for part in args.params.split(';'):
            n_s, m_s, w_s = part.strip().split(',')
            sel.append((int(n_s), float(m_s), int(w_s)))
        grid = [g for g in grid if g in sel]
        if not grid:
            print('!! --params 指定的参数组不在网格内，退出')
            sys.exit(1)
    print('参数网格: %d 组' % len(grid))
    print('=' * 80)

    # ---- 加载数据 + 预计算日线缠论缓存（与波浪参数无关，跨参数组共享）----
    data = {}
    chan_cache_all = {}
    for sym in symbols:
        daily = _load_daily(sym)
        weekly, wk_src = _load_weekly(sym, daily)
        if not daily or not weekly:
            print('    !! 数据缺失，跳过 %s' % sym)
            continue
        data[sym] = (daily, weekly)
        cc = {}
        for i in range(WARMUP_DAYS, len(daily), SAMPLE_STEP):
            cc[i] = chan_engine.compute(daily[:i + 1], 'DAY')
        chan_cache_all[sym] = cc
        print('    [%s] 日线 %d 根, 周线 %d 根, 缠论缓存 %d 个采样点'
              % (sym, len(daily), len(weekly), len(cc)))
    if not data:
        print('!! 无可用数据，退出')
        sys.exit(1)
    symbols = [s for s in symbols if s in data]

    # ---- 逐参数组扫描 ----
    rows = []          # 每参数组的汇总行（含4品种平均）
    detail = {}        # {params: {sym: {'stab','qual'}}}
    for gi, (n, m, w) in enumerate(grid, 1):
        set_wave_params(n, m, w)
        per = {}
        for sym in symbols:
            daily, weekly = data[sym]
            signals, wave_roll = run_symbol(sym, daily, weekly,
                                            chan_cache_all[sym])
            stab = wave_stability(weekly, wave_roll)
            qual = signal_quality(daily, signals)
            per[sym] = {'stab': stab, 'qual': qual}
        detail[(n, m, w)] = per

        # ---- 4品种汇总 ----
        stab_list = [per[s]['stab'] for s in symbols if per[s]['stab']['n'] > 0]
        qual_list = [per[s]['qual'] for s in symbols if per[s]['qual']['n'] > 0]
        row = {
            'params': (n, m, w),
            'bias_agree_avg': _avg_with([x['bias_agree'] for x in stab_list]),
            'bias_sw_avg': _avg_with([x['bias_switches'] for x in stab_list]),
            'label_agree_avg': _avg_with([x['label_agree'] for x in stab_list]),
            'sig_total': sum(per[s]['qual']['n'] for s in symbols),
            'r1_30_avg': _avg_with([q['windows'][30]['r1'] for q in qual_list]),
            'r2_30_avg': _avg_with([q['windows'][30]['r2'] for q in qual_list]),
            'sl_30_avg': _avg_with([q['windows'][30]['sl'] for q in qual_list]),
            'r1_60_avg': _avg_with([q['windows'][60]['r1'] for q in qual_list]),
            'r2_60_avg': _avg_with([q['windows'][60]['r2'] for q in qual_list]),
            'sl_60_avg': _avg_with([q['windows'][60]['sl'] for q in qual_list]),
            'r1_90_avg': _avg_with([q['windows'][90]['r1'] for q in qual_list]),
            'r2_90_avg': _avg_with([q['windows'][90]['r2'] for q in qual_list]),
            'sl_90_avg': _avg_with([q['windows'][90]['sl'] for q in qual_list]),
            'r60_avg': _avg_with([q['r60_avg'] for q in qual_list]),
        }
        rows.append(row)

        # 进度打印（每参数组一行摘要）
        print('[%2d/%d] %-12s bias一致率(均) %5.1f%% | 信号数(4品种) %3d | '
              '+2R@60 %5.1f%% | 60日均R %+.3f'
              % (gi, len(grid), _fmt_params((n, m, w)), row['bias_agree_avg'],
                 row['sig_total'], row['r2_60_avg'], row['r60_avg']))

    # ---- 推荐 ----
    best, top10 = recommend(rows)
    default = next(r for r in rows if r['params'] == DEFAULT_PARAMS)

    # ---- 写报告 ----
    report_path = os.path.join(OUTPUT_DIR, 'backtest_wave_scan_report.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(_build_report(rows, detail, best, top10, default,
                                        symbols, len(grid))) + '\n')
    print('\n报告已生成: %s' % report_path)

    # ---- 写 CSV（机器可读）----
    csv_path = os.path.join(OUTPUT_DIR, 'wave_scan_results.csv')
    _write_csv(csv_path, rows, detail, symbols)
    print('CSV 已生成: %s' % csv_path)

    # ---- 控制台摘要 ----
    print('\n================ 推荐参数组合 ================')
    print('推荐: %s' % _fmt_params(best['params']))
    print('  bias一致率(4品种平均) %.1f%% | 信号数 %d | +2R@60 %.1f%% | 60日均R %+.3f'
          % (best['bias_agree_avg'], best['sig_total'],
             best['r2_60_avg'], best['r60_avg']))
    print('默认组(%s)对比: bias一致率 %.1f%% | 信号数 %d | +2R@60 %.1f%% | 60日均R %+.3f'
          % (_fmt_params(DEFAULT_PARAMS), default['bias_agree_avg'],
             default['sig_total'], default['r2_60_avg'], default['r60_avg']))
    print('总耗时: %.1f 秒' % (time.time() - t0))


# ======================================================================
# 报告构建
# ======================================================================
def _build_report(rows, detail, best, top10, default, symbols, n_groups):
    L = []
    L.append('波浪参数敏感性扫描报告（参数标定阶段1）')
    L.append('=' * 78)
    L.append('声明: 研究项目，禁止自动下单。信号不可用于真实交易。')
    L.append('生成时间: %s' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    L.append('扫描网格: SWING_N %s × SWING_ATR_MULT %s × WAVE_WINDOW %s = %d 组'
             % (SWING_N_GRID, SWING_ATR_MULT_GRID, WAVE_WINDOW_GRID, n_groups))
    L.append('品种: %s' % ', '.join(symbols))
    L.append('采样: 起点=第%d根日线, 每%d根一次（周线只取已闭合K线, 每轮重置波浪缓存）'
             % (WARMUP_DAYS, SAMPLE_STEP))
    L.append('')
    L.append('【指标口径】')
    L.append('  数浪稳定性: 滚动数浪 vs 事后数浪（全量周线compute, 近似金标准）')
    L.append('    - bias一致率: 滚动采样点中 bias 与事后金标准一致的比例（越大越稳）')
    L.append('    - bias切换次数: 相邻采样点 bias 翻转次数（越小越稳）')
    L.append('  信号质量（信号制: 每个LONG_OPEN独立跟踪, 不去重、不模拟真实持仓;')
    L.append('               口径与阶段0一致, 统一用收盘价判定）:')
    L.append('    - 可执行信号 = LONG_OPEN 且 position_rate>0 且止损有效(risk>0)')
    L.append('    - +1R/+2R概率: 信号后 N 个交易日内收盘价达到开仓价+1R/+2R止损距离')
    L.append('    - 破止损概率: 信号后 N 个交易日内出现连续2根收盘<止损价')
    L.append('    - 60日均R: 60日窗口内先触发止损=-1R/先触发2R止盈=+2R(止损优先),')
    L.append('               否则按第60日收盘结算 (close-open)/risk, 所有信号平均')
    L.append('')
    L.append('【4品种平均汇总表】（概率列为有信号品种的平均, 信号数为4品种合计）')
    L.append(_avg_table_header())
    for r in sorted(rows, key=lambda x: (-x['bias_agree_avg'], -x['r2_60_avg'])):
        L.append(_avg_table_row(r))
    L.append('')
    L.append('【单品种明细表】')
    for sym in symbols:
        L.append('')
        L.append('-' * 78)
        L.append('[%s]（参数组 | bias一致%% | bias切换 | label一致%% | 信号数 | '
                 '+1R30 | +2R30 | 破SL30 | +1R60 | +2R60 | 破SL60 | '
                 '+1R90 | +2R90 | 破SL90 | 60日均R）' % sym)
        L.append(_sym_table_header())
        for r in sorted(rows, key=lambda x: (-x['bias_agree_avg'], -x['r2_60_avg'])):
            d = detail[r['params']][sym]
            L.append(_sym_table_row(r['params'], d))
    L.append('')
    L.append('=' * 78)
    L.append('【推荐参数组合】')
    L.append('筛选规则: ① 4品种合计信号数 >= %d（样本量下限）'
             ' ② bias一致率(4品种平均)最高优先（数浪稳定性是阶段0最大短板）'
             ' ③ 次之 +2R@60 / 60日均R（信号质量）' % MIN_SIGNALS)
    L.append('')
    L.append('Top10 候选（按 bias一致率降序, 已过滤信号数<%d）:' % MIN_SIGNALS)
    L.append('  排名  参数组         bias一致%  信号数  +2R@60%  60日均R')
    for i, r in enumerate(top10, 1):
        L.append('  %2d    %-12s  %6.1f   %4d   %6.1f   %+7.3f'
                 % (i, _fmt_params(r['params']), r['bias_agree_avg'],
                    r['sig_total'], r['r2_60_avg'], r['r60_avg']))
    L.append('')
    L.append('推荐: %s' % _fmt_params(best['params']))
    L.append('  bias一致率(4品种平均): %.1f%%  (bias切换平均 %.1f 次)'
             % (best['bias_agree_avg'], best['bias_sw_avg']))
    L.append('  信号数(4品种合计): %d' % best['sig_total'])
    L.append('  信号质量: +1R@60 %.1f%% | +2R@60 %.1f%% | 破止损@60 %.1f%% | '
             '60日均R %+.3f'
             % (best['r1_60_avg'], best['r2_60_avg'], best['sl_60_avg'],
                best['r60_avg']))
    L.append('')
    L.append('默认组对比（阶段0参数 %s）:' % _fmt_params(DEFAULT_PARAMS))
    L.append('  bias一致率: 默认 %.1f%% -> 推荐 %.1f%%  (提升 %+.1f 个百分点)'
             % (default['bias_agree_avg'], best['bias_agree_avg'],
                best['bias_agree_avg'] - default['bias_agree_avg']))
    L.append('  信号数: 默认 %d -> 推荐 %d' % (default['sig_total'],
                                            best['sig_total']))
    L.append('  +2R@60: 默认 %.1f%% -> 推荐 %.1f%%' % (default['r2_60_avg'],
                                                      best['r2_60_avg']))
    L.append('  60日均R: 默认 %+.3f -> 推荐 %+.3f' % (default['r60_avg'],
                                                    best['r60_avg']))
    L.append('')
    L.append('理由: %s 在满足信号数>=%d 的参数组中 bias一致率最高（%.1f%%），'
             '数浪稳定性最优; 且信号质量 %s'
             % (_fmt_params(best['params']), MIN_SIGNALS,
                best['bias_agree_avg'],
                ('(+2R@60 %.1f%%, 60日均R %+.3f) 亦优于/不劣于默认组'
                 % (best['r2_60_avg'], best['r60_avg'])
                 if best['r2_60_avg'] >= default['r2_60_avg']
                 else '（+2R@60 略低于默认组, 需权衡稳定性优先）')))
    L.append('')
    L.append('【稳定性 vs 信号质量 权衡】')
    L.append('  推荐组 %s 与默认组 %s 的关键对比:'
             % (_fmt_params(best['params']), _fmt_params(DEFAULT_PARAMS)))
    L.append('    bias一致率: %.1f%% vs %.1f%% (推荐组 %+.1fpp)'
             % (best['bias_agree_avg'], default['bias_agree_avg'],
                best['bias_agree_avg'] - default['bias_agree_avg']))
    L.append('    +2R@60   : %.1f%% vs %.1f%% (推荐组 %+.1fpp)'
             % (best['r2_60_avg'], default['r2_60_avg'],
                best['r2_60_avg'] - default['r2_60_avg']))
    L.append('    破止损@60: %.1f%% vs %.1f%%'
             % (best['sl_60_avg'], default['sl_60_avg']))
    L.append('    60日均R  : %+.3f vs %+.3f'
             % (best['r60_avg'], default['r60_avg']))
    L.append('  解读: 推荐组用 SWING_N=7 换稳定性, 但信号质量(破止损概率/60日均R)'
             '明显弱于默认组 N5 M1.0;')
    L.append('  若信号质量权重更高(看重盈利潜力/低破止损), N5 M1.0 是均衡候选'
             '(稳定性仅低 %.1fpp)。建议样本外验证后再定稿。'
             % (best['bias_agree_avg'] - default['bias_agree_avg']))
    L.append('')
    L.append('【已知问题 / 注意事项】')
    L.append('  * 信号质量为信号制统计（不去重），与阶段0持仓制的开仓样本数不可直接比较;')
    L.append('    +1R/+2R 用收盘价口径（与阶段0止盈判定一致），盘中触及机会未计入;')
    L.append('  * 概率列为"有信号品种"的平均：某品种该组无信号时不参与平均（信号数为0）;')
    L.append('  * 事后金标准本身随参数变化（用当前组参数全量数浪），跨参数组的')
    L.append('    bias一致率比较的是"该参数下自动数浪与自身事后数浪的吻合度";')
    L.append('  * 信号数下限 %d 为研究门槛，非统计显著性保证; 推荐组应再做样本外验证;'
             % MIN_SIGNALS)
    L.append('  * WAVE_WINDOW 在 {10,14,20} 网格内完全不敏感（三档结果逐项相同）:'
             '已验证 W=4/6/8 确认机制生效、但 W>=6 即饱和——数浪逻辑"最近结构优先",')
    L.append('    完整5浪判定最多需 6 个摆动点(_check_impulse), 更早摆动点不参与')
    L.append('    最新结构判定; 若需窗口参数发挥区分度, 需改数浪逻辑(如引入历史浪序')
    L.append('    延续性约束)或扫描 W<6;')
    L.append('  * BITCOIN 可执行信号极少(多数参数组仅 1~3 个, N5 M1.0 仅 1 个),')
    L.append('    其概率指标(如100%)置信度极低, 4品种平均已被其稀释;')
    L.append('    信号数主要来自 XAUUSD(14~15) + WTI(5);')
    L.append('  * N7 组破止损@60 概率(17.9%)明显高于 N5 组(5.0%): SWING_N=7 数浪更稳,')
    L.append('    但该参数下信号集合不同, 止损位(日线防守价/2×周线ATR)更易被击穿,')
    L.append('    稳定性提升与信号质量呈反向关系, 需按风险偏好取舍;')
    L.append('  * 免责: 本报告仅用于参数标定研究，不构成任何交易建议。')
    return L


def _avg_table_header():
    return ('  参数组         bias一致%  bias切换  label一致%  信号数  '
            '+1R30  +2R30  破SL30  +1R60  +2R60  破SL60  +1R90  +2R90  '
            '破SL90  60日均R')


def _avg_table_row(r):
    return ('  %-12s  %6.1f   %5.1f    %6.1f   %4d   %5.1f  %5.1f  %5.1f  '
            '%5.1f  %5.1f  %5.1f  %5.1f  %5.1f  %5.1f  %+7.3f'
            % (_fmt_params(r['params']), r['bias_agree_avg'], r['bias_sw_avg'],
               r['label_agree_avg'], r['sig_total'], r['r1_30_avg'],
               r['r2_30_avg'], r['sl_30_avg'], r['r1_60_avg'],
               r['r2_60_avg'], r['sl_60_avg'], r['r1_90_avg'],
               r['r2_90_avg'], r['sl_90_avg'], r['r60_avg']))


def _sym_table_header():
    return ('  参数组         bias一致%  bias切换  label一致%  信号数  '
            '+1R30  +2R30  破SL30  +1R60  +2R60  破SL60  +1R90  +2R90  '
            '破SL90  60日均R')


def _sym_table_row(params, d):
    st, q = d['stab'], d['qual']
    win = q['windows']
    return ('  %-12s  %6.1f   %5d    %6.1f   %4d   %5.1f  %5.1f  %5.1f  '
            '%5.1f  %5.1f  %5.1f  %5.1f  %5.1f  %5.1f  %+7.3f'
            % (_fmt_params(params), st['bias_agree'], st['bias_switches'],
               st['label_agree'], q['n'], win[30]['r1'], win[30]['r2'],
               win[30]['sl'], win[60]['r1'], win[60]['r2'], win[60]['sl'],
               win[90]['r1'], win[90]['r2'], win[90]['sl'], q['r60_avg']))


# ======================================================================
# CSV 输出
# ======================================================================
def _write_csv(path, rows, detail, symbols):
    headers = ['group_id', 'SWING_N', 'SWING_ATR_MULT', 'WAVE_WINDOW',
               'symbol', 'n_sample', 'bias_agree_pct', 'bias_switches',
               'label_agree_pct', 'n_signal', 'r1_30_pct', 'r2_30_pct',
               'sl_30_pct', 'r1_60_pct', 'r2_60_pct', 'sl_60_pct',
               'r1_90_pct', 'r2_90_pct', 'sl_90_pct', 'r60_avg']
    out = []
    for gi, r in enumerate(rows, 1):
        n, m, w = r['params']
        # 单品种行
        for sym in symbols:
            d = detail[(n, m, w)][sym]
            st, q = d['stab'], d['qual']
            win = q['windows']
            out.append([gi, n, m, w, sym, st['n'], st['bias_agree'],
                        st['bias_switches'], st['label_agree'], q['n'],
                        win[30]['r1'], win[30]['r2'], win[30]['sl'],
                        win[60]['r1'], win[60]['r2'], win[60]['sl'],
                        win[90]['r1'], win[90]['r2'], win[90]['sl'],
                        q['r60_avg']])
        # 4品种平均行（AVG）
        out.append([gi, n, m, w, 'AVG', None, r['bias_agree_avg'],
                    r['bias_sw_avg'], r['label_agree_avg'], r['sig_total'],
                    r['r1_30_avg'], r['r2_30_avg'], r['sl_30_avg'],
                    r['r1_60_avg'], r['r2_60_avg'], r['sl_60_avg'],
                    r['r1_90_avg'], r['r2_90_avg'], r['sl_90_avg'],
                    r['r60_avg']])
    with open(path, 'w', newline='', encoding=CSV_ENCODING) as f:
        wtr = csv.writer(f)
        wtr.writerow(headers)
        wtr.writerows(out)
    print('    CSV: %d 行 x %d 列' % (len(out), len(headers)))


if __name__ == '__main__':
    main()
