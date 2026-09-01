# -*- coding: utf-8 -*-
"""
backtest_daily.py — 日线慢速版回测（周线波浪 + 日线缠论组合）
================================================================
所属系统：缠论+波浪共振交易信号系统（模拟盘研究项目，禁止自动下单，只做回测研究）
阶段：参数标定阶段0 —— 日线慢速版回测基础设施

设计要点（防未来函数）：
  * 每周采样一次：从日线数据 WARMUP_DAYS 根（预热，供缠论/波浪算法充分计算）
    开始，每 SAMPLE_STEP=7 根日线取一个采样点。
  * 每个采样点只能用"截至该采样时刻"的数据：
      - 日线：daily[:i+1]（第 i 根日线在 i+1 日 0 点闭合，采样时刻=当日收盘后）
      - 周线：仅包含已完整闭合的周线K线（w.time + 7天 <= 采样时刻），
        否则当周K线包含未来几天数据 = 未来函数
  * wave_engine 有模块级浪号锁定缓存（_CACHE，增量路径防抖动）——回测中
    每轮采样前重置，保证每个采样点是独立全量重算（滚动数浪与事后数浪公平对比）。
  * chan_engine.compute 为纯函数（无内部状态），直接传截断序列即可。
  * resonance_engine 冷却状态已做进程内隔离（monkey-patch 读写函数为 no-op），
    防止 daemon 的 state/cooldown.json 污染回测；回测内不反馈证伪，不触发冷却。

信号模拟执行（持仓管理，全部基于每日收盘价）：
  * 开仓：LONG_OPEN 信号，当日收盘价成交；止损价=共振器输出（日线防守价），
    无效时回退 2×周线ATR 默认止损
  * 平仓（按每日检查顺序）：
      1) 连续2根收盘 < 止损价  -> LOSS（R=-1）
      2) 收盘 >= 开仓价 + 2R   -> WIN（R=+2，记录达标日期）
      3) 周线转BEAR / wave_broken -> 提前平仓（R按实际）
      4) 持有 > 60 个交易日    -> 时间止损（R按实际）
      5) 数据末尾仍未平仓      -> 强制平仓（R按实际，结果=数据结束）
  * 同品种已有持仓时不重复开仓（信号去重）

数浪稳定性评估（金标准对照）：
  * 滚动序列：每个采样点的 wave_label/bias（独立全量重算）
  * 事后金标准：用全部周线数据 compute 一次的 wave_label/bias（近似金标准：
    能看到全貌的数浪结果）
  * 统计 bias 一致率 / label 一致率 / 标签切换次数（抖动率）——作为
    "自动数浪准不准"的量化代理指标。

输出（OUTPUT_DIR）：
  * signals_<SYM>.csv  —— 信号列表（时间/信号/仓位/止损/check_list摘要/是否执行）
  * trades_<SYM>.csv    —— 结算列表（开仓时间/平仓时间/持仓天数/R/结果）
  * wave_roll_<SYM>.csv —— 滚动数浪状态（每采样点 label/bias/status/broken）
  * backtest_report.txt —— 汇总报告（每品种统计 + 4品种合计 + 数浪稳定性）

用法：
  python backtest_daily.py --symbol XAUUSD   # 单品种
  python backtest_daily.py                   # 全品种（BITCOIN/XAUUSD/XAGUSD/WTI）
"""

import argparse
import csv
import datetime
import os
import sys
import time

# ======================================================================
# 参数区（集中于此，便于后续标定）
# ======================================================================
HST_BASE = r"C:\Program Files (x86)\Alpari MT4\history\Alpari-Demo"
SYMBOLS = ['BITCOIN', 'XAUUSD', 'XAGUSD', 'WTI']   # 回测品种
WARMUP_DAYS = 600          # 日线预热根数（从第600根起开始采样，留足算法预热期）
SAMPLE_STEP = 7            # 每周采样一次（7根日线）
MAX_HOLD_DAYS = 60         # 时间止损：持仓超过60个交易日平仓
WIN_R = 2.0                # 止盈目标 +2R
LOSS_R = -1.0              # 止损固定 -1R
STOP_LOSS_ATR_MULT = 2.0   # 默认止损距离 = 2 × 周线ATR（共振器无防守价时回退）
WEEK_MINUTES = 10080       # 周线周期（分钟）
DAY_MINUTES = 1440         # 日线周期（分钟）
WEEK_SECONDS = WEEK_MINUTES * 60   # 604800 秒
DAY_SECONDS = DAY_MINUTES * 60     # 86400 秒
CSV_ENCODING = 'utf-8-sig'         # Excel 打开中文不乱码

# 输出目录：本脚本同目录下 backtest_daily_output/
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'backtest_daily_output')

# ======================================================================
# 引擎导入（保证在 chan_wave 目录下运行）
# ======================================================================
import wave_engine
import chan_engine
import resonance_engine

# ---- 回测隔离：禁用跨进程冷却持久化 ----
# daemon（Windows 计划任务每30分钟启新进程）用 state/cooldown.json 持久化冷却；
# 回测进程若读到 daemon 写入的冷却状态会被错误屏蔽信号。这里把读写函数替换为
# no-op 并重置内存状态，回测与 daemon 完全隔离（不动磁盘文件）。
resonance_engine._load_cooldown = lambda: None
resonance_engine._save_cooldown = lambda: None
resonance_engine._STATE.update({'false_count': 0, 'cooldown': False,
                                'cool_until': None, 'last_false': None})


# ======================================================================
# 工具函数
# ======================================================================
def _fmt_date(ts):
    """.hst 时间戳为服务器时间（EET，UTC+2/+3），显示时 +3h 贴近 MT4 时间。"""
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
    """截断周线到采样时刻：只保留已完整闭合的周线K线。

    周线K线 w 覆盖 [w.time, w.time+7天)，在 w.time+7天 时刻闭合；
    采样时刻 scan_ts = 采样日收盘后（daily[i].time + 1天）。
    若当周K线未闭合（采样日在周中），包含它 = 用了未来几天的高低价 = 未来函数。
    """
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


# ======================================================================
# 主流程：单品种滚动采样 + 信号生成
# ======================================================================
def run_symbol(symbol, daily, weekly):
    """对单品种执行滚动采样，返回 (signals, wave_roll)。

    signals: list[dict]，非 NONE 信号（含 LONG_OPEN/LONG_CLOSE/REDUCE），
             LONG_OPEN 且仓位>0 且止损有效者才进入模拟执行。
    wave_roll: list[dict]，每采样点的滚动数浪状态（供稳定性评估）。
    """
    n = len(daily)
    signals = []
    wave_roll = []
    # 采样点索引序列（从 WARMUP_DAYS 起，每 SAMPLE_STEP 根取一次）
    sample_idx = list(range(WARMUP_DAYS, n, SAMPLE_STEP))
    print('    [%s] 日线 %d 根, 采样起点=第%d根, 共 %d 次采样'
          % (symbol, n, WARMUP_DAYS, len(sample_idx)))

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
                'executed': 0,      # 是否实际开仓（下方模拟执行时置 1）
                'skip_note': '',
            })
    return signals, wave_roll


# ======================================================================
# 模拟执行：单笔持仓跟踪
# ======================================================================
def simulate_trade(symbol, daily, sig, sample_idx, sample_states):
    """按日线逐日跟踪一笔 LONG_OPEN 持仓，返回结算 dict 或 None（无法开仓）。

    sample_idx: 全部采样点索引列表（升序）
    sample_states: dict {采样索引: (bias, broken)} —— 持仓期间周线状态
                   取最近采样点的结果（周线每周才确认一次，与真实一致）
    """
    open_i = sig['idx']
    open_price = sig['open_price']
    stop = sig['stop_loss_price']
    risk = open_price - stop
    # ---- 止损有效性：0 < stop < open_price，否则回退默认止损 ----
    if not (0 < stop < open_price):
        stop = _default_stop(daily, open_i, open_price, sig['atr_week'])
        risk = open_price - stop
    if risk <= 0:                 # 兜底仍无效 → 放弃该信号
        return None
    tp = open_price + WIN_R * risk

    # ---- 周线状态指针：定位到开仓采样点 ----
    k = 0
    while k < len(sample_idx) and sample_idx[k] <= open_i:
        k += 1
    cur_bias, cur_broken = sample_states.get(open_i, ('BULL', False))

    n = len(daily)
    exit_info = None
    for j in range(open_i + 1, n):
        c = daily[j]['close']
        # 1) 止损：连续2根收盘 < 止损价（盘中穿刺不算）→ -1R
        if c < stop and daily[j - 1]['close'] < stop:
            exit_info = (j, c, LOSS_R, '止损', 'LOSS')
            break
        # 2) 止盈：达到 +2R → 平仓 WIN
        if c >= tp:
            exit_info = (j, c, WIN_R, '2R止盈', 'WIN')
            break
        # 3) 周线状态更新：越过采样点则采用该采样点的最新波浪结果
        while k < len(sample_idx) and sample_idx[k] <= j:
            cur_bias, cur_broken = sample_states.get(
                sample_idx[k], (cur_bias, cur_broken))
            k += 1
        # 4) 周线转 BEAR / 浪型破坏 → 提前平仓（R 按实际）
        if cur_bias == 'BEAR':
            exit_info = (j, c, None, '周线转BEAR', 'WAVE_EXIT')
            break
        if cur_broken:
            exit_info = (j, c, None, '浪型破坏', 'WAVE_EXIT')
            break
        # 5) 时间止损：持有超过 MAX_HOLD_DAYS 个交易日
        if (j - open_i) > MAX_HOLD_DAYS:
            exit_info = (j, c, None, '时间止损(%d日)' % (j - open_i), 'TIME_STOP')
            break

    # ---- 数据末尾仍未平仓 → 强制平仓 ----
    if exit_info is None:
        j = n - 1
        exit_info = (j, daily[j]['close'], None, '数据结束', 'TIME_END')

    j, exit_price, r_fixed, reason, result = exit_info
    if r_fixed is not None:
        r = r_fixed
    else:
        r = round((exit_price - open_price) / risk, 2)   # R 按实际计算
    return {
        'symbol': symbol,
        'open_idx': open_i,
        'exit_idx': j,
        'open_time': daily[open_i]['time'],
        'open_price': round(open_price, 2),
        'stop_loss': round(stop, 2),
        'risk': round(risk, 2),
        'exit_time': daily[j]['time'],
        'exit_price': round(exit_price, 2),
        'hold_days': j - open_i,
        'r': r,
        'result': result,
        'reason': reason,
    }


# ======================================================================
# 数浪稳定性评估（滚动 vs 事后金标准）
# ======================================================================
def wave_stability(weekly, wave_roll):
    """滚动数浪 vs 事后数浪（近似金标准）对比。

    返回 dict: bias 一致率 / label 一致率 / 非None label 一致率 /
               标签切换次数 / bias 切换次数 / 事后金标准 label/bias。
    """
    # 事后金标准：用全部周线数据独立重算一次（先重置缓存）
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

    # 抖动率：相邻采样点标签/方向切换次数
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
# 统计与报告
# ======================================================================
def compute_stats(trades):
    """交易统计：胜率 / 总R / 期望R / 最大连续亏损 / 最长空窗。"""
    n = len(trades)
    if n == 0:
        return {'n': 0, 'win_rate': 0.0, 'total_r': 0.0, 'expect_r': 0.0,
                'max_loss_streak': 0, 'longest_gap': 0, 'gaps': []}
    wins = sum(1 for t in trades if t['r'] > 0)
    total_r = round(sum(t['r'] for t in trades), 2)
    # 最大连续亏损（按平仓顺序，R<0 视为亏损）
    streak = max_streak = 0
    for t in trades:
        if t['r'] < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    # 最长空窗：首笔开仓前的空窗 + 相邻两次开仓的日线索引间隔
    gaps = []
    if trades:
        gaps.append(trades[0]['open_idx'] - WARMUP_DAYS)   # 采样起点->首笔开仓
    for a, b in zip(trades[:-1], trades[1:]):
        gaps.append(b['open_idx'] - a['open_idx'])
    longest_gap = max(gaps) if gaps else 0
    return {'n': n,
            'win_rate': round(wins / n * 100.0, 1),
            'wins': wins,
            'total_r': total_r,
            'expect_r': round(total_r / n, 3),
            'max_loss_streak': max_streak,
            'longest_gap': longest_gap}


def write_csv(path, headers, rows):
    """写 CSV（utf-8-sig）。"""
    with open(path, 'w', newline='', encoding=CSV_ENCODING) as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)
    print('    已生成: %s' % path)


# ======================================================================
# 主入口
# ======================================================================
def main():
    ap = argparse.ArgumentParser(description='日线慢速版回测（周线波浪+日线缠论）')
    ap.add_argument('--symbol', default=None,
                    help='单品种回测（如 XAUUSD）；缺省跑全品种')
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
    print('=' * 80)
    print('日线慢速版回测（周线波浪 + 日线缠论组合）')
    print('用途: 参数标定（研究项目，禁止自动下单）')
    print('品种: %s' % ', '.join(symbols))
    print('=' * 80)

    report_lines = []
    report_lines.append('日线慢速版回测报告（周线波浪 + 日线缠论组合）')
    report_lines.append('=' * 78)
    report_lines.append('声明: 日线慢速版回测，用于参数标定（周线+日线组合），'
                        '非生产配置（生产是周线+M30+M5）。')
    report_lines.append('生成时间: %s'
                        % datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    report_lines.append('回测区间: 按各品种 .hst 数据（约2018-2026，BITCOIN 自2020-06起）')
    report_lines.append('')
    report_lines.append('【防未来函数设计】')
    report_lines.append('  * 每周采样一次（每7根日线），采样点只用截至该时刻的数据；')
    report_lines.append('  * 周线仅取已完整闭合的K线（当周K线未走完不参与，避免用未来数据）；')
    report_lines.append('  * wave_engine 浪号锁定缓存每轮采样前重置（独立全量重算）；')
    report_lines.append('  * resonance_engine 冷却状态进程内隔离（不读 daemon 的 cooldown.json）。')
    report_lines.append('')
    report_lines.append('【M30/M5 缺失时的行为（日线慢速版降级路径）】')
    report_lines.append('  * 买点判定: M30 买点 -> 改用日线缠论买点(B1/B2/B3)；')
    report_lines.append('  * 背驰确认: M30/M5 背驰检查跳过（无次级别确认层级）；')
    report_lines.append('  * 止损位: 取日线防守价 defend_price（中枢下沿）；')
    report_lines.append('  * 平仓条件: LONG_CLOSE① 用日线收盘判断连续2根破位；')
    report_lines.append('    M30/M5 卖点类平仓条件自动跳过。')
    report_lines.append('')

    grand = {'signals': 0, 'trades': 0, 'total_r': 0.0, 'wins': 0}
    for sym in symbols:
        print('\n---------- %s ----------' % sym)
        daily = _load_daily(sym)
        weekly, wk_src = _load_weekly(sym, daily)
        if not daily or not weekly:
            print('    !! 数据缺失，跳过 %s' % sym)
            report_lines.append('[%s] 数据缺失，跳过' % sym)
            continue

        # ---- 1. 滚动采样 + 信号生成 ----
        signals, wave_roll = run_symbol(sym, daily, weekly)

        # ---- 2. 模拟执行（按采样点顺序，持仓中忽略新 LONG_OPEN） ----
        sample_idx = [s['idx'] for s in signals]          # 非 NONE 信号的 idx
        # 周线状态表：{采样点idx: (bias, broken)}，显式键对应（防采样点跳过错位）
        all_sample_idx = list(range(WARMUP_DAYS, len(daily), SAMPLE_STEP))
        sample_states = {r['idx']: (r['bias'], r['broken']) for r in wave_roll}

        trades = []
        position = None            # 当前持仓 {'exit_idx': 平仓日索引}；None=空仓
        for sig in signals:
            if sig['signal'] != 'LONG_OPEN':
                continue
            # 信号去重：持仓期间（开仓日 <= 信号日 <= 平仓日）不重复开仓
            if position is not None and sig['idx'] <= position['exit_idx']:
                sig['executed'] = 0
                sig['skip_note'] = '已有持仓，信号去重跳过'
                continue
            if sig['position_rate'] <= 0:
                sig['executed'] = 0
                sig['skip_note'] = '仓位=0（非2/3/5浪或非买点），不执行'
                continue
            tr = simulate_trade(sym, daily, sig, all_sample_idx, sample_states)
            if tr is None:
                sig['executed'] = 0
                sig['skip_note'] = '止损无效（数据异常），不执行'
                continue
            sig['executed'] = 1
            trades.append(tr)
            position = {'exit_idx': tr['exit_idx']}

        # ---- 3. 数浪稳定性 ----
        stab = wave_stability(weekly, wave_roll)

        # ---- 4. 输出 CSV ----
        sig_rows = [[_fmt_date(s['time']), s['signal'], s['position_rate'],
                     s['stop_loss_price'], round(s['open_price'], 2),
                     s['reason'], s['skip_note'] or ('执行' if s['executed'] else '未执行')]
                    for s in signals]
        write_csv(os.path.join(OUTPUT_DIR, 'signals_%s.csv' % sym),
                  ['时间', '信号', '仓位', '止损价', '采样日收盘', '原因', '执行状态'],
                  sig_rows)
        tr_rows = [[t['symbol'], _fmt_date(t['open_time']), t['open_price'],
                    t['stop_loss'], t['risk'], _fmt_date(t['exit_time']),
                    t['exit_price'], t['hold_days'], t['r'], t['result'],
                    t['reason']] for t in trades]
        write_csv(os.path.join(OUTPUT_DIR, 'trades_%s.csv' % sym),
                  ['品种', '开仓时间', '开仓价', '止损价', '风险距离', '平仓时间',
                   '平仓价', '持仓天数', 'R', '结果', '平仓原因'], tr_rows)
        wr_rows = [[_fmt_date(r['time']), r['label'], r['bias'], r['status'],
                    r['broken']] for r in wave_roll]
        write_csv(os.path.join(OUTPUT_DIR, 'wave_roll_%s.csv' % sym),
                  ['采样时间', 'wave_label', 'bias', 'status', 'wave_broken'],
                  wr_rows)

        # ---- 5. 品种统计 + 报告段落 ----
        st = compute_stats(trades)
        open_signals = sum(1 for s in signals if s['executed'] == 1)
        skip_dup = sum(1 for s in signals if s['skip_note'] == '已有持仓，信号去重跳过')
        grand['signals'] += open_signals
        grand['trades'] += st['n']
        grand['total_r'] += st['total_r']
        grand['wins'] += st['wins']

        print('    LONG_OPEN信号: %d (开仓 %d, 去重跳过 %d, 仓位0/无效 %d)'
              % (sum(1 for s in signals if s['signal'] == 'LONG_OPEN'),
                 open_signals, skip_dup,
                 sum(1 for s in signals if s['signal'] == 'LONG_OPEN')
                 - open_signals - skip_dup))
        print('    平仓: %d 笔 | 胜率 %.1f%% | 总R %+.2f | 期望R/信号 %.3f'
              % (st['n'], st['win_rate'], st['total_r'], st['expect_r']))
        print('    最大连续亏损 %d | 最长空窗 %d 根日线' % (st['max_loss_streak'],
                                                    st['longest_gap']))
        print('    数浪稳定性: bias一致率 %.1f%% | label一致率 %.1f%% (非None %.1f%%) | '
              '标签切换 %d 次 / %d 次采样'
              % (stab['bias_agree'], stab['label_agree'], stab['label_agree_nn'],
                 stab['label_switches'], stab['n']))

        report_lines.append('-' * 78)
        report_lines.append('[%s] 回测摘要' % sym)
        report_lines.append('  数据: 日线 %d 根 (%s ~ %s), 周线 %d 根 (%s)'
                            % (len(daily), _fmt_date(daily[0]['time']),
                               _fmt_date(daily[-1]['time']), len(weekly), wk_src))
        report_lines.append('  采样: 起点=第%d根, 每%d根一次, 共 %d 次采样'
                            % (WARMUP_DAYS, SAMPLE_STEP, len(wave_roll)))
        report_lines.append('  信号: LONG_OPEN %d 个 (开仓 %d, 去重跳过 %d, '
                            '仓位0/无效 %d)'
                            % (sum(1 for s in signals if s['signal'] == 'LONG_OPEN'),
                               open_signals, skip_dup,
                               sum(1 for s in signals
                                   if s['signal'] == 'LONG_OPEN')
                               - open_signals - skip_dup))
        report_lines.append('  平仓: %d 笔 | 胜率 %.1f%% (%d胜/%d负, R>0计胜)'
                            % (st['n'], st['win_rate'], st['wins'],
                               st['n'] - st['wins']))
        report_lines.append('  总R: %+.2f | 期望R/信号: %.3f'
                            % (st['total_r'], st['expect_r']))
        report_lines.append('  最大连续亏损: %d 笔 | 最长空窗: %d 根日线'
                            % (st['max_loss_streak'], st['longest_gap']))
        report_lines.append('  平仓明细: %s'
                            % '; '.join('%s x%d' % (k, v) for k, v in
                                        _count_by(trades, 'result').items()))
        report_lines.append('  输出: signals_%s.csv / trades_%s.csv / wave_roll_%s.csv'
                            % (sym, sym, sym))
        report_lines.append('')
        report_lines.append('  [数浪稳定性] 滚动数浪 vs 事后数浪（近似金标准）')
        report_lines.append('    事后金标准: wave_label=%s bias=%s (全量数据compute)'
                            % (stab['final_label'], stab['final_bias']))
        report_lines.append('    bias 一致率: %.1f%% (%d/%d 采样点与事后一致)'
                            % (stab['bias_agree'], stab['bias_hit'], stab['n']))
        report_lines.append('    label 一致率: %.1f%% (%d/%d) | 非None label 一致率: '
                            '%.1f%% (%d个非None采样点)'
                            % (stab['label_agree'], stab['label_hit'], stab['n'],
                               stab['label_agree_nn'], stab['non_none_count']))
        report_lines.append('    标签切换次数: label %d 次 / bias %d 次 (共 %d 次采样, '
                            '切换频繁=数浪不稳定)'
                            % (stab['label_switches'], stab['bias_switches'],
                               stab['n']))
        report_lines.append('    解读: 事后数浪能看到全貌，近似金标准；一致率低说明自动数浪'
                            '对窗口敏感，需参数标定/人工复核。')
        report_lines.append('    注意: label 一致率天然偏低——波浪会随行情推进自然切换'
                            '(1->2->3->4->5->A->B->C)，滚动早期label与事后最终label'
                            '本就不可能相同；bias 一致率更能反映大方向数浪稳定性。')

    # ---- 合计 ----
    report_lines.append('=' * 78)
    report_lines.append('[4品种合计]')
    report_lines.append('  开仓总数: %d | 平仓总数: %d | 胜率: %.1f%% | 总R: %+.2f'
                        % (grand['signals'], grand['trades'],
                           round(grand['wins'] / grand['trades'] * 100.0, 1)
                           if grand['trades'] else 0.0, grand['total_r']))
    report_lines.append('  期望R/信号: %.3f'
                        % (grand['total_r'] / grand['trades']
                           if grand['trades'] else 0.0))
    report_lines.append('')
    report_lines.append('免责: 本回测为研究用途，信号不可用于自动下单。'
                        '参数未标定，结果不构成任何交易建议。')

    # ---- 写报告 ----
    report_path = os.path.join(OUTPUT_DIR, 'backtest_report.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(report_lines) + '\n')
    print('\n报告已生成: %s' % report_path)
    print('总耗时: %.1f 秒' % (time.time() - t0))


def _count_by(trades, key):
    """按字段统计计数（保持出现顺序）。"""
    out = {}
    for t in trades:
        v = t.get(key, '未知')
        out[v] = out.get(v, 0) + 1
    return out


if __name__ == '__main__':
    main()
