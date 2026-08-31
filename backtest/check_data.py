import struct, os, datetime
base = r'C:\Program Files (x86)\Alpari MT4\history\Alpari-Demo'
for sym in ['XAUUSD', 'XAGUSD', 'WTI', 'BITCOIN']:
    files = [f for f in os.listdir(base) if f.startswith(sym) and f.endswith('.hst')]
    if not files:
        print(sym + ': 无历史文件')
        continue
    for f in sorted(files):
        p = os.path.join(base, f)
        sz = os.path.getsize(p)
        n = (sz - 148) // 60
        if n <= 0:
            print(sym + '/' + f + ': empty')
            continue
        with open(p, 'rb') as fh:
            fh.seek(148)
            t0 = struct.unpack('<q', fh.read(8))[0]
            fh.seek(148 + (n - 1) * 60)
            t1 = struct.unpack('<q', fh.read(8))[0]
        s0 = datetime.datetime.fromtimestamp(t0).strftime('%m-%d')
        s1 = datetime.datetime.fromtimestamp(t1).strftime('%m-%d %H:%M')
        print(sym + '/' + f + ': bars=' + str(n) + ' ' + s0 + ' ~ ' + s1)
