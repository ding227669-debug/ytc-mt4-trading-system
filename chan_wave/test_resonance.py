# -*- coding: utf-8 -*-
"""resonance_engine 规则分支合成验证 (直接构造输入 dict, 不依赖行情引擎)。

覆盖: LONG_OPEN(3浪0.6/5浪0.3/B3 0.2) / NONE(各前置过滤、黑名单) /
      REDUCE(5浪末端、S1+顶背驰) / LONG_CLOSE(S2/S3、破止损、wave_broken、转BEAR)
      / 冷却机制。
"""
import sys
import os
import json
import tempfile
sys.path.insert(0, r'C:\Users\Administrator\Documents\Trading\chan_wave')
import resonance_engine as re

# ---- 冷却状态隔离：把持久化文件指到临时文件并重置，避免受/污染真实
#      state/cooldown.json（21 分支测试可重复运行，且不依赖真实冷却状态）----
re.COOLDOWN_FILE = os.path.join(tempfile.gettempdir(),
                                'chan_wave_test_resonance_cooldown.json')
try:
    os.remove(re.COOLDOWN_FILE)
except OSError:
    pass
re._STATE = {'false_count': 0, 'cooldown': False,
             'cool_until': None, 'last_false': None}

# ---- 基础输入构造 ----
def wave(label='3', status='RUNNING', bias='BULL', broken=False):
    return {'wave_label': label, 'wave_status': status, 'bias': bias,
            'wave_broken': broken, 'details': {'atr': 100.0, 'last_close': 1000.0}}

def chan(trend='UP', bp=None, sp=None, beichi=False, defend=0.0, close_hist=None):
    d = {'last_close': (close_hist[-1] if close_hist else 1000.0)}
    if close_hist:
        d['close_hist'] = close_hist
    return {'trend': trend, 'buy_point': bp, 'sell_point': sp, 'beichi': beichi,
            'defend_price': defend, 'details': d}

VOL = {'atr_pct': 5.0, 'abnormal': False}
N = 0
def show(name, res):
    global N
    N += 1
    print('[%02d] %-28s -> %-11s pos=%s stop=%s' %
          (N, name, res['signal'], res['position_rate'], res['stop_loss_price']))
    for c in res['check_list']:
        print('        - ' + c)

# ---- LONG_OPEN 分支 ----
# 1. 3浪主升 + M30 B2 + M5背驰 -> 0.6
show('3浪+B2共振(期望LONG_OPEN 0.6)',
     re.evaluate('T1', wave('3'), chan('UP', 'B2', None, True, 950.0),
                 chan('UP', 'B2', None, True, 950.0),
                 chan('UP', None, None, True), VOL))
# 2. 5浪阶段 + M30 B1 -> 0.3
show('5浪+B1(期望LONG_OPEN 0.3)',
     re.evaluate('T2', wave('5'), chan('UP', 'B1', None, True, 950.0),
                 chan('UP', 'B1', None, True, 950.0),
                 chan('UP', None, None, True), VOL))
# 3. M30 B3 -> 0.2
show('B3买点(期望LONG_OPEN 0.2)',
     re.evaluate('T3', wave('3'), chan('UP', 'B3', None, True, 950.0),
                 chan('UP', 'B3', None, True, 950.0),
                 chan('UP', None, None, True), VOL))

# ---- LONG_OPEN 否决分支 ----
# 4. 日线 trend=DOWN -> NONE
show('日线DOWN(期望NONE)',
     re.evaluate('T4', wave('3'), chan('DOWN', None, None, False),
                 chan('UP', 'B1', None, True, 950.0),
                 chan('UP', None, None, True), VOL))
# 5. M30 无背驰 -> NONE
show('M30无背驰(期望NONE)',
     re.evaluate('T5', wave('3'), chan('UP', None, None, False),
                 chan('UP', 'B1', None, False, 950.0),
                 chan('UP', None, None, True), VOL))
# 6. M5 无背驰(不允许提前预判抄底) -> NONE
show('M5无背驰(期望NONE)',
     re.evaluate('T6', wave('3'), chan('UP', None, None, False),
                 chan('UP', 'B1', None, True, 950.0),
                 chan('UP', None, None, False), VOL))
# 7. M5 强烈顶背驰 S1 -> NONE
show('M5顶背驰S1(期望NONE)',
     re.evaluate('T7', wave('3'), chan('UP', None, None, False),
                 chan('UP', 'B1', None, True, 950.0),
                 chan('UP', None, 'S1', True), VOL))
# 8. 日线顶卖点 S1 -> NONE
show('日线顶卖点S1(期望NONE)',
     re.evaluate('T8', wave('3'), chan('UP', None, 'S1', False),
                 chan('UP', 'B1', None, True, 950.0),
                 chan('UP', None, None, True), VOL))
# 9. 共振冲突: M30 同时有卖点 -> 平仓条件③(S3)优先触发 LONG_CLOSE (先平后开)
show('M30卖点冲突(期望LONG_CLOSE平仓优先)',
     re.evaluate('T9', wave('3'), chan('UP', None, None, False),
                 chan('UP', 'B1', 'S3', True, 950.0),
                 chan('UP', None, None, True), VOL))
# 10. 波浪 UNCERTAIN -> NONE
show('周线UNCERTAIN(期望NONE)',
     re.evaluate('T10', wave('3', 'UNCERTAIN', 'NEUTRAL'),
                 chan('UP', None, None, False),
                 chan('UP', 'B1', None, True, 950.0),
                 chan('UP', None, None, True), VOL))
# 11. 异常波动率 -> NONE
show('异常波动率(期望NONE)',
     re.evaluate('T11', wave('3'), chan('UP', None, None, False),
                 chan('UP', 'B1', None, True, 950.0),
                 chan('UP', None, None, True),
                 {'atr_pct': 5.0, 'abnormal': True}))
# 12. 黑名单: 波浪看多但缠论无任何买点 (单一信号不交易) -> NONE
show('单一信号(期望NONE)',
     re.evaluate('T12', wave('3'), chan('UP', None, None, False),
                 chan('UP', None, None, False),
                 chan('UP', None, None, True), VOL))
# 13. 黑名单: 5浪走完进A浪 -> 平仓条件④(bias=BEAR)优先触发 LONG_CLOSE (先平后开)
show('A浪调整(期望LONG_CLOSE平仓优先)',
     re.evaluate('T13', wave('A', 'RUNNING', 'BEAR'), chan('UP', None, None, False),
                 chan('UP', 'B1', None, True, 950.0),
                 chan('UP', None, None, True), VOL))
# 14. 黑名单: BEAR大C浪 -> 平仓条件④优先触发 LONG_CLOSE (先平后开)
show('大C浪下跌(期望LONG_CLOSE平仓优先)',
     re.evaluate('T14', wave('C', 'RUNNING', 'BEAR'), chan('UP', None, None, False),
                 chan('UP', 'B1', None, True, 950.0),
                 chan('UP', None, None, True), VOL))

# ---- 平仓分支 ----
# 15. REDUCE: 5浪末端量能衰竭 (label=5 COMPLETE)
show('5浪末端(期望REDUCE)',
     re.evaluate('T15', wave('5', 'COMPLETE'), chan('UP', None, None, False),
                 chan('UP', None, None, False),
                 chan('UP', None, None, False), VOL))
# 16. REDUCE: M30 S1 + 顶背驰确认
show('M30 S1+顶背驰(期望REDUCE)',
     re.evaluate('T16', wave('3'), chan('UP', None, None, False),
                 chan('DOWN', None, 'S1', True, 950.0),
                 chan('UP', None, None, False), VOL))
# 17. LONG_CLOSE: M30 S2
show('M30 S2(期望LONG_CLOSE)',
     re.evaluate('T17', wave('3'), chan('UP', None, None, False),
                 chan('DOWN', None, 'S2', False, 950.0),
                 chan('UP', None, None, False), VOL))
# 18. LONG_CLOSE: 连续2根M30收盘<defend_price
show('连续2根破止损(期望LONG_CLOSE)',
     re.evaluate('T18', wave('3'), chan('UP', None, None, False),
                 chan('UP', None, None, False, 950.0, [940.0, 930.0]),
                 chan('UP', None, None, False), VOL))
# 19. LONG_CLOSE: wave_broken
show('浪型破坏(期望LONG_CLOSE)',
     re.evaluate('T19', wave('3', 'RUNNING', 'BULL', True),
                 chan('UP', None, None, False),
                 chan('UP', None, None, False),
                 chan('UP', None, None, False), VOL))
# 20. LONG_CLOSE: 周线转BEAR
show('周线转BEAR(期望LONG_CLOSE)',
     re.evaluate('T20', wave('3', 'RUNNING', 'BEAR'), chan('UP', None, None, False),
                 chan('UP', None, None, False),
                 chan('UP', None, None, False), VOL))

# ---- 冷却机制 ----
re._STATE['false_count'] = 0
re._STATE['cooldown'] = False
for _ in range(3):
    re.mark_result(False)
res = re.evaluate('T21', wave('3'), chan('UP', None, None, False),
                  chan('UP', 'B1', None, True, 950.0),
                  chan('UP', None, None, True), VOL)
show('冷却中(期望NONE冷却)', res)
re.mark_result(True)
print('\n分支验证完成 (test_resonance.py 无报错)')
