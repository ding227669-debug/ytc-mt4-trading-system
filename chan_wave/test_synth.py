# -*- coding: utf-8 -*-
"""chan_engine 买卖点逻辑合成数据验证 (构造典型 a+A+b 缠论结构, 带小幅噪声)"""
import sys
import random
sys.path.insert(0, r'C:\Users\Administrator\Documents\Trading\chan_wave')
from chan_engine import compute

def synth(segments, base=1000.0, seed=7):
    """segments: list of (n根K线, 目标价), 线性插值 + 小幅噪声"""
    rng = random.Random(seed)
    candles, t, price, idx = [], 1000000, base, 0
    for n, target in segments:
        step = (target - price) / n
        for i in range(n):
            o = price
            c = price + step + rng.uniform(-0.0015, 0.0015) * base
            h = max(o, c) * (1 + rng.uniform(0.001, 0.004))
            l = min(o, c) * (1 - rng.uniform(0.001, 0.004))
            candles.append({'time': t + idx * 60, 'open': o, 'high': h,
                            'low': l, 'close': c, 'vol': 100.0})
            idx += 1
            price = c
    return candles

def show(name, res):
    d = res['details']
    bd = d['beichi_detail']
    print(f"[{name}] trend={res['trend']} beichi={res['beichi']} "
          f"buy={res['buy_point']} sell={res['sell_point']} "
          f"defend={res['defend_price']:.1f}")
    print(f"        笔数={d['bi_count']} 中枢数={d['zs_count']} 事件数={d['beichi_events']}")
    if bd:
        print(f"        最近背驰={bd['type']} a_ext={bd['a_ext']:.1f} b_ext={bd['b_ext']:.1f} "
              f"a_area={bd['a_area']:.1f} b_area={bd['b_area']:.1f}")

PRE = [(20, 950), (15, 1000), (15, 960), (20, 1020)]   # 预热: 制造中枢前的早期笔
PRE_S = [(20, 1050), (15, 1000), (15, 1040), (20, 980)]  # 预热(对称, 先跌)

# 场景1 B1: a段大跌(1031->760) -> 中枢[~900/840] -> b段单笔创新低(750) 面积缩小
c1 = synth(PRE + [(60, 760), (50, 900), (50, 840), (50, 920), (60, 750), (25, 800)])
show('B1场景(下跌背驰)', compute(c1, 'M5'))

# 场景2 B2: B1 结构 + 反弹(890) + 回调(810) 不破 B1 低点(750) + 回调后再反弹
# (回调段加长至45根, 保证形成独立回调笔; 回调后接反弹段, 验证"不要求回调笔是最新一笔")
c2 = synth(PRE + [(60, 760), (50, 900), (50, 840), (50, 920), (60, 750),
                  (25, 800), (45, 890), (45, 810), (25, 870)])
show('B2场景(回调不破低)', compute(c2, 'M5'))

# 场景3 B3: 预热 -> 中枢[~1050/980] -> 突破1200 -> 回踩1100(不回中枢)
c3 = synth([(20, 1000), (15, 1050), (15, 1010), (20, 1080),
            (50, 950), (50, 1050), (50, 980),
            (50, 1200), (30, 1100), (25, 1150)])
show('B3场景(突破回踩)', compute(c3, 'M5'))

# 场景4 S1: a段大涨(985->1250) -> 中枢[~1180/1120] -> b段单笔创新高(1300) 面积缩小
c4 = synth(PRE_S + [(60, 1250), (50, 1100), (50, 1180), (50, 1120),
                    (60, 1300), (25, 1270)])
show('S1场景(上涨背驰)', compute(c4, 'M5'))

# 场景5 S3: 预热 -> 中枢[~1000/940] -> 跌破830 -> 反抽900(不回中枢)
c5 = synth([(20, 1000), (15, 950), (15, 990), (20, 920),
            (50, 1020), (50, 940), (50, 1000),
            (50, 830), (30, 900), (25, 860)])
show('S3场景(跌破反抽)', compute(c5, 'M5'))
