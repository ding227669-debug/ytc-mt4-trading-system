# -*- coding: utf-8 -*-
"""枚举 MT4 进程所有顶层窗口"""
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
EnumWindows = user32.EnumWindows
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
IsWindowVisible = user32.IsWindowVisible
GetWindowTextW = user32.GetWindowTextW
GetWindowTextLengthW = user32.GetWindowTextLengthW
GetClassNameW = user32.GetClassNameW

PID = 34340
results = []

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

def callback(hwnd, lparam):
    pid = wintypes.DWORD()
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == PID:
        n = GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        GetWindowTextW(hwnd, buf, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        GetClassNameW(hwnd, cls, 256)
        vis = IsWindowVisible(hwnd)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        results.append((hwnd, cls.value, buf.value, vis, rect.left, rect.top, rect.right, rect.bottom))
    return True

EnumWindows(WNDENUMPROC(callback), 0)
for h, cls, txt, vis, l, t, r, b in results:
    print(f'{h:#x} | {cls} | {txt!r} | vis={vis} | ({l},{t})-({r},{b})')
