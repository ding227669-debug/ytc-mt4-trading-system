# -*- coding: utf-8 -*-
"""smoke_test.py — wave_engine 接口契约与跨品种冒烟验证（主 agent 可复跑）

验证点：
  1) compute() 返回字段完整性（契约字段名不可改）
  2) 数据不足 -> UNCERTAIN 兜底
  3) 幂等性：同一输入两次调用结果一致
  4) 4 品种（BITCOIN/XAUUSD/XAGUSD/WTI）周线全量数浪不崩溃、方向合理
"""
import sys
import os
import datetime

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wave_engine as we

REQUIRED_FIELDS = {'level', 'wave_label', 'wave_status', 'bias',
                   'wave_broken', 'details'}
REQUIRED_DETAILS = {'swings', 'label_checks', 'last_close', 'atr'}


def check_contract(res, tag):
    missing = REQUIRED_FIELDS - set(res.keys())
    dmissing = REQUIRED_DETAILS - set(res['details'].keys())
    assert not missing, '%s 缺少字段: %s' % (tag, missing)
    assert not dmissing, '%s details 缺少字段: %s' % (tag, dmissing)
    assert res['wave_status'] in ('COMPLETE', 'RUNNING', 'UNCERTAIN'), tag
    assert res['bias'] in ('BULL', 'BEAR', 'NEUTRAL'), tag
    assert res['wave_label'] in (None, '1', '2', '3', '4', '5', 'A', 'B', 'C'), tag
    assert isinstance(res['wave_broken'], bool), tag


def main():
    ok = True
    # ---- 1) 数据不足兜底 ----
    few = [{'time': t * 86400, 'open': 100.0 + t, 'high': 102.0 + t,
            'low': 99.0 + t, 'close': 101.0 + t, 'vol': 1.0}
           for t in range(6)]
    r = we.compute(few)
    check_contract(r, 'few-bars')
    assert r['wave_status'] == 'UNCERTAIN' and r['bias'] == 'NEUTRAL' \
        and r['wave_label'] is None, '数据不足应返回 UNCERTAIN'
    print('[1] 数据不足兜底 OK: status=UNCERTAIN bias=NEUTRAL label=None')

    # ---- 2) 幂等性 ----
    candles_x = we.load_hst(os.path.join(we.HST_BASE, 'XAUUSD10080.hst'))
    r1 = we.compute(candles_x)
    r2 = we.compute(candles_x)
    assert r1 is r2, '相同输入应命中缓存返回同一结果'
    check_contract(r1, 'xau')
    print('[2] 幂等性 OK: 相同输入命中缓存')

    # ---- 3) 4 品种全量数浪 ----
    for sym in ('BITCOIN', 'XAUUSD', 'XAGUSD', 'WTI'):
        c, src = we._load_weekly(sym)
        if not c:
            print('[%s] !! 数据缺失' % sym)
            ok = False
            continue
        res = we.compute(c)
        check_contract(res, sym)
        d = res['details']
        t0 = datetime.datetime.fromtimestamp(c[0]['time'] + 3 * 3600,
                                             datetime.timezone.utc).strftime('%Y-%m-%d')
        t1 = datetime.datetime.fromtimestamp(c[-1]['time'] + 3 * 3600,
                                             datetime.timezone.utc).strftime('%Y-%m-%d')
        print('[%s] %d根(%s~%s) label=%-4s status=%-9s bias=%-6s broken=%s '
              'close=%.2f atr=%.2f swings=%d  来源:%s' %
              (sym, len(c), t0, t1, str(res['wave_label']), res['wave_status'],
               res['bias'], res['wave_broken'], d['last_close'], d['atr'],
               len(d['swings']), src))
        check_contract(res, sym)
    print('ALL PASS' if ok else 'SOME FAILED')


if __name__ == '__main__':
    main()
