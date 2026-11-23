#!/usr/bin/env python3
"""入口：集群对客户端的一扇门（用户 11-03 定）。独立进程，只做转发，自己不算东西，也不依赖 oMLX：
重启或升级哪一台的 oMLX，都不影响经过它去别的机器的请求。只用标准库。

· 验子密钥：读这台的 settings.json，吊销立刻生效；主密钥不收，它只管管理。
· 只放行推理接口：管理页、加载卸载、联网搜索一律挡住。
· 模型名写成"机器/模型"：多轮缓存跟着机器走，换一台要重读全部上下文，所以不做自动分配。
· 客户端断开就断开上游，那台 oMLX 会停止生成。
· 日志只记元数据（谁、从哪个地址来、哪台、哪个模型、多少字节、多久），不记内容，不记密钥。
· 停用名单（0.5.1）：gateway.json 的 disabled_keys 里只存子密钥的 SHA-256；文件一改就重读，停用、启用立刻生效，不用重启入口。
· 浏览器的预检（OPTIONS）不带密钥，直接放行并回 CORS 头；真请求照旧要子密钥。
· 按网络开门（0.6）：gateway.json 的 networks 里勾了哪些网络，就在那些网络的地址上各开一扇门（绑具体地址，不绑 0.0.0.0），
  网络变了跟着变；本机那扇门（listen）不动。勾出来的门不要求 TLS，但只收那个网络的私网来源，公网来源一律拒（用户 11-21 定）。
  从这些门进来的请求（"外来"）：错钥按来源锁定、按子密钥限流、不认太短的子密钥、/health 只回 ok。本机来的不限。
· listen 里手写非本机地址仍然必须配 TLS（老的高级用法）：明文 HTTP 会把密钥和对话都露在网上。
"""
from i18n import tr
import argparse
import asyncio
import collections
import hashlib
import hmac
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import sys
import time

from paths import CLUSTER_FILE, RUN_DIR, RUNTIME
from gateway_defaults import LIMITS, LOCKOUT, numbers   # 默认数字和后端共用
import netscan
import omlx_adapter

# 第 3 期：这台的 oMLX 是自管还是官方（omlx_adapter.py）。自管版照旧读 omlx-state 的设置、用 /v1/models；
# 官方版的子密钥在它自己的数据目录，而且没加载的模型也列在 /v1/models 里、被点到会自动加载（草案 10.2），所以改认 /v1/models/status 的 loaded
ENGINE = omlx_adapter.detect(RUNTIME, check_signature=False)
SETTINGS = ENGINE.settings if not ENGINE.managed and ENGINE.settings else RUNTIME / 'omlx-state/settings.json'
CONFIG = RUNTIME / 'gateway.json'
LOG = RUN_DIR / 'gateway.log'
ALLOWED = {('POST', '/v1/chat/completions'), ('POST', '/v1/completions'), ('POST', '/v1/messages'),
           ('POST', '/v1/responses'), ('POST', '/v1/embeddings'), ('POST', '/v1/rerank')}
DROP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding',
        'upgrade', 'host', 'content-length', 'authorization', 'x-api-key', 'expect'}
MODELS_TTL = 5
# 浏览器里的客户端（网页版聊天软件）先发一个不带密钥的 OPTIONS 预检：oMLX 直连时回的是 *，经入口也要一样，不然"直连能用、经入口不能用"。
# 不回 Allow-Credentials：密钥在 Authorization 头里，不靠 cookie，别的网页拿不到密钥就调不了
CORS = 'Access-Control-Allow-Origin: *\r\n'
# ---- 0.6：外来请求的规矩。数字都能在 gateway.json 里改（limits、lockout），改了立刻生效 ----
HEALTH_VERSION = 2            # 本机问 /health 时带上：后端靠它认出还在跑的是不是 0.5.1 的旧入口（旧的没有这一项）
MIN_OUTSIDE_KEY = 32          # 外来请求不认比这短的子密钥：控制台建的有 48 个字符，oMLX 管理页能建 4 个字符的
FORGET = 900                  # 这么久没再错，这个来源的记录就清掉
MAX_CONNECTIONS = 32          # 同一个外来来源同时最多几条连接（排队的请求也占着连接：每把子密钥最多 2 个在跑加 8 个在排）
ROUTE_EVENTS = {0xc, 0xd, 0xe}   # 路由套接字里的 RTM_NEWADDR、RTM_DELADDR、RTM_IFINFO：地址增减、网卡上下线


class Door:
    """一扇门：听在哪个地址、属于哪个网络、收哪些来源。kind：local 是本机那扇（listen）；listen 是老的高级用法（listen 写了非本机地址并配了 TLS）；
    lan、tailscale、virtual 是 networks 勾出来的。"""
    def __init__(self, kind, address='', accept=(), ident=None):
        self.kind, self.address, self.accept, self.ident, self.server = kind, address, tuple(accept), ident, None

    def outside(self, source):
        """这个请求算不算外来。本机那扇门只有本机连得上；老的宽门上回环来的算本机；勾出来的门上一律算外来。"""
        if self.kind == 'local': return False
        if self.kind == 'listen': return not netscan.is_loopback(source)
        return True


LOCAL_DOOR = Door('local')
# 开着网络时不让这台睡眠（gateway.json 的 keep_awake，默认关）：系统自带的 caffeinate，-s 只在接着电源时起作用，-w 跟着入口进程走。环境变量只给测试用
CAFFEINATE = (os.environ.get('LMC_CAFFEINATE') if os.environ.get('LMC_ROOT') else None) or '/usr/bin/caffeinate'


class Gateway:
    def __init__(self, nodes, max_body):
        self.nodes = nodes              # {机器: {'title':…, 'port':…}}
        self.max_body = max_body
        self.keys, self.keys_mtime = {}, None
        self.disabled, self.disabled_mtime = frozenset(), None
        self.limits, self.lockout, self.networks = dict(LIMITS), dict(LOCKOUT), ()
        self.awake, self.caffeinate = False, None   # 要不要不让睡眠；正在跑的 caffeinate
        self.doors = {}                 # 勾出来的门：{地址: Door}
        self.port, self.context = None, None
        self.fails = {}                 # 外来的错钥：{来源: [连错几次, 锁到什么时候, 锁过几回, 最近一次错]}
        self.hits = {}                  # {子密钥的 SHA-256: 最近一分钟各个请求的时间}
        self.busy, self.queued, self.outside_busy = {}, {}, 0   # 外来请求：每把在跑几个、在排几个；合计在跑几个
        self.cond = None                # 有请求答完时叫醒排队的
        self.poke = None                # 该重新认一遍网络了（HUP 信号、系统说网络变了）
        self.pending, self.retries = False, 0   # 勾过的网络里有一个还在等路由器应答
        self.syncing, self.again = False, False
        self.route_socket = None
        self.logged = {}                # 拒绝、锁定、限流的日志：同一个来源同一类 10 秒最多记一行
        self.conns = {}                 # 外来的连接：{来源: 现在几条}
        self.unbound = set()            # 绑不上的地址：只在头一回记日志
        self.active = 0
        self.draining = False
        self.server = None
        self.models = {}                # {子密钥: (时间, {机器: [模型 id]})}

    # ---------- 密钥 ----------
    def key_name(self, key):
        """这把子密钥的名字；不认识返回 None。读不了设置就谁都不放（不开门比开错门好）。"""
        try:
            mtime = SETTINGS.stat().st_mtime_ns
            if mtime != self.keys_mtime:
                auth = json.loads(SETTINGS.read_text())['auth']
                self.keys = {k['key']: k.get('name') or tr('未命名') for k in auth.get('sub_keys', []) if k.get('key')}
                self.keys_mtime = mtime
        except (OSError, ValueError, KeyError):
            self.keys, self.keys_mtime = {}, None
            return None
        found = None
        for value, name in self.keys.items():  # 全部比一遍，不让耗时泄露哪一把接近
            if hmac.compare_digest(value.encode(), key.encode()): found = name
        return found

    def read_conf(self):
        """gateway.json 一改就重读：停用名单（0.5.1）、限流和锁定的数、勾了哪些网络（0.6）。
        停用名单在 disabled_keys：{SHA-256: {name, at}}，也认列表。文件没了、读不懂、格式不对都当没有名单（不挡）：
        名单坏了不能把能用的密钥全挡在外面，控制台的体检会报。networks 读不懂则当一个都没勾（门关上，不开错）。"""
        try: mtime = CONFIG.stat().st_mtime_ns
        except OSError: mtime = 0
        if mtime == self.disabled_mtime: return
        conf = None
        if mtime:
            try: conf = json.loads(CONFIG.read_text())
            except (OSError, ValueError): conf = None
        if not isinstance(conf, dict): conf = {}
        rows = conf.get('disabled_keys')
        self.disabled = frozenset(k.lower() for k in rows if isinstance(k, str)) if isinstance(rows, (dict, list)) else frozenset()
        self.limits, self.lockout = numbers(conf.get('limits'), LIMITS), numbers(conf.get('lockout'), LOCKOUT)
        nets = conf.get('networks')
        self.networks = tuple(n for n in nets if isinstance(n, str)) if isinstance(nets, list) else ()
        self.awake = conf.get('keep_awake') is True
        self.disabled_mtime = mtime

    def is_disabled(self, key):
        """这把子密钥在停用名单里（0.5.1）。"""
        self.read_conf()
        return bool(self.disabled) and hashlib.sha256(key.encode()).hexdigest() in self.disabled

    # ---------- 外来请求的规矩（0.6） ----------
    def locked(self, source):
        """这个来源还要锁多少秒；没锁是 0。"""
        row = self.fails.get(source)
        if not row: return 0
        left = row[1] - time.monotonic()
        return int(left) + 1 if left > 0 else 0

    def failed(self, source, tag):
        """外来的一次错钥。连错 tries 次锁一回：头一回 first 秒，再犯 again 秒。记录很久没动就清掉；来源太多时先清最久没动的。"""
        now = time.monotonic()
        row = self.fails.get(source)
        if row and now - row[3] > FORGET and row[1] <= now: row = None
        row = row or [0, 0, 0, now]
        row[0], row[3] = row[0] + 1, now
        if row[0] >= self.lockout['tries']:
            row[0], row[2] = 0, row[2] + 1
            seconds = self.lockout['first'] if row[2] == 1 else self.lockout['again']
            row[1] = now + seconds
            self.event(tr('锁定'), tag, tr('密钥连错 {0} 次，锁 {1} 秒', self.lockout['tries'], seconds), force=True)
        self.fails[source] = row
        if len(self.fails) > 4096:
            for old in sorted(self.fails, key=lambda s: self.fails[s][3])[:1024]: del self.fails[old]

    async def enter(self, digest, who, tag):
        """外来的一个调用进门：先看这把子密钥这一分钟发了几个，再占一个位子；位子满了排队，等到有人答完。
        返回 None（进去了，答完要 leave）或 (说明, 几秒后再试)。"""
        lim, now = self.limits, time.monotonic()
        hits = self.hits.setdefault(digest, collections.deque())
        while hits and now - hits[0] >= 60: hits.popleft()
        if len(hits) >= lim['per_minute']:
            retry = int(60 - (now - hits[0])) + 1
            self.event(tr('限流'), f'{who}@{tag}', tr('一分钟超过 {0} 个', lim['per_minute']))
            return tr('子密钥「{0}」这一分钟的请求超过 {1} 个，{2} 秒后再试。', who, lim['per_minute'], retry), retry
        hits.append(now)
        free = lambda: self.busy.get(digest, 0) < self.limits['concurrent'] and self.outside_busy < self.limits['total']
        if not free():
            if self.queued.get(digest, 0) >= lim['queue']:
                self.event(tr('限流'), f'{who}@{tag}', tr('排队超过 {0} 个', lim['queue']))
                return tr('子密钥「{0}」排队的请求太多（同时最多 {1} 个，其余排队），稍后再试。', who, lim['concurrent']), 5
            self.queued[digest] = self.queued.get(digest, 0) + 1
            try:
                async with self.cond:
                    await asyncio.wait_for(self.cond.wait_for(free), lim['queue_seconds'])
                    self.busy[digest] = self.busy.get(digest, 0) + 1   # 还拿着锁：别人插不进来
                    self.outside_busy += 1
                return None
            except asyncio.TimeoutError:
                self.event(tr('限流'), f'{who}@{tag}', tr('排队 {0} 秒没轮上', lim['queue_seconds']))
                return tr('这台正忙：子密钥「{0}」的请求排了 {1} 秒还没轮上，稍后再试。', who, lim['queue_seconds']), 10
            finally:
                self.queued[digest] -= 1
                if self.queued[digest] <= 0: del self.queued[digest]
        self.busy[digest] = self.busy.get(digest, 0) + 1
        self.outside_busy += 1
        return None

    async def leave(self, digest):
        self.busy[digest] = self.busy.get(digest, 1) - 1
        if self.busy[digest] <= 0: del self.busy[digest]
        self.outside_busy = max(0, self.outside_busy - 1)
        async with self.cond: self.cond.notify_all()

    # ---------- 按网络开门（0.6） ----------
    async def sync_doors(self):
        """照 gateway.json 的 networks 和这台现在的网络，把门对齐：勾了、现在在的开着，别的关上。一扇绑不上就跳过，下回再试。
        关门只是不再收新连接，正在答的照常答完。本机那扇门不归这里管。"""
        if self.syncing:
            self.again = True
            return
        self.syncing = True
        try:
            while True:
                self.again = False
                self.read_conf()
                rows = await asyncio.to_thread(netscan.scan) if self.networks and not self.draining else []
                want = netscan.wanted(rows, self.networks)
                for address in [a for a, d in self.doors.items() if a not in want or want[a].get('id') != d.ident or tuple(want[a].get('accept') or ()) != d.accept]:
                    door = self.doors.pop(address)
                    door.server.close()
                    self.event(tr('关门'), f'{address}:{self.port}[{door.kind}]', door.ident or '', force=True)
                for address, row in want.items():
                    if address in self.doors: continue
                    door = Door(row.get('kind') or 'virtual', address, row.get('accept') or (), row.get('id'))
                    try:
                        door.server = await asyncio.start_server(lambda r, w, d=door: self.handle(r, w, d), address, self.port, ssl=self.context, limit=128 * 1024)
                    except OSError as error:
                        if address not in self.unbound: self.event(tr('开门失败'), f'{address}:{self.port}[{door.kind}]', str(error)[:120], force=True)
                        self.unbound.add(address)
                        continue
                    self.unbound.discard(address)
                    self.doors[address] = door
                    self.event(tr('开门'), f'{address}:{self.port}[{door.kind}]', door.ident or '', force=True)
                self.unbound &= set(want)   # 网络没了、取消勾选了：下回再绑不上要重新记
                was = self.pending
                self.pending = bool(self.networks) and netscan.waiting(rows, self.networks)
                self.retries = self.retries + 1 if self.pending and was else 0
                self.watch_route(bool(self.networks))
                self.keep_awake(self.awake and bool(self.doors))
                if not self.again: break
        finally:
            self.syncing = False

    def keep_awake(self, on):
        """有门开着、又勾了"不让这台睡眠"：起一个 caffeinate 跟着自己；门都关了或取消了就停掉。起不来不要紧（照常服务，只是会睡）。"""
        running = self.caffeinate is not None and self.caffeinate.poll() is None
        if on and not running:
            try: self.caffeinate = subprocess.Popen([CAFFEINATE, '-s', '-w', str(os.getpid())], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError: self.caffeinate = None
        elif not on and self.caffeinate is not None:
            if running:
                self.caffeinate.terminate()
                try: self.caffeinate.wait(2)   # 收掉它，不留僵尸进程
                except subprocess.TimeoutExpired: pass
            self.caffeinate = None

    def watch_route(self, on):
        """勾了网络才听系统的路由通知（地址增减、网卡上下线）：有动静就叫醒 watch，不用轮询。一个都没勾时不听。开不了（别的系统）就只靠每分钟核对一次。"""
        loop = asyncio.get_running_loop()
        if not on:
            if self.route_socket is not None:
                loop.remove_reader(self.route_socket.fileno())
                self.route_socket.close()
                self.route_socket = None
            return
        if self.route_socket is not None: return
        try:
            s = socket.socket(socket.AF_ROUTE, socket.SOCK_RAW, 0)
            s.setblocking(False)
        except (AttributeError, OSError): return

        def readable():
            try:
                while True:
                    message = s.recv(4096)
                    if len(message) > 3 and message[3] in ROUTE_EVENTS: self.poke.set()
            except (BlockingIOError, InterruptedError): pass
            except OSError: self.watch_route(False)
        loop.add_reader(s.fileno(), readable)
        self.route_socket = s

    async def watch(self):
        """看着网络：被叫醒（HUP、路由通知）就重新对一遍门；没人叫也每分钟对一次（顺带发现 gateway.json 被手改）。
        有网络在等路由器应答时 3 秒一次，试 10 回还没有就回到每分钟。"""
        while True:
            try: await asyncio.wait_for(self.poke.wait(), 3 if self.pending and self.retries < 10 else 60)
            except asyncio.TimeoutError: pass
            if self.poke.is_set():
                await asyncio.sleep(1)   # 换网络时通知是一串：等它们到齐再认
                self.poke.clear()
            try: await self.sync_doors()
            except Exception as error:   # 认网络出了没想到的错：门保持原样，下一轮再来，不能让入口退出
                self.event(tr('开门失败'), '-', f'{type(error).__name__}: {error}'[:160])

    # ---------- 上游 ----------
    async def upstream_get(self, node, path, key, timeout=3):
        """问一台一个 GET，返回 (状态码, 正文)；连不上返回 (None, b'')。"""
        writer = None
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', self.nodes[node]['port']), timeout)
            writer.write(f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {key}\r\nConnection: close\r\n\r\n'.encode())
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(-1), timeout)
            head, _, body = raw.partition(b'\r\n\r\n')
            if b'chunked' in head.lower(): body = decode_chunked(body)
            return int(head.split(b' ', 2)[1]), body
        except Exception:   # 连不上、超时、回复读不懂：都当这台问不到
            return None, b''
        finally:
            if writer: writer.close()

    async def online(self, key):
        """各台现在在线的模型；自管版的 oMLX 只列得出已经链进去的，所以这就是实时的。官方版只认 loaded 为真的（strict）。
        短暂缓存，免得每个请求都问一遍。"""
        cached = self.models.get(key)
        if cached and time.monotonic() - cached[0] < MODELS_TTL: return cached[1]
        names = list(self.nodes)
        answers = await asyncio.gather(*(self.upstream_get(n, '/v1/models/status' if self.nodes[n].get('strict') else '/v1/models', key) for n in names))
        table = {}
        for node, (status, body) in zip(names, answers):
            try:
                if status != 200: table[node] = []
                elif self.nodes[node].get('strict'): table[node] = [m['id'] for m in json.loads(body)['models'] if m.get('loaded') is True]
                else: table[node] = [m['id'] for m in json.loads(body)['data']]
            except (ValueError, KeyError, TypeError, AttributeError): table[node] = []
        self.models[key] = (time.monotonic(), table)
        return table

    async def route(self, model, key):
        """(机器, 那台的模型 id) 或 (None, 中文原因)。不写机器时只有一台在线才认，两台都有就要说清楚选哪台。"""
        node, slash, rest = model.partition('/')
        if slash and node in self.nodes:
            if not self.nodes[node].get('strict'): return node, rest
            if rest in (await self.online(key)).get(node, []): return node, rest   # 官方版：写了机器也要查它在线，不然会把模型点起来
            return None, tr('{0} 上的 {1} 现在没有加载。加载和卸载在控制台里操作。', self.nodes[node]['title'], rest)
        table = await self.online(key)
        hits = [n for n, ids in table.items() if model in ids]
        if len(hits) == 1: return hits[0], model
        if hits: return None, tr('{0} 上都有 {1}，请写成 机器/模型，比如 {2}/{3}。多轮缓存跟着机器走，入口不替你选。', tr('、').join(self.nodes[n]['title'] for n in hits), model, hits[0], model)
        where = tr('；').join(tr('{0}：{1}', self.nodes[n]['title'], (tr('、').join(ids) or tr('没有在线的模型'))) for n, ids in table.items())
        return None, tr('{0} 现在没有在线。{1}。加载和卸载在控制台里操作。', model, where)

    # ---------- 一个请求 ----------
    async def handle(self, reader, writer, door=LOCAL_DOOR):
        self.active += 1
        crowd = None
        try:
            if door.kind != 'local':   # 同一个外来来源的连接太多：直接关，不让一台设备把入口的连接占满
                peer = writer.get_extra_info('peername')
                crowd = str(peer[0]) if isinstance(peer, tuple) and peer else '?'
                if door.outside(crowd):
                    self.conns[crowd] = self.conns.get(crowd, 0) + 1
                    if self.conns[crowd] > MAX_CONNECTIONS:
                        self.event(tr('拒绝'), f'{crowd}[{door.kind}]', tr('同时超过 {0} 条连接', MAX_CONNECTIONS))
                        return
                else: crowd = None
            await self.serve_one(reader, writer, door)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError, asyncio.LimitOverrunError, ssl.SSLError):
            pass
        finally:
            if crowd is not None:
                self.conns[crowd] -= 1
                if self.conns[crowd] <= 0: del self.conns[crowd]
            self.active -= 1
            writer.close()
            if self.draining and self.active == 0: os.kill(os.getpid(), signal.SIGTERM)

    async def serve_one(self, reader, writer, door=LOCAL_DOOR):
        start = time.monotonic()
        try: raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 30 if door.kind == 'local' else 10)   # 外来的少等一会儿：连上不说话的不能久占着
        except asyncio.LimitOverrunError: return await reply(writer, 431, tr('请求头太长。'))
        lines = raw.decode('latin-1').split('\r\n')
        try: method, target, _ = lines[0].split(' ', 2)
        except ValueError: return await reply(writer, 400, tr('请求格式不对。'))
        headers = {}
        order = []
        for line in lines[1:]:
            if ':' not in line: continue
            name, value = line.split(':', 1)
            headers[name.strip().lower()] = value.strip()
            order.append((name.strip(), value.strip()))
        path = target.split('?', 1)[0]
        peer = writer.get_extra_info('peername')
        source = str(peer[0]) if isinstance(peer, tuple) and peer else '?'
        outside = door.outside(source)                             # 外来：不是从本机那扇门进来的
        tag = f'{source}[{door.kind}]' if outside else source     # 日志里的来源；外来的标上走的哪条路
        if door.kind != 'local' and not netscan.within(source, door.accept):
            self.event(tr('拒绝'), tag, tr('不是这个网络的私网来源'))
            return await reply(writer, 403, tr('入口只收私网来源，{0} 不在这个网络的地址范围里。', source))

        if method == 'OPTIONS':   # 预检：不要密钥，什么都不转发
            asked = ''.join(c for c in headers.get('access-control-request-headers', '') if c.isalnum() or c in '-, ')[:300]
            return await reply(writer, 204, None, extra=['Access-Control-Allow-Methods: GET, POST, OPTIONS',
                                                         f"Access-Control-Allow-Headers: {asked or 'authorization, content-type, x-api-key'}", 'Access-Control-Max-Age: 600'])
        if path == '/health' and method == 'GET':
            if outside: return await reply(writer, 200, None, {'ok': True})   # 外来的只告诉它"在"，不说这台忙不忙
            return await reply(writer, 200, None, {'ok': True, 'active': self.active - 1, 'draining': self.draining, 'version': HEALTH_VERSION, 'pending': self.pending,
                                                   'awake': self.caffeinate is not None and self.caffeinate.poll() is None,
                                                   'doors': [{'address': d.address, 'kind': d.kind, 'id': d.ident} for d in self.doors.values()]})
        if self.draining: return await reply(writer, 503, tr('入口正在移交，请稍后再试。'))
        if outside:
            self.read_conf()
            wait = self.locked(source)
            if wait:   # 锁着的时候对的密钥也不看：不让它边锁边试
                return await reply(writer, 429, tr('密钥错了太多次，{0} 秒后再试。', wait), extra=[f'Retry-After: {wait}'])

        key = headers.get('x-api-key', '')
        auth = headers.get('authorization', '')
        if auth.lower().startswith('bearer '): key = auth[7:].strip()
        who = self.key_name(key) if key and not (outside and len(key) < MIN_OUTSIDE_KEY) else None
        if who is None:
            if not outside: return await reply(writer, 401, tr('需要有效的子密钥。主密钥不能用来调用入口。'))
            if key: self.failed(source, tag)
            return await reply(writer, 401, tr('需要有效的子密钥。主密钥不能用来调用入口；从别的设备连时，少于 {0} 个字符的子密钥也不认。', MIN_OUTSIDE_KEY))
        if outside: self.fails.pop(source, None)
        if self.is_disabled(key): return await reply(writer, 403, tr('子密钥「{0}」已停用。要再用，在控制台的密钥页点启用。', who))

        if method == 'GET' and path == '/v1/models': return await self.list_models(writer, key)
        if (method, path) not in ALLOWED:
            return await reply(writer, 403, tr('入口只放行模型调用，不放行 {0} {1}。管理、加载和卸载在控制台里操作。', method, path))

        if not outside: return await self.forward(reader, writer, method, target, headers, order, key, who, tag, start)
        digest = hashlib.sha256(key.encode()).hexdigest()
        refused = await self.enter(digest, who, tag)
        if refused: return await reply(writer, 429, refused[0], extra=[f'Retry-After: {refused[1]}'])
        try: return await self.forward(reader, writer, method, target, headers, order, key, who, tag, start)
        finally: await self.leave(digest)

    async def forward(self, reader, writer, method, target, headers, order, key, who, source, start):
        """认过密钥、占好位子以后：读正文、找到那台、转过去、把回复原样传回。"""
        try:
            size = int(headers.get('content-length', ''))
            if size < 0: raise ValueError('negative')
        except ValueError: return await reply(writer, 411, tr('请求要带 Content-Length。'))
        if size > self.max_body:
            await reply(writer, 413, tr('请求太大：{0} MB，上限 {1} MB。', (size // 2**20), (self.max_body // 2**20)))
            # 把对方正在发的读完再关，不然它还没读到这句话就被断开了
            try: await asyncio.wait_for(wait_gone(reader), 10)
            except asyncio.TimeoutError: pass
            return
        if headers.get('expect', '').lower() == '100-continue':
            writer.write(b'HTTP/1.1 100 Continue\r\n\r\n')
            await writer.drain()
        body = await asyncio.wait_for(reader.readexactly(size), 300)
        try: data = await asyncio.to_thread(json.loads, body)
        except (ValueError, RecursionError): return await reply(writer, 400, tr('请求正文不是 JSON。'))
        model = data.get('model') if isinstance(data, dict) else None
        if not isinstance(model, str) or not model: return await reply(writer, 400, tr('请求里没有 model。'))
        node, target_model = await self.route(model, key)
        if node is None: return await reply(writer, 404, target_model)
        data['model'] = target_model
        def encoded():
            try: return json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()
            except UnicodeEncodeError:   # 正文里有半个表情（孤立的代理项）：转义着发，不让请求断在这里
                return json.dumps(data, separators=(',', ':')).encode()
        body = await asyncio.to_thread(encoded)

        up_writer = None
        status, sent, cancelled = 0, 0, False
        try:
            try:
                up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', self.nodes[node]['port']), 5)
            except (OSError, asyncio.TimeoutError):
                status = 503
                return await reply(writer, 503, tr('{0} 现在连不上，或者它的 oMLX 没开。别的机器不受影响。', self.nodes[node]['title']))
            head = [f'{method} {target} HTTP/1.1', 'Host: 127.0.0.1', f'Authorization: Bearer {key}', f'Content-Length: {len(body)}', 'Connection: close']
            head += [f'{n}: {v}' for n, v in order if n.lower() not in DROP]
            up_writer.write(('\r\n'.join(head) + '\r\n\r\n').encode('latin-1') + body)
            await up_writer.drain()
            progress = {'status': 0, 'sent': 0}
            pump = asyncio.create_task(relay(up_reader, writer, progress))
            gone = asyncio.create_task(wait_gone(reader))
            done, _ = await asyncio.wait({pump, gone}, return_when=asyncio.FIRST_COMPLETED)
            if pump in done:
                gone.cancel()
                try: pump.result()
                except (asyncio.IncompleteReadError, ConnectionError):
                    if progress['status']: raise   # 已经开始回了，只能断开
                    status = 502
                    return await reply(writer, 502, tr('{0} 的 oMLX 没答完就断开了（可能正在重启）。稍后再试。', self.nodes[node]['title']))
                status, sent = progress['status'], progress['sent']
            else:
                pump.cancel()
                cancelled = True
                status, sent = 499, progress['sent']
        finally:
            if up_writer: up_writer.close()
            self.note(who, node, target_model, status, len(body), sent, time.monotonic() - start, cancelled, source)

    async def list_models(self, writer, key):
        table = await self.online(key)
        rows = [{'id': f'{node}/{mid}', 'object': 'model', 'owned_by': self.nodes[node]['title']}
                for node, ids in table.items() for mid in ids]
        return await reply(writer, 200, None, {'object': 'list', 'data': rows})

    def note(self, who, node, model, status, up, down, seconds, cancelled, source='?'):
        """一行日志。来源地址接在名字后面（名字@地址）：0.5.1 都是 127.0.0.1，0.6 开局域网后靠它看是哪台设备在用。"""
        model = ''.join(c if c.isprintable() else '?' for c in str(model))[:120]
        line = tr('{0} {1} {2} {3} {4} 上行 {5} 下行 {6} {7} 秒{8}\n', time.strftime('%Y-%m-%d %H:%M:%S'), f'{who}@{source}', node, model, status, up, down, f'{seconds:.1f}', (tr(' 客户端断开') if cancelled else ''))
        self.write_log(line)

    def write_log(self, line):
        try:
            if LOG.exists() and LOG.stat().st_size > 5 * 2**20: LOG.replace(LOG.with_name('gateway.log.old'))
            with LOG.open('a', errors='replace') as f: f.write(line)
        except (OSError, ValueError):
            pass

    def event(self, what, tag, detail='', force=False):
        """调用以外的事也记一行（0.6）：拒绝、锁定、限流、开门、关门。同一个来源同一类 10 秒最多一行，被人狂敲时日志不会刷屏；force 的（开关门、锁定）每次都记。"""
        now = time.monotonic()
        if not force:
            if now - self.logged.get((what, tag), -100) < 10: return
            if len(self.logged) > 1024: self.logged.clear()
            self.logged[(what, tag)] = now
        detail = ''.join(c if c.isprintable() else '?' for c in str(detail))[:200]
        self.write_log(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {what} {tag} {detail}".rstrip() + '\n')


async def relay(up, writer, progress):
    """把上游的回复原样传回去（只改 Connection 头，流式的 chunked 不拆），状态码和字节数记在 progress 里。"""
    head = await up.readuntil(b'\r\n\r\n')
    lines = head.decode('latin-1').split('\r\n')
    progress['status'] = int(lines[0].split(' ')[1])
    kept = [line for line in lines[1:] if line and not line.lower().startswith(('connection:', 'keep-alive:'))]
    writer.write(('\r\n'.join([lines[0], *kept, 'Connection: close']) + '\r\n\r\n').encode('latin-1'))
    await writer.drain()
    while True:
        chunk = await up.read(65536)
        if not chunk: return
        progress['sent'] += len(chunk)
        writer.write(chunk)
        await writer.drain()


async def wait_gone(reader):
    """客户端断开时返回。模型读长资料时可能很久没有输出，所以不能等写失败才发现。"""
    while await reader.read(4096): pass


async def reply(writer, status, message, payload=None, extra=()):
    words = {200: 'OK', 204: 'No Content', 400: 'Bad Request', 401: 'Unauthorized', 403: 'Forbidden', 404: 'Not Found', 411: 'Length Required',
             413: 'Payload Too Large', 429: 'Too Many Requests', 431: 'Request Header Fields Too Large', 503: 'Service Unavailable'}
    more = ''.join(f'{line}\r\n' for line in extra)
    if status == 204:   # 预检的回复没有正文
        writer.write(f'HTTP/1.1 204 {words[204]}\r\n{CORS}{more}Content-Length: 0\r\nConnection: close\r\n\r\n'.encode())
        return await writer.drain()
    data = payload if payload is not None else {'error': {'message': message, 'type': 'gateway', 'code': status}}
    body = json.dumps(data, ensure_ascii=False).encode()
    writer.write(f'HTTP/1.1 {status} {words.get(status, "Error")}\r\nContent-Type: application/json; charset=utf-8\r\n{CORS}{more}Content-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode() + body)
    await writer.drain()


def decode_chunked(body):
    """把 chunked 编码的正文拼回来；格式不对抛 ValueError。"""
    out, rest = b'', body
    while True:
        line, sep, rest = rest.partition(b'\r\n')
        if not sep: raise ValueError('chunked')
        size = int(line.split(b';', 1)[0].strip() or b'0', 16)
        if size == 0: return out
        if size < 0 or len(rest) < size: raise ValueError('chunked')
        out, rest = out + rest[:size], rest[size:]
        if rest[:2] == b'\r\n': rest = rest[2:]


def worker_ports():
    """主机记下的"哪些副机是官方版"（clusterctl 在设置里记它们报的 oMLX 端口）。读不到就当没有。"""
    try: ports = json.loads((RUNTIME / 'clusterctl-settings.json').read_text()).get('worker_ports')
    except (OSError, ValueError, AttributeError): return {}
    return ports if isinstance(ports, dict) else {}


def node_entry(row, official_workers=()):
    """机器表的一行 → 入口要的 {title, port, strict}。本机是官方版时端口按 oMLX 自己的设置，并只放行已加载的模型。"""
    entry = {'title': row.get('title', row['name']), 'port': row['port']}
    if 'ssh' not in row and not ENGINE.managed:
        entry['strict'] = True
        if ENGINE.get('port'): entry['port'] = ENGINE['port']
    elif 'ssh' in row and row['name'] in official_workers: entry['strict'] = True   # 副机是官方版：同样只放行已加载的，不然会被点起来（体检 11-15）
    return entry


async def main_async(args):
    conf = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    listen = args.listen or conf.get('listen', '127.0.0.1')
    port = args.port or conf.get('port', 8900)
    tls = conf.get('tls') or {}
    if args.cert: tls = {'cert': args.cert, 'key': args.key}
    context = None
    if tls.get('cert'):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(tls['cert'], tls.get('key'))
    if listen not in ('127.0.0.1', '::1', 'localhost') and context is None:
        sys.exit(tr('入口要听局域网就必须配 TLS：明文 HTTP 会把密钥和对话都露在网上。'))
    rows = json.loads(CLUSTER_FILE.read_text())['nodes']
    officials = worker_ports()
    gateway = Gateway({r['name']: node_entry(r, officials) for r in rows}, int(conf.get('max_body_mb', 64)) * 2**20)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    # 本机那扇门。listen 写了非本机地址（上面已经保证配了 TLS）时它是一扇宽门：也只收私网来源，回环以外的都按外来的规矩办
    local = LOCAL_DOOR if listen in ('127.0.0.1', '::1', 'localhost') else Door('listen', listen, netscan.WIDE)
    server = await asyncio.start_server(lambda r, w: gateway.handle(r, w, local), listen, port, ssl=context, limit=128 * 1024)
    gateway.server = server
    gateway.port, gateway.context = port, context
    gateway.cond, gateway.poke = asyncio.Condition(), asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGHUP, gateway.poke.set)   # 后端改了 networks 以后发一个 HUP：马上重新对一遍门
    finished = loop.create_future()
    loop.add_signal_handler(signal.SIGTERM, lambda: finished.done() or finished.set_result(None))

    def drain():
        """移交用：不收新请求，已有的答完就退出。"""
        gateway.draining = True
        if gateway.active == 0 and not finished.done(): finished.set_result(None)
    loop.add_signal_handler(signal.SIGUSR1, drain)

    def undrain():
        """关了又马上开（还有请求没答完、这个进程还没退）：不退了，接着收新请求。"""
        if not finished.done(): gateway.draining = False
    loop.add_signal_handler(signal.SIGUSR2, undrain)
    print(tr('入口在听 {0}://{1}:{2}，转发到 {3}', ('https' if context else 'http'), listen, port, tr('、').join(r.get('title', r['name']) for r in rows)), flush=True)
    await gateway.sync_doors()   # 一个网络都没勾（0.5.1 的样子）时这里什么都不开
    watcher = asyncio.create_task(gateway.watch())
    await finished
    watcher.cancel()
    gateway.watch_route(False)
    gateway.keep_awake(False)
    for door in gateway.doors.values(): door.server.close()
    server.close()


def main():
    parser = argparse.ArgumentParser(description=tr('本地模型入口：只做转发'))
    parser.add_argument('--listen')
    parser.add_argument('--port', type=int)
    parser.add_argument('--cert')
    parser.add_argument('--key')
    asyncio.run(main_async(parser.parse_args()))


if __name__ == '__main__':
    main()
