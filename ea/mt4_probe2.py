# -*- coding: utf-8 -*-
"""探测 MT4 左侧面板结构: 标签页、市场报价列表、导航器树"""
import time
from pywinauto import Application

app = Application(backend='win32').connect(process=34340)
w = app.top_window()
w.set_focus()

print('=== 左侧控制栏 (市场报价) 的后代控件 ===')
bars = [c for c in w.children() if c.friendly_class_name() == 'AfxControlBar140s']
for b in bars:
    txt = b.window_text()
    if '市场报价' in txt or '导航' in txt:
        print('控制栏:', repr(txt), b.rectangle())
        for d in b.descendants():
            try:
                cls = d.friendly_class_name()
                if cls in ('SysListView32', 'SysTreeView32', 'ListBox', 'Tab', 'Button', 'Edit', 'Toolbar'):
                    print('   ', repr(cls), '|', repr(d.window_text())[:50], '|', d.rectangle())
            except Exception:
                pass

print()
print('=== 尝试菜单: 查看 -> 导航器 (Alt+V) ===')
w.type_keys('%v')
time.sleep(0.8)
# 菜单弹出后按 n (导航器) 或截图菜单项
# 直接尝试键盘: 导航器菜单项通常第一个
w.type_keys('n')
time.sleep(1.0)

print('=== 再找树控件 ===')
trees = w.descendants(control_type='Tree')
for t in trees:
    print('树:', t.window_text(), t.rectangle())
    try:
        print('  根节点:', t.texts()[:15])
    except Exception as e:
        print('  ERR', e)
