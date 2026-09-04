# -*- coding: utf-8 -*-
"""截图 MT4 主窗口"""
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

hwnd = 0x260196  # MT4 主窗口
rect = wintypes.RECT()
user32.GetWindowRect(hwnd, ctypes.byref(rect))
w, h = rect.right - rect.left, rect.bottom - rect.top
print(f'窗口: {w}x{h}')

# 用 PrintWindow 截取
hdc_window = user32.GetWindowDC(hwnd)
hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
hbmp = gdi32.CreateCompatibleBitmap(hdc_window, w, h)
gdi32.SelectObject(hdc_mem, hbmp)
user32.PrintWindow(hwnd, hdc_mem, 2)  # PW_RENDERFULLCONTENT

# 保存为 BMP
class BITMAPFILEHEADER(ctypes.Structure):
    _fields_ = [('bfType', wintypes.WORD), ('bfSize', wintypes.DWORD),
                ('bfReserved1', wintypes.WORD), ('bfReserved2', wintypes.WORD),
                ('bfOffBits', wintypes.DWORD)]
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wintypes.DWORD), ('biWidth', ctypes.c_long), ('biHeight', ctypes.c_long),
                ('biPlanes', wintypes.WORD), ('biBitCount', wintypes.WORD),
                ('biCompression', wintypes.DWORD), ('biSizeImage', wintypes.DWORD),
                ('biXPelsPerMeter', ctypes.c_long), ('biYPelsPerMeter', ctypes.c_long),
                ('biClrUsed', wintypes.DWORD), ('biClrImportant', wintypes.DWORD)]

bmi = BITMAPINFOHEADER()
bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
bmi.biWidth = w
bmi.biHeight = -h  # top-down
bmi.biPlanes = 1
bmi.biBitCount = 32
bmi.biCompression = 0

buf = ctypes.create_string_buffer(w * h * 4)
gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0)

bf = BITMAPFILEHEADER()
bf.bfType = 0x4D42
bf.bfSize = 54 + w * h * 4
bf.bfOffBits = 54
with open(r'C:\Users\Administrator\Documents\Trading\ea\mt4_shot.bmp', 'wb') as f:
    f.write(bf)
    f.write(bmi)
    f.write(buf.raw)

gdi32.DeleteObject(hbmp)
gdi32.DeleteDC(hdc_mem)
user32.ReleaseDC(hwnd, hdc_window)
print('已保存 mt4_shot.bmp')
