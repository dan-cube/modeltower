"""oMLX 对接（第 3 期，草案 10.1、10.3）：这台的 oMLX 在哪、是哪种装法、端口多少、版本在不在兼容表里。

两种做法，按每台检测的结果选：
- managed（自管）：我们的启动层 serve_omlx.py、运行目录里的 omlx-state、端口 8800、软链 serving-models。主机、副机 是这种，
  认法是运行目录里有 omlx-state/settings.json；行为和第 2 期完全一样，不提示升级。
- official（官方）：官方 App（app）、Homebrew（brew）或别的装法（other，比如 pip）。数据目录按官方的三步找，
  端口读它自己的 settings.json，只走公开接口。
都没有是 none（没装，3b 给安装的三条路）。

这里只给事实和代号，不出界面文字（文字在 clusterctl 里按代号 tr()），不读、不打印主密钥（主密钥由 clusterctl.main_key 按这里给的路径现读）。
只用标准库：gateway.py、rpc.py 也会用。
"""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time

MANAGED_PORT = int(os.environ.get('LMC_OMLX_PORT') or 8800)   # 环境变量只给测试用（和 clusterctl.OMLX_PORT 同一个）
OFFICIAL_PORT = 8000            # 官方默认端口（AppConfig.swift 的默认值）
APP_ID, APP_TEAM = 'app.omlx', 'PSK5Q5T46L'   # 官方 App 的 Bundle ID 和开发团队（摸底 11-13，按 v0.8.0rc1 的 Info.plist 和发行包签名）
APP_REQUIREMENT = f'identifier "{APP_ID}" and anchor apple generic and certificate leaf[subject.OU] = "{APP_TEAM}"'
APP_NAMES = ('oMLX.app',)
BREW_BINS = ('/opt/homebrew/bin/omlx', '/usr/local/bin/omlx')
OPEN = os.environ.get('LMC_OMLX_OPEN') or '/usr/bin/open'   # 环境变量只给测试用：测试里换成 /usr/bin/false，免得把真的 oMLX 应用在主用户里打开
CODE_DIR = Path(__file__).resolve().parent.parent
COMPAT_FILE = CODE_DIR / 'config' / 'engine-compat.json'


class Install(dict):
    """一台 oMLX 的描述：kind、how、base、port、app、signed、control。都是事实，没有密钥，可以随状态报给主机。
    用 dict 是为了能直接 json.dumps；路径存成字符串，用 .path(key) 取 Path。"""
    def path(self, key):
        value = self.get(key)
        return Path(value) if value else None

    @property
    def managed(self): return self.get('kind') == 'managed'

    @property
    def settings(self): return self.path('base') / 'settings.json' if self.get('base') else None

    @property
    def model_settings(self): return self.path('base') / 'model_settings.json' if self.get('base') else None


# ---------- 官方的数据目录（AppConfig.currentBasePath 的三步） ----------

def app_support(home):
    return Path(home) / 'Library' / 'Application Support' / 'oMLX'


def official_base(env=None, home=None):
    """官方 App 和 CLI 认的数据目录：OMLX_BASE_PATH → ~/Library/Application Support/oMLX/base-path 那一行 → ~/.omlx，取第一个。
    只算出路径，不管里面有没有东西；有没有用见 base_ready()。"""
    env = os.environ if env is None else env
    home = Path(home or Path.home())
    custom = (env.get('OMLX_BASE_PATH') or '').strip()
    if custom: return Path(os.path.normpath(os.path.expanduser(custom)))
    try:
        stored = (app_support(home) / 'base-path').read_text(encoding='utf-8').strip()
        if stored: return Path(os.path.normpath(os.path.expanduser(stored)))
    except (OSError, ValueError):   # ValueError：文件不是 UTF-8
        pass
    return home / '.omlx'


def base_ready(base):
    """数据目录里有官方的 settings.json 才算数。主机 的 ~/.omlx 只有 bin/omlx-cluster-python 和 cluster/（oMLX 分布式推理留下的，
    官方 App 里也带同一个 omlx-cluster-python），没有 settings.json，不当成官方数据目录（摸底 11-13，11-13 拆包核对）。"""
    return bool(base) and (Path(base) / 'settings.json').is_file()


def leftover_base(base):
    """数据目录里只有分布式推理留下的东西、没有 settings.json（给检测说明用，不影响判断：判断只看 settings.json）。"""
    base = Path(base)
    return not base_ready(base) and ((base / 'bin' / 'omlx-cluster-python').exists() or (base / 'cluster' / 'runtime').exists())


def other_install_alive(base, env=None):
    """只找到数据目录、没有 App 也没有 Homebrew 版时，是不是真有"别的装法"的 oMLX：它的端口有人在听，或 PATH 里有 omlx 命令。
    都没有，多半是 App 被拖进废纸篓、设置文件留下了（体检 11-15）。"""
    env = os.environ if env is None else env
    import shutil
    if shutil.which('omlx', path=env.get('PATH') or os.defpath): return True
    try:
        with socket.create_connection(('127.0.0.1', official_port(base, env)), timeout=0.3): return True
    except OSError:
        return False


def read_server(base):
    """官方 settings.json 里的 server 段：{'host':…, 'port':…}；读不到返回 {}。只取 server 段，不碰 auth（主密钥在 auth 里）。"""
    try: data = json.loads((Path(base) / 'settings.json').read_text(encoding='utf-8'))
    except (OSError, ValueError): return {}
    server = data.get('server') if isinstance(data, dict) else None
    return {k: server[k] for k in ('host', 'port') if isinstance(server, dict) and k in server}


def official_port(base, env=None):
    """官方 App 的取法：settings.json 的 server.port，环境变量 OMLX_PORT 最后覆盖（AppConfig.load），都没有用 8000。"""
    env = os.environ if env is None else env
    port = read_server(base).get('port') if base else None
    if str(env.get('OMLX_PORT') or '').isdigit(): port = int(env['OMLX_PORT'])
    return port if isinstance(port, int) and 0 < port < 65536 else OFFICIAL_PORT


# ---------- 装在哪 ----------

def app_candidates(home, env=None):
    env = os.environ if env is None else env
    if env.get('LMC_OMLX_APP'): return [Path(env['LMC_OMLX_APP'])]   # 只给测试用：工作目录里取出来的 App（草案 10.9 b）
    return [root / name for root in (Path('/Applications'), Path(home) / 'Applications') for name in APP_NAMES]


def app_signed(app, run=subprocess.run):
    """codesign 核对是不是官方签的（Bundle ID 加开发团队），包里的文件有没有被改。True 是，False 不是或被改过，None 核对不了（没有 codesign、超时）。
    不加 --strict：App 放在 iCloud 同步的文件夹里会被加上 Finder 信息，--strict 会误报（11-13 实测：在开了同步的桌面上报 detritus，
    挪出来就过）；签名和资源封印不加它也照样核。"""
    try:
        r = run(['/usr/bin/codesign', '--verify', '--deep', '-R=' + APP_REQUIREMENT, str(app)], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.returncode == 0


def app_version(app):
    """App 包里写的版本（Info.plist 的 CFBundleShortVersionString），读不到返回 None。不代表正在跑的版本，正在跑的看 /api/status。"""
    import plistlib
    try:
        with open(Path(app) / 'Contents' / 'Info.plist', 'rb') as f: info = plistlib.load(f)
    except Exception:   # 坏的 XML 抛的是 ExpatError，不是 ValueError
        return None
    if not isinstance(info, dict): return None
    value = info.get('CFBundleShortVersionString')
    return value if isinstance(value, str) else None


def brew_bin():
    """Homebrew 装的 omlx：PATH 里那个命令指向 Cellar/omlx 才算（别的装法也可能叫 omlx）。"""
    for candidate in BREW_BINS:
        p = Path(candidate)
        try:
            if p.exists() and '/Cellar/omlx/' in str(p.resolve()): return p
        except OSError:
            continue
    return None


def detect(root, env=None, home=None, run=subprocess.run, check_signature=True):
    """这台的 oMLX。root 是控制台的运行目录（自管版的 omlx-state 在它下面）。
    先认自管：运行目录里有 omlx-state/settings.json 就是 managed，不再往下找（你两台装了官方 App 也照旧按自管走）。"""
    env = os.environ if env is None else env
    home = Path(home or Path.home())
    state = Path(root) / 'omlx-state'
    if (state / 'settings.json').is_file():
        return Install(kind='managed', how='managed', base=str(state), port=MANAGED_PORT, app=None, signed=None, control=None)
    base = official_base(env, home)
    ready = base_ready(base)
    app = next((a for a in app_candidates(home, env) if (a / 'Contents' / 'Info.plist').is_file()), None)
    brew = None if app else brew_bin()
    if not (app or brew) and ready and not other_install_alive(base, env): ready = False   # 只剩设置文件：按没装算，能重新装，装回来接着用这份设置
    if not (app or brew or ready):
        return Install(kind='none', how=None, base=str(base) if ready else None, port=None, app=None, signed=None, control=None,
                       leftover=leftover_base(base) or base_ready(base))
    info = Install(kind='official', how='app' if app else 'brew' if brew else 'other', base=str(base) if ready else None,
                   port=official_port(base if ready else None, env), app=str(app) if app else None, brew=str(brew) if brew else None,
                   signed=(app_signed(app, run) if check_signature else None) if app else None,
                   control=str(app_support(home) / 'control.sock') if app else None, leftover=leftover_base(base))
    if app: info['app_version'] = app_version(app)
    return info


# ---------- 启停：官方 App 走它的控制接口，Homebrew 走 omlx start/stop ----------

def listening(port, host='127.0.0.1', timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout): return True
    except OSError:
        return False


def control(sock_path, command, timeout=35):
    """给官方 App 发一行 {"command": start|stop|restart|status}，读回一行 JSON（AppControlServer.swift）。
    App 没开时没有这个文件或连不上，返回 None；回复读不懂返回 {'ok': False, 'status': 'unreadable'}。App 那边最多等 30 秒。"""
    if command not in ('start', 'stop', 'restart', 'status'): raise ValueError(command)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(sock_path))
            s.sendall((json.dumps({'command': command}) + '\n').encode())
            data = b''
            while b'\n' not in data and len(data) < 65536:
                chunk = s.recv(4096)
                if not chunk: break
                data += chunk
    except (OSError, socket.timeout):
        return None
    try:
        reply = json.loads(data.split(b'\n', 1)[0] or b'null')
    except ValueError:
        return {'ok': False, 'status': 'unreadable'}
    return reply if isinstance(reply, dict) else {'ok': False, 'status': 'unreadable'}


def start(info, run=subprocess.run, opener=None, wait=20, sleep=time.sleep):
    """开官方 oMLX 的服务，不加载模型。返回代号：
    'sent'（App 收到了，服务在起）、'running'（本来就在跑）、'opened'（App 没开，已打开它，控制接口 wait 秒内还没出来；它默认一打开就起服务）、
    'port_conflict'（端口被别的进程占着，App 不会去抢）、'unsupported'（装法管不了启停）、'failed'（附 message）。"""
    how = info.get('how')
    if how == 'app':
        reply = control(info['control'], 'start') if info.get('control') else None
        if reply is None:
            r = (opener or run)([OPEN, '-g', info['app']], capture_output=True, text=True, timeout=30)
            if r.returncode: return ('failed', (r.stderr or '').strip()[:200])
            for _ in range(int(wait * 2)):   # 用户关了"打开就启动"的，等控制接口出来再发一次 start（重复发无害：在跑就回 running）
                sleep(0.5)
                reply = control(info['control'], 'start') if info.get('control') else None
                if reply is not None: break
            if reply is None: return ('opened', '')
        status = reply.get('status')
        if status in ('starting', 'running', 'ok'): return ('running' if status == 'running' else 'sent', '')
        if status == 'port_conflict': return ('port_conflict', str(reply.get('message') or '')[:200])
        return ('failed', str(reply.get('message') or status or '')[:200])
    if how == 'brew':
        r = run([info['brew'], 'start'], capture_output=True, text=True, timeout=60, start_new_session=True)   # 它起的服务不跟着这个后端进程的进程组走：界面上强制结束这次操作时不连服务一起停
        return ('sent', '') if r.returncode == 0 else ('failed', ((r.stderr or '') + (r.stdout or '')).strip()[:200])
    return ('unsupported', '')


def stop(info, run=subprocess.run):
    """停官方 oMLX 的服务（App 留在菜单栏）。不直接结束进程：App 会把崩掉的服务拉起来（摸底：5、10、20 秒，最多 3 次）。
    返回 'sent' | 'not_running' | 'unsupported' | 'failed'。"""
    how = info.get('how')
    if how == 'app':
        reply = control(info['control'], 'stop') if info.get('control') else None
        if reply is None: return ('not_running', '')   # App 没开：它管的服务也不在
        return ('sent', '') if reply.get('ok') else ('failed', str(reply.get('message') or reply.get('status') or '')[:200])
    if how == 'brew':
        r = run([info['brew'], 'stop'], capture_output=True, text=True, timeout=60)
        return ('sent', '') if r.returncode == 0 else ('failed', ((r.stderr or '') + (r.stdout or '')).strip()[:200])
    return ('unsupported', '')


# ---------- 兼容表（3.3 第 1、3 条；10.3） ----------

_STAGES = {'dev': 0, 'a': 1, 'b': 2, 'rc': 3, None: 4, 'post': 5}
VERSION = re.compile(r'^v?(\d+)\.(\d+)(?:\.(\d+))?(?:[.-]?(dev|a|b|rc|post)(\d*))?$')


def version_key(text):
    """'0.8.0rc1' → (0, 8, 0, 3, 1)；读不懂返回 None。顺序按 PEP 440：dev < a < b < rc < 正式 < post。"""
    found = VERSION.match((text or '').strip())
    if not found: return None
    major, minor, patch, stage, n = found.groups()
    return (int(major), int(minor), int(patch or 0), _STAGES[stage], int(n or 0))


def is_prerelease(text):
    """测试版（dev、a、b、rc）：更新页说"测试版，请升级到正式版"，不说"太旧"（0.5.1 A2）。读不懂的不算。"""
    key = version_key(text)
    return bool(key) and key[3] < _STAGES[None]


def load_compat(path=COMPAT_FILE):
    """随控制台发的兼容表；读不到或格式不对返回 None（调用方按"读不到兼容表"说，不当成没问题）。"""
    try: data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError): return None
    if not isinstance(data, dict) or data.get('schema') != 1 or not isinstance(data.get('versions'), list): return None
    return data


def same_version(a, b):
    """两个版本号是不是同一版：都读得懂就按数值比（'v0.8.1' 和 '0.8.1' 一样），读不懂的按原文比。"""
    ka, kb = version_key(a), version_key(b)
    return a == b if ka is None or kb is None else ka == kb


def entry(table, version):
    return next((v for v in (table or {}).get('versions', []) if same_version(v.get('version'), version)), None)


def verdict(version, table):
    """装的版本按兼容表落在哪一档（3.3 第 3 条）。返回 (代号, 兼容表里那一条或 None)：
    'verified' 已验证；'works' 能用但有缺（附 note）；'testing' 我们正在测；'bad' 已知有问题（附 instead）；'old' 低于最低版本；
    'newer' 比已测的最高版本新，还没验证；'unlisted' 在范围里但兼容表没列；'unknown' 版本读不到或读不懂；'no_table' 没有兼容表。"""
    if table is None: return ('no_table', None)
    key = version_key(version)
    if key is None: return ('unknown', None)
    bad = next((b for b in table.get('bad', []) if same_version(b.get('version'), version)), None)
    if bad: return ('bad', bad)
    row = entry(table, version)
    if row and row.get('state') in ('verified', 'works', 'testing'): return (row['state'], row)
    if row and row.get('state') == 'old': return ('old', row)   # 列着但标了太旧（0.7.3：11-14 定不测）
    minimum, top = version_key(table.get('minimum')), version_key(table.get('tested_max'))
    if minimum and key < minimum: return ('old', row)
    if top and key > top: return ('newer', row)
    return ('unlisted', row)


def package(table, sha256):
    """按 SHA-256 找安装包：(版本那一条, 包那一条)，不在表里返回 (None, None)。同名文件被替换过也认得出来（0.8.0rc1 的教训）。"""
    sha256 = (sha256 or '').lower()
    for row in (table or {}).get('versions', []):
        for pkg in row.get('packages', []):
            if pkg.get('sha256', '').lower() == sha256: return row, pkg
    return None, None


def system_slot(macos_version):
    """macOS 版本 → 兼容表里的包位：15 → 'macos15'，26 及以上 → 'macos26'，别的 None（控制台要 macOS 14 起，oMLX 官方包要 15 起）。"""
    key = version_key(macos_version)
    if not key: return None
    return 'macos15' if key[0] == 15 else 'macos26' if key[0] >= 16 else None   # 16–25：旧 SDK 构建的程序在 macOS 26 上读到的兼容值


if __name__ == '__main__':   # 给人看：python omlx_adapter.py [运行目录]，只打印事实，没有密钥
    import sys
    from paths import RUNTIME
    print(json.dumps(detect(sys.argv[1] if len(sys.argv) > 1 else RUNTIME), ensure_ascii=False, indent=2))
