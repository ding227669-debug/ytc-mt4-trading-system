# -*- coding: utf-8 -*-
"""Windows popup + sound notification. Non-blocking (run via subprocess.Popen).
Usage: python notify_popup.py "title" "message" [beep_count]
"""
import ctypes, sys, time

title = sys.argv[1] if len(sys.argv) > 1 else 'YTC'
msg = sys.argv[2] if len(sys.argv) > 2 else ''
beeps = int(sys.argv[3]) if len(sys.argv) > 3 else 3

# sound: system exclamation + N loud beeps (higher freq = more audible)
try:
    import winsound
    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    time.sleep(0.2)
    for i in range(beeps):
        winsound.Beep(1500, 350)
        time.sleep(0.2)
except Exception:
    pass

# popup: MB_OK | MB_ICONINFORMATION | MB_TOPMOST | MB_SETFOREGROUND
try:
    ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40 | 0x40000 | 0x10000)
except Exception:
    pass
