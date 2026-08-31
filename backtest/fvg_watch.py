# -*- coding: utf-8 -*-
"""
FVG 实时监控 (单次检查): 读 US500 .hst, 输出今日状态, FVG 信号出现时弹窗+声音
用法:
  python fvg_watch.py              # 单次检查 (配合定时任务每10分钟跑)
  python fvg_watch.py --loop       # 循环模式 (每60秒, 直到19:05服务器时间)
依赖: MT4 运行 + EA(MCPBridge_Unified) 挂载 + US500 的 M15/M5 .hst
"""
import os, sys, struct, datetime, subprocess

BASE = r"C:\Program Files (x86)\Alpari MT4\history\Alpari-Demo"
SYM = "US500"
SRV_OFFSET = 3  # 服务器 = UTC+3 (已验证 2026-08-31)

def read_hst(name):
    p = os.path.join(BASE, name)
    if not os.path.exists(p): return None
    sz = os.path.getsize(p)
    n = (sz - 148) // 60
    bars = []
    with open(p, 'rb') as f:
        f.seek(148)
        for _ in range(n):
            rec = f.read(60)
            if len(rec) < 60: break
            t, o, h, l, c = struct.unpack('<qdddd', rec[:40])
            bars.append((t, o, h, l, c))
    bars.sort()
    return bars

def srv(ts):
    """时间戳 -> 服务器钟面时间 (datetime)"""
    return datetime.datetime.utcfromtimestamp(ts + SRV_OFFSET * 3600)

def trading_day(ts):
    d = srv(ts)
    if d.weekday() >= 5: return None
    return d.date()

def notify(title, msg, beeps=4):
    subprocess.Popen([sys.executable, os.path.join(os.path.dirname(__file__), "notify_popup.py"), title, msg, str(beeps)])

def check_once():
    m15 = read_hst(f"{SYM}15.hst")
    m5 = read_hst(f"{SYM}5.hst")
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=SRV_OFFSET)
    today = now.date()
    if now.weekday() >= 5:
        print(f"[{now:%H:%M}] 周末, 无交易"); return
    if m15 is None or m5 is None:
        print("!! 数据缺失: 确认 MT4 运行 + EA 挂载 + US500 图表打开过")
        return

    # 1. 今天的开盘区间 (16:30-16:45 服务器时间 M15 K线)
    rng = None
    for t, o, h, l, c in m15:
        if trading_day(t) == today and srv(t).strftime("%H:%M") == "16:30":
            rng = (h, l); break

    # 2. 16:45 之后的 M5, 找 FVG
    fvg = None
    after = [b for b in m5 if trading_day(b[0]) == today and srv(b[0]) >= datetime.datetime(today.year, today.month, today.day, 16, 45)]
    if rng and len(after) >= 3:
        rh, rl = rng
        for i in range(2, len(after)):
            k1, k2, k3 = after[i-2], after[i-1], after[i]
            if srv(k3[0]).hour >= 19: break
            if k3[4] > rh:
                gap_low, gap_high = k1[2], k3[3]
                if gap_low < gap_high:
                    closes_in = [b[4] <= rh and b[4] >= rl for b in (k1, k2, k3)]
                    if any(closes_in):
                        fvg = ("BUY", k1[2], k1[3], k3[0]); break
            elif k3[4] < rl:
                gap_low, gap_high = k3[2], k1[3]
                if gap_low < gap_high:
                    closes_in = [b[4] <= rh and b[4] >= rl for b in (k1, k2, k3)]
                    if any(closes_in):
                        fvg = ("SELL", k1[3], k1[2], k3[0]); break

    # 3. 输出状态 + 弹窗
    if not rng:
        print(f"[{now:%H:%M}] 等待开盘区间 (16:30 后) - 当前 US500 无区间数据")
    elif fvg is None:
        last = after[-1] if after else None
        px = last[4] if last else 0
        print(f"[{now:%H:%M}] 区间高={rh:.1f} 低={rl:.1f} | 现价~{px:.1f} | 等待 FVG 突破信号")
    else:
        d, entry, sl, ts = fvg
        msg = (f"FVG 信号! US500 {d}\n进场限价: {entry:.1f}\n止损: {sl:.1f}\n"
               f"止盈(2:1): {entry + 2*abs(entry-sl):.1f}\n"
               f"信号时间: {srv(ts):%H:%M}\n请核对后手动下单(模拟盘)")
        print(f"[{now:%H:%M}] *** FVG {d} 信号! entry={entry:.1f} sl={sl:.1f} ***")
        notify("FVG 信号 - US500", msg, 6)

if __name__ == "__main__":
    if "--loop" in sys.argv:
        end = datetime.datetime.now() + datetime.timedelta(hours=1)
        while datetime.datetime.now() < end:
            try: check_once()
            except Exception as e: print("ERR:", e)
            time.sleep(60)
    else:
        check_once()
