# -*- coding: utf-8 -*-
"""
chan_engine.py — 缠论核心算法引擎 (模拟盘信号研究项目, 只做信号计算, 禁止自动下单)

功能: 输入已闭合K线序列, 输出标准化的 chan_result 字典:
  包含处理 -> 分型 -> 笔 -> 线段 -> 中枢 -> 背驰(MACD面积) -> 买卖点 -> 趋势/防守价

输入契约:
  compute(candles: list[dict], level: str) -> dict
  candles 元素: {'time': int, 'open': float, 'high': float, 'low': float,
                 'close': float, 'vol': float}, 按时间升序, 必须是已闭合K线
                 (调用方负责剔除未闭合K线, 本模块不处理)。
  level: 'DAY'/'M30'/'M5' 等字符串, 仅用于返回结果标注。

数据读取辅助:
  load_hst(path) 读取 MT4 .hst 历史文件 (Alpari 格式, 实测验证):
    - 文件头 148 字节 (版本信息, 跳过)
    - 每条记录 60 字节: struct '<qddddq' = ctm(8) + open(8) + high(8)
      + low(8) + close(8) + vol(8), 剩余 12 字节填充
    - 返回与 compute 输入一致的 candles 列表 (按时间升序)
  (解析方式参考 Trading/backtest/fvg_backtest.py、autotrade_daemon.py 的 read_hst)

算法说明 (标准缠论 + 明确标注的简化规则):
  1. 包含处理: 相邻K线存在包含关系时, 按最近趋势方向合并
     (向上趋势取高高=max(高,高)/max(低,低), 向下趋势取低低)
  2. 分型: 标准三K分型 (中间K线高点同时最高且低点同时最低 = 顶分型, 对称 = 底分型),
     分型不共用K线 (由笔构建阶段的间隔约束保证)
  3. 笔: 顶底分型交替连接, 一笔至少覆盖 MIN_BI_GAP+1 根合并K线(含两端分型),
     分型确认后才成笔 (全量计算中所有分型右侧均有K线, 天然已确认)
  4. 线段: 简化规则 —— 至少 MIN_SEG_BI 笔, 线段方向=第一笔方向,
     同向笔必须创新高/新低才延续, 反向笔(回调)与线段区间重叠即可
  5. 中枢: 连续 ZS_MIN_BI 笔的重叠区间 [ZG=min(高点), ZD=max(低点)],
     之后连续 ZS_EXIT_BI 笔完全脱离区间才确认中枢结束 (简化: 不做扩展合并)
  6. 背驰: MACD(12,26,9) 柱面积比较 (默认) —— 最后中枢的离开段(b, 中枢后全部笔)
     与进入段(a, 中枢前最近同向笔) 比较: 价格创新高/新低 且 同色柱面积缩小
     => 背驰 (顶背驰/底背驰); 无中枢时退化为比较最后两笔同向笔。
     背驰比较方式可配置 (BEICHI_MODE): 'area'=柱面积比较(默认) /
     'dif_peak'=DIF峰值比较 (标定用, 直接比较 a/b 段 DIF 极值, 不看面积)。
     MACD 周期参数 (MACD_FAST/SLOW/SIGNAL) 亦为模块级常量, 支持回测标定。
  7. 买卖点:
     B1 = 下跌趋势末端底背驰 (价格新低 + MACD绿柱面积缩小)
     B2 = B1 后存在历史回调笔, 回调低点不破 B1 低点, 且回调笔之后未再跌破
          B1 低点 (不要求回调笔是最新一笔; 2026-09-01 修复, 原逻辑要求
          "最新一笔是回调"导致 B2 从不触发)
     B3 = 突破中枢上沿(ZG)后回踩, 回踩低点不跌回中枢内
     S1/S2/S3 对称 (顶背驰 / 反弹不破前高 / 跌破中枢下沿后反抽不回中枢)
  8. trend: 最后中枢方向 + 最近笔方向 + 现价相对中枢位置
  9. defend_price: 最近买点对应的防守位 (B1 按 DEFEND_MODE 计算, 默认=中枢下沿;
     B2=B1低点, B3=中枢下沿; 无中枢=B1低点)。DEFEND_MODE 见模块顶部参数注释。

用法:
  python chan_engine.py          # 自测: 读 XAUUSD1440/M30 运行 compute 并打印摘要
"""

import os
import struct

# ==================== 引擎参数 (集中于此, 便于后续标定) ====================
# --- MACD 参数 ---
MACD_FAST = 12          # 快线周期
MACD_SLOW = 26          # 慢线周期
MACD_SIGNAL = 9         # 信号线周期

# --- 分型参数 ---
FRACTAL_CONFIRM = 1     # 分型确认根数: 1 = 标准三K分型 (右侧1根K线确认);
                        # 2 = 四K分型 (右侧2根K线确认, 更严格, 分型更少)

# --- 笔参数 ---
MIN_BI_GAP = 4          # 一笔两端分型在合并K线上的最小间隔 (间隔>=4 即至少5根合并K线含分型)

# --- 线段参数 ---
MIN_SEG_BI = 3          # 一条线段最少包含的笔数

# --- 中枢参数 ---
ZS_MIN_BI = 3           # 中枢最少由连续几笔构成 (标准=3笔次级别走势)
ZS_EXIT_BI = 3          # 连续几笔完全脱离中枢区间才确认中枢结束 (标准=3笔)

# --- 背驰参数 ---
BEICHI_RATIO = 1.0      # 背驰面积缩小判定: b段面积 < a段面积 * BEICHI_RATIO 即为缩小
                        # (=1.0 表示严格小于; 调低如 0.8 要求缩小 20% 才判背驰, 更保守)
USE_DIF_PEAK_FALLBACK = True   # 面积均为0时, 退化为用 DIF 极值比较 (True=启用)
BEICHI_MODE = 'area'    # 背驰比较方式: 'area'=MACD柱面积比较(默认, 原行为)
                        #   'dif_peak'=DIF峰值比较(标定用, 不看面积直接比较 a/b 段 DIF 极值)
                        # 注: calc_macd 默认参数取模块级常量, 支持回测标定时 monkey-patch

# --- 防守价(defend_price)参数 (参数标定阶段2: B1买点防守位定义对比) ---
# DEFEND_MODE 只影响 B1 (背驰类买点) 的 defend 计算:
#   'zs_low'    = 最近中枢下沿 (默认, 与原行为完全一致)。已知缺陷: B1 出现在
#                下跌趋势末端(价格创新低), 此时"最近中枢"在价格上方,
#                defend_price 可能高于现价 -> 无效止损(daemon 表现为"收盘破止损"误判)
#   'b1_low'    = B1 买点低点本身 (背驰极值低点 b_ext, 最紧的止损)
#   'b1_atr'    = B1 买点低点 - DEFEND_ATR_MULT × ATR(DEFEND_ATR_PERIOD)
#                (低点下方留一个 ATR 缓冲, 防止最紧止损被毛刺扫掉)
#   'seg_start' = 背驰段(离开段/b段)起点下方: 取背驰段第一只同向笔的摆动低点
#                (即"当前趋势段起始摆动低点"; 对底背驰 = 末段下跌第一腿的低点)
# B2 (回调类) 的 defend 保持"B1低点"、B3 (突破类) 的 defend 保持"中枢下沿"不变 ——
# 回调/突破类买点出现时价格已在中枢上方/回调低点高于B1低点, 原语义更合理。
DEFEND_MODE = 'b1_low'          # B1 防守位定义 (阶段2标定结论: 'zs_low'中枢下沿在B1场景100%无效止损, 已切换'b1_low'; 回测扫描时 monkey-patch 切换)
DEFEND_ATR_MULT = 1.0           # 'b1_atr' 模式: B1 低点下方 ATR 倍数
DEFEND_ATR_PERIOD = 14          # 'b1_atr' 模式 ATR 周期
# ==========================================================================


# ---------------------------------------------------------------- 数据读取
def load_hst(path):
    """读取 MT4 .hst 历史文件, 返回 candles 列表 (与 compute 输入格式一致)。

    格式 (实测 Alpari 文件验证): 148 字节版本头 + 每条 60 字节记录,
    记录 = '<qddddq': ctm(8) + open(8) + high(8) + low(8) + close(8) + vol(8)。
    注意: MT4 .hst 只包含已闭合K线 (当前形成中的K线不写入历史文件),
    故本函数不剔除任何记录; 若调用方数据源含未闭合K线, 需自行剔除后再调 compute。
    """
    sz = os.path.getsize(path)
    n = (sz - 148) // 60          # 记录条数 (版本头 148 字节)
    bars = []
    with open(path, 'rb') as f:
        f.seek(148)
        for _ in range(n):
            rec = f.read(60)
            if len(rec) < 60:     # 尾部残缺记录直接丢弃
                break
            t, o, h, l, c, v = struct.unpack('<qddddq', rec[:48])
            bars.append({'time': int(t), 'open': o, 'high': h,
                         'low': l, 'close': c, 'vol': float(v)})
    bars.sort(key=lambda x: x['time'])
    return bars


# ---------------------------------------------------------------- 1. 包含处理
def merge_containing(candles):
    """相邻K线包含合并, 生成标准K线序列。

    返回 list[dict]: {'high','low','start_idx','end_idx'}
      start_idx/end_idx 为该合并K线覆盖的原始 candles 索引范围 (左闭右闭),
      用于把后续分型/笔的 bar 索引映射回输入 candles。
    包含规则: 趋势向上(高点抬高)时取高高, 趋势向下(高点降低)时取低低。
    """
    merged = []
    direction = 0                 # 1=向上, -1=向下, 0=初始未知
    for i, c in enumerate(candles):
        k = {'high': c['high'], 'low': c['low'], 'start_idx': i, 'end_idx': i}
        if not merged:
            merged.append(k)
            continue
        last = merged[-1]
        # 包含关系: 一根K线的高低点完全包住另一根 (含相等)
        contain = ((last['high'] >= k['high'] and last['low'] <= k['low']) or
                   (k['high'] >= last['high'] and k['low'] <= last['low']))
        if contain:
            if direction >= 0:    # 向上或方向未知: 取高高
                new_h = max(last['high'], k['high'])
                new_l = max(last['low'], k['low'])
            else:                 # 向下: 取低低
                new_h = min(last['high'], k['high'])
                new_l = min(last['low'], k['low'])
            merged[-1] = {'high': new_h, 'low': new_l,
                          'start_idx': last['start_idx'], 'end_idx': k['end_idx']}
        else:
            # 非包含: 由高点关系更新方向 (不包含时高点更高必然低点也更高)
            direction = 1 if k['high'] > last['high'] else -1
            merged.append(k)
    return merged


# ---------------------------------------------------------------- 2. 分型
def find_fractals(merged):
    """在合并K线序列上找顶/底分型 (标准三K分型, 中间K线高低点同时极值)。

    返回 list[dict]: {'type':'top'/'bottom', 'pos': 合并K线位置, 'price': 分型价格,
                      'bar': 映射回输入 candles 的 bar 索引}
    bar 索引取分型所在合并K线的 end_idx (分型需右侧K线确认, 用最晚确认时刻)。
    """
    fractals = []
    conf = FRACTAL_CONFIRM
    for i in range(1, len(merged) - conf):
        k = merged[i]
        left = merged[i - 1]
        right_list = merged[i + 1:i + 1 + conf]
        # 顶分型: 中间K线高点最高且低点最高
        if (k['high'] > left['high'] and k['low'] > left['low'] and
                all(k['high'] > r['high'] and k['low'] > r['low'] for r in right_list)):
            fractals.append({'type': 'top', 'pos': i, 'price': k['high'],
                             'bar': k['end_idx']})
        # 底分型: 中间K线低点最低且高点最低
        elif (k['low'] < left['low'] and k['high'] < left['high'] and
                all(k['low'] < r['low'] and k['high'] < r['high'] for r in right_list)):
            fractals.append({'type': 'bottom', 'pos': i, 'price': k['low'],
                             'bar': k['start_idx']})
    return fractals


# ---------------------------------------------------------------- 3. 笔
def build_bi(fractals):
    """由分型序列构建笔序列。

    规则:
      - 分型类型必须交替 (顶底顶底...), 同型分型保留更强者 (顶=价高者, 底=价低者)
      - 相邻分型间隔 >= MIN_BI_GAP (一笔至少 5 根合并K线含分型),
        间隔不足时删除较弱分型 (迭代直到稳定)
    返回 list[dict]: {'start','end','dir','high','low','start_price','end_price',
                      'start_pos','end_pos'}
      start/end 为输入 candles 的 bar 索引 (首尾相接: 前一笔 end == 后一笔 start),
      high/low 在 compute 中补充 (按原始K线区间极值)。
    """
    if len(fractals) < 2:
        return []
    seq = list(fractals)
    # 迭代: 交替化 + 间隔检查, 直到没有分型被删除
    while True:
        # --- 步骤1: 同型分型合并 (保留强者, 保证类型交替) ---
        new_seq = []
        for fr in seq:
            if new_seq and fr['type'] == new_seq[-1]['type']:
                last = new_seq[-1]
                stronger = ((fr['type'] == 'top' and fr['price'] > last['price']) or
                            (fr['type'] == 'bottom' and fr['price'] < last['price']))
                if stronger:
                    new_seq[-1] = fr
            else:
                new_seq.append(fr)
        seq = new_seq
        # --- 步骤2: 间隔不足的分型对, 删除较弱者 ---
        removed = False
        i = 0
        while i < len(seq) - 1:
            f1, f2 = seq[i], seq[i + 1]
            if f2['pos'] - f1['pos'] < MIN_BI_GAP:
                # 强者判定: 顶分型价格高者强, 底分型价格低者强
                f1_strong = ((f1['type'] == 'top' and f1['price'] >= f2['price']) or
                             (f1['type'] == 'bottom' and f1['price'] <= f2['price']))
                if f1_strong:
                    del seq[i + 1]
                else:
                    del seq[i]
                    i = max(0, i - 1)
                removed = True
            else:
                i += 1
        if not removed:
            break
    # --- 成笔 ---
    bis = []
    for i in range(len(seq) - 1):
        f1, f2 = seq[i], seq[i + 1]
        bis.append({'start': f1['bar'], 'end': f2['bar'],
                    'dir': 'UP' if f1['type'] == 'bottom' else 'DOWN',
                    'start_price': f1['price'], 'end_price': f2['price'],
                    'start_pos': f1['pos'], 'end_pos': f2['pos']})
    return bis


# ---------------------------------------------------------------- 4. 线段
def build_seg(bis):
    """由笔序列构建线段 (简化规则)。

    规则:
      - 线段至少 MIN_SEG_BI 笔, 方向 = 第一笔方向
      - 同向笔必须创新高(UP段)/创新低(DOWN段)才延续, 否则线段结束于该笔之前
      - 反向笔(回调)直接吸收 (简化: 不检查特征序列分型)
    返回 list[dict]: {'start','end','dir','high','low'} (bar 索引)
    """
    segs = []
    n = len(bis)
    i = 0
    while i < n:
        seg_dir = bis[i]['dir']
        last_same = bis[i]
        j = i + 1
        while j < n:
            bj = bis[j]
            if bj['dir'] == seg_dir:
                if seg_dir == 'UP':
                    if bj['end_price'] > last_same['end_price']:
                        last_same = bj
                        j += 1
                    else:
                        break
                else:
                    if bj['end_price'] < last_same['end_price']:
                        last_same = bj
                        j += 1
                    else:
                        break
            else:
                j += 1
        if j - i >= MIN_SEG_BI:
            seg_bis = bis[i:j]
            segs.append({'start': seg_bis[0]['start'], 'end': seg_bis[-1]['end'],
                         'dir': seg_dir,
                         'high': max(b['high'] for b in seg_bis),
                         'low': min(b['low'] for b in seg_bis)})
            i = j
        else:
            i += 1
    return segs


# ---------------------------------------------------------------- 5. 中枢
def build_zhongshu(bis):
    """由笔序列构建中枢 (简化规则)。

    规则:
      - 连续 ZS_MIN_BI 笔 (默认3笔) 的重叠区间:
          ZG = min(三笔高点), ZD = max(三笔低点), ZG > ZD 才有重叠
      - 中枢延伸: 后续笔与 [ZD, ZG] 有重叠则延长 end_bar
        (简化: 延伸不更新 ZG/ZD, 即不做标准的中枢扩展/升级)
      - 中枢结束: 连续 ZS_EXIT_BI 笔完全脱离区间 (简化: 不要求回抽确认)
    返回 list[dict]: {'high','low','start_bar','end_bar'} (bar 索引)
    """
    zs_list = []
    n = len(bis)
    i = 0
    while i + ZS_MIN_BI - 1 < n:
        trio = bis[i:i + ZS_MIN_BI]
        zg = min(b['high'] for b in trio)
        zd = max(b['low'] for b in trio)
        if zg > zd:
            # 中枢成立, 尝试延伸
            end_idx = i + ZS_MIN_BI - 1
            j = end_idx + 1
            depart = 0
            while j < n:
                bj = bis[j]
                if bj['high'] >= zd and bj['low'] <= zg:   # 与中枢区间重叠 (含触碰)
                    depart = 0
                    end_idx = j
                else:
                    depart += 1
                    if depart >= ZS_EXIT_BI:
                        break
                j += 1
            zs_list.append({'high': zg, 'low': zd,
                            'start_bar': bis[i]['start'],
                            'end_bar': bis[end_idx]['end']})
            i = end_idx + 1        # 从中枢结束后的下一笔重新寻找
        else:
            i += 1
    return zs_list


# ---------------------------------------------------------------- MACD
def _ema(values, period):
    """标准 EMA (以首值为种子)。"""
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def calc_macd(closes, fast=None, slow=None, signal=None):
    """MACD: 返回 (dif, dea, hist), hist = 2*(DIF-DEA) (国内惯例乘2, 不影响面积比较)。
    数据不足 (len < slow+signal) 时返回 (None, None, None)。
    fast/slow/signal 为 None 时取模块级 MACD_FAST/MACD_SLOW/MACD_SIGNAL
    —— 支持回测标定时 monkey-patch 模块级常量直接生效。"""
    if fast is None:
        fast = MACD_FAST
    if slow is None:
        slow = MACD_SLOW
    if signal is None:
        signal = MACD_SIGNAL
    if len(closes) < slow + signal:
        return None, None, None
    ema_f = _ema(closes, fast)
    ema_s = _ema(closes, slow)
    dif = [f - s for f, s in zip(ema_f, ema_s)]
    dea = _ema(dif, signal)
    hist = [2.0 * (d - e) for d, e in zip(dif, dea)]
    return dif, dea, hist


def _compute_atr(candles, period=None):
    """ATR (Wilder 平滑, 公式与 wave_engine.compute_atr 一致)。

    供 'b1_atr' 防守位模式使用 (B1 低点下方留 DEFEND_ATR_MULT×ATR 缓冲)。
    输入不足 period 根时退回简单平均波幅; 不足 2 根返回 0.0。
    """
    if period is None:
        period = DEFEND_ATR_PERIOD
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c['high'] - c['low'],
                       abs(c['high'] - p['close']),
                       abs(c['low'] - p['close'])))
    if len(trs) < period:
        return sum(trs) / len(trs)
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


# ---------------------------------------------------------------- 6. 背驰
def _beichi_for_zs(zs, bis, dif, hist):
    """单中枢离开段背驰判定 (供事件扫描/主背驰复用)。

    - 离开方向: 中枢后走势相对中枢上下边界的突破方向
      (上下都突破=宽幅震荡, 兜底用第一笔方向)
    - a 段: 中枢前最近同向笔; b 段: 从中枢最后一笔重叠笔(离开笔)起点
      到"达到离开方向极值"的最后一笔 (只取到极值笔为止, 避免后续
      反弹/回调笔污染面积比较; 含离开笔本身, 避免漏掉穿越中枢的离开段)
    - 顶背驰: 离开 UP, b 高点 > a 高点 且 b 红柱面积 < a 红柱面积
    - 底背驰: 离开 DOWN, b 低点 < a 低点 且 b 绿柱面积 < a 绿柱面积
    - 面积均为 0 时退化为 DIF 峰值比较 (USE_DIF_PEAK_FALLBACK)
    返回背驰 dict 或 None: {'type','a_bi','b_bi','a_ext','b_ext','a_area','b_area'}
    """
    # ---- b 段选取 (离开段) ----
    # 中枢延伸可能把离开笔 + 其后的反弹/回调笔全部吸进中枢 (end_bar 被推到最后
    # 一根重叠笔), 因此"end_bar 之后的笔"可能为空或只剩未创新极值的笔。
    # 处理: 用"中枢 start_bar 之后的全部笔"确定离开方向 (突破幅度大者优先),
    # 极值笔取"方向==离开方向且达到极值"的最后一笔 (被吸收的离开笔也会被找回)。
    all_after = [b for b in bis if b['start'] >= zs['start_bar']]
    if not all_after:
        return None
    up_ext = max(b['high'] for b in all_after)
    dn_ext = min(b['low'] for b in all_after)
    up_break = up_ext - zs['high']          # 向上突破幅度 (>0 表示突破上沿)
    dn_break = zs['low'] - dn_ext           # 向下突破幅度 (>0 表示突破下沿)
    if up_break > 0 and up_break >= dn_break:
        b_dir = 'UP'
    elif dn_break > 0:
        b_dir = 'DOWN'
    else:
        # 中枢内震荡无突破: 兜底用最后一笔方向
        b_dir = all_after[-1]['dir']
    # 极值笔必须与离开方向同向 —— 离开笔创出新低后, 反弹笔的 low 常与其起点
    # 相连而相等, 若不限定同向, 反弹笔会被误选为极值笔 (即"极值笔被中枢延伸
    # 吸到反弹笔"缺陷, 导致 B1 事件错绑)。
    same = [b for b in all_after if b['dir'] == b_dir]
    if not same:
        return None
    ext = max(b['high'] for b in same) if b_dir == 'UP' else min(b['low'] for b in same)
    if b_dir == 'UP':
        cand = [b for b in same if b['high'] == ext]
    else:
        cand = [b for b in same if b['low'] == ext]
    b_end_bi = cand[-1]                      # 最后一笔达到极值的同向笔
    b_end = b_end_bi['end']
    # b 段起点: end_bar 之后第一只同向笔; 若中枢延伸吸光了后续笔, 回溯到极值笔
    after_end = [b for b in all_after if b['start'] >= zs['end_bar'] and b['dir'] == b_dir]
    b_start = after_end[0]['start'] if after_end else b_end_bi['start']
    # b 段区间下界: 中枢延伸吸收离开笔时 b_start 可能 > b_end_bi['end']
    # (面积模式空切片=0 不报错, DIF 峰值模式 max() 会炸) -> 取下界 min
    b_lo = min(b_start, b_end_bi['start'])
    # a 段: 中枢前最近同向笔
    before = [b for b in bis if b['end'] <= zs['start_bar']]
    a_bi = None
    for b in reversed(before):
        if b['dir'] == b_dir:
            a_bi = b
            break
    if a_bi is None:
        return None
    if b_dir == 'UP':
        a_ext = a_bi['high']
        b_ext = ext
        a_area = sum(max(0.0, x) for x in hist[a_bi['start']:a_bi['end'] + 1])
        b_area = sum(max(0.0, x) for x in hist[b_start:b_end + 1])
        new_ext = b_ext > a_ext
    else:
        a_ext = a_bi['low']
        b_ext = ext
        a_area = sum(max(0.0, -x) for x in hist[a_bi['start']:a_bi['end'] + 1])
        b_area = sum(max(0.0, -x) for x in hist[b_start:b_end + 1])
        new_ext = b_ext < a_ext
    shrink = (a_area > 0 and b_area < a_area * BEICHI_RATIO)
    if BEICHI_MODE == 'dif_peak':
        # DIF 峰值比较 (标定模式): 不看面积, 直接比较 a/b 段 DIF 极值
        #   UP(顶背驰): b 段 DIF 峰值 < a 段峰值; DOWN(底背驰): b 段谷值 > a 段谷值
        if b_dir == 'UP':
            a_peak = max(dif[a_bi['start']:a_bi['end'] + 1])
            b_peak = max(dif[b_lo:b_end + 1])
        else:
            a_peak = min(dif[a_bi['start']:a_bi['end'] + 1])
            b_peak = min(dif[b_lo:b_end + 1])
        shrink = (b_peak < a_peak) if b_dir == 'UP' else (b_peak > a_peak)
    elif not shrink and USE_DIF_PEAK_FALLBACK and a_area == 0 and b_area == 0:
        # 面积全为0 (极端行情), 退化为 DIF 峰值比较
        if b_dir == 'UP':
            a_peak = max(dif[a_bi['start']:a_bi['end'] + 1])
            b_peak = max(dif[b_lo:b_end + 1])
        else:
            a_peak = min(dif[a_bi['start']:a_bi['end'] + 1])
            b_peak = min(dif[b_lo:b_end + 1])
        shrink = (b_peak < a_peak) if b_dir == 'UP' else (b_peak > a_peak)
    if new_ext and shrink:
        return {'type': 'top' if b_dir == 'UP' else 'bottom',
                'a_bi': a_bi, 'b_bi': b_end_bi,
                'a_ext': a_ext, 'b_ext': b_ext,
                'a_area': a_area, 'b_area': b_area,
                'b_start': b_start}   # 背驰段(b段/离开段)起点 bar 索引
                                      # (供 DEFEND_MODE='seg_start' 取背驰段起点摆动低点)
    return None


def detect_beichi(bis, zs_list, dif, hist):
    """主背驰检测: 从最后一个中枢往前找第一个有离开段且背驰成立的中枢。
    无中枢时退化为比较最后两笔同向笔 (退化路径)。
    返回背驰 dict 或 None。
    """
    if len(bis) < 2 or hist is None:
        return None
    for zs in reversed(zs_list):
        ev = _beichi_for_zs(zs, bis, dif, hist)
        if ev is not None:
            return ev
    # 无中枢 (或所有中枢均无离开段): 比较最后两笔同向笔
    b_bi = bis[-1]
    a_bi = None
    for b in reversed(bis[:-1]):
        if b['dir'] == b_bi['dir']:
            a_bi = b
            break
    if a_bi is None:
        return None
    b_dir = b_bi['dir']
    if b_dir == 'UP':
        a_ext, b_ext = a_bi['high'], b_bi['high']
        a_area = sum(max(0.0, x) for x in hist[a_bi['start']:a_bi['end'] + 1])
        b_area = sum(max(0.0, x) for x in hist[b_bi['start']:b_bi['end'] + 1])
        new_ext = b_ext > a_ext
    else:
        a_ext, b_ext = a_bi['low'], b_bi['low']
        a_area = sum(max(0.0, -x) for x in hist[a_bi['start']:a_bi['end'] + 1])
        b_area = sum(max(0.0, -x) for x in hist[b_bi['start']:b_bi['end'] + 1])
        new_ext = b_ext < a_ext
    shrink = (a_area > 0 and b_area < a_area * BEICHI_RATIO)
    if BEICHI_MODE == 'dif_peak':
        # DIF 峰值比较 (标定模式, 退化路径同样支持)
        if b_dir == 'UP':
            a_peak = max(dif[a_bi['start']:a_bi['end'] + 1])
            b_peak = max(dif[b_bi['start']:b_bi['end'] + 1])
        else:
            a_peak = min(dif[a_bi['start']:a_bi['end'] + 1])
            b_peak = min(dif[b_bi['start']:b_bi['end'] + 1])
        shrink = (b_peak < a_peak) if b_dir == 'UP' else (b_peak > a_peak)
    if new_ext and shrink:
        return {'type': 'top' if b_dir == 'UP' else 'bottom',
                'a_bi': a_bi, 'b_bi': b_bi,
                'a_ext': a_ext, 'b_ext': b_ext,
                'a_area': a_area, 'b_area': b_area,
                'b_start': b_bi['start']}   # 退化路径: 背驰段=最后一笔, 起点即其起点
    return None


def scan_beichi_events(bis, zs_list, dif, hist):
    """扫描所有中枢的离开段背驰, 返回事件列表 (按时间序)。
    每项: {'ev': 背驰dict, 'zs': 对应中枢}。供买卖点检测使用
    (B1/B2 依赖历史底背驰事件, 不能只看当前最后中枢)。
    """
    events = []
    for zs in zs_list:
        ev = _beichi_for_zs(zs, bis, dif, hist)
        if ev is not None:
            events.append({'ev': ev, 'zs': zs})
    return events


# ---------------------------------------------------------------- 8. 趋势
def detect_trend(bis, zs_list, last_close):
    """本级别趋势: 最后中枢 + 最近笔方向 + 现价相对中枢位置。
    返回 'UP'/'DOWN'/'SIDE'。
    """
    if not bis:
        return 'SIDE'
    last = bis[-1]
    zs = zs_list[-1] if zs_list else None
    # 最近同向笔是否创新高/新低 (隔一笔比较)
    new_ext = None
    if len(bis) >= 3 and last['dir'] == bis[-3]['dir']:
        if last['dir'] == 'UP' and last['end_price'] > bis[-3]['end_price']:
            new_ext = 'UP'
        elif last['dir'] == 'DOWN' and last['end_price'] < bis[-3]['end_price']:
            new_ext = 'DOWN'
    if zs:
        if last_close > zs['high']:
            return 'UP' if new_ext == 'UP' else 'UP'   # 中枢上方: 离开段向上
        if last_close < zs['low']:
            return 'DOWN' if new_ext == 'DOWN' else 'DOWN'
        return 'SIDE'                                   # 价格在中枢内部震荡
    return new_ext if new_ext else 'SIDE'


# ---------------------------------------------------------------- 7. 买卖点
def _defend_b1(b1_ev, bis, candles, atr):
    """按 DEFEND_MODE 计算 B1 买点防守价 (语义详见模块顶部参数注释)。

    只影响 B1 (背驰类买点); B2/B3 的 defend 在 detect_points 中保持不变。
    b1_ev: {'ev': 背驰dict, 'zs': 对应中枢或 None}; ev 含 b_ext(B1低点)/b_start。
    返回 defend 价格 (zs_low 无中枢时退化为 B1 低点, 与原行为一致)。
    """
    ev = b1_ev['ev']
    b1_low = ev['b_ext']                    # B1 买点低点 = 背驰极值低点
    mode = DEFEND_MODE
    if mode == 'b1_low':
        return b1_low
    if mode == 'b1_atr':
        return b1_low - DEFEND_ATR_MULT * atr
    if mode == 'seg_start':
        # 背驰段(离开段/b段)起点下方: 取背驰段第一只同向笔的摆动低点
        # (当前趋势段起始摆动低点; 对底背驰 = 末段下跌第一腿的低点)
        b_start = ev.get('b_start')
        if b_start is not None:
            for b in bis:
                if b['start'] == b_start:   # b_start 即该笔的起点 bar
                    return b['low']
            if candles and 0 <= b_start < len(candles):
                return candles[b_start]['low']   # 兜底: 起点 bar 的最低价
        return b1_low                       # 无背驰段信息 -> 退化为 B1 低点
    # 默认 'zs_low': 最近中枢下沿; 无中枢时用 B1 低点 (与原行为完全一致)
    return b1_ev['zs']['low'] if b1_ev['zs'] else b1_low


def detect_points(bis, zs_list, events, candles=None, atr=0.0):
    """买卖点检测 (基于历史背驰事件 + 最新笔结构, 简化规则)。

    参数 events: scan_beichi_events 的返回 (按时间序的背驰事件列表)。
    规则:
      B1: 最近底背驰事件 (下跌离开段创新低 + MACD绿柱面积缩小)
      B2: B1 之后出现过 UP 反弹笔, 且最新一笔是 DOWN 回调, 回调低点 > B1 低点
      B3: 存在 UP 笔突破最后中枢上沿 ZG, 且其后最近一笔是 DOWN 且低点 > ZG
      S1/S2/S3 对称。
    返回 (buy_point, sell_point, defend_price):
      defend = B1 时按 DEFEND_MODE 计算 (默认=最后中枢下沿, 无中枢用 B1 低点);
               B3 时最后中枢下沿, B2 时 B1 低点 (回调/突破类保持原语义不变)。
    """
    if not bis:
        return None, None, 0.0
    buy = sell = None
    buy_sig = sell_sig = -1
    defend = 0.0
    last_zs = zs_list[-1] if zs_list else None

    # 最近一次底/顶背驰事件 (按时间序取最后一个)
    b1_ev = s1_ev = None
    for item in events:
        if item['ev']['type'] == 'bottom':
            b1_ev = item
        else:
            s1_ev = item

    # ---- 三买/三卖 (基于最后中枢) ----
    if last_zs:
        # B3: 中枢形成后存在突破 ZG 的 UP 笔, 且其后有 DOWN 回踩笔低点 > ZG,
        #     且回踩笔之后没有笔再跌破 ZG (回踩确认后仍在中枢上方)
        ups_break = [b for b in bis
                     if b['start'] >= last_zs['start_bar'] and b['dir'] == 'UP'
                     and b['high'] > last_zs['high']]
        if ups_break:
            last_up = ups_break[-1]
            later = [b for b in bis if b['start'] >= last_up['end']]
            pullbacks = [b for b in later if b['dir'] == 'DOWN' and b['low'] > last_zs['high']]
            if pullbacks:
                pb = pullbacks[-1]
                after_pb = [b for b in later if b['start'] >= pb['end']]
                if not any(b['low'] < last_zs['high'] for b in after_pb):
                    buy, buy_sig, defend = 'B3', pb['end'], last_zs['low']
        # S3: 对称 —— 中枢形成后首次跌破 ZD 的 DOWN 笔之后, 有 UP 反抽笔高点 < ZD,
        #     且反抽笔之后没有笔再回到中枢上方 (反抽确认后仍在中枢下方)
        dns_break = [b for b in bis
                     if b['start'] >= last_zs['start_bar'] and b['dir'] == 'DOWN'
                     and b['low'] < last_zs['low']]
        if dns_break:
            first_dn = dns_break[0]
            later = [b for b in bis if b['start'] >= first_dn['end']]
            pullbacks = [b for b in later if b['dir'] == 'UP' and b['high'] < last_zs['low']]
            if pullbacks:
                pb = pullbacks[-1]
                after_pb = [b for b in later if b['start'] >= pb['end']]
                if not any(b['high'] > last_zs['low'] for b in after_pb):
                    sell, sell_sig = 'S3', pb['end']

    # ---- 一买/一卖 (最近背驰事件) ----
    if b1_ev:
        buy, buy_sig = 'B1', b1_ev['ev']['b_bi']['end']
        # B1 防守价按 DEFEND_MODE 计算 (阶段2标定参数, 见模块顶部注释);
        # 默认 'zs_low' = 最近中枢下沿(无中枢用 B1 低点), 与原行为完全一致
        defend = _defend_b1(b1_ev, bis, candles, atr)
    if s1_ev:
        sell, sell_sig = 'S1', s1_ev['ev']['b_bi']['end']

    # ---- 二买/二卖 (B1/S1 后的回调/反弹确认) ----
    # B2 修复 (2026-09-01): 原判定要求"最新一笔是 DOWN 回调笔且 B1 后必须有
    # 新的 UP 反弹笔", 一旦回调笔后出现再次反弹 (新 UP 笔), B2 即失效,
    # 导致 B2 几乎从不触发。现改为: B1 之后存在任意历史 DOWN 回调笔,
    # 回调低点 > B1 低点, 且该回调笔之后没有笔跌破 B1 低点
    # (不要求回调笔是最新一笔 —— 最新笔可以是回调后的再次反弹)。
    if b1_ev:
        b1_low = b1_ev['ev']['b_ext']
        b1_end = b1_ev['ev']['b_bi']['end']
        after_b1 = [b for b in bis if b['start'] >= b1_end]
        pullbacks = [b for b in after_b1
                     if b['dir'] == 'DOWN' and b['low'] > b1_low]
        if pullbacks:
            pb = pullbacks[-1]              # 最近的历史回调笔
            after_pb = [b for b in after_b1 if b['start'] >= pb['end']]
            if not any(b['low'] <= b1_low for b in after_pb):
                buy, buy_sig, defend = 'B2', pb['end'], b1_low
    if s1_ev:
        s1_high = s1_ev['ev']['b_ext']
        last = bis[-1]
        pullback = [b for b in bis
                    if b['start'] >= s1_ev['ev']['b_bi']['end'] and b['dir'] == 'DOWN']
        if last['dir'] == 'UP' and pullback and last['high'] < s1_high:
            sell, sell_sig = 'S2', last['end']

    # ---- 互斥: 买卖点同时出现时, 背驰类信号(B1/B2/S1/S2)优先于结构类(B3/S3);
    #      同类信号保留时间更晚者 ----
    if buy and sell:
        b_beichi = buy in ('B1', 'B2')
        s_beichi = sell in ('S1', 'S2')
        if b_beichi and not s_beichi:
            sell = None
        elif s_beichi and not b_beichi:
            buy, defend = None, 0.0
        elif sell_sig > buy_sig:
            buy, defend = None, 0.0
        else:
            sell = None
    return buy, sell, defend


# ---------------------------------------------------------------- 主入口
def compute(candles, level):
    """缠论主流程: 输入已闭合K线 -> 输出标准 chan_result 字典。

    chan_result = {
      'level': level,
      'bi_list': [{'start','end','dir'}],            # bar 索引
      'seg_list': [{'start','end','dir','high','low'}],
      'zhongshu_list': [{'high','low','start_bar','end_bar'}],
      'trend': 'UP'/'DOWN'/'SIDE',
      'beichi': bool,
      'buy_point': None/'B1'/'B2'/'B3',
      'sell_point': None/'S1'/'S2'/'S3',
      'defend_price': float,
      'details': {...}
    }
    """
    n = len(candles)
    empty = {'level': level, 'bi_list': [], 'seg_list': [], 'zhongshu_list': [],
             'trend': 'SIDE', 'beichi': False, 'buy_point': None,
             'sell_point': None, 'defend_price': 0.0,
             'details': {'macd_params': (MACD_FAST, MACD_SLOW, MACD_SIGNAL),
                         'last_close': candles[-1]['close'] if candles else 0.0,
                         'bars': n}}
    if n < 5:    # 数据太少无法成笔
        return empty

    # 1. 包含处理 -> 标准K线
    merged = merge_containing(candles)
    # 2. 分型
    fractals = find_fractals(merged)
    # 3. 笔 (补充每笔的 high/low = 原始K线区间极值)
    bis = build_bi(fractals)
    for b in bis:
        seg = candles[b['start']:b['end'] + 1]
        b['high'] = max(c['high'] for c in seg)
        b['low'] = min(c['low'] for c in seg)
    # 4. 线段
    segs = build_seg(bis)
    # 5. 中枢
    zs_list = build_zhongshu(bis)
    # 6. MACD + 背驰 (主背驰 + 全中枢事件扫描)
    closes = [c['close'] for c in candles]
    dif, dea, hist = calc_macd(closes)
    beichi = detect_beichi(bis, zs_list, dif, hist)
    events = scan_beichi_events(bis, zs_list, dif, hist)
    # 7. 趋势
    trend = detect_trend(bis, zs_list, closes[-1])
    # 8. 买卖点 + 防守价 (基于背驰事件)
    atr_day = _compute_atr(candles)          # 日线 ATR (供 b1_atr 防守位模式)
    buy_point, sell_point, defend_price = detect_points(bis, zs_list, events,
                                                        candles, atr_day)

    bi_out = [{'start': b['start'], 'end': b['end'], 'dir': b['dir']} for b in bis]
    return {
        'level': level,
        'bi_list': bi_out,
        'seg_list': segs,
        'zhongshu_list': zs_list,
        'trend': trend,
        'beichi': beichi is not None,
        'buy_point': buy_point,
        'sell_point': sell_point,
        'defend_price': defend_price,
        'details': {
            'macd_params': (MACD_FAST, MACD_SLOW, MACD_SIGNAL),
            'last_close': closes[-1],
            'close_hist': closes[-3:],   # 最近3根收盘价 (供共振决策器判断"连续2根收盘跌破"用)
            'bars': n,
            'merged_bars': len(merged),
            'fractal_count': len(fractals),
            'bi_count': len(bis),
            'seg_count': len(segs),
            'zs_count': len(zs_list),
            'beichi_detail': events[-1]['ev'] if events else None,  # 最近背驰事件详情
            'beichi_events': len(events),      # 全历史背驰事件数 (供标定参考)
            'defend_mode': DEFEND_MODE,        # 当前 B1 防守位定义 (阶段2标定)
            'atr_day': round(atr_day, 6),      # 日线 ATR(14) (b1_atr 模式/止损距离统计用)
            'dif_last': dif[-1] if dif else None,
            'dea_last': dea[-1] if dea else None,
            'hist_last': hist[-1] if hist else None,
            'first_time': candles[0]['time'],
            'last_time': candles[-1]['time'],
        },
    }


# ---------------------------------------------------------------- 自测
if __name__ == '__main__':
    import datetime

    BASE = r'C:\Program Files (x86)\Alpari MT4\history\Alpari-Demo'

    def fmt_ts(ts):
        return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime('%Y-%m-%d %H:%M')

    print('=' * 70)
    print('缠论引擎自测 (chan_engine.py)')
    print('=' * 70)
    for fname, lv in [('XAUUSD1440.hst', 'DAY'), ('XAUUSD30.hst', 'M30')]:
        path = os.path.join(BASE, fname)
        candles = load_hst(path)
        print(f'\n--- {fname} ({lv}) bars={len(candles)} '
              f'{fmt_ts(candles[0]["time"])} ~ {fmt_ts(candles[-1]["time"])} ---')
        res = compute(candles, lv)
        d = res['details']
        print(f"  trend={res['trend']}  beichi={res['beichi']}  "
              f"buy_point={res['buy_point']}  sell_point={res['sell_point']}  "
              f"defend_price={res['defend_price']:.2f}")
        print(f"  笔数={d['bi_count']}  线段数={d['seg_count']}  "
              f"中枢数={d['zs_count']}  (合并K线={d['merged_bars']}, 分型={d['fractal_count']})")
        print(f"  last_close={d['last_close']:.2f}  DIF={d['dif_last']:.3f}  "
              f"DEA={d['dea_last']:.3f}  HIST={d['hist_last']:.3f}")
        if res['zhongshu_list']:
            zs = res['zhongshu_list']
            print(f"  中枢区间: " + "; ".join(
                f"[{z['high']:.1f}/{z['low']:.1f}] bar {z['start_bar']}~{z['end_bar']}"
                for z in zs))
        else:
            print('  中枢区间: 无')
        if res['bi_list']:
            print('  最近5笔: ' + ' | '.join(
                f"{b['dir']}({b['start']}->{b['end']})" for b in res['bi_list'][-5:]))
        if d['beichi_detail']:
            bd = d['beichi_detail']
            print(f"  背驰详情: {bd['type']} a_ext={bd['a_ext']:.2f} b_ext={bd['b_ext']:.2f} "
                  f"a_area={bd['a_area']:.3f} b_area={bd['b_area']:.3f}")
        # 输出契约字段完整性检查
        need = ['level', 'bi_list', 'seg_list', 'zhongshu_list', 'trend',
                'beichi', 'buy_point', 'sell_point', 'defend_price', 'details']
        missing = [k for k in need if k not in res]
        print(f"  契约字段检查: {'OK' if not missing else 'MISSING ' + str(missing)}")
    print('\n自测完成 (python chan_engine.py 无报错)')
