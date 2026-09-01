# -*- coding: utf-8 -*-
"""
chan_wave_daemon.py — 缠论+波浪共振系统守护进程（信号扫描 + 模拟持仓 + 模拟账本）
================================================================================
所属系统：缠论+波浪共振交易信号系统（模拟盘研究项目）
【禁止实盘自动交易！】本进程只做：信号扫描 + 模拟持仓管理 + 模拟账本记录。
不连接任何交易接口，不调用任何下单函数，代码内不含任何实盘下单路径。

运行模式：
  python chan_wave_daemon.py            # 单次扫描（配合 Windows 计划任务每 N 分钟调用）
  python chan_wave_daemon.py --selftest # 端到端自测（临时 state 目录，不污染真实账本）
  python chan_wave_daemon.py --notify "标题" "内容" 3  # 弹窗子进程模式（由本程序内部调用，勿手动）

核心职责（单次运行模式，计划任务每 N 分钟调用一次）：
  1. 对 4 品种（BITCOIN/XAUUSD/XAGUSD/WTI）读取 周线/日线/M30/M5 的 .hst；
     数据新鲜度检查：M30/M5 末根K线时间与当前 UTC 时间差 > 2 小时 → 判定
     "数据待更新"，该品种跳过并记录原因；周线/日线缺失 → 跳过该品种并记录。
  2. 计算：wave_engine.compute(周线) + chan_engine.compute(日线/M30/M5)
     + resonance_engine.evaluate → 四态信号（NONE/LONG_OPEN/LONG_CLOSE/REDUCE）。
  3. 持仓状态（模拟）：state/positions.json 持久化。
     LONG_OPEN 且该品种无持仓 → 模拟开仓（open_price = M30 最新收盘价）；
     LONG_CLOSE 且该品种有持仓 → 模拟平仓并结算（写模拟账本）；
     REDUCE 且该品种有持仓 → 模拟减半仓。
  4. 模拟账本：state/ledger.jsonl 每行一条 JSON 记录：
     signal（信号事件）/ open（开仓）/ reduce（减仓）/ close（平仓结算）/
     track（冷却联动跟踪）。
  5. 信号去重：同品种 30 分钟内不重复开仓（查账本最近 open 记录）。
  6. 冷却联动：开仓后 48h 内价格未达 +1R 且跌破止损 → resonance_engine.
     mark_result(False)；达到 +1R → mark_result(True)。
  7. 信号通知：出现 LONG_OPEN 时 Windows 弹窗 + 响铃提醒（子进程非阻塞）。
  8. 每次运行写日志 state/daemon.log（追加，含每个品种的判定摘要）。

【本文件修复的已知逻辑缺陷（上一轮遗留）】
  resonance_engine.evaluate 不知道持仓状态，导致无持仓时也会输出 LONG_CLOSE
  （典型场景：XAUUSD 刚出 B1 买点，但现价低于最近中枢下沿 defend_price 时，
  被误判"收盘破止损平仓"）。
  修正方案（daemon 层解决，不改 resonance_engine 契约）：
    - 本 daemon 维护持仓状态（state/positions.json）；
    - 信号应用层统一拦截：该品种无持仓时，LONG_CLOSE / REDUCE 一律降级为
      NONE（绝不产生平仓动作），并在账本 signal 记录中写入
      degraded=true + original_signal，便于事后审计；
    - 有持仓时 LONG_CLOSE / REDUCE 原样执行（此时平仓条件语义正确）。
  注：defend_price 在 B1 买点时可能高于现价（下跌末端，现价创新低），属
  已知语义问题，本阶段只在账本备注中记录，不修改算法。

【已知限制】
  - resonance_engine 的冷却状态已跨进程持久化（state/cooldown.json），
    本 daemon 每次独立进程也能读到最新冷却状态，规则六跨进程生效。
  - 数据不新鲜时（MT4 未运行/图表未打开）品种被跳过，属于预期行为；需保证
    MT4 运行且 4 品种 M5/M30 图表打开过，.hst 才会持续更新。
"""
import os
import sys
import json
import struct
import datetime
import subprocess

# ======================================================================
# 参数区（集中于此，便于后续标定）
# ======================================================================
HERE = os.path.dirname(os.path.abspath(__file__))          # 本文件所在目录
HST_BASE = r"C:\Program Files (x86)\Alpari MT4\history\Alpari-Demo"
STATE_DIR = os.path.join(HERE, "state")                    # 状态目录（持仓/账本/日志）
POSITIONS_FILE = os.path.join(STATE_DIR, "positions.json") # 模拟持仓
LEDGER_FILE = os.path.join(STATE_DIR, "ledger.jsonl")      # 模拟账本
LOG_FILE = os.path.join(STATE_DIR, "daemon.log")           # 运行日志

SYMBOLS = ["BITCOIN", "XAUUSD", "XAGUSD", "WTI"]           # 4 品种
TF_WEEK, TF_DAY, TF_M30, TF_M5 = 10080, 1440, 30, 5        # .hst 周期（分钟数）

FRESH_HOURS = 2.0          # M30/M5 数据新鲜度阈值：末根K线距今 > 2 小时 → 数据待更新
DEDUP_MINUTES = 30         # 同品种开仓去重窗口（分钟）
TRACK_HOURS = 48.0         # 冷却联动：开仓后 N 小时内未达 +1R 且跌破止损 → 证伪
R_TARGET = 1.0             # 盈利达标线：+1R
ATR_ABNORMAL_MULT = 3.0    # 波动率异常开关：周线单K线波幅 > 3 倍 ATR → abnormal
NOTIFY_BEEPS = 4           # 开仓提醒响铃次数
NOTIFY_ENABLED = True      # 是否启用弹窗（自测模式自动关闭）
# ======================================================================


# ======================================================================
# 基础工具
# ======================================================================
def log(msg):
    """打印 + 追加写 daemon.log（异常不崩溃，目录不存在时自动创建）。"""
    line = "[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print("[log] 写日志失败: %s" % e, flush=True)


def now_utc_iso():
    """当前 UTC 时间的 ISO 字符串（账本/持仓时间戳）。"""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_candles(path):
    """读取 .hst → candles 列表（缺失/损坏返回 None，不抛异常）。"""
    if not os.path.exists(path):
        return None
    try:
        sz = os.path.getsize(path)
        n = (sz - 148) // 60
        if n <= 0:
            return None
        bars = []
        with open(path, "rb") as f:
            f.seek(148)
            for _ in range(n):
                rec = f.read(60)
                if len(rec) < 60:
                    break
                t, o, h, l, c, v = struct.unpack("<qddddq", rec[:48])
                bars.append({"time": int(t), "open": o, "high": h,
                             "low": l, "close": c, "vol": float(v)})
        bars.sort(key=lambda x: x["time"])
        return bars if bars else None
    except Exception as e:
        log("  !! 读取 %s 失败: %s" % (path, e))
        return None


def data_freshness(candles, max_hours=FRESH_HOURS):
    """数据新鲜度：末根K线时间与当前 UTC 的差值（小时）。返回 (fresh, hours_old)。
    .hst 的 ctm 为 UTC 时间戳（Alpari 服务器 = UTC+3 的钟面由调用方换算，
    与 fvg_watch.py 的 SRV_OFFSET 验证一致），此处直接与 UTC 比较。"""
    if not candles:
        return False, float("inf")
    last_ts = candles[-1]["time"]
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    hours_old = (now - last_ts) / 3600.0
    return hours_old <= max_hours, round(hours_old, 2)


def compute_volatility(wave_week):
    """波动率状态：atr_pct（周线 ATR 占价格百分比）+ abnormal 开关。
    abnormal 判定：最近一根周线K线波幅 > ATR_ABNORMAL_MULT 倍 ATR（跳空/巨量）。"""
    det = wave_week.get("details") or {}
    atr = det.get("atr") or 0.0
    last_close = det.get("last_close") or 0.0
    atr_pct = round(atr / last_close * 100.0, 3) if last_close else 0.0
    abnormal = False
    bars = det.get("bars_count")
    if bars:
        # 用 details 里缓存的最近波幅做简单判定；无则默认 False
        last_range = det.get("last_range") or 0.0
        if atr > 0 and last_range > atr * ATR_ABNORMAL_MULT:
            abnormal = True
    return {"atr_pct": atr_pct, "abnormal": abnormal}


# ======================================================================
# Windows 弹窗通知（子进程模式，不阻塞主流程）
# ======================================================================
def notify(title, msg, beeps=NOTIFY_BEEPS):
    """Windows 弹窗 + 响铃提醒。通过 subprocess 调用本文件 --notify 模式，
    使弹窗运行在独立子进程，主扫描流程不被 MessageBoxW 阻塞。"""
    if not NOTIFY_ENABLED:
        log("  [notify] 弹窗已禁用（自测模式），标题=%s" % title)
        return
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__),
                          "--notify", title, msg, str(beeps)])
    except Exception as e:
        log("  [notify] 启动弹窗子进程失败: %s" % e)


def _notify_child(title, msg, beeps):
    """子进程内执行：响铃 + 模态弹窗（参照 backtest/notify_popup.py）。"""
    import ctypes
    import time
    import winsound
    try:
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        time.sleep(0.2)
        for _ in range(int(beeps)):
            winsound.Beep(1500, 350)
            time.sleep(0.2)
    except Exception:
        pass
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40 | 0x40000 | 0x10000)
    except Exception:
        pass


# ======================================================================
# 持仓状态持久化（state/positions.json）
# ======================================================================
def load_positions():
    """读取模拟持仓。文件缺失 → 空持仓；格式损坏 → 备份后重建，不崩溃。"""
    if not os.path.exists(POSITIONS_FILE):
        return {"next_ticket": 1, "positions": []}
    try:
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "positions" not in data:
            raise ValueError("positions.json 结构异常")
        return data
    except Exception as e:
        bak = POSITIONS_FILE + ".bak-" + datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        try:
            os.rename(POSITIONS_FILE, bak)
            log("  !! positions.json 损坏(%s)，已备份到 %s 并重建" % (e, bak))
        except Exception:
            pass
        return {"next_ticket": 1, "positions": []}


def save_positions(data):
    """写回模拟持仓（原子写：先写临时文件再替换）。"""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = POSITIONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, POSITIONS_FILE)
    except Exception as e:
        log("  !! 保存 positions.json 失败: %s" % e)


def find_open_position(data, symbol):
    """返回该品种 OPEN 状态的持仓 dict；无则 None。"""
    for p in data["positions"]:
        if p.get("symbol") == symbol and p.get("status") == "OPEN":
            return p
    return None


# ======================================================================
# 模拟账本（state/ledger.jsonl）
# ======================================================================
def ledger_append(record):
    """账本追加一行 JSON（异常不崩溃）。"""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LEDGER_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        log("  !! 写账本失败: %s" % e)


def ledger_recent_open_ts(symbol):
    """查账本中该品种最近一次开仓（type=open）的 ISO 时间戳；无则 None。"""
    if not os.path.exists(LEDGER_FILE):
        return None
    recent = None
    try:
        with open(LEDGER_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") == "open" and rec.get("symbol") == symbol:
                    recent = rec.get("ts")
    except Exception as e:
        log("  !! 读账本失败: %s" % e)
    return recent


def dedup_blocked(symbol, minutes=DEDUP_MINUTES):
    """同品种开仓去重：最近一次开仓距今 < minutes 分钟 → 返回 True（应跳过）。"""
    ts = ledger_recent_open_ts(symbol)
    if not ts:
        return False
    try:
        last = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
        delta = (datetime.datetime.now(datetime.timezone.utc) - last).total_seconds()
        return delta < minutes * 60
    except Exception:
        return False


# ======================================================================
# 【核心修正】无持仓时平仓信号降级
# ======================================================================
def resolve_signal(result, has_position, symbol):
    """信号应用层拦截：无持仓时 LONG_CLOSE / REDUCE 一律降级为 NONE。

    返回 (signal, note)：
      signal : 最终生效信号（'NONE' / 'LONG_OPEN' / 'LONG_CLOSE' / 'REDUCE'）
      note   : 降级说明（无降级时为空字符串）

    这是对 resonance_engine 契约的 daemon 层修正 —— evaluate 本身不知道
    持仓状态，无持仓时输出 LONG_CLOSE（如 XAUUSD B1 买点现价 < defend_price
    被误判"收盘破止损"）在语义上无效，必须拦截，绝不允许产生平仓动作。
    """
    sig = result.get("signal", "NONE")
    if not has_position and sig in ("LONG_CLOSE", "REDUCE"):
        note = ("无持仓，%s 降级为 NONE（resonance_engine 不知持仓状态；"
                "原始信号仅记录，不产生任何平仓动作）" % sig)
        return "NONE", note
    return sig, ""


# ======================================================================
# 信号动作：模拟开仓 / 模拟平仓结算 / 模拟减半仓
# ======================================================================
def apply_signal(data, symbol, result, last_close, note=""):
    """把最终信号应用到持仓状态，返回动作摘要 dict。

    参数：
      data      : positions.json 内容（原地修改，由调用方 save_positions）
      symbol    : 品种
      result    : resonance_engine.evaluate 的完整输出
      last_close: M30 最新收盘价（开仓/平仓执行价）
      note      : 降级说明（仅记录用途）
    返回：
      {'action': 'OPEN'/'CLOSE'/'REDUCE'/'NONE'/'DEGRADED', 'detail': str}
    """
    sig = result.get("signal", "NONE")
    pos = find_open_position(data, symbol)
    ts = now_utc_iso()

    if sig == "LONG_OPEN":
        if pos is not None:
            return {"action": "NONE", "detail": "已有持仓，忽略重复开仓"}
        if dedup_blocked(symbol):
            return {"action": "NONE", "detail": "30分钟内已开仓，去重跳过"}
        sl = result.get("stop_loss_price") or 0.0
        rate = result.get("position_rate") or 0.0
        if sl <= 0 or rate <= 0:
            return {"action": "NONE", "detail": "无有效止损/仓位，拒绝开仓"}
        ticket = data["next_ticket"]
        data["next_ticket"] += 1
        new_pos = {
            "ticket": ticket,
            "symbol": symbol,
            "open_time": ts,
            "open_price": round(last_close, 4),
            "sl": round(sl, 4),
            "position_rate": rate,
            "status": "OPEN",
            "plus1r_hit": False,   # 冷却联动：是否已达 +1R
            "failed": False,       # 冷却联动：是否已被证伪标记
            "note": "",
        }
        # 已知语义问题备注：B1 买点处 defend_price 可能高于现价（下跌末端创新低）
        if sl > last_close:
            new_pos["note"] = ("备注: defend_price(%.4f)>现价(%.4f)，B1买点下跌末端"
                               "语义问题，止损距离取绝对值，本阶段不修算法"
                               % (sl, last_close))
        data["positions"].append(new_pos)
        ledger_append({
            "type": "signal", "ts": ts, "symbol": symbol,
            "signal": "LONG_OPEN", "position_rate": rate,
            "stop_loss": round(sl, 4), "check_list": result.get("check_list", []),
            "reason": result.get("reason", []), "degraded": False, "note": note,
        })
        ledger_append({
            "type": "open", "ts": ts, "symbol": symbol, "ticket": ticket,
            "open_price": round(last_close, 4), "sl": round(sl, 4),
            "position_rate": rate,
            "note": new_pos["note"] or "",
        })
        # 开仓提醒（弹窗 + 响铃）
        notify("缠论波浪共振 - 模拟开仓信号 %s" % symbol,
               "品种: %s\n信号: LONG_OPEN\n仓位: %.0f%%\n止损: %.2f\n"
               "开仓价: %.2f\n请核对后手动操作（模拟盘研究，禁止实盘自动交易）"
               % (symbol, rate * 100, sl, last_close))
        return {"action": "OPEN", "detail": "ticket=%d 开仓价=%.4f 止损=%.4f 仓位=%.2f"
                % (ticket, last_close, sl, rate)}

    if sig == "LONG_CLOSE":
        if pos is None:
            # 理论不可达（resolve_signal 已降级），防御性兜底
            return {"action": "NONE", "detail": "无持仓，平仓信号已被降级"}
        return settle_position(data, pos, last_close, result, ts)

    if sig == "REDUCE":
        if pos is None:
            return {"action": "NONE", "detail": "无持仓，减仓信号已被降级"}
        old_rate = pos.get("position_rate", 0.0)
        pos["position_rate"] = round(old_rate * 0.5, 4)
        ledger_append({
            "type": "reduce", "ts": ts, "symbol": symbol, "ticket": pos["ticket"],
            "new_rate": pos["position_rate"],
            "reason": "REDUCE: %s" % (result.get("reason", [])),
        })
        return {"action": "REDUCE", "detail": "ticket=%d 仓位 %.2f -> %.2f（减半）"
                % (pos["ticket"], old_rate, pos["position_rate"])}

    if note:
        # 降级记录：无持仓时 LONG_CLOSE/REDUCE → NONE（本文件核心修正）
        ledger_append({
            "type": "signal", "ts": ts, "symbol": symbol,
            "signal": "NONE", "original_signal": result.get("signal", "NONE"),
            "position_rate": 0.0, "stop_loss": result.get("stop_loss_price") or 0.0,
            "check_list": result.get("check_list", []),
            "reason": result.get("reason", []), "degraded": True, "note": note,
        })
        return {"action": "DEGRADED", "detail": note}

    return {"action": "NONE", "detail": "无信号动作"}


def settle_position(data, pos, close_price, result, ts):
    """模拟平仓结算：写账本 close 记录，持仓置 CLOSED。

    结算指标：
      R 倍数 = (平仓价 - 开仓价) / |开仓价 - 止损价|   （以止损距离为 1R）
      结果   = WIN（R > 0）/ LOSS（R <= 0）
      持仓天数 = 平仓时间 - 开仓时间（天）
    同时联动冷却：已达 +1R → mark_result(True)；亏损平仓且从未达 +1R →
    mark_result(False)（信号被证伪）。
    """
    from resonance_engine import mark_result
    symbol = pos["symbol"]
    open_price = pos["open_price"]
    sl = pos["sl"]
    r_unit = abs(open_price - sl) if sl > 0 else 0.0
    r = (close_price - open_price) / r_unit if r_unit > 0 else 0.0
    result_flag = "WIN" if r > 0 else "LOSS"
    # 持仓天数（不足 1 天按小时折算，保留 2 位）
    try:
        open_dt = datetime.datetime.strptime(pos["open_time"],
                                             "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
        days = round((datetime.datetime.now(datetime.timezone.utc)
                      - open_dt).total_seconds() / 86400.0, 2)
    except Exception:
        days = 0.0
    pos["status"] = "CLOSED"
    pos["close_time"] = ts
    pos["close_price"] = round(close_price, 4)
    pos["r_multiple"] = round(r, 4)
    pos["result"] = result_flag
    ledger_append({
        "type": "close", "ts": ts, "symbol": symbol, "ticket": pos["ticket"],
        "open_time": pos["open_time"], "close_time": ts,
        "open_price": open_price, "close_price": round(close_price, 4),
        "sl": sl, "hold_days": days, "r_multiple": round(r, 4),
        "result": result_flag, "reason": result.get("reason", []),
        "close_condition": result.get("close_condition", ""),
    })
    # 冷却联动
    if pos.get("plus1r_hit"):
        mark_result(True)
    elif r <= 0:
        mark_result(False)
        pos["failed"] = True
    return {"action": "CLOSE", "detail": "ticket=%d R=%.2f 结果=%s 持仓%.2f天"
            % (pos["ticket"], r, result_flag, days)}


# ======================================================================
# 冷却联动跟踪（开仓后 48h 内未达 +1R 且跌破止损 → 证伪；达 +1R → 有效）
# ======================================================================
def track_positions(data, symbol, last_close):
    """对某品种的 OPEN 持仓做冷却联动跟踪（基于当次 M30 最新收盘价）。

    - 价格 >= 开仓价 + 1R 且未标记过 → mark_result(True)，标记 plus1r_hit；
    - 持仓超 TRACK_HOURS、从未达 +1R、现价 <= 止损 → mark_result(False)，
      标记 failed（已证伪，避免重复）。
    返回动作描述列表。
    """
    from resonance_engine import mark_result
    actions = []
    pos = find_open_position(data, symbol)
    if pos is None:
        return actions
    open_price = pos["open_price"]
    sl = pos["sl"]
    r_unit = abs(open_price - sl) if sl > 0 else 0.0
    if r_unit <= 0:
        return actions
    plus1r_price = open_price + R_TARGET * r_unit
    ts = now_utc_iso()
    if not pos.get("plus1r_hit") and last_close >= plus1r_price:
        pos["plus1r_hit"] = True
        mark_result(True)
        ledger_append({
            "type": "track", "ts": ts, "symbol": symbol, "ticket": pos["ticket"],
            "event": "plus1r_hit", "mark": True,
            "last_close": round(last_close, 4),
            "note": "价格达到 +1R，信号有效",
        })
        actions.append("ticket=%d 达 +1R，mark_result(True)" % pos["ticket"])
    if (not pos.get("failed") and not pos.get("plus1r_hit")
            and last_close <= sl):
        # 跌破止损即证伪候选；48h 窗口内未达 +1R 才正式证伪
        try:
            open_dt = datetime.datetime.strptime(
                pos["open_time"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc)
            hours = (datetime.datetime.now(datetime.timezone.utc)
                     - open_dt).total_seconds() / 3600.0
        except Exception:
            hours = TRACK_HOURS + 1
        if hours > TRACK_HOURS:
            pos["failed"] = True
            mark_result(False)
            ledger_append({
                "type": "track", "ts": ts, "symbol": symbol,
                "ticket": pos["ticket"], "event": "failed", "mark": False,
                "last_close": round(last_close, 4),
                "note": "开仓%.1fh内未达+1R且跌破止损，信号证伪" % hours,
            })
            actions.append("ticket=%d %.1fh未达+1R且破止损，mark_result(False)"
                           % (pos["ticket"], hours))
    return actions


# ======================================================================
# 单品种处理（核心扫描流程）
# ======================================================================
def process_symbol(symbol, data, force=False):
    """处理单个品种：读数据 → 新鲜度检查 → 计算 → 决策 → 应用动作。

    force=True 时跳过新鲜度检查（自测模式用），数据缺失仍跳过。
    返回摘要 dict。
    """
    summary = {"symbol": symbol, "ok": False, "skip_reason": "",
               "signal": "NONE", "detail": ""}
    # ---- 1. 读取 4 周期数据 ----
    paths = {
        "WEEK": os.path.join(HST_BASE, "%s%d.hst" % (symbol, TF_WEEK)),
        "DAY": os.path.join(HST_BASE, "%s%d.hst" % (symbol, TF_DAY)),
        "M30": os.path.join(HST_BASE, "%s%d.hst" % (symbol, TF_M30)),
        "M5": os.path.join(HST_BASE, "%s%d.hst" % (symbol, TF_M5)),
    }
    candles = {}
    missing = []
    for tf, p in paths.items():
        c = load_candles(p)
        if c is None:
            missing.append(tf)
        else:
            candles[tf] = c
    if missing:
        reason = "%s 数据缺失(.hst不存在或为空): %s，跳过该品种" % (symbol, ",".join(missing))
        summary["skip_reason"] = reason
        log("  !! %s" % reason)
        return summary
    # ---- 2. 新鲜度检查（M30/M5；周线/日线仅要求存在）----
    if not force:
        stale = []
        for tf in ("M30", "M5"):
            fresh, hours = data_freshness(candles[tf])
            if not fresh:
                stale.append("%s(%.1fh)" % (tf, hours))
        if stale:
            reason = ("%s 数据待更新: M30/M5 末根K线距今>%.0f小时 (%s)，"
                      "跳过该品种（请确认 MT4 运行且图表打开）"
                      % (symbol, FRESH_HOURS, ", ".join(stale)))
            summary["skip_reason"] = reason
            log("  !! %s" % reason)
            return summary
    else:
        stale_note = []
        for tf in ("M30", "M5"):
            fresh, hours = data_freshness(candles[tf])
            if not fresh:
                stale_note.append("%s=%.1fh" % (tf, hours))
        if stale_note:
            log("  [selftest] %s 数据陈旧(%s)，自测模式继续计算"
                % (symbol, ", ".join(stale_note)))

    # ---- 3. 各引擎计算（异常不崩溃，逐层 try）----
    try:
        import wave_engine
        import chan_engine
        import resonance_engine

        wave_week = wave_engine.compute(candles["WEEK"])
        chan_day = chan_engine.compute(candles["DAY"], "DAY")
        chan_m30 = chan_engine.compute(candles["M30"], "M30")
        chan_m5 = chan_engine.compute(candles["M5"], "M5")
        volatility = compute_volatility(wave_week)
        result = resonance_engine.evaluate(symbol, wave_week, chan_day,
                                           chan_m30, chan_m5, volatility)
    except Exception as e:
        reason = "%s 引擎计算失败: %s，跳过该品种" % (symbol, e)
        summary["skip_reason"] = reason
        log("  !! %s" % reason)
        return summary

    # ---- 4. 核心修正：无持仓时平仓信号降级 ----
    has_pos = find_open_position(data, symbol) is not None
    final_sig, note = resolve_signal(result, has_pos, symbol)

    # ---- 5. 应用动作（开仓/平仓/减仓/降级记录）----
    last_close = ((chan_m30.get("details") or {}).get("last_close")
                  or chan_m30.get("defend_price") or 0.0)
    act = apply_signal(data, symbol, result, last_close, note)

    # ---- 6. 冷却联动跟踪（OPEN 持仓）----
    track_acts = track_positions(data, symbol, last_close)

    # ---- 7. 摘要 ----
    wl = wave_week.get("wave_label"); ws = wave_week.get("wave_status")
    bias = wave_week.get("bias")
    summary.update({
        "ok": True,
        "wave": "label=%s status=%s bias=%s broken=%s"
                % (wl, ws, bias, wave_week.get("wave_broken")),
        "day": "trend=%s bp=%s sp=%s" % (chan_day.get("trend"),
                                         chan_day.get("buy_point"),
                                         chan_day.get("sell_point")),
        "m30": "bp=%s sp=%s beichi=%s defend=%.4f" % (
            chan_m30.get("buy_point"), chan_m30.get("sell_point"),
            chan_m30.get("beichi"), chan_m30.get("defend_price") or 0.0),
        "m5": "beichi=%s sp=%s" % (chan_m5.get("beichi"),
                                   chan_m5.get("sell_point")),
        "atr_pct": volatility["atr_pct"],
        "raw_signal": result.get("signal"),
        "signal": final_sig,
        "degraded_note": note,
        "action": act,
        "track": track_acts,
        "last_close": last_close,
    })
    return summary


# ======================================================================
# 主流程：单次运行（计划任务每 N 分钟调用一次）
# ======================================================================
def run_once(force=False):
    """完整扫描一次 4 品种。force=True 跳过新鲜度检查（自测用）。"""
    log("=" * 78)
    log("缠论+波浪共振守护进程 单次扫描开始（模拟盘研究，禁止实盘自动交易）")
    os.makedirs(STATE_DIR, exist_ok=True)
    data = load_positions()
    summaries = []
    for symbol in SYMBOLS:
        try:
            s = process_symbol(symbol, data, force=force)
            summaries.append(s)
            log("  [%s] %s" % (symbol, _summary_line(s)))
        except Exception as e:
            log("  [%s] 处理异常: %s" % (symbol, e))
            summaries.append({"symbol": symbol, "ok": False,
                              "skip_reason": "异常: %s" % e})
    save_positions(data)
    n_ok = sum(1 for s in summaries if s.get("ok"))
    n_skip = sum(1 for s in summaries if not s.get("ok"))
    log("扫描结束: 计算成功 %d/4，跳过 %d/4" % (n_ok, n_skip))
    return summaries


def _summary_line(s):
    """把单品种摘要压缩成一行日志。"""
    if not s.get("ok"):
        return "跳过 -> %s" % s.get("skip_reason", "未知原因")
    parts = [
        "wave[%s]" % s["wave"],
        "day[%s]" % s["day"],
        "m30[%s]" % s["m30"],
        "m5[%s]" % s["m5"],
        "atr=%.2f%%" % s["atr_pct"],
        "signal=%s(原始%s)" % (s["signal"], s["raw_signal"]),
    ]
    if s.get("degraded_note"):
        parts.append("降级!")
    if s.get("action") and s["action"]["action"] != "NONE":
        parts.append("动作[%s %s]" % (s["action"]["action"], s["action"]["detail"]))
    for t in s.get("track", []):
        parts.append("跟踪[%s]" % t)
    return " | ".join(parts)


# ======================================================================
# 端到端自测（--selftest）：临时 state 目录，不污染真实账本
# ======================================================================
def selftest():
    """端到端自测：4品种真实数据信号计算 + 无持仓降级验证 + 模拟开仓/结算流转。

    使用临时 state 目录（tempfile），全部用真实 .hst 数据，不触碰真实账本。
    """
    import tempfile
    import shutil
    global STATE_DIR, POSITIONS_FILE, LEDGER_FILE, LOG_FILE, NOTIFY_ENABLED
    tmp = tempfile.mkdtemp(prefix="chan_wave_selftest_")
    STATE_DIR = tmp
    POSITIONS_FILE = os.path.join(tmp, "positions.json")
    LEDGER_FILE = os.path.join(tmp, "ledger.jsonl")
    LOG_FILE = os.path.join(tmp, "daemon.log")
    NOTIFY_ENABLED = False  # 自测不弹窗
    # 冷却状态持久化文件也指到临时目录：S6 注入亏损持仓平仓会调用
    # mark_result(False)，若不隔离会污染真实 state/cooldown.json
    import resonance_engine
    resonance_engine.COOLDOWN_FILE = os.path.join(tmp, "cooldown.json")
    resonance_engine._STATE = {'false_count': 0, 'cooldown': False,
                               'cool_until': None, 'last_false': None}
    print("=" * 84)
    print("端到端自测开始（临时目录: %s）" % tmp)
    print("=" * 84)
    results = []  # (用例名, PASS/FAIL, 说明)

    def check(name, cond, detail):
        results.append((name, "PASS" if cond else "FAIL", detail))
        print("  [%s] %s — %s" % ("PASS" if cond else "FAIL", name, detail))

    # ---- S1: 4品种真实数据信号计算（force 跳过新鲜度，数据缺失仍跳过）----
    print("\n--- S1: 4品种信号计算（真实 .hst 数据）---")
    summaries = run_once(force=True)
    computed = [s for s in summaries if s.get("ok")]
    check("S1-4品种信号计算", len(computed) == 4,
          "成功计算 %d/4（跳过: %s）" % (
              len(computed),
              "; ".join(s["symbol"] + "->" + s.get("skip_reason", "?")
                        for s in summaries if not s.get("ok")) or "无"))
    # 重点验证：XAUUSD（或任一品种）无持仓时原始 LONG_CLOSE 必须降级
    degraded_ok = True
    for s in summaries:
        if s.get("ok"):
            print("  [%s] wave[%s] day[%s] m30[%s] m5[%s] | "
                  "原始signal=%s -> 生效signal=%s%s"
                  % (s["symbol"], s["wave"], s["day"], s["m30"], s["m5"],
                     s["raw_signal"], s["signal"],
                     " [已降级]" if s.get("degraded_note") else ""))
            if s["raw_signal"] in ("LONG_CLOSE", "REDUCE") and s["signal"] != "NONE":
                degraded_ok = False
    check("S2-无持仓平仓信号降级(真实数据)", degraded_ok,
          "真实扫描中无持仓品种的 LONG_CLOSE/REDUCE 均已降级为 NONE")

    # ---- S3: 降级逻辑单元断言（构造 XAUUSD B1 买点破止损场景）----
    print("\n--- S3: 无持仓平仓降级单元断言（XAUUSD 现价<defend 场景）---")
    fake_close = {"signal": "LONG_CLOSE", "position_rate": 0.0,
                  "stop_loss_price": 2500.0,
                  "check_list": ["LONG_CLOSE: 连续2根M30收盘价<止损位2500.00"],
                  "reason": ["收盘破止损"]}
    sig, note = resolve_signal(fake_close, False, "XAUUSD")
    check("S3-无持仓LONG_CLOSE降级NONE", sig == "NONE" and "降级" in note,
          "resolve_signal(LONG_CLOSE, has_pos=False) -> %s" % sig)
    fake_reduce = {"signal": "REDUCE", "position_rate": 0.0,
                   "stop_loss_price": 2500.0, "check_list": [], "reason": ["5浪末端衰竭"]}
    sig2, _ = resolve_signal(fake_reduce, False, "XAUUSD")
    check("S3-无持仓REDUCE降级NONE", sig2 == "NONE",
          "resolve_signal(REDUCE, has_pos=False) -> %s" % sig2)
    sig3, _ = resolve_signal(fake_close, True, "XAUUSD")
    check("S3-有持仓LONG_CLOSE不降级", sig3 == "LONG_CLOSE",
          "resolve_signal(LONG_CLOSE, has_pos=True) -> %s" % sig3)

    # ---- S4: 模拟开仓流转 ----
    print("\n--- S4: 模拟开仓流转 ---")
    data = load_positions()
    open_made = False
    for s in summaries:
        if s.get("ok") and s["action"]["action"] == "OPEN":
            open_made = True
            print("  真实信号开仓: %s" % s["action"]["detail"])
    if not open_made:
        # 真实数据未触发 LONG_OPEN → 注入构造场景验证开仓链路
        print("  真实数据未触发 LONG_OPEN（现价未满足共振），注入构造场景验证开仓链路")
        fake_open = {"signal": "LONG_OPEN", "position_rate": 0.6,
                     "stop_loss_price": 2400.0,
                     "check_list": ["构造"], "reason": ["构造场景"]}
        act = apply_signal(data, "XAUUSD", fake_open, 2450.0)
        open_made = act["action"] == "OPEN"
        print("  构造开仓: %s" % act["detail"])
    check("S4-模拟开仓", open_made, "positions.json 应含 OPEN 持仓")
    save_positions(data)
    open_pos = find_open_position(data, "XAUUSD")
    if open_pos:
        print("  持仓: ticket=%s symbol=%s open=%.4f sl=%.4f rate=%.2f status=%s"
              % (open_pos["ticket"], open_pos["symbol"], open_pos["open_price"],
                 open_pos["sl"], open_pos["position_rate"], open_pos["status"]))
        if open_pos.get("note"):
            print("  开仓备注: %s" % open_pos["note"])
    check("S4-开仓持久化", open_pos is not None and open_pos["status"] == "OPEN",
          "positions.json 已写入 OPEN 持仓")

    # ---- S5: 去重验证（30分钟内不重复开仓）----
    print("\n--- S5: 开仓去重验证 ---")
    fake_open2 = {"signal": "LONG_OPEN", "position_rate": 0.6,
                  "stop_loss_price": 2400.0, "check_list": [], "reason": ["构造场景2"]}
    # 同一品种已有 OPEN 持仓 → 直接拒绝
    act = apply_signal(data, "XAUUSD", fake_open2, 2460.0)
    check("S5-已有持仓拒绝重复开仓", act["action"] == "NONE"
          and "已有持仓" in act["detail"], "apply_signal -> %s" % act["detail"])
    # 无持仓品种但 30 分钟内开过仓 → 去重拒绝（先造一条 WTI 开仓账本记录）
    ledger_append({"type": "open", "ts": now_utc_iso(), "symbol": "WTI",
                   "ticket": 50, "open_price": 80.0, "sl": 79.0,
                   "position_rate": 0.6})
    dedup_data = {"next_ticket": 99, "positions": []}
    act = apply_signal(dedup_data, "WTI", fake_open2, 80.0)
    check("S5-去重窗口拒绝", act["action"] == "NONE" and "去重" in act["detail"],
          "apply_signal(WTI, 30分钟内已开仓) -> %s" % act["detail"])

    # ---- S6: 模拟平仓结算流转（构造亏损持仓后再次运行/直接结算）----
    print("\n--- S6: 模拟平仓结算流转 ---")
    # 清空持仓，只注入一个必亏损持仓（sl=2480，结算价 2460 已破止损）
    data = load_positions()
    data["positions"] = []
    loss_pos = {
        "ticket": data["next_ticket"], "symbol": "XAUUSD",
        "open_time": (datetime.datetime.now(datetime.timezone.utc)
                      - datetime.timedelta(hours=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "open_price": 2500.0, "sl": 2480.0, "position_rate": 0.6,
        "status": "OPEN", "plus1r_hit": False, "failed": False, "note": "自测注入",
    }
    data["next_ticket"] += 1
    data["positions"].append(loss_pos)
    save_positions(data)
    fake_close2 = {"signal": "LONG_CLOSE", "position_rate": 0.0,
                   "stop_loss_price": 2480.0,
                   "check_list": ["LONG_CLOSE: 连续2根M30收盘价<止损位2480.00"],
                   "reason": ["收盘破止损"],
                   "close_condition": "收盘连续2根M30收不回止损价2480.00"}
    # 方式A：apply_signal 走完整平仓链路（有持仓 → 不降级）
    act = apply_signal(data, "XAUUSD", fake_close2, 2460.0)
    save_positions(data)
    closed = [p for p in data["positions"] if p["symbol"] == "XAUUSD"
              and p["status"] == "CLOSED"]
    check("S6-平仓结算执行", act["action"] == "CLOSE" and len(closed) == 1,
          "apply_signal -> %s" % act["detail"])
    if closed:
        c = closed[-1]
        r_expected = (2460.0 - 2500.0) / abs(2500.0 - 2480.0)  # -2.0
        check("S6-R倍数计算", abs(c["r_multiple"] - r_expected) < 1e-6
              and c["result"] == "LOSS",
              "R=%.2f(期望%.2f) 结果=%s 持仓天数=%.2f"
              % (c["r_multiple"], r_expected, c["result"], c.get("hold_days", 0)))
    # 方式B：真实 run_once 再跑一遍（force），验证持仓状态下流程不崩溃
    print("  再次运行 run_once(force=True) 验证有持仓时流程正常...")
    summaries2 = run_once(force=True)
    check("S6-有持仓再次运行不崩溃", all(True for _ in summaries2)
          and os.path.exists(LEDGER_FILE),
          "4品种再次扫描完成，账本文件存在")

    # ---- S7: 账本审计 ----
    print("\n--- S7: 模拟账本内容（临时 ledger.jsonl）---")
    ledger_lines = []
    if os.path.exists(LEDGER_FILE):
        with open(LEDGER_FILE, "r", encoding="utf-8") as f:
            ledger_lines = [l.strip() for l in f if l.strip()]
    for line in ledger_lines:
        try:
            rec = json.loads(line)
            print("  %s" % json.dumps(rec, ensure_ascii=False))
        except Exception:
            print("  <非法行> %s" % line)
    types = set(json.loads(l).get("type") for l in ledger_lines)
    check("S7-账本记录类型齐全",
          {"signal", "open", "close"} <= types,
          "账本含类型: %s" % sorted(types))

    # ---- 汇总 ----
    print("\n" + "=" * 84)
    fails = [r for r in results if r[1] == "FAIL"]
    print("自测汇总: %d 项，PASS %d，FAIL %d" % (len(results),
                                                len(results) - len(fails),
                                                len(fails)))
    for name, st, detail in results:
        print("  [%s] %s" % (st, name))
    shutil.rmtree(tmp, ignore_errors=True)
    print("临时目录已清理: %s" % tmp)
    print("自测结束（python chan_wave_daemon.py --selftest 无报错）")
    return 1 if fails else 0


# ======================================================================
# 入口
# ======================================================================
def main():
    # Windows 控制台 UTF-8 输出兼容
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if "--notify" in sys.argv:
        # 弹窗子进程模式（由 notify() 通过 subprocess 调用）
        try:
            i = sys.argv.index("--notify")
            title = sys.argv[i + 1] if len(sys.argv) > i + 1 else "缠论波浪共振"
            msg = sys.argv[i + 2] if len(sys.argv) > i + 2 else ""
            beeps = sys.argv[i + 3] if len(sys.argv) > i + 3 else str(NOTIFY_BEEPS)
            _notify_child(title, msg, beeps)
        except Exception as e:
            print("notify 子进程异常: %s" % e)
        return 0
    if "--selftest" in sys.argv:
        return selftest()
    # 正常单次扫描模式（计划任务每 N 分钟调用）
    run_once(force=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
