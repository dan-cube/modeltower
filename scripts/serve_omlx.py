#!/usr/bin/env python3
"""Pinned oMLX entry point: local UI, offline models, explicit peers only.

The small discovery and Bonjour overrides enforce this project's
no-LAN-discovery and no-LAN-advertising scope. They do not modify model
computation, SSH validation, or JACCL.

10-29 起 主机 和 副机 各跑一份同样的这个脚本（控制台每次启动 副机 前同步过去）。
多轮缓存（SSD 前缀缓存）由控制台按机器传入：LOCAL_OMLX_CACHE=0 关，LOCAL_OMLX_CACHE_SIZE 是上限。
10-30 起（路线图 1.1-12）：加载、卸载模型只认主密钥；找不到模型、内存不够的报错改成中文。
OMLX_NODE_TITLE 是报错里的机器名，由控制台传入。都在运行时挂上，不改 oMLX 源码。
11-02 起：/lmc/rescan 让 oMLX 重新看模型目录但不卸载（它自带的 /admin/api/reload 会先卸载全部），一台同时放多个模型时用。
"""
import hmac
import json
import logging
import os
from pathlib import Path
import re
import sys

ROOT = Path('/Users/Shared/mlx-cluster')
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
os.environ['OMLX_BASE_PATH'] = str(ROOT / 'omlx-state')
# 10-29：模型一次生成很长的工具调用（比如整份文件）时，推理主进程可能几分钟不回数据（10-28 13:04 实测触发过
# "no rank-zero data for 300s"）。放宽到 60 分钟只当兜底；真卡住由控制台的卡住报警（clusterctl progress）发现。
os.environ.setdefault('OMLX_DISTRIBUTED_REQUEST_READ_TIMEOUT', '3600')

from omlx.cluster import discovery


def manual_only_start(self):
    """Do not start Bonjour, UDP multicast, Tailscale or probe threads."""
    return None


def manual_only_peers(**kwargs):
    return {'peers': [], 'warning': '本项目仅使用已固定身份的两台 Mac；自动发现已关闭。',
            'trusted': False, 'pairing_token': None}


def no_bonjour_publish(self):
    """Do not advertise this server as _omlx._tcp (hostname, version, SSH port)."""
    return False


discovery.DiscoveryService.start = manual_only_start
discovery.discover_all_peers = manual_only_peers
# server.py starts and supervises the publisher whenever distributed
# inference is enabled; patch the class so both entry points stay inert.
discovery.BonjourPublisher.start = no_bonjour_publish
discovery.BonjourPublisher.ensure_running = no_bonjour_publish

from omlx import server

NODE = os.environ.get('OMLX_NODE_TITLE', '这台机器')
FAMILIES = ('DeepSeek', 'MiniMax', 'Qwen')
LOAD_UNLOAD = re.compile(r'/v1/models/[^/]+/(load|unload)/?|/lmc/rescan')
NOT_FOUND = re.compile(r"Model '(?P<want>.+)' not found\. Available models: (?P<have>.*)", re.S)
# dsh 靠这几个英文字样认出"上下文超长"、自动压缩历史，带这些字样的报错一律不改
CONTEXT_WORDS = ('maximum context length', 'prompt is too long', 'context_length_exceeded')
log = logging.getLogger('omlx.serve_omlx')


def short(model):
    """'DeepSeek-V4.1-Flash-1017-MLX-original' → 'DeepSeek'，和 dsh 里"机器 · 模型"的叫法一致；认不出的照原样。"""
    return next((family for family in FAMILIES if model.startswith(family)), model)


class MainKeyOnlyLoadUnload:
    """oMLX 的子密钥不分权限，也能加载、卸载模型，客户端里的 Agent 可能打断别人的对话。这两个地址只放行主密钥；
    主密钥每次现读 oMLX 运行中的值，控制台重置后立刻生效。纯 ASGI，其他请求和流式输出原样穿过。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] != 'POST' or not LOAD_UNLOAD.fullmatch(scope['path']):
            return await self.app(scope, receive, send)
        main = server._server_state.api_key
        headers = dict(scope['headers'])
        auth = headers.get(b'authorization', b'')
        given = auth[7:] if auth[:7].lower() == b'bearer ' else headers.get(b'x-api-key', b'')
        if main is None or hmac.compare_digest(given.strip(), main.encode()):
            return await self.app(scope, receive, send)
        log.warning('%s %s → 403: 不是主密钥，拒绝加载或卸载', scope['method'], scope['path'])
        body = json.dumps({'error': {'message': '子密钥不能加载或卸载模型，请在ModelTower操作。 / Sub-keys cannot load or unload models. Use ModelTower.',
                                     'type': 'permission_error', 'param': None, 'code': None}},
                          ensure_ascii=False).encode()
        await send({'type': 'http.response.start', 'status': 403,
                    'headers': [(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode())]})
        await send({'type': 'http.response.body', 'body': body})


def chinese(status, detail):
    """只改文字，状态码和错误类型不变，客户端照样按状态码归类。"""
    if not isinstance(detail, str) or any(word in detail for word in CONTEXT_WORDS): return detail
    if status == 404 and (m := NOT_FOUND.fullmatch(detail)):
        have = [short(x.strip()) for x in m['have'].split(',') if x.strip() not in ('', '(none)')]
        online = f'在线的是：{"、".join(have)}' if have else '现在没有模型在线'
        online_en = f'online: {", ".join(have)}' if have else 'no model is online'
        return (f'{NODE} 上现在没有加载 {short(m["want"])}（{online}）。'
                '请在ModelTower加载，或在 dsh 里改选在线的那一项。'
                f' / {short(m["want"])} is not loaded on {NODE} ({online_en}). Load it in ModelTower, or pick a model that is online.')
    if status == 507:
        return (f'{NODE} 内存不够，这次没有执行。请到ModelTower看内存；如果是对话太长，开个新对话再试。'
                f' / Not enough memory on {NODE}; the request did not run. Check memory in ModelTower; if the conversation is too long, start a new one. （原文 Original: {detail}）')
    return detail


english_errors = server.app.exception_handlers[server.HTTPException]


async def chinese_errors(request, exc):
    exc.detail = chinese(exc.status_code, exc.detail)
    return await english_errors(request, exc)


@server.app.post('/lmc/rescan')
async def rescan_keeping_loaded():
    """和 oMLX 下载完模型后的做法一样（server.py _refresh_models_after_download）：只重新发现，已加载和加载中的不动，
    目录里没有了又没加载的从列表去掉。只认主密钥，由 MainKeyOnlyLoadUnload 把关。"""
    pool, manager = server._server_state.engine_pool, server._server_state.settings_manager
    pool.discover_models([str(d) for d in pool._model_dirs], manager.get_pinned_model_ids())
    pool.apply_settings_overrides(manager)
    return {'status': 'ok', 'models': pool.model_count}


server.app.exception_handlers[server.HTTPException] = chinese_errors
server.app.add_middleware(MainKeyOnlyLoadUnload)

from omlx.cli import main

if __name__ == '__main__':
    sys.argv = ['omlx', 'serve', '--host', '127.0.0.1', '--port', '8800',
                '--base-path', str(ROOT / 'omlx-state'),
                '--model-dir', str(ROOT / 'serving-models'), '--no-hf-cache',
                '--max-concurrent-requests', '2', '--memory-guard', 'safe',
                '--log-level', 'info']
    # 没传或写法不对就什么都不带，按 settings.json（oMLX 自己的设置）来，免得手动启动时按错的上限淘汰笔记。
    # --no-cache 会把"关"存进 settings.json，之后只给上限它还是关的；--paged-ssd-cache-dir 才会把"开"存回去，所以开的时候一起带。
    cache, size = [], os.environ.get('LOCAL_OMLX_CACHE_SIZE')
    if os.environ.get('LOCAL_OMLX_CACHE') == '0': cache = ['--no-cache']
    elif size in ('50GB', '100GB', '200GB', '500GB'):
        cache = ['--paged-ssd-cache-dir', str(ROOT / 'omlx-state' / 'cache'), '--paged-ssd-cache-max-size', size]
    sys.argv[-2:-2] = cache
    main()
