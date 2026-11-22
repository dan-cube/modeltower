"""第 3b 期：安装、更新中心、联网的兼容表和通知、下载源（草案 10.5–10.8）。
只做机械的事，不出文字：返回代号和数，说法在 clusterctl.py 里用 tr() 写（中英文表只扫那边）。只用标准库。

联网读的是一份签了名的清单 updates.json（兼容表、控制台最新版、通知合在一份，一天一次只读一个小文件）和它的签名 updates.json.sig
（Ed25519，base64）。公开的那一半在 brand.py；没配置地址或公钥时什么都不读。镜像上的文件被改了、比手上的旧，都不认。
11-14 独立审查后加严：清单逐层核结构（签了名但写错的清单不能把状态弄崩）、issued 只认固定格式、缓存一个文件整体替换、
防回滚另记下限、下载核对 Content-Range、同一个包同时只下一份、自动更新设置的备份原样存字节。"""
import base64
import calendar
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import omlx_adapter
import signing

MANIFEST, SIGNATURE = 'updates.json', 'updates.json.sig'
CACHE = 'manifest-cache.json'      # 本机缓存：清单原文和签名放一个文件，整体替换（分两个文件时进程被杀会留下对不上的一对）
MAX_MANIFEST = 1 << 20             # 清单最多 1 MB
LAUNCH_GAP, DAILY_GAP = 3600, 24 * 3600
STALE_DAYS = 45                    # 联网读到的清单这么久没更新，更新页提示"可能被拦住了"
PREFS_FILE = 'update-prefs.json'   # 官方 App 的更新设置（UpdateController.swift：启动时读一次，只在 App 里改设置时写回）
ISSUED = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', re.ASCII)   # 固定格式，字符串顺序就是时间顺序；用 fullmatch：$ 会放过末尾的换行，\d 不带 ASCII 会认全角数字
NET_ERRORS = (urllib.error.URLError, OSError, ValueError, http.client.HTTPException)


# 联网走系统代理（中国的用户常要代理），用到时才读代理设置。clusterctl 给本机请求装的全局 opener 不走代理（请求里有主密钥），这里另建
_OPENERS = {}


def _open(req, timeout):
    local = urllib.parse.urlsplit(req.full_url).hostname in ('127.0.0.1', 'localhost', '::1')   # 本机地址不走代理：macOS 的代理例外默认不含 127.0.0.1
    if local not in _OPENERS:
        _OPENERS[local] = urllib.request.build_opener(urllib.request.ProxyHandler({} if local else urllib.request.getproxies()))
    return _OPENERS[local].open(req, timeout=timeout)


class Failure(Exception):
    """code 是代号（clusterctl 按它出文字），info 是附带的数。"""
    def __init__(self, code, **info):
        super().__init__(code)
        self.code, self.info = code, info


# ── 版本条件 ──

def _key(text):
    """版本 → 可比的元组；'26'、'15' 这种只有大版本的也认（macOS）。不是字符串的算读不懂。"""
    if not isinstance(text, str): return None
    text = text.strip()
    if text.isdigit(): text += '.0'
    return omlx_adapter.version_key(text)


_OPS = {'>=': lambda a, b: a >= b, '<=': lambda a, b: a <= b, '==': lambda a, b: a == b,
        '!=': lambda a, b: a != b, '>': lambda a, b: a > b, '<': lambda a, b: a < b}


def in_range(version, spec):
    """'>=1.2.0,<1.3' 这样的条件，逗号是"并且"（多余的逗号不算）。spec 为空算满足；版本读不懂、条件写坏了都算不满足（宁可不给人看）。"""
    if not spec: return True
    key = _key(version)
    if key is None or not isinstance(spec, str): return False
    for part in (p.strip() for p in spec.split(',') if p.strip()):
        op = next((o for o in ('>=', '<=', '==', '!=', '>', '<') if part.startswith(o)), '==')
        other = _key(part[len(op):] if part.startswith(op) else part)
        if other is None or not _OPS[op](key, other): return False
    return True


# ── 清单：结构、核签名、缓存 ──

def safe_name(name):
    """安装包的文件名只能是一个纯文件名（不带目录、不以点开头），免得写到下载目录外面。"""
    return isinstance(name, str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,200}', name)) and '..' not in name


def compat_ok(table):
    """兼容表逐层核结构：用到的字段类型都对才算数。随程序发的和联网读到的都用它。"""
    if not isinstance(table, dict) or table.get('schema') != 1 or not isinstance(table.get('versions'), list): return False
    for key in ('minimum', 'recommended', 'tested_max', 'updated'):
        if table.get(key) is not None and not isinstance(table[key], str): return False
    if not isinstance(table.get('bad', []), list) or not all(isinstance(b, dict) and isinstance(b.get('version'), str) for b in table.get('bad', [])): return False
    if table.get('sources') is not None and not isinstance(table['sources'], dict): return False
    for row in table['versions']:
        if not isinstance(row, dict) or not isinstance(row.get('version'), str) or not isinstance(row.get('state'), str): return False
        if not isinstance(row.get('packages', []), list): return False
        for pkg in row.get('packages', []):
            if not isinstance(pkg, dict) or not isinstance(pkg.get('system'), str) or not safe_name(pkg.get('file')): return False
            if not isinstance(pkg.get('size'), int) or isinstance(pkg['size'], bool) or pkg['size'] <= 0: return False
            if not isinstance(pkg.get('sha256'), str) or not re.fullmatch(r'[0-9a-fA-F]{64}', pkg['sha256']): return False
            if pkg.get('url') is not None and not isinstance(pkg['url'], str): return False
            if pkg.get('mirrors') is not None and not (isinstance(pkg['mirrors'], dict) and all(isinstance(v, str) for v in pkg['mirrors'].values())): return False
    return True


def parse_manifest(raw):
    """bytes → dict；格式不对抛 Failure('bad_manifest')。签名对、结构错的清单也不认（不然会让官方版的状态一直崩）。"""
    try: data = json.loads(raw.decode('utf-8'))
    except (ValueError, RecursionError): raise Failure('bad_manifest')
    if not isinstance(data, dict) or data.get('schema') != 1 or not isinstance(data.get('issued'), str) or not ISSUED.fullmatch(data['issued']):
        raise Failure('bad_manifest')
    if data.get('compat') is not None and not compat_ok(data['compat']): raise Failure('bad_manifest')
    console = data.get('console', {})
    if not isinstance(console, dict) or any(console.get(k) is not None and not isinstance(console[k], str) for k in ('version', 'url')): raise Failure('bad_manifest')
    if not isinstance(data.get('notices', []), list): raise Failure('bad_manifest')
    return data


def verify(raw, sig_text, public_hex):
    try: public, sig = bytes.fromhex(public_hex), base64.b64decode(sig_text.strip(), validate=True)
    except (ValueError, TypeError, AttributeError): return False
    return signing.verify(public, raw, sig)


def fetch(url, timeout=20, limit=MAX_MANIFEST):
    """GET 一个小文件，走系统代理。"""
    req = urllib.request.Request(url, headers={'User-Agent': 'local-model-console', 'Cache-Control': 'no-cache'})
    with _open(req, timeout) as r:
        data = r.read(limit + 1)
    if len(data) > limit: raise Failure('too_big')
    return data


def cached(folder, public_hex, floor=None):
    """上次读到、核过签名的那份（每次都重核，免得本机文件被改）；没有或不对返回 None，不抛异常。
    floor 是以前接受过的最新 issued：缓存被换成一份更旧、签名有效的清单时不用它。"""
    if not public_hex: return None
    try:
        box = json.loads((Path(folder) / CACHE).read_bytes())
        raw, sig = base64.b64decode(box['raw'], validate=True), box['sig']
    except Exception:   # 含 RecursionError（几千层嵌套的坏文件）：以前接不住，坏缓存永远换不掉
        return None
    if not verify(raw, sig, public_hex): return None
    try: data = parse_manifest(raw)
    except Exception: return None
    if isinstance(floor, str) and ISSUED.fullmatch(floor) and data['issued'] < floor: return None
    return data


def check_online(base_url, public_hex, folder, floor=None, fetcher=fetch):
    """读一次远端清单，核对签名，比手上的新才换上。floor 是以前接受过的最新 issued（另存在设置里：缓存被删了也不让旧清单回来）。
    返回新清单（dict）；失败抛 Failure：network（连不上）、bad_signature、bad_manifest、rollback（远端的比手上的旧）、too_big。"""
    base = base_url.rstrip('/') + '/'
    try:
        raw = fetcher(base + MANIFEST)
        sig = fetcher(base + SIGNATURE, limit=4096).decode('ascii', 'replace')
    except Failure: raise
    except NET_ERRORS as exc:
        raise Failure('network', detail=str(getattr(exc, 'reason', exc))[:120])
    if not verify(raw, sig, public_hex): raise Failure('bad_signature')
    data = parse_manifest(raw)
    old = cached(folder, public_hex)
    have = max([x for x in ((old or {}).get('issued'), floor) if isinstance(x, str) and ISSUED.fullmatch(x)], default=None)
    if have and data['issued'] < have: raise Failure('rollback', have=have, got=data['issued'])
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / (CACHE + '.tmp')
    tmp.write_text(json.dumps({'raw': base64.b64encode(raw).decode(), 'sig': sig.strip()}))
    os.replace(tmp, folder / CACHE)
    return data


def due(last, mode, now=None):
    """该不该联网：force 立即；launch 距上次一小时以上；平时 24 小时以上。last 是上次读的时间戳（秒），没有、或比现在还晚（时钟校正过）都算该读。"""
    now = time.time() if now is None else now
    if mode == 'force' or not isinstance(last, (int, float)) or last > now: return True
    return now - last >= (LAUNCH_GAP if mode == 'launch' else DAILY_GAP)


def stale_days(manifest, now=None):
    """联网读到的清单发出来多少天了（读不懂返回 None）。"""
    issued = (manifest or {}).get('issued')
    if not isinstance(issued, str) or not ISSUED.fullmatch(issued): return None
    try: then = calendar.timegm(time.strptime(issued, '%Y-%m-%dT%H:%M:%SZ'))
    except (ValueError, OverflowError): return None
    return int(((time.time() if now is None else now) - then) // 86400)


def compat_in_use(bundled, manifest):
    """用哪份兼容表：联网读到的比随程序发的新（updated 日期更晚或相同）就用它，否则用随程序发的。返回 (表, 'online'|'bundled')。
    两份都要结构对；随程序发的写坏了当成没有。"""
    bundled = bundled if compat_ok(bundled) else None
    online = (manifest or {}).get('compat')
    if compat_ok(online) and (bundled is None or str(online.get('updated') or '') >= str(bundled.get('updated') or '')): return online, 'online'
    return bundled, 'bundled'


# ── 状态：控制台、oMLX、通知 ──

def console_state(current, manifest):
    """('update' | 'latest' | 'unknown', 清单里 console 那段)。"""
    info = (manifest or {}).get('console') or {}
    latest = info.get('version')
    if not _key(latest) or not _key(current): return 'unknown', None
    return ('update' if _key(latest) > _key(current) else 'latest'), info


def best_target(table, system=None):
    """建议装、升级到的那版：兼容表写了 recommended 就用它；否则最新的 verified；再没有就最新的 testing、works。
    给了 system 时，那一版要有这台系统的包。没有返回 None。"""
    if not compat_ok(table): return None
    rows = [r for r in table['versions'] if _key(r['version'])]
    if system: rows = [r for r in rows if any(p['system'] == system for p in r.get('packages', []))]
    bad = {b['version'] for b in table.get('bad', [])}
    rows = [r for r in rows if r['version'] not in bad]
    recommended = table.get('recommended')
    pick = next((r for r in rows if r['version'] == recommended and r['state'] in ('verified', 'works', 'testing')), None)
    for states in (('verified',), ('testing', 'works')):
        if pick: break
        pool = [r for r in rows if r['state'] in states]
        pick = max(pool, key=lambda r: _key(r['version'])) if pool else None
    return pick


def omlx_state(kind, version, table, system=None):
    """更新页 oMLX 那一行：(代号, 建议的那版或 None)。代号：managed、none、current、verified_newer、unverified_newer、
    newer_untested、bad、old、unknown（见 CONTRACT.md）。自管版不按兼容表提示（你两台不提示升级）。"""
    if kind == 'managed': return 'managed', None
    target = best_target(table, system)
    if kind != 'official': return 'none', target
    code, _ = omlx_adapter.verdict(version, table)
    if code in ('unknown', 'no_table'): return 'unknown', None
    newer = target if target and _key(target['version']) > _key(version) else None
    if code in ('bad', 'old'): return code, newer or (target if target and target['version'] != version else None)
    if code == 'newer': return 'newer_untested', None
    if newer: return ('verified_newer' if newer.get('state') == 'verified' else 'unverified_newer'), newer
    return 'current', None


def notices(manifest, versions, dismissed=(), today=None):
    """按控制台、oMLX、macOS 版本和做法筛过的通知（已定第 20 条），去掉关掉的、过期的。versions：{'console','omlx','macos','kind'}。"""
    today = today or time.strftime('%Y-%m-%d')
    out = []
    for n in (manifest or {}).get('notices', []):
        if not isinstance(n, dict) or not isinstance(n.get('id'), str) or n['id'] in dismissed: continue
        if isinstance(n.get('expires'), str) and n['expires'] < today: continue
        when = n.get('when') if isinstance(n.get('when'), dict) else {}
        kinds = when.get('kind')
        if kinds and versions.get('kind') not in (kinds if isinstance(kinds, list) else [kinds]): continue
        if all(in_range(versions.get(k), when.get(k)) for k in ('console', 'omlx', 'macos')): out.append(n)
    return out


# ── 安装包：挑来源、下载、核对 ──

def install_package(table, system, version=None):
    """(版本那一条, 这台系统的包)；version 不给时用 best_target。没有返回 (None, None)。"""
    if not compat_ok(table): return None, None
    row = omlx_adapter.entry(table, version) if version else best_target(table, system)
    pkg = next((p for p in (row or {}).get('packages', []) if p['system'] == system), None)
    return (row, pkg) if pkg else (None, None)


def package_sources(pkg, mirror_base=None):
    """[(来源代号, 地址)]：GitHub 是兼容表里写死的地址；魔搭转存按 mirror_base（第 6 期才有）或包里写的 mirrors.modelscope。"""
    out = [('github', pkg['url'])] if pkg.get('url') else []
    ms = (pkg.get('mirrors') or {}).get('modelscope') or (mirror_base.rstrip('/') + '/' + pkg['file'] if mirror_base and pkg.get('file') else None)
    if ms: out.append(('modelscope', ms))
    return out


def probe_speed(url, size=512 * 1024, timeout=8):
    """读前 512 KB 量速度（字节/秒），连不上返回 None。"""
    req = urllib.request.Request(url, headers={'Range': f'bytes=0-{size - 1}', 'User-Agent': 'local-model-console'})
    start = time.monotonic()
    try:
        with _open(req, timeout) as r:
            got = len(r.read(size))
    except NET_ERRORS:
        return None
    return got / max(time.monotonic() - start, 1e-3) if got else None


def choose_source(candidates, pref='auto', probe=probe_speed):
    """挑来源：intl 只用 GitHub；cn 有魔搭就用魔搭；auto 测速挑快的。返回 (代号, 地址, {代号: 速度或 None})；
    一个都连不上抛 Failure('no_source', speeds=…)，让人手选或改用"我已经下载好了"。只有一个候选时不测速。"""
    pool = dict(candidates)
    if pref == 'intl' and 'github' in pool: pool = {'github': pool['github']}
    if pref == 'cn' and 'modelscope' in pool: pool = {'modelscope': pool['modelscope']}
    if not pool: raise Failure('no_source', speeds={})
    if len(pool) == 1:
        name = next(iter(pool))
        return name, pool[name], {}
    speeds = {name: probe(url) for name, url in pool.items()}
    good = {k: v for k, v in speeds.items() if v}
    if not good: raise Failure('no_source', speeds=speeds)
    best = max(good, key=good.get)
    return best, pool[best], speeds


def file_sha256(path, progress=None, chunk=4 << 20):
    h, done = hashlib.sha256(), 0
    with open(path, 'rb') as f:
        total = os.fstat(f.fileno()).st_size
        while True:
            block = f.read(chunk)
            if not block: break
            h.update(block)
            done += len(block)
            if progress: progress(done, total)
    return h.hexdigest()


def _range_start(header):
    found = re.match(r'bytes (\d+)-', header or '')
    return int(found.group(1)) if found else None


def download(url, dest, size, sha256, progress=None, chunk=1 << 20, timeout=30):
    """下到 dest（先写 dest.part，断了下次接着下），边下边算 SHA-256，大小和哈希都对上才改名成 dest。
    dest 已经在而且对得上就不下（返回 'cached'）；接着下的返回 'resumed'，从头下的 'fresh'。同一个包同时只下一份（busy）。
    对不上抛 Failure：size（多了，删掉）、incomplete（少了，留着 .part）、sha256（删掉）、network、busy。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    lock = open(dest.with_name(dest.name + '.lock'), 'w')
    try:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError: raise Failure('busy')
        return _download(url, dest, size, sha256.lower(), progress, chunk, timeout)
    finally:
        lock.close()


def _download(url, dest, size, sha256, progress, chunk, timeout):
    if dest.exists() and dest.stat().st_size == size and file_sha256(dest) == sha256: return 'cached'
    part = dest.with_name(dest.name + '.part')
    have = part.stat().st_size if part.exists() else 0
    if have > size: part.unlink(); have = 0
    h = hashlib.sha256()
    if have:
        with open(part, 'rb') as f:
            for block in iter(lambda: f.read(4 << 20), b''): h.update(block)
    how = 'resumed' if have else 'fresh'
    if have < size:
        req = urllib.request.Request(url, headers={'User-Agent': 'local-model-console', **({'Range': f'bytes={have}-'} if have else {})})
        try:
            with _open(req, timeout) as r:
                start = _range_start(r.headers.get('Content-Range')) if r.status == 206 else 0
                if have and start != have:   # 对方不认断点续传（200），或给的段不是接着的：从它给的开头算
                    if start != 0: raise Failure('network', detail=f'Content-Range {r.headers.get("Content-Range")}', done=have)
                    have, h, how = 0, hashlib.sha256(), 'fresh'
                with open(part, 'ab' if have else 'wb') as f:
                    while have <= size:
                        block = r.read(chunk)
                        if not block: break
                        f.write(block)
                        h.update(block)
                        have += len(block)
                        if progress: progress(have, size)
        except Failure: raise
        except NET_ERRORS as exc:
            raise Failure('network', detail=str(getattr(exc, 'reason', exc))[:120], done=have)
    if have > size:
        part.unlink()
        raise Failure('size', got=have)
    if have < size: raise Failure('incomplete', done=have)
    if h.hexdigest() != sha256:
        part.unlink()
        raise Failure('sha256')
    os.replace(part, dest)
    return how


# ── 官方 App 自己的自动更新（10.5） ──

def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.lmc-tmp')
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _read_backup(backup):
    """备份：{'existed': bool, 'content_b64': 原文的字节}；结构不对返回 None。"""
    try: saved = json.loads(Path(backup).read_bytes())
    except (OSError, ValueError): return None
    if not isinstance(saved, dict) or not isinstance(saved.get('existed'), bool): return None
    if not saved['existed']: return saved
    try: saved['content'] = base64.b64decode(saved.get('content_b64'), validate=True)
    except (ValueError, TypeError): return None
    return saved


def prefs_state(path):
    """官方 App 的 update-prefs.json，照它的读法（backgroundDownload 优先，没有看老的 autoDownload）：没有这个文件时检查和后台下载都开。"""
    try: data = json.loads(Path(path).read_bytes())
    except (OSError, ValueError): data = {}
    if not isinstance(data, dict): data = {}
    return {'autoCheck': data.get('autoCheck', True) is not False, 'backgroundDownload': data.get('backgroundDownload', data.get('autoDownload', True)) is not False}


def backup_ok(backup): return _read_backup(backup) is not None


def prefs_off(path, backup):
    """关掉检查和后台下载，别的字段（频道等）原样留着。第一次关时把原样（字节，或"原来没有这个文件"）备份；
    已有好的备份不覆盖，免得还原成我们关过的；备份坏了就当没有、重新备。"""
    path, backup = Path(path), Path(backup)
    try: original = path.read_bytes()
    except OSError: original = None
    if not backup_ok(backup):
        _write(backup, json.dumps({'existed': original is not None,
                                   'content_b64': base64.b64encode(original).decode() if original is not None else None}).encode())
    try: data = json.loads(original) if original else {}
    except ValueError: data = {}
    if not isinstance(data, dict): data = {}
    data.update(autoCheck=False, backgroundDownload=False)
    if 'autoDownload' in data: data['autoDownload'] = False
    data.setdefault('channel', 'stable')
    data.setdefault('autoNotify', False)
    _write(path, json.dumps(data).encode())


def prefs_restore(path, backup):
    """换回关之前的样子（逐字节；原来没有这个文件就删掉），删掉备份。没有备份抛 no_backup，备份坏了抛 bad_backup（都不碰原文件）。"""
    path, backup = Path(path), Path(backup)
    if not backup.exists(): raise Failure('no_backup')
    saved = _read_backup(backup)
    if saved is None: raise Failure('bad_backup')
    if saved['existed']: _write(path, saved['content'])
    elif path.exists(): path.unlink()
    backup.unlink()


# ── 升级前备份官方的设置（10.6） ──

def backup_settings(settings, folder, keep=5, stamp=None):
    """把官方的 settings.json 复制到 folder/omlx-settings-<时间>.json，权限 600（里面有主密钥，只放本机）。
    内容和已有的某一份一样就不新建（升级失败重试几次，升级前那份不会被轮换掉）；只留最近 keep 份我们备的。"""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    data = Path(settings).read_bytes()
    for old in folder.glob('omlx-settings-*.json'):
        try:
            if old.read_bytes() == data: return old
        except OSError: pass
    target = folder / f"omlx-settings-{stamp or time.strftime('%Y%m%d-%H%M%S')}.json"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f: f.write(data)
    os.chmod(target, 0o600)
    for old in sorted(folder.glob('omlx-settings-*.json'))[:-keep]: old.unlink()
    return target
