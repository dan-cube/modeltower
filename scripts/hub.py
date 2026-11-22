#!/usr/bin/env python3
"""主机角色（用户 11-03 定）：一台主机总控，主机是可移交的角色，节点各自能独立运行。

没有 hub.json 时这里什么都不做，行为和原来一样。启用后：

· 每个节点只授权一把"主机钥匙"（`~/.ssh/lmc/hub_ed25519`）。移交是把这把钥匙和机器表交给新主机，
  节点的 authorized_keys 不用改（D1、副机 红线不受影响），加节点也只点一次允许。
· 任期号防止两台同时当主机：主机每条指令都带 @任期，节点只认当前或更新的任期，旧主机发不了指令。
· 最高密码：统一一个，只在主机本机设置和修改。这里只存校验值和用它加密的主机钥匙，不存明文；
  走标准输入，不进命令行和日志。Claude 不碰。
· 能当主机的机器有名单，只有它们存着加密的钥匙。副机不能自封：没有最高密码打不开。
· 安全芯片（chip.py）：主机、任期、名单、密码这些变化，都要一个已登记钥匙的签名，每台按自己的登记表核对。
  只有任期只增这一条防重放，所以每次变化都涨任期。主机钥匙在手也签不出这个，这条补上了"被攻破的旧主机拦不住"。
· 名单、授权、密码这些"动信任"的操作不在 rpc 白名单里，远程调不到，只能在主机本机做。

主机钥匙泄露（比如怀疑旧主机被攻破）时，任期号拦不住持有钥匙的人：要每台重新配对，换一把钥匙。
"""
from i18n import tr
import base64
import fcntl
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time

import chip
from paths import CLUSTER_FILE, RUN_DIR, RUNTIME as ROOT, control_path

HUB_FILE = ROOT / 'hub.json'
SECRET_FILE = ROOT / 'hub-secret.json'
APPLY_FILE = RUN_DIR / 'hub-apply.json'
ATTEMPTS_FILE = RUN_DIR / 'hub-attempts.json'
PENDING_SECRET = RUN_DIR / 'hub-secret-pending.json'
PENDING_MACHINES = ROOT / 'hub-pending-machines.json'
GATEWAY_PID = RUN_DIR / 'gateway-pid.json'
LMC_SSH = Path.home() / '.ssh/lmc'
HUB_KEY = LMC_SSH / 'hub_ed25519'
PASSWORD_INFO = ROOT / 'hub-password.json'
USED_PROOFS = RUN_DIR / 'hub-reset-used.json'
PROOF_DIR = Path('/var/run')   # 只有 root 能写：证明刚过了 macOS 管理员验证
PROOF_OWNER, PROOF_TTL = 0, 180
APPLY_TTL, MAX_FAILS, LOCK_SECONDS, MIN_PASSWORD = 600, 5, 600, 8
SCRYPT = {'n': 2**15, 'r': 8, 'p': 1, 'maxmem': 64 * 2**20, 'dklen': 96}


class HubError(RuntimeError): pass


def load():
    """没有文件才算没启用。读不懂要报错：当成没启用，下一条指令就走"第一次加入"，什么都不查。"""
    try: return json.loads(HUB_FILE.read_text())
    except FileNotFoundError: return None
    except (OSError, ValueError): raise HubError(tr('{0} 读不懂，先停下。到这台的 设置 → 安全 退出主机模式，重新配对。', HUB_FILE.name))


def save_private(path, text):
    """写到旁边再改名，0600。临时名每次不同，两个进程同时写不会互相截断；落盘后才改名，断电不会留下空文件。"""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save(data): save_private(HUB_FILE, json.dumps(data, indent=2, ensure_ascii=False))


# ---------- 最高密码 ----------

def _derive(password, salt):
    out = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return out[:32], out[32:64], out[64:]   # 校验值、加密钥匙、认证钥匙


def _stream(key, nonce, size):
    out, block = bytearray(), 0
    while len(out) < size:
        out += hmac.new(key, nonce + block.to_bytes(8, 'big'), hashlib.sha256).digest()
        block += 1
    return bytes(out[:size])


def seal(password, plain):
    """用 scrypt 派生钥匙，HMAC-SHA256 计数器模式加密，再算一次认证码（先加密后认证）。只用标准库，不经过命令行。"""
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(16)
    check, enc, mac = _derive(password, salt)
    cipher = bytes(a ^ b for a, b in zip(plain, _stream(enc, nonce, len(plain))))
    tag = hmac.new(mac, salt + nonce + cipher, hashlib.sha256).digest()
    pack = lambda b: base64.b64encode(b).decode()
    return {'v': 1, 'salt': pack(salt), 'nonce': pack(nonce), 'cipher': pack(cipher), 'tag': pack(tag), 'check': pack(check)}


def unseal(password, blob):
    """密码不对返回 None。"""
    salt, nonce, cipher, tag, check = (base64.b64decode(blob[k]) for k in ('salt', 'nonce', 'cipher', 'tag', 'check'))
    good, enc, mac = _derive(password, salt)
    if not hmac.compare_digest(good, check): return None
    if not hmac.compare_digest(hmac.new(mac, salt + nonce + cipher, hashlib.sha256).digest(), tag): raise HubError(tr('加密的主机钥匙损坏了。'))
    return bytes(a ^ b for a, b in zip(cipher, _stream(enc, nonce, len(cipher))))


def _gate():
    try: info = json.loads(ATTEMPTS_FILE.read_text())
    except (OSError, ValueError): info = {'fails': 0, 'since': 0}
    if info['fails'] >= MAX_FAILS and time.time() - info['since'] < LOCK_SECONDS:
        raise HubError(tr('最高密码连错 {0} 次，已锁定，约 {1} 分钟后再试。', MAX_FAILS, (int((LOCK_SECONDS - (time.time() - info['since'])) / 60) + 1)))
    return info


def check_password(password):
    """对了返回主机钥匙的明文；错了记一次，连错就锁。整段占一把文件锁：同时来的几次尝试一个个过，每次都记数（体检 11-15）。"""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with open(ATTEMPTS_FILE.with_name(ATTEMPTS_FILE.name + '.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _check_password(password)


def _check_password(password):
    info = _gate()
    try: blob = json.loads(SECRET_FILE.read_text())
    except (OSError, ValueError): raise HubError(tr('这台没有存加密的主机钥匙：它不在可当主机的名单里，或者还没设最高密码。'))
    plain = unseal(password, blob)
    if plain is None:
        info = {'fails': info['fails'] + 1, 'since': time.time()}
        save_private(ATTEMPTS_FILE, json.dumps(info))
        note('最高密码错误')
        raise HubError(tr('最高密码不对（连错 {0} 次会锁定 {1} 分钟）。', MAX_FAILS, (LOCK_SECONDS // 60)))
    ATTEMPTS_FILE.unlink(missing_ok=True)
    return plain


def check_strength(new):
    if len(new) < MIN_PASSWORD: raise HubError(tr('最高密码至少 {0} 位。', MIN_PASSWORD))
    if new.isdigit() or len(set(new)) < 4: raise HubError(tr('最高密码太简单：不要全是数字，也不要只用几个重复的字符。'))


def record_change(me):
    """记什么时候在哪台改过，设置页显示；别人改了你能看出来。"""
    save_private(PASSWORD_INFO, json.dumps({'at': time.strftime('%Y-%m-%d %H:%M'), 'where': me}))


def password_view():
    try: info = json.loads(PASSWORD_INFO.read_text())
    except (OSError, ValueError): info = {}
    return {'password_set': SECRET_FILE.exists(), 'password_at': info.get('at'), 'password_where': info.get('where')}


def check_reset_proof(nonce):
    """忘了密码：应用先在系统的管理员密码框里建一个只有 root 能写的凭证，一次一用。Agent 过不了那个密码框，
    走不通这条正规的路；它要是直接改文件，那是账号隔离的事（见设置页的说明）。"""
    if not re.fullmatch(r'[0-9a-f]{16,64}', nonce): raise HubError(tr('没有通过 macOS 管理员验证。'))
    try: stat = (PROOF_DIR / f'lmc-reset-{nonce}').stat()
    except OSError: raise HubError(tr('没有通过 macOS 管理员验证。'))
    try: used = json.loads(USED_PROOFS.read_text())
    except (OSError, ValueError): used = []
    if stat.st_uid != PROOF_OWNER or time.time() - stat.st_mtime > PROOF_TTL or nonce in used: raise HubError(tr('没有通过 macOS 管理员验证。'))
    save_private(USED_PROOFS, json.dumps((used + [nonce])[-50:]))


def read_password(prompt=tr('最高密码：')):
    """应用从标准输入传（一行）；终端里不回显。绝不从参数读，参数会出现在进程列表里。"""
    if sys.stdin.isatty(): return getpass.getpass(prompt)
    return sys.stdin.readline().rstrip('\n')


def protected():
    """这台开了最高密码没有。主机模式一定设过；关机这类操作没开就不查（用户 11-04 定：局域网自用，默认不用提前设置）。"""
    return bool(SECRET_FILE.exists() or load())


def check_if_set(password):
    if protected(): check_password(password)


def read_password_if_set(prompt=tr('最高密码：')):
    """应用总是传这一行，没设时是空行；终端里没设就不问。"""
    if sys.stdin.isatty() and not protected(): return ''
    return read_password(prompt)


def read_proof():
    """安全芯片的确认：应用在密码后面再传一行。终端里没有，命令行签不了。"""
    return '' if sys.stdin.isatty() else sys.stdin.readline().strip()


def split_input(data):
    """rpc 的标准输入：第一行密码，第二行确认。"""
    password, _, rest = data.partition('\n')
    return password, rest.partition('\n')[0].strip()


def note(text):
    """每次用到最高权限都记一笔，只记动作，不记密码。"""
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        with (RUN_DIR / 'hub.log').open('a') as f: f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError: pass


# ---------- 和别的机器说话：只用主机钥匙 ----------

def peer_call(meta, request, term, data=None, timeout=30):
    target = f"{meta['user']}@{meta['ip']}"
    args = ['ssh', '-F', 'none', '-i', str(HUB_KEY), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3',
            '-o', f"UserKnownHostsFile={LMC_SSH / 'known_hosts'}", '-o', 'StrictHostKeyChecking=yes', '-p', str(meta.get('ssh_port', 22)), target, f'@{term} {request}']
    try: r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=data or '')
    except subprocess.TimeoutExpired: return None
    if r.returncode:
        if r.returncode == 6: raise HubError(tr('{0} 不认这个任期：主机已经换了。', meta.get('title', meta['ip'])))
        return None
    try: return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError): return None


def peers(data, me): return {n: m for n, m in data['machines'].items() if n != me}


def public_state(data):
    return {**{k: data[k] for k in ('cluster', 'term', 'hub', 'candidates', 'machines')}, 'secret': data.get('secret'), 'proof': data.get('proof')}


TRUST = ('cluster', 'term', 'hub', 'candidates', 'secret')
DISPLAY = ('title', 'dsh_title')   # 只有显示名能不经签名改
SAFE_TITLE = r'''[^"'\\`$\x00-\x1f\x7f\x85\u2028\u2029]{1,64}'''
MACHINE_TEXT = {'title': SAFE_TITLE, 'dsh_title': SAFE_TITLE, 'link': SAFE_TITLE, 'ip': r'[A-Za-z0-9][A-Za-z0-9.-]{0,63}', 'user': r'[A-Za-z0-9_][A-Za-z0-9._-]{0,63}',
                'interface': r'[a-z0-9]{1,16}', 'dsh_provider': r'[A-Za-z0-9_-]{1,64}', 'host_key': r'ssh-ed25519 [A-Za-z0-9+/=]{20,200}'}
NODE_NAME = r'[a-z0-9][a-z0-9_-]{0,31}'
LEFT_FILE = ROOT / 'hub-left.json'
PORTS_FILE = ROOT / 'hub-ports.json'


def check_machines(machines):
    """机器表会写进 ~/.ssh/lmc/config、命令行和 cluster.json。不校验的话，带换行的用户名能塞进 ProxyCommand，
    以 - 开头的能被 ssh 当成选项，都是在这台跑任意命令；带引号的名字会让机器表读不出来，这台就变砖了。"""
    if not isinstance(machines, dict) or not machines or len(machines) > 64: raise HubError(tr('机器表不对。'))
    for name, meta in machines.items():
        if not re.fullmatch(NODE_NAME, name) or name == 'all' or not isinstance(meta, dict) or 'ip' not in meta or 'user' not in meta or 'title' not in meta: raise HubError(tr('机器表不对。'))
        for key, value in meta.items():
            if key in ('ssh_port', 'omlx_port'):
                if type(value) is not int or not 0 < value < 65536: raise HubError(tr('机器表不对。'))
            elif key not in MACHINE_TEXT or not isinstance(value, str) or not re.fullmatch(MACHINE_TEXT[key], value): raise HubError(tr('机器表不对。'))


def check_shape(me, state):
    """别的机器发来的状态，写法不对的一律不要。任期必须是整数：5.0 等于 5 能混过比较，存下来之后这台的指令都发不出去。"""
    if not isinstance(state, dict) or type(state.get('term')) is not int or state['term'] < 1 or not re.fullmatch(r'[0-9a-z]{1,64}', str(state.get('cluster'))): raise HubError(tr('状态的写法不对。'))
    check_machines(state.get('machines'))
    machines, candidates = state['machines'], state.get('candidates')
    if not isinstance(state.get('hub'), str) or state['hub'] not in machines or me not in machines: raise HubError(tr('主机名字不对。'))
    if not isinstance(candidates, list) or not all(isinstance(n, str) and n in machines for n in candidates) or len(set(candidates)) != len(candidates): raise HubError(tr('名单不对。'))
    if state.get('secret') is not None and not re.fullmatch(r'[0-9a-f]{64}', str(state['secret'])): raise HubError(tr('状态的写法不对。'))
    if state.get('proof') is not None and (not isinstance(state['proof'], str) or len(state['proof']) > 2 * chip.MAX_BODY): raise HubError(tr('状态的写法不对。'))


def machines_id(machines):
    """机器表里除显示名以外的部分：连到哪、用谁登录。这部分进签名，不能不经确认就改。"""
    rows = {name: {k: v for k, v in meta.items() if k not in DISPLAY} for name, meta in (machines or {}).items()}
    return hashlib.sha256(chip.canonical(rows).encode()).hexdigest()


def trust_view(data): return {**{k: data.get(k) for k in TRUST}, 'machines': machines_id(data.get('machines'))}
def state_text(data): return chip.canonical({'op': 'state', **trust_view(data)})
def secret_sha(text): return hashlib.sha256(text.encode()).hexdigest()


def file_secret():
    try: return secret_sha(SECRET_FILE.read_text())
    except OSError: return None


def next_state(data, **changes):
    """任何动信任的变化都涨任期：离线的机器靠"任期只增"防别人重放旧签名。"""
    return {**data, 'term': data['term'] + 1, 'secret': data.get('secret') or file_secret(), **changes}


def next_state_text(me, what, rest):
    """这次要签的内容，交给应用；后面真做的时候按同样的规则再算一遍，必须一字不差。"""
    data = load()
    if not data: raise HubError(tr('还没有启用主机模式。'))
    if what == 'handover' and len(rest) == 1: return state_text(next_state(data, hub=rest[0]))
    if what == 'takeover' and not rest: return state_text(next_state(data, hub=me))
    if what == 'candidates' and rest: return state_text(next_state(data, candidates=sorted({*rest, me})))
    if what == 'machine-add' and len(rest) == 1: return state_text(with_machine(data, rest[0]))
    if what == 'machine-remove' and len(rest) == 1: return state_text(without_machine(data, rest[0]))
    if what == 'resign' and not rest: return state_text(next_state(data))
    raise HubError(tr('不支持这个操作。'))


# ---------- 加减机器：机器表进了签名，这也是最高操作 ----------

def pending_machines():
    """添加到一半的机器：记下要连哪里，还没进机器表，也没有任何机器信它。"""
    try: rows = json.loads(PENDING_MACHINES.read_text())
    except (OSError, ValueError): return {}
    return rows if isinstance(rows, dict) else {}


def remember_machine(me, name, row):
    require_hub(me)
    check_machines({name: row})
    save_private(PENDING_MACHINES, json.dumps({**pending_machines(), name: row}, ensure_ascii=False))


def forget_machine(name):
    rows = pending_machines()
    rows.pop(name, None)
    save_private(PENDING_MACHINES, json.dumps(rows, ensure_ascii=False))


def with_machine(data, name):
    """问对方自己叫什么，用这个名字进机器表：各台按自己的名字认表，界面起的名字对方不认，
    它就进不了集群，给它的确认也对不上。连不通就到此为止，不用白按一次指纹。"""
    row = pending_machines().get(name)
    if not row: raise HubError(tr('还没有添加 {0}：先在机器页生成配对串。', name))
    check_machines({name: row})
    try: info = peer_call(row, 'ping', data['term'])
    except HubError: raise HubError(tr('{0} 还记着别的主机：到那台的 设置 → 安全 退出主机模式，再来添加。什么都没改。', row['title']))
    if not isinstance(info, dict) or not isinstance(info.get('node'), str): raise HubError(tr('连不通 {0}：对方还没有允许，或者地址不对。什么都没改。', row['title']))
    node = info['node']
    if not re.fullmatch(NODE_NAME, node) or node == 'all': raise HubError(tr('{0} 的名字不合规。什么都没改。', row['title']))
    if node in data['machines']: raise HubError(tr('{0} 自称 {1}，和机器表里的一台重名，不能添加。什么都没改。', row['title'], node))
    return next_state(data, machines={**data['machines'], node: row})


def without_machine(data, name):
    if name not in data['machines']: raise HubError(tr('机器表里没有 {0}。', name))
    if name == data['hub']: raise HubError(tr('主机不能把自己移除：先移交给别的机器。'))
    return next_state(data, machines={n: m for n, m in data['machines'].items() if n != name}, candidates=[c for c in data['candidates'] if c != name])


def add_machine(me, name, password, proof=''):
    """对方允许后：先核对签名和密码，再试连，通了才进机器表，然后告诉各台。"""
    data = require_hub(me)
    new = with_machine(data, name)
    new['proof'] = proof or None
    chip.check_state(proof, state_text(new))
    check_password(password)
    save(new)
    adopt(new, me)
    forget_machine(name)
    note(f'把 {name} 加进机器表，任期 {new["term"]}')
    return push_state(new, me)


def resign(me, password, proof=''):
    """重新签一次：换过钥匙、吊销过钥匙后，旧签名在别的机器上过不了。内容不变，任期涨一，各台就都跟上了。"""
    data = require_hub(me)
    new = next_state(data, proof=proof or None)
    chip.check_state(proof, state_text(new))
    check_password(password)
    save(new)
    note(f'重新签了一次，任期 {new["term"]}')
    return push_state(new, me)


def needs_resign(data):
    """这份状态的签名，按现在的登记表还过不过得了。过不了要重新签一次，不然落后的机器一直跟不上。"""
    try: chip.check_state(data.get('proof'), state_text(data))
    except (chip.ChipError, ValueError, TypeError): return True
    return False


def remove_machine(me, name, password, proof=''):
    """各台不再认它。它那边的授权和加密钥匙要到那台退出主机模式才清掉：没有远程命令能动授权。"""
    data = require_hub(me)
    new = without_machine(data, name)
    new['proof'] = proof or None
    chip.check_state(proof, state_text(new))
    check_password(password)
    save(new)
    adopt(new, me)
    note(f'把 {name} 移出机器表，任期 {new["term"]}')
    return push_state(new, me)


def init_text(me, machines, candidates, cluster):
    """启用时要签的内容。集群编号每次随机，由这份内容带着：退出再启用后，旧的签名就对不上了。"""
    return state_text({'cluster': cluster, 'term': 1, 'hub': me, 'candidates': sorted({*candidates, me}), 'secret': file_secret(), 'machines': machines})


def push_chip(meta, term):
    """先把登记发过去，不然对方可能还不认识新钥匙。失败不算错：下面的指令对方自己会拒绝，之后 sync 再补。"""
    if not chip.seq(): return
    try: peer_call(meta, 'chip-apply', term, json.dumps({'updates': chip.log()}, ensure_ascii=False))
    except HubError: pass


def push_state(data, me):
    """把现在的主机和任期告诉各台，返回没通知到的机器。"""
    missed = []
    for name, meta in peers(data, me).items():
        push_chip(meta, data['term'])
        if peer_call(meta, 'hub-accept', data['term'], json.dumps(public_state(data), ensure_ascii=False)) is None: missed.append(name)
    return missed


# ---------- 这台该看到哪些机器 ----------

def tunnel_port(name):
    """本机连这台用的端口，按机器固定，永不复用。按序号算的话，移除一台后后面的都错位，流量会接到别的机器上。"""
    try: ports = json.loads(PORTS_FILE.read_text())
    except (OSError, ValueError): ports = {}
    if not isinstance(ports.get(name), int):
        ports[name] = max([8810, *(v for v in ports.values() if isinstance(v, int))]) + 10
        save_private(PORTS_FILE, json.dumps(ports, ensure_ascii=False))
    return ports[name]


def adopt(data, me):
    """按现在谁是主机写这台的机器表：主机看到所有机器，经主机钥匙走 rpc；副机只看到自己，它归主机管。
    改之前留一份旧的，回滚时用。"""
    rows = []
    is_hub = data['hub'] == me
    try: before = {r['name']: r for r in json.loads(CLUSTER_FILE.read_text())['nodes']}
    except (OSError, ValueError, KeyError, TypeError): before = {}
    for name, meta in data['machines'].items():
        if name != me and not is_hub: continue
        row = {'name': name, 'title': meta.get('title', name), 'role': '主机' if name == data['hub'] else '副机', 'kind': 'hub' if name == data['hub'] else 'worker'}
        if meta.get('interface') and meta.get('link'): row.update(ip=meta['ip'], interface=meta['interface'], link=meta['link'])
        if meta.get('dsh_provider'): row['dsh_provider'] = meta['dsh_provider']
        if meta.get('dsh_title'): row['dsh_title'] = meta['dsh_title']
        if name == me: row['port'] = meta.get('omlx_port', 8800)
        else: row.update(port=tunnel_port(name), ssh=f'lmc-{name}', transport='rpc')
        # 这台登记过几个地址、.local 名：主机记的地址还在里面就留着（主机状态里还没有这两项，第 4 期再并进去）
        old = before.get(name) or {}
        if old.get('addresses') and meta.get('ip') in [a.get('ip') for a in old['addresses'] if isinstance(a, dict)]: row['addresses'] = old['addresses']
        if old.get('mdns'): row['mdns'] = old['mdns']
        rows.append(row)
    if CLUSTER_FILE.exists():
        backup = CLUSTER_FILE.with_name(f"cluster.json.bak-{data['term']}")
        if not backup.exists(): backup.write_text(CLUSTER_FILE.read_text())
    save_private(CLUSTER_FILE, json.dumps({'nodes': rows}, indent=2, ensure_ascii=False))
    os.chmod(CLUSTER_FILE, 0o644)
    if is_hub: write_ssh_config(data, me)


def write_known_hosts(data, me):
    """按签名的机器表写主机指纹：没接过移交的备用机，接管后也认得各台。"""
    path = LMC_SSH / 'known_hosts'
    LMC_SSH.mkdir(parents=True, exist_ok=True)
    known = {line.split(' ', 1)[0]: line for line in (path.read_text().splitlines() if path.exists() else []) if ' ' in line}
    for name, meta in peers(data, me).items():
        if not meta.get('host_key'): continue
        port = meta.get('ssh_port', 22)
        host = meta['ip'] if port == 22 else f"[{meta['ip']}]:{port}"
        known[host] = f"{host} {meta['host_key']}"
    if known: save_private(path, '\n'.join(known.values()) + '\n')


def write_ssh_config(data, me):
    """和 clusterctl 的 save_lmc_hosts 同样的写法，只是都用主机钥匙。"""
    check_machines(data['machines'])
    LMC_SSH.mkdir(parents=True, exist_ok=True)
    os.chmod(LMC_SSH, 0o700)
    write_known_hosts(data, me)
    text, hosts = tr('# ModelTower生成，手改会被覆盖\n'), {}
    for name, m in peers(data, me).items():
        hosts[name] = {'ip': m['ip'], 'user': m['user'], 'ssh_port': m.get('ssh_port', 22)}
        text += (f'Host lmc-{name}\n  HostName {m["ip"]}\n  User {m["user"]}\n  Port {m.get("ssh_port", 22)}\n  IdentityFile "{HUB_KEY}"\n'
                 f'  IdentitiesOnly yes\n  UserKnownHostsFile "{LMC_SSH / "known_hosts"}"\n  StrictHostKeyChecking yes\n  BatchMode yes\n'
                 f'  ConnectTimeout 3\n  ServerAliveInterval 5\n  ServerAliveCountMax 3\n  ExitOnForwardFailure yes\n'
                 f'  ControlPath "{control_path(name)}"\n')
    (LMC_SSH / 'hosts.json').write_text(json.dumps(hosts, indent=2, ensure_ascii=False))
    (LMC_SSH / 'config').write_text(text)


# ---------- 入口跟着主机走 ----------

LAST_GATEWAY = []   # 这个进程里刚起的入口（Popen）：ensure 看它是不是马上退了，gateway_pid 看它是不是已经退了

def gateway_setting():
    """gateway.json 里写的 enabled：True、False；没有文件、没写、读不懂都是 None（没表过态）。"""
    try: value = json.loads((ROOT / 'gateway.json').read_text()).get('enabled')
    except (OSError, ValueError, AttributeError): return None
    return value if isinstance(value, bool) else None


def gateway_enabled(kind=None):
    """开不开入口。写了就照写的；没表过态时按这台的 oMLX 定（已定第 29 条 ②）：官方版默认开（它的子密钥能点起没加载的模型），
    自管版（你两台）和没装照旧不开。kind 是调用方已经认出来的做法，不给就现认一次（只看几个文件）。"""
    setting = gateway_setting()
    if setting is not None: return setting
    if kind is None:
        import omlx_adapter
        kind = omlx_adapter.detect(ROOT, check_signature=False)['kind']
    return kind == 'official'


def gateway_follows_omlx():
    """没开主机模式：入口由这台 oMLX 的启停带着（单机的官方版）。开了主机模式照旧由主机的流程管，这里不插手。"""
    try: return load() is None
    except HubError: return False


def gateway_pid():
    try: pid = json.loads(GATEWAY_PID.read_text())['pid']
    except (OSError, ValueError, KeyError): return None
    mine = next((p for p in LAST_GATEWAY if p.pid == pid), None)
    if mine is not None and mine.poll() is not None: return None   # 这个进程自己起的、已经退了：没收尸前 kill 0 还当它活着
    try: os.kill(pid, 0)
    except OSError: return None
    # 记下的号可能已经被别的程序用了（运行目录开机后还在）：认不出是入口就当没有，免得把信号发给别人
    try: command = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError): return pid
    return pid if 'gateway.py' in command else None


def start_gateway(kind=None):
    """入口开着（gateway_enabled）而且没在跑才起。查和起之间加锁：控制台和终端同时起时只起一个。"""
    if not gateway_enabled(kind): return False
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with (RUN_DIR / 'gateway-start.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if gateway_pid(): return False
        log = (RUN_DIR / 'gateway-control.log').open('ab')
        p = subprocess.Popen([sys.executable, '-B', str(Path(__file__).with_name('gateway.py'))], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True, env={**os.environ, 'PYTHONPATH': str(Path(__file__).parent)})
        GATEWAY_PID.write_text(json.dumps({'pid': p.pid}))
        LAST_GATEWAY[:] = [p]
    return True


def stop_gateway(drain=False):
    pid = gateway_pid()
    if not pid: return False
    try: os.kill(pid, signal.SIGUSR1 if drain else signal.SIGTERM)
    except OSError: return False   # 刚好自己退了
    return True


# ---------- 启用 ----------

def init(me, machines, candidates, password, proof=''):
    """在这台启用主机模式：生成主机钥匙，这台当主机。之后到每台亲手加一行（hub line）。"""
    if load(): raise HubError(tr('已经启用了。'))
    check_strength(password)
    if me not in machines: raise HubError(tr('机器表里没有这台。'))
    bad = [c for c in candidates if c not in machines]
    if bad: raise HubError(tr('这些机器不在机器表里：{0}', tr('、').join(bad)))
    check_machines(machines)
    cluster = secrets.token_hex(8)
    if proof or chip.required():
        if not SECRET_FILE.exists(): raise HubError(tr('先到 设置 → 安全 设最高密码。'))
        try: cluster = str(json.loads(json.loads(proof)['payload'])['cluster'])
        except (ValueError, KeyError, TypeError): raise HubError(chip.NEED_PROOF)
        chip.check_state(proof, init_text(me, machines, candidates, cluster))
    LMC_SSH.mkdir(parents=True, exist_ok=True)
    os.chmod(LMC_SSH, 0o700)
    if SECRET_FILE.exists():
        # 已经在设置里设过最高密码：钥匙那时生成好了，这里只核对
        plain = check_password(password)
        if not HUB_KEY.exists(): save_private(HUB_KEY, plain.decode())
    else:
        make_hub_key()
        save_private(SECRET_FILE, json.dumps(seal(password, HUB_KEY.read_bytes())))
        record_change(me)
    data = {'cluster': cluster, 'term': 1, 'hub': me, 'candidates': sorted({*candidates, me}), 'machines': machines, 'secret': file_secret(), 'proof': proof or None}
    save(data)
    adopt(data, me)
    note(f'启用主机模式，主机 {me}')
    return data


def make_hub_key():
    if HUB_KEY.exists(): raise HubError(tr('{0} 已经存在，先确认是不是别的集群留下的。', HUB_KEY))
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'lmc-hub', '-f', str(HUB_KEY)], check=True, capture_output=True)


def setup_password(me, new):
    """第一次设最高密码，可以在启用主机模式之前（用户 11-04 定：在本机设置里设）。主机钥匙这时生成，还没授权给任何机器。"""
    admin_here(me)
    if SECRET_FILE.exists(): raise HubError(tr('已经设过了。要改就用修改，忘了就用忘记密码。'))
    check_strength(new)
    LMC_SSH.mkdir(parents=True, exist_ok=True)
    os.chmod(LMC_SSH, 0o700)
    make_hub_key()
    save_private(SECRET_FILE, json.dumps(seal(new, HUB_KEY.read_bytes())))
    record_change(me)
    note('设了最高密码')


def admin_here(me):
    """这台能不能动最高密码：没启用主机模式时都能，启用后只有主机本机能。"""
    data = load()
    if data and data['hub'] != me: raise HubError(tr('这台不是主机，主机是 {0}。这个操作只能在主机本机做。', data['machines'][data['hub']].get('title', data['hub'])))
    return data


def rename(me, node, title, dsh_title):
    data = require_hub(me)
    if node not in data['machines']: raise HubError(tr('机器表里没有这台。'))
    data['machines'][node].update(title=title, dsh_title=dsh_title)
    save(data)
    adopt(data, me)
    note(f'{node} 改名为 {title}')
    return push_state(data, me)


def public_key():
    """从私钥算，不读 .pub：接任的主机只收到私钥，留下的旧 .pub 也可能不是这把。"""
    r = subprocess.run(['/usr/bin/ssh-keygen', '-y', '-f', str(HUB_KEY)], capture_output=True, text=True)
    if r.returncode: raise HubError(tr('这台没有主机钥匙。'))
    return ' '.join(r.stdout.split()[:2]) + ' lmc-hub'


def join_all(me):
    """把状态告诉各台，把加密的钥匙发给名单里的机器。各台要先亲手加好 hub line。"""
    data = require_hub(me)
    missed = push_state(data, me)
    secret = SECRET_FILE.read_text()
    for name in data['candidates']:
        if name != me and name not in missed:
            if peer_call(data['machines'][name], 'hub-secret-store', data['term'], secret) is None: missed.append(name)
    return missed


def require_hub(me):
    data = load()
    if not data: raise HubError(tr('还没有启用主机模式。'))
    if data['hub'] != me: raise HubError(tr('这台不是主机，主机是 {0}。这个操作只能在主机本机做。', data['machines'][data['hub']].get('title', data['hub'])))
    return data


def publish_secret(me, data, proof=None):
    """这台的加密钥匙换了：涨任期、告诉各台新文件的指纹，再把文件发给名单里的机器。对方核对指纹，发错了存不进去。"""
    if not data: return []
    new = next_state(data, secret=file_secret(), proof=proof)
    save(new)
    missed = push_state(new, me)
    return sorted({*missed, *(n for n in new['candidates'] if n != me and n not in missed and peer_call(new['machines'][n], 'hub-secret-store', new['term'], SECRET_FILE.read_text()) is None)})


def set_password(me, old, new):
    data = admin_here(me)
    if chip.required(): raise HubError(tr('改最高密码要安全芯片确认：到控制台的 设置 → 安全 里改。'))
    check_strength(new)
    if old == new: raise HubError(tr('新密码和旧密码一样。'))
    plain = check_password(old)
    save_private(SECRET_FILE, json.dumps(seal(new, plain)))
    record_change(me)
    note('改了最高密码')
    return publish_secret(me, data)


def password_text(me, data, sealed):
    """改密码要签的内容：绑定新文件的指纹，签的是这一份，换一份就不算。"""
    digest = secret_sha(sealed)
    if data: return state_text(next_state(data, secret=digest))
    return chip.canonical({'op': 'password', 'machine': me, 'secret': digest})


def prepare_password(me, old, new):
    """改密码分两步：先验旧密码、封好新文件，再拿去签名，commit 时才换。"""
    data = admin_here(me)
    check_strength(new)
    if old == new: raise HubError(tr('新密码和旧密码一样。'))
    sealed = json.dumps(seal(new, check_password(old)))
    save_private(PENDING_SECRET, sealed)
    return password_text(me, data, sealed)


def prepare_reset(me, new):
    """忘了密码：macOS 管理员验证由调用方先做（check_reset_proof）。主机钥匙在这台，所以不用旧密码。"""
    data = admin_here(me)
    check_strength(new)
    if not HUB_KEY.exists(): raise HubError(tr('这台没有主机钥匙，重设不了。主机也没了就要每台重新配对。'))
    sealed = json.dumps(seal(new, HUB_KEY.read_bytes()))
    save_private(PENDING_SECRET, sealed)
    return password_text(me, data, sealed)


def commit_password(me, proof):
    data = admin_here(me)
    try: sealed = PENDING_SECRET.read_text()
    except OSError: raise HubError(tr('没有等着确认的新密码，再做一次。'))
    chip.check_state(proof, password_text(me, data, sealed))
    save_private(SECRET_FILE, sealed)
    PENDING_SECRET.unlink(missing_ok=True)
    ATTEMPTS_FILE.unlink(missing_ok=True)
    record_change(me)
    note('改了最高密码（安全芯片确认）')
    return publish_secret(me, data, proof)


def reset_password(me, new):
    """忘了密码：主机还在就能重设，主机钥匙在这台本来就有。macOS 管理员验证由调用方先做（check_reset_proof 或 sudo）。"""
    data = admin_here(me)
    if chip.required(): raise HubError(tr('重设最高密码要安全芯片确认：到控制台的 设置 → 安全 里改。'))
    check_strength(new)
    if not HUB_KEY.exists(): raise HubError(tr('这台没有主机钥匙，重设不了。主机也没了就要每台重新配对。'))
    save_private(SECRET_FILE, json.dumps(seal(new, HUB_KEY.read_bytes())))
    ATTEMPTS_FILE.unlink(missing_ok=True)
    record_change(me)
    note('重设了最高密码')
    return publish_secret(me, data)


def set_candidates(me, candidates, password, proof=''):
    data = require_hub(me)
    bad = [c for c in candidates if c not in data['machines']]
    if bad: raise HubError(tr('这些机器不在机器表里：{0}', tr('、').join(bad)))
    new = sorted({*candidates, me})
    removed = [c for c in data['candidates'] if c not in new]
    data = next_state(data, candidates=new, proof=proof or None)
    chip.check_state(proof, state_text(data))   # 先验签名再验密码：不然任何钥匙发 5 次错密码，就能把最高密码锁 10 分钟
    check_password(password)
    save(data)
    note('改了可当主机的名单：' + tr('、').join(new))
    missed = push_state(data, me)
    for name in new:
        if name != me and peer_call(data['machines'][name], 'hub-secret-store', data['term'], SECRET_FILE.read_text()) is None: missed.append(name)
    for name in removed:
        if peer_call(data['machines'][name], 'hub-secret-drop', data['term']) is None: missed.append(name)
    return sorted(set(missed))


# ---------- 移交 ----------

def handover(me, target, password, known_hosts='', proof=''):
    """主机在线时换主机：先让新主机接好，再通知各台，最后这台退下来。任何一步没成，这台还是主机。"""
    data = require_hub(me)
    if target == me: raise HubError(tr('这台已经是主机了。'))
    if target not in data['candidates']: raise HubError(tr('{0} 不在可当主机的名单里。先在主机上把它加进名单。', target))
    new = next_state(data, hub=target, proof=proof or None)
    chip.check_state(proof, state_text(new))
    plain = check_password(password)
    meta = data['machines'][target]
    state = peer_call(meta, 'hub-state', data['term'])
    if state is None: raise HubError(tr('连不上 {0}，这台还是主机，什么都没改。', meta.get('title', target)))
    push_chip(meta, data['term'])
    package = {'state': public_state(new), 'key': base64.b64encode(plain).decode(), 'secret': SECRET_FILE.read_text(), 'known_hosts': known_hosts or known_hosts_text()}
    if peer_call(meta, 'hub-receive', data['term'], json.dumps(package, ensure_ascii=False), timeout=60) is None:
        raise HubError(tr('{0} 没接上，这台还是主机，什么都没改。', meta.get('title', target)))
    save(new)
    note(f'主机移交给 {target}，任期 {new["term"]}')
    missed = []
    for name, meta in peers(new, target).items():
        if name == me: continue
        push_chip(meta, new['term'])
        if peer_call(meta, 'hub-accept', new['term'], json.dumps(public_state(new), ensure_ascii=False)) is None: missed.append(name)
    step_down()
    adopt(new, me)
    return missed


def known_hosts_text():
    path = LMC_SSH / 'known_hosts'
    return path.read_text() if path.exists() else ''


def step_down():
    """不再是主机：删掉钥匙明文，入口让已有的请求答完再关。"""
    HUB_KEY.unlink(missing_ok=True)
    HUB_KEY.with_name(HUB_KEY.name + '.pub').unlink(missing_ok=True)
    stop_gateway(drain=True)
    note('这台不再是主机')


def apply(me):
    data = load()
    if not data: raise HubError(tr('还没有启用主机模式。'))
    if data['hub'] == me: raise HubError(tr('这台已经是主机了。'))
    save_private(APPLY_FILE, json.dumps({'who': me, 'at': time.time()}))
    note('申请当主机')


def pending_apply():
    try: info = json.loads(APPLY_FILE.read_text())
    except (OSError, ValueError): return None
    if time.time() - info.get('at', 0) > APPLY_TTL:
        APPLY_FILE.unlink(missing_ok=True)
        return None
    return info


def reject(me, node):
    data = require_hub(me)
    return peer_call(data['machines'][node], 'hub-apply-clear', data['term'])


def takeover(me, password, force=False, proof=''):
    """主机崩了：名单里的机器凭最高密码接管。主机还连得上时多一步警告（force）。"""
    data = load()
    if not data: raise HubError(tr('还没有启用主机模式。'))
    if data['hub'] == me: raise HubError(tr('这台已经是主机了。'))
    if me not in data['candidates']: raise HubError(tr('这台不在可当主机的名单里，不能接管。'))
    new = next_state(data, hub=me, proof=proof or None)
    chip.check_state(proof, state_text(new))
    plain = check_password(password)     # 没有密码连主机钥匙都打不开
    LMC_SSH.mkdir(parents=True, exist_ok=True)
    save_private(HUB_KEY, plain.decode())
    write_known_hosts(data, me)
    meta = data['machines'][data['hub']]
    if not force and peer_call(meta, 'hub-state', data['term']) is not None:
        HUB_KEY.unlink(missing_ok=True)
        raise HubError(tr('主机 {0} 还连得上。正常情况用申请或移交；确实要强制接管，再确认一次。', meta.get('title', data['hub'])))
    save(new)
    note(f'强制接管，任期 {new["term"]}，原主机 {data["hub"]}')
    adopt(new, me)
    missed = push_state(new, me)
    start_gateway()
    return missed


def sync(me):
    """主机定期做：通知落后的机器；发现别人任期更新就退下来。开机时先跑一次，再决定开不开入口。"""
    data = load()
    if not data: return None
    report = {'me': me, 'hub': data['hub'], 'term': data['term'], 'peers': {}, 'stepped_down': False}
    newest, seen = data, {}
    if not HUB_KEY.exists(): return report
    for name, meta in peers(data, me).items():
        try: state = peer_call(meta, 'hub-state', data['term'])
        except HubError: state = {'term': data['term'] + 1, 'hub': None}
        if not isinstance(state, dict) or type(state.get('term')) is not int: state = None   # 写法不对的回答当没回答
        report['peers'][name] = state['term'] if state else None
        seen[name] = state
    pull_chip(data, seen)   # 先拿到别人新登记的钥匙，下面才认得它签的名
    conflicts = []
    hearsay = []   # 说"任期更新了"、但没法当真的机器：没有哪台说得准时算冲突，不开入口
    for name, state in seen.items():
        # 任期一样、说法不一样：主机离线时两边各改了一次。要再做一次带签名的变更才能恢复，先标出来，不开入口
        if state and state.get('hub') and state['term'] == data['term'] and any(state.get(k) != data.get(k) for k in ('hub', 'candidates', 'secret')): conflicts.append(name)
        if state and state['term'] > newest['term'] and state.get('hub'):
            claimed = {**newest, **{k: state.get(k) for k in ('term', 'hub', 'candidates', 'machines', 'secret', 'proof')}}
            if state.get('cluster') != data.get('cluster') or (chip.current() is None and not (state['hub'] == name and name in data.get('candidates', []))):
                # 体检 11-15：没登记安全芯片时没有签名可核，以前直接放行——任何一台副机回一句话，就能让主机退出主机模式、删掉自己的钥匙。
                # 现在只认"新主机自己说的"，而且它得在这台现有的可当主机名单里（强制接管就是这样来的）；别的机器转述的不算，别的集群的不算
                note(f'{name} 说任期是 {state["term"]}、主机是 {state.get("hub")}，不是新主机自己说的或对不上，不照它的改')
                report['peers'][name] = data['term']
                hearsay.append(name)
                continue
            try:
                check_shape(me if me in (claimed.get('machines') or {}) else next(iter(claimed.get('machines') or {'x': 0})), claimed)
                chip.check_state(claimed['proof'], state_text(claimed))
                if me not in claimed['machines']:
                    # 离线时被移出了机器表：退下来，不再当主机。指令各台本来也不认了
                    note(f'这台已经被移出机器表（任期 {state["term"]}），退出主机模式')
                    leave()
                    report.update(stepped_down=True, hub=claimed['hub'], term=claimed['term'], removed=True)
                    return report
            except (HubError, chip.ChipError, ValueError, TypeError):
                # 空口说任期更新的不算：不能让一台被攻破的副机把主机骗得退下来。但也不开入口，免得真换了主机时两边都开
                note(f'{name} 说任期是 {state["term"]}，但核对不过，不理它')
                report['peers'][name] = data['term']
                conflicts.append(name)
                continue
            newest = claimed
    if newest is data: conflicts += [n for n in hearsay if n not in conflicts]
    report['conflicts'] = conflicts
    report['unjoined'] = [n for n, s in seen.items() if s and (not s.get('enabled') or s.get('term', 0) < data['term'])]
    if newest['term'] > data['term'] or any(v is not None and v > data['term'] for v in report['peers'].values()):
        if newest['term'] > data['term']: save(newest)
        if newest.get('hub') != me:
            step_down()
            adopt(newest, me)
            report.update(stepped_down=True, hub=newest['hub'], term=newest['term'])
        return report
    if data['hub'] == me:
        for meta in (data['machines'][n] for n, s in seen.items() if s and chip_seq_of(s) < chip.seq()): push_chip(meta, data['term'])
        for name, term in report['peers'].items():
            if term is not None and term < data['term']:
                peer_call(data['machines'][name], 'hub-accept', data['term'], json.dumps(public_state(data), ensure_ascii=False))
                seen[name] = peer_call(data['machines'][name], 'hub-state', data['term'])
        mine, shared = secret_id(), state_id(data)
        for name, state in seen.items():
            if not state or state.get('term') != data['term']: continue
            if state.get('state_id') != shared: peer_call(data['machines'][name], 'hub-accept', data['term'], json.dumps(public_state(data), ensure_ascii=False))
            if name in data['candidates'] and mine and state.get('secret_id') != mine: peer_call(data['machines'][name], 'hub-secret-store', data['term'], SECRET_FILE.read_text())
        if not conflicts: start_gateway()
    return report


def leave():
    """到机器前退出主机模式：调用方先过 macOS 管理员验证。旧状态改名留着，模型和服务不受影响。这是"每台重新配对"的第一步。"""
    set_aside(left=True)
    for path in (SECRET_FILE, PENDING_SECRET, APPLY_FILE, PASSWORD_INFO, ATTEMPTS_FILE, HUB_KEY): path.unlink(missing_ok=True)
    stop_gateway()
    # 旧钥匙的授权一起撤：可能就是因为它泄露才重新配对。rpc 每次都查这一行，删了就立刻失效。人在机器前过了管理员验证才走到这里
    path = Path.home() / '.ssh/authorized_keys'
    if path.exists():
        lines = path.read_bytes().decode().split('\n')
        kept = [line for line in lines if '--key-id hub ' not in line]
        if len(kept) != len(lines): save_private(path, '\n'.join(kept))
    note('退出了主机模式（重新配对）')


def set_aside(left=False):
    """当前状态不要了：改名留着。left 是人决定退出那个集群：记下编号，它的旧签名不能再把这台拉回去。"""
    try: cluster = load()['cluster']
    except (HubError, KeyError, TypeError): cluster = None
    if cluster and left:
        try: left = json.loads(LEFT_FILE.read_text())
        except (OSError, ValueError): left = []
        save_private(LEFT_FILE, json.dumps((left + [cluster])[-20:]))
    if HUB_FILE.exists(): HUB_FILE.replace(HUB_FILE.with_name(f'hub.json.left-{int(time.time())}'))


def after_accept():
    """刚接受登记：之前这台没有登记，状态不用签名就能写进来，连"这台就是主机"也能写。按新登记再核对一遍，
    对不上的不要，等主机重新通知。真的主机很少走到这里（它重新登记走不到这条路），走到了也能在机器前重新启用。"""
    try: data = load()
    except HubError: data = None
    if not data or not needs_resign(data): return
    set_aside()
    note('接受登记时发现主机状态没有有效签名，放到一边了')


def clear_left():
    """人在机器前又允许了主机钥匙：之前退出的集群不再挡着。"""
    LEFT_FILE.unlink(missing_ok=True)


def chip_seq_of(state):
    value = state.get('chip_seq')
    return value if type(value) is int else 0


def pull_chip(data, seen):
    """有别的机器的登记比这台新（在那台用恢复码登记了新钥匙）：拿过来，对不上的不要。"""
    for name, state in seen.items():
        if state and chip_seq_of(state) > chip.seq():
            try:
                rows = peer_call(data['machines'][name], 'chip-log', data['term'])
                if isinstance(rows, dict): chip.receive(rows.get('updates'))
            except (HubError, chip.ChipError) as exc: note(f'{name} 的登记对不上：{exc}')


def applications(data, me):
    """主机问一圈谁在申请（申请写在副机自己那里，不新增授权）。"""
    if data['hub'] != me or not HUB_KEY.exists(): return []
    found = []
    for name, meta in peers(data, me).items():
        try: state = peer_call(meta, 'hub-state', data['term'], timeout=8)
        except HubError: continue
        if isinstance(state, dict) and state.get('apply'): found.append(name)
    return found


def view(me):
    """给界面用：现在谁是主机、这台能做什么。不含任何密钥。"""
    data = load()
    if not data: return {'enabled': False, **password_view()}
    return {**password_view(), 'applications': applications(data, me), 'enabled': True, 'me': me, 'hub': data['hub'], 'term': data['term'], 'is_hub': data['hub'] == me, 'candidates': data['candidates'],
            'machines': {n: m.get('title', n) for n, m in data['machines'].items()}, 'has_secret': SECRET_FILE.exists(), 'apply': pending_apply(),
            'locked': bool(_locked()), 'gateway': bool(gateway_pid()), 'needs_resign': data['hub'] == me and needs_resign(data)}


def _locked():
    try: _gate()
    except HubError as exc: return str(exc)
    return None


# ---------- rpc 那边的一半：对方是主机钥匙（或管理控制台）调过来的 ----------

def term_ok(term):
    """这条指令带的任期能不能接受。没启用时不查。"""
    data = load()
    return data is None or term >= data['term']


def secret_id():
    """加密钥匙那份文件的指纹：主机和名单里的机器对一下，就知道谁还留着旧密码加密的副本。不含密码。"""
    try: return hashlib.sha256(SECRET_FILE.read_bytes()).hexdigest()[:16]
    except OSError: return None


def state_id(data):
    return hashlib.sha256(json.dumps({'c': data['candidates'], 'm': data['machines']}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def rpc_state():
    data = load()
    if not data: return {'enabled': False, 'term': 0, 'password_set': SECRET_FILE.exists()}
    return {'enabled': True, 'term': data['term'], 'hub': data['hub'], 'cluster': data['cluster'], 'candidates': data['candidates'], 'apply': pending_apply(),
            'secret_id': secret_id(), 'state_id': state_id(data), 'machines': data['machines'], 'secret': data.get('secret'), 'proof': data.get('proof'), 'chip_seq': chip.seq(), 'password_set': SECRET_FILE.exists()}


def rpc_accept(me, state):
    """别的机器告诉这台：现在谁是主机。第一次是加入集群。"""
    data = load()
    check_shape(me, state)
    if not data and LEFT_FILE.exists() and state['cluster'] in json.loads(LEFT_FILE.read_text()): raise HubError(tr('这台已经退出了那个集群。'))
    if data and data['cluster'] != state.get('cluster'): raise HubError(tr('这台属于另一个集群。'))
    if data and state['term'] < data['term']: raise HubError(tr('任期比这台记的旧。'))
    was_hub = bool(data and data['hub'] == me)
    if data and trust_view(data) == trust_view(state):
        # 信任的内容没变：只有显示名能变，签名留这台自己的，不让人写坏
        new = {**data, 'machines': state['machines']}
    else:
        if data and state['term'] == data['term']: raise HubError(tr('任期没变，主机、名单、密码和机器表都不能变。'))
        # 这台还没有登记时沿用配对时的信任（授权行是用户亲手贴的）。有登记就要签名，拿着主机钥匙也改不了
        chip.check_state(state.get('proof'), state_text(state))
        new = {k: state.get(k) for k in ('cluster', 'term', 'hub', 'candidates', 'machines', 'secret', 'proof')}
    save(new)
    if state['hub'] != me and (was_hub or HUB_KEY.exists()): step_down()
    elif state['hub'] != me: stop_gateway(drain=True)   # 单机时自己开着的入口（官方版默认开）：加入后入口只在主机上
    if not data or data['hub'] != state['hub'] or data['machines'] != state['machines']: adopt(new, me)
    if state['hub'] == me or pending_apply() and state['term'] > (data or {}).get('term', 0): APPLY_FILE.unlink(missing_ok=True)
    return {'ok': True, 'term': state['term']}


def rpc_receive(me, package):
    """这台被指定为新主机。"""
    state = package['state']
    check_shape(me, state)
    if state['hub'] != me: raise HubError(tr('这份移交不是给这台的。'))
    rows = load()
    if not rows and LEFT_FILE.exists() and state['cluster'] in json.loads(LEFT_FILE.read_text()): raise HubError(tr('这台已经退出了那个集群。'))
    if rows and rows['cluster'] != state['cluster']: raise HubError(tr('这台属于另一个集群。'))
    if rows and state['term'] <= rows['term']: raise HubError(tr('任期不对。'))
    chip.check_state(state.get('proof'), state_text(state))
    if chip.required() and secret_sha(package['secret']) != state.get('secret'): raise HubError(tr('加密钥匙的指纹和确认的不一样。'))
    if any(line.lstrip().startswith('@') for line in str(package.get('known_hosts', '')).splitlines()): raise HubError(tr('主机指纹的写法不对。'))
    LMC_SSH.mkdir(parents=True, exist_ok=True)
    os.chmod(LMC_SSH, 0o700)
    save_private(HUB_KEY, base64.b64decode(package['key']).decode())
    save_private(SECRET_FILE, package['secret'])
    if package.get('known_hosts'):
        path = LMC_SSH / 'known_hosts'
        old = path.read_text().splitlines() if path.exists() else []
        path.write_text('\n'.join(dict.fromkeys(old + package['known_hosts'].splitlines())) + '\n')
    new = {k: state.get(k) for k in ('cluster', 'term', 'hub', 'candidates', 'machines', 'secret', 'proof')}
    save(new)
    adopt(new, me)
    APPLY_FILE.unlink(missing_ok=True)
    note(f'接任主机，任期 {state["term"]}')
    start_gateway()
    return {'ok': True}


def rpc_store_secret(text):
    blob = json.loads(text)
    if set(blob) != {'v', 'salt', 'nonce', 'cipher', 'tag', 'check'}: raise HubError(tr('格式不对。'))
    if chip.required() and (load() or {}).get('secret') != secret_sha(text): raise HubError(tr('加密钥匙的指纹和确认的不一样。'))
    save_private(SECRET_FILE, text)
    return {'ok': True}


def rpc_drop_secret(me):
    if chip.required() and me in (load() or {}).get('candidates', [me]): raise HubError(tr('这台还在可当主机的名单里。'))
    SECRET_FILE.unlink(missing_ok=True)
    return {'ok': True}


def rpc_clear_apply():
    APPLY_FILE.unlink(missing_ok=True)
    return {'ok': True}
