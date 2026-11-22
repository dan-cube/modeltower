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
· 听局域网必须配 TLS：明文 HTTP 会把密钥和对话都露在网上。
"""
from i18n import tr
import argparse
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import signal
import ssl
import sys
import time

from paths import CLUSTER_FILE, RUN_DIR, RUNTIME
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


class Gateway:
    def __init__(self, nodes, max_body):
        self.nodes = nodes              # {机器: {'title':…, 'port':…}}
        self.max_body = max_body
        self.keys, self.keys_mtime = {}, None
        self.disabled, self.disabled_mtime = frozenset(), None
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

    def is_disabled(self, key):
        """这把子密钥在停用名单里（0.5.1）。名单在 gateway.json 的 disabled_keys：{SHA-256: {name, at}}，也认列表。
        文件没了、读不懂、格式不对都当没有名单（不挡）：名单坏了不能把能用的密钥全挡在外面，控制台的体检会报。"""
        try: mtime = CONFIG.stat().st_mtime_ns
        except OSError: mtime = 0
        if mtime != self.disabled_mtime:
            rows = None
            if mtime:
                try: rows = json.loads(CONFIG.read_text()).get('disabled_keys')
                except (OSError, ValueError, AttributeError): rows = None
            self.disabled = frozenset(k.lower() for k in rows if isinstance(k, str)) if isinstance(rows, (dict, list)) else frozenset()
            self.disabled_mtime = mtime
        return bool(self.disabled) and hashlib.sha256(key.encode()).hexdigest() in self.disabled

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
    async def handle(self, reader, writer):
        self.active += 1
        try:
            await self.serve_one(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError, asyncio.LimitOverrunError, ssl.SSLError):
            pass
        finally:
            self.active -= 1
            writer.close()
            if self.draining and self.active == 0: os.kill(os.getpid(), signal.SIGTERM)

    async def serve_one(self, reader, writer):
        start = time.monotonic()
        try: raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 30)
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

        if method == 'OPTIONS':   # 预检：不要密钥，什么都不转发
            asked = ''.join(c for c in headers.get('access-control-request-headers', '') if c.isalnum() or c in '-, ')[:300]
            return await reply(writer, 204, None, extra=['Access-Control-Allow-Methods: GET, POST, OPTIONS',
                                                         f"Access-Control-Allow-Headers: {asked or 'authorization, content-type, x-api-key'}", 'Access-Control-Max-Age: 600'])
        if path == '/health' and method == 'GET':
            return await reply(writer, 200, None, {'ok': True, 'active': self.active - 1, 'draining': self.draining})
        if self.draining: return await reply(writer, 503, tr('入口正在移交，请稍后再试。'))

        key = headers.get('x-api-key', '')
        auth = headers.get('authorization', '')
        if auth.lower().startswith('bearer '): key = auth[7:].strip()
        who = self.key_name(key) if key else None
        if who is None: return await reply(writer, 401, tr('需要有效的子密钥。主密钥不能用来调用入口。'))
        if self.is_disabled(key): return await reply(writer, 403, tr('子密钥「{0}」已停用。要再用，在控制台的密钥页点启用。', who))

        if method == 'GET' and path == '/v1/models': return await self.list_models(writer, key)
        if (method, path) not in ALLOWED:
            return await reply(writer, 403, tr('入口只放行模型调用，不放行 {0} {1}。管理、加载和卸载在控制台里操作。', method, path))

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
        try:
            if LOG.exists() and LOG.stat().st_size > 5 * 2**20: LOG.replace(LOG.with_name('gateway.log.old'))
            with LOG.open('a', errors='replace') as f: f.write(line)
        except (OSError, ValueError):
            pass


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
             413: 'Payload Too Large', 431: 'Request Header Fields Too Large', 503: 'Service Unavailable'}
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
    server = await asyncio.start_server(gateway.handle, listen, port, ssl=context, limit=128 * 1024)
    gateway.server = server
    loop = asyncio.get_running_loop()
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
    await finished
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
