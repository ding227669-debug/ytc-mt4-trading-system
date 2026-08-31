# -*- coding: utf-8 -*-
"""
FVG Open-Range Breakout backtest (Alpari MT4 .hst data)
规则来源: 交易之家B站《这套'无聊到炸'的交易系统》(BV1ec2TBZEg6) 视频转写
完整规则文档: Trading-Knowledge/strategy/fvg-open-range-system.md

规则:
  1. 每天美股开盘 9:30-9:45 ET 第一根15分钟K线 -> 区间 (range_high/range_low)
  2. 5分钟图等 FVG 三K线形态确认突破 (做多: 收盘>区间高; 做空: 收盘<区间低)
     - 中间K线为强势大实体, 第一根K线影线与第三根K线影线间留缺口
     - 三根K线至少一根收盘在区间内 + 至少一根收盘在区间外
     - 不限于首次突破
  3. 限价单挂在缺口处 (做多=缺口下沿/第一根K线高点, 做空=缺口上沿/第一根K线低点)
  4. 止损 = FVG 第一根K线极值外侧 (做多=第一根K线低点下方, 做空=第一根K线高点上方)
  5. 止盈 = 固定 2:1 (entry +/- 2*(entry-SL))
  6. 入场须在东部 12:00 前 (服务器时间 19:00), 未成交撤单
  7. 每天最多一单 (当天第一个有效 FVG 方向)

时间: Alpari 服务器时间 = EET (UTC+2冬/UTC+3夏), 美股9:30ET = 服务器16:30 全年恒定
用法: python fvg_backtest.py [SYMBOL] [起始日YYYY-MM-DD] [结束日YYYY-MM-DD]
"""
import os, sys, struct, datetime

BASE = r"C:\Program Files (x86)\Alpari MT4\history\Alpari-Demo"
SYM = sys.argv[1] if len(sys.argv) > 1 else "US500"
FORCE_M15 = any(a == "--m15" for a in sys.argv)
pos_args = [a for a in sys.argv[1:] if not a.startswith("--")]
DATE_FROM = pos_args[1] if len(pos_args) > 1 else "2025-01-01"
DATE_TO = pos_args[2] if len(pos_args) > 2 else "2026-08-31"

# ---- 时间常量 (服务器时间 = 美股ET + 7h) ----
OPEN_H, OPEN_M = 16, 30      # 美股 9:30 ET 开盘
RANGE_END_H, RANGE_END_M = 16, 45   # 第一根15分钟K线收盘
DEADLINE_H, DEADLINE_M = 19, 0      # 东部 12:00 ET 截止

def read_hst(name):
    p = os.path.join(BASE, name)
    if not os.path.exists(p):
        print(f"!! MISSING {p}"); return None
    sz = os.path.getsize(p)
    n = (sz - 148) // 60
    bars = []
    with open(p, 'rb') as f:
        f.seek(148)
        for _ in range(n):
            rec = f.read(60)
            t, o, h, l, c = struct.unpack('<qdddd', rec[:40])
            bars.append((t, o, h, l, c))
    bars.sort()
    return bars

def resample_m1(m1):
    """M1 -> M5 重采样"""
    out, cur = [], None
    for t, o, h, l, c in m1:
        bucket = t - (t % 300)
        if cur is None or bucket != cur[0]:
            if cur: out.append(tuple(cur))
            cur = [bucket, o, h, l, c]
        else:
            cur[2] = max(cur[2], h); cur[3] = min(cur[3], l); cur[4] = c
    if cur: out.append(tuple(cur))
    return out

def dt(ts): return datetime.datetime.utcfromtimestamp(ts) + datetime.timedelta(hours=3)  # 服务器时间 (EET夏) 显示用

def trading_day(ts):
    """返回该时间戳所属的美股交易日 (服务器时间周一~周五)"""
    d = datetime.datetime.utcfromtimestamp(ts) + datetime.timedelta(hours=3)
    if d.weekday() >= 5: return None
    return d.date()

def main():
    m15 = read_hst(f"{SYM}15.hst")
    m5 = read_hst(f"{SYM}5.hst")
    sig_period = "M5"
    if FORCE_M15 or m5 is None or len(m5) < 500:
        # M5 不足: 尝试 M1 重采样 (覆盖太短则直接 M15 fallback)
        m1 = read_hst(f"{SYM}1.hst")
        if m1 and len(m1) > 4000:
            m5 = resample_m1(m1)
            sig_period = "M5(from M1)"
        else:
            m5 = m15
            sig_period = "M15(fallback)"
            print(f"!! M5 数据不足, 用 M15 做信号周期 (信号更粗, 仅初测)")
    if m15 is None or m5 is None:
        print("需要 M15 历史数据 (先开 H1 图让 MT4 下载)"); return
    print(f"== FVG backtest: {SYM} {DATE_FROM} ~ {DATE_TO} ==  M15:{len(m15)} signal={sig_period}:{len(m5)}")

    # 按交易日分组 M15, 找每天 16:30-16:45 的第一根K线
    from_ts = int(datetime.datetime.strptime(DATE_FROM, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc).timestamp()) - 3*3600
    to_ts = int(datetime.datetime.strptime(DATE_TO, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc).timestamp()) - 3*3600 + 86400

    days = {}  # date -> (range_high, range_low, first_bar_ts)
    for t, o, h, l, c in m15:
        if t < from_ts or t > to_ts: continue
        d = trading_day(t)
        if d is None: continue
        hm = (datetime.datetime.utcfromtimestamp(t) + datetime.timedelta(hours=3)).hour * 60 + (datetime.datetime.utcfromtimestamp(t) + datetime.timedelta(hours=3)).minute
        if hm == OPEN_H * 60 + OPEN_M:  # 16:30 服务器时间
            days[d] = (h, l, t)

    # 按交易日分组 M5
    m5_by_day = {}
    for t, o, h, l, c in m5:
        if t < from_ts or t > to_ts: continue
        d = trading_day(t)
        if d is None: continue
        m5_by_day.setdefault(d, []).append((t, o, h, l, c))

    trades = []
    for d in sorted(days):
        rh, rl, first_ts = days[d]
        bars = m5_by_day.get(d, [])
        if not bars: continue
        # 只处理 16:45 之后的 5分钟K线 (第一根15分钟K线收盘后才找 FVG)
        bars = [b for b in bars if b[0] > first_ts]
        done = False
        for i in range(2, len(bars)):
            if done: break
            k1, k2, k3 = bars[i-2], bars[i-1], bars[i]
            t3 = k3[0]
            # 12:00 ET 截止 = 19:00 服务器
            hm3 = (datetime.datetime.utcfromtimestamp(t3) + datetime.timedelta(hours=3)).hour * 60 + (datetime.datetime.utcfromtimestamp(t3) + datetime.timedelta(hours=3)).minute
            if hm3 > DEADLINE_H * 60 + DEADLINE_M: break
            # ---- 做多 FVG: K3收盘 > 区间高 ----
            if k3[4] > rh:
                gap_low, gap_high = k1[2], k3[3]   # K1高点 ~ K3低点
                if gap_low < gap_high and k2[4] - k2[3] > 0:  # 缺口存在
                    # 强势实体: K2 实体 >= 1.5x 前5根平均实体, 且 >= 0.05% 价格
                    avg_body = sum(abs(bars[j][4] - bars[j][3]) for j in range(max(0, i-7), i-2)) / max(1, min(5, i-2))
                    body2 = abs(k2[4] - k2[3])
                    if body2 < max(avg_body * 1.5, rh * 0.0005): 
                        continue
                    # 区间触碰: 至少一根收盘在区间内
                    closes_in = [b[4] <= rh and b[4] >= rl for b in (k1, k2, k3)]
                    if not any(closes_in): continue
                    # 挂限价: 缺口下沿 (K1高点), 止损 K1低点下方
                    entry = gap_low
                    sl = k1[3] * 0.9999
                    tp = entry + 2 * (entry - sl)
                    direction = "BUY"
                else:
                    continue
            # ---- 做空 FVG: K3收盘 < 区间低 ----
            elif k3[4] < rl:
                gap_low, gap_high = k3[2], k1[3]   # K3高点 ~ K1低点
                if gap_low < gap_high and k2[4] - k2[3] > 0:
                    avg_body = sum(abs(bars[j][4] - bars[j][3]) for j in range(max(0, i-7), i-2)) / max(1, min(5, i-2))
                    body2 = abs(k2[4] - k2[3])
                    if body2 < max(avg_body * 1.5, rl * 0.0005):
                        continue
                    closes_in = [b[4] <= rh and b[4] >= rl for b in (k1, k2, k3)]
                    if not any(closes_in): continue
                    entry = gap_high
                    sl = k1[2] * 1.0001
                    tp = entry - 2 * (sl - entry)
                    direction = "SELL"
                else:
                    continue
            else:
                continue
            # ---- 模拟执行: 挂单后逐K线检查 ----
            result, exit_ts, exit_price = None, None, None
            filled = False
            for b in bars[i+1:]:
                t, o, h, l, c = b
                if not filled:
                    if direction == "BUY" and l <= entry:
                        filled = True; fill_ts = t
                    elif direction == "SELL" and h >= entry:
                        filled = True; fill_ts = t
                    if filled:
                        # 成交当根也检查 SL/TP
                        if direction == "BUY":
                            if l <= sl: result, exit_ts, exit_price = -1.0, t, sl
                            elif h >= tp: result, exit_ts, exit_price = 2.0, t, tp
                        else:
                            if h >= sl: result, exit_ts, exit_price = -1.0, t, sl
                            elif l <= tp: result, exit_ts, exit_price = 2.0, t, tp
                    continue
                # 已持仓
                if direction == "BUY":
                    if l <= sl: result, exit_ts, exit_price = -1.0, t, sl; break
                    if h >= tp: result, exit_ts, exit_price = 2.0, t, tp; break
                else:
                    if h >= sl: result, exit_ts, exit_price = -1.0, t, sl; break
                    if l <= tp: result, exit_ts, exit_price = 2.0, t, tp; break
            if not filled:
                result, exit_ts, exit_price = "NOFILL", None, None
            trades.append((d, direction, rh, rl, round(entry, 1), round(sl, 1), round(tp, 1),
                           result, dt(exit_ts).strftime("%m-%d %H:%M") if exit_ts else "-",
                           round(exit_price, 1) if exit_price else "-"))
            done = True

    # ---- 输出 ----
    wins = [t for t in trades if t[7] == 2.0]
    losses = [t for t in trades if t[7] == -1.0]
    nofill = [t for t in trades if t[7] == "NOFILL"]
    print(f"\n{'日期':<12}{'方向':<5}{'区间高':>10}{'区间低':>10}{'进场':>10}{'止损':>10}{'止盈':>10}  结果      出场       出场价")
    print("-" * 100)
    for d, dr, rh, rl, e, s, tp_, res, exts, expx in trades:
        print(f"{str(d):<12}{dr:<5}{rh:>10.1f}{rl:>10.1f}{e:>10.1f}{s:>10.1f}{tp_:>10.1f}  {str(res):<8}  {exts:<10} {expx}")
    tot = len(wins) + len(losses)
    if tot:
        rr = sum(t[7] for t in trades if isinstance(t[7], float))
        print(f"\nTOTAL: {tot} trades, {len(wins)}W/{len(losses)}L, winrate={len(wins)/tot*100:.1f}%, net={rr:+.1f}R, nofill={len(nofill)}")
        print(f"expected value = {rr/tot:+.2f}R/trade  (positive = profitable)")
    else:
        print("\n0 trades in period")

if __name__ == "__main__":
    main()
