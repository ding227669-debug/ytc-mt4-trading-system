# -*- coding: utf-8 -*-
"""枚举 MT4 工具栏按钮, 查 算法交易(Algo Trading) 按钮状态"""
from pywinauto import Application

app = Application(backend='win32').connect(process=None, title_re='Alpari-Demo')
w = app.top_window()
print('主窗口:', w.window_text())

for tb in w.descendants(control_type='Toolbar'):
    try:
        txt = tb.window_text()
        print(f'\n=== Toolbar: {txt!r} rect={tb.rectangle()} ===')
        btns = tb.buttons() if hasattr(tb, 'buttons') else []
        for i, b in enumerate(btns):
            try:
                name = b.window_text() or b.friendly_class_name()
                state = '?'
                try:
                    state = 'CHECKED' if b.is_checked() else ('pressed' if b.is_pressed() else 'normal')
                except Exception:
                    pass
                print(f'  [{i}] {name!r} rect={b.rectangle()} state={state}')
            except Exception as e:
                print(f'  [{i}] ERR {e}')
    except Exception as e:
        print('Toolbar ERR', e)
