"""Ed25519 签名和核验（RFC 8032），只用标准库（草案 10.7：联网读的兼容表和通知要用我们自己的钥匙签名）。
控制台只用 verify 和公开的那一半；sign、public_key 给发布时签名的工具和测试用。纯 Python，核一次几十毫秒，文件都很小，够用。
核验照 RFC 8032 5.1.7：拒绝 s ≥ L、拒绝编码不规范的点。测试用 RFC 8032 第 7.1 节的向量，并和 macOS 自带的 CryptoKit 互相核过。"""
import hashlib

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def _sha512(data): return hashlib.sha512(data).digest()


def _add(p, q):
    """扩展坐标 (X, Y, Z, T) 下的点加（RFC 8032 5.1.4）。"""
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % P
    b = (y1 + x1) * (y2 + x2) % P
    c = 2 * t1 * t2 * D % P
    d = 2 * z1 * z2 % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(scalar, point):
    result = (0, 1, 1, 0)
    while scalar:
        if scalar & 1: result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _same(p, q):
    x1, y1, z1, _ = p
    x2, y2, z2, _ = q
    return (x1 * z2 - x2 * z1) % P == 0 and (y1 * z2 - y2 * z1) % P == 0


def _recover_x(y, sign):
    if y >= P: return None
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    if x2 == 0: return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P: x = x * SQRT_M1 % P
    if (x * x - x2) % P: return None
    if (x & 1) != sign: x = P - x
    return x


_GY = 4 * pow(5, P - 2, P) % P
_GX = _recover_x(_GY, 0)
G = (_GX, _GY, 1, _GX * _GY % P)


def _encode(point):
    x, y, z, _ = point
    zi = pow(z, P - 2, P)
    x, y = x * zi % P, y * zi % P
    return (y | ((x & 1) << 255)).to_bytes(32, 'little')


def _decode(data):
    if len(data) != 32: return None
    y = int.from_bytes(data, 'little')
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


def _expand(seed):
    if not isinstance(seed, (bytes, bytearray)) or len(seed) != 32: raise ValueError('私钥要 32 字节')
    digest = _sha512(bytes(seed))
    a = int.from_bytes(digest[:32], 'little')
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, digest[32:]


def public_key(seed):
    """32 字节私钥 → 32 字节公钥。"""
    return _encode(_mul(_expand(seed)[0], G))


def sign(seed, message):
    """私钥签 message（bytes），返回 64 字节签名。确定性的：同一私钥、同一内容签出来一样。"""
    a, prefix = _expand(seed)
    public = _encode(_mul(a, G))
    r = int.from_bytes(_sha512(prefix + message), 'little') % L
    big_r = _encode(_mul(r, G))
    h = int.from_bytes(_sha512(big_r + public + message), 'little') % L
    return big_r + ((r + h * a) % L).to_bytes(32, 'little')


def verify(public, message, signature):
    """签名对不对。任何格式问题都按不对处理，不抛异常。"""
    try:
        public, message, signature = bytes(public), bytes(message), bytes(signature)
    except TypeError:
        return False
    if len(public) != 32 or len(signature) != 64: return False
    point_a, point_r = _decode(public), _decode(signature[:32])
    if point_a is None or point_r is None: return False
    if _same(_mul(8, point_a), (0, 1, 1, 0)): return False   # 小阶的公钥（如 01 00…00）什么签名都能"核过"，不认
    s = int.from_bytes(signature[32:], 'little')
    if s >= L: return False
    h = int.from_bytes(_sha512(signature[:32] + public + message), 'little') % L
    return _same(_mul(s, G), _add(point_r, _mul(h, point_a)))
