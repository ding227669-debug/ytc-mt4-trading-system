# -*- coding: utf-8 -*-
"""YTC trailing stop monitor: raise SL at each +1R milestone.
- price >= open+1R  -> SL to breakeven (open price)
- price >= open+2R  -> SL to open+1R
- price >= open+3R  -> SL to open+2R
- ... (each new +1R locks previous R)
Exits when position disappears (SL hit / closed).
Usage: python monitor_trade.py <ticket> <open_price> <sl_init> <r_size>
"""
import sys, time, json, urllib.request, datetime, os

API = 'http://127.0.0.1:8080'
LOCK = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.modify.lock')


def acquire_lock(timeout=15):
    """文件锁: 防多个 monitor 并发 /api/modify 串读结果 (2026-09-01 修复)"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            time.sleep(0.5)
    return False


def release_lock():
    try:
        os.remove(LOCK)
    except Exception:
        pass


def api_get_positions():
    with urllib.request.urlopen(API + '/api/positions', timeout=10) as r:
        return json.loads(r.read().decode())


def api_modify(ticket, sl=None, tp=None):
    payload = {'ticket': int(ticket)}
    if sl is not None:
        payload['stop_loss'] = sl
    if tp is not None:
        payload['take_profit'] = tp
    got = acquire_lock()
    if not got:
        print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] LOCK TIMEOUT, skip modify', flush=True)
        return {'error': 'lock timeout'}
    try:
        req = urllib.request.Request(API + '/api/modify', data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    finally:
        release_lock()

ticket, open_p, sl, r = int(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] monitor start: ticket={ticket} open={open_p} SL={sl} R={r}', flush=True)

while True:
    try:
        pos = api_get_positions()
        found = None
        for p in pos.get('positions', []):
            if str(p.get('Ticket')) == str(ticket):
                found = p
                break
        if found is None:
            print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] POSITION CLOSED (SL hit or manual close)', flush=True)
            try:
                import subprocess, os
                subprocess.Popen(['python', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'notify_popup.py'),
                                  'YTC 持仓平仓', f'ticket {ticket} 已平仓（止损触发或手动）', '4'])
            except Exception:
                pass
            sys.exit(0)

        cur = float(found['CurrentPrice'])
        cur_sl = float(found['StopLoss'])
        # how many R above open?
        r_above = (cur - open_p) / r
        # target SL: lock previous R milestone
        if r_above >= 3 and cur_sl < open_p + 2 * r - 1:
            new_sl = round(open_p + 2 * r, 2)
            res = api_modify(ticket, sl=new_sl)
            print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] +3R: SL -> {new_sl} {res}', flush=True)
        elif r_above >= 2 and cur_sl < open_p + 1 * r - 1:
            new_sl = round(open_p + 1 * r, 2)
            res = api_modify(ticket, sl=new_sl)
            print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] +2R: SL -> {new_sl} {res}', flush=True)
        elif r_above >= 1 and cur_sl < open_p - 1:
            res = api_modify(ticket, sl=round(open_p, 2))
            print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] +1R: SL -> breakeven {round(open_p,2)} {res}', flush=True)
        else:
            t = int(time.time())
            if t % 300 < 15:
                print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] probe: price={cur} ({r_above:+.1f}R) SL={cur_sl}', flush=True)
    except Exception as e:
        print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] error: {e}', flush=True)
    time.sleep(15)
