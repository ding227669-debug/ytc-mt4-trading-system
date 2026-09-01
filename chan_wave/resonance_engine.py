# -*- coding: utf-8 -*-
"""
resonance_engine.py — 缠论+波浪共振决策器 (信号核心)
====================================================
所属系统：缠论+波浪共振交易信号系统（模拟盘研究项目，禁止自动下单，只做信号计算）

职责：
  输入 周线波浪(wave_engine.compute) + 日线/M30/M5 缠论(chan_engine.compute)
  + 波动率状态, 按用户完整规则集输出四态信号:
      NONE / LONG_OPEN / LONG_CLOSE / REDUCE
  附仓位比例、止损价、减仓/平仓条件、检查清单与原因标签。

周期层级（用户原始规则，一字不差编码）：
  战略=周线波浪 → 战术=日线缠论 → 执行=M30缠论买点 → 确认=M5背驰校验
  共振才开仓；冲突（波浪看多但缠论出卖点）→ 放弃；单一信号不交易。

函数契约：
  evaluate(symbol, wave_week, chan_day, chan_m30, chan_m5, volatility) -> dict
    wave_week : wave_engine.compute 的输出 (周线波浪)
    chan_day / chan_m30 / chan_m5 : chan_engine.compute 的输出
    volatility : {'atr_pct': float(周线ATR占价格百分比), 'abnormal': bool}
                 abnormal=True 时市场结构异常(跳空/巨量), 屏蔽一切信号

输出 JSON 契约（严格遵守）：
  {
    "signal": "NONE"|"LONG_OPEN"|"LONG_CLOSE"|"REDUCE",
    "position_rate": float,      # 仓位比例 0~0.6
    "stop_loss_price": float,    # 买点对应中枢下沿 (chan_m30['defend_price'])
    "reduce_condition": str,     # 减仓条件描述
    "close_condition": str,      # 全部平仓条件描述
    "check_list": [str],         # 各检查项通过/不通过说明
    "reason": [str],             # 信号原因标签 (LONG_OPEN 时如 ["周线波浪多头",...])
    "warning": "仅回测研究，禁止实盘自动交易",
    "timestamp": "2026-09-01T14:00:00Z",
    "symbol": symbol
  }

防主观篡改（编码进模块）：
  - 波浪标签锁定 / 已闭合K线: 由 wave_engine / chan_engine 保证 (调用方只喂已闭合K线)
  - 冷却跨进程持久化: 连续 COOLDOWN_THRESHOLD 次 LONG_OPEN 被 mark_result(False)
    证伪后进入冷却, evaluate 直接返回 NONE 并注明冷却原因; mark_result(True) 解除冷却。
    状态持久化到 state/cooldown.json（daemon 由计划任务每 30 分钟启动新进程,
    内存模块级变量无法跨进程生效, 故改为文件持久化; 文件读写失败降级为内存模式）。
    冷却为全局状态（不分品种）——按规则集原文"连续3次信号被证伪, Agent自动进入
    冷却"理解为系统级冷却; daemon 单进程顺序跑 4 品种, 无并发写冲突。

已知缺陷（写进注释，用户规则第八条）：
  - 震荡盘整时缠论频繁假背驰、波浪持续 UNCERTAIN → 输出 NONE 空仓（正确行为）
  - 突发消息跳空: 全部技术结构失效 → abnormal 开关屏蔽信号
  - AI 自动识别笔/线段/中枢/数浪存在偏差, 不能 100% 等价人工划分

禁止：本模块不含任何下单/交易功能。
"""

import os
import json
import datetime

# ==================== 决策参数 (集中于此, 便于后续标定) ====================
# --- 仓位 (规则五: 总仓位硬上限 0.6, 绝不满仓) ---
MAX_POSITION = 0.6          # 总仓位硬上限
POS_WAVE3_B1B2 = 0.6        # 周线3浪主升 + M30 B1/B2 共振 -> 0.6
POS_WAVE3_DEFAULT = 0.5     # 周线3浪阶段默认仓位 (规则区间 0.4~0.6)
POS_WAVE5 = 0.3             # 周线5浪阶段 + M30 买点 -> 0.3 (规则区间 0.2~0.3)
POS_B3 = 0.2                # M30 B3 买点共振 -> 0.2 (低优先级买点, 仓位从轻)
REDUCE_RATIO = 0.5          # 减仓比例 50%
# --- 冷却 (规则六: 连续 3 次信号被证伪 -> 冷却) ---
COOLDOWN_THRESHOLD = 3
COOLDOWN_DAYS = 7           # 冷却时长（规则集未定义时长, 默认 7 天, 可标定）
# ==========================================================================

# ==================== 模块级状态 (冷却 / 证伪计数, 跨进程持久化) ====================
# 由调用方通过 mark_result(True/False) 反馈最近一次 LONG_OPEN 是否被证伪。
# 状态持久化到 state/cooldown.json —— daemon 由 Windows 计划任务每 30 分钟
# 启动一次新进程, 纯内存变量无法跨进程生效, 故每次 evaluate/mark_result
# 都先读文件再写回。文件读写失败时降级为进程内内存模式（不崩溃）。
# 注意: 冷却为全局状态, 不分品种（规则集原文"连续3次信号被证伪, Agent自动
# 进入冷却"理解为系统级冷却; daemon 单进程顺序跑 4 品种, 无并发写冲突）。
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
COOLDOWN_FILE = os.path.join(STATE_DIR, "cooldown.json")
_STATE = {'false_count': 0, 'cooldown': False,
          'cool_until': None, 'last_false': None}


def _load_cooldown():
    """从 state/cooldown.json 读取冷却状态到内存镜像 _STATE。

    文件不存在 / 损坏 / IO 错误 → 保持内存默认（降级为内存模式, 不抛异常）。
    """
    try:
        with open(COOLDOWN_FILE, 'r', encoding='utf-8') as f:
            st = json.load(f)
        _STATE['false_count'] = int(st.get('false_count', 0))
        _STATE['cooldown'] = bool(st.get('cooldown', False))
        _STATE['cool_until'] = st.get('cool_until')
        _STATE['last_false'] = st.get('last_false')
    except Exception:
        pass  # 缺失/损坏 → 保持内存默认


def _save_cooldown():
    """把内存冷却状态写回 state/cooldown.json（临时文件+原子替换）。

    失败 → 仅保留内存状态（降级）, 不抛异常。
    """
    tmp = None
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = COOLDOWN_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_STATE, f, ensure_ascii=False, indent=2)
        os.replace(tmp, COOLDOWN_FILE)
    except Exception:
        if tmp:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass


def _cooling_active():
    """冷却是否生效。cooldown=True 且未到期 → True（evaluate 返回 NONE）。

    cool_until 为 None（旧格式/降级）→ 视为永久冷却, 保持旧行为;
    cool_until 已过期 → 自动解除冷却并写回文件。
    """
    if not _STATE['cooldown']:
        return False
    until = _STATE.get('cool_until')
    if until:
        try:
            exp = datetime.datetime.fromisoformat(until)
            if datetime.datetime.now(datetime.timezone.utc) >= exp:
                # 冷却到期, 自动解除（规则六: 冷却期结束恢复输出信号）
                _STATE['cooldown'] = False
                _STATE['false_count'] = 0
                _STATE['cool_until'] = None
                _save_cooldown()
                return False
        except Exception:
            pass  # 时间解析失败 → 按冷却处理（保守）
    return True


def mark_result(correct):
    """调用方反馈最近一次 LONG_OPEN 是否被后续行情证伪（接口不变）。

    correct=True  : 信号有效, 清零证伪计数并解除冷却
    correct=False : 信号被证伪, 计数+1; 连续 COOLDOWN_THRESHOLD 次
                    证伪后进入冷却, 冷却期间 evaluate 直接返回 NONE。
    状态持久化到 state/cooldown.json（跨进程生效）; 文件读写失败时
    降级为进程内内存模式, 不抛异常。
    """
    _load_cooldown()  # 先读最新持久化状态（跨进程）
    now = datetime.datetime.now(datetime.timezone.utc)
    if correct:
        _STATE['false_count'] = 0
        _STATE['cooldown'] = False
        _STATE['cool_until'] = None
        _STATE['last_false'] = None
    else:
        _STATE['false_count'] += 1
        _STATE['last_false'] = now.isoformat()
        if _STATE['false_count'] >= COOLDOWN_THRESHOLD:
            _STATE['cooldown'] = True
            _STATE['false_count'] = 0
            _STATE['cool_until'] = (now + datetime.timedelta(
                days=COOLDOWN_DAYS)).isoformat()
    _save_cooldown()


# ==================== 内部工具 ====================
def _close_condition_text(stop):
    """全部平仓条件描述 (规则三 LONG_CLOSE, 含具体止损价)。"""
    if stop and stop > 0:
        return ('收盘连续2根M30收不回止损价%.2f(盘中穿刺不算); 或wave_broken浪型破坏; '
                '或M30出现S2/S3卖点; 或周线BULL转BEAR —— 任一触发全部平仓' % stop)
    return ('收盘连续2根M30收不回止损价(盘中穿刺不算); 或wave_broken浪型破坏; '
            '或M30出现S2/S3卖点; 或周线BULL转BEAR —— 任一触发全部平仓')


def _closed_below_2bars(chan, defend):
    """LONG_CLOSE 条件①: 连续 2 根收盘价 < defend_price (盘中穿刺不算)。

    chan: 任一级别 chan_engine.compute 输出 (生产=M30, 日线慢速版回测=DAY)。
    按 chan_engine details.close_hist (最近3根收盘) 判断; 数据不足时
    退化为单根收盘判断。
    """
    if not defend or defend <= 0:
        return False
    d = chan.get('details') or {}
    hist = d.get('close_hist') or []
    if len(hist) >= 2:
        return hist[-1] < defend and hist[-2] < defend
    last = d.get('last_close') or 0.0
    return last < defend


def _calc_position(wave_label, buy_point):
    """仓位计算 (规则二 + 规则五)。

    规则五: 3浪主升+M30 B1/B2 共振->0.6; 5浪+M30买点->0.3; B3->0.2; 其他->0
    规则二: 3浪阶段 0.4~0.6(默认0.5); 5浪阶段 0.2~0.3(默认0.25)
    综合: B3 优先 0.2; 3浪+B1/B2 取 0.6 (满配), 3浪其他 0.5; 5浪取 0.3。
    """
    if buy_point == 'B3':
        return POS_B3
    if wave_label in ('2', '3'):            # '2'刚结束准备3浪 与 3浪主升
        if buy_point in ('B1', 'B2'):
            return POS_WAVE3_B1B2
        return POS_WAVE3_DEFAULT
    if wave_label == '5':
        return POS_WAVE5
    return 0.0


def _out(symbol, signal, pos, stop, check, reason, ts):
    """组装输出 JSON (契约字段)。"""
    return {
        'signal': signal,
        'position_rate': round(min(pos, MAX_POSITION), 2),
        'stop_loss_price': round(stop, 2) if stop and stop > 0 else 0.0,
        'reduce_condition': ('周线5浪末端量能衰竭减仓50%; 或M30出现S1顶卖点'
                             '且顶背驰确认减仓50%'),
        'close_condition': _close_condition_text(stop),
        'check_list': check,
        'reason': reason,
        'warning': '仅回测研究，禁止实盘自动交易',
        'timestamp': ts,
        'symbol': symbol,
    }


# ==================== 主入口 evaluate ====================
def evaluate(symbol, wave_week, chan_day, chan_m30, chan_m5, volatility):
    """共振决策主流程: 按完整规则集输出四态信号 (详见模块 docstring)。

    判定顺序（先平后开、风险优先）:
      0 冷却检查 -> 1 全局前置过滤 -> 2 LONG_CLOSE -> 3 REDUCE
      -> 4 开仓黑名单 -> 5 LONG_OPEN 全条件 AND -> 6 仓位 -> 输出
    """
    check = []      # check_list: 各检查项通过/不通过说明
    reason = []     # reason: 信号原因标签
    ts = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    # ---- 周期缺失降级 (日线慢速版回测: 仅周线波浪+日线缠论) ----
    # 生产配置传入真实 M30/M5 chan_result, 行为与历史完全一致;
    # chan_m30/chan_m5 传 None 表示该周期不参与 (回测降级):
    #   买点判定改用日线买点, M30/M5 背驰与卖点检查跳过, 止损位取日线防守价。
    m30_missing = chan_m30 is None
    m5_missing = chan_m5 is None
    chan_m30 = chan_m30 or {}
    chan_m5 = chan_m5 or {}
    chan_day = chan_day or {}
    wl = wave_week.get('wave_label')
    ws = wave_week.get('wave_status')
    bias = wave_week.get('bias')
    broken = wave_week.get('wave_broken', False)
    defend = chan_m30.get('defend_price') or chan_day.get('defend_price') or 0.0

    # ---------- 0. 冷却状态 (规则六, 跨进程持久化) ----------
    _load_cooldown()  # 读取 state/cooldown.json 最新状态（跨进程生效）
    if _cooling_active():
        check.append('冷却中: 连续%d次LONG_OPEN被证伪, 暂停输出新信号'
                     ' (冷却至 %s)' % (COOLDOWN_THRESHOLD,
                                    _STATE.get('cool_until') or '待定'))
        return _out(symbol, 'NONE', 0.0, defend, check, ['冷却中'], ts)

    # ---------- 一、全局前置过滤 (最高优先级) ----------
    # 1a. 周线波浪方向不明确 -> 直接 NONE (禁止开新仓)
    if ws == 'UNCERTAIN':
        check.append('不通过: 周线波浪UNCERTAIN, 大周期方向不明确, 禁止开新仓')
        return _out(symbol, 'NONE', 0.0, defend, check,
                    ['周线波浪UNCERTAIN'], ts)
    # 1b. 异常波动率开关 (跳空/异常巨量, 市场结构异常) -> 屏蔽信号
    if volatility.get('abnormal'):
        check.append('不通过: 异常波动率开关触发(跳空/异常巨量), '
                     '市场结构异常, 暂停信号输出')
        return _out(symbol, 'NONE', 0.0, defend, check,
                    ['异常波动率'], ts)
    # 注: 规则一"bias==BEAR 只允许极小仓位短线反弹, 禁止开多重仓"——
    #     LONG_OPEN 条件1 要求 bias=='BULL', 故 BEAR 时永不产生开仓信号;
    #     若已有持仓, 由下方 LONG_CLOSE 条件④ (BULL->BEAR) 负责离场。

    # ---------- 三、平仓信号 (先平后开, 风险优先) ----------
    # LONG_CLOSE 条件④: 周线波浪由 BULL 转为 BEAR
    if bias == 'BEAR':
        check.append('LONG_CLOSE: 周线波浪由BULL转为BEAR, 全部离场')
        return _out(symbol, 'LONG_CLOSE', 0.0, defend, check,
                    ['周线转BEAR'], ts)
    # LONG_CLOSE 条件②: 浪型结构破坏
    if broken:
        check.append('LONG_CLOSE: 周线浪型结构破坏(wave_broken=True), 全部离场')
        return _out(symbol, 'LONG_CLOSE', 0.0, defend, check,
                    ['浪型破坏'], ts)
    # LONG_CLOSE 条件①: 收盘价有效跌破止损位, 连续2根收盘 < defend_price
    #   (生产: M30收盘; 日线慢速版降级: M30缺失, 用日线收盘)
    if _closed_below_2bars(chan_day if m30_missing else chan_m30, defend):
        lvl = '日线' if m30_missing else 'M30'
        check.append('LONG_CLOSE: 连续2根%s收盘价<止损位%.2f(盘中穿刺不算), 全部平仓'
                     % (lvl, defend))
        return _out(symbol, 'LONG_CLOSE', 0.0, defend, check,
                    ['收盘破止损'], ts)
    # LONG_CLOSE 条件③: M30 出现 S2/S3 卖点 (线段反转)
    if chan_m30.get('sell_point') in ('S2', 'S3'):
        check.append('LONG_CLOSE: M30出现%s顶卖点, 线段反转, 全部平仓'
                     % chan_m30.get('sell_point'))
        return _out(symbol, 'LONG_CLOSE', 0.0, defend, check,
                    ['M30 %s卖点' % chan_m30.get('sell_point')], ts)
    # REDUCE 条件①: 波浪运行到5浪末端量能衰竭。
    #   解读: wave_label=='5' 且 wave_status=='COMPLETE' (5浪已走完=末端衰竭);
    #   5浪 RUNNING(刚启动/延伸中) 不算末端, 允许进入 LONG_OPEN 的 5 浪小仓位分支
    #   (否则规则二/五的"周线5浪仓位0.2~0.3"将成为死代码)。
    if wl == '5' and ws == 'COMPLETE':
        check.append('REDUCE: 周线5浪末端量能衰竭(wave_label=5且已走完), 减仓50%')
        return _out(symbol, 'REDUCE', 0.0, defend, check,
                    ['5浪末端衰竭'], ts)
    # REDUCE 条件②: M30 出现 S1 顶卖点且顶背驰确认
    if chan_m30.get('sell_point') == 'S1' and chan_m30.get('beichi'):
        check.append('REDUCE: M30出现S1顶卖点且顶背驰确认, 减仓50%')
        return _out(symbol, 'REDUCE', 0.0, defend, check,
                    ['M30 S1+顶背驰'], ts)

    # ---------- 四、禁止开仓黑名单 (命中直接 NONE) ----------
    # 黑名单2: 周线5浪已走完进入A浪调整 (label=='A' 且 bias 转空)
    #   (与 LONG_CLOSE 条件④ 重叠, 此处防御性保留: 无持仓时调用方忽略 LONG_CLOSE,
    #    本分支保证不会开新仓)
    if wl == 'A' and bias == 'BEAR':
        check.append('不通过: 周线5浪已走完进入A浪调整, 禁止开新仓')
        return _out(symbol, 'NONE', 0.0, defend, check, ['A浪调整'], ts)
    # 黑名单4: 缠论有买点但周线 BEAR 大C浪下跌
    if wl == 'C' and bias == 'BEAR':
        check.append('不通过: 周线BEAR大C浪下跌, 禁止抄底')
        return _out(symbol, 'NONE', 0.0, defend, check, ['大C浪下跌'], ts)
    # 黑名单3: 波浪看多但缠论无任何买点 (单一信号不交易)
    if bias == 'BULL' and not chan_m30.get('buy_point') and not chan_day.get('buy_point'):
        check.append('不通过: 波浪看多但缠论无任何买点, 单一信号不交易')
        return _out(symbol, 'NONE', 0.0, defend, check,
                    ['单一信号不交易'], ts)

    # ---------- 二、LONG_OPEN 触发条件 (全部 AND, 缺一不可) ----------
    ok = True
    # 条件1: 周线 bias=BULL + wave_status=RUNNING(2浪结束准备3浪/4浪结束准备5浪)
    #        + wave_broken=False
    c1 = (bias == 'BULL' and ws == 'RUNNING' and not broken)
    check.append('周线波浪检查: bias=%s status=%s broken=%s -> %s'
                 % (bias, ws, broken, '通过' if c1 else '不通过'))
    if not c1:
        ok = False
    # 条件2: 日线 trend != DOWN; 且无大级别顶背驰 (sell_point 不是 S1/S2/S3
    #        保守处理: 日线出现任何顶卖点则不加仓)
    c2 = chan_day.get('trend') != 'DOWN'
    check.append('日线趋势检查: trend=%s -> %s'
                 % (chan_day.get('trend'), '通过' if c2 else '不通过'))
    if not c2:
        ok = False
    if chan_day.get('sell_point') in ('S1', 'S2', 'S3'):
        check.append('日线顶卖点检查: 日线存在%s顶卖点, 不加仓'
                     % chan_day.get('sell_point'))
        ok = False
    # 条件3: 买点 B1/B2 (B3 允许但优先级低) + 背驰确认
    #   (生产: M30 买点; 日线慢速版降级: M30缺失, 以日线买点为准)
    bp = chan_day.get('buy_point') if m30_missing else chan_m30.get('buy_point')
    c3 = bp in ('B1', 'B2', 'B3')
    check.append('%s买点检查: buy_point=%s -> %s'
                 % ('日线' if m30_missing else 'M30', bp,
                    '通过' if c3 else '不通过'))
    if not c3:
        ok = False
    if not chan_m30.get('beichi'):
        if m30_missing:
            check.append('M30背驰检查(降级): M30缺失, 跳过背驰确认')
        else:
            check.append('M30背驰检查: M30无背驰确认(beichi=False), 不通过')
            ok = False
    # 条件4: M5 次级别背驰确认 (不允许提前预判抄底); M5 不能处于强烈顶背驰
    #   (日线慢速版降级: M5缺失, 跳过次级别确认)
    if m5_missing:
        c4 = True
        check.append('M5背驰检查(降级): M5缺失, 跳过次级别背驰确认')
    else:
        c4 = chan_m5.get('beichi') is True
        check.append('M5背驰检查: beichi=%s -> %s'
                     % (chan_m5.get('beichi'), '通过' if c4 else '不通过'))
        if not c4:
            ok = False
    if chan_m5.get('sell_point') in ('S1', 'S2'):
        check.append('M5顶卖点检查: M5处于%s强烈顶背驰, 否决' % chan_m5.get('sell_point'))
        ok = False
    # 条件5: 共振校验 —— 波浪看多+缠论买点, 无冲突 (缠论无卖点冲突)
    conflict = (chan_day.get('sell_point') in ('S1', 'S2', 'S3')
                or chan_m30.get('sell_point') in ('S1', 'S2', 'S3')
                or chan_m5.get('sell_point') in ('S1', 'S2', 'S3'))
    check.append('共振冲突检查: 波浪看多 vs 缠论卖点 -> %s'
                 % ('通过(无冲突)' if not conflict else '不通过(存在冲突)'))
    if conflict:
        ok = False

    if not ok:
        return _out(symbol, 'NONE', 0.0, defend, check, [], ts)

    # ---------- 五、仓位计算 (规则二+五, 硬上限 0.6) ----------
    pos = _calc_position(wl, bp)
    bp_src = '日线缠论' if m30_missing else 'M30缠论'
    reason = ['周线波浪多头', '%s%s买点' % (bp_src, bp),
              'M5背驰确认' if not m5_missing else 'M5缺失跳过', '浪型完好']
    if bp == 'B3':
        reason.append('B3低优先级买点, 仓位从轻')
    if wl == '5':
        reason.append('周线5浪阶段, 小仓位')
    check.append('仓位: %s阶段 + %s买点 -> position_rate=%.2f (上限%.1f)'
                 % (wl, bp, min(pos, MAX_POSITION), MAX_POSITION))
    return _out(symbol, 'LONG_OPEN', pos, defend, check, reason, ts)


# ==================== 自测 (真实 .hst 数据, 4品种全链路) ====================
if __name__ == '__main__':
    import os
    import sys
    import json
    # Windows 控制台 UTF-8 输出兼容
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    import wave_engine
    import chan_engine

    HST_BASE = r'C:\Program Files (x86)\Alpari MT4\history\Alpari-Demo'
    SYMBOLS = ['XAUUSD', 'BITCOIN', 'XAGUSD', 'WTI']

    def load(sym, tf):
        """读取指定周期 .hst, 不存在返回 None。tf 为分钟数 (WEEK=10080)。"""
        path = os.path.join(HST_BASE, '%s%d.hst' % (sym, tf))
        if os.path.exists(path):
            return chan_engine.load_hst(path)
        return None

    print('=' * 84)
    print('共振决策器自测 (resonance_engine.py) — 4品种完整链路')
    print('=' * 84)
    for sym in SYMBOLS:
        # ---- 读取各周期数据 (周线缺失用日线重采样兜底) ----
        wk = load(sym, 10080)
        wk_src = '原生周线10080.hst'
        if not wk:
            daily0 = load(sym, 1440)
            wk = wave_engine.resample(daily0, 10080) if daily0 else None
            wk_src = '1440.hst重采样->周线(原生缺失)'
        day = load(sym, 1440)
        m30 = load(sym, 30)
        m5 = load(sym, 5)
        ok = {'WEEK': bool(wk), 'DAY': bool(day), 'M30': bool(m30), 'M5': bool(m5)}
        print('\n' + '-' * 84)
        print('[%s] 数据齐全: WEEK=%s(%s) DAY=%s M30=%s M5=%s'
              % (sym, ok['WEEK'], wk_src, ok['DAY'], ok['M30'], ok['M5']))
        if not all(ok.values()):
            print('  !! 数据不齐全, 跳过 evaluate')
            continue

        # ---- 各引擎计算 ----
        wave_week = wave_engine.compute(wk)
        chan_day = chan_engine.compute(day, 'DAY')
        chan_m30 = chan_engine.compute(m30, 'M30')
        chan_m5 = chan_engine.compute(m5, 'M5')
        atr = (wave_week.get('details') or {}).get('atr', 0.0)
        lc = (wave_week.get('details') or {}).get('last_close', 0.0)
        atr_pct = (atr / lc * 100.0) if lc else 0.0
        volatility = {'atr_pct': round(atr_pct, 3), 'abnormal': False}
        # abnormal 由调用方(数据层)判定: 跳空/异常巨量时置 True。
        # 自测中仅展示 atr_pct, 不自行判定 abnormal。

        # ---- 决策 ----
        res = evaluate(sym, wave_week, chan_day, chan_m30, chan_m5, volatility)
        print('  周线波浪: label=%s status=%s bias=%s broken=%s | 日线: trend=%s '
              'bp=%s sp=%s' % (wave_week['wave_label'], wave_week['wave_status'],
                               wave_week['bias'], wave_week['wave_broken'],
                               chan_day['trend'], chan_day['buy_point'],
                               chan_day['sell_point']))
        print('  M30: trend=%s bp=%s sp=%s beichi=%s | M5: beichi=%s sp=%s'
              % (chan_m30['trend'], chan_m30['buy_point'], chan_m30['sell_point'],
                 chan_m30['beichi'], chan_m5['beichi'], chan_m5['sell_point']))
        print('  波动率: atr_pct=%.3f%% abnormal=%s | 决策: signal=%s 仓位=%s '
              '止损=%s' % (atr_pct, volatility['abnormal'], res['signal'],
                          res['position_rate'], res['stop_loss_price']))
        print('  check_list:')
        for c in res['check_list']:
            print('    - ' + c)
        if res['reason']:
            print('  reason: ' + ', '.join(res['reason']))
        # 输出完整 JSON 摘要 (signal/仓位/止损/条件/标签)
        print('  JSON摘要: %s' % json.dumps(
            {k: res[k] for k in ('signal', 'position_rate', 'stop_loss_price',
                                 'reduce_condition', 'close_condition', 'reason')},
            ensure_ascii=False))

    # ---- 冷却机制演示 (规则六: 连续3次证伪 -> 冷却) ----
    # 用临时文件演示, 避免污染真实 state/cooldown.json
    print('\n' + '=' * 84)
    print('冷却机制演示 (连续3次 LONG_OPEN 被证伪 -> 冷却 -> NONE)')
    print('=' * 84)
    import tempfile
    _demo_file = os.path.join(tempfile.gettempdir(), 'chan_wave_cooldown_demo.json')
    _orig_file = globals()['COOLDOWN_FILE']
    globals()['COOLDOWN_FILE'] = _demo_file
    _STATE['false_count'] = 0
    _STATE['cooldown'] = False
    _STATE['cool_until'] = None
    for i in range(1, 4):
        mark_result(False)
        print('  mark_result(False) 第%d次: false_count=%d cooldown=%s cool_until=%s'
              % (i, _STATE['false_count'], _STATE['cooldown'],
                 _STATE['cool_until']))
    dummy = {'wave_label': '3', 'wave_status': 'RUNNING', 'bias': 'BULL',
             'wave_broken': False, 'details': {}}
    demo = evaluate('XAUUSD', dummy, {}, {}, {}, {'atr_pct': 1.0, 'abnormal': False})
    print('  冷却期间 evaluate -> signal=%s (check_list: %s)'
          % (demo['signal'], demo['check_list'][0]))
    mark_result(True)
    print('  mark_result(True) 解除冷却 -> cooldown=%s false_count=%s'
          % (_STATE['cooldown'], _STATE['false_count']))
    globals()['COOLDOWN_FILE'] = _orig_file
    try:
        if os.path.exists(_demo_file):
            os.remove(_demo_file)
    except Exception:
        pass
    print('\n自测完成 (python resonance_engine.py 无报错)')
