import math, statistics
from datetime import datetime, timezone
from collections import defaultdict

def fmt_size(b: int) -> str:
    try:
        b = int(b)
    except Exception:
        return "неизвестно"
    if b < 1024: return f"{b} B"
    kb = b / 1024
    if kb < 1024: return f"{kb:.1f} KB"
    mb = kb / 1024
    return f"{mb:.2f} MB"

def human_speed(bps: float | None) -> str:
    if bps is None: return "неизвестно"
    if math.isinf(bps): return "inf"
    mb_s = bps / (1024*1024)
    mbit_s = (bps*8) / (1024*1024)
    return f"{mb_s:.6f} MB/s ({mbit_s:.6f} Mbit/s)"

def percentile(arr, q):
    if not arr: return None
    s = sorted(arr)
    k = (len(s) - 1) * q
    f = math.floor(k)
    c = math.ceil(k)
    if f == c: return s[int(k)]
    return s[f]*(c-k) + s[c]*(k-f)

def compute_stats(entries):
    res = {}
    if not entries: return {"sizes":{}, "times":{}, "speeds_bps":{}}
    sizes = [e['bytes'] for e in entries if isinstance(e.get('bytes'), (int,float))]
    times = [e['delivery_seconds'] for e in entries if isinstance(e.get('delivery_seconds'), (int,float))]
    speeds = [e['speed_bps'] for e in entries if isinstance(e.get('speed_bps'), (int,float)) and not math.isinf(e['speed_bps'])]

    def pack(arr):
        if not arr: return {'count': 0}
        med = statistics.median(arr)
        return {
            'count': len(arr),
            'sum': sum(arr),
            'mean': statistics.mean(arr),
            'median': med,
            'min': min(arr),
            'max': max(arr),
            'stdev': statistics.stdev(arr) if len(arr) > 1 else 0.0,
            'mad': statistics.median([abs(x-med) for x in arr]),
            'p25': percentile(arr, 0.25),
            'p75': percentile(arr, 0.75),
        }
    res['sizes'] = pack(sizes); res['times'] = pack(times); res['speeds_bps']=pack(speeds)
    return res

def now_utc():
    return datetime.now(timezone.utc)

def compute_reach_stats(history: dict):
    per_day = defaultdict(set)
    per_hour = defaultdict(set)
    all_users = set()
    for uid, entries in (history or {}).items():
        for e in entries:
            all_users.add(int(uid))
            ts = e.get("timestamp")
            if not ts: continue
            try:
                dt = datetime.fromisoformat(ts)
            except Exception:
                continue
            per_day[dt.date().isoformat()].add(int(uid))
            per_hour[dt.hour].add(int(uid))
    days_sorted = sorted(per_day.items(), key=lambda x: x[0], reverse=True)
    per_day_counts = [(d, len(s)) for d, s in days_sorted]
    per_hour_counts = [(h, len(s)) for h, s in sorted(per_hour.items())]
    return {
        "total_unique_users": len(all_users),
        "per_day_counts": per_day_counts,
        "per_hour_counts": per_hour_counts,
    }