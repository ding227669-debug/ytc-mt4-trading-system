# -*- coding: utf-8 -*-
import json, time

# 1. order_result.txt 确切字节
raw = open(r'C:\Program Files (x86)\Alpari MT4\MQL4\Files\order_result.txt', 'rb').read()
print('order_result 字节:', repr(raw))

# 2. 复刻 MQL4 ExtractJsonValue 逻辑
def extract(json_str, key):
    search = '"' + key + '":'
    p = json_str.find(search)
    if p == -1:
        return ''
    p += len(search)
    while p < len(json_str) and (json_str[p] == ' ' or json_str[p] == '"'):
        p += 1
    e = p
    while e < len(json_str):
        c = json_str[e]
        if c == '\\':
            e += 2
            continue
        if c == '"' or c == ',' or c == '}':
            break
        e += 1
    return json_str[p:e]

cmd = {'action': 'PLACE_ORDER', 'symbol': 'BITCOIN', 'operation': 'BUY', 'lots': 0.01,
       'stop_loss': 77900, 'take_profit': 0, 'comment': 'TEST3', 'timestamp': int(time.time() * 1000)}
j = json.dumps(cmd)
print('复刻解析 operation:', repr(extract(j, 'operation')))
print('复刻解析 symbol:', repr(extract(j, 'symbol')))
print('复刻解析 lots:', repr(extract(j, 'lots')))
