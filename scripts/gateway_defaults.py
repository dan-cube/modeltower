"""入口的默认数字（0.6）。入口（gateway.py）和后端（clusterctl.py）共用：后端显示和改限流时要知道默认值，
但不能直接 import gateway（它一导入就去认这台的 oMLX）。只用标准库。"""
LIMITS = {'concurrent': 2, 'per_minute': 60, 'total': 6, 'queue': 8, 'queue_seconds': 60}   # 每把子密钥同时几个、每分钟几个；外来合计几路；排队最多几个、等多久
LOCKOUT = {'tries': 8, 'first': 30, 'again': 300}   # 照 oMLX：同一来源错 8 次锁 30 秒，再错锁 300 秒


def numbers(raw, defaults, top=100000):
    """gateway.json 里的一组数：每项要是 1 到 top 的整数，不是就用默认的（写错一项不影响别的）。"""
    raw = raw if isinstance(raw, dict) else {}
    return {k: (raw[k] if isinstance(raw.get(k), int) and not isinstance(raw.get(k), bool) and 1 <= raw[k] <= top else v) for k, v in defaults.items()}
