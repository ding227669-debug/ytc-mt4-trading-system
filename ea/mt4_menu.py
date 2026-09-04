# -*- coding: utf-8 -*-
"""枚举并操作 MT4 主菜单: 查看 -> 导航器"""
import ctypes, time
from ctypes import wintypes

user32 = ctypes.windll.user32
hwnd = 0x260196

# 激活窗口
user32.SetForegroundWindow(hwnd)
time.sleep(0.3)

class MENUITEMINFO(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.UINT), ('fMask', wintypes.UINT),
                ('fType', wintypes.UINT), ('fState', wintypes.UINT),
                ('wID', wintypes.UINT), ('hSubMenu', wintypes.HMENU),
                ('hbmpChecked', wintypes.HBITMAP), ('hbmpUnchecked', wintypes.HBITMAP),
                ('dwItemData', ctypes.c_ulonglong), ('dwTypeData', wintypes.LPWSTR),
                ('cch', wintypes.UINT), ('hbmpItem', wintypes.HBITMAP)]

menu = user32.GetMenu(hwnd)
if not menu:
    print('无菜单句柄'); raise SystemExit

n = user32.GetMenuItemCount(menu)
print(f'顶层菜单项数: {n}')
items = []
for i in range(n):
    buf = ctypes.create_unicode_buffer(128)
    info = MENUITEMINFO()
    info.cbSize = ctypes.sizeof(MENUITEMINFO)
    info.fMask = 0x00000001 | 0x00000004 | 0x00000080  # MIIM_STRING|MIIM_SUBMENU|MIIM_ID
    info.dwTypeData = ctypes.cast(buf, ctypes.c_wchar_p)
    info.cch = 128
    if user32.GetMenuItemInfoW(menu, i, True, ctypes.byref(info)):
        items.append((i, buf.value, info.hSubMenu))
        print(f'  [{i}] {buf.value} submenu={info.hSubMenu:#x}')

# 找"查看"菜单
view_idx = None
for i, txt, sub in items:
    if '查看' in txt or 'View' in txt:
        view_idx = i
        view_menu = sub
        break
if view_idx is None:
    print('未找到查看菜单'); raise SystemExit
print(f'查看菜单: index={view_idx}')

# 打开查看菜单 (发送 Alt+V)
user32.keybd_event(0x12, 0, 0, 0)
user32.keybd_event(ord('V'), 0, 0, 0)
user32.keybd_event(ord('V'), 0, 2, 0)
user32.keybd_event(0x12, 0, 2, 0)
time.sleep(0.8)

# 枚举查看菜单的子项
vn = user32.GetMenuItemCount(view_menu)
print(f'查看菜单子项数: {vn}')
for j in range(vn):
    buf = ctypes.create_unicode_buffer(128)
    info = MENUITEMINFO()
    info.cbSize = ctypes.sizeof(MENUITEMINFO)
    info.fMask = 0x00000001 | 0x00000080
    info.dwTypeData = ctypes.cast(buf, ctypes.c_wchar_p)
    info.cch = 128
    if user32.GetMenuItemInfoW(view_menu, j, True, ctypes.byref(info)):
        rect = wintypes.RECT()
        user32.GetMenuItemRect(hwnd, view_menu, j, ctypes.byref(rect))
        print(f'  [{j}] {buf.value} rect=({rect.left},{rect.top})-({rect.right},{rect.bottom})')

# 按 Esc 关闭菜单
user32.keybd_event(0x1B, 0, 0, 0)
user32.keybd_event(0x1B, 0, 2, 0)
