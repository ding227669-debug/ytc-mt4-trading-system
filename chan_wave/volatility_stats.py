# -*- coding: utf-8 -*-
"""
volatility_stats.py — 4品种日线 ATR_pct 分布统计（参数标定：异常波动率阈值）
============================================================================
所属系统：缠论+波浪共振交易信号系统（模拟盘研究项目，禁止自动下单）

用途：
  统计 4 品种（BITCOIN/XAUUSD/XAGUSD/WTI）日线真实波幅 ATR(14)（Wilder 平滑）
  占收盘价百分比 atr_pct 的分布，给出"异常波动率"建议阈值：
    atr_pct >= max(99分位, 均值+3σ)  →  视为异常波动率（跳空/异常巨量场景）
  供 daemon 的 volatility['abnormal'] 判定做参数标定参考。

数据源：C:\\Program Files (x86)\\Alpari MT4\\history\\Alpari-Demo\\<SYM>1440.hst
解析格式（实测 Alpari 文件验证）：148 字节版本头 + 每条 60 字节记录，
  记录 = struct '<qddddq'：ctm(8) + open(8) + high(8) + low(8) + close(8) + vol(8)
  （与 chan_engine.load_hst / daemon.load_candles 同一格式）
输出：volatility_stats_report.txt（本文件同目录，覆盖写）

关于"较稳妥者"的取法：
  99 分位 与 均值+3σ 两者取 max —— 阈值更高 = 更保守 = 正常波动被误判为
  异常（漏报信号）的概率更低。abnormal 触发会屏蔽一切信号（宁可少报不漏报），
  故保守方向（取大者）与系统语义一致。σ 为标准差（样本）。

运行：python volatility_stats.py
"""
import os
import struct
import datetime
import statistics

HST_BASE = r"C:\Program Files (x86)\Alpari MT4\history\Alpari-Demo"
SYMBOLS = ["BITCOIN", "XAUUSD", "XAGUSD", "WTI"]   # 4 品种
TF_DAY = 1440                                        # 日线周期（分钟数）
ATR_PERIOD = 14                                      # ATR 周期（Wilder 平滑）
REPORT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "volatility_stats_report.txt")


def load_daily(symbol):
    """读取 <SYM>1440.hst → bars 列表（dict: time/open/high/low/close/vol）。

    缺失/损坏 → 返回 None（不抛异常）。
    """
    path = os.path.join(HST_BASE, "%s%d.hst" % (symbol, TF_DAY))
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read()
        n = (len(raw) - 148) // 60
        if n <= 0:
            return None
        bars = []
        for i in range(n):
            rec = raw[148 + i * 60: 148 + i * 60 + 60]
            if len(rec) < 60:
                break
            t, o, h, l, c, v = struct.unpack("<qddddq", rec[:48])
            bars.append({"time": int(t), "open": o, "high": h,
                         "low": l, "close": c, "vol": float(v)})
        return bars
    except Exception as e:
        print("  !! 读取 %s1440.hst 失败: %s" % (symbol, e))
        return None


def true_range(bars, i):
    """第 i 根K线真实波幅 TR = max(高-低, |高-前收|, |低-前收|)。

    首根无前收 → 用 高-低（近似）。
    """
    h, l = bars[i]["high"], bars[i]["low"]
    if i == 0:
        return h - l
    pc = bars[i - 1]["close"]
    return max(h - l, abs(h - pc), abs(l - pc))


def atr_series(bars):
    """每根K线位置的 ATR(period)（Wilder 平滑）序列。

    首值 = 前 ATR_PERIOD 根 TR 的简单均值；其后
      atr = (prev_atr * (period-1) + tr) / period
    K线不足 period 根 → 用已积累 TR 的滚动简单均值（不抛异常）。
    """
    trs = [true_range(bars, i) for i in range(len(bars))]
    out = []
    if len(trs) <= ATR_PERIOD:
        s = 0.0
        for i, tr in enumerate(trs):
            s += tr
            out.append(s / (i + 1))
        return out
    atr = sum(trs[:ATR_PERIOD]) / ATR_PERIOD
    out.append(atr)
    for tr in trs[ATR_PERIOD:]:
        atr = (atr * (ATR_PERIOD - 1) + tr) / ATR_PERIOD
        out.append(atr)
    return out


def percentile(values, q):
    """百分位数（线性插值，与 numpy.percentile 默认算法一致）。

    values 未排序也可用；空序列返回 0.0。
    """
    if not values:
        return 0.0
    vs = sorted(values)
    k = (len(vs) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(vs) - 1)
    frac = k - lo
    return vs[lo] * (1 - frac) + vs[hi] * frac


def analyze(symbol):
    """统计单品种日线 atr_pct 分布。返回 (stats dict 或 None, 错误说明或 None)。"""
    bars = load_daily(symbol)
    if not bars or len(bars) < 2:
        return None, "数据缺失或不足(需>=2根K线)"
    atrs = atr_series(bars)
    # 每根K线 atr_pct = ATR(i) / close(i) * 100（百分比）
    atr_pct = [a / c * 100.0 for a, c in zip(atrs, [b["close"] for b in bars])
               if c > 0]
    n = len(atr_pct)
    if n == 0:
        return None, "收盘价全为0，无法计算 atr_pct"
    mean = statistics.mean(atr_pct)
    stdev = statistics.stdev(atr_pct) if n > 1 else 0.0
    p99 = percentile(atr_pct, 0.99)
    mean3 = mean + 3 * stdev
    # 建议阈值：99分位 与 均值+3σ 取较稳妥者（max=更保守，误报更少）
    th = max(p99, mean3)
    stats = {
        "n": n,
        "min": round(min(atr_pct), 4),
        "median": round(statistics.median(atr_pct), 4),
        "mean": round(mean, 4),
        "std": round(stdev, 4),
        "p90": round(percentile(atr_pct, 0.90), 4),
        "p95": round(percentile(atr_pct, 0.95), 4),
        "p99": round(p99, 4),
        "max": round(max(atr_pct), 4),
        "mean_plus_3std": round(mean3, 4),
        "suggest_threshold": round(th, 4),
        # 供参考：按建议阈值，历史数据中被判"异常"的比例
        "abnormal_pct_hist": round(sum(1 for v in atr_pct if v >= th) / n * 100.0, 2),
    }
    return stats, None


def main():
    print("=" * 84)
    print("4品种日线 ATR(14)%% 分布统计（数据源: %s）" % HST_BASE)
    print("=" * 84)
    lines = []
    lines.append("volatility_stats_report — 4品种日线 ATR_pct 分布（自动生成: %s）"
                 % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("口径: 每根日线K线位置的 ATR(14)(Wilder平滑) / 收盘价 * 100 (%%); "
                 "数据源 %s\\<SYM>1440.hst" % HST_BASE)
    lines.append("")
    header = "%-10s %7s %8s %8s %8s %8s %8s %8s %8s" % (
        "品种", "K线数", "min", "中位数", "均值", "p90", "p95", "p99", "max")
    lines.append(header)
    lines.append("-" * len(header))
    print(header)
    print("-" * len(header))
    results = {}
    for sym in SYMBOLS:
        stats, err = analyze(sym)
        if err:
            line = "%-10s  !! %s" % (sym, err)
            lines.append(line)
            print(line)
            continue
        results[sym] = stats
        line = "%-10s %7d %8.4f %8.4f %8.4f %8.4f %8.4f %8.4f %8.4f" % (
            sym, stats["n"], stats["min"], stats["median"], stats["mean"],
            stats["p90"], stats["p95"], stats["p99"], stats["max"])
        lines.append(line)
        print(line)
    # ---- 建议阈值 ----
    lines.append("")
    lines.append("建议异常波动率阈值（atr_pct >= 阈值 → abnormal=True）:")
    for sym, st in results.items():
        lines.append("  %-10s 99分位=%.4f%%  均值+3σ=%.4f%%  ->  建议阈值=%.4f%%"
                     "  (历史异常占比 %.2f%%)"
                     % (sym, st["p99"], st["mean_plus_3std"],
                        st["suggest_threshold"], st["abnormal_pct_hist"]))
    lines.append("  取法: max(99分位, 均值+3σ) —— 两者取较稳妥者(更大者)。abnormal 触发")
    lines.append("  会屏蔽一切信号(宁可少报不漏报), 故阈值取保守方向; 如需更敏感可")
    lines.append("  改用 95 分位, 如需更保守可改用 99.5 分位。")
    # ---- daemon abnormal 判定现状 + 建议接入点 ----
    lines.append("")
    lines.append("daemon 的 volatility['abnormal'] 判定现状（chan_wave_daemon.py "
                 "compute_volatility）:")
    lines.append("  - 已有判定框架: 依赖 wave_engine details 的 bars_count / last_range,")
    lines.append("    判定规则: 最近一根周线K线波幅(last_range) > 周线ATR * "
                 "ATR_ABNORMAL_MULT(3.0) -> abnormal=True。")
    lines.append("  - 但 wave_engine.compute 的 details 目前只输出 last_close / atr /")
    lines.append("    swings / label_checks, 从未输出 bars_count 与 last_range")
    lines.append("    -> compute_volatility 中 'if bars:' 恒为 False, abnormal 恒为 False,")
    lines.append("    判定实际是死代码, 从未生效。")
    lines.append("  - 接入点建议(二选一, 推荐A):")
    lines.append("    A. wave_engine.compute 的 details 补输出 last_range(最近一根K线")
    lines.append("       真实波幅 h-l) 与 bars_count, daemon 现有判定立即生效;")
    lines.append("    B. 在 daemon.compute_volatility 内用本报告的建议阈值直接判:")
    lines.append("       atr_pct(日线口径) >= 建议阈值 -> abnormal=True; 需注意 daemon")
    lines.append("       现用周线口径 atr_pct, 与本报告日线口径的量级不同, 建议先统一口径。")
    lines.append("")
    lines.append("注: 本报告为日线口径; daemon 现用周线口径 atr_pct。若采用建议 B,")
    lines.append("    建议在 daemon 侧对日线数据同样计算 atr_pct 后再比对阈值。")
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n报告已写入: %s" % REPORT_FILE)


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):      # Windows 控制台 UTF-8 兼容
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    main()
