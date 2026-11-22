#!/usr/bin/env python3
"""被控方的 rpc 入口：控制方的受限钥匙只能跑这个程序（authorized_keys 里的强制命令）。

请求从 SSH_ORIGINAL_COMMAND 读，整行按白名单 fullmatch，不经 shell，校验过才 import clusterctl。
带空格的参数（子密钥的名字）和密钥走标准输入的 JSON，只有 INPUT 里的子命令才读，都有长度上限。
级别：call 只能开隧道（这里一律拒绝）｜view 只能看｜control 能操作。没有任何子命令能增加授权、改主密钥、推文件：
那些只能在这台本机做（clusterctl grants、管理页）或走维护通道。

查询类输出一行 JSON。动作类输出进度文字，结果放 "@@result <JSON>"，失败放 "@@error <JSON>"。
退出码：0 成功｜1 操作失败｜2 请求不合规｜3 级别不够｜4 这把钥匙已被吊销｜5 启动参数不对。
"""
from i18n import tr
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time

MODEL = r'[a-z0-9][a-z0-9_-]{0,31}'
MAX_REQUEST, MAX_INPUT = 400, 4096
MAX_BIG_INPUT = 2**20  # 移交的那一包（机器表、加密的钥匙）比子密钥大
LEVELS = {'call': 0, 'view': 1, 'control': 2, 'admin': 3, 'hub': 4}
# 子命令：(要求的级别, 子命令后面整段的写法)
COMMANDS = {
    'ping': ('view', r''),
    'state': ('view', r'|--fast'),
    'requests': ('view', r''),
    'progress': ('view', r''),
    'logs': ('view', r''),
    'doctor-data': ('view', r''),
    'prepare': ('control', r''),   # 第 4 期：这台的准备情况（能不能关机、FileVault、地址是不是手动设的、oMLX 在不在），只读
    'activity': ('view', r''),
    'serve': ('control', r''),
    'stop': ('control', r''),
    'poweroff': ('admin', r''),
    'console-open': ('control', r''),
    'console-quit': ('control', r''),
    'admin-session': ('admin', r''),
    'power-check': ('control', r''),
    'hub-poweroff': ('admin', r'all|[a-z0-9-]{1,32}'),
    'subkey-list': ('view', r''),
    'load': ('control', rf'{MODEL}(?: --free-ollama)?(?: --keep| --replace)?'),
    'unload': ('control', rf'{MODEL}(?: --quiet)?'),
    'option': ('control', rf'{MODEL} (?:mtp|ssd) (?:on|off) (?:now|later|ask)(?: --free-ollama)?'),
    'context': ('control', rf'{MODEL} [0-9]{{1,8}}'),
    'selftest': ('control', MODEL),
    'warmup': ('control', MODEL),
    'clear-cache': ('control', r''),
    'cache-set': ('control', r'(?:off|auto|[0-9]{1,4})(?: --free-ollama)?'),   # auto：官方版的"自动"（0.5.1 A14），自管版由 set_cache 拒绝
    'subkey-create': ('admin', r''),
    'subkey-revoke': ('admin', r''),
    'hub-state': ('view', r''),
    'hub-accept': ('hub', r''),
    'hub-receive': ('hub', r''),
    'hub-secret-store': ('hub', r''),
    'hub-secret-drop': ('hub', r''),
    'hub-apply-clear': ('admin', r''),
    'hub-reject': ('admin', r'[a-z0-9-]{1,32}'),
    'hub-takeover': ('admin', r'|--force'),
    'hub-handover': ('admin', r'[a-z0-9-]{1,32}'),
    'manage': ('view', r'[^\x00-\x1f]{1,300}'),
    # 安全芯片：查询和"算一份要签的内容"不改东西；apply 只接受签名对的，第一份不生效，要人到机器前接受（accept、forget 不在这里，远程调不到）
    'chip-view': ('view', r''),
    'chip-state': ('view', r''),
    'chip-log': ('view', r''),
    'chip-payload': ('view', r'poweroff (?:all|[a-z0-9-]{1,32})|state (?:handover [a-z0-9-]{1,32}|takeover|candidates [a-z0-9-]{1,32}(?: [a-z0-9-]{1,32}){0,20})'),
    'chip-body': ('view', r'recover'),   # 别的改动不开放给远程：管理版核对内容的基准都来自主机，主机被攻破就能偷换恢复码
    'chip-apply': ('admin', r''),
    'chip-request': ('admin', r''),
}
INPUT = {'subkey-create', 'subkey-revoke'}
HUB_INPUT = {'hub-accept', 'hub-receive', 'hub-secret-store', 'hub-takeover', 'hub-handover', 'hub-poweroff', 'poweroff', 'chip-body', 'chip-apply', 'chip-request'}  # 里面可能有密钥和密码，一律不记日志
# 管理控制台能让主机跑的 clusterctl 命令，按级别列清楚；不在这里的（授权、名单、密码、主密钥）远程调不到
MANAGE = [
    ('view', r'status --json( --no-worker)?'), ('view', r'progress --json( --no-notify)?'), ('view', r'requests [a-z0-9-]{1,32}'), ('view', r'logs'),
    ('view', r'doctor'), ('view', r'subkey where'), ('view', r'hub view'),
    ('control', r'(serve|autostart|up)( [a-z0-9-]{1,32})?'), ('control', r'(stop|restart|down) [a-z0-9-]{1,32} --yes'), ('control', r'cycle [a-z0-9-]{1,32} --yes( --reload)?'), ('control', r'start [a-z0-9-]{1,32} --on [a-z0-9-]{1,32}( --yes| --free-ollama| --wait| --keep| --replace)*'),
    ('control', r'unload [a-z0-9-]{1,32} --on [a-z0-9-]{1,32}( --quiet| --wait)*'), ('control', r'unload all'), ('control', r'selftest [a-z0-9-]{1,32} --on [a-z0-9-]{1,32}'),
    ('control', r'option [a-z0-9-]{1,32} [a-z0-9-]{1,32} (mtp|ssd) (on|off)( --yes| --later| --free-ollama)*'), ('control', r'context [a-z0-9-]{1,32} [a-z0-9-]{1,32} [0-9]{1,8}'),
    ('control', r'cache set [a-z0-9-]{1,32} (off|[0-9]{1,4})( --yes)?( --free-ollama)?'), ('control', r'cache clear [a-z0-9-]{1,32} --yes'), ('control', r'warmup [a-z0-9-]{1,32} --on [a-z0-9-]{1,32}'),
    ('admin', r'subkey revoke [^\x00-\x1f\x7f"\\ ]{1,64} --yes'), ('admin', r'subkey create-print [^\x00-\x1f\x7f"\\ ]{1,64}'),
]
SUBKEY_NAME = r'[^\x00-\x1f\x7f"\\]{1,64}'
SUBKEY_VALUE = r'[A-Za-z0-9_-]{16,128}'


class Forgiving:
    """控制方断开（合盖、拔网线）后这边还要把事做完：写不出去就当没写，不能让一次 print 中断加载。"""

    def __init__(self, stream): self.stream = stream

    def write(self, text):
        try: return self.stream.write(text)
        except OSError: return len(text)

    def flush(self):
        try: self.stream.flush()
        except OSError: pass

    def isatty(self): return False


def stop(code, text, tag='@@error '):
    print(tag + json.dumps(text, ensure_ascii=False))
    sys.exit(code)


def granted(key_id):
    """这把钥匙那一行还在不在，返回控制方的名字；不在返回 None。sshd 只在握手时查钥匙，复用连接上的新会话靠这里拦。"""
    try: lines = (Path.home() / '.ssh/authorized_keys').read_bytes().decode().split('\n')
    except OSError: return None
    for line in lines:
        if not line.lstrip().startswith('#') and f'--key-id {key_id} ' in line:
            return line.partition(f'lmc:{key_id}')[2].strip() or key_id
    return None


def split_term(request):
    """主机发的指令带 @任期。(任期或 None, 去掉任期的请求)"""
    if request.startswith('@'):
        head, _, rest = request.partition(' ')
        if re.fullmatch(r'@[0-9]{1,9}', head): return int(head[1:]), rest
        stop(2, tr('不支持这个请求。'))
    return None, request


def manage_level(words):
    """这条 clusterctl 命令要什么级别；不在单子上返回 None。"""
    for level, rule in MANAGE:
        if re.fullmatch(rule, words): return level
    return None


def parse(request):
    """(子命令, 参数列表)。不合规直接退出，错误文字不回显请求内容。"""
    if len(request) > MAX_REQUEST: stop(2, tr('请求太长。'))
    command, _, rest = request.partition(' ')
    if command not in COMMANDS or not re.fullmatch(COMMANDS[command][1], rest): stop(2, tr('不支持这个请求。'))
    return command, rest.split(' ') if rest else []


def read_input(command):
    if command in HUB_INPUT:
        raw = sys.stdin.read(MAX_BIG_INPUT + 1)
        if len(raw) > MAX_BIG_INPUT: stop(2, tr('输入太长。'))
        return raw
    if command not in INPUT: return {}
    raw = sys.stdin.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT: stop(2, tr('输入太长。'))
    try: data = json.loads(raw)
    except ValueError: stop(2, tr('输入不是 JSON。'))
    if not isinstance(data, dict) or not isinstance(data.get('name'), str) or not re.fullmatch(SUBKEY_NAME, data['name']): stop(2, tr('子密钥的名字不合规。'))
    if command == 'subkey-create' and not (isinstance(data.get('key'), str) and re.fullmatch(SUBKEY_VALUE, data['key'])): stop(2, tr('子密钥的写法不合规。'))
    return data


def log(key_id, level, request, code):
    """只记操作和拒绝，10 秒一次的查询不记；标准输入里可能有密钥，一律不记。"""
    from paths import RUN_DIR
    try:
        path = RUN_DIR / 'rpc.log'
        if path.exists() and path.stat().st_size > 2**20: path.replace(path.with_name('rpc.log.old'))
        with path.open('a') as f: f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {key_id} {level} {request[:80]!r} → {code}\n")
    except OSError: pass


def run(command, args, data):
    import clusterctl as c
    node = c.LOCAL
    if command.startswith('chip-'): return run_chip(command[5:], args, data, c)
    if command.startswith('hub-') or command == 'manage': return run_hub(command, args, data, node, c)
    if command in ('load', 'unload', 'option', 'context', 'selftest', 'warmup') and args and args[0] not in c.MODELS and re.fullmatch(c.MODEL_KEY, args[0]):
        c.discover(node, c.model_rows(node))   # 这台 oMLX 列着、还没记进②的模型：主机点到它时现认一遍（第 4 期），和在本机操作一样
    if command not in ('ping', 'state', 'requests', 'progress', 'logs', 'doctor-data', 'prepare', 'activity', 'serve', 'stop', 'clear-cache',
                       'cache-set', 'subkey-create', 'subkey-revoke', 'subkey-list', 'poweroff', 'console-open', 'console-quit', 'admin-session', 'power-check') and args[0] not in {*c.MODELS, 'all'}:
        stop(2, tr('不支持这个请求。'))
    if command in ('option', 'context', 'selftest', 'warmup', 'load') and args[0] == 'all': stop(2, tr('不支持这个请求。'))
    if command == 'ping': return {'proto': c.RPC_PROTO, 'code': c.code_hash(), 'node': node, 'title': c.where(node), 'computer': c.computer_name()}
    if command == 'state': return c.local_state(full=not args)
    if command == 'requests': return c.requests_now(node)
    if command == 'progress': return c.progress_sample(node) if c.health(node) == 'up' else None
    if command == 'logs':
        name, lines = c.latest_log()
        return {'name': name, 'lines': lines}
    if command == 'doctor-data': return c.doctor_data()
    if command == 'prepare': return c.prepare_info(remote=True)
    if command == 'activity': return c.activity()
    if command == 'serve': return c.serve_node(node)
    if command == 'stop': return c.stop_node(node) and None
    if command == 'subkey-list': return c.rpc_subkey_list()
    if command == 'poweroff': return c.poweroff_here(data.partition('\n')[0].strip())
    if command == 'power-check': return {'ok': c.can_poweroff(node)}
    if command == 'console-open': return {'ok': c.console_action('open')}
    if command == 'console-quit': return {'ok': c.console_action('quit')}
    if command == 'admin-session': return {'value': c.login_here()}
    if command == 'load':
        ready = c.start(args[0], node, assume_yes=True, free_ollama_ok='--free-ollama' in args, keep='--keep' in args, replace='--replace' in args)
        print('@@result ' + json.dumps({'context': c.node_context(node, args[0]), 'loaded': ready is not None}))
    elif command == 'unload':
        if args[0] == 'all': c.unload_all(node, quiet='--quiet' in args)
        else: c.unload(args[0], node, quiet='--quiet' in args)
    elif command == 'option':
        c.set_option(node, args[0], args[1], args[2] == 'on', args[3], '--free-ollama' in args)
    elif command == 'context':
        print('@@result ' + json.dumps({'live': c.set_context(node, args[0], args[1]), 'value': int(args[1])}))
    elif command == 'selftest': c.selftest(args[0], node)
    elif command == 'warmup': c.warmup_command(args[0], node)
    elif command == 'clear-cache': c.clear_cache(node, assume_yes=True)
    elif command == 'cache-set': c.set_cache(node, args[0], assume_yes=True, free_ollama_ok='--free-ollama' in args)
    elif command == 'subkey-create': return c.rpc_subkey_create(data['name'], data['key'])
    elif command == 'subkey-revoke': return c.rpc_subkey_revoke(data['name'])
    return None


def run_hub(command, args, data, node, c):
    import hub as h
    if command == 'hub-state': return h.rpc_state()
    if command == 'hub-accept': return h.rpc_accept(node, json.loads(data))
    if command == 'hub-receive': return h.rpc_receive(node, json.loads(data))
    if command == 'hub-secret-store': return h.rpc_store_secret(data)
    if command == 'hub-secret-drop': return h.rpc_drop_secret(node)
    if command == 'hub-apply-clear': return h.rpc_clear_apply()
    if command == 'hub-reject': return h.reject(node, args[0])
    if command == 'hub-takeover':
        password, proof = h.split_input(data)
        return {'missed': h.takeover(node, password, force='--force' in args, proof=proof)}
    if command == 'hub-handover':
        password, proof = h.split_input(data)
        return {'missed': h.handover(node, args[0], password, proof=proof)}
    if command == 'hub-poweroff':
        state = h.load()
        if state and state['hub'] != node: stop(7, state['hub'], tag='@@not-hub ')
        return c.poweroff_by_manager(args[0], *h.split_input(data))
    if command == 'manage': return run_manage(args, c)


def run_chip(what, args, data, c):
    # 经 rpc 来的登记走 receive：这台还没有登记时，第一份不生效
    try: return c.chip_action('receive' if what == 'apply' else what, args, data if what in ('body', 'apply', 'request') else None)
    except ValueError: stop(2, tr('输入不是 JSON。'))


def forward(source, target):
    for line in source:
        target.write(line)
        target.flush()


def run_manage(args, c):
    """管理控制台让主机跑一条 clusterctl 命令。这台不是主机时不跑，告诉对方谁是主机。输出逐行照打，应用当本地命令处理。"""
    import hub as h
    state = h.load()
    if state and state['hub'] != c.LOCAL: stop(7, state['hub'], tag='@@not-hub ')
    child = subprocess.Popen([sys.executable, '-B', '-c', c.SELF_RUN, str(Path(__file__).resolve().parent), *args], env={**os.environ}, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    errors = threading.Thread(target=forward, args=(child.stderr, Forgiving(sys.stderr)), daemon=True)
    errors.start()
    forward(child.stdout, sys.stdout)
    errors.join()
    code = child.wait()
    if code: sys.exit(code)
    return None


def main():
    for sig in (signal.SIGHUP, signal.SIGPIPE): signal.signal(sig, signal.SIG_IGN)
    sys.stdout.reconfigure(line_buffering=True)
    sys.stdout = Forgiving(sys.stdout)
    if len(sys.argv) != 5 or sys.argv[1] != '--key-id' or sys.argv[3] != '--level' or sys.argv[4] not in LEVELS \
            or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', sys.argv[2]):
        stop(5, tr('启动参数不对。'))
    key_id, level, request = sys.argv[2], sys.argv[4], os.environ.get('SSH_ORIGINAL_COMMAND', '')
    # 控制方的界面是英文时带这个前缀，这边按它回文字。只认这两个值，不进白名单之后的任何地方
    if language := re.match(r'lang=(zh|en) ', request):
        os.environ['LMC_LANG'] = language[1]
        request = request[language.end():]
    code = 2
    try:
        term, request = split_term(request)
        command, args = parse(request)
        who = granted(key_id)
        if who is None: stop(4, tr('这把钥匙已被吊销。'))
        needed = COMMANDS[command][0]
        if command == 'manage':
            args = (request.partition(' ')[2]).split(' ')
            needed = manage_level(' '.join(args))
            if needed is None: stop(2, tr('不支持这个请求。'))
        if LEVELS[level] < LEVELS[needed]:
            stop(3, tr('这把钥匙的级别不够。') if level != 'call' else tr('这把钥匙只能开隧道。'))
        if level == 'hub':
            import hub as h
            if term is None and h.load(): stop(6, tr('主机的指令要带任期。'))
            # 只读的 hub-state 不拦：旧主机要靠它发现自己已经被换了
            if term is not None and not h.term_ok(term) and command not in ('hub-state', 'chip-state', 'chip-log'): stop(6, tr('任期已过：主机已经换了。'))
        if command == 'poweroff' and level != 'hub':
            import hub as h
            if h.load(): stop(3, tr('启用主机模式后，这里只听主机的。管理版的关机要最高密码。'))
        data = read_input(command)
        code = 1
        import clusterctl as c
        c.OPERATOR = who
        c.OPERATOR_LEVEL = level
        result = run(command, args, data)
        if command == 'hub-state' and level != 'hub' and isinstance(result, dict):
            result = {k: v for k, v in result.items() if k not in ('machines', 'proof', 'secret')}   # 各台的地址、账号只给主机钥匙
        code = 0
        if result is not None or (COMMANDS[command][0] == 'view' and command != 'manage') or command in INPUT: print(json.dumps(result, ensure_ascii=False))
    except SystemExit as exc:
        if isinstance(exc.code, int): code = exc.code
        raise
    except Exception as exc:
        stop(1, str(exc))
    finally:
        if code or COMMANDS.get(request.partition(' ')[0], ('view',))[0] != 'view' or (request.startswith('manage ') and manage_level(request[7:]) != 'view'):
            log(key_id, level, request, code)


if __name__ == '__main__':
    main()
