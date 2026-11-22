#!/usr/bin/env python3
"""安全芯片确认（用户 11-04 定）：最高操作除了最高密码，还要一个安全芯片的签名。

钥匙在 Mac 的安全芯片里，读不出、抄不走，每次签名系统弹框要本人确认（指纹或 Mac 密码）。这里只管核对，不碰私钥：
· 登记表（chip.json）是一串更新，每条由当时已登记的钥匙签名，后一条带前一条的指纹。各台都存一份，不信文件来源，只信这串签名。
· 第一份登记决定信任谁，所以经网络来的不生效，只放着，等人到机器前核对指纹后接受（accept，远程调不到）。
  拿着主机钥匙的程序种不进自己的钥匙；到机器前清掉登记后，也不会被别的机器立刻补回旧的。
· 已有登记的机器，只接受接得上的、签名对的更新。
· 恢复码算出一把普通钥匙，各台只存它的公钥。它只能登记一把新钥匙，用一次就换一个新的。
· 核对用系统自带的 ssh-keygen -Y verify，不自己写椭圆曲线。路径写死，改 PATH 骗不了它。

挡不住的：和这台同一账号的程序能改这台自己的 chip.json，所以这台"放行自己"不算数。
真正的门在对面：别的机器按它们自己的登记表核对，主机钥匙在手也过不去。
"""
from i18n import tr
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time

from paths import RUN_DIR, RUNTIME as ROOT

CHIP_FILE = ROOT / 'chip.json'
PENDING_FILE = ROOT / 'chip-pending.json'
REQUESTS_FILE = RUN_DIR / 'chip-requests.json'
USED_FILE = RUN_DIR / 'chip-used.json'
SSH_KEYGEN = '/usr/bin/ssh-keygen'
NAMESPACE = 'lmc-top'
FRESH, REQUEST_TTL, MAX_REQUESTS, MAX_SIGNATURE, MAX_BODY, MAX_LOG = 300, 600, 5, 4096, 65536, 1000
KEY_HEAD = b'\x00\x00\x00\x13ecdsa-sha2-nistp256\x00\x00\x00\x08nistp256\x00\x00\x00\x41\x04'
KEY_SIZE = len(KEY_HEAD) + 64
TITLE = r'[^\x00-\x1f\x7f\x85  "\\]{1,64}'
BODY_FIELDS = {'seq', 'prev', 'keys', 'recovery', 'recovery_at', 'password_only'}
KEY_FIELDS = {'id', 'title', 'pub', 'added'}
NOT_REGISTERED = tr('还没有登记安全芯片：先到主机的 设置 → 安全 登记。')
NEED_PROOF = tr('这是最高操作，要安全芯片确认。在控制台里操作，系统会弹框要指纹或 Mac 密码。')


class ChipError(RuntimeError): pass


def canonical(value): return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
def sha(text): return hashlib.sha256(text.encode()).hexdigest()
def now(): return time.strftime('%Y-%m-%d %H:%M')


def save_private(path, text):
    """写到旁边再改名，0600。临时名每次不同，两个进程同时写不会互相截断；落盘后才改名，断电不会留下空文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
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


# ---------- 钥匙 ----------

def valid_pub(pub):
    """只认 P-256 的公钥：安全芯片只有这一种。"""
    if not isinstance(pub, str) or len(pub) > 200: return False
    try: blob = base64.b64decode(pub, validate=True)
    except ValueError: return False
    return len(blob) == KEY_SIZE and blob.startswith(KEY_HEAD)


def key_hash(pub): return hashlib.sha256(base64.b64decode(pub)).hexdigest()
def key_id(pub): return key_hash(pub)[:12]


def fingerprint(pub):
    """给人核对的指纹，16 位：太短的话，有心人能算出一把指纹相同的钥匙。"""
    text = key_hash(pub)[:16].upper()
    return '-'.join(text[i:i + 4] for i in range(0, 16, 4))


def clean_title(title):
    if not isinstance(title, str) or not re.fullmatch(TITLE, title): raise ChipError(tr('机器的名字不合规：不超过 64 个字，不能带换行和引号。'))
    return title


def new_key(title, pub):
    if not valid_pub(pub): raise ChipError(tr('这不是安全芯片的公钥。'))
    return {'id': key_id(pub), 'title': clean_title(title), 'pub': pub, 'added': now()}


def good_signature(pub, text, armored):
    """交给系统的 ssh-keygen 核对。对不上就是 False，核对不了（工具不在、超时）也当不对。"""
    if not valid_pub(pub) or not isinstance(text, str) or not isinstance(armored, str) or len(armored) > MAX_SIGNATURE: return False
    with tempfile.TemporaryDirectory(prefix='lmc-chip-') as folder:
        allowed, signature = Path(folder) / 'allowed', Path(folder) / 'signature'
        allowed.write_text(f'signer namespaces="{NAMESPACE}" ecdsa-sha2-nistp256 {pub}\n')
        signature.write_text(armored)
        try: r = subprocess.run([SSH_KEYGEN, '-Y', 'verify', '-n', NAMESPACE, '-f', str(allowed), '-I', 'signer', '-s', str(signature)],
                                input=text.encode(), capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired): return False
    return r.returncode == 0


# ---------- 登记表 ----------

def load():
    """文件在但读不懂时报错，不当成没登记：没登记是"第一份照单全收"，坏文件不能走那条路。"""
    try: return json.loads(CHIP_FILE.read_text())
    except FileNotFoundError: return None
    except (OSError, ValueError): raise ChipError(tr('{0} 读不懂，最高操作先停下。到这台的 设置 → 安全 重新配对。', CHIP_FILE.name))


def log():
    data = load()
    return data['log'] if data else []


def seq(): return len(log())


def current():
    """现在生效的那一份；没登记过返回 None。"""
    rows = log()
    return json.loads(rows[-1]['body']) if rows else None


def required():
    """这台是不是要核对。开了"允许只用最高密码"就不强制，签了照样核对。"""
    state = current()
    return bool(state and not state.get('password_only'))


def state_view():
    """给别的机器看：登记到第几条、最后一条的指纹、这台在等谁批。"""
    rows = log()
    return {'seq': len(rows), 'top': sha(rows[-1]['body']) if rows else '', 'requests': requests(), 'pending': bool(pending_view())}


def view():
    state = current()
    rows = [{'id': k['id'], 'title': k['title'], 'added': k['added'], 'fingerprint': fingerprint(k['pub'])} for k in (state or {}).get('keys', [])]
    return {'enabled': bool(state), 'seq': seq(), 'keys': rows, 'recovery_at': (state or {}).get('recovery_at'), 'recovery_id': key_id(state['recovery']) if state else None,
            'password_only': bool((state or {}).get('password_only')), 'requests': requests(), 'root': root_fingerprint(log()), 'pending': pending_view()}


def root_fingerprint(rows):
    """第一把钥匙的指纹：整串登记都从它开始，各台要一样。"""
    return fingerprint(json.loads(rows[0]['body'])['keys'][0]['pub']) if rows else None


def root_hash():
    rows = log()
    return sha(rows[0]['body']) if rows else ''


def chain_digest(rows): return sha(canonical([row['body'] for row in rows]))


def pending_view():
    """等着接受的那一份，给人核对。写法不对就当没有。"""
    try:
        rows = json.loads(PENDING_FILE.read_text())['log']
        top = json.loads(rows[-1]['body'])
        return {'root': root_fingerprint(rows), 'title': json.loads(rows[0]['body'])['keys'][0]['title'], 'seq': len(rows), 'digest': chain_digest(rows),
                'keys': [{'title': k['title'], 'fingerprint': fingerprint(k['pub'])} for k in top['keys']]}
    except (OSError, ValueError, KeyError, IndexError, TypeError): return None


def check_body(body):
    """一份登记本身写得对不对，和谁签的无关。"""
    if not isinstance(body, dict) or set(body) != BODY_FIELDS: raise ChipError(tr('登记的内容不对。'))
    if type(body['seq']) is not int or not isinstance(body['password_only'], bool) or not isinstance(body['keys'], list) or not body['keys']: raise ChipError(tr('登记的内容不对：至少要留一把钥匙。'))
    if not valid_pub(body['recovery']): raise ChipError(tr('恢复码的公钥不对。'))
    seen = set()
    for key in body['keys']:
        if not isinstance(key, dict) or set(key) != KEY_FIELDS or not valid_pub(key['pub']) or key['id'] != key_id(key['pub']) or key['id'] in seen or not isinstance(key['added'], str): raise ChipError(tr('登记里有一把钥匙不对。'))
        if key['pub'] == body['recovery']: raise ChipError(tr('恢复码的钥匙不能当日常钥匙。'))
        clean_title(key['title'])
        seen.add(key['id'])


def check_update(rows, update):
    """能不能接到这串后面。rows 是已有的。"""
    if not isinstance(update, dict) or not isinstance(update.get('body'), str) or len(update['body']) > MAX_BODY: raise ChipError(tr('登记的内容不对。'))
    try: body = json.loads(update['body'])
    except ValueError: raise ChipError(tr('登记的内容不对。'))
    check_body(body)
    if not rows:
        if body['seq'] != 1 or body['prev'] != '' or len(body['keys']) != 1 or update.get('signer') != body['keys'][0]['id']: raise ChipError(tr('第一份登记只能有一把钥匙，由它自己签名。'))
        if not good_signature(body['keys'][0]['pub'], update['body'], update.get('sig')): raise ChipError(tr('第一份登记的签名不对。'))
        return body
    old = json.loads(rows[-1]['body'])
    if body['seq'] != old['seq'] + 1 or body['prev'] != sha(rows[-1]['body']): raise ChipError(tr('这份登记和这台记的接不上：可能有两台同时改过，或者这台是重新配对前的旧记录。'))
    if update.get('signer') == 'recovery':
        # 恢复码只能登记一把新钥匙，用一次就换
        added = [k for k in body['keys'] if k['id'] not in {o['id'] for o in old['keys']}]
        if body['keys'][:len(old['keys'])] != old['keys'] or len(added) != 1 or len(body['keys']) != len(old['keys']) + 1: raise ChipError(tr('恢复码只能登记一把新钥匙。'))
        if body['recovery'] == old['recovery'] or body['password_only'] != old['password_only']: raise ChipError(tr('恢复码用过就要换新的，也不能改别的设置。'))
        pub = old['recovery']
    else:
        signer = next((k for k in old['keys'] if k['id'] == update.get('signer')), None)
        if not signer: raise ChipError(tr('签这份登记的钥匙没有登记，或者已经吊销了。'))
        pub = signer['pub']
    if not good_signature(pub, update['body'], update.get('sig')): raise ChipError(tr('这份登记的签名不对。'))
    return body


def extend(rows, updates):
    """把一串更新接到 rows 后面：已有的要一字不差，新的逐条核对，有一条不对整串都不要。只算不存。"""
    rows = list(rows)
    if not isinstance(updates, list) or len(updates) > MAX_LOG: raise ChipError(tr('登记的内容不对。'))
    for update in updates:
        try: number = json.loads(update['body'])['seq']
        except (TypeError, ValueError, KeyError): raise ChipError(tr('登记的内容不对。'))
        if type(number) is not int or number < 1: raise ChipError(tr('登记的内容不对。'))
        if number <= len(rows):
            if rows[number - 1]['body'] != update['body']: raise ChipError(tr('这份登记和这台记的对不上：这台是重新配对前的旧记录，到这台的 设置 → 安全 重新配对。'))
            continue
        check_update(rows, update)
        rows.append({'body': update['body'], 'signer': update['signer'], 'sig': update['sig']})
    return rows


def apply(updates):
    """这台自己收下。第一份也算数：只有人在这台机器前（这台的控制台、accept）才走这里。"""
    data = load() or {'log': []}
    rows = extend(data['log'], updates)
    if len(rows) != len(data['log']):
        save_private(CHIP_FILE, json.dumps({'log': rows}, indent=1, ensure_ascii=False))
        PENDING_FILE.unlink(missing_ok=True)
    return len(rows)


def receive(updates):
    """别的机器发来的。这台已有登记：接得上就收。这台还没有：签名对也不生效，放着等人来接受，返回 0。"""
    if log(): return apply(updates)
    rows = extend([], updates)
    if rows: save_private(PENDING_FILE, json.dumps({'log': rows}, ensure_ascii=False))
    return 0


def accept(digest):
    """人在这台机器前，核对过指纹。rpc 里没有这一条，远程调不到。digest 是人看到的那一份：
    文件随时可能被别的机器发来的盖掉，不绑定的话，核对的和生效的可能不是同一份。"""
    if log(): raise ChipError(tr('这台已经有登记了。'))
    try: rows = json.loads(PENDING_FILE.read_text())['log']
    except (OSError, ValueError, KeyError, TypeError): raise ChipError(tr('没有等着接受的登记。'))
    if chain_digest(rows) != digest: raise ChipError(tr('这份登记刚刚变了，和你核对的不是同一份，没有接受。再核对一次；反复变就是有人在捣乱。'))
    return apply(rows)


def forget():
    """到机器前重新配对：调用方先过 macOS 管理员验证。"""
    CHIP_FILE.unlink(missing_ok=True)
    PENDING_FILE.unlink(missing_ok=True)
    REQUESTS_FILE.unlink(missing_ok=True)


# ---------- 算下一份，交给应用去签 ----------

def next_body(action, form):
    """只算不存：应用拿去签名，再用 apply 交回来。"""
    state = current()
    if action == 'init':
        if state: raise ChipError(tr('已经登记过了。'))
        if not valid_pub(form.get('recovery')): raise ChipError(tr('恢复码的公钥不对。'))
        return canonical({'seq': 1, 'prev': '', 'keys': [new_key(form.get('title'), form.get('pub'))], 'recovery': form['recovery'], 'recovery_at': now(), 'password_only': False})
    if not state: raise ChipError(NOT_REGISTERED)
    body = {**state, 'seq': state['seq'] + 1, 'prev': sha(log()[-1]['body'])}
    if action == 'add':
        key = new_key(form.get('title'), form.get('pub'))
        if key['id'] in {k['id'] for k in state['keys']}: raise ChipError(tr('这把钥匙已经登记了。'))
        body['keys'] = state['keys'] + [key]
    elif action == 'revoke':
        if form.get('id') not in {k['id'] for k in state['keys']}: raise ChipError(tr('没有这把钥匙。'))
        body['keys'] = [k for k in state['keys'] if k['id'] != form['id']]
        if not body['keys']: raise ChipError(tr('这是最后一把钥匙，不能吊销：吊销了就没有机器能做最高操作了。'))
    elif action == 'password-only':
        if not isinstance(form.get('on'), bool): raise ChipError(tr('要写开还是关。'))
        body['password_only'] = form['on']
    elif action == 'recovery':
        if not valid_pub(form.get('recovery')) or form['recovery'] == state['recovery']: raise ChipError(tr('恢复码的公钥不对。'))
        body.update(recovery=form['recovery'], recovery_at=now())
    elif action == 'recover':
        key = new_key(form.get('title'), form.get('pub'))
        if key['id'] in {k['id'] for k in state['keys']}: raise ChipError(tr('这把钥匙已经登记了。'))
        if not valid_pub(form.get('recovery')) or form['recovery'] == state['recovery']: raise ChipError(tr('恢复码的公钥不对。'))
        body.update(keys=state['keys'] + [key], recovery=form['recovery'], recovery_at=now())
    else: raise ChipError(tr('不支持这个操作。'))
    check_body(body)
    return canonical(body)


# ---------- 登记申请：写在申请的那台，有人批才生效，10 分钟没人批就失效 ----------

def clean_requests(rows):
    """编号和指纹按公钥重算，不信写的：同账号的程序能写一个真指纹配假公钥。写法不对的、过期的、已登记的都不要。"""
    state = current()
    known = {k['id'] for k in (state or {}).get('keys', [])}
    good = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not valid_pub(row.get('pub')) or not isinstance(row.get('at'), (int, float)) or not isinstance(row.get('title'), str) or not re.fullmatch(TITLE, row['title']): continue
        if time.time() - row['at'] < REQUEST_TTL and key_id(row['pub']) not in known: good.append({**row, 'id': key_id(row['pub']), 'fingerprint': fingerprint(row['pub'])})
    return good


def requests():
    try: return clean_requests(json.loads(REQUESTS_FILE.read_text()))
    except (OSError, ValueError): return []


def add_request(title, pub):
    key = new_key(title, pub)
    rows = [r for r in requests() if r['id'] != key['id']]
    rows.append({'id': key['id'], 'title': key['title'], 'pub': pub, 'fingerprint': fingerprint(pub), 'at': time.time()})
    save_private(REQUESTS_FILE, json.dumps(rows[-MAX_REQUESTS:], ensure_ascii=False))


# ---------- 核对一次操作 ----------

def op_text(op, targets):
    """要签的内容：做什么、对哪几台、什么时候、一次性的号。"""
    return canonical({'op': op, 'targets': sorted(targets), 'at': int(time.time()), 'nonce': secrets.token_hex(16)})


def read_proof(proof):
    try: value = json.loads(proof) if isinstance(proof, str) else proof
    except ValueError: raise ChipError(tr('安全芯片的确认写法不对。'))
    if not isinstance(value, dict) or not isinstance(value.get('payload'), str): raise ChipError(tr('安全芯片的确认写法不对。'))
    return value


def check_key(state, value):
    key = next((k for k in state['keys'] if k['id'] == value.get('key')), None)
    if not key: raise ChipError(tr('这把钥匙没有登记，或者已经吊销了。'))
    if not good_signature(key['pub'], value['payload'], value.get('sig')): raise ChipError(tr('安全芯片的签名不对，什么都没动。'))


def check_signed(proof, op):
    """签名对不对、是不是刚签的。对了返回内容。这台没有登记表（没开这层保护，或者收到了还没接受），
    或者"只用最高密码"开着又没签，返回 None。一次一用归 check_op。"""
    state = current()
    if state is None: return None
    if not proof:
        if state.get('password_only'): return None
        raise ChipError(NEED_PROOF)
    value = read_proof(proof)
    check_key(state, value)
    try: body = json.loads(value['payload'])
    except ValueError: raise ChipError(tr('安全芯片的确认写法不对。'))
    if not isinstance(body, dict) or body.get('op') != op: raise ChipError(tr('这次确认不是给这个操作的。'))
    at, nonce, targets = body.get('at'), body.get('nonce'), body.get('targets')
    if not isinstance(at, int) or not isinstance(nonce, str) or not isinstance(targets, list): raise ChipError(tr('安全芯片的确认写法不对。'))
    if abs(time.time() - at) > FRESH: raise ChipError(tr('这次确认已经过期了（5 分钟内有效），再做一次。'))
    return body


def consume(nonce, target):
    """每个号对每台只能用一次，别人录下来重放没用。"""
    try: used = json.loads(USED_FILE.read_text())
    except (OSError, ValueError): used = []
    used = [row for row in used if time.time() - row[1] < FRESH * 2]
    mark = f'{nonce}:{target}'
    if any(row[0] == mark for row in used): raise ChipError(tr('这次确认已经用过了，再做一次。'))
    save_private(USED_FILE, json.dumps(used + [[mark, time.time()]]))


def check_op(proof, op, target):
    """这个操作能不能落到 target 上。"""
    body = check_signed(proof, op)
    if body is None: return
    if target not in body['targets']: raise ChipError(tr('这次确认不包括这台。'))
    consume(body['nonce'], target)


def check_state(proof, text):
    """主机、任期、名单、密码的变化：text 是这边算出来的应有内容，必须一字不差。
    防重放靠任期只增，不看时间，因为离线的机器可能几天后才收到。这台还没有登记时沿用配对时的信任。"""
    state = current()
    if state is None: return
    if not proof:
        if state.get('password_only'): return
        raise ChipError(NEED_PROOF)
    value = read_proof(proof)
    if value['payload'] != text: raise ChipError(tr('确认的内容和这次操作对不上：状态可能刚变过，再做一次。'))
    check_key(state, value)
