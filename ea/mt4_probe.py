# -*- coding: utf-8 -*-
"""探测 MT4 控件结构: 打开导航器/市场报价, 列出树和列表"""
import sys
from pywinauto import Application

app = Application(backend='win32').connect(process=34340)
w = app.top_window()
w.set_focus()

# 打开导航器 (Ctrl+N) 和市场报价 (Ctrl+M)
w.type_keys('^n')
w.type_keys('^m')
import time; time.sleep(1.5)

print('=== 所有顶层子窗口 ===')
for c in w.children():
    try:
        cls = c.friendly_class_name()
        txt = c.window_text()
        if cls in ('AfxControlBar140s', 'SysTreeView32', 'SysListView32', 'ListBox', 'AfxWnd140s', 'Toolbar', 'Button', 'Tab'):
            print(repr(cls), '|', repr(txt)[:60], '|', c.rectangle())
    except Exception as e:
        print('ERR', e)

print()
print('=== 查找树控件 (导航器) ===')
trees = w.descendants(control_type='Tree')
for t in trees:
    print('树:', t.friendly_class_name(), t.window_text(), t.rectangle())
    try:
        items = t.texts()
        print('  节点数:', len(items), items[:20])
    except Exception as e:
        print('  ERR', e)
