#!/usr/bin/env python3
"""ModelTower：每台 Mac 跑一个 oMLX，单机加载一个模型。机器表在 cluster.json，型号档案在 config/model-profiles。
现在的部署：主机（主机）和 副机（副机），每台单机加载 DeepSeek、MiniMax 或 Qwen。

2026-10-29 用户决定（方案 A 实测后）：不再用双机流水线。单机比双机快（生成 37.4 对 33.2 token/s，
读入 456–498 对 380–407），还能读图；两台都能单机跑这两个模型，控制台同时管两台。
  · 每台同一时间只放一个大模型：DeepSeek 约 270 GB（Engram 查询表留在 SSD），MiniMax 约 235 GB，
    两个加起来超过一台的上限。同一个模型可以两台都开。
  · 副机 的 oMLX 只听它自己的 127.0.0.1:8800，主机 经 SSH 隧道 127.0.0.1:8820 访问，不动两台防火墙。
  · 两台用同一套密钥：以 主机 的 settings.json 为准，副机 每次启动前同步；运行中的改动两台一起改。
  · 模型只在被控制台加载的那台机器上"可见"：加载时链进那台的 serving-models，卸载时撤掉。客户端点到
    没加载的模型只会报"找不到模型"，不会让 oMLX 自己把它加载进来、挤掉那台正在用的模型。
  · 每个模型两个开关（10-31）：MTP、查询表放 SSD，按"机器 × 模型"分别存。两项都要重新加载模型才生效。
服务和模型都由用户手动开关：不开机自启，打开控制台也不自动加载。Qwen 10-31 起以发布精度 BF16 复制进 oMLX；
Ollama 里那份原件保留作对比，Ollama 只在 主机 上，这里只显示它占的内存，内存不够时经用户确认后请它释放
（Ollama 看不见 oMLX 占的内存）。
"""
from i18n import lang, tr
import argparse
import base64
import contextlib
import ctypes
import errno
import fcntl
import hashlib
import io
import json
import os
import platform
from pathlib import Path
import re
import secrets
import select
import shlex
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tarfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

PROJECT = Path(__file__).resolve().parent.parent
from paths import CLUSTER_FILE, CONFIG_DIR, FAMILIES_DIR, MODEL_DATA, REPORTS, RUN_DIR, RUNTIME as ROOT, control_path  # noqa: E402
import omlx_adapter  # noqa: E402
import brand  # noqa: E402
import updates  # noqa: E402  第 3b 期：安装、更新中心、联网兼容表
RUN_DIR.mkdir(parents=True, exist_ok=True)
SERVING = ROOT / 'serving-models'
STATE = ROOT / 'omlx-state'
SETTINGS = ROOT / 'clusterctl-settings.json'
MODEL_SETTINGS = STATE / 'model_settings.json'
OLLAMA = 'http://127.0.0.1:11434'
SERVE_SCRIPT = PROJECT / 'scripts/serve_omlx.py'
# 每台的 oMLX 都只听它自己的 127.0.0.1:8800。本机直接访问；远程机器经 SSH 隧道，用机器表里的 port 访问（8810 是 H3 视频页）
OMLX_PORT = int(os.environ.get('LMC_OMLX_PORT') or 8800)  # 环境变量只给测试用
# 第 3 期：这台的 oMLX 是哪种（自管 managed / 官方 official / 没装 none），见 omlx_adapter.py 和草案 10.1。
# 你两台运行目录里有 omlx-state，是自管：下面凡是"if official(node)"的分支都不走，老路一个字不变。签名这里不核（几秒），体检和对接时才核
ENGINE_HERE = omlx_adapter.detect(ROOT, check_signature=False)
MANAGED_HERE = ENGINE_HERE.managed
# 这里的请求都发给本机（oMLX、Ollama、隧道），不走系统代理：主密钥在请求里，127.0.0.1 不在 macOS 代理的默认例外里
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
# 显示名不能带任何能断行的字符：authorized_keys 是一行一条，夹一个 \r 就可能多出一条全权限的钥匙
SAFE_TEXT = r"""[^"'\\`$\x00-\x1f\x7f\x85\u2028\u2029]+"""
# 机器表的字段：会进文件名、命令行和远程 shell，只收安全的写法
NODE_FIELDS = {'name': r'[a-z0-9][a-z0-9_-]*', 'title': SAFE_TEXT, 'role': SAFE_TEXT,
               'ssh': r'[A-Za-z0-9][A-Za-z0-9._-]*', 'ip': r'[A-Za-z0-9][A-Za-z0-9.-]{0,63}', 'interface': r'[a-z0-9]+', 'link': SAFE_TEXT,
               'dsh_provider': r'[A-Za-z0-9_-]+', 'dsh_title': SAFE_TEXT, 'transport': r'rpc', 'legacy_ssh': r'[A-Za-z0-9][A-Za-z0-9._-]*',
               'mdns': r'[A-Za-z0-9][A-Za-z0-9-]{0,62}\.local', 'kind': r'hub|worker'}
NAME_MAX = 32   # 新添加的机器名最长多少；已在机器表里的不按它查，见 machine_add
ADDRESS_MAX = 4   # 一台最多登记几个地址：网线、Wi-Fi，再留两个备用
LINK_VALUES = ('有线', 'Wi-Fi', '雷雳')   # 机器表里存的连接方式，显示时才 tr()；界面的下拉框从 status 的 links 拿
LINK_CODES = ('wired', 'wifi', 'thunderbolt')   # 给应用的代号：status、machine addresses 里不放中文，存表时再换回来


def link_code(value): return LINK_CODES[LINK_VALUES.index(value)] if value in LINK_VALUES else value
def link_value(code): return LINK_VALUES[LINK_CODES.index(code)] if code in LINK_CODES else code
def coded(rows): return [{**a, 'link': link_code(a['link'])} for a in rows]
# 地址列表只收 IPv4：挑地址时几个地址同时试，写名字的话要先解析，.local 解析可能卡好几秒
ADDRESS_IP = r'(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}'
NEED_FACTS = ('id', 'native', 'kv', 'size_gib')   # 模型事实里内存预判离不开的；缺了这个模型先不列
MODEL_KEY = r'[a-z0-9][a-z0-9_-]{0,31}'   # 模型的名字：进命令行、文件名和 rpc（rpc.py 的 MODEL 同一条）
DEFAULT_ETA = {'zh': '几十秒到几分钟', 'en': 'tens of seconds to a few minutes'}   # 没有本机实测时，重新加载要下线多久
LOAD_WARNINGS = []   # 读模型说明时跳过了什么，给体检和 status


def config_failure(text):
    """配置读不了：应用调 status --json 时只认 JSON，也要让它看到原因。"""
    if '--json' in sys.argv: print(json.dumps({'ok': False, 'error': text}, ensure_ascii=False))
    else: print('✕ ' + text, file=sys.stderr)
    sys.exit(1)


def address_problem(rows):
    """地址列表哪里不对，对了返回 None。每项正好有 ip、interface、link，ip 不重复。"""
    if not isinstance(rows, list) or not 1 <= len(rows) <= ADDRESS_MAX: return tr('要 1 到 {0} 个地址', ADDRESS_MAX)
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'ip', 'interface', 'link'}: return tr('每个地址要写 ip、interface、link')
        if not (re.fullmatch(ADDRESS_IP, str(row['ip'])) and re.fullmatch(NODE_FIELDS['interface'], str(row['interface']))
                and re.fullmatch(NODE_FIELDS['link'], str(row['link']))):
            return tr('地址 {0} 的写法不对', row.get('ip'))
    if len({row['ip'] for row in rows}) != len(rows): return tr('地址重复了')
    return None


def here_python():
    """这台跑控制台后端用的 Python：早期装法是运行目录的虚拟环境；新用户没有它，用正在跑这个脚本的那个（后端只用标准库）。"""
    venv = ROOT / '.venv/bin/python'
    return venv if venv.exists() else Path(sys.executable)


def first_cluster():
    """第 4 期（草案 11.1）：全新的机器第一次打开还没有机器表，就建一份只有本机的——装上就是单机，不用先选角色（草案 3.2）。
    名字取系统的本地主机名，显示名取电脑名。自管版不走这里：那是早期装法的两台，机器表丢了要人来看。"""
    def scutil(key):
        try: return subprocess.run(['scutil', '--get', key], capture_output=True, text=True, errors='replace', timeout=5).stdout.strip()
        except (OSError, subprocess.TimeoutExpired): return ''
    host = os.environ['LMC_FIRST_HOST'] if 'LMC_FIRST_HOST' in os.environ else scutil('LocalHostName')   # 环境变量只给测试用
    name = re.sub(r'[^a-z0-9_-]+', '-', host.lower()).strip('-_')[:NAME_MAX].rstrip('-_') or 'mac'
    if name == 'all': name = 'mac'
    title = next((text for text in (os.environ['LMC_FIRST_TITLE'] if 'LMC_FIRST_TITLE' in os.environ else scutil('ComputerName'), host) if text and len(text) <= 40 and re.fullmatch(SAFE_TEXT, text)), 'Mac')
    row = {'name': name, 'title': title, 'role': '主机', 'port': OMLX_PORT}
    if re.fullmatch(NODE_FIELDS['mdns'], host + '.local'): row['mdns'] = host + '.local'
    CLUSTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CLUSTER_FILE.with_name(f'{CLUSTER_FILE.name}.{os.getpid()}.tmp')   # 打开控制台时几条命令同时跑，各写各的临时文件
    temporary.write_text(json.dumps({'nodes': [row]}, indent=2, ensure_ascii=False) + '\n')
    os.replace(temporary, CLUSTER_FILE)


def load_cluster():
    """机器表 cluster.json：这台知道的机器，顺序就是界面上的顺序，字段说明见 config/cluster.example.json。
    没写 ssh 的是本机。现在必须正好一台；只控制、本机不跑模型的 MacBook 是阶段 4 的事。"""
    if not MANAGED_HERE and not CLUSTER_FILE.exists():
        try: first_cluster()
        except OSError as exc: config_failure(tr('建不了机器表 {0}（{1}）。', CLUSTER_FILE, exc))
    try: rows = json.loads(CLUSTER_FILE.read_text())['nodes']
    except (OSError, ValueError, KeyError, TypeError) as exc:
        config_failure(tr('读不了机器表 {0}（{1}）。照 {2}/cluster.example.json 写一份。', CLUSTER_FILE, exc, CONFIG_DIR))
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        config_failure(tr('机器表 {0} 的 nodes 要是一个列表，每台一个对象。', CLUSTER_FILE))
    nodes = {}
    for row in rows:
        wrong = [k for k, rx in NODE_FIELDS.items() if (k in row or k in ('name', 'title', 'role')) and not re.fullmatch(rx, str(row.get(k, '')))]
        if not isinstance(row.get('port'), int): wrong.append('port')
        if not isinstance(row.get('omlx_port', 0), int): wrong.append('omlx_port')
        if row.get('transport') and 'ssh' not in row: wrong.append(tr('transport 要带 ssh'))
        if row.get('name') in nodes or row.get('name') == 'all': wrong.append('name')
        if row.get('ip') and not (row.get('interface') and row.get('link')): wrong.append(tr('ip 要带 interface 和 link'))
        if 'addresses' in row and address_problem(row['addresses']): wrong.append('addresses')
        if wrong: config_failure(tr('机器表 {0} 里 {1} 的这些字段不对：{2}。', CLUSTER_FILE, row.get('name'), tr('、').join(wrong)))
        nodes[row['name']] = row
    if sum('ssh' not in row for row in nodes.values()) != 1: config_failure(tr('机器表 {0} 里要有正好一台没写 ssh 的本机。', CLUSTER_FILE))
    return nodes


def pick(value):
    """模型说明里给人看的文字写成 {zh, en}，按界面语言取（英文缺了用中文）。{eta}、{title} 这样的占位照留。"""
    if isinstance(value, dict) and 'zh' in value and set(value) <= {'zh', 'en'}:
        return (value.get('en') or value['zh']) if lang() == 'en' else value['zh']
    if isinstance(value, list): return [pick(v) for v in value]
    if isinstance(value, dict): return {k: pick(v) for k, v in value.items()}
    return value


def read_layer(path):
    """读一份模型说明。没有就是空；读不了、格式不对只跳过这一份，记进 LOAD_WARNINGS，控制台照常运行。"""
    try: data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError: return {}
    except (OSError, ValueError) as exc:
        LOAD_WARNINGS.append(tr('模型说明 {0} 读不了，先不用它（{1}）。', path, exc))
        return {}
    if isinstance(data, dict): return data
    LOAD_WARNINGS.append(tr('模型说明 {0} 的格式不对，先不用它。', path))
    return {}


def family_shape(fam):
    """①出厂说明转成有效说明的形状：开关声明 → mtp、ssd、ssd_locked、load、info。没声明的开关就是没有（界面上不显示）。"""
    switches = {s['key']: s for s in fam.get('switches', []) if isinstance(s, dict) and s.get('key') in ('mtp', 'ssd')}   # 2a 只认这两个（OPTIONS），别的开关键 2b 再开
    mtp, ssd = switches.get('mtp'), switches.get('ssd')
    load = {k: v for k, v in (('always', dict(fam.get('load_always') or {})),
                              ('ssd_key', ssd and ssd.get('mode') == 'free' and ssd.get('setting')),
                              ('official_ssd_key', ssd and ssd.get('official_setting')),   # 3a：自管锁定、官方可设的（DeepSeek 的 Engram 表）
                              ('when_mtp', dict(mtp.get('when_on') or {}) if mtp else None)) if v}
    shape = {'family': fam.get('family', 'generic'), 'title': fam.get('title', ''), 'about': fam.get('about', []), 'switches': sorted(switches),
             'mtp': bool(mtp.get('default', True)) if mtp else False,
             'ssd': ('locked' if ssd.get('mode') == 'locked' else bool(ssd.get('default', False))) if ssd else None,
             'info': {k: s['info'] for k, s in switches.items() if s.get('info')},
             'note': '', 'eta': DEFAULT_ETA, 'help': '', 'context_why': '', 'context_notes': {}, 'tested_up_to': {}, 'verified': [], 'precision': {}}
    if load: shape['load'] = load
    if ssd and ssd.get('locked_reason'): shape['ssd_locked'] = ssd['locked_reason']
    if fam.get('warmup'): shape['warmup'] = True
    return shape


class ModelSet(dict):
    """一台的模型清单 {名字: 有效说明}。遍历只给这一台的；查不在这台清单里的名字时退到 KNOWN（别的机器的、自动识别的），
    免得只在副机上有的模型在主机那份清单里一查就出错（2b：每台一份清单）。"""
    def __missing__(self, key): return KNOWN[key]


KNOWN = {}   # 这条命令里见过的所有模型：主机的、副机的、自动识别的；先见到的为准
FAMILIES = {}   # ①出厂说明 {家族: 原文}
HOST_FACTS = {}   # 这台的②原文，副机没有自己的②时借它
HOST_NOTES = {}   # 这台的③原文，副机没有自己的③时借它


def eta_text(seconds):
    """按本机实测的加载用时说重新加载要下线多久（没写"你的说法"、也不是迁移来的时候）。"""
    return tr('约 {0} 秒', round(seconds)) if seconds < 90 else tr('约 {0} 分钟', max(2, round(seconds / 60)))


def merge_one(families, fact, note):
    """一个模型的三层合并；字段的类型不对（手写坏了）返回 None。"""
    try:
        fam = families.get(fact['family'] if isinstance(fact.get('family'), str) else 'generic') or families.get('generic') or {}
        model = {k: pick(v) for k, v in {**family_shape(fam), **fact, **note}.items()}
        if 'eta' not in note and isinstance(fact.get('load_seconds'), (int, float)) and fact['load_seconds'] > 0: model['eta'] = eta_text(fact['load_seconds'])
        if isinstance(model.get('ssd_locked'), str): model['ssd_locked'] = model['ssd_locked'].replace('{title}', model['title'])
        model.setdefault('context', model['native'])
    except (TypeError, ValueError, AttributeError, KeyError):
        return None
    good = (all(isinstance(model.get(f), str) for f in ('id', 'title', 'note', 'eta')) and all(isinstance(model.get(f), int) and not isinstance(model.get(f), bool) for f in ('native', 'context'))
            and all(isinstance(model.get(f), (int, float)) for f in ('kv', 'size_gib')))
    return model if good else None


def merge_models(families, facts, notes):
    """三层合并成有效说明：①家族说明打底，盖上②这个模型的事实，再盖上③你的编辑；逐个顶层字段，上层盖住下层。
    顺序就是②里的顺序。缺了内存预判离不开的事实、或②写坏了，这个模型先不列；③写坏了只不用你的说法（都记进 LOAD_WARNINGS）。"""
    if not isinstance(notes, dict): notes = {}
    models = ModelSet()
    for key, fact in (facts.items() if isinstance(facts, dict) else ()):
        missing = [f for f in NEED_FACTS if not isinstance(fact, dict) or f not in fact]
        if not re.fullmatch(MODEL_KEY, str(key)) or missing:
            LOAD_WARNINGS.append(tr('模型 {0} 的事实缺 {1}，先不列。', key, tr('、').join(missing) or tr('合法的名字')))
            continue
        note = notes.get(key) if isinstance(notes.get(key), dict) else {}
        model = merge_one(families, fact, note)
        if model is None and note:
            LOAD_WARNINGS.append(tr('模型 {0} 的你的说法写得不对，先不用它。', key))
            model = merge_one(families, fact, {})
        if model is None:
            LOAD_WARNINGS.append(tr('模型 {0} 的事实写得不对，先不列。', key))
            continue
        models[key] = model
    return models


def load_models():
    """模型说明三层合并（草案 9.2，已定第 27 条）：①出厂说明 config/model-families/<家族>.json，②模型事实 model-facts.json，
    ③你的编辑 model-notes.json（②③在运行目录，每台一份）。上层盖住下层，逐个顶层字段；合并出来的字段和旧的型号档案一样，下游不用改。
    顺序就是 model-facts.json 里的顺序。GB 一律是 1024³。ssd：'locked' 恒开；None 没有这个开关；true/false 可开关的默认值。
    这里读的是这台自己的②③；副机的在 models_for()，自动识别的在 discover()。"""
    global HOST_FACTS, HOST_NOTES
    FAMILIES.update({path.stem: read_layer(path) for path in sorted(FAMILIES_DIR.glob('*.json'))})
    facts, notes = (read_layer(MODEL_DATA / name).get('models') or {} for name in ('model-facts.json', 'model-notes.json'))
    HOST_FACTS = facts if isinstance(facts, dict) else {}
    HOST_NOTES = notes if isinstance(notes, dict) else {}
    models = merge_models(FAMILIES, facts, notes)
    KNOWN.update(models)
    return models


NODES = load_cluster()
LOCAL = next(name for name, meta in NODES.items() if 'ssh' not in meta)
if not MANAGED_HERE and ENGINE_HERE.get('port'): NODES[LOCAL]['port'] = ENGINE_HERE['port']   # 官方版的端口按它自己的设置（默认 8000），只改内存里的，不写机器表
HERE_PORT = OMLX_PORT if MANAGED_HERE else (ENGINE_HERE.get('port') or OMLX_PORT)   # 这台 oMLX 实际听的端口：探测、受限钥匙的放行、报给主机的都用它
NODE_CHOICES = '|'.join(NODES)
EVERY = {1: tr('这台'), 2: tr('两台')}.get(len(NODES), tr('{0} 台', len(NODES)))  # 界面文字里的"两台"，按机器数变
SOLO = len(NODES) == 1   # 只有一台：句子换成单机的说法，不说"这台都……"（审查 F2）
MODELS = load_models()   # 当前这台的清单：平时是本机的；按某台做事时由 models_of() 换成那台的
HOST_MODELS = MODELS
MODEL_TITLES = [m['title'] for m in MODELS.values()]

# ---------- 每台一份模型清单（2b，草案 9.4）----------
# 每台列：②里有、文件在这台的模型，加上这台 oMLX 列出来、②里没有的（自动识别）。副机的②③和"有哪些模型文件夹"随完整刷新
# 那一次 SSH 带回（REMOTE_PARTS 的 have、facts、notes），在 RUN_DIR/node-models 留一份给快速刷新和命令用；读不到就照旧用这台的。
NODE_DATA_DIR = RUN_DIR / 'node-models'
REMOTE_DATA = {}   # 副机 → {'have': [模型文件夹名], 'facts': ②|None, 'notes': ③|None}
MERGED = {}   # 副机 → 合并好的清单（REMOTE_DATA 变了就重算）
AUTO = {}   # 机器 → {名字: 有效说明}：这条命令里自动识别出来的
SCOPE = [None]   # models_of() 现在按哪台


def node_data(node):
    """副机上次带回来的②③和模型文件夹清单；没有就是空。"""
    if node not in REMOTE_DATA:
        try: data = json.loads((NODE_DATA_DIR / f'{node}.json').read_text())
        except (OSError, ValueError): data = {}
        REMOTE_DATA[node] = data if isinstance(data, dict) else {}
    return REMOTE_DATA[node]


def keep_node_data(node, data):
    """记下这次带回来的；和上次一样就不写文件。"""
    REMOTE_DATA[node] = data
    MERGED.pop(node, None)
    path = NODE_DATA_DIR / f'{node}.json'
    text = json.dumps(data, ensure_ascii=False, sort_keys=True)
    try:
        if path.exists() and path.read_text() == text: return
        NODE_DATA_DIR.mkdir(exist_ok=True)
        temporary = path.with_name(f'{path.name}.{os.getpid()}.tmp')
        temporary.write_text(text)
        os.replace(temporary, path)
    except OSError:
        pass


def models_for(node):
    """这台的模型说明：本机读自己的②③；副机用它带回来的②③（它没有哪份就借这台的那份）；再加上自动识别的。"""
    if is_local(node): base = HOST_MODELS
    else:
        data = node_data(node)
        if data.get('facts') is None and data.get('notes') is None: base = HOST_MODELS
        else:
            if node not in MERGED: MERGED[node] = merge_models(FAMILIES, data['facts'] if data.get('facts') is not None else HOST_FACTS,
                                                               data['notes'] if data.get('notes') is not None else HOST_NOTES)
            base = MERGED[node]
    out = ModelSet(base)
    out.update(AUTO.get(node, {}))
    for key, model in out.items(): KNOWN.setdefault(key, model)
    return out


@contextlib.contextmanager
def models_of(node):
    """按这台做事：这段代码里的 MODELS 是这台的清单，出来后换回去。"""
    global MODELS
    mine = models_for(node)
    before, SCOPE[0], MODELS = (MODELS, SCOPE[0]), node, mine
    try: yield MODELS
    finally: MODELS, SCOPE[0] = before


def node_has(node, n=None):
    """这台有哪些模型（按清单的顺序）：模型文件夹在这台的，加上这台 oMLX 列出来的。读不到副机的文件夹清单时照旧全列。
    n 是 node_snapshot() 的结果（带着 oMLX 的列表）。"""
    models, rows = models_for(node), (n or {}).get('rows') or {}
    if is_local(node): files = {m['id'] for m in models.values() if (ROOT / 'models' / m['id'] / 'config.json').is_file()}
    else:
        have = node_data(node).get('have')
        if not isinstance(have, list): return list(models)
        files = set(have)
    return [key for key, m in models.items() if m['id'] in files or m['id'] in rows]


def model_key(mid):
    """自动识别的模型取个短名字（进命令行、文件名和 rpc，见 MODEL_KEY）：小写，只留字母、数字和连字符；
    超长或已被别的模型占了，加 6 位校验码。同一个 id 每次都得到同一个名字。"""
    for key, model in KNOWN.items():
        if model['id'] == mid: return key
    base = re.sub(r'[^a-z0-9]+', '-', mid.lower()).strip('-') or 'model'
    if len(base) <= 32 and base not in KNOWN: return base
    return base[:25].rstrip('-') + '-' + hashlib.sha256(mid.encode()).hexdigest()[:6]


def kv_per_token(config):
    """每 token 缓存的估算（字节）：标准注意力的 K、V 各一份 × 层数 × KV 头数 × 每头维度 × 2 字节。压缩注意力、
    线性注意力这类架构会差几倍（摸底 N12），所以界面上标"估算"；读不到 config 按 128 KB 算。"""
    try:
        heads = int(config['num_attention_heads'])
        dim = int(config.get('head_dim') or int(config['hidden_size']) // heads)
        return 2 * int(config['num_hidden_layers']) * int(config.get('num_key_value_heads') or heads) * dim * 2
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return 131072


def model_config(node, path):
    """模型文件夹里的 config.json（自动识别用）；读不到返回 {}。"""
    if not path: return {}
    try:
        if is_local(node): text = Path(path, 'config.json').read_text()
        else: text = ssh_to(node, 'cat ' + shlex.quote(f'{path}/config.json'), timeout=10).stdout
        data = json.loads(text)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    return data if isinstance(data, dict) else {}


def known_facts(mid):
    """①里写明了规格的已发布模型（按 id 对上）：(家族, 事实)。新机器上装的是同一个模型时，开关和内存预判和你两台一样。"""
    for family, fam in FAMILIES.items():
        fact = (fam.get('known') or {}).get(mid)
        if isinstance(fact, dict): return family, fact
    return None


def identify(node, row):
    """按 oMLX 报的（类型、上下文上限、估算大小）和 config.json 认一个模型，得出它的②。家族按 config 的 model_type
    对上①写明的架构，对不上用通用说明（没有开关：陌生架构的开关没验证过）。认过的存在 RUN_DIR/node-models/<机器>.auto.json，
    同一个 id、同一个文件夹下次直接用，不再读 config.json。"""
    path = NODE_DATA_DIR / f'{node}.auto.json'
    try: seen = json.loads(path.read_text())
    except (OSError, ValueError): seen = {}
    if not isinstance(seen, dict): seen = {}
    hit = seen.get(row['id'])
    if isinstance(hit, dict) and hit.get('path') == row.get('model_path') and isinstance(hit.get('fact'), dict): return dict(hit['fact'])
    fact = identify_fresh(node, row)
    try:
        NODE_DATA_DIR.mkdir(exist_ok=True)
        write_json(path, {**seen, row['id']: {'path': row.get('model_path'), 'fact': fact}})
    except OSError:
        pass
    return fact


def identify_fresh(node, row):
    mid, config = row['id'], model_config(node, row.get('model_path'))
    text = config.get('text_config') if isinstance(config.get('text_config'), dict) else config
    kind = config.get('model_type') or row.get('config_model_type') or ''
    known = known_facts(mid)
    family = known[0] if known else next((name for name, fam in FAMILIES.items() if kind and kind in ((fam.get('match') or {}).get('model_type') or [])), 'generic')
    size = row.get('estimated_size') or row.get('actual_size') or 0
    fact = {'family': family, 'id': mid, 'title': mid, 'native': int(row.get('model_context_length') or text.get('max_position_embeddings') or 32768),
            'kv': kv_per_token(text), 'kv_guess': True, 'size_gib': max(round(size / GIB, 1), 1), 'mtp_gib': 0, 'ssd_gib': max(round(size / GIB, 1), 1),
            'vision': row.get('model_type') == 'vlm' or 'vision_config' in config, 'auto': True}
    if known:
        fact.update(known[1])
        fact.pop('kv_guess', None)
        fact.pop('title', None)
    else: fact['context'] = min(fact['native'], CONTEXTS[0])   # 陌生模型的上限可能上千万：默认先用最小一档，卡片上能调大
    return fact


def node_notes(node):
    """这台的③原文（副机用带回来的，没有就借这台的）。"""
    if is_local(node): return HOST_NOTES
    notes = node_data(node).get('notes')
    return notes if isinstance(notes, dict) else HOST_NOTES


SAFE_ID = r'[A-Za-z0-9][A-Za-z0-9._+@-]{0,127}'   # 自动识别只收这样的 id：要拼进网址和远程命令


def adopt_auto(node, auto):
    """配对加入的副机自己认出来的模型（它的 oMLX 列着、还没记进它的②）：按它报的事实合并出说明，记进 AUTO，主机才列得出、点得了。
    内容来自对方：名字、id 按白名单查，事实过和本机一样的校验（merge_models）；最多收 200 个。"""
    if not isinstance(auto, dict): return
    found = {}
    for key, fact in list(auto.items())[:200]:
        if not (isinstance(key, str) and re.fullmatch(MODEL_KEY, key) and isinstance(fact, dict) and re.fullmatch(SAFE_ID, str(fact.get('id', '')))): continue
        model = merge_models(FAMILIES, {key: fact}, node_notes(node)).get(key)
        if model: found[key] = {**model, 'fact': fact}
    if not found: return
    for key, model in found.items(): KNOWN.setdefault(key, model)
    AUTO.setdefault(node, {}).update(found)
    if SCOPE[0] == node: MODELS.update(found)


def discover(node, data):
    """这台 oMLX 列出来、清单里没有的模型（不是我们链进去的）：现认一遍，记进 AUTO。不写文件：第一次加载成功后才写进那台的②（remember_model）。
    只认聊天模型（oMLX 的 model_type 是 llm、vlm 或没写），向量、重排、语音这类不列。"""
    known = {m['id'] for m in models_for(node).values()}
    found = {}
    for row in (data or {}).get('models', []):
        mid = row.get('id')
        if not isinstance(mid, str) or not re.fullmatch(SAFE_ID, mid) or mid in known or row.get('is_helper') or row.get('model_type') not in (None, 'llm', 'vlm'): continue
        key, fact = model_key(mid), identify(node, row)
        model = merge_models(FAMILIES, {key: fact}, node_notes(node)).get(key)   # 还没加载过就改了说法的，也按改的显示
        if model:
            found[key] = {**model, 'fact': fact}   # fact：加载成功后原样写进②
            KNOWN.setdefault(key, found[key])
    if not found: return
    AUTO.setdefault(node, {}).update(found)
    if SCOPE[0] == node or (SCOPE[0] is None and is_local(node)): MODELS.update(found)
OPTIONS = {'mtp': 'MTP', 'ssd': tr('表放 SSD')}
# 多轮缓存（oMLX 的 SSD 前缀缓存）：每台一个仓库，在 omlx-state/cache 的 0–f 和 _gdn_sidecars（Qwen 的状态快照）里，
# oMLX 把两者算在同一个上限里，所有模型共用。开不开、上限多大按机器存（设置文件的 cache_size，0 是关）。
# 和 oMLX 自己的"清空 SSD 缓存"范围一致，不碰 response-state、vision_features。旧的双机缓存目录一并清理。
CACHE_DIR = STATE / 'cache'
CACHE_SHARDS = '0 1 2 3 4 5 6 7 8 9 a b c d e f _gdn_sidecars'
LEGACY_CACHE = '.omlx/cluster/runtime/prompt-cache-ssd'  # 双机时代的缓存，相对各自的家目录
CACHE_SIZES = (50, 100, 200, 500)  # 能选的上限（GB）；只列不超过（剩余硬盘 + 现有缓存）÷ 2 的，依据见上下文与缓存报告第 7.5 节
CACHE_RECOMMENDED = 100
# 内存安全线：超过总内存的这个百分比就报红。设置文件里的 memory_alert_percent 可覆盖。
ALERT_PERCENT = 90
# oMLX --memory-guard safe 的软水位：加载后超过内存上限的这个比例，它就先卸载最久没用的模型（process_memory_enforcer）
OMLX_SOFT_WATERMARK = 0.85
# 档位候选（用户 11-02 定，依据见上下文与缓存报告第 9.3 节）。实际能选哪些看模型自己的上限（oMLX 报的 model_context_length，
# 服务没开时用档案的 native）：Qwen 的上限就是 256K，只剩一档。超过内存安全线的不去掉，标出来由用户决定。
CONTEXTS = [262144, 524288, 1048576]


def context_mark(name, value):
    """实测过的档位（档案的 verified）；没把额度塞满的，标出实际测到多长（档案的 tested_up_to）。"""
    tested = MODELS[name].get('tested_up_to', {}).get(str(value))
    if value not in MODELS[name]['verified']: return tested or tr('未实测')
    return tr('已实测，') + tested if tested else tr('已实测')


def compact_text(value):
    """dsh 用到多少就自动压缩：压缩插件的公式，留足最大输出（32768）和余量（65536），最多用八成。算出来不是正数就压缩不了。"""
    at = min(value * 0.8, value - 32768 - 65536)
    return tr('到 {0} 万压缩', f'{at / 10000:.1f}' if lang() == 'zh' else f'{at / 1000:.0f}') if at > 0 else tr('自动压缩失效')


def context_note(name, value):
    """这一档的说明（档案的 context_notes）：推荐、可选、不建议，和"已实测"是两回事。"""
    return MODELS[name].get('context_notes', {}).get(str(value), '')
TTY = sys.stdout.isatty()


def paint(text, code): return f'\033[{code}m{text}\033[0m' if TTY else text
def ok(text): return paint('● ', '32') + text
def off(text): return paint('○ ', '90') + text
def warn(text): return paint('▲ ', '33') + text
def bad(text): return paint('✕ ', '31') + text
# 内存一律按 1024 进制显示，和"关于本机"的 512 GB 一致；Ollama 和文件大小按 1000 进制，同一个模型这里会少约 7%
GIB = 2**30
def gb(n): return f'{n / GIB:.0f} GB'
def gib(n): return round(n / GIB)
COMMUNITY_QUANT = re.compile(r'(?i)(?:^|[-_.])(?:q[2-8]|[2-8]-?bit)(?:$|[-_.])')


def model_label(name):
    """卡片头的短标签（用户 11-03 定）：档案写了短名（如 27B Q4）就用；官方原样发布的写满血版，无损的写满血无损版。
    档案说满血，id 却像社区量化（4bit、Q4）时不写，体检会提示。"""
    model = MODELS[name]
    if model.get('short_label'): return model['short_label'][:10]
    precision = model.get('precision') or {}
    if not precision.get('full') or COMMUNITY_QUANT.search(model['id']): return ''
    return tr('满血无损版') if precision.get('lossless') else tr('满血版')


def label_problem(name):
    """档案说满血，但名字或这台的 config.json 像量化过的（mlx-lm 量化会写 quantization 键；官方的 quantization_config 不算）。"""
    model = MODELS[name]
    if not (model.get('precision') or {}).get('full'): return None
    if COMMUNITY_QUANT.search(model['id']): return tr('名字 {0} 像量化版', model['id'])
    try: config = json.loads((ROOT / 'models' / model['id'] / 'config.json').read_text())
    except (OSError, ValueError): return None
    return tr('config.json 里有 quantization') if 'quantization' in config else None
def where(node): return NODES[node]['title']
def is_hub_node(node):   # 机器表写了 kind 就看 kind；旧表看 role（存的是中文"主机"）
    kind = NODES[node].get('kind')
    return kind == 'hub' if kind else NODES[node].get('role') == '主机'
def is_local(node): return 'ssh' not in NODES[node]
def official(node): return not MANAGED_HERE and is_local(node)   # 这台本机的 oMLX 不是自管的（官方或没装）。副机各管各的：走 rpc 的由它自己的控制台判断，全权限 SSH 的老路只认自管
def remotes(): return [n for n in NODES if not is_local(n)]
def is_rpc(node): return NODES[node].get('transport') == 'rpc'  # 走受限钥匙和 rpc（阶段 2）；没写的是全权限 SSH 加主密钥的老路
def forward(node): return f"{NODES[node]['port']}:127.0.0.1:{NODES[node].get('omlx_port') or worker_port(node) or OMLX_PORT}"


def worker_port(node):
    """副机是官方版时它的 oMLX 端口（它在 rpc state 里报 omlx_port，这边记在设置里，开隧道时不用再问）；自管副机没有，None。"""
    return saved().get('worker_ports', {}).get(node)
def offline(node): return tr('连不上 {0}。确认它开着、网络和地址对；开着 FileVault 的要先解锁。', where(node))


def get(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def post(url, body, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def ask(question, default=False):
    try: answer = input(question).strip().lower()
    except EOFError: return default
    return answer in {'y', 'yes', '是'} if answer else default


def saved(node=None):
    """设置文件。给了机器且它走 rpc：设置存在它自己那边（D5），用 rpc state 带回来的；否则读本机的文件。"""
    if node is not None and is_rpc(node): return (rpc_state(node) or {}).get('conf', {})
    return json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}


DEFAULT_CONTEXT = 262144   # 256K


def node_context(node, name):
    """这台跑这个模型用的上下文（10-30 起按机器分开存）：先看按机器存的，再看以前两台共用的旧设置，最后用默认值。
    官方版的默认值（0.5.1 A7，已定第 31 条 ⑧）：oMLX 里给这个模型单独设过就用它的，没有就一律 256K（模型上限更小的用上限），
    不按出厂说明里各模型不同的推荐值——新用户第一次加载不会因为默认 1M 把内存塞满。自管版（两台内部机）照旧。"""
    conf = saved(node)
    kept = conf.get('contexts', {}).get(node, {}).get(name) or conf.get(name)
    if kept or not official(node): return kept or MODELS[name]['context']
    own = engine_settings(node).get(MODELS[name]['id'], {}).get('max_context_window')
    if isinstance(own, int) and not isinstance(own, bool) and own > 0: return own
    return min(MODELS[name]['native'], DEFAULT_CONTEXT)


def save_node_context(node, name, value):
    conf = saved()
    conf.setdefault('contexts', {}).setdefault(node, {})[name] = value
    save_conf(conf)


def option_mode(name, key, node=None):
    """开关能不能拨：'free' 可开关｜'locked' 锁定为开（DeepSeek 的 Engram 表）｜'na' 不适用（MiniMax 没有查询表）｜
    'unsupported' 官方 oMLX 说这个模型不支持（3a，灰掉）。给了 node 且是官方版时按它报的能力（official_switch）。"""
    declared = key in MODELS[name].get('switches', OPTIONS)
    if node is not None and official(node):
        found = official_switch(node, name, key)
        # 0.5.1 A13：出厂说明没声明、oMLX 却报这个模型支持表放 SSD 的（陌生模型），也显示开关；它说不支持的陌生模型照旧不显示
        if found and (declared or (key == 'ssd' and found[0] != 'unsupported')): return found[0]
    if not declared: return 'na'   # 出厂说明里没声明这个开关：界面上不显示
    if key == 'mtp': return 'free'
    return {'locked': 'locked', None: 'na'}.get(MODELS[name]['ssd'], 'free')


def node_options(node, name):
    """这台这个模型的两个开关（10-31 起按机器存，和上下文一样）：按机器存的 > 模型默认值；锁定的恒开，不适用的恒关。"""
    conf, model = saved(node).get('options', {}).get(node, {}).get(name, {}), MODELS[name]
    out = {}
    for key in OPTIONS:
        mode = option_mode(name, key, node)
        out[key] = mode == 'locked' or (mode == 'free' and bool(conf.get(key, model[key] if not isinstance(model[key], str) else False)))
    return out


def save_node_option(node, name, key, value):
    conf = saved()
    conf.setdefault('options', {}).setdefault(node, {}).setdefault(name, {})[key] = bool(value)
    save_conf(conf)


def applied_options(node, name):
    """这台上次由 clusterctl 加载这个模型时实际用的开关；没有记录（10-31 以前加载的）返回 None。"""
    return saved(node).get('applied', {}).get(node, {}).get(name)


# 每台 oMLX 自己存的模型设置（model_settings.json），一条命令里只读一次；远程机器的随 remote_stats() 那次 SSH 一起带回
ENGINE = {}


def engine_settings(node):
    """{模型 id: oMLX 存的设置}，读不到返回 {}。oMLX 按存的设置建引擎；MTP、表放 SSD 在引擎签名里，已加载的模型
    改了这两项 oMLX 会自己卸载它（MTP 报告 §9.1），所以模型在内存里时，存的就是正在用的（11-01 两台核对一致）。"""
    if is_rpc(node): return (rpc_state(node) or {}).get('engine', {})
    if official(node):   # 官方版：不读它的文件（格式会变：0.8.0rc1 已改成第 2 版），走管理接口的 settings
        if node not in ENGINE: ENGINE[node] = {mid: row.get('settings') or {} for mid, row in engine_rows(node).items()}
        return ENGINE[node]
    if node not in ENGINE:
        try:
            text = MODEL_SETTINGS.read_text() if is_local(node) else ssh_to(node, f'cat "{MODEL_SETTINGS}"', timeout=15).stdout
            ENGINE[node] = json.loads(text).get('models', {})
        except (OSError, ValueError, subprocess.TimeoutExpired):
            ENGINE[node] = {}
    return ENGINE[node]


ENGINE_ROWS = {}


def engine_rows(node):
    """官方版 /admin/api/models 的每一行 {模型 id: 行}（含 settings 和能力字段 mtp_compatible、*_ssd_offload_supported/_forced），
    一条命令里只读一次；读不到（没允许、服务没开）是 {}。"""
    if node not in ENGINE_ROWS:
        rows = []
        if key_consent() is True and health(node) == 'up':
            try: rows = api(node, '/admin/api/models', timeout=10).get('models', [])
            except Exception: rows = []
        ENGINE_ROWS[node] = {row['id']: row for row in rows if isinstance(row, dict) and isinstance(row.get('id'), str)}
    return ENGINE_ROWS[node]


def official_switch(node, name, key):
    """官方 oMLX 报的这个开关：(mode, oMLX 的设置名, 不支持的原因)；它没报这个模型或这项能力时 None（照出厂说明）。
    mtp：mtp_compatible 为 False 是不支持。表放 SSD（0.5.1 A13：跟 oMLX 一样）：设置名取出厂说明的 setting 或 official_setting，
    出厂说明没写的（陌生模型）认它报的 <某某>_ssd_offload_supported；_supported 为假是不支持；<设置名>_forced 为真（放不下，
    它按内存强制放 SSD）锁定，第三项记 'forced'；别的都可开关——出厂说明里写死锁定的（DeepSeek）在官方版也不再锁，默认关。"""
    row = engine_rows(node).get(MODELS[name]['id'])
    if not row: return None
    if key == 'mtp':
        if row.get('mtp_compatible') is False: return ('unsupported', 'mtp_enabled', row.get('mtp_compatibility_reason') or '')
        return ('free', 'mtp_enabled', '') if row.get('mtp_compatible') is True else None
    load = MODELS[name].get('load', {})
    setting = load.get('ssd_key') or load.get('official_ssd_key') or next((k[:-len('_supported')] for k in sorted(row) if k.endswith('_ssd_offload_supported')), None)
    if not setting or f'{setting}_supported' not in row: return None
    if not row.get(f'{setting}_supported'): return ('unsupported', setting, '')
    if row.get(f'{setting}_forced'): return ('locked', setting, 'forced')
    return ('free', setting, '')


def live_options(node, name):
    """正在跑的这个模型实际用的开关（模型已加载时才有意义）：读 oMLX 存的设置，读不到退回 applied_options()。
    不只认 applied：控制台以外改过的（管理页、命令行直接 PUT）也能如实显示。"""
    row = engine_settings(node).get(MODELS[name]['id'], {})
    if 'mtp_enabled' not in row: return applied_options(node, name)
    ssd = option_mode(name, 'ssd', node)
    found = official_switch(node, name, 'ssd') if official(node) else None
    key = found[1] if found else MODELS[name].get('load', {}).get('ssd_key')
    return {'mtp': bool(row['mtp_enabled']), 'ssd': ssd == 'locked' or (ssd == 'free' and bool(key and row.get(key)))}


def ctx_label(n):
    return f'{n // 1048576}M' if n >= 1048576 else f'{n // 1024}K'


def save_conf(conf):
    """写设置文件：先写同目录的临时文件再改名。直接覆盖的话，磁盘满或中途被打断会把它截成空文件，之后所有命令都读不了（体检 11-15）。
    内容和以前逐字节相同（缩进 2、非 ASCII 转义）。"""
    temporary = SETTINGS.with_name(f'{SETTINGS.name}.{os.getpid()}.tmp')
    try:
        temporary.write_text(json.dumps(conf, indent=2))
        os.replace(temporary, SETTINGS)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def save_setting(name, value):
    conf = saved()
    conf[name] = value
    save_conf(conf)


def node_cache(node):
    """这台的多轮缓存：0 是关，否则是上限（GB）。按机器存；没存过的看以前各台共用的开关，开着就用推荐值。"""
    conf = saved(node)
    value = conf.get('cache_size', {}).get(node)
    if isinstance(value, int): return value
    return CACHE_RECOMMENDED if conf.get('ssd_cache', conf.get('deepseek_ssd_cache', True)) else 0


def save_node_cache(node, value):
    conf = saved()
    conf.setdefault('cache_size', {})[node] = value
    save_conf(conf)


def alert_percent():
    return int(saved().get('memory_alert_percent', ALERT_PERCENT))


def pick_nodes(target):
    if target in (None, 'all'): return list(NODES)
    if target not in NODES: raise RuntimeError(tr('机器只能是 {0} 或 all。', tr('、').join(NODES)))
    return [target]


# dsh（DeepSeek Harness）按自己配置里的 contextWindow 决定什么时候压缩、显示多少剩余，不会来问 oMLX。
# 加载成功或改上下文后，把这台的实际上下文写回 dsh 两份配置里这台对应来源下的那一条（主机 是 omlx、副机 是 mini），
# dsh 重开后生效。10-30 起两台各设各的，不再一起改。
DSH_PATCHES = [Path.home() / '.dsh/profiles' / p / 'cordis.patch.yml' for p in ('desktop', 'web')]
H3_FILES = (PROJECT / 'scripts/run_h3.py', PROJECT / 'scripts/serve_h3.py', ROOT / '.h3-venv')


# 个人工具检测到才出现（2b，草案 9.5、已定第 27 条）：没有就不显示、不打印、不提示。dsh 看运行控制台这台的配置文件；
# Ollama 看 11434 端口答不答（ollama_loaded() 不是 None）；H3 看脚本和环境都在不在。
def has_dsh(): return any(path.exists() for path in DSH_PATCHES)
def has_h3(): return all(path.exists() for path in H3_FILES)
def has_ollama(): return Path('/Applications/Ollama.app').exists() or shutil.which('ollama') is not None   # 装了但没在跑：终端菜单才说"未运行"


def dsh_provider(node): return NODES[node].get('dsh_provider', node)


def sync_dsh_context(name, context, node):
    """返回改动了的 dsh 配置（desktop / web）；没装 dsh 或已经一致就返回空列表。"""
    model_id, changed = MODELS[name]['id'], []
    for path in DSH_PATCHES:
        if not path.exists(): continue
        lines, inside, hit, provider = path.read_text().splitlines(keepends=True), False, False, None
        for i, line in enumerate(lines):
            text = line.strip()
            # providers 下面一层（缩进 6 格）是来源名：omlx、mini、ollama
            if len(line) - len(line.lstrip()) == 6 and text.endswith(':') and not text.startswith('-'):
                provider = text[:-1]
            if text.startswith('- id:'):
                inside = provider == dsh_provider(node) and text.split(':', 1)[1].strip().strip('"\'') == model_id
            elif inside and text.startswith('contextWindow:'):
                new = line[:len(line) - len(line.lstrip())] + f'contextWindow: {context}\n'
                if new != line: lines[i], hit = new, True
                inside = False
        if hit:
            tmp = path.with_name(path.name + '.tmp')
            tmp.write_text(''.join(lines))
            os.chmod(tmp, path.stat().st_mode & 0o777)
            os.replace(tmp, path)
            changed.append(path.parent.name)
    return changed


def dsh_models():
    """dsh 两份配置里登记了哪些 (来源, 模型 id)，卡片上"dsh 里选"那一行用；没装 dsh 返回空集合。解析同上。"""
    found = set()
    for path in DSH_PATCHES:
        if not path.exists(): continue
        provider = None
        for line in path.read_text().splitlines():
            text = line.strip()
            if len(line) - len(line.lstrip()) == 6 and text.endswith(':') and not text.startswith('-'): provider = text[:-1]
            if text.startswith('- id:'): found.add((provider, text.split(':', 1)[1].strip().strip('"\'')))
    return found


def owned_pid(path, field, marker):
    if not path.exists(): return None
    pid = json.loads(path.read_text()).get(field)
    if not isinstance(pid, int): return None
    command = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True, errors='replace').stdout
    return pid if marker in command else None


def process_running(script, *args):
    """只认由 Python 直接运行这个脚本的进程；命令行里碰巧提到脚本名的 shell、编辑器不算。"""
    pattern = re.compile(r'\S*[Pp]ython\S*\s(?:-\S+\s+)*\S*' + re.escape(script) + r'(\s|$)')
    lines = subprocess.run(['ps', '-axo', 'command='], capture_output=True, text=True, errors='replace').stdout.splitlines()
    return any(pattern.match(line) and all(arg in line.split() for arg in args) for line in lines)


def no_active_validation():
    if owned_pid(REPORTS / 'llm-validation-queue-pid.json', 'pid', 'validate_llm_queue.py'):
        raise RuntimeError(tr('自动验收正在运行，请等待完成后再切换模型。'))


def guard_devices():
    """本机上有 H3 任务或别的启动器占着设备锁时拒绝加载。"""
    no_active_validation()
    if process_running('scripts/run_h3.py'):
        raise RuntimeError(tr('H3 正在生成视频，请完成或在视频页取消后再在 {0} 上加载文字模型。', where(LOCAL)))
    with (ROOT / 'inference-session.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError(tr('另一个启动或推理任务正占用 {0}，请稍后再试。', where(LOCAL)))


# ---------- 和各台的连接 ----------

def ssh_to(node, command, timeout=10, data=None):
    """登记了几个地址的，连接都没建立就换下一个地址再试一次（命令没送到对方，重试是安全的）。"""
    while True:
        # -n 和 stdin=DEVNULL 防止 ssh 吞掉菜单输入；要往远程机器写文件时才把 data 接到 stdin
        args = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', *address_options(node), NODES[node]['ssh'], command]
        if data is None:
            r = subprocess.run(args[:1] + ['-n'] + args[1:], capture_output=True, text=True, errors='replace', timeout=timeout,
                               stdin=subprocess.DEVNULL)
        else: r = subprocess.run(args, capture_output=True, text=True, errors='replace', timeout=timeout, input=data)
        if not (r.returncode == 255 and CONNECT_FAILED.search(r.stderr) and other_address(node)): return r


def ssh_sure(node, command, timeout=10):
    """要拿结果做判断的远程命令（体检 T8）：SSH 这一下没连上（255）或超时，不能当成对方答了"没有"——报连不上，让调用方停下。"""
    try: r = ssh_to(node, command, timeout=timeout)
    except subprocess.TimeoutExpired: raise Unreachable(offline(node))
    if r.returncode == 255: raise Unreachable(offline(node))
    return r


def run_on(node, command, timeout=10):
    """在一台上跑一段 shell：本机直接跑，远程经 SSH。"""
    if is_local(node): return subprocess.run(['/bin/sh', '-c', command], capture_output=True, text=True, errors='replace', timeout=timeout)
    return ssh_to(node, command, timeout=timeout)


def posix(command):
    """远程那台的登录 shell 多半是 zsh：通配符一个都没匹配上就报 no matches found，整串命令中止（踩坑 #57）。
    带通配符的远程命令用它包一层，交给 /bin/sh 跑，和本机 run_on 一样。"""
    return '/bin/sh -c ' + shlex.quote(command)


def write_json(path, data):
    """先写同目录的临时文件再改名：同时刷新的另一个进程不会读到写了一半的文件。"""
    temporary = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(temporary, path)


# ---------- 一台几个地址（11-10，质检开关机实测 B2、用户决定 4）----------
# 机器表每台可以登记几个地址（addresses，网线在前、Wi-Fi 在后）。连之前同时试各个地址的 22 端口，用排在最前、通的那个。
# 结果记在 RUN_DIR：首选通着就一直用到连不上；用的不是首选时每 60 秒再试一次首选，网线插回来一分钟内切回去。
# 换地址不改用户的 SSH 配置：ssh 加 -o HostName=<地址>，主机指纹照旧按原来的名字核对（见 address_options）。
# 只登记了一个地址（或没登记）的机器和原来完全一样，按 SSH 配置连。
ADDRESS_FILE = RUN_DIR / 'addresses.json'
ADDRESS_RETRY = 60    # 用的不是首选时，隔多久再试首选
MDNS_RETRY = 60       # 都不通时，隔多久再问一次 .local
CONNECT_FAILED = re.compile(r'ssh: connect to host \S+ port \d+: ')   # 连接没建立：命令没送到对方
_PROBED, _USED, _SSH_SHOWN = set(), {}, {}   # 一条命令里：挑过地址的机器、最近一次用的地址、ssh -G 的结果


def addresses(node):
    """这台登记的地址，网线在前；旧写法的 ip / interface / link 算一个。"""
    meta = NODES[node]
    if meta.get('addresses'): return meta['addresses']
    return [{k: meta[k] for k in ('ip', 'interface', 'link')}] if meta.get('ip') else []


def multi(node): return not is_local(node) and len(addresses(node)) > 1
def sole_address(node):   # 机器页只留了一个地址：就连它（质检 K1）。旧写法只有 ip 的不算，照旧按 SSH 配置连
    rows = NODES[node].get('addresses') or []
    return rows[0]['ip'] if len(rows) == 1 and not is_local(node) else None


def first_open(ips, port=22, timeout=1.0):
    """同时试这些地址的 port 端口，返回排在最前、连得上的那个；都不通返回 None，最多等 timeout 秒。
    后面的先通了，前面的最多再等 0.3 秒：局域网里开着的机器几毫秒就回。"""
    result, waiting = {}, {}
    for ip in ips:
        s = socket.socket()
        s.setblocking(False)
        code = s.connect_ex((ip, port))
        if code == errno.EINPROGRESS: waiting[s] = ip
        else:
            result[ip] = code == 0
            s.close()
    deadline = time.monotonic() + timeout
    try:
        while True:
            for ip in ips:
                if ip not in result: break     # 前面还有没回的，接着等
                if result[ip]: return ip
            else: return None                  # 都回了，都不通
            left = deadline - time.monotonic()
            if left <= 0 or not waiting: return next((ip for ip in ips if result.get(ip)), None)
            _, done, _ = select.select([], list(waiting), [], left)
            for s in done:
                ip = waiting.pop(s)
                result[ip] = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0
                s.close()
                if result[ip]: deadline = min(deadline, time.monotonic() + 0.3)
    finally:
        for s in waiting: s.close()


def address_cache():
    try: return json.loads(ADDRESS_FILE.read_text())
    except (OSError, ValueError): return {}


def pick_address(node, fresh=False):
    """这台现在用哪个地址。只登记了一个地址的返回 None（按 SSH 配置连）；都不通也返回 None。
    一条命令里最多挑一次，fresh 是刚才的地址连不上、要重新挑。"""
    if not multi(node): return None
    ips = [a['ip'] for a in addresses(node)]
    cache = address_cache()
    have = cache.get(node) or {}
    if node in _PROBED and not fresh: return have.get('ip') if have.get('ip') in ips else None
    if not fresh and have.get('ip') in ips and (have['ip'] == ips[0] or time.time() - have.get('at', 0) < ADDRESS_RETRY): return have['ip']
    ip = first_open(ips, ssh_port(node))
    _PROBED.add(node)
    if ip != have.get('ip') or ip != ips[0]:   # 首选一直通着就不用每次写文件
        write_json(ADDRESS_FILE, {**cache, node: {**have, 'ip': ip, 'at': time.time()}})
    return ip


def ssh_shown(node):
    """ssh -G 的结果：这台按 SSH 配置怎么连。一条命令里只问一次。"""
    if node not in _SSH_SHOWN:
        args = ['ssh', *(['-F', str(SSH_CONFIG)] if is_rpc(node) else []), '-G', NODES[node]['ssh']]
        try: out = subprocess.run(args, capture_output=True, text=True, errors='replace', timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired): out = ''
        _SSH_SHOWN[node] = dict(line.split(' ', 1) for line in out.splitlines() if ' ' in line)
    return _SSH_SHOWN[node]


def ssh_port(node):
    try: return int(ssh_shown(node).get('port') or 22)
    except ValueError: return 22


def address_options(node):
    """连这台时加的 ssh 选项：登记了几个地址、挑出的不是 SSH 配置里那个，就改连挑出的地址。
    主机指纹照旧按原来的名字核对：配置里写了 HostKeyAlias 就是它，没写就拿原来的地址当别名（同一台机器各个网口的主机钥匙一样）。"""
    ip = pick_address(node) or sole_address(node)
    shown = ssh_shown(node) if ip else {}
    original, port = shown.get('hostname'), shown.get('port', '22')
    if not ip or not original or ip == original:
        _USED[node] = None
        return []
    _USED[node] = ip
    alias = shown.get('hostkeyalias') or (original if port == '22' else f'[{original}]:{port}')
    return ['-o', f'HostName={ip}', '-o', f'HostKeyAlias={alias}']


def address_now(node):
    """这台现在连的是哪个地址（给界面和体检看）。"""
    if is_local(node): return None
    return pick_address(node) or (NODES[node].get('ip') if not multi(node) else None)


def other_address(node):
    """刚才的地址连不上：重新挑一次，挑到别的地址才值得再试。这条命令里已经挑过就不再挑。"""
    if not multi(node) or node in _PROBED: return False
    old = _USED.get(node) or ssh_shown(node).get('hostname')
    return pick_address(node, fresh=True) not in (None, old)


def mdns_lookup(name):
    """问局域网的 mDNS：<名字>.local 现在是哪个地址，限时 1 秒。问不到返回 None。"""
    try: out = subprocess.run(['dig', name, 'A', '@224.0.0.251', '-p', '5353', '+short', '+time=1', '+tries=1'], capture_output=True, text=True, errors='replace', timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired): return None
    return next((line for line in out.split() if re.fullmatch(ADDRESS_IP, line)), None)


def address_note(node):
    """登记的地址都不通时问一次 .local（每分钟最多一次）。解析出机器表里没有的地址，就说地址可能变了；不自动改表，免得被同名设备骗。"""
    name = NODES[node].get('mdns')
    if not name or is_local(node): return None
    cache = address_cache()
    have = cache.get(node) or {}
    if time.time() - have.get('mdns_at', 0) >= MDNS_RETRY:
        have = {**have, 'mdns_ip': mdns_lookup(name), 'mdns_at': time.time()}
        write_json(ADDRESS_FILE, {**cache, node: have})
    found, listed = have.get('mdns_ip'), [a['ip'] for a in addresses(node)]
    if found and found not in listed:
        return tr('地址可能变了：现在是 {0}，机器表里是 {1}。检查路由器的地址绑定和私有 Wi-Fi 地址。', found, tr('、').join(listed))
    return None


# ---------- 经受限钥匙调对方的 rpc（对方的 scripts/rpc.py）----------
# 钥匙、地址、主机指纹都在这台的 ~/.ssh/lmc 里，SSH 选项写在那里的 config，不动用户自己的 ~/.ssh/config。
# 每台一条复用连接：隧道就是主连接（ensure_tunnel），rpc 会话走它，不用每次重新握手；主连接断了 ssh 自己退回直连。
LMC_SSH = Path.home() / '.ssh/lmc'
SSH_CONFIG = LMC_SSH / 'config'
RPC_PROTO = 1
GOING_DOWN = '@@going-down'
UNLOADING = '@@unloading'   # 卸载开始了，不能再停（应用收到就灰掉停止）
DOCTOR_FAILED = '@@doctor-failed'   # 体检有没过的项（应用按它显示红绿，不认"要看"两个字）
RPC_STATE = {}  # 一条命令里各台的状态：{机器: (是否完整, 状态或 None)}


class Unreachable(RuntimeError): pass


def rpc_failure(node, r):
    """把一次失败的 rpc 变成一句话。退出码 255 是 ssh 自己失败（连不上、钥匙被拒），其余是对方的回答。"""
    if r.returncode == 255:
        if 'Permission denied' in r.stderr: return Unreachable(tr('{0} 拒绝了这台的钥匙：授权可能已被吊销，要重新配对。', where(node)))
        if 'Host key verification failed' in r.stderr or 'IDENTIFICATION HAS CHANGED' in r.stderr:
            return Unreachable(tr('{0} 的主机指纹和配对时不一样，已停止连接。确认它没有重装系统、地址没有被别的机器占用。', where(node)))
        return Unreachable(offline(node))
    for line in r.stdout.splitlines():
        if line.startswith('@@error '): return RuntimeError(json.loads(line[8:]))
    return RuntimeError(tr('{0} 没有完成这次操作（退出码 {1}）：{2}', where(node), r.returncode, r.stderr.strip()[:200]))


def hub_prefix():
    """启用主机模式后，发给各台的指令都带 @任期；界面是英文时再带语言，对方按它回文字。中文、没启用时是空的，和原来一样。"""
    import hub
    state = hub.load()
    return ('lang=en ' if lang() == 'en' else '') + (f"@{state['term']} " if state else '')


def rpc_query(node, *words, timeout=30, data=None):
    """查询类 rpc：对方回一行 JSON。参数都是这边校验过的固定词，带空格的走 data（标准输入）。"""
    while True:
        try: r = subprocess.run(['ssh', '-F', str(SSH_CONFIG), *address_options(node), NODES[node]['ssh'], hub_prefix() + ' '.join(words)], capture_output=True, text=True, errors='replace',
                                timeout=timeout, input=data or '')
        except subprocess.TimeoutExpired: raise Unreachable(offline(node))
        # 连接都没建立（请求没送到对方）、又登记了别的地址：换一个再试一次，和 ssh_to() 一样（第 4 期）
        if not (r.returncode == 255 and CONNECT_FAILED.search(r.stderr) and other_address(node)): break
    if r.returncode: raise rpc_failure(node, r)
    return json.loads(r.stdout)


def rpc_action(node, *words, before_last=None, data=None):
    """动作类 rpc：对方的进度逐行照打；最后一行留到最后，应用把最后一行显示在提示条里，before_last 的提示要排在它前面。
    对方的事不随这边结束而停：断开后它照样做完，这边再查状态就能看到。"""
    process = subprocess.Popen(['ssh', '-F', str(SSH_CONFIG), *address_options(node), NODES[node]['ssh'], hub_prefix() + ' '.join(words)], stdin=subprocess.DEVNULL if data is None else subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace', bufsize=1)
    if data is not None:
        try:
            process.stdin.write(data)
            process.stdin.close()
        except OSError: pass  # 对方马上拒绝时管道已断，错误文字下面照样读得到
    held, result, failure, going = None, {}, None, False
    for line in process.stdout:
        line = line.rstrip('\n')
        if line == GOING_DOWN: going = True
        elif line.startswith('@@result '): result = json.loads(line[9:])
        elif line.startswith('@@error '): failure = json.loads(line[8:])
        else:
            if held is not None: print(held)
            held = line
    code = process.wait()
    RPC_STATE.pop(node, None)
    if going and code == 255 and not failure: code = 0   # 对方说了要关机，连接随后断开，算成功
    if failure or code:
        if held is not None: print(held)
        if failure: raise RuntimeError(failure)
        raise rpc_failure(node, subprocess.CompletedProcess(words, code, '', process.stderr.read()))
    if before_last: before_last(result)
    if held is not None: print(held)
    return result


def rpc_state(node, full=False):
    """这台的状态（对方的 rpc state，见 local_state()）：一条命令里只取一次；连不上返回 None。
    full 才带内存、硬盘这些要跑探测的数。连得上就顺手把隧道接上。"""
    have = RPC_STATE.get(node)
    if have and (have[0] or not full): return have[1]
    try: state = rpc_query(node, 'state', *([] if full else ['--fast']), timeout=20 if full else 10)
    except Unreachable: state = None
    if state:
        port = state.get('omlx_port')   # 只有官方版的副机报这一项（3a）；变了就记下，隧道按它重开（ensure_tunnel 比的是 forward()）
        if isinstance(port, int) and 0 < port < 65536 and worker_port(node) != port:
            stop_tunnel(node)   # 先停旧隧道（第 4 期）：它转到的是旧端口，记下新端口以后就认不出它是这台的隧道，会一直占着本机那个端口，新的开不起来
            save_setting('worker_ports', {**saved().get('worker_ports', {}), node: port})
        stats = state.get('stats')
        if isinstance(stats, dict) and 'have' in stats:   # 完整刷新带回来的：这台有哪些模型、它的②③。和走全权限 SSH 的副机一样记下（第 4 期）
            keep_node_data(node, {name: stats.pop(name, None) for name in ('have', 'facts', 'notes')})
        adopt_auto(node, state.get('auto'))
        state['conf'] = {**state.pop('fallback'), **{k: {node: v} for k, v in state['conf'].items()}}
        state['node']['models'] = {name: state['node']['models'].get(name, 'off') for name in models_for(node)}   # 按这台自己的清单取，不按主机的
    RPC_STATE[node] = (full, state)
    if state: reach(node)
    return state


def console_files():
    """装到被控方的那几个文件（相对 PROJECT）；对方的 code_hash() 和这边不同就提示版本不一致。
    模型说明只算①出厂说明（随程序发）；②③在各台的运行目录，是那台自己的，不算、也不同步。"""
    return [Path('scripts') / f for f in ('clusterctl.py', 'paths.py', 'brand.py', 'rpc.py', 'serve_omlx.py', 'hub.py', 'chip.py', 'gateway.py', 'device_service.py', 'i18n.py', 'i18n_en.json',
                                          'omlx_adapter.py', 'updates.py', 'signing.py', 'netscan.py', 'gateway_defaults.py')] + [Path('config/engine-compat.json')] + \
        sorted(p.relative_to(PROJECT) for p in FAMILIES_DIR.glob('*.json'))   # 3a 加对接模块和兼容表：副机少了它们 import 就失败


def code_hash():
    digest = hashlib.sha256()
    for path in console_files(): digest.update((PROJECT / path).read_bytes())
    return digest.hexdigest()[:12]


# 每台一把操作锁：加载、卸载、启停、改开关期间持有，进程退出系统自动放开；两个控制台同时操作同一台时，后到的停下。
OPERATOR = None  # 谁在操作：rpc 入口填控制方的名字；None 是这台自己的控制台
OPERATOR_LEVEL = None  # rpc 入口填那把钥匙的级别（体检 R7）
_op_lock = None


def lock_holder():
    """谁占着这台的操作锁：{who, what, since}；没人占返回 None。只读文件和进程，不碰锁本身，刷新时不会和真操作抢。"""
    try:
        info = json.loads((RUN_DIR / 'op-lock').read_text())
        os.kill(info['pid'], 0)
    except (OSError, ValueError, KeyError, TypeError): return None
    return {k: info.get(k) for k in ('who', 'what', 'since')}


def hold_lock(what):
    """占住这台的操作锁到这次命令结束（终端菜单每项操作后放开，见 release_lock()）。已经占着就什么都不做。"""
    global _op_lock
    if _op_lock: return
    lock = (RUN_DIR / 'op-lock').open('a+')
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        info = lock_holder() or {}
        lock.close()
        raise RuntimeError(tr('{0} 正被 {1} 占用', where(LOCAL), info.get('who', tr('另一个控制台'))) + (tr('：{0}，{1} 开始', info['what'], info['since']) if info.get('what') else '') + tr('。等它做完再试。'))
    lock.seek(0)
    lock.truncate()
    lock.write(json.dumps({'who': OPERATOR or tr('{0} 的控制台', where(LOCAL)), 'what': what, 'since': time.strftime('%H:%M'), 'pid': os.getpid()}, ensure_ascii=False))
    lock.flush()
    _op_lock = lock


def release_lock():
    global _op_lock
    if _op_lock: _op_lock.close()
    _op_lock = None


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(('127.0.0.1', port)) == 0


def tunnel_file(node): return RUN_DIR / f'{node}-tunnel-pid.json'


def command_of(pid): return subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True, errors='replace').stdout


def is_tunnel(node, pid, command=None):
    command = command_of(pid) if command is None else command
    # 走 rpc 的还要核对连的是哪台：端口对、机器不对的隧道不能认领，不然流量接到别的机器上
    return forward(node) in command and (not is_rpc(node) or NODES[node]['ssh'] in command)


def tunnel_pid(node):
    path = tunnel_file(node)
    if not path.exists(): return None
    pid = json.loads(path.read_text()).get('pid')
    return pid if isinstance(pid, int) and is_tunnel(node, pid) else None


def adopt_tunnel(node):
    """端口开着、pid 文件却对不上时，认领正在听这个端口的隧道；是别的程序在听就返回 None。
    10-30 到 11-01 就是这样：pid 文件记着一个早已退出的 ssh，每次刷新都再起一个 ssh，因端口已占立刻退出，
    又把自己的 pid 写进文件，日志攒了 1.4 万行。"""
    r = subprocess.run(['/usr/sbin/lsof', '-nP', '-t', f"-iTCP:{NODES[node]['port']}", '-sTCP:LISTEN'],
                       capture_output=True, text=True, errors='replace')
    for pid in map(int, r.stdout.split()):
        command = command_of(pid)
        if is_tunnel(node, pid, command):
            via = re.search(r'HostName=(\S+)', command)
            tunnel_file(node).write_text(json.dumps({'pid': pid, **({'ip': via[1]} if via else {})}, indent=2))
            return pid
    return None


def tunnel_address(node):
    """隧道连的是哪个地址：建隧道时记下的，没记就是 SSH 配置里的。"""
    try: via = json.loads(tunnel_file(node).read_text()).get('ip')
    except (OSError, ValueError, AttributeError): via = None
    return via or ssh_shown(node).get('hostname')


def ensure_tunnel(node):
    """本机 127.0.0.1:port → 这台的 127.0.0.1:8800。隧道断了（睡眠、网络抖动）就重开；返回是否在听。"""
    port = NODES[node]['port']
    if port_open(port): return bool(tunnel_pid(node) or adopt_tunnel(node))
    old = tunnel_pid(node)
    if old: os.kill(old, signal.SIGTERM)
    log = (RUN_DIR / f'{node}-tunnel.log').open('ab')
    if is_rpc(node):  # 隧道同时是复用连接的主连接；超时、保活等选项在 SSH_CONFIG 里
        control_path(node).unlink(missing_ok=True)  # 上一个主连接没收尾留下的
        command = ['ssh', '-F', str(SSH_CONFIG), '-M', '-N', *address_options(node), '-L', f'127.0.0.1:{forward(node)}', NODES[node]['ssh']]
    else:
        # 保活 5 秒 × 3：换线、睡醒后旧隧道约 15 秒断开，下次刷新就接上（原来 15 秒 × 3，约 45 秒）
        command = ['ssh', '-N', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', '-o', 'ExitOnForwardFailure=yes',
                   '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=3', *address_options(node),
                   '-L', f'127.0.0.1:{forward(node)}', NODES[node]['ssh']]
    via = _USED.get(node)
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(50):
        # 同时有两次刷新各起一个 ssh 时，抢不到端口的那个会退出：这时认领抢到的那个，pid 文件只记活着的隧道
        if process.poll() is not None: return port_open(port) and bool(adopt_tunnel(node))
        if port_open(port):
            tunnel_file(node).write_text(json.dumps({'pid': process.pid, **({'ip': via} if via else {})}, indent=2))
            return True
        time.sleep(0.1)
    return False


def stop_tunnel(node):
    pid = tunnel_pid(node) or adopt_tunnel(node)
    if pid: os.kill(pid, signal.SIGTERM)


def reach(node):
    """操作远程机器前确保隧道在；本机不用。隧道走的地址不通了、另一个地址通，就换一条（换线时不用等保活断开）。
    网线插回来时不换：隧道走的 Wi-Fi 还通，换了会打断正在答的对话，新的命令照样先走网线。"""
    if is_local(node): return
    if port_open(NODES[node]['port']) and tunnel_pid(node):
        if not tunnel_stale(node): return
        renew_tunnel(node)
        return
    ensure_tunnel(node)


def tunnel_stale(node):
    """隧道走的地址已经不通、现在该用别的地址。只看登记了几个地址的机器。"""
    if not multi(node): return False
    now, via = pick_address(node), tunnel_address(node)
    return bool(now and via and now != via and first_open([via], ssh_port(node)) is None)


def tunnel_broken(node):
    """隧道是不是坏了：经隧道问不通（限时 5 秒），而在那台本机直接问 oMLX 通。那台自己也答得慢（正忙着答题）就不算坏，
    免得剪断客户端正在用的隧道。"""
    try:
        get(f"http://127.0.0.1:{NODES[node]['port']}/health", timeout=5)
        return False
    except urllib.error.HTTPError: return False
    except Exception: pass
    try: r = ssh_to(node, PROBE['here'], timeout=10)
    except subprocess.TimeoutExpired: return False
    return r.returncode == 0 and r.stdout.strip() not in ('', '000')


def renew_tunnel(node):
    """关掉旧隧道、等端口放开，再按现在挑出的地址建一条。"""
    stop_tunnel(node)
    for _ in range(30):
        if not port_open(NODES[node]['port']): break
        time.sleep(0.1)
    return ensure_tunnel(node)


def main_key():
    """主密钥以本机的 settings.json 为准（各台用同一把）。含明文密钥，只在程序内部使用、不打印。"""
    if not MANAGED_HERE: return official_key()
    return json.loads((STATE / 'settings.json').read_text())['auth']['api_key']


def key_consent():
    """官方版：用户允不允许控制台读这台 oMLX 的主密钥（10.3）。True 允许、False 不允许、None 还没问。存在设置文件里，只存态度，不存密钥。"""
    value = saved().get('engine_key')
    return value if isinstance(value, bool) else None


LISTENER = {}   # 端口 → 听它的进程是不是自己人（一条命令里只查一次）


def listener_owner(port):
    """127.0.0.1:port 上听着的进程是谁的：'mine' 这个账号的，'root'，'other' 别的账号的，None 没人听。
    lsof 只看得到自己账号的进程：看得到就是自己的；看不到、端口又通，再用 netstat 找进程号，看它是不是 root 的。"""
    def run(command):
        try: return subprocess.run(command, capture_output=True, text=True, errors='replace', timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired): return ''
    if 'p' in run(['/usr/sbin/lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-Fp']): return 'mine'
    if not port_open(port): return None
    for line in run(['/usr/sbin/netstat', '-anv', '-p', 'tcp']).splitlines():
        cols = line.split()
        if 'LISTEN' not in cols or len(cols) < 4 or cols[3].rsplit('.', 1)[-1] != str(port): continue
        found = next((m for m in (re.search(r':(\d+)$', c) for c in cols[cols.index('LISTEN') + 1:]) if m), None)
        if found and run(['/bin/ps', '-o', 'uid=', '-p', found[1]]).strip() == '0': return 'root'
    return 'other'


def listener_ours(port):
    """这台 127.0.0.1:port 上听着的是不是自己人（这个账号或 root）；没人听也算（没有可发的对象）。
    官方版的 oMLX 不是控制台起的：它没开时，别的账号的程序可以先占住这个端口，等控制台把主密钥发过去（体检 11-15）。"""
    if port not in LISTENER: LISTENER[port] = listener_owner(port) != 'other'
    return LISTENER[port]


def official_key():
    """官方版的主密钥在它数据目录的 settings.json（auth.api_key，明文）。第一次对接时问一次，同意后每次现读、不存副本。"""
    if ENGINE_HERE['kind'] == 'none': raise RuntimeError(tr('{0} 还没装 oMLX。', where(LOCAL)))
    if not listener_ours(HERE_PORT):
        raise RuntimeError(tr('{0} 的端口 {1} 上听着的不是这个账号的程序，没有把 oMLX 的主密钥发过去。先弄清是谁占着这个端口。', where(LOCAL), HERE_PORT))
    if key_consent() is not True: raise RuntimeError(tr('还没允许控制台读 {0} 的 oMLX 主密钥：加载、卸载、子密钥这些管理功能先用不了。', where(LOCAL)))
    if not ENGINE_HERE.settings: raise RuntimeError(tr('还没找到 {0} 的 oMLX 数据目录：请先打开一次 oMLX，完成它的首次设置。', where(LOCAL)))
    try: return json.loads(ENGINE_HERE.settings.read_text())['auth']['api_key']
    except (OSError, ValueError, KeyError, TypeError) as exc: raise RuntimeError(tr('读不了 {0} 的 oMLX 主密钥（{1}）。', where(LOCAL), type(exc).__name__)) from None


def api(node, route, body=None, method=None, timeout=30, key=None, retry=False):
    """retry：连接被重置时等 1 秒再试一次。只给重复做也无害的卸载、重新扫描、改设置用；加载不重试。
    10-31 评测里 主机 切模型两次被重置，都紧跟在 oMLX 重新扫描之后，重试即成功，oMLX 日志无报错。"""
    if is_rpc(node): raise RuntimeError(tr('内部错误：{0} 走 rpc，不该把这台的主密钥发过去（{1}）。', where(node), route))
    url = f"http://127.0.0.1:{NODES[node]['port']}{route}"
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={'Authorization': 'Bearer ' + (key or main_key()),
                                          'Content-Type': 'application/json'}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        for secret in [req.headers.get('Authorization', '')[7:], *(v for k, v in (body or {}).items() if k in ('key', 'api_key'))]:
            if isinstance(secret, str) and len(secret) >= 8: detail = detail.replace(secret, '…')
        raise RuntimeError(tr('{0}：HTTP {1}：{2}', where(node), exc.code, detail[:300])) from exc
    except (ConnectionResetError, urllib.error.URLError) as exc:
        # 回复途中被重置直接抛 ConnectionResetError；连接还没建好时包在 URLError 里
        if not (retry and (isinstance(exc, ConnectionResetError) or isinstance(exc.reason, ConnectionResetError))): raise
    time.sleep(1)
    return api(node, route, body, method, timeout, key)


def health(node, listening=None, answers=None):
    """'up' 就绪；'starting' 进程在但还没就绪；'reconnecting' 远程的 oMLX 在、它本机答得了、经隧道却不通（换线、睡醒后旧隧道还没断）；
    None 没有在运行。远程机器的 listening、answers 来自一次 SSH 探测；不知道它本机答不答时按 starting 说，不说成网络的事。"""
    if is_rpc(node): return (rpc_state(node) or {'node': {}})['node'].get('health')
    port = NODES[node]['port']
    try:
        get(f'http://127.0.0.1:{port}/health', timeout=2)
        return 'up'
    except urllib.error.HTTPError:
        return 'starting'
    except Exception:
        if is_local(node): return 'starting' if port_open(port) else None
        if not listening: return None
        return 'reconnecting' if answers else 'starting'


def model_rows(node):
    try: return api(node, '/v1/models/status', timeout=5)
    except Exception: return None


def models_sure(node):
    """加载、卸载之前用：服务开着却读不到模型状态（这一下超时、连接被重置）时再试一次，还读不到就停下。
    node_models() 读不到时全算 off：加载会不问一句就挤掉正在跑的模型，卸载会只撤链接、不卸模型（体检 11-15）。"""
    data = model_rows(node)
    if data is None:
        time.sleep(1)
        data = model_rows(node)
    if data is None: raise RuntimeError(tr('读不到 {0} 的模型状态（oMLX 没有回答），什么都没动。稍后再试。', where(node)))
    return node_models(node, data)


def state(entry):
    if entry and entry.get('loaded'): return 'loaded'
    if entry and entry.get('is_loading'): return 'loading'
    return 'off'


def node_models(node, data=None):
    """{模型名: loaded|loading|off}；服务没开时全是 off。"""
    if data is None and is_rpc(node): return (rpc_state(node) or {'node': {}})['node'].get('models') or {n: 'off' for n in MODELS}
    data = data if data is not None else model_rows(node)
    if SCOPE[0] == node: discover(node, data)   # 按这台做事时，oMLX 新列出来的也算上（每条命令只认一次）
    rows = {m['id']: m for m in (data or {}).get('models', [])}
    return {name: state(rows.get(model['id'])) for name, model in MODELS.items()}


def wait_ready(node, process=None, seconds=120):
    for _ in range(seconds):
        if health(node) == 'up': return
        if process is not None and process.poll() is not None:
            raise RuntimeError(tr('{0} 的 oMLX 启动失败，请看日志（菜单 l）。', where(node)))
        time.sleep(1)
    raise RuntimeError(tr('{0} 的 oMLX 服务没有就绪，请看日志（菜单 l）。', where(node)))


# ---------- 状态 ----------

def ollama_loaded():
    try: return get(OLLAMA + '/api/ps').get('models', [])
    except Exception: return None


# 各台用同一段只读脚本取状态：分段输出，本机直接跑，远程机器经一次 SSH 跑，解析只有一份。
# 内存压力用内核自己的判断（1 正常、2 警告、4 严重）；不用 memory_pressure 的"可用百分比"：
# 模型权重是映射进来的文件页，它算作随时能腾出，模型在内存里时也报 98%。
# 进程的 phys_footprint（内核记录的实际占用，和活动监视器进程列表的"内存"列同一个数）：
# rusage_info_v2 开头是 16 字节 uuid，之后第 8 个计数就是它，按 uint64 数是下标 9
FOOTPRINT_PY = ('import ctypes,sys;b=(ctypes.c_uint64*32)();'
                'ctypes.CDLL("/usr/lib/libproc.dylib").proc_pid_rusage(int(sys.argv[1]),2,b);print(b[9])')
PROBE = {
    'mem': 'sysctl -n hw.pagesize hw.memsize; sysctl -n kern.memorystatus_vm_pressure_level',
    'swap': 'sysctl -n vm.swapusage',
    'vm': 'vm_stat',
    'gpu': "ioreg -r -d 1 -c IOAccelerator | grep -o '\"Device Utilization %\"=[0-9]*' | head -1",
    'disk': 'df -k /Users/Shared | tail -1',
    'rdma': 'ibv_devinfo -d rdma_en4 2>/dev/null | grep -c PORT_ACTIVE',
    'procs': "pgrep -f 'omlx[.]cluster[.]inference_worker' | wc -l",
    'cache': f'cd "{CACHE_DIR}" 2>/dev/null && du -sk -c {CACHE_SHARDS} 2>/dev/null | tail -1 | cut -f1',
    # oMLX 设置里缓存开没开、上限多大。settings.json 里有主密钥：sed 只取 cache 那一段，grep -o 只留这两项的键值，别的都不出来
    'cache_conf': f"sed -n '/\"cache\": {{/,/}}/p' \"{STATE}/settings.json\" 2>/dev/null | grep -oE '\"(enabled|ssd_cache_max_size)\": *(\"[^\"]*\"|true|false)' | head -2",
    'omlx': f'lsof -nP -tiTCP:{HERE_PORT} -sTCP:LISTEN 2>/dev/null | head -1',
    # oMLX 进程的实际占用，远程机器上用一行 Python 读（本机在本进程里直接读，见 footprint()）
    'foot': f"p=$(lsof -nP -tiTCP:{HERE_PORT} -sTCP:LISTEN 2>/dev/null | head -1); "
            f"if [ -n \"$p\" ]; then {shlex.quote(str(here_python()))} -I -c '{FOOTPRINT_PY}' \"$p\"; fi",
    # oMLX 存的模型设置（约 5 KB，不含密钥），看正在跑的开关用，见 engine_settings()；本机直接读本地文件
    'engine': f'cat "{MODEL_SETTINGS}" 2>/dev/null; echo',  # 文件末尾没有换行，补一个，免得下一段的 @@ 标记接在同一行
    # 2b 每台一份清单：这台有哪些模型文件夹（带 config.json 的），和它自己的②③（约 1 KB、十几 KB，不含密钥）。不用通配符（踩坑 #57）
    'have': f'ls -1 "{ROOT}/models" 2>/dev/null | while read d; do [ -f "{ROOT}/models/$d/config.json" ] && echo "$d"; done; true',
    'facts': f'cat "{ROOT}/model-facts.json" 2>/dev/null; echo',
    'notes': f'cat "{ROOT}/model-notes.json" 2>/dev/null; echo',
    'desk': 'stat -f %Su /dev/console 2>/dev/null',   # 谁登录了桌面；停在登录界面时是 root（T1）
    # oMLX 在那台本机答不答（限时 1 秒）：经隧道问不通、这里通，才是隧道的事（D2）；这里也不通是 oMLX 自己忙或没起来
    'here': f"curl -s -m 1 -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{HERE_PORT}/health 2>/dev/null",
}
LOCAL_PARTS = ['mem', 'swap', 'vm', 'gpu', 'disk', 'cache', 'cache_conf']
REMOTE_PARTS = ['mem', 'swap', 'vm', 'gpu', 'disk', 'cache', 'cache_conf', 'omlx', 'procs', 'engine', 'have', 'facts', 'notes', 'desk', 'here', 'foot']  # foot 放最后：退出码看它


def parse_cache_conf(text):
    """上面 cache_conf 那两行 → oMLX 设置的上限：0 是关，否则 GB；读不到或不是具体数字（比如 auto）返回 None。"""
    on, size = re.search(r'"enabled":\s*(true|false)', text or ''), re.search(r'"ssd_cache_max_size":\s*"(\d+(?:\.\d+)?)\s*(GB|TB)"', text or '', re.I)
    if on and on[1] == 'false': return 0
    if not on or not size: return None
    return round(float(size[1]) * (1024 if size[2].upper() == 'TB' else 1))


def probe_script(parts):
    return '; '.join(f"echo '@@{name}'; {PROBE[name]}" for name in parts)


def sections(text):
    out, name = {}, None
    for line in text.splitlines():
        if line.startswith('@@'):
            name = line[2:].strip()
            out[name] = []
        elif name:
            out[name].append(line)
    return {k: '\n'.join(v).strip() for k, v in out.items()}


def parse_probe(text):
    """把分段输出解析成字节和数字；哪一段读不到就留空，不让一项失败拖垮整个状态。"""
    part, out = sections(text), {}
    numbers = part.get('mem', '').split()
    try:
        page, total = int(numbers[0]), int(numbers[1])
        stats = part.get('vm', '')
        pages = lambda key: int(re.search(rf'{key}:\s+(\d+)', stats).group(1))
        # 和活动监视器的"已使用内存"同一算法：应用内存（匿名页 − 可清除页）+ 锁定 + 压缩。不能只数"活跃"页：
        # MoE 模型暂时没用到的专家权重会被系统挪进不活跃队列，只数活跃页会少算 12–17%（10-30 实测）
        out['used'] = (pages('Anonymous pages') - pages('Pages purgeable') + pages('Pages wired down')
                       + pages('Pages occupied by compressor')) * page
        out['total'] = total
        out['pressure'] = int(numbers[2]) if len(numbers) > 2 and numbers[2].isdigit() else 1
    except (IndexError, ValueError, AttributeError):
        pass
    swap = re.search(r'used = ([\d.]+[MG])', part.get('swap', ''))
    out['swap'] = swap.group(1).replace('0.00M', '0 MB') if swap else '?'
    gpu = re.search(r'=(\d+)', part.get('gpu', ''))
    out['gpu'] = int(gpu.group(1)) if gpu else None
    disk = part.get('disk', '').split()
    out['disk_free'] = int(disk[3]) * 1024 if len(disk) > 3 and disk[3].isdigit() else None
    if 'rdma' in part: out['rdma'] = part['rdma'] not in ('', '0')
    if 'procs' in part: out['procs'] = int(part['procs']) if part['procs'].isdigit() else 0
    if 'cache' in part: out['cache'] = int(part['cache']) * 1024 if part['cache'].isdigit() else 0
    if 'cache_conf' in part: out['cache_limit'] = parse_cache_conf(part['cache_conf'])
    if 'omlx' in part: out['omlx'] = int(part['omlx']) if part['omlx'].isdigit() else None
    if 'foot' in part: out['omlx_now'] = int(part['foot']) if part['foot'].isdigit() else None
    if part.get('desk'): out['desktop'] = part['desk'] != 'root'   # 读不到就不说
    if 'here' in part: out['answers'] = part['here'] not in ('', '000')
    if 'engine' in part:
        try: out['engine'] = json.loads(part['engine']).get('models', {})
        except (ValueError, AttributeError): out['engine'] = {}
    if 'have' in part: out['have'] = [line.strip() for line in part['have'].splitlines() if line.strip()]
    for name in ('facts', 'notes'):   # 没有这份文件、读不了：None，用这台的（models_for）
        if name not in part: continue
        try: layer = json.loads(part[name]).get('models')
        except (ValueError, AttributeError): layer = None
        out[name] = layer if isinstance(layer, dict) else None
    return out


def local_stats():
    try: text = subprocess.run(['/bin/sh', '-c', probe_script(LOCAL_PARTS)], capture_output=True, text=True, errors='replace', timeout=15).stdout
    except subprocess.TimeoutExpired: text = ''
    # oMLX 启动后把进程名改成 omlx-server（看不到 serve_omlx.py），按这个认；控制台以外的人拉起的才退回 lsof 现查
    pid = owned_pid(RUN_DIR / 'omlx-server-pid.json', 'pid', 'omlx-server') or listener(LOCAL)[0]
    out = {'online': True, **parse_probe(text), 'omlx_now': footprint(pid) if pid else None}
    if official(LOCAL): out.update(official_cache())
    return out


def official_cache_conf():
    """官方版的缓存设置（cache 段）：服务开着、允许读主密钥时读 global-settings（它按这个在跑）；否则读它的 settings.json
    （下次启动用这个）。两处都含主密钥，只取 cache 段，别的不留。读不到返回 None。"""
    conf = None
    if key_consent() is True and health(LOCAL) == 'up':
        try: conf = api(LOCAL, '/admin/api/global-settings', timeout=5).get('cache')
        except Exception: conf = None
    if conf is None and ENGINE_HERE.settings:
        try: conf = json.loads(Path(ENGINE_HERE.settings).read_text()).get('cache')
        except (OSError, ValueError, AttributeError): conf = None
    return conf if isinstance(conf, dict) else None


def official_cache_limit(conf):
    """cache 段 → 上限：0 关，'auto' 是官方默认的"自动"（按硬盘剩余定），否则 GB；读不到 None。"""
    if not conf: return None
    if conf.get('enabled') is True and str(conf.get('ssd_cache_max_size', '')).strip().lower() == 'auto': return 'auto'
    return parse_cache_conf(json.dumps(conf))


def official_cache_dir(conf):
    """缓存放在哪：设了 ssd_cache_dir 就是它，没设是数据目录下的 cache（oMLX 的 get_ssd_cache_dir）。"""
    folder = conf.get('ssd_cache_dir') if conf else None
    if isinstance(folder, str) and folder.strip(): return Path(folder).expanduser()
    return Path(ENGINE_HERE['base']) / 'cache' if ENGINE_HERE.get('base') else None


def cache_used(folder):
    """缓存目录里那几段占了多少字节（和 PROBE 的 cache 同一算法）；进不了目录是 0。"""
    if not folder or not Path(folder).is_dir(): return 0
    try: r = subprocess.run(['/bin/sh', '-c', f'du -sk -c {CACHE_SHARDS} 2>/dev/null | tail -1 | cut -f1'], cwd=folder, capture_output=True, text=True, errors='replace', timeout=15)
    except subprocess.TimeoutExpired: return 0
    return int(r.stdout.strip()) * 1024 if r.stdout.strip().isdigit() else 0


def official_cache():
    """官方版的多轮缓存：上限读接口（摸底：直接读文件会核对失败），用量按它的缓存目录自己算（它没有"已用多少"）。"""
    conf = official_cache_conf()
    return {'cache_limit': official_cache_limit(conf), 'cache': cache_used(official_cache_dir(conf))}


def footprint(pid):
    """进程的实际占用（phys_footprint，见 FOOTPRINT_PY），本机直接调系统函数读，约 30 微秒；读不到返回 None。"""
    buf = (ctypes.c_uint64 * 32)()
    if ctypes.CDLL('/usr/lib/libproc.dylib').proc_pid_rusage(pid, 2, buf) != 0: return None
    return buf[9]


def remote_stats(node):
    """远程机器的内存、GPU、硬盘、oMLX 进程、缓存大小和 oMLX 存的模型设置，一次只读 SSH 取完。"""
    if is_rpc(node):
        state = rpc_state(node, full=True)
        return {'online': True, **state['stats']} if state else {'online': False}
    try: r = ssh_to(node, probe_script(REMOTE_PARTS), timeout=15)
    except subprocess.TimeoutExpired: return {'online': False}
    if r.returncode: return {'online': False}
    out = parse_probe(r.stdout)
    ENGINE[node] = out.pop('engine', {})
    if 'have' in out: keep_node_data(node, {name: out.pop(name, None) for name in ('have', 'facts', 'notes')})
    return {'online': True, **out}


def node_snapshot(node, probe=None):
    """一台的服务和模型状态。远程机器的 oMLX 在跑而隧道断了时，顺手把隧道接上。"""
    if is_rpc(node):
        state = rpc_state(node)
        return state['node'] if state else {'health': None, 'up': False, 'models': {n: 'off' for n in MODELS}, 'committed': 0, 'ceiling': 0}
    listening = probe.get('omlx') if probe else None
    if not is_local(node) and listening: reach(node)
    out = {'health': health(node, listening, probe.get('answers') if probe else None), 'models': {n: 'off' for n in MODELS}, 'committed': 0, 'ceiling': 0}
    data = model_rows(node) if out['health'] == 'up' else None
    out['up'] = data is not None
    if data:
        discover(node, data)   # oMLX 列出来、清单里没有的，先认出来（2b）
        out['models'] = node_models(node, data)
        out['committed'], out['ceiling'] = data.get('current_model_memory', 0), data.get('final_ceiling', 0)
        out['rows'] = {m['id']: m for m in data.get('models', [])}
    return out


def snapshot(check_worker=True):
    s = {'ollama': ollama_loaded(), 'stats': {}, 'nodes': {}}
    for node in NODES:
        s['stats'][node] = local_stats() if is_local(node) else remote_stats(node) if check_worker else {'online': None}
    for node in NODES:
        probe = s['stats'][node] if not is_local(node) and check_worker else None
        with models_of(node):   # 每台按它自己的清单（2b）
            if probe is not None and not probe['online']:
                s['nodes'][node] = {'health': None, 'up': False, 'models': {n: 'off' for n in MODELS}, 'committed': 0, 'ceiling': 0}
            else:
                s['nodes'][node] = node_snapshot(node, probe)
    return s


def live_context(n, name):
    """oMLX 里这个模型此刻的上限（只认已加载的）；没加载返回 None。n 是 node_snapshot 的结果。"""
    row = n.get('rows', {}).get(MODELS[name]['id'], {})
    return row.get('max_context_window') if row.get('loaded') else None


def context_info(node, name, n, total=None):
    """一台上一个模型的上下文：设置值、oMLX 里的实时值、模型上限、能选的档位和每档的说明。
    档位不超过模型上限；正在用的和存的值不在档内时也列出。total 是这台的总内存，给了才算 over：
    对话塞满时超内存安全线的档位，照样能选，由用户决定。"""
    model = MODELS[name]
    row = n.get('rows', {}).get(model['id'], {})
    native = row.get('model_context_length') or model['native']
    live, setting = live_context(n, name), node_context(node, name)
    options = sorted({v for v in CONTEXTS if v <= native} | {setting} | ({live} if live else set()))
    # 在跑的模型还要给同一台别的在线模型留地方；没在跑的这一档是下次加载用的，那时别的模型不一定还在
    others = sum(model_bytes(o, node, live_context(n, o), n) for o in active(n['models']) if o != name) if live else 0
    line = total * alert_percent() / 100 if total else None
    return {'setting': setting, 'live': live, 'native': native, 'options': options,
            'marks': {str(v): context_mark(name, v) for v in options},
            'notes': {str(v): context_note(name, v) for v in options},
            'compact': {str(v): compact_text(v) for v in options} if has_dsh() else {},   # dsh 的压缩点；没有 dsh 不说
            'over': [v for v in options if line and model_bytes(name, node, v, n) + others > line],
            'recommended': next(iter(model.get('recommended') or []), None),   # ③里的显式字段，不再看文字是不是"推荐"开头
            'max_output': row.get('max_tokens')}


def active(models):
    return [name for name, st in models.items() if st != 'off']


def ollama_bytes(s):
    return sum(m.get('size', 0) for m in (s.get('ollama') or []))


def weight_key(name, opts):
    """实测权重按开关组合分开记：切了表放 SSD（Qwen 差约 95 GB）或 MTP，不拿旧组合的数去预判。
    MTP 关、表在默认位置时就是模型名本身，10-31 以前记下的数照常可用。"""
    return name + ('+mtp' if opts['mtp'] else '') + ('+ssd' if opts['ssd'] and option_mode(name, 'ssd') == 'free' else '')


def weight_bytes(name, node=None, n=None, opts=None):
    """权重占多少：这台上次在同样开关下加载后量到的（record_weight）> 在线但没量过的（不是经 clusterctl 加载的）
    用 oMLX 自己报的 actual_size > MODELS 里的常数。同一个模型两台不一定一样（副机 上的 DeepSeek 量到 288 GB，常数是 269）。"""
    model = MODELS[name]
    row = (n or {}).get('rows', {}).get(model['id'], {})
    if row.get('loaded'): opts = opts or live_options(node, name)  # 在线的按它正在用的开关算
    opts = opts or node_options(node, name)  # 不给机器就是模型默认值
    known = saved(node).get('weights', {}).get(node, {}).get(weight_key(name, opts))
    if known: return known
    if row.get('loaded') and row.get('actual_size'): return row['actual_size']
    base = model['ssd_gib'] if opts['ssd'] and option_mode(name, 'ssd') == 'free' else model['size_gib']
    return (base + (model['mtp_gib'] if opts['mtp'] else 0)) * GIB


def record_weight(node, name):
    """加载完、自检通过后量 oMLX 进程此刻的实际占用，记作这台这组开关下的权重，预判"放不放得下"和"最多约"时用。
    这时进程里只有这一个模型，量到的比纯权重多 1–3 GB（进程本身），宁多勿少。不用 oMLX 自己报的 actual_size：
    它是"加载后 − 加载前"，刚卸载完上一个模型时进程里还残留十几到几十 GB 没释放，"加载前"偏大，减出来就少
    （10-31 评测 主机 的 Qwen 报 317.6、266.3 GB，进程实占 331.9、336.8，和 副机 的 337.1 一致）。"""
    now = (local_stats() if is_local(node) else remote_stats(node)).get('omlx_now')
    opts = applied_options(node, name)
    if not now or not opts: return
    conf = saved()
    conf.setdefault('weights', {}).setdefault(node, {})[weight_key(name, opts)] = now
    save_conf(conf)


def model_bytes(name, node=None, context=None, n=None, opts=None):
    """权重 + 塞满上下文时的 KV。不给机器就按模型默认上下文、默认开关和常数估。"""
    context = context or (node_context(node, name) if node else MODELS[name]['context'])
    return weight_bytes(name, node, n, opts) + context * MODELS[name]['kv']


def omlx_max(s, node):
    """oMLX 在这台上最多会占多少：在线（含加载中）模型的权重 + 对话塞满上下文时的 KV。"""
    n = s['nodes'][node]
    return sum(model_bytes(name, node, live_context(n, name), n) for name in active(n['models']))


def memory_view(s, node):
    """一台的内存（字节）：(已用, oMLX 现在实占, 对话塞满时整台最多约)；没有系统数据时返回 None。
    已用里本来就含 oMLX 现在的占用（主机 上还含 Ollama），所以"最多" = 已用 − oMLX 现在 + oMLX 最多。"""
    probe = s['stats'].get(node, {})
    if 'used' not in probe: return None
    used, now, most = probe['used'], probe.get('omlx_now'), omlx_max(s, node)
    if not most: return used, now, used
    return used, now, max(used, used - now + most if now is not None else most)


def node_view(node):
    """只取一台的状态，给 memory_view()、fits_alongside() 用。"""
    probe = local_stats() if is_local(node) else remote_stats(node)
    return {'stats': {node: probe}, 'nodes': {node: node_snapshot(node, None if is_local(node) else probe)}}


def fits_alongside(name, node, s=None, opts=None, replacing=False):
    """不卸载别的，再放这个模型行不行：(能不能, 加上后整台最多约多少字节)。两条都要满足：
    ① 按最坏情况不超过安全线：现在整台最多约（各模型对话都塞满）+ 它的权重 + 它塞满上下文的 KV；
    ② 权重不超过 oMLX 的软水位：否则它加载时会自己卸载最久没用的模型，等于没保住。
    replacing：这个模型正在跑、要按新开关重新加载，先扣掉它现在的占用。读不到内存算放不下。"""
    s = s or node_view(node)
    view, n = memory_view(s, node), s['nodes'][node]
    if not view: return False, 0
    gone = model_bytes(name, node, live_context(n, name), n) if replacing else 0
    after = view[2] + model_bytes(name, node, opts=opts) - gone
    weights = n.get('committed', 0) + weight_bytes(name, node, opts=opts) - (weight_bytes(name, node, n) if replacing else 0)
    ceiling = n.get('ceiling', 0)
    return after <= s['stats'][node]['total'] * alert_percent() / 100 and (not ceiling or weights <= ceiling * OMLX_SOFT_WATERMARK), after


def memory_alert(node, used, most, ollama, total, pressure, percent):
    """(超线原因, 提醒)。已用超过安全线、或系统内存压力报警，算超线（红）；还没超、但对话塞满上下文后会超，
    算提醒（黄）。参数都是字节和数字，不读系统状态，便于用假数字测试。"""
    line, reasons, notes, name = total * percent / 100, [], [], where(node)
    extra = tr('（其中 Ollama 约 {0}）', gb(ollama)) if ollama else ''
    if used > line:
        reasons.append(tr('{0} 已用约 {1}{2}，超过安全线 {3}（总内存的 {4}%）', name, gb(used), extra, gb(line), percent))
    elif most > line:
        notes.append(tr('{0} 已用约 {1}{2}；对话塞满上下文时最多约 {3}，会超过安全线 {4}', name, gb(used), extra, gb(most), gb(line)))
    if pressure >= 2:
        reasons.append(tr('{0} 系统内存压力{1}：macOS 已经在压缩内存或把内容换到硬盘', name, (tr('严重') if pressure >= 4 else tr('警告'))))
    return reasons, notes


def omlx_stats(node, n):
    """这台在线模型正在处理 / 排队的请求数和生成速度：正在生成时取实时速度，否则取本次服务启动以来的平均。
    这个管理接口的返回里带明文主密钥，取完立刻丢掉，不往外传。"""
    if is_rpc(node): return (rpc_state(node) or {}).get('omlx_stats', {})
    loaded = [name for name, st in n['models'].items() if st == 'loaded']
    if not n['up'] or not loaded: return {}
    try: data = api(node, '/admin/api/stats', timeout=5)
    except Exception: return {}
    data.pop('api_key', None)
    rows = {m.get('id'): m for m in (data.get('active_models') or {}).get('models', [])}
    out = {}
    for name in loaded:
        model_id = MODELS[name]['id']
        row = rows.get(model_id, {})
        info = {'requests_active': row.get('active_requests', 0), 'requests_waiting': row.get('waiting_requests', 0)}
        live = max((g.get('tokens_per_second') or 0 for g in row.get('generating') or []), default=0)
        if live > 0:
            info.update(speed_tps=round(live, 1), speed_live=True)
        else:
            try:
                avg = api(node, '/admin/api/stats?model=' + urllib.parse.quote(model_id), timeout=5)
                avg.pop('api_key', None)
                info.update(speed_tps=avg.get('avg_generation_tps') or 0, speed_live=False)
            except Exception:
                pass
        out[name] = info
    return out


def h3_state():
    if not has_h3(): return {'job': False, 'page': False, 'progress': None}   # 没装 H3：不用每次刷新都跑一次 ps
    progress = None
    current = ROOT / 'h3-jobs/current.json'
    if current.exists():
        try: progress = json.loads(current.read_text())
        except (OSError, json.JSONDecodeError): pass
    return {'job': process_running('scripts/run_h3.py'), 'page': bool(owned_pid(RUN_DIR / 'h3-ui-pid.json', 'pid', 'scripts/serve_h3.py')),
            'progress': progress}


# ---------- 卡住报警 ----------
# 本地模型写长内容（比如一整份文件）时，oMLX 要等整段生成完才发给客户端，客户端看到的是"很久没动静"，
# 分不清慢和死。这里看两台 oMLX 自己的逐请求计数（生成中的 token 数、读入进度）：有请求在跑、计数 4 分钟
# 没变才算卡住。应用每分钟查一次，所以最晚约 5 分钟发现。
STALL_SECONDS = 240
STALL_STATE = ROOT / 'stall-watch.json'


def progress_sample(node):
    """(模型名, 正在跑的请求数, 进度标记)；这台没有在线模型时返回 None。"""
    if is_rpc(node): return rpc_query(node, 'progress', timeout=15)
    data = api(node, '/admin/api/stats', timeout=5)
    data.pop('api_key', None)
    rows = {m.get('id'): m for m in (data.get('active_models') or {}).get('models', [])}
    found = []
    for name, model in MODELS.items():
        row = rows.get(model['id'])
        if row is None or row.get('is_loading'): continue
        marks = sorted([(str(g.get('request_id')), g.get('generated_tokens') or 0, 0) for g in row.get('generating') or []]
                       + [(str(p.get('request_id')), 0, p.get('processed') or 0) for p in row.get('prefilling') or []])
        found.append((name, row.get('active_requests') or 0, marks))
    return max(found, key=lambda f: f[1], default=None)


def notify(text):
    subprocess.run(['osascript', '-e', 'on run argv', '-e', tr('display notification (item 1 of argv) with title "ModelTower" sound name "Basso"'),
                    '-e', 'end run', '--', text], capture_output=True, timeout=10)


def progress_node(node, previous, now):
    """一台的进度判断；previous 是这台上次记下的状态，会被原地更新。"""
    try:
        with models_of(node): sample = progress_sample(node)   # 按这台的清单（2b）
    except Exception as e: return {'ok': False, 'state': 'unknown', 'text': tr('{0}：读取进度失败：{1}', where(node), e)}
    if sample is None or sample[1] == 0:
        previous.clear()
        return {'ok': True, 'state': 'idle', 'text': tr('{0} 没有正在处理的请求。', where(node))}
    name, count, marks = sample
    title = tr('{0} 的 {1}', where(node), MODELS[name]['title'])
    key = json.dumps(marks)
    if previous.get('model') != name or previous.get('key') != key:
        previous.clear()
        previous.update(model=name, key=key, since=now, noticed=False)
    still = int(now - previous['since'])
    tokens = sum(m[1] for m in marks)
    out = {'ok': True, 'node': node, 'model': title, 'requests': count, 'tokens': tokens, 'still_seconds': still}
    if still >= STALL_SECONDS:
        out.update(state='stalled', text=tr('{0} 有 {1} 个请求，已经 {2} 分钟没有新进度，可能卡住了。可以在客户端里停止这次请求；还不行就到控制台卸载后重新加载。', title, count, (still // 60)))
    else:
        read = sum(m[2] for m in marks)
        done = tr('已输出约 {0} token', tokens) if tokens else tr('正在读入，已读约 {0} token', read)
        out.update(state='working', text=tr('{0} 正在处理 {1} 个请求，进度正常（{2}）。', title, count, done))
    return out


def progress(send_notice=True):
    """state：down 各台服务都没开｜idle 没有请求｜working 有进展｜stalled 卡住。各台各判断，报最要紧的那台。"""
    previous = json.loads(STALL_STATE.read_text()) if STALL_STATE.exists() else {}
    if not all(isinstance(v, dict) for v in previous.values()): previous = {}  # 旧版（单台）格式
    now, results = time.time(), []
    for node in NODES:
        if not is_local(node) and not is_rpc(node) and not port_open(NODES[node]['port']): continue
        if health(node) != 'up': continue
        mine = previous.setdefault(node, {})
        out = progress_node(node, mine, now)
        if out['state'] == 'stalled' and not mine.get('noticed'):
            if send_notice: notify(out['text'])
            mine['noticed'] = True
        results.append(out)
    STALL_STATE.write_text(json.dumps(previous))
    if not results: return {'ok': True, 'state': 'down', 'text': tr('oMLX 没有运行。') if SOLO else tr('{0}的 oMLX 都没有运行。', EVERY)}
    rank = {'stalled': 3, 'unknown': 2, 'working': 1, 'idle': 0}
    worst = max(results, key=lambda r: rank.get(r['state'], 0))
    if worst['state'] == 'idle': return {'ok': True, 'state': 'idle', 'text': tr('没有正在处理的请求。')}
    busy = [r['text'] for r in results if r['state'] != 'idle']
    return {**worst, 'text': ' '.join(busy)}


# ---------- 文字状态 ----------

ALERT_RISK = tr('继续用下去会开始拿硬盘顶内存，模型变得极慢，严重时可能崩溃。')


def node_line(node, s):
    n, probe = s['nodes'][node], s['stats'].get(node, {})
    if probe.get('online') is False: return tr('{0} 连不上（确认它开着、网络和地址对；开着 FileVault 的要先解锁）', where(node))
    if probe.get('online') is None and not n['health']: return tr('{0} 没有检查', where(node))
    if not n['health']: return tr('{0} 服务没开', where(node))
    if n['health'] == 'reconnecting': return tr('{0} 正在重新连接', where(node))
    if not n['up']: return tr('{0} 服务启动中', where(node))
    on = active(n['models'])
    if not on: return tr('{0} 空闲', where(node))
    loading = [MODELS[m]['title'] for m in on if n['models'][m] == 'loading']
    if loading: return tr('{0} 正在加载 {1}', where(node), tr('、').join(loading))
    return tr('{0} 跑着 {1}', where(node), tr('、').join(MODELS[m]['title'] for m in on))


def describe(s, keys=True):
    """一句话说明各台的现状和下一步。keys=True 给终端菜单（带按键），False 给应用（说按钮）。"""
    say = lambda menu, app: menu if keys else app
    lines = tr('；').join(node_line(node, s) for node in NODES) + tr('。')
    if not any(n['health'] for n in s['nodes'].values()):
        return lines + say(tr('按 s 启动{0}的服务（只启动服务，不加载模型）。', EVERY), tr('点"启动服务"（只启动服务，不加载模型）。'))
    if not any(active(n['models']) for n in s['nodes'].values()):
        lists = [(node, node_has(node, s['nodes'][node])) for node in NODES]   # 各台自己的清单（2b）
        titles = list(dict.fromkeys(MODELS[name]['title'] for _, names in lists for name in names))
        if not titles: return lines + tr('还没有模型：在 oMLX 里下载一个，下载好后这里会列出来。')
        choices = tr('、').join(titles[:-1]) + tr(' 或 ') + titles[-1] if len(titles) > 1 else titles[0]
        if len(NODES) == 1: return lines + say(tr('加载 {0}（1–{1}）。', choices, sum(len(names) + 1 for _, names in lists)), tr('点 {0} 加载。', choices))   # 只有一台（第 5 期）
        return lines + say(tr('选一台加载 {0}（1–{1}）。', choices, sum(len(names) + 1 for _, names in lists)), tr('在任一台上点 {0} 加载。', choices))
    if not has_dsh(): return lines + tr('在客户端里选用（地址、模型名和子密钥见"密钥"页）；要换就在这里切。')
    return lines + say(tr('在 dsh 里按"机器 · 模型"选用；要换就在这里切。'), tr('在 dsh 里按"机器 · 模型"选用；要换就在这里切。'))


def label(text, width=12):
    shown = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in text)
    return '  ' + text + ' ' * max(1, width - shown)


def status(check_worker=True):
    s = snapshot(check_worker)
    line, percent = '━' * 66, alert_percent()
    if len(NODES) > 1: print(paint(tr('{0}\n  ModelTower · oMLX（{1} 各自单机，{2} 任选）\n{3}', line, tr('、').join(map(where, NODES)), ' / '.join(MODEL_TITLES), line), '1'))
    elif MODEL_TITLES: print(paint(tr('{0}\n  ModelTower · oMLX（{1}，{2} 任选）\n{3}', line, where(LOCAL), ' / '.join(MODEL_TITLES), line), '1'))   # 只有一台（第 5 期）
    else: print(paint(tr('{0}\n  ModelTower · oMLX（{1}）\n{2}', line, where(LOCAL), line), '1'))
    print(label(tr('现状')) + paint(describe(s), '1'))
    reasons, warns = [], []
    for node in NODES:
        n, probe = s['nodes'][node], s['stats'].get(node, {})
        title = tr('{0}（{1}）', where(node), tr(NODES[node]['role']))
        if not is_local(node) and not check_worker: print(label(title) + off(tr('没有检查（--no-worker）'))); continue
        if not is_local(node) and not probe.get('online'): print(label(title) + bad(tr('连不上：确认 {0} 开着、网络和地址对；开着 FileVault 的要先解锁', where(node)))); continue
        if n['up']: service = ok(tr('服务运行中 http://127.0.0.1:{0}', NODES[node]['port']))
        elif n['health']: service = warn(tr('服务正在启动或没有响应'))
        else: service = off(tr('服务没开'))
        print(label(title) + service)
        for name, st in n['models'].items():
            model = MODELS[name]
            ctx = live_context(n, name) or node_context(node, name)
            text = tr('{0} · 约 {1} · 上下文 {2}（模型上限 {3}）', model['note'], gb(model_bytes(name, node, ctx)), ctx_label(ctx), ctx_label(context_info(node, name, n)['native']))
            running, wanted = live_options(node, name) if st != 'off' else None, node_options(node, name)
            text += ' · ' + options_text(name, running or wanted)
            if running and running != wanted: text += tr('（已改为 {0}，下次加载生效）', options_text(name, wanted))
            if st == 'loaded': print(label('') + ok(tr('{0} 已加载 · {1}', model['title'], text)))
            elif st == 'loading': print(label('') + warn(tr('{0} 加载中… · {1}', model['title'], text)))
        if n['up'] and not active(n['models']): print(label('') + off(tr('没有加载模型')))
        view = memory_view(s, node)
        if view:
            used, now, most = view
            alert, notes = memory_alert(node, used, most, ollama_bytes(s) if is_local(node) else 0,
                                        probe['total'], probe.get('pressure', 1), percent)
            extra = ' · '.join(x for x in (f"GPU {probe['gpu']}%" if probe.get('gpu') is not None else '',
                                           tr('硬盘剩 {0}', gb(probe['disk_free'])) if probe.get('disk_free') else '',
                                           cache_text(node, n, probe)) if x)
            inside = tr('；').join(x for x in (tr('其中 oMLX {0}', gb(now)) if now is not None else '',
                                           tr('对话塞满时最多约 {0}', gb(most)) if most > used else '') if x)
            text = (tr('内存已用约 {0} / {1}', gb(used), gb(probe['total'])) + (tr('（{0}）', inside) if inside else '')
                    + f" · swap {probe['swap']} · {extra}")
            print(label('') + paint('  ' + text, '31' if alert else '33' if notes else '90'))
            reasons += alert
            warns += notes
    for reason in reasons: print(label(tr('内存超线')) + bad(paint(reason, '31')))
    for note in warns: print(label(tr('内存提醒')) + warn(note))
    if reasons: print(label('') + paint('  ' + ALERT_RISK + (tr('按 o 让 Ollama 释放，或卸载那台的模型。') if s['ollama'] is not None else tr('请卸载那台的模型。')), '31'))
    models = s['ollama']
    if models is None:
        if has_ollama(): print(label('Ollama') + off(tr('未运行')))
    elif not models: print(label('Ollama') + ok(tr('运行中 · 没有加载模型（只在 {0}）', where(LOCAL))))
    else:
        names = tr('、').join(f"{m['name']} {gb(m.get('size', 0))}" for m in models)
        print(label('Ollama') + warn(tr('已加载 {0}（和 {1} 的 oMLX 共用内存）', names, where(LOCAL))))
    h3 = h3_state()
    if h3['job']:
        progress = h3['progress'] if isinstance(h3['progress'], dict) else {}
        detail = progress.get('message') or progress.get('stage') or ''
        print(label(tr('H3 视频')) + warn(tr('正在生成视频') + (tr('：{0}', detail) if detail else '')))
    elif h3['page'] or has_h3():   # 没装 H3、也没有在跑的：不出这一行（第 5 期；副机上以前多这一行）
        print(label(tr('H3 视频')) + (ok(tr('视频页运行中 http://127.0.0.1:8810/')) if h3['page'] else off(tr('未启动'))))
    print(paint(line, '1'))


# ---------- 服务 ----------

def clear_links(node):
    """服务启动前撤掉 serving-models 里所有链接：启动后什么都不可见，要哪个模型由控制台加载时再链进来。
    只删链接，不碰真正的模型目录。官方版没有软链（模型在它自己的目录里），不做。"""
    if official(node): return
    if is_local(node):
        for link in SERVING.iterdir() if SERVING.is_dir() else []:
            if link.is_symlink(): link.unlink()
        return
    ssh_sure(node, f'mkdir -p "{SERVING}" && find "{SERVING}" -mindepth 1 -maxdepth 1 -type l -delete')


def clear_pins_on_start():
    """启动服务前要不要取消 oMLX 里的固定标记（9.9 d）：设置文件的 clear_pins_on_start，没写按开（你两台的老规矩：开机不自动加载）。"""
    return saved().get('clear_pins_on_start', True) is not False


def clear_pins():
    """oMLX 启动时会预加载固定（pin）的模型；服务没在运行时先清掉固定标记，保证启动后不自动加载。设置里选了保留就不动。"""
    if not MODEL_SETTINGS.exists() or not clear_pins_on_start(): return []
    data = json.loads(MODEL_SETTINGS.read_text())
    cleared = [mid for mid, conf in data.get('models', {}).items() if conf.get('is_pinned')]
    if cleared:
        for mid in cleared: data['models'][mid]['is_pinned'] = False
        temporary = MODEL_SETTINGS.with_name(MODEL_SETTINGS.name + '.tmp')
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.chmod(temporary, MODEL_SETTINGS.stat().st_mode & 0o777)
        os.replace(temporary, MODEL_SETTINGS)
        print(warn(tr('已取消固定标记：') + tr('、').join(cleared) + tr('（不然 oMLX 启动时会自动加载）')))
    return cleared


def push_config(node):
    """远程机器启动前：密钥、全局设置、每个模型的设置和启动脚本都以本机为准同步过去。走 SSH 的标准输入，
    不经过命令行参数和剪贴板，也不打印。缓存开不开、上限多大各台不同，写成这台自己的。"""
    conf = json.loads((STATE / 'settings.json').read_text())
    conf['server'].update(distributed_inference_enabled=False, server_aliases=['localhost', '127.0.0.1'])
    size = node_cache(node)
    conf.setdefault('cache', {}).update(enabled=bool(size), **({'ssd_cache_max_size': f'{size}GB'} if size else {}))
    models = json.loads(MODEL_SETTINGS.read_text()) if MODEL_SETTINGS.exists() else {'version': 1, 'models': {}}
    for entry in models.get('models', {}).values(): entry['is_pinned'] = False
    for name, data in (('settings.json', conf), ('model_settings.json', models)):
        path = STATE / name
        r = ssh_to(node, f'umask 077; mkdir -p "{STATE}" && cat > "{path}.tmp" && mv "{path}.tmp" "{path}"', timeout=30,
                   data=json.dumps(data, indent=2, ensure_ascii=False))
        if r.returncode: raise RuntimeError(tr('没能把 {0} 同步到 {1}：{2}', name, where(node), r.stderr.strip()[:200]))
    local = hashlib.sha256(SERVE_SCRIPT.read_bytes()).hexdigest()
    remote = ssh_to(node, f'shasum -a 256 "{ROOT}/scripts/serve_omlx.py" 2>/dev/null | cut -d" " -f1').stdout.strip()
    if remote != local:
        r = ssh_to(node, f'cat > "{ROOT}/scripts/serve_omlx.py.tmp" && mv "{ROOT}/scripts/serve_omlx.py.tmp" "{ROOT}/scripts/serve_omlx.py"',
                   timeout=30, data=SERVE_SCRIPT.read_text())
        if r.returncode: raise RuntimeError(tr('没能把启动脚本同步到 {0}。', where(node)))


def serve_node(node, quiet=False):
    """启动一台的 oMLX：按 HTTP 判断是否已在运行，别人拉起的实例也认得，不会起第二个。"""
    here, port, size = where(node), NODES[node]['port'], node_cache(node)
    cache = {'LOCAL_OMLX_CACHE': '1' if size else '0', 'LOCAL_OMLX_CACHE_SIZE': f'{size}GB'}
    if official(node): return serve_official(node, quiet)
    if is_local(node):
        if health(node):
            wait_ready(node)
            if not quiet: print(ok(tr('{0} 的 oMLX 已经在运行。', here)))
            return
        hold_lock(tr('启动服务'))
        clear_pins()
        clear_links(node)
        log = (RUN_DIR / 'omlx-server-control.log').open('ab')
        process = subprocess.Popen([str(ROOT / '.omlx-venv/bin/python'), str(SERVE_SCRIPT)], cwd=PROJECT,
                                   env=dict(os.environ, **cache, OMLX_NODE_TITLE=here), stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        (RUN_DIR / 'omlx-server-pid.json').write_text(json.dumps({'pid': process.pid}, indent=2))
        print(tr('正在启动 {0} 的 oMLX（不加载模型）…', here))
        wait_ready(node, process)
        print(ok(tr('{0} 的 oMLX 已启动：http://127.0.0.1:{1}', here, port)))
        return
    if is_rpc(node):
        rpc_action(node, 'serve')
        reach(node)
        return
    probe = remote_stats(node)
    if not probe['online']: raise RuntimeError(offline(node))
    if probe.get('omlx'):
        reach(node)
        if tunnel_broken(node): renew_tunnel(node)   # 旧隧道没断但不通：先换一条，不干等 120 秒
        wait_ready(node)
        if not quiet: print(ok(tr('{0} 的 oMLX 已经在运行。', here)))
        return
    push_config(node)
    clear_links(node)
    print(tr('正在启动 {0} 的 oMLX（不加载模型）…', here))
    r = ssh_to(node, f'cd "{ROOT}" && ({" ".join(f"{k}={v}" for k, v in cache.items())} OMLX_NODE_TITLE={shlex.quote(here)} nohup .omlx-venv/bin/python scripts/serve_omlx.py '
                     f'>> "{STATE}/server-control.log" 2>&1 < /dev/null &) ; echo started', timeout=20)
    if 'started' not in r.stdout: raise RuntimeError(tr('{0} 的 oMLX 没能启动：{1}', here, r.stderr.strip()[:200]))
    for _ in range(60):
        if remote_stats(node).get('omlx'): break
        time.sleep(1)
    if not ensure_tunnel(node): raise RuntimeError(tr('{0} 的 oMLX 起来了，但 {1} 到它的 SSH 隧道没接上。', here, where(LOCAL)))
    wait_ready(node)
    print(ok(tr('{0} 的 oMLX 已启动（{1} 经 http://127.0.0.1:{2} 访问）', here, where(LOCAL), port)))


def serve_official(node, quiet=False):
    """官方版：让 oMLX 自己起服务（App 走它的控制接口，Homebrew 走 omlx start），不加载模型。
    不由我们起进程、不撤链接、不清固定标记（启动时固定的模型由 oMLX 自己决定，草案 10.2）。"""
    here, port = where(node), NODES[node]['port']
    if health(node):
        wait_ready(node)
        if not quiet: print(ok(tr('{0} 的 oMLX 已经在运行。', here)))
        report_gateway(ensure_gateway(), quiet)
        return
    if ENGINE_HERE['kind'] == 'none': raise RuntimeError(tr('{0} 还没装 oMLX。', here))
    hold_lock(tr('启动服务'))
    code, detail = omlx_adapter.start(ENGINE_HERE)
    if code == 'unsupported': raise RuntimeError(tr('{0} 的 oMLX 不是官方 App 或 Homebrew 装的，控制台开关不了，请用它自己的方法启动。', here))
    if code == 'port_conflict': raise RuntimeError(tr('{0} 的 oMLX 起不来：端口 {1} 被别的程序占着（{2}）。', here, port, detail))
    if code == 'failed': raise RuntimeError(tr('{0} 的 oMLX 没能启动：{1}', here, detail or tr('打不开 oMLX 应用（{0}）', ENGINE_HERE.get('app') or '')))
    print(tr('正在启动 {0} 的 oMLX（不加载模型）…', here))
    wait_ready(node)
    print(ok(tr('{0} 的 oMLX 已启动：http://127.0.0.1:{1}', here, port)))
    report_gateway(ensure_gateway(), quiet)


def stop_official(node):
    """官方版：先卸载模型，再请 oMLX 自己停（App 留在菜单栏）。不直接结束进程：App 会把它拉起来。"""
    here, port = where(node), NODES[node]['port']
    import hub
    follows = hub.gateway_follows_omlx()   # 入口跟着 oMLX 停（正在答的答完再停）；主机模式照旧由主机管。没停成就留着入口
    if not health(node):
        if follows: hub.stop_gateway(drain=True)
        return False
    if ENGINE_HERE.get('how') not in ('app', 'brew'):
        raise RuntimeError(tr('{0} 的 oMLX 不是官方 App 或 Homebrew 装的，控制台关不了，请用它自己的方法关闭。', here))
    hold_lock(tr('关闭服务'))
    if health(node) == 'up': unload_all(node, quiet=True)
    code, detail = omlx_adapter.stop(ENGINE_HERE)
    if code == 'unsupported': raise RuntimeError(tr('{0} 的 oMLX 不是官方 App 或 Homebrew 装的，控制台关不了，请用它自己的方法关闭。', here))
    if code == 'not_running': raise RuntimeError(tr('{0} 端口 {1} 上的 oMLX 不归 oMLX 应用管（应用没开），控制台关不了，请用启动它的方法关闭。', here, port))
    if code == 'failed': raise RuntimeError(tr('{0} 的 oMLX 没能关闭：{1}', here, detail))
    for _ in range(60):
        if not port_open(port):
            if follows: hub.stop_gateway(drain=True)
            return True
        time.sleep(1)
    raise RuntimeError(tr('{0} 的 oMLX 60 秒内没有停下，请在 oMLX 里查看。', here))


def serve(target='all'):
    for node in pick_nodes(target): serve_node(node)


def listener(node):
    """(PID, 命令行)：这台 8800 端口上监听的进程。"""
    if is_rpc(node):
        pid = remote_stats(node).get('omlx')
        return pid, 'omlx' if pid else ''
    if is_local(node):
        out = subprocess.run(['lsof', '-nP', f'-tiTCP:{NODES[node]["port"] if official(node) else OMLX_PORT}', '-sTCP:LISTEN'], capture_output=True, text=True, errors='replace').stdout.split()
        pid = int(out[0]) if out else None
        command = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True, errors='replace').stdout if pid else ''
        return pid, command
    r = ssh_sure(node, f'p=$({PROBE["omlx"]}); [ -n "$p" ] && echo "$p" && ps -p "$p" -o command=')
    lines = r.stdout.strip().splitlines()
    return (int(lines[0]), ' '.join(lines[1:])) if lines and lines[0].isdigit() else (None, '')


def stop_node(node):
    if official(node): return stop_official(node)
    pid, command = listener(node)
    if is_rpc(node):  # 隧道留着：它也是 rpc 的复用连接，服务停了照样要查状态
        if pid: rpc_action(node, 'stop')
        return bool(pid)
    if not pid:
        if not is_local(node): stop_tunnel(node)
        return False
    if 'omlx' not in command and 'serve_omlx' not in command:
        raise RuntimeError(tr('{0} {1} 端口上的进程（PID {2}）不是 oMLX，不会去停它。', where(node), OMLX_PORT, pid))
    if is_local(node): hold_lock(tr('关闭服务'))
    if health(node) == 'up': unload_all(node, quiet=True)
    if is_local(node):
        os.kill(pid, signal.SIGTERM)
        alive = lambda: subprocess.run(['ps', '-p', str(pid)], capture_output=True).returncode == 0 or port_open(OMLX_PORT)
    else:
        ssh_sure(node, f'kill {pid}')
        def alive():
            stats = remote_stats(node)
            return stats.get('online') is False or bool(stats.get('omlx'))   # 这一下没连上是"不知道"，接着等，不能当成已经停了（T8）
    deadline = time.time() + 60   # 按墙上的钟：远程失联时每次探测就要几秒到十几秒，按次数算会等上十几分钟
    while time.time() < deadline:
        if not alive(): break
        time.sleep(1)
    else:
        if not is_local(node) and remote_stats(node).get('online') is False: raise Unreachable(offline(node))
        raise RuntimeError(tr('{0} 的 oMLX（PID {1}）60 秒内没有退出，请在活动监视器里查看；不要批量结束其他进程。', where(node), pid))
    if not is_local(node): stop_tunnel(node)
    return True


POWEROFF = '/sbin/shutdown -h now'
OWNER_CONFIRMED = 'owner-confirmed'   # 应用过了系统确认框才传，见 owner_proof
SUDOERS = '/etc/sudoers.d/mlx-poweroff'


def poweroff_rule():
    """给用户照抄的一行：只放行关机这一条命令。sudoers 属于系统设置，由用户本人在那台上写。"""
    return f'echo "$(id -un) ALL=(root) NOPASSWD: {POWEROFF}" | sudo tee {SUDOERS} >/dev/null && sudo chmod 440 {SUDOERS} && sudo visudo -cf {SUDOERS}'


def can_poweroff(node):
    """只认明写的免密规则。不能用 `sudo -n -l 命令`：机器上只要有一条免密规则，管理员的任何命令它都报成功（11-03 主机 实测）。"""
    listing = run_on(node, 'sudo -n -l 2>/dev/null').stdout
    rules = (line.partition('NOPASSWD:')[2].strip() for line in listing.splitlines())
    return any(POWEROFF in rule or rule == 'ALL' for rule in rules)


POWER_FILE = RUN_DIR / 'poweroff.json'
POWER_TIMEOUT = 180  # 发出关机后这么久还连得上，就算卡住
POWER_WAIT = int(os.environ.get('LMC_POWER_WAIT') or POWER_TIMEOUT)  # 环境变量只给测试用
POWER_STALE = 1800
POWER_FRESH = 60      # 断开后这么久内算刚关机（D4）；11-10 起界面不再分开说（已定第 26 条）


def power_marks():
    try: return json.loads(POWER_FILE.read_text())
    except (OSError, ValueError): return {}


def power_state(node, reachable, checked):
    """主机从外面看副机关机到哪一步：going_down 正在关机｜stuck 关机卡住｜off 已关机；没关过返回 None。
    关过机又连得上了，说明人按了电源键，记号就清掉。快速刷新不走 SSH（checked 为假），这时不下结论。"""
    marks = power_marks()
    mark = marks.get(node)
    if not mark: return None
    if not reachable:
        if checked and not mark.get('gone'):
            mark.update(gone=True, gone_at=time.time())
            POWER_FILE.write_text(json.dumps(marks))
        return {'state': 'off', 'since': mark['since'], 'fresh': time.time() - mark.get('gone_at', 0) < POWER_FRESH}
    if mark.get('gone'):
        if checked:
            del marks[node]
            POWER_FILE.write_text(json.dumps(marks))
            return None
        return {'state': 'off', 'since': mark['since'], 'fresh': time.time() - mark.get('gone_at', 0) < POWER_FRESH}
    age = time.time() - mark['since']
    if age > POWER_STALE:
        if checked:
            del marks[node]
            POWER_FILE.write_text(json.dumps(marks))
        return None
    return {'state': 'stuck' if age > POWER_TIMEOUT else 'going_down', 'since': mark['since']}


def clear_power_mark(node):
    marks = power_marks()
    if marks.pop(node, None) is not None: POWER_FILE.write_text(json.dumps(marks))


def is_hub_here():
    """这台现在是不是主机。没启用主机模式时看机器表里的角色。"""
    import hub
    data = hub.load()
    return data['hub'] == LOCAL if data else is_hub_node(LOCAL)


def finish_poweroff(node, mark=False):
    """停服务，再发关机。停不掉也继续：机器反正要关，不能因为它关不成。控制台不先退出，由系统结束它：
    关机命令发不出去时控制台还在，能报错、能重新启动 oMLX。用 shutdown，应用拦不住；代价是不提示保存。"""
    try:
        if stop_node(node): print(ok(tr('{0} 的 oMLX 已停止。', where(node))))
    except Exception as exc:
        print(warn(tr('{0} 的 oMLX 没停干净（{1}），继续关机。', where(node), exc)))
    try:
        if mark: print(GOING_DOWN, flush=True)
        result = run_on(node, f'sudo -n {POWEROFF}', timeout=15)
        # 连接都没建立（几个地址都试过了）：命令没送到，它还开着，不能报"正在关机"，全部关机也不能接着关主机
        if not is_local(node) and result.returncode == 255 and CONNECT_FAILED.search(result.stderr):
            raise RuntimeError(tr('{0} 没有关机：连不上它，关机命令没送到。', where(node)))
        # 远程的 255 是连接随关机断开；别的非零（比如规则刚被删）是真没关成，不能报"正在关机"
        if result.returncode not in ((0,) if is_local(node) else (0, 255)):
            raise RuntimeError(tr('{0} 没有关机：{1}。oMLX 已经停了，要用就重新启动。', where(node), result.stderr.strip()[:120]))
    except subprocess.TimeoutExpired: pass  # 连接随关机断开


def poweroff_here(proof=''):
    """rpc 那边调：这台关自己。主机不能被远程关，要人工关机（用户 11-04 定），这条限制做在被关的机器上。
    安全芯片的签名也在这里核对：发指令的机器就算被攻破、手里有钥匙，也签不出这个。"""
    import chip
    if is_hub_here(): raise RuntimeError(tr('{0} 是主机，只能在它自己的控制台关机。', where(LOCAL)))
    if not can_poweroff(LOCAL): raise RuntimeError(tr('{0} 还没允许控制台关机，什么都没动。到 {1} 的控制台：机器 → 本机授权 → 一次设好。', where(LOCAL), where(LOCAL)))
    if not chip.required() and OPERATOR_LEVEL != 'hub':
        # 没登记安全芯片（体检 R7）：拿着受限钥匙直接发一条 poweroff 不认，要主机的控制台确认过才带的那一行。
        # 和 owner_proof 一样不是安全措施，防的是脚本、测试、Agent 误发：真的会关机，关了远程开不回来
        import hub
        if hub.protected(): raise RuntimeError(tr('{0} 设了最高密码，只能在它自己的控制台关机，什么都没动。', where(LOCAL)))
        if not proof: raise RuntimeError(tr('关机要在主机的控制台里确认，什么都没动。主机的控制台是旧版的话，先更新它。'))   # 确认行，或主机登记了安全芯片时它签的那份（这台还没接受登记，验不了，但说明主机那边确认过）
    if proof == OWNER_CONFIRMED: proof = ''
    chip.check_op(proof, 'poweroff', LOCAL)
    finish_poweroff(LOCAL, mark=True)


def owner_proof(proof):
    """没开任何保护时，关机只认控制台传的 owner-confirmed 和终端里答 y，别的内容一律不认。
    这不是安全措施，只防脚本、测试和 Agent 误跑：真的会关机，关了远程开不回来。"""
    import chip, hub
    if proof == OWNER_CONFIRMED: return ''
    if not sys.stdin.isatty() and chip.current() is None and not hub.protected():
        raise RuntimeError(tr('关机要在控制台里按，或者在终端里确认，什么都没动。'))
    return proof


def poweroff_gate(node):
    """关机的保护按用户开了什么来（用户 11-04 定：局域网自用，默认不用提前设置）：系统确认框在应用里；
    登记了安全芯片就核对签名，设了最高密码就核对密码，都从标准输入读。返回那份确认，发给对方再核对一次。"""
    import chip, hub
    password, proof = hub.read_password_if_set(), owner_proof(hub.read_proof())
    # 先验签名再验密码：不然任何一把钥匙发 5 次错密码，就能把最高密码锁 10 分钟
    body = chip.check_signed(proof, 'poweroff')
    if body is not None and node not in body['targets']: raise RuntimeError(tr('这次确认不包括这台，什么都没动。'))
    hub.check_if_set(password)
    return proof


def poweroff(node, assume_yes=False, proof=None, asked=False):
    """关机（用户 11-04 定，11-10 放宽，决定 9）：每台的控制台都能关自己，主机的控制台还能关副机；别的机器经 rpc 关主机
    仍然拒绝（决定 6，做在被关的机器上，见 poweroff_here）。开机只能人工按电源键。先查权限、再确认，都过了才停服务。
    发出前就记下"正在关机"（D4），主机从外面盯着，status 报正在关机、已关机或卡住。
    proof 是已经核对过的确认（管理版、全部关机）；没有就从标准输入读密码和确认。asked：调用方已经问过人了。"""
    import chip
    if node not in NODES: raise RuntimeError(tr('关哪台？{0}', ' | '.join(NODES)))
    if not is_local(node) and is_hub_node(node): raise RuntimeError(tr('{0} 是主机，只能在它自己的控制台关机。', where(node)))
    if is_local(node) and not can_poweroff(LOCAL): raise RuntimeError(tr('{0} 还没允许控制台关机，什么都没动。到 {1} 的控制台：机器 → 本机授权 → 一次设好。', where(LOCAL), where(LOCAL)))
    if not is_local(node) and not is_rpc(node) and not remote_stats(node)['online']: raise RuntimeError(tr('{0} 连不上，可能已经关机了。', where(node)))
    if not (asked or (assume_yes and not sys.stdin.isatty()) or ask(tr('将先卸载 {0} 上的模型、停止 oMLX，再关机。关机后只能到机器上按电源键。确定吗？[y/N] ', where(node)))):
        print(tr('已取消。'))
        return
    if proof is None: proof = poweroff_gate(node)
    if is_local(node):
        chip.check_op(proof, 'poweroff', LOCAL)
        print(ok(tr('{0} 正在关机。要再开机，到机器上按电源键。', where(LOCAL))), flush=True)
        finish_poweroff(LOCAL)   # 本机不记"正在关机"：开机回来它还在，卡片会一直说正在关机
        return
    if is_rpc(node): chip_align(node)
    else:
        if not can_poweroff(node):
            raise RuntimeError(tr('{0} 还没允许控制台关机，什么都没动。到 {1} 的终端运行一次：{2}', where(node), where(node), poweroff_rule()))
        # 老路走全权限 SSH，对方不核对，这边替它核对。要真正挡住同账号的程序，得等它只认受限钥匙（§8）
        chip.check_op(proof, 'poweroff', node)
    POWER_FILE.write_text(json.dumps({**power_marks(), node: {'since': time.time()}}))   # D4：发出前就记，关机过程中卡片就说正在关机
    try:
        if is_rpc(node): rpc_action(node, 'poweroff', data=(proof or OWNER_CONFIRMED) + '\n')   # 到这里已经过了这边的确认（poweroff_gate）；没有签名时带上确认行（R7）
        else: finish_poweroff(node)
    except BaseException:
        clear_power_mark(node)
        raise
    print(ok(tr('{0} 正在关机。要再开机，到机器上按电源键。', where(node))))


def poweroff_all(assume_yes=False):
    """全部关机（用户 11-10 定，决定 9）：只在主机上有。先逐台关开着的副机，盯到都关掉（合计最多 3 分钟）再关这台；
    有副机关不掉，这台就留着，说清是哪台。连不上的副机当已经关了。保护和单台关机一样，确认要覆盖每一台。
    管理版的 poweroff_by_manager 是同一个顺序，那边不等请求答完、还能关别处的主机，所以分开写。"""
    import chip, hub
    if not is_hub_here(): raise RuntimeError(tr('全部关机只在主机上有。'))
    subs = [n for n in remotes() if not is_hub_node(n) and reachable_now(n)]
    targets = [*subs, LOCAL]
    missing = [where(n) for n in targets if not can_poweroff_any(n)]
    if missing: raise RuntimeError(tr('{0} 还没允许控制台关机，什么都没动。到那台的控制台：机器 → 本机授权 → 一次设好。', tr('、').join(missing)))
    if not ((assume_yes and not sys.stdin.isatty()) or ask(tr('将先关 {0}，确认关掉后再关 {1}。关机后只能到机器上按电源键。确定吗？[y/N] ', tr('、').join(map(where, subs)) or tr('（没有开着的副机）'), where(LOCAL)))):
        print(tr('已取消。'))
        return
    password, proof = hub.read_password_if_set(), owner_proof(hub.read_proof())
    body = chip.check_signed(proof, 'poweroff')   # 先验签名再验密码，理由见 poweroff_gate
    if body is not None and not set(targets) <= set(body['targets']): raise RuntimeError(tr('这次确认不包括要关的机器，什么都没动。'))
    hub.check_if_set(password)
    chip.check_op(proof, 'poweroff', LOCAL)
    failures = []
    for node in subs:
        try: poweroff(node, proof=proof, asked=True)
        except Exception as exc: failures.append(tr('{0}：{1}', where(node), exc))
    if failures: raise RuntimeError(tr('；').join(failures) + tr('。{0} 没有关，好继续看它们。', where(LOCAL)))
    deadline, left, quiet = time.time() + POWER_WAIT, list(subs), {}
    if left: print(tr('等 {0} 关掉（最多 {1} 秒）…', tr('、').join(map(where, left)), POWER_WAIT), flush=True)
    while left and time.time() < deadline:
        time.sleep(min(5, POWER_WAIT / 3))
        quiet = {n: quiet.get(n, 0) + 1 if powered_off(n) else 0 for n in left}   # 连续两次都没回应才算关掉
        left = [n for n in left if quiet[n] < 2]
    if left: raise RuntimeError(tr('{0} 没关掉，{1} 先留着，好继续看它们。要关 {2} 再单独点一次。', tr('、').join(map(where, left)), where(LOCAL), where(LOCAL)))
    marks = power_marks()
    for node in subs: marks.setdefault(node, {'since': time.time()}).update(gone=True, gone_at=time.time())
    POWER_FILE.write_text(json.dumps(marks))
    print(ok(tr('{0} 正在关机。要再开机，到机器上按电源键。', where(LOCAL))), flush=True)
    finish_poweroff(LOCAL)


SILENT = {errno.ETIMEDOUT, errno.EHOSTDOWN, errno.EHOSTUNREACH}


def port_word(code):
    """open 通；refused 拒绝（机器开着，只是 sshd 不在了，比如关机关到一半）；silent 没回应（那台关了、拔了线）；
    unknown 别的（比如这台自己没网，到不了那个网段）：不当成关了，免得主机自己断网时误以为副机都关了。"""
    return 'open' if code == 0 else 'refused' if code == errno.ECONNREFUSED else 'silent' if code in SILENT else 'unknown'


def port_states(ips, port=22, timeout=2.0):
    """同时试这些地址的端口，每个地址回一个 port_word；限时内没回的算 silent。"""
    out, waiting = {}, {}
    for ip in ips:
        s = socket.socket()
        s.setblocking(False)
        code = s.connect_ex((ip, port))
        if code == errno.EINPROGRESS: waiting[s] = ip
        else:
            out[ip] = port_word(code)
            s.close()
    deadline = time.monotonic() + timeout
    while waiting:
        left = deadline - time.monotonic()
        if left <= 0: break
        _, done, _ = select.select([], list(waiting), [], left)
        for s in done:
            ip, code = waiting.pop(s), s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            s.close()
            out[ip] = port_word(code)
    for s, ip in waiting.items():
        out[ip] = 'silent'
        s.close()
    return out


def powered_off(node):
    """这台是不是真的关掉了：登记的每个地址的 SSH 端口都没回应。拒绝连接说明机器还开着（关机关到一半 sshd 先停了），不算。
    没登记地址的按 SSH 配置里的地址试；解析不了的名字跳过，一个都试不了就不算关了（宁可不关主机）。"""
    ips = [a['ip'] for a in addresses(node)] or [ssh_shown(node).get('hostname') or '']
    resolved = []
    for ip in ips:
        try: resolved.append(ip if re.fullmatch(ADDRESS_IP, ip) else socket.gethostbyname(ip))
        except OSError: pass
    states = port_states(resolved, ssh_port(node)) if resolved else {}
    return bool(states) and all(v == 'silent' for v in states.values())


def gone_now(node):
    """这台是不是真的连不上了：重新问一次，不读缓存。回了错误说明它还在，算没关掉。"""
    RPC_STATE.pop(node, None)
    try: return not reachable_now(node)
    except RuntimeError: return False


def can_poweroff_any(node):
    """这台写了关机规则没有。走 rpc 的机器要问它自己，连不上算没有。"""
    if not is_rpc(node): return can_poweroff(node)
    try: return (rpc_query(node, 'power-check', timeout=15) or {}).get('ok') is True
    except (RuntimeError, subprocess.TimeoutExpired): return False


def poweroff_by_manager(target, password, proof=''):
    """管理版的关机（用户 11-04 定）：一台或全部，主机也能关，这样人不在也能远程切断，切断后远程开不回来。
    不等请求答完，运行中的操作也拦不住；开了安全芯片、最高密码就核对。先查每台的权限，都过了才动。
    副机先关，盯到真的关了才关这台：这台一关就没人报告了，副机关不掉时这台留着，让管理版看得到。"""
    import chip, hub
    proof = owner_proof(proof)
    body = chip.check_signed(proof, 'poweroff')   # 先验签名再验密码，理由见 poweroff_gate
    hub.check_if_set(password)
    nodes = [n for n in pick_nodes(target) if reachable_now(n)]
    if not nodes: raise RuntimeError(tr('要关的机器都连不上，可能已经关机了。'))
    if body is not None and not set(nodes) <= set(body['targets']): raise RuntimeError(tr('这次确认不包括要关的机器，什么都没动。'))
    missing = [where(n) for n in nodes if not can_poweroff_any(n)]
    if missing: raise RuntimeError(tr('{0} 还没允许控制台关机，什么都没动。到那台的控制台：机器 → 本机授权 → 一次设好。', tr('、').join(missing)))
    if LOCAL in nodes: chip.check_op(proof, 'poweroff', LOCAL)
    subs, failures, failed = [n for n in nodes if not is_local(n)], [], set()
    for node in subs:
        try: poweroff(node, assume_yes=True, proof=proof)
        except Exception as exc:
            failures.append(tr('{0}：{1}', where(node), exc))
            failed.add(where(node))
    if LOCAL not in nodes:
        if failures: raise RuntimeError(tr('；').join(failures))
        return
    deadline = time.time() + POWER_WAIT
    left = list(subs)
    while left and time.time() < deadline:
        time.sleep(min(5, POWER_WAIT / 3))
        left = [n for n in left if not gone_now(n)]
    if failures or left:
        names = tr('、').join(sorted({where(n) for n in left} | failed))
        raise RuntimeError(tr('{0} 没关掉，{1} 先留着，好继续看它们。要关 {2} 再单独点一次。', names, where(LOCAL), where(LOCAL)))
    marks = power_marks()
    for node in subs: marks.get(node, {}).update(gone=True, gone_at=time.time())
    POWER_FILE.write_text(json.dumps(marks))
    print(ok(tr('{0} 正在关机。要再开机，到机器上按电源键。', where(LOCAL))))
    finish_poweroff(LOCAL, mark=True)


# ---------- D1（用户 11-10 定，已定第 17 条）：副机自己回来时，主机自动启动它的 oMLX ----------
# 主机每次刷新看到一台服务在跑，就记"要开"；用户点关闭、关闭全部时记"不开"；从控制台关机不改它；点解锁记"要开"。
# 记在文件里，应用重开也还在。触发在应用里（主机控制台开着、设置里打开控制台时启动 oMLX 开着、没有操作在跑），见 autostart。
WANTS_FILE = RUN_DIR / 'wants-up.json'


def wants_records():
    """{机器: {'up': 要不要开, 'at': 什么时候记的}}。"""
    try: data = json.loads(WANTS_FILE.read_text())
    except (OSError, ValueError): return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get('up'), bool)} if isinstance(data, dict) else {}


def wants_up(): return {k: v['up'] for k, v in wants_records().items()}


def set_wants(nodes, value, since=None):
    """since：刷新开始的时间。刷新要花几秒，这期间用户点了关闭，记录比它新，刷新就不盖它。"""
    data = wants_records()
    changed = {n: {'up': value, 'at': time.time()} for n in nodes
               if (data.get(n) or {}).get('up') is not value and not (since and (data.get(n) or {}).get('at', 0) >= since)}
    if changed: write_json(WANTS_FILE, {**data, **changed})


# ---------- 控制台和 oMLX 同进退（用户 11-04 定）：启动、关闭、重启都带上副机的控制台 ----------

from brand import BUNDLE_ID as CONSOLE_BUNDLE, EXECUTABLE as CONSOLE_PROCESS  # noqa: E402  品牌集中在 brand.py


def console_action(what):
    """在这台打开或退出控制台窗口。不走 AppleScript：经 SSH 会碰上自动化权限弹窗。退出用 TERM，不弹确认。
    没装应用、没登录桌面时打不开，不算失败：oMLX 不依赖窗口。"""
    others = [pair for pair in brand.KNOWN_APPS if pair[0] != CONSOLE_BUNDLE]   # 这台装的可能是另一种名字的版本（第 6 期）
    commands = ([['open', '-g', '-b', CONSOLE_BUNDLE], *(['open', '-g', '-b', bundle] for bundle, _ in others),
                 ['open', '-g', str(Path.home() / 'Applications/ModelTower.app')]] if what == 'open'
                else [['pkill', '-TERM', '-x', CONSOLE_PROCESS], *(['pkill', '-TERM', '-x', process] for _, process in others)])
    try: return any(subprocess.run(command, capture_output=True, timeout=10).returncode == 0 for command in commands)
    except (OSError, subprocess.TimeoutExpired): return False


def console_remote(node, what):
    """让副机的控制台开或关。本机的窗口由应用自己管。"""
    if is_local(node): return
    try:
        if is_rpc(node): done = (rpc_query(node, f'console-{what}', timeout=15) or {}).get('ok')
        else:
            command = f'open -g -b {CONSOLE_BUNDLE} || open -g "$HOME/Applications/ModelTower.app"' if what == 'open' else f'pkill -TERM -x {CONSOLE_PROCESS}'
            done = ssh_to(node, command, timeout=15).returncode == 0
    except (RuntimeError, subprocess.TimeoutExpired): done = False
    if what == 'open' and not done: print(off(tr('{0} 的控制台窗口没打开（可能没装，或没登录桌面）；oMLX 不受影响。', where(node))))


def reachable_now(node):
    if is_local(node): return True
    return remote_stats(node).get('online') is not False


def up(target='all'):
    """启动：oMLX 和副机的控制台一起。不加载模型。连不上的机器：指定了就报错，全部时跳过。"""
    failures = []
    for node in pick_nodes(target):
        if not reachable_now(node):
            if target not in (None, 'all'): raise RuntimeError(offline(node))
            print(off(tr('{0} 连不上，先跳过。', where(node))))
            continue
        if target in (None, 'all') and (power_state(node, True, False) or {}).get('state') == 'going_down':
            print(off(tr('{0} 正在关机，先跳过。', where(node))))
            continue
        clear_power_mark(node)
        try:
            serve_node(node)
            console_remote(node, 'open')
        except Exception as exc: failures.append(tr('{0}：{1}', where(node), exc))
    if failures: raise RuntimeError(tr('；').join(failures))


def live_targets(target):
    """连得上的机器，本机排最后：中途一直有人报告。指定的那台连不上就报错；全部时跳过。"""
    nodes = pick_nodes(target)
    live = [n for n in nodes if reachable_now(n)]
    if target not in (None, 'all') and not live: raise RuntimeError(offline(nodes[0]))
    for node in nodes:
        if node not in live: print(off(tr('{0} 连不上，先跳过。', where(node))))
    return sorted(live, key=is_local)


def down(target='all', assume_yes=False):
    """关闭：卸载模型、停 oMLX，副机连控制台一起退出。主机最后关，中途一直有人报告。"""
    nodes = live_targets(target)
    if not (assume_yes or ask(tr('将卸载 {0} 的模型、停止 oMLX，副机的控制台也一起退出。确定吗？[y/N] ', tr('、').join(map(where, nodes))))):
        print(tr('已取消。'))
        return
    failures = []
    for node in nodes:
        try:
            if stop_node(node): print(ok(tr('{0} 的 oMLX 已停止。模型文件和设置都保留。', where(node))))
            set_wants([node], False)   # 有意关掉的，回来时不自动启动（D1）
            console_remote(node, 'quit')
        except Exception as exc: failures.append(tr('{0}：{1}', where(node), exc))
    if failures: raise RuntimeError(tr('；').join(failures))


def cycle(target='all', reload_models=False, assume_yes=False):
    """重启：oMLX 重启，副机的控制台重开。reload_models 把原来在跑的模型加载回来（用户 11-04 定，界面默认勾上）；
    平时打开控制台还是不自动加载。一台没成不影响下一台，最后一起报。"""
    nodes = live_targets(target)
    if not (assume_yes or ask(tr('将重启 {0} 的 oMLX。确定吗？[y/N] ', tr('、').join(map(where, nodes))))):
        print(tr('已取消。'))
        return
    s = snapshot(True)
    loaded = {n: [m for m, st in s['nodes'][n]['models'].items() if st == 'loaded'] for n in nodes}
    failures = []
    for node in nodes:
        try:
            if stop_node(node): print(ok(tr('{0} 的 oMLX 已停止。', where(node))))
            console_remote(node, 'quit')
            serve_node(node)
            console_remote(node, 'open')
            if reload_models:
                with models_of(node):   # 按这台的清单加载回来（2b）
                    for i, name in enumerate(loaded[node]):
                        start(name, node, assume_yes=True, keep=i > 0)
        except Exception as exc: failures.append(tr('{0}：{1}', where(node), exc))
    if failures: raise RuntimeError(tr('；').join(failures))


# ---------- 管理页：控制台代为登录 ----------

SESSION_COOKIE = 'omlx_admin_session'


def login_here():
    """向这台的 oMLX 换一个管理页会话（24 小时，签名里没有主密钥）。只在这台本机做，主密钥不出这台。"""
    request = urllib.request.Request(f"http://127.0.0.1:{NODES[LOCAL]['port']}/admin/api/login", data=json.dumps({'api_key': main_key(), 'remember': False}).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=10) as response:
        for value in response.headers.get_all('Set-Cookie') or []:
            found = re.match(rf'{SESSION_COOKIE}=([^;]+)', value)
            if found: return found[1]
    raise RuntimeError(tr('{0} 的 oMLX 没有发登录会话。', where(LOCAL)))


def admin_session(node):
    """给应用用：管理页地址和会话，只走标准输出的管道，不记日志、不写文件。"""
    if node not in NODES: raise RuntimeError(tr('哪台？{0}', ' | '.join(NODES)))
    if is_local(node): value = login_here()
    elif is_rpc(node): value = rpc_query(node, 'admin-session', timeout=20)['value']
    else:
        code = ("import json,re,urllib.request as u;"
                f"k=json.load(open('{STATE / 'settings.json'}'))['auth']['api_key'];"
                f"r=u.urlopen(u.Request('http://127.0.0.1:{OMLX_PORT}/admin/api/login',data=json.dumps({{'api_key':k,'remember':False}}).encode(),headers={{'Content-Type':'application/json'}},method='POST'),timeout=10);"
                f"print(re.search(r'{SESSION_COOKIE}=([^;]+)',' '.join(r.headers.get_all('Set-Cookie') or [])).group(1))")
        r = ssh_to(node, f'{ROOT}/.omlx-venv/bin/python -I -c {shlex.quote(code)}', timeout=20)
        value = r.stdout.strip()
        if r.returncode or not value: raise RuntimeError(tr('{0} 的 oMLX 没有发登录会话。', where(node)))
    print(json.dumps({'ok': True, 'url': f"http://127.0.0.1:{NODES[node]['port']}/admin", 'name': SESSION_COOKIE, 'value': value}))


# ---------- 新机器的准备清单：只读；要改的由用户本人在系统密码框里确认 ----------

def address_methods():
    """这台各个网络服务现在的地址是手动设的还是自动获取的：[{service, ip, method}]，method 是 manual / dhcp / other。读不到返回 []。"""
    def run(*args):
        try: return subprocess.run(['/usr/sbin/networksetup', *args], capture_output=True, text=True, errors='replace', timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired): return ''
    rows = []
    for service in run('-listallnetworkservices').splitlines()[1:]:
        if not service.strip() or service.startswith('*'): continue   # 带星号的是关掉的
        info = run('-getinfo', service)
        ip = re.search(r'^IP address: (\S+)', info, re.M)
        if not ip or not re.fullmatch(ADDRESS_IP, ip[1]): continue
        first = info.splitlines()[0] if info else ''
        rows.append({'service': service.strip()[:60], 'ip': ip[1], 'method': 'manual' if first.startswith('Manual') else 'dhcp' if 'DHCP' in first else 'other'})
    return rows


def prepare_info(remote=False):
    """这台 Mac 的准备情况。remote：给主机的自检用（rpc prepare），多带上 oMLX 在不在、各个地址是手动还是自动，不带要用户照抄的命令。"""
    def run(*command):
        try: return subprocess.run(command, capture_output=True, text=True, errors='replace', timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired): return ''
    settings, vault = run('pmset', '-g'), run('fdesetup', 'status')
    restart, sleeping = re.search(r'^\s*autorestart\s+(\d)', settings, re.M), re.search(r'^\s*sleep\s+(\d+)', settings, re.M)
    info = {'remote_login': port_open(22), 'autorestart': bool(restart and restart[1] == '1'), 'no_sleep': bool(sleeping and sleeping[1] == '0'),
            'can_poweroff': can_poweroff(LOCAL), 'filevault': True if 'is On' in vault else False if 'is Off' in vault else None}   # 读不到是 None，不说成没开
    if remote: info.update(engine=ENGINE_HERE['kind'], up=health(LOCAL) == 'up', addresses=address_methods())
    return info


def machine_check(name, as_json=False):
    """配好后自检六项（第 4 期，草案 11.4）：看得到、能登录、oMLX 在、能关机、FileVault、地址固定。只读。
    每项 state：ok 通过、fix 要处理（hint 说怎么办）、note 只是提醒、unknown 查不了。只给配对加入（走受限钥匙）的机器。"""
    if name not in NODES or is_local(name): raise RuntimeError(tr('没有这台，或者它是本机：{0}。', name))
    if not is_rpc(name): raise RuntimeError(tr('{0} 走的是全权限 SSH，这项检查只给配对加入的机器。', where(name)))
    title, host, items = where(name), lmc_hosts().get(name, {}), []
    def item(key, text, state, hint=''): items.append({'id': key, 'title': text, 'state': state, 'hint': hint})
    ip, port = host.get('ip') or NODES[name].get('ip') or '', host.get('ssh_port', 22)
    try:
        with socket.create_connection((ip, port), timeout=3): seen = True
    except OSError: seen = False
    item('reach', tr('看得到 {0}', title), 'ok' if seen else 'fix', '' if seen else tr('连不上 {0}（{1}）：确认它开机、和这台在同一个网络、地址没写错；它的远程登录要开着。', title, ip))
    ping, why = None, ''
    if seen:
        try: ping = rpc_query(name, 'ping', timeout=20)
        except (RuntimeError, ValueError, OSError) as exc: ping, why = None, str(exc)   # 被拒、指纹变了、超时，rpc_failure 各有一句
    if not isinstance(ping, dict): item('login', tr('能登录'), 'fix' if seen else 'unknown', (why + ' ' if why else '') + tr('重新配对一次就好（会重新确认指纹、换上新的授权）。') if seen else '')
    elif ping.get('proto') != RPC_PROTO: item('login', tr('能登录'), 'fix', tr('{0} 的控制台程序版本不同，先更新它（再配对一次就会换上这台的版本）。', title))
    else: item('login', tr('能登录'), 'ok', '' if ping.get('code') == code_hash() else tr('{0} 的控制台程序和这台不是同一版：能用，建议再配对一次换上新的。', title))
    info = None
    if items[-1]['state'] == 'ok':
        try: info = rpc_query(name, 'prepare', timeout=30)
        except (RuntimeError, ValueError, OSError): info = None
    old = ''
    if items[-1]['state'] == 'ok' and not isinstance(info, dict):   # 版本一样却没查到，是这一次没问到；版本不同才是对方太旧、不认这条
        old = tr('这次没问到（超时或出错），过一会儿再点一次检查。') if ping.get('code') == code_hash() else tr('{0} 的控制台程序是旧版，查不了这一项：再配对一次就会换上新的。', title)
    info = info if isinstance(info, dict) else {}
    if not info: item('engine', tr('oMLX 在'), 'unknown', old)
    elif info.get('engine') == 'none': item('engine', tr('oMLX 在'), 'fix', tr('{0} 还没装 oMLX：到那台上打开控制台，卡片上有"安装 oMLX…"；不装控制台的话，从 oMLX 官方下载安装。', title) + tr('装好后如果改过 oMLX 的端口（默认 8000），再配对一次。'))
    elif not info.get('up'): item('engine', tr('oMLX 在'), 'note', tr('{0} 装了 oMLX，现在没在运行：在它的卡片上点启动。', title))
    else: item('engine', tr('oMLX 在'), 'ok')
    if not info: item('poweroff', tr('能关机'), 'unknown', old)
    elif info.get('can_poweroff'): item('poweroff', tr('能关机'), 'ok')
    else: item('poweroff', tr('能关机'), 'fix', tr('{0} 还没允许控制台关它：到那台的控制台 机器 → 本机授权 点"一次设好"（要输一次管理员密码）。不设也能用，只是卡片上的关机用不了。', title))
    if not info or info.get('filevault') is None: item('filevault', tr('FileVault'), 'unknown', old)
    elif info.get('filevault'): item('filevault', tr('FileVault 开着'), 'ok', tr('断电重启后要先解锁：在这台点它卡片上的"解锁…"（要 macOS 26），或到 {0} 前输密码。', title))
    else: item('filevault', tr('FileVault 没开'), 'note', tr('硬盘没加密，断电重启后不用解锁。想更安全，在 {0} 的 系统设置 → 隐私与安全性 里打开 FileVault。', title))
    rows = info.get('addresses') if isinstance(info.get('addresses'), list) else None
    mine = next((a for a in rows or [] if isinstance(a, dict) and a.get('ip') == ip), None)
    if not ip: item('address', tr('地址固定'), 'unknown')
    elif not re.fullmatch(ADDRESS_IP, ip): item('address', tr('地址固定'), 'ok', tr('登记的是名字（{0}），地址变了也找得到。', ip))
    elif rows is None: item('address', tr('地址固定'), 'unknown', old)
    elif mine and mine.get('method') == 'manual': item('address', tr('地址固定'), 'ok')
    elif mine: item('address', tr('地址固定'), 'note', tr('{0} 是自动获取的，可能会变：在路由器里把它固定给 {1}（地址保留），或在 {2} 的系统设置里改成手动；已经在路由器里固定过就不用管。', ip, title, title))
    else: item('address', tr('地址固定'), 'unknown', tr('{0} 现在不在 {1} 报的地址里，可能已经变了。', ip, title))
    out = {'ok': True, 'name': name, 'title': title, 'items': items, 'fix': sum(i['state'] == 'fix' for i in items)}
    if as_json: return print(json.dumps(out, ensure_ascii=False))
    for i in items:
        mark = {'ok': ok, 'fix': bad, 'note': warn, 'unknown': off}[i['state']]
        print(mark(i['title']) + (f"\n    {i['hint']}" if i['hint'] else ''))


def prepare_state():
    settings = subprocess.run(['pmset', '-g'], capture_output=True, text=True, errors='replace', timeout=10).stdout
    restart, sleeping = re.search(r'^\s*autorestart\s+(\d)', settings, re.M), re.search(r'^\s*sleep\s+(\d+)', settings, re.M)
    vault = subprocess.run(['fdesetup', 'status'], capture_output=True, text=True, errors='replace', timeout=10).stdout
    user = subprocess.run(['id', '-un'], capture_output=True, text=True, errors='replace', timeout=5).stdout.strip()
    command = (f"/usr/bin/pmset -a autorestart 1 sleep 0 && /bin/echo '{user} ALL=(root) NOPASSWD: {POWEROFF}' > {SUDOERS} "
               f"&& /bin/chmod 440 {SUDOERS} && /usr/sbin/visudo -cf {SUDOERS}")
    fingerprint = host_fingerprint()   # 远程登录从没开过的 Mac 还没有主机钥匙，这时不给这一项
    print(json.dumps({'remote_login': port_open(22), 'autorestart': bool(restart and restart[1] == '1'), 'no_sleep': bool(sleeping and sleeping[1] == '0'),
                      'can_poweroff': can_poweroff(LOCAL), 'filevault': 'is On' in vault, 'command': command,
                      **({'host_key': fingerprint} if fingerprint else {})}, ensure_ascii=False))


def host_fingerprint(path=None):
    """这台的 SSH 主机钥匙指纹，和别的 Mac 第一次 ssh 过来时终端里显示的 SHA256:… 是同一个，配对时两边对照（0.5.1 A8）。
    只读公钥文件（谁都能读），不要 sudo；读不到或不是一行公钥就返回 None，不显示。"""
    try: parts = Path(path or HOST_KEY_FILE).read_text().split()
    except OSError: return None
    if len(parts) < 2 or not parts[0].startswith('ssh-'): return None
    try: blob = base64.b64decode(parts[1], validate=True)
    except ValueError: return None   # binascii.Error 是 ValueError 的子类
    return 'SHA256:' + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')


def computer_name():
    try: return subprocess.run(['scutil', '--get', 'ComputerName'], capture_output=True, text=True, errors='replace', timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired): return ''


def ssh_port_open(node):
    """连不上时分清两种：22 端口通，多半开着在等 FileVault 解锁；不通，多半关机或断网。登记了几个地址的，哪个通都算。"""
    if multi(node): return pick_address(node) is not None
    host = lmc_hosts().get(node, {})
    ip = NODES[node].get('ip') or host.get('ip')
    if not ip: return False
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex((ip, host.get('ssh_port', 22))) == 0


def unlock(node):
    """打开一个终端窗口，连到开着、等解锁的机器（macOS 26 的 SSH 解锁）。密码由用户直接输给苹果，
    这里只写连接命令，不经手密码。等解锁时的主机指纹可能和平时不同，所以单独记一份。"""
    if node not in NODES or is_local(node): raise RuntimeError(tr('这台不用解锁。'))
    meta = NODES[node]
    shown = subprocess.run(['ssh', *(['-F', str(SSH_CONFIG)] if is_rpc(node) else []), '-G', meta['ssh']],
                           capture_output=True, text=True, errors='replace', timeout=5).stdout
    fields = dict(line.split(' ', 1) for line in shown.splitlines() if ' ' in line)
    host, user, port = (pick_address(node, fresh=True) if multi(node) else None) or meta.get('ip') or fields.get('hostname'), fields.get('user'), fields.get('port', '22')
    if not host or not user: raise RuntimeError(tr('读不到 {0} 的地址和账号。', where(node)))
    try:  # 指纹记在这里；没开主机模式时还没有这个文件夹（B3）。建不了也照样解锁，只是指纹记不住
        for folder in (LMC_SSH.parent, LMC_SSH): folder.mkdir(mode=0o700, exist_ok=True)
    except OSError: pass
    command = ['ssh', '-F', 'none', '-o', 'PubkeyAuthentication=no', '-o', 'PreferredAuthentications=keyboard-interactive,password',
               '-o', 'StrictHostKeyChecking=accept-new', '-o', f"UserKnownHostsFile={LMC_SSH / 'preboot_known_hosts'}",
               '-p', port, f'{user}@{host}']
    script = RUN_DIR / f'unlock-{node}.command'
    script.write_text(f"#!/bin/zsh\necho {shlex.quote(tr('{0} 开机解锁：下面输入 {1} 的 macOS 密码，密码直接交给系统。看到 System successfully unlocked 就可以关掉这个窗口。', where(node), where(node)))}\n{shlex.join(command)}\n")
    script.chmod(0o700)
    subprocess.run(['open', str(script)], check=True, timeout=10)
    set_wants([node], True)   # 点了解锁就是要用：回来后自动启动（D1，原来只记在应用的内存里）
    print(ok(tr('已打开终端，到那里输入 {0} 的 macOS 密码。解锁后这边会自动启动它的 oMLX，不会自动加载模型。', where(node))))


def window_holder():
    """运行窗口被谁占着：有人在维护时，打开控制台不要自动启动服务。没有这个脚本（公开版）算没人占；有脚本却查不出来，按有人占算。"""
    script = PROJECT.parent / '02-研发管理/运行窗口.py'
    if not script.exists(): return None
    try: r = subprocess.run([sys.executable, str(script), 'status'], capture_output=True, text=True, errors='replace', timeout=30)
    except subprocess.TimeoutExpired: return tr('查不出这台是不是正在维护（检查超时）')
    lines = r.stdout.splitlines()
    if r.returncode not in (0, 2, 3): return tr('查不出这台是不是正在维护（检查出错）')   # 0 空闲、2 有人占、3 有活动但没人占；别的是脚本自己出了错
    return (lines[0] if lines else tr('有人')) if r.returncode == 2 else None   # 退出码 2：有人占着


def autostart(target=None):
    """打开控制台就启动 oMLX（用户 11-03 定）：连得上、服务没开的都启动，不加载模型。
    指定了一台是 D1：那台刚回来（应用看到它从连不上变成开着、服务没运行），记的是"要开"才启动。"""
    holder = window_holder()
    if holder:
        print(warn(tr('这台正在维护（{0}），这次没有自动启动 oMLX。', holder)))
        return
    if target:
        if target not in remotes(): raise RuntimeError(tr('autostart 只认副机：{0}', ' | '.join(remotes())))
        if wants_up().get(target) is not True: return print(off(tr('{0} 关机前 oMLX 没在跑，没有自动启动。', where(target))))
        if not reachable_now(target): raise RuntimeError(offline(target))
        if health(target) in ('up', 'starting'): return print(off(tr('{0} 的 oMLX 已经在运行。', where(target))))
        serve_node(target, quiet=True)
        console_remote(target, 'open')
        print(ok(tr('{0} 回来了，已自动启动 oMLX（没有加载模型）。', where(target))))
        return
    for node in NODES:
        if not is_local(node) and not is_rpc(node) and not remote_stats(node)['online']:
            print(off(tr('{0} 连不上，先跳过。', where(node))))
            continue
        try:
            if health(node) not in ('up', 'starting'): serve_node(node)
            elif official(node): report_gateway(ensure_gateway())
            console_remote(node, 'open')
        except Exception as exc: print(warn(tr('{0} 没启动成：{1}', where(node), exc)))


from brand import SERVICE_LABEL  # noqa: E402


def service_plist():
    log = RUN_DIR / 'device-service.log'
    python, entry = here_python(), Path(__file__).with_name('device_service.py')
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{SERVICE_LABEL}</string>
  <key>ProgramArguments</key><array><string>{python}</string><string>-B</string><string>{entry}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""


def service_command(rest):
    """常驻项由用户本人决定：这里只生成文件、打印命令，不替用户加载（角色卡：常驻项要用户同意）。"""
    path = Path.home() / f'Library/LaunchAgents/{SERVICE_LABEL}.plist'
    what = rest[0] if rest else 'show'
    if what == 'show':
        print(service_plist())
    elif what == 'write':
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(service_plist())
        print(ok(tr('已写 {0}。要启用，运行：launchctl bootstrap gui/$(id -u) {1}', path, path)))
    else:
        raise RuntimeError('service show | write')


def known_host_key(node, ip, port):
    """这台的主机指纹：本机读系统的，别的机器读配对时记下的。进签名的机器表，备用机接管后也认得各台。"""
    try:
        if is_local(node): text = HOST_KEY_FILE.read_text()
        else:
            host = ip if port == 22 else f'[{ip}]:{port}'
            text = next((line for line in (LMC_SSH / 'known_hosts').read_text().splitlines() if line.split(' ', 1)[0] == host), '')
    except OSError: return None
    found = re.search(r'ssh-ed25519 [A-Za-z0-9+/=]{20,200}', text)
    return found[0] if found else None


def hub_machines():
    """机器表和 hosts.json 合起来：主机移交时新主机要知道怎么连各台。"""
    hosts, rows = lmc_hosts(), {}
    for name, meta in NODES.items():
        h = hosts.get(name, {})
        rows[name] = {'title': meta['title'], 'ip': meta.get('ip') or h.get('ip', '127.0.0.1'), 'user': h.get('user') or subprocess.run(['id', '-un'], capture_output=True, text=True, errors='replace').stdout.strip(),
                      'ssh_port': h.get('ssh_port', 22), **{k: meta[k] for k in ('interface', 'link', 'dsh_provider') if meta.get(k)}}
        host_key = known_host_key(name, rows[name]['ip'], rows[name]['ssh_port'])
        if host_key: rows[name]['host_key'] = host_key
    return rows


def node_chip(node, what, data=None, timeout=15):
    """问另一台的安全芯片登记。走受限钥匙的用 rpc；老路要对方装了 console/ 才有这个命令，没装返回 None。"""
    if is_rpc(node): return rpc_query(node, f'chip-{what}', timeout=timeout, data=data)
    script = ROOT / 'console/scripts/clusterctl.py'
    command = 'receive' if what == 'apply' else what   # 经网络来的第一份不生效，和 rpc 一样
    r = ssh_to(node, f'[ -f "{script}" ] && "{ROOT}/.venv/bin/python" -B "{script}" chip {command}', timeout=timeout, data=data)
    if r.returncode: return None
    return json.loads(r.stdout.strip().splitlines()[-1])


def chip_align(node, theirs=None):
    """和一台对齐登记：谁新听谁的，缺的按顺序补。返回 True 对齐了｜False 连不上（最高操作到了对方自己会拒绝）｜'pending' 对方第一次收到，
    要人到那台核对指纹后接受。两边接不上才报错。"""
    import chip
    try: theirs = theirs or node_chip(node, 'state')
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired): return False
    if not isinstance(theirs, dict) or type(theirs.get('seq')) is not int: return False
    mine = chip.seq()
    if theirs['seq'] < mine:
        try: reply = node_chip(node, 'apply', data=json.dumps({'updates': chip.log()}, ensure_ascii=False))
        except (Unreachable, subprocess.TimeoutExpired): return False
        if reply is None: return False
        return 'pending' if reply.get('pending') else True
    if theirs['seq'] > mine: chip.receive(node_chip(node, 'log')['updates'])
    elif mine and theirs.get('top') != chip.state_view()['top']:
        raise RuntimeError(tr('{0} 记的登记和这台对不上：到 {1} 的 设置 → 安全 重新配对。', where(node), where(node)))
    return True


def chip_push_all():
    """主机把登记发给各台，返回没发到的。"""
    missed = []
    for node in remotes():
        try:
            result = chip_align(node)
            if result == 'pending': missed.append(tr('{0}（第一次要到那台的 设置 → 安全 核对指纹后接受）', where(node)))
            elif not result: missed.append(where(node))
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            missed.append(tr('{0}（{1}）', where(node), exc))
    return missed


def chip_view():
    """主机还要问一圈各台有没有人申请登记。只在打开设置页时查，不进 10 秒一次的刷新。"""
    import chip
    here = is_hub_here()
    pending, waiting = [], []
    if here:
        for node in remotes():
            try:
                theirs = node_chip(node, 'state', timeout=6)
                if isinstance(theirs, dict):
                    # 别的机器写的申请不信：按公钥重算，写法不对的不要
                    pending += [{**row, 'where': where(node)} for row in chip.clean_requests(theirs.get('requests'))]
                    if chip_align(node, theirs) == 'pending' or theirs.get('pending'): waiting.append(where(node))
            except (RuntimeError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired): continue
    view = chip.view()
    known = {k['id'] for k in view['keys']}
    view['requests'] = [{**row, 'where': where(LOCAL)} for row in view['requests']] + [row for row in pending if row['id'] not in known]
    view['is_hub'] = here
    view['waiting'] = waiting
    return view


def chip_payload(rest):
    """这次要签的内容。只算不存。"""
    import chip, hub
    if rest[:1] == ['poweroff'] and len(rest) == 2: return chip.op_text('poweroff', pick_nodes(rest[1]))
    if rest[:2] == ['state', 'init']: return hub.init_text(LOCAL, hub_machines(), rest[2:], secrets.token_hex(8))
    if rest[:1] == ['state'] and len(rest) >= 2: return hub.next_state_text(LOCAL, rest[1], rest[2:])
    raise RuntimeError(tr('chip payload poweroff <机器|all> | state handover <机器> | state takeover | state candidates <机器…>'))


def chip_action(what, rest, data):
    """chip 子命令，本机的 CLI 和 rpc 共用。一律回一行 JSON。"""
    import chip
    if what == 'view': return chip_view()
    if what == 'state': return chip.state_view()
    if what == 'log': return {'updates': chip.log()}
    if what == 'payload': return {'text': chip_payload(rest)}
    form = json.loads(data or '{}')
    if not isinstance(form, dict): raise RuntimeError(tr('输入不对。'))
    if what == 'body':
        return {'text': chip.next_body(rest[0] if rest else '', form)}
    if what in ('apply', 'receive'):
        # apply 是人在这台点的，第一份也算数。receive 是经网络来的，第一份只放着等人接受
        number = (chip.apply if what == 'apply' else chip.receive)(form.get('updates'))
        here = is_hub_here()
        return {'seq': number, 'pending': number == 0, 'missed': (chip_push_all() if number else []) if here else None}
    if what == 'request':
        chip.add_request(form.get('title'), form.get('pub'))
        return {'ok': True}
    raise RuntimeError(tr('chip view | state | log | payload … | body … | apply | receive | request | accept | forget <凭证>'))


def chip_command(rest):
    import chip, hub
    what = rest[0] if rest else 'view'
    try:
        if what == 'forget':
            # 到机器前重新配对：过系统的管理员密码框，和忘记最高密码同一个门
            if len(rest) != 2: raise RuntimeError(tr('chip forget <凭证>'))
            hub.check_reset_proof(rest[1])
            chip.forget()
            hub.note('清掉了这台的安全芯片登记（重新配对）')
            print(json.dumps({'ok': True}))
            return
        if what == 'accept':
            # 人在这台机器前核对过指纹。rpc 里没有这一条，远程调不到
            if len(rest) != 2: raise RuntimeError(tr('chip accept <核对过的那一份的摘要>'))
            print(json.dumps({'seq': chip.accept(rest[1])}))
            hub.after_accept()
            hub.note('接受了别的机器发来的安全芯片登记')
            return
        data = sys.stdin.read() if what in ('body', 'apply', 'receive', 'request') else None
        print(json.dumps(chip_action(what, rest[1:], data), ensure_ascii=False))
    except hub.HubError as exc:
        raise RuntimeError(str(exc))
    except ValueError:
        raise RuntimeError(tr('输入不是 JSON。'))


def hub_command(rest, args):
    import hub
    what = rest[0] if rest else 'view'
    try:
        if what == 'view': print(json.dumps(hub.view(LOCAL), ensure_ascii=False))
        elif what == 'state': print(json.dumps(hub.rpc_state(), ensure_ascii=False))
        elif what == 'init':
            candidates = rest[1:]
            password = hub.read_password(tr('设最高密码：'))
            data = hub.init(LOCAL, hub_machines(), candidates, password, proof=hub.read_proof())
            print(ok(tr('已启用主机模式，这台是主机。到每台亲手加一行授权（hub line），再运行 hub join。可当主机：{0}', tr('、').join(data['candidates']))))
        elif what == 'line':
            print(grant_line('hub', 'hub', hub.public_key(), 'hub'))
        elif what == 'join':
            missed = hub.join_all(LOCAL)
            print(ok(tr('各台都通知到了。')) if not missed else warn(tr('这些机器没通知到：{0}。确认已加好授权行、开机，再运行一次。', tr('、').join(missed))))
        elif what == 'password':
            if rest[1:] == ['set']:
                hub.setup_password(LOCAL, hub.read_password(tr('设最高密码：')))
                print(ok(tr('最高密码已设好。')))
                return
            if rest[1:] == ['prepare']:
                old = hub.read_password(tr('现在的最高密码：'))
                print(json.dumps({'text': hub.prepare_password(LOCAL, old, hub.read_password(tr('新的最高密码：')))}, ensure_ascii=False))
                return
            if rest[1:2] == ['prepare-reset'] and len(rest) == 3:
                hub.check_reset_proof(rest[2])
                print(json.dumps({'text': hub.prepare_reset(LOCAL, hub.read_password(tr('新的最高密码：')))}, ensure_ascii=False))
                return
            if rest[1:] == ['commit']:
                missed = hub.commit_password(LOCAL, hub.read_proof())
                print(ok(tr('最高密码已改。')) if not missed else warn(tr('这台改好了，这些机器没连上，还留着旧密码加密的副本：{0}。它们连上后主机会补发，补发前旧密码在那边还能用。', tr('、').join(missed))))
                return
            if rest[1:2] == ['reset']:
                # 应用先过系统的管理员密码框，留下只有 root 能写的凭证；终端里用 sudo 验证
                if len(rest) == 3: hub.check_reset_proof(rest[2])
                elif not sys.stdin.isatty() or subprocess.run(['sudo', '-k', '-v']).returncode != 0: raise RuntimeError(tr('没有通过 macOS 管理员验证。'))
                missed = hub.reset_password(LOCAL, hub.read_password(tr('新的最高密码：')))
            else:
                old = hub.read_password(tr('现在的最高密码：'))
                missed = hub.set_password(LOCAL, old, hub.read_password(tr('新的最高密码：')))
            print(ok(tr('最高密码已改。')) if not missed else warn(tr('这台改好了，这些机器没连上，还留着旧密码加密的副本：{0}。它们连上后主机会补发，补发前旧密码在那边还能用。', tr('、').join(missed))))
        elif what == 'candidates':
            password = hub.read_password()
            missed = hub.set_candidates(LOCAL, rest[1:], password, hub.read_proof())
            print(ok(tr('名单已改。')) if not missed else warn(tr('名单已改，这些机器没通知到：{0}', tr('、').join(missed))))
        elif what in ('handover', 'approve') and len(rest) == 2:
            password = hub.read_password()
            missed = hub.handover(LOCAL, rest[1], password, proof=hub.read_proof())
            print(ok(tr('主机已移交给 {0}，这台现在是副机。', where(rest[1]))) + (warn(tr('这些机器没通知到：{0}，主机之后会补。', tr('、').join(missed))) if missed else ''))
        elif what == 'apply':
            hub.apply(LOCAL)
            print(ok(tr('已申请，等主机批准。10 分钟没人批就失效。')))
        elif what == 'reject' and len(rest) == 2:
            hub.reject(LOCAL, rest[1])
            print(ok(tr('已拒绝 {0} 的申请。', where(rest[1]))))
        elif what == 'takeover':
            password = hub.read_password()
            missed = hub.takeover(LOCAL, password, force=args.force, proof=hub.read_proof())
            print(ok(tr('这台现在是主机。')) + (warn(tr('这些机器没通知到：{0}，它们开机后会补。', tr('、').join(missed))) if missed else ''))
        elif what == 'sync':
            print(json.dumps(hub.sync(LOCAL), ensure_ascii=False))
        elif what == 'resign':
            password = hub.read_password()
            missed = hub.resign(LOCAL, password, hub.read_proof())
            print(ok(tr('重新签好了。')) if not missed else warn(tr('这台签好了，这些机器没通知到：{0}。它们要先接受登记，或者连上后主机会补。', tr('、').join(missed))))
        elif what == 'leave' and len(rest) == 2:
            # 到机器前退出主机模式：过系统的管理员密码框，和忘记最高密码同一个门。rpc 里没有这一条
            hub.check_reset_proof(rest[1])
            hub.leave()
            disconnect_sessions()   # sshd 只在握手时查钥匙：旧钥匙已开的隧道也要断
            print(ok(tr('这台退出了主机模式。模型和服务不受影响；要重新加入，到主机上重新贴授权行、再通知各台。')))
        else:
            raise RuntimeError(tr('hub view | init <可当主机的机器…> | line | join | password [reset] | candidates <机器…> | handover <机器> | apply | approve <机器> | reject <机器> | takeover [--force] | sync'))
    except hub.HubError as exc:
        raise RuntimeError(str(exc))


def shutdown(target='all', assume_yes=False):
    """一键关闭：先卸载模型并确认，再只停 8800 上监听的那个 oMLX 进程。"""
    nodes = [n for n in pick_nodes(target) if is_local(n) or remote_stats(n)['online']]
    running = [n for n in nodes if listener(n)[0]]
    if not running:
        print(off(tr('要关的服务本来就没有运行。')))
        return
    names = tr('、').join(where(n) for n in running)
    if not (assume_yes or ask(tr('将先卸载 {0} 上的模型，再停止它们的 oMLX 服务。确定吗？[y/N] ', names))):
        print(tr('已取消。'))
        return
    for node in running:
        stop_node(node)
        set_wants([node], False)
        print(ok(tr('{0} 的 oMLX 已停止。模型文件和设置都保留。', where(node))))


def restart(target='all', assume_yes=False):
    """重启：先卸载模型、停服务，再启动。重启后不自动加载模型（和"手动开关"一致）。"""
    if not (assume_yes or ask(tr('将先卸载模型、停止 oMLX，再重新启动；重启后不会自动加载模型。确定吗？[y/N] '))):
        print(tr('已取消。'))
        return
    shutdown(target, assume_yes=True)
    serve(target)


# ---------- 模型 ----------

def quote_id(model_id): return urllib.parse.quote(model_id, safe='')   # 拼进网址的模型 id：你的三个模型转出来不变


def native_model(name):
    """自动识别的模型住在 oMLX 自己的模型文件夹里（不是我们从 models/ 链进去的）：不用链、不撤链（2b）。"""
    return bool(MODELS[name].get('auto'))


def model_present(node, name):
    if native_model(name): return True   # oMLX 列着它才认得出来；文件后来没了，加载时 oMLX 会如实报错
    config = ROOT / 'models' / MODELS[name]['id'] / 'config.json'
    if is_local(node): return config.is_file()
    return ssh_sure(node, f'test -f "{config}"').returncode == 0


def link_model(node, name):
    if native_model(name) or official(node): return
    target, link = ROOT / 'models' / MODELS[name]['id'], SERVING / MODELS[name]['id']
    if is_local(node):
        SERVING.mkdir(exist_ok=True)
        if not link.is_symlink(): link.symlink_to(target, target_is_directory=True)
        return
    r = ssh_to(node, f'mkdir -p "{SERVING}" && {{ [ -L "{link}" ] || ln -s "{target}" "{link}"; }}')
    if r.returncode: raise RuntimeError(tr('没能在 {0} 上登记 {1}：{2}', where(node), MODELS[name]["title"], r.stderr.strip()[:200]))


def unlink_model(node, name):
    """撤掉链接（只删链接，不碰模型目录）；返回原来是否链着。住在 oMLX 自己文件夹里的不是我们链的，不碰。"""
    if native_model(name) or official(node): return False
    link = SERVING / MODELS[name]['id']
    if is_local(node):
        if not link.is_symlink(): return False
        link.unlink()
        return True
    r = ssh_sure(node, f'[ -L "{link}" ] && rm "{link}" && echo removed || true')
    return 'removed' in r.stdout


def rescan_keeping(node, must=True):
    """这台还有模型在内存里时让 oMLX 重新看 serving-models。它自带的重新扫描（/admin/api/reload）会先卸载全部模型，
    不能用；用 serve_omlx.py 加的 /lmc/rescan，只扫描、不卸载。旧版启动脚本没有这个接口：must 时报错（什么都没动），
    否则放过，和以前一样，撤了链接的模型暂时还在 oMLX 的列表里。官方版没有这个接口，也用不着（模型都在它自己的目录里）。"""
    if official(node): return
    try: api(node, '/lmc/rescan', {}, timeout=60, retry=True)
    except RuntimeError as exc:
        if 'HTTP 404' not in str(exc): raise
        if must: raise RuntimeError(tr('{0} 的 oMLX 还在用旧的启动脚本，不能在别的模型在线时再加载一个。重启这台的 oMLX 后再试。', where(node))) from exc


def wait_state(node, name, wanted, seconds=180):
    for _ in range(seconds):
        if node_models(node)[name] == wanted: return
        time.sleep(1)
    raise RuntimeError(tr('{0} 上的 {1} 在 {2} 秒内没有变成预期状态，请在管理页确认。', where(node), MODELS[name]['title'], seconds))


def unload(name, node, quiet=False):
    no_active_validation()
    if is_rpc(node):
        rpc_action(node, 'unload', name, *(['--quiet'] if quiet else []))
        return
    reach(node)
    if health(node) != 'up':
        if not quiet: print(off(tr('{0} 的 oMLX 没有运行，没有需要卸载的模型。', where(node))))
        return
    model, models = MODELS[name], models_sure(node)
    if is_local(node): hold_lock(tr('卸载 {0}', model['title']))
    if models[name] == 'loading': raise RuntimeError(tr('{0} 上的 {1} 正在加载，等加载完再卸载。', where(node), model['title']))
    if models[name] == 'loaded':
        try: api(node, f"/v1/models/{quote_id(model['id'])}/unload", {}, timeout=300, retry=True)
        except RuntimeError:
            # 第一次其实卸掉了、只是回复被重置，重试时 oMLX 回 400"没有加载"：已经是想要的结果
            if node_models(node)[name] != 'off': raise
        wait_state(node, name, 'off')
    # 撤掉链接后要重新扫描才会从列表消失；重新扫描会卸载这台的全部模型，所以只在这台已经空了时做
    if unlink_model(node, name):
        if active(node_models(node)): rescan_keeping(node, must=False)
        else: api(node, '/admin/api/reload', {}, timeout=300, retry=True)
    if not quiet and models[name] == 'loaded': print(ok(tr('{0} 上的 {1} 已卸载，服务保持运行。', where(node), model['title'])))


def requests_now(node):
    """这台此刻在算、排队的请求数（控制台卸载、切换前的确认框用）；读不到返回 None。
    oMLX 的 active_requests 从请求进来就算，已含排队的，减掉才是在算的。"""
    try:
        if is_rpc(node): return rpc_query(node, 'requests', timeout=8)
        s = api(node, '/api/status', timeout=3)
    except Exception: return None
    waiting = s.get('waiting_requests') or 0
    return {'active': max((s.get('active_requests') or 0) - waiting, 0), 'waiting': waiting}


def wait_idle(node, minutes=10):
    """等这台的请求（在算 + 排队）都答完再往下走（控制台"等它答完再卸载"）。等的时候控制台可以停止；
    一旦开始卸载就不理会停止信号，免得停在一半：模型卸了、链接还在，下一个请求又会把它自动加载回来。
    读不到请求数不算答完，接着等；服务没在运行就没有请求可等。"""
    deadline, last = time.monotonic() + minutes * 60, -1
    if not is_local(node):
        try: reach(node)   # 隧道断着时会被当成"服务没在运行"，不等就卸载（体检 11-15）
        except RuntimeError: pass
    while True:
        RPC_STATE.pop(node, None)  # 要现查
        counts = requests_now(node)
        busy = None if counts is None else sum(counts.values())
        if busy == 0 or (busy is None and health(node) is None): break
        if busy != last:
            print(tr('{0} 读不到请求数，先不卸载，接着等（最长 {1} 分钟）…', where(node), minutes) if busy is None
                  else tr('{0} 还有 {1} 个请求没答完，等它答完再卸载（最长 {2} 分钟）…', where(node), busy, minutes))
        if time.monotonic() > deadline:
            raise RuntimeError(tr('{0} 等了 {1} 分钟{2}，没有卸载。', where(node), minutes, (tr('还读不到请求数') if busy is None else tr('还有请求没答完'))))
        last = busy
        time.sleep(2)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if not sys.stdout.isatty(): print(UNLOADING, flush=True)   # 给应用（经管道读）：从这里起不能停止；终端里不打
    print(tr('{0} 现在没有请求在处理，开始卸载。', where(node)))


def unload_all(target='all', quiet=False, wait=False):
    done = []
    for node in pick_nodes(target):
        if not is_local(node) and not remote_stats(node)['online']: continue
        reach(node)
        if health(node) != 'up': continue
        with models_of(node):   # 按这台自己的清单卸（2b）
            loaded = active(node_models(node))
            if wait and loaded: wait_idle(node)
            if is_rpc(node):
                if loaded: rpc_action(node, 'unload', 'all', '--quiet')
            else:
                for name in MODELS: unload(name, node, quiet=True)
            done += [tr('{0} 的 {1}', where(node), MODELS[n]['title']) for n in loaded]
    if quiet: return
    print(ok(tr('已卸载 ') + tr('、').join(done) + tr('，服务保持运行。')) if done else off(tr('没有加载着的模型。')))


def canary(node, model_id):
    body = {'model': model_id, 'messages': [{'role': 'user', 'content': '计算 17 × 23，只输出十进制整数答案。'}],
            'temperature': 0, 'max_tokens': 16, 'chat_template_kwargs': {'enable_thinking': False}}
    started = time.monotonic()
    reply = api(node, '/v1/chat/completions', body, timeout=600)
    text = (reply['choices'][0]['message'].get('content') or '').strip()
    return '391' in text, text[:40], time.monotonic() - started


def selftest(name, node):
    """让已加载的模型算一道 17×23，确认它能正常回答（加载时也自动做这一步）。"""
    if is_rpc(node):
        rpc_action(node, 'selftest', name)
        return
    model = MODELS[name]
    reach(node)
    if node_models(node)[name] != 'loaded':
        raise RuntimeError(tr('{0} 上没有加载 {1}，加载后才能自检。', where(node), model['title']))
    passed, text, seconds = canary(node, model['id'])
    if not passed:
        raise RuntimeError(tr('{0} 上的 {1} 自检没通过：17×23 得到 {2}。请先不要使用，查看日志找原因。', where(node), model['title'], f'{text!r}'))
    print(ok(tr('{0} 上的 {1} 自检通过：17×23={2}（{3} 秒）', where(node), model['title'], text, f'{seconds:.1f}')))


def free_ollama(allowed, names):
    """allowed：None 当场询问（终端菜单）；True 已确认；False 不释放，报错说明怎么确认（命令行和应用）。"""
    if allowed is False:
        raise RuntimeError(tr('Ollama 正占用 {0} 的内存：{1}，加上要加载的模型会超过上限。确认让 Ollama 先释放后再加载（命令行加 --free-ollama）。', where(LOCAL), names))
    if not (allowed or ask(tr('Ollama 正占用 {0} 的内存：{1}，加上要加载的模型会超过上限。要先让它释放吗？[y/N] ', where(LOCAL), names))):
        raise RuntimeError(tr('已取消：内存不够时硬加载，会让系统开始用 swap 变慢。'))
    unload_ollama()


def release_ollama(assume_yes=False):
    """内存超线时的手动补救：请 Ollama 卸载它当前加载的全部模型（菜单 o、应用里的"让 Ollama 释放"）。"""
    models = ollama_loaded()
    if models is None: raise RuntimeError(tr('Ollama 没有运行，没有可以释放的。'))
    if not models:
        print(off(tr('Ollama 没有加载模型，不用释放。')))
        return
    names = tr('、').join(tr('{0}（{1}）', m['name'], gb(m.get('size', 0))) for m in models)
    if not (assume_yes or ask(tr('让 Ollama 卸载 {0}？下次在 Ollama 或 dsh 里用到时它会自动重新加载。[y/N] ', names) if has_dsh() else tr('让 Ollama 卸载 {0}？下次在 Ollama 里用到时它会自动重新加载。[y/N] ', names))):
        print(tr('已取消。'))
        return
    unload_ollama()


def unload_ollama():
    for m in ollama_loaded() or []:
        post(OLLAMA + '/api/generate', {'model': m['name'], 'keep_alive': 0})
    for _ in range(60):
        if not ollama_loaded(): print(tr('Ollama 已释放模型。')); return
        time.sleep(1)
    raise RuntimeError(tr('Ollama 没有在 60 秒内释放模型，请在 Ollama 里手动停止。'))


def check_ollama(name, allowed, opts=None):
    """只在本机加载时检查：Ollama 只看本机，和本机的 oMLX 共用内存，按安全线算放不放得下
    （按这台现在存的开关估；切换开关时 opts 是要改成的那组，开关还没存）。"""
    models = ollama_loaded() or []
    extra = sum(m.get('size', 0) for m in models)
    if not extra: return
    line = (local_stats().get('total') or 0) * alert_percent() / 100   # 读不到总内存就当放不下，先问，不按 512 GB 猜
    if model_bytes(name, LOCAL, opts=opts) + extra > line:
        free_ollama(allowed, tr('、').join(tr('{0}（{1}）', m['name'], gb(m.get('size', 0))) for m in models))
    else:
        print(warn(tr('Ollama 正占着约 {0}，和 oMLX 共用 {1} 的内存；目前还放得下，继续加载。', gb(extra), where(LOCAL))))


def load_settings(name, context, opts, node=None):
    """加载前 PUT 给 oMLX 的设置。声明了 MTP 的模型显式写 mtp_enabled，防止有人在管理页改过以后和控制台显示的不一致；
    没声明的不写（陌生模型给它写 MTP 会怎样没测过）。其余按模型说明的 load 段。
    MTP、MTP 深度、放表方式都在 oMLX 的引擎签名里：只能在模型不在内存时改，见 set_option()。"""
    load = MODELS[name].get('load', {})
    body = {'max_context_window': context, **({'mtp_enabled': opts['mtp']} if 'mtp' in MODELS[name].get('switches', OPTIONS) else {})}
    found = official_switch(node, name, 'ssd') if node is not None and official(node) else None
    if found: body.update({found[1]: opts['ssd'] if found[0] == 'free' else True} if found[0] != 'unsupported' else {})   # 官方版：按它报的
    elif load.get('ssd_key'): body[load['ssd_key']] = opts['ssd']
    body.update(load.get('always', {}))
    if found and found[0] == 'unsupported': body.pop(found[1], None)   # 它说不支持的，出厂说明总写的也不写
    elif found and found[0] == 'free': body[found[1]] = opts['ssd']    # 官方版可开关的：照开关写，不让出厂说明"总写开"的那一项盖回去（A13）
    if opts['mtp']: body.update(load.get('when_mtp', {}))
    return body


def load_model(node, name, context, alongside=False):
    """把模型链进这台的 serving-models、重新扫描、设好上下文和开关后加载。alongside：这台还有别的模型在内存里，要留着。
    哪一步失败都撤掉链接再报错，见 drop_failed_link()。"""
    model_id, opts = MODELS[name]['id'], node_options(node, name)
    link_model(node, name)
    try:
        if official(node): pass   # 官方版：模型本来就在它的列表里，不用重新扫描（它的重新扫描会先卸载全部）
        elif alongside: rescan_keeping(node)
        else: api(node, '/admin/api/reload', {}, timeout=300, retry=True)  # 这台这时没有模型在内存里，重新扫描不会误伤
        api(node, f'/admin/api/models/{quote_id(model_id)}/settings', load_settings(name, context, opts, node), method='PUT', timeout=30, retry=True)
        ENGINE.pop(node, None)  # oMLX 存的设置变了，之后现读
        api(node, f'/v1/models/{quote_id(model_id)}/load', {}, timeout=1800)
        if node_models(node)[name] != 'loaded':
            raise RuntimeError(tr('oMLX 没有确认 {0} 上的 {1} 加载成功，请在管理页查看。', where(node), MODELS[name]["title"]))
    except Exception as exc:
        raise RuntimeError(f'{exc}\n{drop_failed_link(node, name)}') from exc
    conf = saved()
    conf.setdefault('applied', {}).setdefault(node, {})[name] = opts
    save_conf(conf)


def remember_model(node, name, seconds):
    """自动识别的模型加载成功后写进那台的②：第一次连同认出来的事实和日期（以后不用每次现认），每次都更新加载用时
    （"重新加载要下线多久"按它说，你在③里写了就用你的）。那台还没有②时，先把这台②里它也有的模型抄过去，免得清单少了。"""
    model, here = MODELS[name], where(node)
    path = (MODEL_DATA if is_local(node) else ROOT) / 'model-facts.json'
    try:
        text = (path.read_text() if path.exists() else '') if is_local(node) else remote_file(node, path)
        data = json.loads(text) if text.strip() else {'models': {key: fact for key, fact in HOST_FACTS.items() if isinstance(fact, dict) and fact.get('id') in set(node_data(node).get('have') or ())}}
        if not isinstance(data, dict) or not isinstance(data.setdefault('models', {}), dict): raise ValueError(tr('格式不对'))
        known = HOST_FACTS.get(name) if isinstance(HOST_FACTS.get(name), dict) else {}
        entry = data['models'].get(name) or {**(model.get('fact') or known), 'added': time.strftime('%Y-%m-%d')}
        if any(key not in entry for key in NEED_FACTS): return   # 没有完整的事实可写（体检 11-15）
        data['models'][name] = {**entry, 'load_seconds': round(seconds)}
        body = json.dumps(data, indent=2, ensure_ascii=False)
        if is_local(node): write_json(path, data)
        else:
            r = ssh_to(node, f'cat > "{path}.tmp" && mv "{path}.tmp" "{path}"', data=body)
            if r.returncode: raise OSError(r.stderr.strip()[:200])
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(warn(tr('没能把 {0} 记进 {1} 的模型事实（{2}），下次还会现认，不影响使用。', model['title'], here, exc)))


def drop_failed_link(node, name):
    """加载失败后撤掉链接，返回一句说明。PUT 已把这次的设置写进 oMLX，链接留着的话，客户端一点就会按这份设置
    把它自动加载回来，控制台却不知道（踩坑 43 同类）。和 unload() 一样，这台空了才重新扫描。"""
    here, title = where(node), MODELS[name]['title']
    if official(node): return tr('{0} 没加载上，{1} 上别的模型不受影响。', title, here)   # 没有软链要撤；没加载的模型挡在"入口"外（10.2）
    try: unlink_model(node, name)
    except Exception as exc: return tr('撤链接也没成功（{0}），客户端可能会把 {1} 自动加载回来，请在管理页确认。', exc, title)
    try:
        if active(node_models(node)):
            rescan_keeping(node, must=False)
            return tr('已撤下 {0} 的链接，{1} 上别的模型不受影响。', title, here)
        api(node, '/admin/api/reload', {}, timeout=300, retry=True)
    except Exception:
        pass  # 服务已经停了：下次启动服务时会清掉全部链接
    return tr('已撤下 {0} 的链接，{1} 现在空着：可以重新加载；刚改过开关的，也可以拨回原值再加载。', title, here)


def options_text(name, opts):
    """'MTP 开 · 表放内存'；没有可选项的不写（DeepSeek 的表恒在 SSD，MiniMax 没有表）。"""
    parts = [f"MTP {tr('开') if opts['mtp'] else tr('关')}"]
    if option_mode(name, 'ssd') == 'free': parts.append(tr('表放 SSD') if opts['ssd'] else tr('表放内存'))
    return ' · '.join(parts)


def start(name, node, assume_yes=False, free_ollama_ok=None, wait=False, keep=False, replace=False):
    """在一台上加载一个模型；这台已有另一个模型时先二次确认，再卸载它。free_ollama_ok 见 free_ollama()。
    这次真加载了就返回自检通过的时刻（time.monotonic()，重新加载时算下线多久），否则返回 None。
    这台已有别的模型时：放得下（fits_alongside）就留着它们一起跑；放不下才走切换。keep：只许留着，放不下报错；replace：一定切换。"""
    if is_rpc(node): return start_remote(name, node, assume_yes, free_ollama_ok, wait, keep, replace)
    if is_local(node):
        guard_devices()
        hold_lock(tr('加载 {0}', MODELS[name]['title']))
    else: no_active_validation()
    model, here = MODELS[name], where(node)
    if not is_local(node) and not remote_stats(node)['online']: raise RuntimeError(offline(node))
    if not model_present(node, name):
        raise RuntimeError(tr('{0} 上没有 {1} 的模型文件（{2}），先把模型复制过去。', here, model['title'], (ROOT / 'models' / model['id'])))
    reach(node)
    if not health(node, remote_stats(node).get('omlx') if not is_local(node) else None):
        if not (assume_yes or ask(tr('{0} 的 oMLX 服务没有运行，要先启动它吗？[Y/n] ', here), default=True)):
            raise RuntimeError(tr('已取消，没有启动任何东西。'))
        serve_node(node)
    wait_ready(node)
    models = models_sure(node)
    if models[name] == 'loaded':
        print(ok(tr('{0} 上的 {1} 已经在线，不用重复加载。', here, model['title'])))
        return
    if 'loading' in models.values(): raise RuntimeError(tr('{0} 有模型正在加载，请稍等后刷新。', here))
    others = [n for n in active(models) if n != name]
    current = tr('、').join(MODELS[n]['title'] for n in others)
    alongside = bool(others) and not replace and fits_alongside(name, node)[0]
    if keep and others and not alongside:
        raise RuntimeError(tr('{0} 放不下：{1} 和 {2} 一起，对话塞满时会超过内存安全线。没有动任何模型。', here, model['title'], current))
    if others and not alongside:
        if not (assume_yes or ask(tr('{0} 上现在跑着 {1}。加载 {2} 会先卸载 {3}，正在用它的对话会中断。确定切换吗？[y/N] ', here, current, model['title'], current))):
            print(tr('已取消，保持现状。'))
            return
        if is_local(node): check_ollama(name, free_ollama_ok)   # 先算，免得卸了才发现要请 Ollama 让内存：那时点取消，这台就空了（体检 11-15）
        if wait: wait_idle(node)
        for n in others: unload(n, node, quiet=True)
        print(off(tr('{0} 上的 {1} 已卸载。', here, current)))
    else:
        if alongside: print(off(tr('{0} 放得下：{1} 继续运行，不卸载。', here, current)))
        if is_local(node): check_ollama(name, free_ollama_ok)
    context = node_context(node, name)
    print(tr('正在 {0} 上加载 {1}（单机，上下文 {2}，{3}，{4}）…', here, model['title'], context, options_text(name, node_options(node, name)), model['eta']))
    started = time.monotonic()
    load_model(node, name, context, alongside)
    passed, text, seconds = canary(node, model['id'])
    if not passed: raise RuntimeError(tr('模型已加载，但自检答案不对（得到 {0}），请先不要使用。', f'{text!r}'))
    ready = time.monotonic()
    if alongside:  # 进程里不止这一个模型，量到的不是它的权重，不记。oMLX 万一自己赶走了别的模型，要说清楚，并撤掉链接
        now = node_models(node)
        for n in others:
            if now[n] != 'loaded':
                unlink_model(node, n)
                print(warn(tr('oMLX 为腾内存卸载了 {0} 上的 {1}，已撤下它的链接。', here, MODELS[n]['title'])))
    else: record_weight(node, name)  # 在预热之前量，预热写的约 2,200 token 不算进权重
    if model.get('auto'): remember_model(node, name, ready - started)
    print(ok(tr('{0} 上的 {1} 已就绪：加载 {2} 秒，自检 17×23={3}（{4} 秒）', here, model['title'], f'{ready - started:.0f}', text, f'{seconds:.1f}')))
    if model.get('warmup') and node_options(node, name)['mtp'] and not warmed(node, name):
        warmup(name, node)
    try: synced = sync_dsh_context(name, context, node)
    except OSError as e: print(warn(tr('没能把上下文同步到 dsh：{0}', e)))
    else:
        if synced: print(ok(tr('已把 dsh 里「{0} · {1}」的上下文改成 {2}（{3}），重开 dsh 后生效。', here, model['title'], context, tr('、').join(synced))))
    print(tr('在 dsh 里选「{0} · {1}」使用。', here, model['title']) if has_dsh() else tr('现在可以在客户端里用 {0} 上的 {1} 了（模型名 {2}）。', here, model['title'], model['id']))
    return ready


def start_remote(name, node, assume_yes, free_ollama_ok, wait, keep=False, replace=False):
    """走 rpc 的机器：该问的在这边问完，整个加载由对方做（链接、设置、自检、量权重、预热都在它那边），这边只同步 dsh。"""
    no_active_validation()
    model, here = MODELS[name], where(node)
    state = rpc_state(node, full=True)
    if not state: raise RuntimeError(offline(node))
    if not health(node) and not (assume_yes or ask(tr('{0} 的 oMLX 服务没有运行，要先启动它吗？[Y/n] ', here), default=True)):
        raise RuntimeError(tr('已取消，没有启动任何东西。'))
    models = node_models(node)
    others = [n for n in active(models) if n != name] if models[name] != 'loaded' else []
    current = tr('、').join(MODELS[n]['title'] for n in others)
    alongside = bool(others) and not replace and fits_alongside(name, node)[0]
    if keep and others and not alongside:
        raise RuntimeError(tr('{0} 放不下：{1} 和 {2} 一起，对话塞满时会超过内存安全线。没有动任何模型。', here, model['title'], current))
    if others and not alongside:
        if not (assume_yes or ask(tr('{0} 上现在跑着 {1}。加载 {2} 会先卸载 {3}，正在用它的对话会中断。确定切换吗？[y/N] ', here, current, model['title'], current))):
            print(tr('已取消，保持现状。'))
            return
        if wait: wait_idle(node)

    def sync(result):
        if not result.get('loaded'): return
        try: synced = sync_dsh_context(name, result['context'], node)
        except OSError as e: print(warn(tr('没能把上下文同步到 dsh：{0}', e)))
        else:
            if synced: print(ok(tr('已把 dsh 里「{0} · {1}」的上下文改成 {2}（{3}），重开 dsh 后生效。', here, model['title'], result['context'], tr('、').join(synced))))
    mode = ['--keep'] if alongside else ['--replace'] if others else []  # 这边定了，那边照办；那边的内存数变了放不下时报错，不会误卸载
    rpc_action(node, 'load', name, *(['--free-ollama'] if free_ollama_ok else []), *mode, before_last=sync)
    reach(node)


# ---------- 模型开关：MTP、表放 SSD ----------

def set_option(node, name, key, value, when=None, free_ollama_ok=None):
    """改这台这个模型的一个开关。两项都在 oMLX 的引擎签名里，不能在模型运行中直接改，要重新加载这个模型：
    只下线这台的这个模型，oMLX 服务、控制台和 dsh 都不用重启。也不能直接对已加载的模型发 PUT：oMLX 会自己卸载它，
    绕过这里撤链接（MTP 报告 §9.1），所以由这里依次卸载、改设置、加载。
    when：'now' 正在跑就当场重新加载，会中断正在答的请求（11-02 用户定：不等，想保住就取消、过会儿再拨）｜
    'later' 只保存，下次加载生效｜None 在终端里问。没在跑的直接保存。"""
    if node not in NODES or name not in MODELS or key not in OPTIONS:
        raise RuntimeError(f"option {NODE_CHOICES} {'|'.join(MODELS)} {'|'.join(OPTIONS)} on|off")
    model, here, mode, label = MODELS[name], where(node), option_mode(name, key, node), OPTIONS[key]
    if mode == 'unsupported':
        reason = (official_switch(node, name, key) or ('', '', ''))[2]
        raise RuntimeError(tr('{0} 上的 oMLX 说 {1} 不支持 {2}', here, model['title'], label) + (tr('（{0}）', reason) if reason else '') + tr('。'))
    if mode == 'locked' and official(node) and (official_switch(node, name, key) or ('', '', ''))[2] == 'forced':
        raise RuntimeError(tr('{0} 上的 oMLX 按内存把 {1} 的表固定放在 SSD 上，现在不能改。', here, model['title']))
    if mode == 'locked': raise RuntimeError(model.get('ssd_locked') or tr('{0} 的表放 SSD 是锁定的，不能改。', model['title']))
    if mode == 'na': raise RuntimeError(tr('{0} 没有这类查询表，表放 SSD 不适用。', model['title']))
    value, saved_now = bool(value), node_options(node, name)
    want = dict(saved_now, **{key: value})
    if is_rpc(node):
        word, flags = 'on' if value else 'off', ['--free-ollama'] if free_ollama_ok else []
        asked = rpc_action(node, 'option', name, key, word, when or 'ask', *flags).get('ask')
        if asked:
            when = ask_switch(model, here, label, asked['counts'])
            if when is None: print(tr('已取消，保持原样。'))
            else: rpc_action(node, 'option', name, key, word, when, *flags)
        return
    if is_local(node): hold_lock(tr('改 {0} 的 {1}', model['title'], label))
    reach(node)
    models = node_models(node) if health(node) == 'up' else {}
    state = models.get(name, 'off')
    if state == 'loading': raise RuntimeError(tr('{0} 上的 {1} 正在加载，等加载完再改。', here, model['title']))
    running = live_options(node, name) if state == 'loaded' else None
    word = tr('开') if value else tr('关')
    if state != 'loaded':
        save_node_option(node, name, key, value)
        print(ok(tr('已保存：{0} 上 {1} 的 {2} 改为{3}，下次加载时生效。', here, model['title'], label, word)))
        return
    if running == want or (running is None and saved_now == want):
        if saved_now == want:
            print(off(tr('{0} 上 {1} 的 {2} 本来就是{3}。', here, model['title'], label, word)))
        else:  # 之前选过"稍后"，现在拨回和正在跑的一样：只存，不用下线
            save_node_option(node, name, key, value)
            print(ok(tr('已保存：{0} 上 {1} 的 {2} 改回{3}，和正在运行的一致，不用重新加载。', here, model['title'], label, word)))
        return
    if when == 'ask':  # 对方在控制方的终端菜单上问：这边不问人，把请求数交回去，由那边问完再发一次
        print('@@result ' + json.dumps({'ask': {'counts': requests_now(node)}}))
        return
    if when is None:
        when = ask_switch(model, here, label, requests_now(node))
        if when is None:
            print(tr('已取消，保持原样。'))
            return
    if when == 'later':
        save_node_option(node, name, key, value)
        print(ok(tr('已保存：{0} 上 {1} 的 {2} 改为{3}，下次加载时生效；现在照常运行。', here, model['title'], label, word)))
        return
    reload_model(node, name, key, value, free_ollama_ok, [n for n in active(models) if n != name])


def ask_switch(model, here, label, counts):
    """在跑的模型要改开关：问用户什么时候切换。返回 'now'、'later'，取消返回 None。"""
    busy = sum(counts.values()) if counts else 0
    try:
        answer = input((tr('{0} 正在 {1} 上运行。{2}要重新加载才生效，这台的 {3} 会下线{4}（oMLX 服务、控制台、dsh 都不用重启）。', model['title'], here, label, model['title'], model['eta']) if has_dsh()
                        else tr('{0} 正在 {1} 上运行。{2}要重新加载才生效，这台的 {3} 会下线{4}（oMLX 服务和控制台都不用重启）。', model['title'], here, label, model['title'], model['eta']))
                       + (tr('读不到此刻的请求数，切换会中断正在答的请求。') if counts is None
                          else tr('此刻在处理 {0} 个请求，切换会立刻中断它们。', busy) if busy else '')
                       + tr('y 切换 / l 稍后（下次加载生效）/ 回车取消：')).strip().lower()
    except EOFError: answer = ''
    return {'y': 'now', 'yes': 'now', '是': 'now', 'l': 'later'}.get(answer)


def reload_model(node, name, key, value, free_ollama_ok=None, others=()):
    """把一个开关改成 value，并按新设置重新加载正在跑的这个模型：存开关 → 卸载（撤链接，中断正在答的请求）→ PUT → 加载 → 自检。
    开始后不理会停止信号，理由同 wait_idle()。"""
    model, here = MODELS[name], where(node)
    want = dict(node_options(node, name), **{key: value})
    if others and not fits_alongside(name, node, opts=want, replacing=True)[0]:  # 先算，免得卸了才发现装不回去，又误卸别的模型
        raise RuntimeError(tr('{0} 放不下：按新设置，{1} 和 {2} 一起会超过内存安全线。开关没有改。', here, model['title'], tr('、').join(MODELS[n]['title'] for n in others)))
    if is_local(node):
        guard_devices()
        check_ollama(name, free_ollama_ok, want)  # 先按新开关算放得下，免得卸了才发现装不回去（表从 SSD 改回内存要多约 95 GB）
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    save_node_option(node, name, key, value)
    print(tr('{0} 要重新加载才用上新设置：先卸载 {1} 上的它，再按新设置加载。', model['title'], here))
    started = time.monotonic()
    unload(name, node, quiet=True)
    ready = start(name, node, assume_yes=True, free_ollama_ok=free_ollama_ok, keep=bool(others))
    print(ok(tr('{0} 上的 {1} 已按新设置运行（{2}），下线约 {3} 秒。', here, model['title'], options_text(name, node_options(node, name)), f'{(ready or time.monotonic()) - started:.0f}')))


def warm_key(node):
    """预热记录的键：macOS 版本 + oMLX、MLX 版本。编好的 Metal 内核存在系统的硬盘缓存里，重载模型、重启 oMLX 都还在；
    升级 macOS 或 oMLX 后可能要重编（MTP 报告 §6.5、§6.7）。读不到返回 None，当作没预热过。"""
    if official(node):   # 官方版：oMLX 的版本读 /api/status；读不到返回 None，warmed() 当作没预热过
        version = engine_version(node)
        try: build = subprocess.run(['sw_vers', '-buildVersion'], capture_output=True, text=True, errors='replace', timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired): build = ''
        return f'{build} omlx {version}' if version and build else None
    command = f"sw_vers -buildVersion; {DOCTOR['version']}"
    try: r = run_on(node, command, timeout=30)
    except subprocess.TimeoutExpired: return None
    if r.returncode: return None
    return ' '.join(r.stdout.split()) or None


def warmed(node, name):
    """这台这个模型在现在的 macOS 和 oMLX 上预热过没有。官方版读不到版本时算没有（摸底：两边都是空，会被当成相等而悄悄不做）。"""
    key = warm_key(node)
    if official(node) and key is None: return False
    return key == saved().get('warmed', {}).get(node, {}).get(name)


# 用写代码当预热：草稿约九成被收下，核对深度 1–3 都会用到。10-31 副机 实测：用"数数"这种几乎全猜中的内容预热，
# 只编到深度 3 的版本，之后第一个写代码请求仍只有 35 token/s（每轮核对 93 毫秒），同一请求再跑一次 62.7
WARMUP_PROMPT = ('用 Python 写一个完整的命令行记账程序：支持添加、删除、按月汇总、导出 CSV，用 argparse 和 sqlite3，'
                 '每个函数都写类型标注和 docstring，最后附上完整的 pytest 单元测试。只输出代码。')


def warmup(name, node):
    """DeepSeek 开 MTP 后，核对草稿用的注意力内核按上下文长度分约 450 个版本，第一次遇到某个版本要现场编译，
    头一两分钟忽快忽慢（MTP 报告 §6）。连续写约 2,200 token，把各个长度都编一遍；同一台、同样的 macOS 和 oMLX
    只做一次。期间可以用，但会慢。没做完不算失败，只提示。"""
    model, here = MODELS[name], where(node)
    print(tr('{0} 第一次开着 MTP 用 {1}，正在预热（约 1–3 分钟，期间可以用，但会慢）…', here, model['title']))
    body = {'model': model['id'], 'messages': [{'role': 'user', 'content': WARMUP_PROMPT}], 'temperature': 0,
            'max_tokens': 2200, 'chat_template_kwargs': {'enable_thinking': False}}
    started = time.monotonic()
    try: reply = api(node, '/v1/chat/completions', body, timeout=900)
    except Exception as exc:
        print(warn(tr('预热没做完（{0}）。不影响使用，头几分钟可能忽快忽慢；可以稍后运行 warmup {1} --on {2} 再做一次。', exc, name, node)))
        return False
    tokens = (reply.get('usage') or {}).get('completion_tokens') or 0
    if tokens < 2000:
        print(warn(tr('预热只写了 {0} token，可能没覆盖所有长度；可以稍后运行 warmup {1} --on {2} 再做一次。', tokens, name, node)))
        return False
    conf = saved()
    conf.setdefault('warmed', {}).setdefault(node, {})[name] = warm_key(node)
    save_conf(conf)
    print(ok(tr('{0} 的 {1} 预热完成：写了 {2} token，用时 {3} 秒；这台以后不用再预热。', here, model["title"], tokens, f'{time.monotonic() - started:.0f}')))
    return True


def warmup_command(name, node):
    """手动预热（命令行 warmup）：系统的 Metal 缓存被别的程序挤掉后，开着 MTP 又忽快忽慢时用。"""
    if is_rpc(node):
        rpc_action(node, 'warmup', name)
        return
    model, here = MODELS[name], where(node)
    if not model.get('warmup'): raise RuntimeError(tr('只有 {0} 开 MTP 时需要预热，{1} 不用。', tr('、').join(m['title'] for m in MODELS.values() if m.get('warmup')), model['title']))
    reach(node)
    if node_models(node)[name] != 'loaded': raise RuntimeError(tr('{0} 上没有加载 {1}，加载后才能预热。', here, model['title']))
    if not (live_options(node, name) or {}).get('mtp'): raise RuntimeError(tr('{0} 上的 {1} 没开 MTP，不用预热。', here, model['title']))
    warmup(name, node)


# ⓘ 的说明：两个开关的在型号档案的 info 段（来源写在档案的"说明"里），上下文和缓存的在这里，文字来自上下文与缓存报告第 14 章；
# 控制台应用和终端菜单都从这里取。上下文还有一条每个模型自己的推荐理由，在档案的 context_why。
def cache_sizes():
    """一段 256K 的对话占多少多轮缓存：这台清单里①写明了这个数的已发布模型（按 id 对上），从小到大；没有就不说。"""
    rows = [((known_facts(m['id']) or (None, {}))[1].get('cache_256k_gb'), pick(FAMILIES.get(m.get('family'), {}).get('title')) or m['title']) for m in MODELS.values()]
    return tr('、').join(f'{title} {size:g} GB' for size, title in sorted(r for r in rows if isinstance(r[0], (int, float))))


CONTEXT_INFO = [
    (tr('是什么'), tr('一次对话最多装多少 token：资料、问题、历史消息和回答加在一起。档位只是允许的长度，不预先占资源。')),
    *([(tr('压缩点'), tr('dsh 不等装满，到压缩点就先压缩：256K 到 16.4 万，512K 到 41.9 万，1M 到 83.9 万。')),
       (tr('调大'), tr('压缩更晚，能放更长的资料；但缓存丢了时重读更久，对话塞满时更占内存（看卡片上的"最多约"）。'))] if has_dsh() else
      [(tr('调大'), tr('能放更长的资料；但缓存丢了时重读更久，对话塞满时更占内存（看卡片上的"最多约"）。'))]),
    *([(tr('占缓存'), tr('一段 256K 的对话占缓存约 {0}。', cache_sizes()))] if cache_sizes() else []),
]
CONTEXT_EFFECT = (tr('生效'), tr('改了当场生效，不用重新加载，正在进行的对话不中断；同时写进 dsh，重开 dsh 后按新额度显示剩余。调小后，已超过新上限的长对话会被拒绝，新开对话即可。') if has_dsh()
                  else tr('改了当场生效，不用重新加载，正在进行的对话不中断。调小后，已超过新上限的长对话会被拒绝，新开对话即可。'))
CACHE_INFO = [   # 0.5.1 A6（单机试用 F1）：整段改短，加"自动"的规则（不写数值）
    (tr('是什么'), tr('读过的内容的中间结果存到这台的硬盘，下次开头相同就直接取回，不用重读。占硬盘，不占内存。')),
    (tr('上限'), tr('每台一个，所有模型共用，满了先删最久没用的。一般选 100 GB（推荐）；常聊 1M 的长对话选 200 GB。只列不超过（剩余硬盘 + 已有缓存）÷ 2 的档。')),
    *([(tr('自动'), tr('oMLX 默认的上限：按（剩余硬盘 + 已有缓存）÷ 2 定，硬盘剩得多就大，随时在变。'))] if not MANAGED_HERE else []),
    (tr('关'), tr('资料不再写入硬盘，续问要从头读。已有的不会自动删，点"清空缓存"才删。')),
    (tr('隐私'), tr('笔记是从你的资料算出来的，关机重启也还在硬盘上；上限越大，留得越久。')),
    (tr('生效'), tr('不用重启服务：这台的模型会卸载，改完再加载回来。')),
]


def option_info(name):
    """两个开关和上下文的 ⓘ：每项是 [标题, 正文]。MTP 的下线时长写在风险里；表放 SSD 的最后一条说明怎么生效。"""
    model = MODELS[name]
    mtp = [[title, text.format(eta=model['eta'])] for title, text in model['info'].get('mtp', [])]   # 没声明这个开关的（陌生模型）没有说明
    effect = [tr('生效'), tr('要重新加载这个模型才生效，这台的 {0} 下线{1}；oMLX 服务、控制台、dsh 都不用重启。', model['title'], model['eta']) if has_dsh()
              else tr('要重新加载这个模型才生效，这台的 {0} 下线{1}；oMLX 服务和控制台都不用重启。', model['title'], model['eta'])]   # 整句：英文才通顺
    mode = option_mode(name, 'ssd', LOCAL)
    ssd = [list(pair) for pair in model['info'].get('ssd', [])]
    if official(LOCAL) and mode != 'na' and (model['ssd'] == 'locked' or not ssd):   # 官方版：出厂说明写死锁定的、没写说明的陌生模型，按 oMLX 现在的做法说（A13）
        ssd = [*(ssd[:1] or [[tr('是什么'), tr('把模型的查询表留在 SSD 上，用到时再读，省内存，答案不变。')]]),
               [tr('什么时候开'), tr('内存放得下整张表时可以关着，读表更快；放不下时 oMLX 会自己放到 SSD 上，这个开关会锁定。对话塞满会超安全线时，打开它能省下表占的内存。')]]
    if mode == 'free': ssd.append(effect)
    return {'mtp': mtp, 'ssd': ssd, 'context': [list(pair) for pair in (*CONTEXT_INFO, (tr('推荐'), model['context_why']), CONTEXT_EFFECT)]}


def option_state(node, name, n):
    """status --json 给卡片用：每个开关存的值（on）、正在跑的值（live，没在跑为 None）、是否等下次加载才生效（pending）。"""
    opts = node_options(node, name)
    running = live_options(node, name) if n['models'].get(name) == 'loaded' else None
    out = {}
    for key in OPTIONS:
        live = running.get(key) if running else None
        out[key] = {'on': opts[key], 'mode': option_mode(name, key, node), 'live': live,
                    'pending': live is not None and live != opts[key]}
        if out[key]['mode'] == 'unsupported':   # 官方版：灰掉，原因是 oMLX 自己给的（英文原文，可能空）
            out[key]['reason'] = (official_switch(node, name, key) or ('', '', ''))[2]
        elif out[key]['mode'] == 'locked' and official(node) and (official_switch(node, name, key) or ('', '', ''))[2] == 'forced':
            out[key]['reason'] = 'forced'   # 放不下，oMLX 自动放 SSD（A13）：界面按这个说，不说"必须放 SSD"
    return out


# ---------- 多轮缓存 ----------

def cache_now(node, n, probe):
    """这台的多轮缓存现在是什么样：服务开着看 oMLX 自己的设置（它按这个在跑），否则看存的（下次启动用）。0 是关；None 是读不到。
    官方版不管开没开都看它自己的设置（启动时不由我们带上）；'auto' 是它默认的"自动"。"""
    return probe.get('cache_limit') if n['up'] or official(node) else node_cache(node)


def cache_word(value): return tr('关') if value == 0 else tr('读不到') if value is None else tr('自动') if value == 'auto' else f'{value} GB'


def cache_fields(now, auto_ok=False):
    """状态里的上限：cache_limit_gb 照旧是整数或没有（应用按 Int 读）；官方默认的自动另给 cache_auto。
    auto_ok：这台能选"自动"（官方版，0.5.1 A14）；自管版没有这个字段，状态和以前逐字段相同"""
    return {**({'cache_limit_gb': None, 'cache_auto': True} if now == 'auto' else {'cache_limit_gb': now}), **({'cache_auto_ok': True} if auto_ok else {})}


def cache_text(node, n, probe):
    now, used = cache_now(node, n, probe), probe.get('cache', 0) / GIB
    if now == 0: return tr('多轮缓存关（留着 {0} GB）', f'{used:.1f}') if used else tr('多轮缓存关')
    if now == 'auto': return tr('多轮缓存 {0} GB（上限自动）', f'{used:.1f}')
    return tr('多轮缓存 {0} / {1} GB', f'{used:.1f}', now) if now else tr('多轮缓存 {0} GB', f'{used:.1f}')


def cache_options(node, n, probe):
    """这台能选的上限（GB，关总能选）：不超过（剩余硬盘 + 现有缓存）÷ 2 的档，加上现在用的和存的。读不到硬盘就都列。"""
    room = None if probe.get('disk_free') is None else (probe['disk_free'] + probe.get('cache', 0)) / 2
    keep = {v for v in (cache_now(node, n, probe), node_cache(node)) if isinstance(v, int) and v}   # 'auto' 不进选项
    return sorted({v for v in CACHE_SIZES if room is None or v * GIB <= room} | keep)


def set_cache(node, word, assume_yes=False, free_ollama_ok=None):
    """改这台的多轮缓存：word 是 off、上限（GB），官方版还能是 auto（它默认的"自动"，0.5.1 A14：改成具体档以后能选回来）。
    不用重启服务：oMLX 的管理接口能改设置和运行中的配置，但它会自己卸载这台的
    全部模型、不撤链接，客户端一点又拉回内存（上下文与缓存报告第 7.4 节，同 set_option 的理由）。所以由这里依次卸载、改、
    按原样加载回来。服务没开时只存，下次启动由启动脚本带上。调小后 oMLX 加载时自己淘汰到上限的约 90%。"""
    if node not in NODES or not (word == 'off' or word.isdigit() or word == 'auto'):
        raise RuntimeError(f"cache set {NODE_CHOICES} off|{'|'.join(map(str, CACHE_SIZES))}")
    if word == 'auto' and not is_rpc(node) and not official(node):
        raise RuntimeError(tr('{0} 的 oMLX 是自管版，多轮缓存没有"自动"，请选关或一个上限。', where(node)))
    value, here = 0 if word == 'off' else 'auto' if word == 'auto' else int(word), where(node)
    if is_rpc(node):
        rpc_action(node, 'cache-set', word, *(['--free-ollama'] if free_ollama_ok else []))
        return
    if is_local(node): hold_lock(tr('改多轮缓存'))
    probe = local_stats() if is_local(node) else remote_stats(node)
    if not probe['online']: raise RuntimeError(offline(node))
    n = node_snapshot(node, None if is_local(node) else probe)
    now, choices = cache_now(node, n, probe), cache_options(node, n, probe)
    if isinstance(value, int) and value and value not in choices:
        raise RuntimeError(tr('{0} 的多轮缓存只能选：关、{1}（不超过（剩余硬盘 + 现有缓存）÷ 2）。', here, tr('、').join(f'{v} GB' for v in choices)))
    if value == now:
        if value != 'auto': save_node_cache(node, value)   # "自动"只有官方版有，它启动时用自己的设置，不存
        print(off(tr('{0} 的多轮缓存本来就是 {1}。', here, cache_word(value))))
        return
    if not n['up']:
        if official(node): raise RuntimeError(tr('{0} 的 oMLX 服务没开。官方版的多轮缓存要开着服务改（它启动时用自己的设置）。', here))
        save_node_cache(node, value)
        print(ok(tr('已保存：{0} 的多轮缓存改为 {1}，下次启动服务时生效。', here, cache_word(value))))
        return
    if 'loading' in n['models'].values(): raise RuntimeError(tr('{0} 有模型正在加载，等加载完再改。', here))
    models = active(n['models'])
    names = tr('、').join(MODELS[m]['title'] for m in models)
    used = probe.get('cache', 0) / GIB
    shrink = isinstance(value, int) and bool(value) and used > value * 0.9
    if not (assume_yes or ask(tr('把 {0} 的多轮缓存从 {1} 改成 {2}？', here, cache_word(now), cache_word(value))
                              + (tr('{0} 会先卸载、改完再加载回来，正在答的请求会中断。', names) if models else '')
                              + (tr('现在占 {0} GB，下次加载时会删掉最久没用的笔记，删到约 {1} GB，不能恢复。', f'{used:.1f}', f'{value * 0.9:.0f}') if shrink else '')
                              + ('[y/N] '))):
        print(tr('已取消，保持原样。'))
        return
    if is_local(node):
        guard_devices()
        for name in models: check_ollama(name, free_ollama_ok)  # 先算，免得卸了才发现装不回去
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # 开始后不理会停止信号，理由同 wait_idle()
    started = time.monotonic()
    try:
        for name in models: unload(name, node, quiet=True)
        # 卸载、重新扫描后紧跟的请求被重置过（见 api() 的说明）：这一条重复做无害，重试一次
        api(node, '/admin/api/global-settings', {'cache_enabled': bool(value), **({'ssd_cache_max_size': 'auto' if value == 'auto' else f'{value}GB'} if value else {})}, timeout=120, retry=True)
        probe = local_stats() if is_local(node) else remote_stats(node)
        if probe.get('cache_limit') != value: raise RuntimeError(tr('oMLX 的设置还是 {0}', cache_word(probe.get('cache_limit'))))
    except Exception as exc:   # 连接被重置、超时不是 RuntimeError，以前接不住，模型停在已卸载（体检 11-15）
        for i, name in enumerate(models): start(name, node, assume_yes=True, free_ollama_ok=free_ollama_ok, keep=i > 0)
        raise RuntimeError(tr('{0} 的多轮缓存没改成（{1}）{2}。', here, exc, (tr('，模型已按原样加载回来') if models else ''))) from exc
    if value != 'auto': save_node_cache(node, value)
    ready = None
    for i, name in enumerate(models): ready = start(name, node, assume_yes=True, free_ollama_ok=free_ollama_ok, keep=i > 0)
    notes = []
    if models: notes.append(tr('{0} 已加载回来，下线约 {1} 秒', names, f'{(ready or time.monotonic()) - started:.0f}'))
    if shrink: notes.append(tr('加载时删掉了最久没用的笔记') if models else tr('下次加载时会删掉最久没用的笔记'))
    if not value and used: notes.append(tr('资料不再写入硬盘；已有的缓存还在，可以用"清空缓存"删掉'))
    print(ok(tr('已生效：{0} 的多轮缓存 {1} → {2}', here, cache_word(now), cache_word(value)) + ''.join(tr('；{0}', x) for x in notes) + tr('。')))


def clear_cache(target='all', assume_yes=False):
    """清空多轮缓存。这台有模型在内存里时缓存正被使用，不能删；卸载后删除，下次加载时 oMLX 重新扫描目录，
    缺了的文件只当没命中。范围和 oMLX 自己的"清空 SSD 缓存"一致（含 _gdn_sidecars 里 Qwen 的快照），
    外加双机时代留下的旧缓存。删除不可恢复。"""
    nodes = pick_nodes(target)
    for node in nodes:
        # 远程的先把隧道接上：隧道断着时 health() 读不到，会把"有模型在内存里"看成没有，把正在用的缓存删掉（体检 11-15）。连不上就不清
        if not is_local(node): reach(node)
        with models_of(node): busy = health(node) == 'up' and active(node_models(node))   # 按这台的清单看（2b）
        if busy: raise RuntimeError(tr('{0} 上有模型在内存里，缓存正在使用；先卸载再清空。', where(node)))
    names = tr('、').join(where(n) for n in nodes)
    if LOCAL in nodes: hold_lock(tr('清空缓存'))
    if not (assume_yes or ask(tr('删除 {0} 上的多轮缓存？删除后不可恢复。[y/N] ', names))):
        print(tr('已取消。'))
        return
    # 每段都在子 shell 里先 cd 成功才删；进不了目录就什么都不删
    command = (f'( cd "{CACHE_DIR}" 2>/dev/null && du -sk -c {CACHE_SHARDS} 2>/dev/null | tail -1 | cut -f1 && '
               f'find {CACHE_SHARDS} -name "*.safetensors" -type f -delete 2>/dev/null; '
               'find _gdn_sidecars -mindepth 1 -type d -empty -delete 2>/dev/null; true ); '
               f'( cd "$HOME/{LEGACY_CACHE}" 2>/dev/null && du -sk . | cut -f1 && find . -mindepth 1 -maxdepth 1 -exec rm -rf -- {{}} + ); '
               'echo done')
    for node in nodes:
        if official(node):   # 官方版：用它自己的清空接口，删前删后各量一次（摸底：照自管的路径删，进不了目录还会说"已删除约 0 GB"）
            folder = official_cache_dir(official_cache_conf())
            before = cache_used(folder)
            try: api(node, '/admin/api/ssd-cache/clear', {}, timeout=300)
            except Exception as exc:
                print(warn(tr('{0} 没有清理成功（{1}），缓存还在；稍后再清一次。', where(node), exc)))
                continue
            print(ok(tr('{0}：已删除多轮缓存约 {1}。', where(node), gb(max(before - cache_used(folder), 0)))))
            continue
        if is_rpc(node):
            try: rpc_action(node, 'clear-cache')
            except RuntimeError: print(warn(tr('{0} 连不上或没有清理成功，缓存还在；稍后再清一次。', where(node))))
            continue
        try: r = run_on(node, command, timeout=120)
        except subprocess.TimeoutExpired: r = None
        lines = r.stdout.split() if r and r.returncode == 0 else []
        if lines[-1:] == ['done']:
            freed = sum(int(x) for x in lines[:-1] if x.isdigit()) * 1024
            print(ok(tr('{0}：已删除多轮缓存约 {1}。', where(node), gb(freed))))
        else:
            print(warn(tr('{0} 连不上或没有清理成功，缓存还在；稍后再清一次。', where(node))))


# ---------- 密钥 ----------

_LATER = tr('、').join(map(where, remotes()))
KEY_NOTE = (tr('只有一把 oMLX 主密钥，{0}共用，管所有模型；它也是管理页的登录密码，权限是管理员级。客户端请用子密钥：能调用模型，但不能加载、卸载模型（只认主密钥，也就是只有本控制台能做），不能登录管理页、不能改设置和密钥；可以按名字单独吊销。新建、吊销、重置都会同时改{1}，立即生效、不用重启；', EVERY, EVERY)
            + (tr('{0} 没开时，下次启动它会先同步过去。', _LATER) if _LATER else ''))
if any(map(is_rpc, NODES)):  # D5：主密钥各台各管，子密钥由这边生成后经 rpc 建到各台
    KEY_NOTE = (tr('每台有自己的 oMLX 主密钥，只管那一台，只在那台本机复制和重置；客户端请用子密钥：能调用模型，但不能加载、卸载模型，不能登录管理页、不能改设置和密钥；可以按名字单独吊销。新建、吊销子密钥会同时改{0}，立即生效、不用重启；那时连不上的机器要等开机后再做一次。', EVERY))


def auth_settings():
    """直接读本机的 settings.json，服务没运行时也能用。内容含明文密钥，只在程序内部使用、不打印。
    官方版读它数据目录里的那份，先过"允许读主密钥"这一关（没允许、没装、没数据目录都在 official_key 里报）。"""
    if not MANAGED_HERE:
        official_key()
        return json.loads(ENGINE_HERE.settings.read_text())['auth']
    return json.loads((STATE / 'settings.json').read_text())['auth']


def api_key():
    return auth_settings()['api_key']


def sub_key(name):
    return next((k for k in auth_settings().get('sub_keys', []) if k.get('name') == name), None)


def key_digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def disabled_keys(conf=None):
    """停用名单（0.5.1 B1）：ROOT/gateway.json 的 disabled_keys，{子密钥的 SHA-256: {name, at}}，只存哈希不存密钥。
    没有、读不懂、格式不对都是 {}（入口同样当没有名单，不挡人）；也认列表写法。"""
    conf = gateway_conf() if conf is None else conf
    rows = (conf or {}).get('disabled_keys')
    if isinstance(rows, list): return {k.lower(): {} for k in rows if isinstance(k, str)}
    if not isinstance(rows, dict): return {}
    return {k.lower(): (v if isinstance(v, dict) else {}) for k, v in rows.items() if isinstance(k, str)}


def can_disable():
    """停用靠入口挡，所以要入口开着、而且就在这台上才起作用：(能不能, 不能时的原因)。客户端直连 oMLX 时入口管不着。"""
    import hub
    if not hub.gateway_enabled(ENGINE_HERE['kind']): return False, tr('入口关着，客户端直连 oMLX，停用不起作用。要停掉一把，只能删除它。')
    if not hub.gateway_follows_omlx() and not gateway_on_hub_here(): return False, tr('入口在主机上，到主机的控制台里停用。')
    return True, ''


def set_sub_key_disabled(name, off):
    """停用 / 启用一把子密钥：只改停用名单，oMLX 里的密钥不动，所以随时能恢复。入口按文件修改时间重读，立刻生效。"""
    entry = sub_key(name)
    if not entry: raise RuntimeError(tr('没有名为「{0}」的子密钥。', name))
    if off:
        allowed, why = can_disable()
        if not allowed: raise RuntimeError(why)
    conf = gateway_conf()
    if conf is None: raise RuntimeError(tr('{0} 读不懂，没改。请先看一眼这个文件。', ROOT / 'gateway.json'))
    rows, digest = disabled_keys(conf), key_digest(entry['key'])
    if off: rows[digest] = {'name': name, 'at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    else: rows.pop(digest, None)
    if rows: conf['disabled_keys'] = rows
    else: conf.pop('disabled_keys', None)
    write_gateway_conf(conf)
    print(ok(tr('已停用子密钥「{0}」：经入口的调用立刻被拒。密钥还留着，随时能启用。', name) if off else tr('已启用子密钥「{0}」。', name)))


def reveal_key(name=None):
    """给应用的"显示"用：完整密钥只走标准输出的管道，应用显示 10 秒就收起，不写日志、不存。不给名字是主密钥。"""
    if not name: return print(json.dumps({'ok': True, 'key': api_key()}))
    entry = sub_key(name)
    if not entry: raise RuntimeError(tr('没有名为「{0}」的子密钥。', name))
    print(json.dumps({'ok': True, 'key': entry['key']}))


def status_sub_keys():
    """给状态用：官方版允许过读主密钥、现在却读不到（oMLX 被卸了、数据目录不在、端口被别人占着）时先空着。
    以前整个 status --json 跟着失败，应用收不到状态，安装入口和装好以后的步骤都不出现（体检 11-15）。自管版照旧。"""
    if MANAGED_HERE: return list_sub_keys()
    try: return list_sub_keys()
    except RuntimeError: return []


def list_sub_keys():
    if not MANAGED_HERE and key_consent() is not True: return []   # 官方版还没允许读主密钥：状态里先空着，不报错（10.3）
    off = disabled_keys()   # 停用的多一个 disabled；没停用的和以前逐字段相同
    return [{'name': k.get('name', ''), 'created_at': k.get('created_at', ''), 'hint': '…' + k.get('key', '')[-4:],
             **({'disabled': True} if off and key_digest(k.get('key', '')) in off else {})}
            for k in auth_settings().get('sub_keys', [])]


def remote_sub_keys(node):
    """那台有哪些子密钥：明文留在那台，这边只拿名字、创建时间和末 4 位。连不上返回 None。"""
    if is_rpc(node):
        try: return rpc_query(node, 'subkey-list')
        except RuntimeError: return None
    code = ("import json;print(json.dumps([{'name':k.get('name',''),'created_at':k.get('created_at',''),'hint':'…'+k.get('key','')[-4:]} "
            f"for k in json.load(open('{STATE / 'settings.json'}'))['auth'].get('sub_keys',[])]))")
    try: r = ssh_to(node, f'{ROOT}/.omlx-venv/bin/python -I -c {shlex.quote(code)}')
    except subprocess.TimeoutExpired: return None
    try: return json.loads(r.stdout) if r.returncode == 0 else None
    except ValueError: return None


def key_coverage():
    """密钥表：一把子密钥在哪几台上（同名且末 4 位相同算同一把）。只在打开密钥窗口时查，不进 10 秒一次的刷新。"""
    rows, unreachable = {}, []
    for key in list_sub_keys(): rows[(key['name'], key['hint'])] = {**key, 'on': [LOCAL]}
    for node in remotes():
        found = remote_sub_keys(node)
        if found is None:
            unreachable.append(node)
            continue
        for key in found: rows.setdefault((key['name'], key['hint']), {**key, 'on': []})['on'].append(node)
    if not MANAGED_HERE:   # 0.6（只在官方版）：每把最近一次经入口用是什么时候、从哪来；太短的别的设备用不了
        used = key_last_used()
        short = {k.get('name', '') for k in auth_settings().get('sub_keys', []) if len(k.get('key', '')) < 32} if key_consent() is True else set()
        for row in rows.values():
            if LOCAL not in row['on']: continue
            if row['name'] in used: row['last_used'] = used[row['name']]
            if row['name'] in short: row['short'] = True
    return {'ok': True, 'order': list(NODES), 'unreachable': unreachable, 'sub_keys': list(rows.values())}


# 再起一个自己：导入着跑，不把 clusterctl.py 当脚本读——系统自带的 Python 3.9 直接读它会报"Non-UTF-8 code"（踩坑 #75）
SELF_RUN = 'import sys; sys.path.insert(0, sys.argv[1]); sys.argv = ["clusterctl.py"] + sys.argv[2:]; import clusterctl; clusterctl.main()'


def copy_secret(value, label, ref):
    subprocess.run(['/usr/bin/pbcopy'], input=value, text=True, check=True)
    # 后台进程按名字自己重新读密钥来比对，密钥不出现在任何命令行参数里
    subprocess.Popen([sys.executable, '-B', '-c', SELF_RUN, str(Path(__file__).resolve().parent), '_clear-clipboard', ref], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(tr('{0}已复制到剪贴板，30 秒后自动清空（那时剪贴板里如果已经换成别的内容就不动）。不要发给别人。', label))


def copy_key(name=None):
    if not name:
        copy_secret(api_key(), tr('oMLX 主密钥'), '')
        return
    entry = sub_key(name)
    if not entry: raise RuntimeError(tr('没有名为「{0}」的子密钥。', name))
    copy_secret(entry['key'], tr('子密钥「{0}」', name), name)


def clear_clipboard_later(ref):
    time.sleep(30)
    entry = sub_key(ref) if ref else None
    expected = entry['key'] if entry else (api_key() if not ref else None)
    current = subprocess.run(['/usr/bin/pbpaste'], capture_output=True, text=True, errors='replace').stdout
    if expected and current == expected:
        subprocess.run(['/usr/bin/pbcopy'], input='', text=True)


def need_service():
    if health(LOCAL) != 'up': raise RuntimeError(tr('{0} 的 oMLX 没有运行，请先启动服务再管理密钥（密钥以 {1} 为准）。', where(LOCAL), where(LOCAL)))


def remote_up(node):
    if not remote_stats(node).get('omlx'): return False
    reach(node)
    return health(node) == 'up'


def create_sub_key(name, show=False):
    name = (name or '').strip()
    if not name: raise RuntimeError(tr('请给子密钥起个名字，例如 dsh。') if has_dsh() else tr('请给子密钥起个名字，例如用它的客户端的名字。'))
    if sub_key(name): raise RuntimeError(tr('已经有名为「{0}」的子密钥了；换个名字，或先吊销旧的。', name))
    need_service()
    value = secrets.token_urlsafe(36)
    api(LOCAL, '/admin/api/sub-keys', {'key': value, 'name': name}, timeout=30)
    for node in remotes():
        if is_rpc(node):
            try: rpc_query(node, 'subkey-create', data=json.dumps({'name': name, 'key': value}))
            except RuntimeError as exc: print(warn(tr('{0} 上没建成（{1}）：等它开机后吊销这把、再新建一次。', where(node), exc)))
        elif remote_up(node): api(node, '/admin/api/sub-keys', {'key': value, 'name': name}, timeout=30)
    print(ok(tr('已新建子密钥「{0}」（末 4 位 {1}）。经入口它只能调用模型，不能加载、卸载，不能登录管理页、不能改设置；客户端请连入口的地址。', name, value[-4:]) if not MANAGED_HERE else   # 官方 oMLX 的子密钥直连它的端口时能加载模型（0.6 报告 R7）
             tr('已新建子密钥「{0}」（末 4 位 {1}）。它只能调用模型，不能加载、卸载，不能登录管理页、不能改设置。', name, value[-4:]) if SOLO else
             tr('已新建子密钥「{0}」（末 4 位 {1}），{2}都能用。它只能调用模型，不能加载、卸载，不能登录管理页、不能改设置。', name, value[-4:], EVERY)))
    if show: print('@@secret ' + value)  # 管理控制台：经 SSH 回去，由那边复制到它的剪贴板；应用不把这行显示出来，也不写日志
    else: copy_secret(value, tr('子密钥「{0}」', name), name)


def revoke_sub_key(name, assume_yes=False):
    entry = sub_key(name)
    if not entry: raise RuntimeError(tr('没有名为「{0}」的子密钥。', name))
    need_service()
    if not (assume_yes or ask(tr('吊销后，用「{0}」这把子密钥的客户端会立刻无法调用。确定吗？[y/N] ', name) if SOLO else
                              tr('吊销后，用「{0}」这把子密钥的客户端会立刻无法调用（{1}都是）。确定吗？[y/N] ', name, EVERY))):
        print(tr('已取消。'))
        return
    api(LOCAL, '/admin/api/sub-keys', {'key': entry['key']}, method='DELETE', timeout=30)
    for node in remotes():
        if is_rpc(node):
            try: rpc_query(node, 'subkey-revoke', data=json.dumps({'name': name}))
            except RuntimeError as exc: print(warn(tr('{0} 上没吊销成（{1}）：它开机后要再吊销一次，在那之前这把在它上面还能用。', where(node), exc)))
        elif remote_up(node): api(node, '/admin/api/sub-keys', {'key': entry['key']}, method='DELETE', timeout=30)
    conf = gateway_conf()   # 删掉的这把如果在停用名单里，顺手清掉；名单读不懂就不碰
    if conf and key_digest(entry['key']) in disabled_keys(conf):
        rows = disabled_keys(conf)
        rows.pop(key_digest(entry['key']), None)
        if rows: conf['disabled_keys'] = rows
        else: conf.pop('disabled_keys', None)
        write_gateway_conf(conf)
    print(ok(tr('已吊销子密钥「{0}」。', name)))


def reset_main_key(assume_yes=False):
    need_service()
    if not (assume_yes or ask(tr('重置后旧主密钥立刻失效，用主密钥的客户端都要换成新的（子密钥不受影响）。确定吗？[y/N] ') if SOLO else
                              tr('重置后旧主密钥立刻失效（{0}都是），用主密钥的客户端都要换成新的（子密钥不受影响）。确定吗？[y/N] ', EVERY))):
        print(tr('已取消。'))
        return
    old, value = api_key(), secrets.token_urlsafe(36)
    sync = [node for node in remotes() if not is_rpc(node) and remote_up(node)]  # 走 rpc 的各管各的主密钥（D5）
    api(LOCAL, '/admin/api/global-settings', {'api_key': value}, timeout=30, key=old)
    if api_key() != value: raise RuntimeError(tr('oMLX 没有把新主密钥写进 settings.json，请在管理页确认。'))
    api(LOCAL, '/v1/models', timeout=10)  # api() 每次重新读 settings.json，这一请求证明新密钥已经生效
    for node in sync:
        api(node, '/admin/api/global-settings', {'api_key': value}, timeout=30, key=old)
        api(node, '/v1/models', timeout=10)
    print(ok(tr('主密钥已重置（末 4 位 {0}），立即生效，不用重启。', value[-4:])))
    copy_secret(value, tr('新主密钥'), '')


def key_menu():
    print('\n' + KEY_NOTE)
    subs = list_sub_keys()
    print(tr('子密钥：') + (tr('、').join(tr('{0}（{1}）', (k['name'] or tr('（未命名）')), k['hint']) for k in subs) if subs else tr('还没有')))
    print(tr('  1 复制主密钥   2 新建子密钥   3 复制子密钥   4 吊销子密钥   5 重置主密钥   回车返回'))
    pick = input(tr('选择：')).strip()
    if pick == '1': copy_key()
    elif pick == '2': create_sub_key(input(tr('子密钥名字（例如 dsh）：') if has_dsh() else tr('子密钥名字（例如用它的客户端的名字）：')))
    elif pick == '3': copy_key(input(tr('复制哪一把（名字）：')).strip())
    elif pick == '4': revoke_sub_key(input(tr('吊销哪一把（名字）：')).strip())
    elif pick == '5': reset_main_key()


# ---------- 其他 ----------

def set_context(node, name, value):
    """改这台跑这个模型的上下文。正在跑就当场推给这台的 oMLX，不重新加载。
    依据（10-30 查 oMLX 源码）：每个请求都按当前设置检查长度（server.py validate_context_window）；
    上下文只在 Qwen 的 YaRN 里影响引擎构造（engine_pool._engine_runtime_signature），DeepSeek、MiniMax 不受影响。"""
    if node not in NODES or name not in MODELS: raise RuntimeError(tr('context {0} {1} <档位>', NODE_CHOICES, '|'.join(MODELS)))
    model, here = MODELS[name], where(node)
    if is_rpc(node):
        def sync(result):
            try: synced = sync_dsh_context(name, result['value'], node)
            except OSError as e: print(warn(tr('没能把上下文同步到 dsh：{0}', e)))
            else:
                if synced: print(tr('dsh 里的「{0} · {1}」也改成了 {2}（{3}），重开 dsh 后它按新额度提示剩余和压缩。', here, model['title'], result['value'], tr('、').join(synced)))
        rpc_action(node, 'context', name, str(value), before_last=sync)
        return
    if is_local(node): hold_lock(tr('改 {0} 的上下文', model['title']))
    n = node_snapshot(node)
    info = context_info(node, name, n)
    if not str(value).isdigit() or int(value) not in info['options']:
        raise RuntimeError(tr('{0} 上的 {1} 只能选：{2}（模型上限 {3}）', here, model['title'], tr('、').join(map(str, info['options'])), info['native']))
    value, old = int(value), info['live'] or info['setting']
    save_node_context(node, name, value)
    live = info['live'] is not None
    if live:
        r = api(node, f"/admin/api/models/{quote_id(model['id'])}/settings", {'max_context_window': value}, method='PUT', timeout=30, retry=True)
        if r.get('requires_reload'):  # 按上面的依据不该发生；万一 oMLX 升级后变了，如实告诉用户
            raise RuntimeError(tr('{0} 的 oMLX 说这项改动要重新加载才生效{1}，请重新加载 {2}。', here, (tr('，已在请求结束后卸载') if r.get('auto_unloaded') else ''), model['title']))
    # 没在跑也同步：dsh 按自己配置里的额度显示和压缩，下次加载前就要对
    try: synced = sync_dsh_context(name, value, node)
    except OSError as e: print(warn(tr('没能把上下文同步到 dsh：{0}', e)))
    else:
        if synced: print(tr('dsh 里的「{0} · {1}」也改成了 {2}（{3}），重开 dsh 后它按新额度提示剩余和压缩。', here, model['title'], value, tr('、').join(synced)))
    # 控制台只把最后一行显示在提示条里，所以结论放最后，注意事项并进同一行
    notes = []
    if value not in MODELS[name]['verified']: notes.append(tr('这一档未实测，资料塞得很长时可能很慢或内存不够，出问题改回已实测的档位'))
    total = (local_stats() if is_local(node) else remote_stats(node)).get('total')
    if value in context_info(node, name, n, total)['over']: notes.append(tr('这一档对话塞满时会超内存安全线'))
    if live and value < old: notes.append(tr('调小后超过新上限的长对话会被拒绝，新开对话即可'))
    head = (tr('已生效：{0} 上的 {1} 上下文 {2} → {3}，不用重新加载，对话不中断', here, model['title'], ctx_label(old), ctx_label(value)) if live
            else tr('已保存：{0} 下次加载 {1} 时用 {2}', here, model['title'], ctx_label(value)))
    print((warn if notes else ok)(head + (tr('；') + tr('；').join(notes) if notes else '') + tr('。')))
    return live


# ---------- 卡片"编辑"：你的说法（③，2b，已定第 27 条 c）----------
NOTE_FIELDS = {'title': 40, 'note': 120, 'recommended': None}   # 能在卡片上改的；数字是最多几个字
NOTE_WORDS = {'title': tr('名称'), 'note': tr('一句话说明'), 'recommended': tr('推荐档位')}


def clean_text(value, most):
    """用户写的一行字：去掉换行和控制字符，不能空，不能太长。"""
    text = re.sub(r'[\x00-\x1f\x7f\u2028\u2029]+', ' ', str(value)).strip()
    if not text: raise RuntimeError(tr('不能是空的；想回到出厂说明就点"恢复默认"。'))
    if len(text) > most: raise RuntimeError(tr('太长了：最多 {0} 个字。', most))
    return text


def notes_path(node): return (MODEL_DATA if is_local(node) else ROOT) / 'model-notes.json'


def remote_file(node, path):
    """读副机上的一个文件：没有这个文件返回 ''；连不上、读不了报错（不能当成"没有"，不然会拿新内容把它盖掉）。"""
    r = ssh_to(node, f'if [ -e "{path}" ]; then cat "{path}"; else echo @@none; fi', timeout=15)
    if r.returncode: raise OSError(tr('读不了 {0} 上的 {1}', where(node), path))
    return '' if r.stdout.strip() == '@@none' else r.stdout


def read_notes(node):
    """那台的③原文；没有就是空的（副机还没有③时，先抄这台③里它也有的模型，免得别的卡片的说法一下子没了）。
    读不了不往下写，免得把你写的别的说法覆盖掉。"""
    path = notes_path(node)
    if is_local(node): text = path.read_text() if path.exists() else ''
    else:
        try: text = remote_file(node, path)
        except OSError as exc: raise RuntimeError(str(exc)) from None
        if not text.strip(): text = json.dumps({'models': {k: v for k, v in HOST_NOTES.items() if k in node_has(node)}}, ensure_ascii=False)
    try: data = json.loads(text) if text.strip() else {'models': {}}
    except ValueError: raise RuntimeError(tr('{0} 的 {1} 格式不对，先修好它再改（没有动它）。', where(node), path)) from None
    if not isinstance(data, dict) or not isinstance(data.setdefault('models', {}), dict): raise RuntimeError(tr('{0} 的 {1} 格式不对，先修好它再改（没有动它）。', where(node), path))
    return data


def write_notes(node, data):
    path = notes_path(node)
    if is_local(node): write_json(path, data)
    else:
        r = ssh_to(node, f'cat > "{path}.tmp" && mv "{path}.tmp" "{path}"', data=json.dumps(data, indent=2, ensure_ascii=False), timeout=15)
        if r.returncode: raise RuntimeError(tr('没能写进 {0}：{1}', where(node), r.stderr.strip()[:200]))
        keep_node_data(node, {**node_data(node), 'notes': data['models']})   # 快速刷新马上看得到


def note_command(node, name, rest, text):
    """note <机器> <模型> set（标准输入是 JSON：title、note、recommended）| reset <字段>… | reset all。"""
    if node not in NODES or name not in MODELS or not rest or rest[0] not in ('set', 'reset'):
        raise RuntimeError(tr('note {0} <模型> set | reset title|note|recommended|all', NODE_CHOICES))
    if is_rpc(node): raise RuntimeError(tr('走 rpc 的机器暂时不能在这里改说法（第 4 期做）。'))
    if not is_local(node) and not reachable_now(node): raise RuntimeError(offline(node))
    model, here = MODELS[name], where(node)
    changes, drops = {}, []
    if rest[0] == 'set':
        try: asked = json.loads(text or '{}')
        except ValueError: raise RuntimeError(tr('要改的内容不是 JSON。')) from None
        if not isinstance(asked, dict) or not asked or set(asked) - set(NOTE_FIELDS): raise RuntimeError(tr('只能改 {0}。', tr('、').join(NOTE_FIELDS)))
        for field, value in asked.items():
            if field == 'recommended':
                if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= model['native']: raise RuntimeError(tr('推荐档位要是 1 到 {0} 之间的整数。', model['native']))
                changes[field] = [value]
            else: changes[field] = clean_text(value, NOTE_FIELDS[field])
    else:
        drops = list(NOTE_FIELDS) if rest[1:] == ['all'] else rest[1:]
        if not drops or set(drops) - set(NOTE_FIELDS): raise RuntimeError(tr('只能恢复 {0}，或 all。', tr('、').join(NOTE_FIELDS)))
    data = read_notes(node)
    entry = data['models'].get(name) if isinstance(data['models'].get(name), dict) else {}
    entry = {**{k: v for k, v in entry.items() if k not in drops}, **changes}
    if entry: data['models'][name] = entry
    else: data['models'].pop(name, None)
    write_notes(node, data)
    words = tr('、').join(NOTE_WORDS[f] for f in (changes or drops))
    print(ok(tr('已保存：{0} 上 {1} 的{2}。', here, model['title'], words) if changes else tr('已恢复默认：{0} 上 {1} 的{2}回到出厂说明。', here, model['title'], words)))


def model_recency(names=None):
    """新模型靠前（用户 11-04 定）：模型事实里的 added（第一次见到的日期），没写的排后面；同一天按原来的顺序。"""
    return sorted(names or MODELS, key=lambda name: MODELS[name].get('added') or '', reverse=True)


def model_meta(name):
    """卡片头的说明（status 的 models）：按当前这台的清单。"""
    m = MODELS[name]
    return {'id': m['id'], 'title': m['title'], 'note': m['note'], 'eta': m['eta'], 'vision': bool(m.get('vision')),
            'version': m.get('version', ''), 'label': model_label(name), 'about': [*m.get('about', []), [tr('模型名'), m['id']]],   # 模型名：客户端里填的就是它（2b）
            'size_gb': gib(model_bytes(name)), 'info': option_info(name), 'warmup': bool(m.get('warmup')), **({'estimated': True} if m.get('kv_guess') else {}),
            # 下面四项只给 10-30 以前生成的控制台应用用（它按模型显示一个共用的上下文），重新生成应用后可删
            'context': node_context(LOCAL, name), 'contexts': CONTEXTS, 'verified': sorted(m['verified']),
            'marks': {str(v): context_mark(name, v) for v in CONTEXTS}}


def node_status(node, meta, s, dsh, percent):
    """status --json 里一台的那一段；在 models_of(node) 里调，模型按这台的清单。"""
    n, probe = s['nodes'][node], s['stats'].get(node, {})
    reachable = probe.get('online') is not False
    info = {'title': meta['title'], 'role': tr(meta['role']), 'kind': 'hub' if is_hub_node(node) else 'worker', 'local': is_local(node), 'reachable': reachable, 'checked': probe.get('online') is not None,
            'service': {'up': 'up', 'starting': 'starting', 'reconnecting': 'reconnecting'}.get(n['health'], 'down'),
            'url': f"http://127.0.0.1:{meta['port']}", 'models': n['models'], 'stats': omlx_stats(node, n),
            'omlx_gb': gib(omlx_max(s, node)) if n['up'] else None, 'time': time.strftime('%H:%M:%S'),
            'contexts': {name: context_info(node, name, n, probe.get('total')) for name in MODELS},
            'options': {name: option_state(node, name, n) for name in MODELS},
            # dsh 里有没有「这台 · 这个模型」：没装 dsh 时为 null
            'dsh': {name: (dsh_provider(node), m['id']) in dsh for name, m in MODELS.items()} if dsh else None,
            'need_gb': {name: gib(model_bytes(name, node, n=n)) for name in MODELS}}
    if not reachable and not is_local(node): info['waiting_unlock'] = ssh_port_open(node)
    if not reachable and not is_local(node) and not info['waiting_unlock']:
        note = address_note(node)
        if note: info['address_note'] = note
    if addresses(node): info.update(addresses=coded(addresses(node)), address=address_now(node) if probe.get('online') is not None or is_local(node) else None)
    if not is_local(node):
        info['wants_up'] = wants_up().get(node) is True
        if 'desktop' in probe: info['desktop'] = probe['desktop']
    info['dsh_title'] = meta.get('dsh_title', meta['title'])
    power = power_state(node, reachable, probe.get('online') is not None)
    if power: info.update(power=power['state'], power_since=power['since'], **({'power_fresh': True} if power.get('fresh') else {}))
    view = memory_view(s, node)
    if view:
        used, now, most = view
        alert, notes = memory_alert(node, used, most, ollama_bytes(s) if is_local(node) else 0,
                                    probe['total'], probe.get('pressure', 1), percent)
        info.update(used_gb=gib(used), omlx_now_gb=gib(now) if now is not None else None, max_gb=gib(most),
                    total_gb=gib(probe['total']), swap=probe['swap'],
                    pressure=probe.get('pressure', 1), line_gb=gib(probe['total'] * percent / 100),
                    gpu=probe.get('gpu'), disk_free_gb=gib(probe['disk_free']) if probe.get('disk_free') else None,
                    cache_gb=round(probe.get('cache', 0) / GIB, 2), **cache_fields(cache_now(node, n, probe), official(node)),
                    cache_options=cache_options(node, n, probe), alert=alert, warn=notes,
                    load_gb=gib(most))  # load_gb 只给 10-30 以前生成的控制台应用画内存条用，重新生成应用后可删
        if active(n['models']):  # 没在跑的模型能不能不卸载别的、一起放；界面据此显示"加载"还是"切换"
            info['fits'] = {name: fits_alongside(name, node, s)[0] for name, st in n['models'].items() if st == 'off'}
    if not is_local(node): info['leftover_procs'] = probe.get('procs', 0)
    holder = (rpc_state(node) or {}).get('lock') if is_rpc(node) else lock_holder() if is_local(node) else None
    if holder: info['lock'] = holder
    if is_rpc(node): info.update(transport='rpc', code_ok=(rpc_state(node) or {}).get('code') in (None, code_hash()))
    return info


ENGINE_WORDS = {'verified': tr('已验证'), 'works': tr('能用，有已知的不足'), 'testing': tr('还在验证'), 'bad': tr('这一版有已知问题'),
                'old': tr('版本太旧'), 'newer': tr('比已验证的新，还没验证，一般能用'), 'unlisted': tr('兼容表里没有这一版，还没验证'),
                'unknown': tr('读不到版本'), 'no_table': tr('读不到兼容表')}
ENGINE_KINDS = {'managed': tr('自管版'), 'official': tr('官方版'), 'none': tr('没装')}
ENGINE_HOW = {'managed': tr('控制台自己管'), 'app': tr('oMLX 应用'), 'brew': 'Homebrew', 'other': tr('别的装法')}


def engine_version(node):
    """正在跑的 oMLX 版本（/api/status 的 version，要密钥，子密钥也行）；服务没开或读不到返回 None。"""
    try: value = api(node, '/api/status', timeout=5).get('version')
    except Exception: return None
    return value if isinstance(value, str) else None


def engine_info(signature=False):
    """这台 oMLX 的事实加兼容表的判断（草案 10.3），不含密钥。自管版只报 kind：不按兼容表提示（你两台不提示升级）。
    版本优先读正在跑的；还没允许读主密钥或服务没开时，退回 App 包里写的版本。signature：要不要现核官方签名（几秒）。"""
    info = dict(ENGINE_HERE)
    info['kind_text'] = ENGINE_KINDS.get(info['kind'], info['kind'])
    if info.get('how'): info['how_text'] = ENGINE_HOW.get(info['how'], info['how'])
    if info['kind'] != 'official': return info
    if signature and info.get('app'): info['signed'] = omlx_adapter.app_signed(info['app'])
    running = engine_version(LOCAL) if key_consent() is True and health(LOCAL) == 'up' else None
    info['version'] = running or info.get('app_version')
    info['version_from'] = 'running' if running else 'app' if info.get('app_version') else None
    code, row = omlx_adapter.verdict(info['version'], compat_now()[0])   # 3b：联网读到、核过签名的更新的那份优先
    info.update(verdict=code, verdict_text=ENGINE_WORDS[code], key={True: 'allowed', False: 'denied', None: 'ask'}[key_consent()])
    if row and isinstance(row.get('note'), dict): info['note'] = pick(row['note'])
    info.update(gateway_info())
    return info


SETUP_SHARE = 0.6   # "开始使用"建议挑多大的模型：总内存的这么多以内，留出系统、上下文和多轮对话的余量


def setup_info(node, engine):
    """第 4 期（草案 11.2）"开始使用"三步，给没装 oMLX 和用官方版的机器；自管版不走这里。只读，数字都来自这一次状态：
    ① oMLX 装好并在运行 ② 这台有至少一个模型 ③ 加载成功过一次（加载完会自动自检，过了才记实测占用）。
    只给代号和数字，文字由界面按语言写。三步都做完 done 为真，界面就把这一块收起来。"""
    kind, up = engine.get('kind'), node.get('service') == 'up'
    has = node.get('has') or []
    loaded = [name for name in has if (node.get('models') or {}).get(name) == 'loaded']
    total = node.get('total_gb')
    steps = [
        {'id': 'engine', 'done': kind == 'official' and up, 'state': 'missing' if kind == 'none' else 'running' if up else 'stopped'},
        {'id': 'model', 'done': bool(has), 'count': len(has), 'need_key': kind == 'official' and up and engine.get('key') != 'allowed',
         'memory_gb': total, 'suggest_gb': int(total * SETUP_SHARE) if total else None, 'china': source_pref() == 'cn'},
        {'id': 'load', 'done': bool(loaded) or bool(saved().get('weights', {}).get(LOCAL)), 'loaded': bool(loaded), 'first': has[0] if has else None},
    ]
    if saved().get('weights', {}).get(LOCAL):   # 加载成功过（记了实测占用）：三步都算做完。不然 oMLX 一停，官方版列不出模型，引导又冒出来说"下载一个模型"
        for step in steps: step['done'] = True
    return {'steps': steps, 'done': all(step['done'] for step in steps), 'next': next((step['id'] for step in steps if not step['done']), None)}


def setup_command(as_json=False):
    """engine setup：这台"开始使用"做到哪一步了（界面用的是状态里的同一份）。"""
    if MANAGED_HERE:
        if as_json: print(json.dumps({'ok': True, 'applies': False}, ensure_ascii=False))
        else: print(off(tr('{0} 是控制台自管的 oMLX，不用走"开始使用"。', where(LOCAL))))
        return
    info = status_json(check_worker=False)['setup']
    if as_json: return print(json.dumps({'ok': True, 'applies': True, **info}, ensure_ascii=False))
    engine, model, load = info['steps']
    words = {'missing': tr('还没装 oMLX：在控制台里点"安装 oMLX…"'), 'stopped': tr('oMLX 装好了，还没启动'), 'running': tr('oMLX 在运行')}
    print((ok if engine['done'] else off)(tr('① {0}', words[engine['state']])))
    if model['done']: print(ok(tr('② 这台有 {0} 个模型', model['count'])))
    elif model['need_key']: print(off(tr('② 还没允许控制台读 oMLX 的主密钥，看不到这台有哪些模型：engine allow-key')))
    else:
        print(off(tr('② 还没有模型：打开 oMLX 的管理页下载一个') + (tr('（这台内存 {0} GB，建议挑 {1} GB 以内的 MLX 格式模型）', model['memory_gb'], model['suggest_gb']) if model['suggest_gb'] else '')
                  + (tr('；下载源选了"中国"，在 oMLX 的下载页里选魔搭') if model['china'] else '')))
    print((ok if load['done'] else off)(tr('③ 加载并自检过一次') if load['done'] else tr('③ 还没加载过模型：加载完会自动自检')))


def model_dirs_now():
    """官方 oMLX 现在的模型目录 (设置里写的, 实际在用的)。global-settings 的回复里有主密钥：只取 model 这一段。"""
    model = api(LOCAL, '/admin/api/global-settings', timeout=10).get('model') or {}
    listed = [d for d in model.get('model_dirs') or [] if isinstance(d, str) and d.strip()]
    return listed, [d for d in model.get('effective_model_dirs') or listed if isinstance(d, str)]


def models_dir_command(rest, assume_yes=False, as_json=False):
    """engine models-dir [show] | add <文件夹> [yes=1]（0.5.1 A11，已定第 31 条 ⑩）：把已有的模型文件夹加成 oMLX 模型目录的后一行。
    第一行（oMLX 下载模型的地方）不动；加的那一行 oMLX 只读不写，这里也不往里试写。oMLX 改模型目录时会卸载全部模型、重新扫一遍，所以有模型在线要先确认。"""
    what = rest[0] if rest else 'show'
    if MANAGED_HERE: raise RuntimeError(tr('{0} 是控制台自管的 oMLX，模型目录由控制台管，不在这里加。', where(LOCAL)))
    if what not in ('show', 'add') or (what == 'add' and len(rest) != 2): raise RuntimeError(tr('engine models-dir show | add <文件夹>'))
    need_service()
    listed, in_use = model_dirs_now()
    if what == 'show':
        if as_json: return print(json.dumps({'ok': True, 'model_dirs': listed, 'active': in_use}, ensure_ascii=False))
        return print('\n'.join(listed) or tr('读不到模型目录。'))
    folder = Path(rest[1]).expanduser()
    if not folder.is_absolute() or not folder.is_dir(): raise RuntimeError(tr('{0} 不是一个文件夹。请选一个放着模型的文件夹。', rest[1]))
    folder = folder.resolve()
    if not listed: raise RuntimeError(tr('读不到 oMLX 现在的模型目录，没有改。'))
    if any(Path(d).expanduser().resolve() == folder for d in listed):
        if as_json: return print(json.dumps({'ok': True, 'added': False, 'model_dirs': listed}, ensure_ascii=False))
        return print(off(tr('{0} 已经在 oMLX 的模型目录里了。', folder)))
    hold_lock(tr('添加模型文件夹'))
    n = node_snapshot(LOCAL, None)
    if 'loading' in n['models'].values(): raise RuntimeError(tr('{0} 有模型正在加载，等加载完再加。', where(LOCAL)))
    online = active(n['models'])
    if online and not (assume_yes or ask(tr('oMLX 改模型目录时会卸载已加载的模型（{0}），正在答的请求会中断；加完要再加载一次。继续吗？[y/N] ',
                                              tr('、').join(MODELS[m]['title'] for m in online)))):
        return print(tr('已取消，保持原样。'))
    before = len(engine_rows(LOCAL))
    api(LOCAL, '/admin/api/global-settings', {'model_dirs': [*listed, str(folder)]}, timeout=180, retry=True)
    listed, in_use = model_dirs_now()
    if not any(Path(d).resolve() == folder for d in in_use):   # oMLX 读不了的文件夹它会悄悄跳过，这里说出来
        raise RuntimeError(tr('已经写进 oMLX 的设置，但它读不了 {0}（多半是权限）：在访达里确认这个账号能打开它，或在 oMLX 管理页的设置里删掉这一行。', folder))
    ENGINE_ROWS.pop(LOCAL, None)
    ENGINE.pop(LOCAL, None)
    found = len(engine_rows(LOCAL)) - before
    note = tr('oMLX 重新扫了一遍，多认出 {0} 个模型', found) if found > 0 else tr('oMLX 重新扫了一遍，这个文件夹里没认出新的模型（要 MLX 格式，一个模型一个子文件夹）')
    if as_json: return print(json.dumps({'ok': True, 'added': True, 'model_dirs': listed, 'found': max(found, 0)}, ensure_ascii=False))
    print(ok(tr('已添加 {0}：{1}。', folder, note) + (tr('原来在线的 {0} 已卸载，要用请再加载。', tr('、').join(MODELS[m]['title'] for m in online if m in MODELS)) if online else '')))


def gateway_conf():
    """ROOT/gateway.json（端口、听哪、TLS、enabled）；没有是 {}。读不懂是 None。"""
    try: conf = json.loads((ROOT / 'gateway.json').read_text())
    except FileNotFoundError: return {}
    except (OSError, ValueError): return None
    return conf if isinstance(conf, dict) else None


def write_gateway_conf(conf):
    """整份写回 ROOT/gateway.json。调用方先读出整份、只改自己那一项：不认识的字段原样留着（0.6 的 networks 等经这一版写一次不会丢，报告 R8）。
    先写临时文件再替换；权限 600（停用名单里是子密钥的 SHA-256，oMLX 管理页能建很短的弱密钥，哈希可能被反推）。"""
    path = ROOT / 'gateway.json'
    tmp = path.with_name(path.name + '.tmp')
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f: f.write(json.dumps(conf, indent=2, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def gateway_port(conf):
    port = (conf or {}).get('port', 8900)
    return port if isinstance(port, int) and not isinstance(port, bool) else None


def port_owner(port):
    """端口上在听的进程，"名字（PID n）"；没有返回 None。"""
    try: out = subprocess.run(['lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-Fpc'], capture_output=True, text=True, errors='replace', timeout=5).stdout.splitlines()
    except (OSError, subprocess.SubprocessError): return None
    pid = next((line[1:] for line in out if line.startswith('p')), None)
    name = next((line[1:] for line in out if line.startswith('c')), None)
    return tr('{0}（PID {1}）', name or '?', pid) if pid else None


def gateway_info():
    """官方版的客户端该连哪：开了入口就连入口（client_port，默认 8900），它只放行已加载的模型；没开时 gateway 是 'off'，
    客户端只能直连 oMLX，没加载的模型会被点起来（契约测试已证实）。已定第 29 条 ②：官方版没表过态就算开（gateway_set 'default'）。
    开了主机模式而这台不是主机：入口在主机上（'elsewhere'），这台不起。"""
    import hub
    setting = hub.gateway_setting()
    # client_urls（0.6 报告 R1）：客户端能填的地址，一行一个 {id, name, url}。0.5.1 只有本机一行；0.6 在这里加局域网、组网软件的地址
    urls = lambda port: {'client_urls': [{'id': 'local', 'name': tr('本机'), 'url': f'http://127.0.0.1:{port}/v1'}]} if port else {}
    if not hub.gateway_enabled(ENGINE_HERE['kind']): return {'gateway': 'off', 'gateway_set': 'off' if setting is False else 'default', **urls(HERE_PORT)}
    shown = {'gateway_set': 'on' if setting else 'default'}
    if not hub.gateway_follows_omlx():
        try: data = hub.load()
        except hub.HubError: data = None
        if data and data.get('hub') != LOCAL: return {'gateway': 'elsewhere', **shown}
    port = gateway_port(gateway_conf())
    out = {'gateway': 'running' if hub.gateway_pid() else 'stopped', **shown, **({'client_port': port} if port else {}), **urls(port)}
    conf = gateway_conf()
    if out['gateway'] == 'running' and port and chosen_networks(conf):   # 0.6：勾了网络才问入口现在开着哪些门（一次本机请求）；没勾时和 0.5.1 一样
        health = gateway_health(conf)
        scheme = 'https' if ((conf or {}).get('tls') or {}).get('cert') else 'http'
        doors = [d for d in (health or {}).get('doors', []) if isinstance(d, dict) and d.get('address')]
        out['client_urls'] += [{'id': f"{d.get('kind')}:{d['address']}", 'name': network_tag(d.get('kind')), 'url': f"{scheme}://{url_host(d['address'])}:{port}/v1"} for d in doors]
        out['networks_open'] = len({d.get('id') for d in doors})
        if health is not None and 'version' not in health: out['old_gateway'] = True
    if out['gateway'] == 'stopped' and port:
        who = port_owner(port)
        if who: out['gateway_note'] = tr('端口 {0} 被别的程序占着：{1}。', port, who)
    return out


def ensure_gateway():
    """官方版、没开主机模式、入口开着：没在跑就起，等它听上端口（最多约 3 秒）。返回 (结果, 说明)，结果是
    None（不归这里管）、'running'（本来就在跑）、'started'、'port_busy'、'failed'。不碰 oMLX。"""
    import hub
    if MANAGED_HERE or ENGINE_HERE['kind'] != 'official' or not hub.gateway_follows_omlx() or not hub.gateway_enabled(ENGINE_HERE['kind']): return None, None
    conf = gateway_conf()
    if conf is None: return 'failed', tr('{0} 读不懂，入口没起。', 'gateway.json')
    port, listen = gateway_port(conf), conf.get('listen', '127.0.0.1')
    host = '127.0.0.1' if listen in ('0.0.0.0', '::', 'localhost') else listen
    pid = hub.gateway_pid()   # 认得出是入口才算（记下的号被别的程序用了不算）
    if pid and gateway_draining(port, host, conf):
        try: os.kill(pid, signal.SIGUSR2)   # 关了又马上开、还有请求没答完：让它接着收新请求
        except OSError: pid = None
        for _ in range(30):
            if not pid or not gateway_draining(port, host, conf): break
            time.sleep(0.1)
        else:
            for _ in range(30):   # 已经在退（撤不回）：等它退完再起新的
                if not hub.gateway_pid(): break
                time.sleep(0.1)
        pid = hub.gateway_pid()
        if pid and gateway_draining(port, host, conf): return 'failed', tr('入口正在等请求答完后退出，稍后再开。')
    if pid and chosen_networks(conf) and swap_old_gateway(only_idle=True) is None and not hub.gateway_pid(): pid = None   # 0.6：勾着网络、还在跑的却是闲着的旧入口——换成新的
    if pid: return 'running', None
    if port and omlx_adapter.listening(port, host):
        return 'port_busy', tr('端口 {0} 被别的程序占着：{1}。', port, port_owner(port) or '?')
    hub.start_gateway(ENGINE_HERE['kind'])
    proc = hub.LAST_GATEWAY[0] if hub.LAST_GATEWAY else None
    for _ in range(40):
        if port and omlx_adapter.listening(port, host): return 'started', None
        if proc and proc.poll() is not None: break
        time.sleep(0.1)
    if proc and proc.poll() is None: return 'started', None   # 还在起，没报错
    hub.GATEWAY_PID.unlink(missing_ok=True)
    try: last = [line for line in (RUN_DIR / 'gateway-control.log').read_text(errors='replace').splitlines() if line.strip()][-1][:200]
    except (OSError, IndexError): last = ''
    return 'failed', last or tr('入口一启动就退出了。')


def gateway_draining(port, host, conf):
    """问入口的 /health（不用密钥）：正在移交返回 True；答不上、不是移交返回 False。"""
    if not port: return False
    try:
        raw = socket.create_connection((host, port), timeout=2)
        if (conf.get('tls') or {}).get('cert'):
            context = ssl.create_default_context()
            context.check_hostname, context.verify_mode = False, ssl.CERT_NONE   # 本机自己的入口，只读一个状态
            raw = context.wrap_socket(raw, server_hostname=host)
        with raw as s:
            s.settimeout(2)
            s.sendall(f'GET /health HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n'.encode())
            data = b''
            while len(data) < 65536:
                chunk = s.recv(4096)
                if not chunk: break
                data += chunk
        body = data.partition(b'\r\n\r\n')[2]
        return json.loads(body or b'{}').get('draining') is True
    except (OSError, ValueError, ssl.SSLError, AttributeError):
        return False


def report_gateway(result, quiet=False):
    """把 ensure_gateway 的结果说给人听：起了、起不来才说；本来就在跑、不归这里管的不说。"""
    state, why = result
    port = gateway_port(gateway_conf())
    if state == 'started' and not quiet: print(ok(tr('入口已启动（端口 {0}）：客户端连这个端口，只能用已加载的模型。', port)))
    elif state in ('port_busy', 'failed'):
        print(warn(tr('入口没起来：{0} 客户端要是直连 oMLX，没加载的模型会被点起来。', why)))


# ---------- 0.6：按网络开门 ----------
SWAP_WAIT = 10   # 换掉旧入口时最多等它几秒把正在答的答完


def url_host(address):
    """地址写进网址里：IPv6 要加方括号。"""
    return f'[{address}]' if ':' in str(address) else str(address)


def network_tag(kind):
    """连接客户端页地址前面的小标签。"""
    return {'lan': tr('局域网'), 'tailscale': 'Tailscale'}.get(kind) or tr('虚拟网络')


def gateway_health(conf=None):
    """问这台入口的 /health（本机那扇门，不用密钥）。答不上返回 None。0.6 的入口多回 version、doors（开着哪些门）、pending、awake；0.5.1 的没有 version。"""
    conf = gateway_conf() if conf is None else conf
    port = gateway_port(conf)
    if not port or conf is None: return None
    listen = conf.get('listen', '127.0.0.1')
    host = '127.0.0.1' if listen in ('0.0.0.0', '::', 'localhost') else listen
    try:
        raw = socket.create_connection((host, port), timeout=2)
        if (conf.get('tls') or {}).get('cert'):
            context = ssl.create_default_context()
            context.check_hostname, context.verify_mode = False, ssl.CERT_NONE   # 本机自己的入口，只读一个状态
            raw = context.wrap_socket(raw, server_hostname=host)
        with raw as s:
            s.settimeout(2)
            s.sendall(f'GET /health HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n'.encode())
            data = b''
            while len(data) < 65536:
                chunk = s.recv(4096)
                if not chunk: break
                data += chunk
        found = json.loads(data.partition(b'\r\n\r\n')[2] or b'{}')
        return found if isinstance(found, dict) and found.get('ok') is True else None
    except (OSError, ValueError, ssl.SSLError, AttributeError):
        return None


def chosen_networks(conf=None):
    """gateway.json 里勾了哪些网络（记号的列表）。没有、读不懂是 []。"""
    conf = gateway_conf() if conf is None else conf
    nets = (conf or {}).get('networks')
    return [n for n in nets if isinstance(n, str)] if isinstance(nets, list) else []


def networks_allowed():
    """这台能不能按网络开门：(能不能, 不能时的原因)。这一版只给单机的官方 oMLX，而且入口要开着——门开在入口上，入口关着就没有门。"""
    import hub
    if MANAGED_HERE or ENGINE_HERE['kind'] != 'official': return False, tr('这一版只有装官方 oMLX 的单机能让别的设备连进来。')
    if not hub.gateway_follows_omlx(): return False, tr('开了主机模式：让别的设备连进来在 1.0 联机里做。')
    if not hub.gateway_enabled(ENGINE_HERE['kind']): return False, tr('入口关着。先在 设置 → oMLX 打开入口：别的设备只能经入口连进来。')
    return True, ''


def network_title(row):
    """列表里这一行叫什么。"""
    if row.get('kind') == 'lan': return tr('家里的网络（{0}）', {'Wi-Fi': 'Wi-Fi', 'Ethernet': tr('网线')}.get(row.get('port'), row.get('port') or tr('网线')))
    if row.get('kind') == 'tailscale': return 'Tailscale'
    return tr('其他虚拟网络（{0}）', row.get('iface') or '?')


def local_hostname():
    """主机名.local：手机上有的软件连不了 IP 地址时的备选。读不到是 ''。环境变量只给测试用。"""
    if 'LMC_FIRST_HOST' in os.environ: name = os.environ['LMC_FIRST_HOST']
    else:
        try: name = subprocess.run(['scutil', '--get', 'LocalHostName'], capture_output=True, text=True, errors='replace', timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError): name = ''
    return f'{name}.local' if re.fullmatch(r'[A-Za-z0-9-]{1,63}', name or '') else ''


def tailscale_name():
    """这台在 Tailscale 里的名字（<机器名>.<网络名>.ts.net）；没装、没登录、读不到是 ''。只读。"""
    if os.environ.get('LMC_ROOT') and 'LMC_TAILSCALE_NAME' in os.environ: return os.environ['LMC_TAILSCALE_NAME']   # 测试给了就用它的，不问真的 Tailscale
    for binary in ('/Applications/Tailscale.app/Contents/MacOS/Tailscale', '/usr/local/bin/tailscale', '/opt/homebrew/bin/tailscale'):
        if not Path(binary).exists(): continue
        try: name = json.loads(subprocess.run([binary, 'status', '--json'], capture_output=True, text=True, errors='replace', timeout=4).stdout)['Self']['DNSName']
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError): continue
        name = str(name).rstrip('.')
        if re.fullmatch(r'[A-Za-z0-9.-]{1,253}', name): return name
    return ''


def address_from_router(iface):
    """这张网卡的地址是不是路由器分配的（DHCP）：是的话过一阵可能变，提示在路由器里固定。读不出来当不是。"""
    if os.environ.get('LMC_ROOT') and 'LMC_DHCP_IFACES' in os.environ: return os.environ['LMC_DHCP_IFACES'].split(',').count(iface) > 0   # 测试给了就用它的，不问系统
    try: out = subprocess.run(['/usr/sbin/ipconfig', 'getpacket', iface], capture_output=True, text=True, errors='replace', timeout=4).stdout
    except (OSError, subprocess.SubprocessError): return False
    return 'yiaddr' in out


def networks_view():
    """engine networks show：这台现在连着哪些网络、勾了哪些、哪些门真的开着。打开设置页、连接客户端页时才查（要读网卡），不进 10 秒一次的刷新。"""
    import netscan
    conf = gateway_conf()
    allowed, why = networks_allowed()
    chosen = chosen_networks(conf)
    names = (conf or {}).get('network_names') if isinstance((conf or {}).get('network_names'), dict) else {}
    health = gateway_health(conf) if chosen and allowed else None
    doors = {d.get('address') for d in (health or {}).get('doors', []) if isinstance(d, dict)}
    port, scheme = gateway_port(conf) or 8900, 'https' if ((conf or {}).get('tls') or {}).get('cert') else 'http'
    host, rows, seen = None, [], set()
    for row in netscan.scan():
        seen.add(row.get('id'))
        item = {'id': row.get('id'), 'kind': row['kind'], 'name': network_title(row), 'iface': row.get('iface', ''), 'address': row['address'], 'subnet': row.get('subnet', ''), 'ready': bool(row.get('ready')),
                'on': bool(row.get('id')) and row.get('id') in chosen, 'open': row['address'] in doors, 'url': f"{scheme}://{url_host(row['address'])}:{port}/v1"}
        if row['kind'] == 'lan':
            if host is None: host = local_hostname()
            if host: item['host_url'] = f'{scheme}://{host}:{port}/v1'
            if address_from_router(row.get('iface', '')): item['dhcp'] = True
        elif row['kind'] == 'tailscale':
            name = tailscale_name()
            if name: item['host'], item['host_url'] = name, f'{scheme}://{name}:{port}/v1'
        if not item['ready']: item['note'] = tr('还在认这个网络的路由器，过几秒再看。')
        else: item['warning'] = network_warning(item)   # 勾之前要说清的话：界面的确认框和命令行用同一段
        rows.append(item)
    for ident in chosen:   # 勾过、现在没连着的：留一行，连上后自动打开，也能取消
        if ident in seen or not re.fullmatch(r'(lan:[0-9a-f]{12}|tailscale|virtual:[0-9./]{9,18})', ident): continue
        kind = ident.split(':', 1)[0]
        rows.append({'id': ident, 'kind': kind, 'name': str(names.get(ident) or ('Tailscale' if kind == 'tailscale' else tr('勾过的网络'))), 'iface': '', 'address': '', 'ready': False,
                     'on': True, 'open': False, 'absent': True, 'note': tr('现在没连着，连上后自动打开。')})
    out = {'ok': True, 'can': allowed, 'networks': rows, 'port': port, 'keep_awake': (conf or {}).get('keep_awake') is True, 'limits': gateway_limits(conf)}
    if not allowed: out['why'] = why
    if health is not None and 'version' not in health: out['old_gateway'] = True
    if chosen and allowed and health is None: out['why_closed'] = tr('入口没在运行：勾着的网络现在都没开门。')
    return out


def gateway_limits(conf=None):
    """限流的数：gateway.json 的 limits，没写、写错的用入口的默认。"""
    import gateway_defaults
    conf = gateway_conf() if conf is None else conf
    return gateway_defaults.numbers((conf or {}).get('limits'), {k: gateway_defaults.LIMITS[k] for k in ('concurrent', 'per_minute', 'total')})


def swap_old_gateway(only_idle=False):
    """升级衔接：入口是独立的常驻进程，应用从 0.5.1 升上来以后，旧入口可能还在跑（它不认 networks，收到 HUP 还会退出）。
    认出是旧的就让它把正在答的答完、退出（新的由调用方起）。only_idle：它正在答请求就先不动。
    返回 None（不是旧的，或已经退了）或一句没换成的原因；没换成时让旧入口接着收请求，本机的客户端不受影响。"""
    import hub
    health = gateway_health()
    if health is None or 'version' in health: return None
    busy = tr('旧版本的入口还在答一个请求，换不了新的。等它答完再勾一次。')
    if only_idle and health.get('active'): return busy
    pid = hub.gateway_pid()
    if not pid or not hub.stop_gateway(drain=True): return None
    for _ in range(SWAP_WAIT * 10):
        if not hub.gateway_pid(): return None
        time.sleep(0.1)
    try: os.kill(pid, signal.SIGUSR2)   # 等了 SWAP_WAIT 秒还没答完：不换了，让它接着收新请求
    except OSError: return None
    return busy


def apply_networks(conf, wait_for=None):
    """写回 gateway.json、叫入口重新对门，等它对好（最多约 5 秒）。wait_for：等这个记号的门开（True）或关（False）。"""
    import hub
    write_gateway_conf(conf)
    problem = swap_old_gateway()
    if problem: return problem
    if not hub.gateway_pid():
        state, note = ensure_gateway()
        if state not in ('started', 'running'): return tr('入口没起来：{0}', note or tr('入口关着'))
    elif 'version' in (gateway_health(conf) or {}): hub.reload_gateway()   # 认得出是 0.6 的入口才发 HUP（0.5.1 的收到会退出）
    if wait_for is None: return None
    ident, want_open = wait_for
    for _ in range(50):
        health = gateway_health(conf) or {}
        is_open = any(isinstance(d, dict) and d.get('id') == ident for d in health.get('doors', []))
        if is_open == want_open: return None
        time.sleep(0.1)
    return tr('已经记下，但入口还没在这个网络上开门：过几秒再看一眼。') if want_open else tr('已经取消，入口正在关门。')


def networks_command(rest):
    """engine networks show | on <网络> yes=1 | off <网络或 all> | awake on|off。<网络> 是 show 里的记号（lan:…、tailscale、virtual:…）或它的序号（从 1 起）。"""
    what, sure = (rest[0] if rest else 'show'), rest[-1:] == ['yes=1']
    args = [a for a in rest[1:] if a != 'yes=1']
    if what == 'show': return networks_view()
    if what not in ('on', 'off', 'awake') or len(args) != 1: raise RuntimeError(tr('engine networks show | on <网络> yes=1 | off <网络|all> | awake on|off'))
    conf = gateway_conf()
    if conf is None: raise RuntimeError(tr('{0} 读不懂，没改。请先看一眼这个文件。', ROOT / 'gateway.json'))
    chosen = chosen_networks(conf)
    if what == 'awake':
        if args[0] not in ('on', 'off'): raise RuntimeError('engine networks awake on|off')
        if args[0] == 'on': conf['keep_awake'] = True
        else: conf.pop('keep_awake', None)
        problem = apply_networks(conf)
        return {**networks_view(), 'note': problem or (tr('已打开：有网络开着门、又接着电源时，这台不会自己睡眠（合上笔记本的盖子照样会睡）。') if args[0] == 'on' else tr('已关闭：这台照系统设置睡眠。'))}
    view = networks_view()
    target = args[0]
    if target.isdigit() and 1 <= int(target) <= len(view['networks']): target = view['networks'][int(target) - 1]['id'] or ''
    if what == 'off':
        gone = chosen if target == 'all' else [target]
        if target != 'all' and target not in chosen: raise RuntimeError(tr('没有勾过这个网络：{0}', args[0]))
        left = [n for n in chosen if n not in gone]
        if left: conf['networks'] = left
        else: conf.pop('networks', None)
        names = {k: v for k, v in (conf.get('network_names') or {}).items() if k in left} if isinstance(conf.get('network_names'), dict) else {}
        if names: conf['network_names'] = names
        else: conf.pop('network_names', None)
        problem = apply_networks(conf, (gone[0], False) if len(gone) == 1 else None) if hub_gateway_running() else (write_gateway_conf(conf) or None)
        return {**networks_view(), 'note': problem or tr('已取消：入口不再在这个网络上收新的连接，正在答的那一路会答完。')}
    allowed, why = networks_allowed()
    if not allowed: raise RuntimeError(why)
    row = next((r for r in view['networks'] if r['id'] == target and not r.get('absent')), None)
    if row is None: raise RuntimeError(tr('这台现在没有连着这个网络：{0}。用 engine networks show 看现在有哪些。', args[0]))
    if not sure:
        raise RuntimeError(network_warning(row) + tr(' 确认要开，在命令最后加 yes=1。'))
    if target not in chosen: chosen.append(target)
    conf['networks'] = chosen
    names = dict(conf.get('network_names')) if isinstance(conf.get('network_names'), dict) else {}
    names[target] = f"{row['name']} {row.get('subnet', '')}".strip()
    conf['network_names'] = names
    problem = apply_networks(conf, (target, True))
    return {**networks_view(), 'note': problem or tr('已打开：别的设备现在可以连 {0}，每台设备用它自己的子密钥。', row['url'])}


def hub_gateway_running():
    import hub
    return bool(hub.gateway_pid())


def network_warning(row):
    """勾一个网络前要说清的话（界面上用同一段）。"""
    if row['kind'] == 'lan':
        return tr('会在 {0} 上打开端口 {1}。这个网络里不加密：同一个网络里的人可能看到密钥和对话，所以只在自己的家庭、办公室网络里开；每台设备用它自己的子密钥；要在外面用、或者要加密，请装组网软件（Tailscale、蒲公英、EasyTier 等）再勾它的网络。',
                  row['address'], gateway_port(gateway_conf()) or 8900)
    return tr('会在 {0} 上打开端口 {1}。这个虚拟网络里的每台设备都连得到入口（仍然要子密钥）：别和外人共用这个网络，网络的密码设复杂一些。', row['address'], gateway_port(gateway_conf()) or 8900)


def limits_command(rest):
    """engine gateway limits [concurrent=2] [per_minute=60] [total=6] | limits reset：外来请求的限流。只改写了的那几项；入口立刻生效，不用重启。"""
    import gateway_defaults
    conf = gateway_conf()
    if conf is None: raise RuntimeError(tr('{0} 读不懂，没改。请先看一眼这个文件。', ROOT / 'gateway.json'))
    if rest == ['reset']:
        conf.pop('limits', None)
        write_gateway_conf(conf)
    elif rest:
        opts = opts_of(rest)
        bad = [part for part in rest if '=' not in part] + [k for k in opts if k not in ('concurrent', 'per_minute', 'total')]
        if bad: raise RuntimeError('engine gateway limits [concurrent=N] [per_minute=N] [total=N] | limits reset')
        current = dict(conf.get('limits')) if isinstance(conf.get('limits'), dict) else {}
        for key, value in opts.items():
            if not value.isdigit() or not 1 <= int(value) <= 100000: raise RuntimeError(tr('{0} 要是 1 到 100000 的整数。', key))
            current[key] = int(value)
        conf['limits'] = current
        write_gateway_conf(conf)
    lim = gateway_limits(conf)
    return {'ok': True, 'limits': lim, 'defaults': {k: gateway_defaults.LIMITS[k] for k in lim},
            'note': tr('别的设备来的请求：每把子密钥同时 {0} 个（多的排队）、每分钟 {1} 个；所有外来设备合计同时 {2} 个。本机来的不限。', lim['concurrent'], lim['per_minute'], lim['total'])}


def key_last_used():
    """每把子密钥最近一次经入口调用：{名字: {'at', 'from', 'via'}}。从入口日志的尾部读（日志里本来就有名字和来源），不另存东西。
    名字里带空格的（oMLX 管理页建的）日志里拆不出来，就没有这一项。"""
    found = {}
    for name in ('gateway.log.old', 'gateway.log'):
        try:
            with (RUN_DIR / name).open('rb') as f:
                f.seek(0, 2)
                start = max(0, f.tell() - 512 * 1024)
                f.seek(start)
                lines = f.read().decode('utf-8', 'replace').splitlines()[1 if start else 0:]   # 从中间读起时第一行是半截的，不要；文件小、从头读时第一行是完整的
        except OSError: continue
        for line in lines:
            hit = re.match(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (\S+)@([0-9A-Fa-f:.]+)(?:\[(\w+)\])? \S+ .* \d{3} ', line)
            if hit: found[hit.group(2)] = {'at': hit.group(1), 'from': hit.group(3), 'via': hit.group(4) or 'local'}
    return found


def gateway_on_hub_here():
    """开了主机模式、这台就是主机。"""
    import hub
    try: data = hub.load()
    except hub.HubError: return False
    return bool(data) and data.get('hub') == LOCAL


def gateway_command(what):
    """engine gateway show | on | off | ensure：官方版的入口开关（已定第 29 条 ②）。写 ROOT/gateway.json 的 enabled，其余几项原样留着。"""
    import hub
    if what not in ('show', 'on', 'off', 'ensure'): raise RuntimeError('engine gateway show | on | off | ensure')
    if MANAGED_HERE and what != 'show':
        raise RuntimeError(tr('{0} 是控制台自管的 oMLX：子密钥本来就加载不了模型，入口只跟着主机模式走，这里不用开关。', where(LOCAL)))
    note, result = None, (None, None)
    if what in ('on', 'off'):
        conf = gateway_conf()
        if conf is None: raise RuntimeError(tr('{0} 读不懂，没改。请先看一眼这个文件。', ROOT / 'gateway.json'))
        mine = hub.gateway_follows_omlx() or gateway_on_hub_here()
        if not mine: return {'ok': True, **gateway_info(), 'note': tr('开了主机模式：入口只在主机上跑，这台的开关不起作用。')}
        conf['enabled'] = what == 'on'
        write_gateway_conf(conf)
        if what == 'off':
            if mine: hub.stop_gateway(drain=True)   # 正在答的答完再停
            note = tr('入口已关。客户端要改连 oMLX 的端口 {0}；这样没加载的模型会被点起来，官方版的子密钥也能加载模型。', HERE_PORT) if mine \
                else tr('开了主机模式：入口只在主机上跑，这台的开关不起作用。')
            if mine and chosen_networks(conf): note += tr('对别的设备开的门也一起关了（勾选还记着，再开入口时自动恢复）。')
        elif hub.gateway_follows_omlx(): result = ensure_gateway()
        elif mine: hub.start_gateway(ENGINE_HERE['kind'])
        else: note = tr('开了主机模式：入口只在主机上跑，这台的开关不起作用。')
    elif what == 'ensure': result = ensure_gateway()
    if result[0] in ('port_busy', 'failed'): note = tr('入口没起来：{0}', result[1])
    return {'ok': True, **gateway_info(), **({'result': result[0]} if result[0] else {}), 'note': note}


def engine_command(rest, as_json=False):
    """engine [show]：这台的 oMLX 是哪种、在哪、版本落在兼容表哪一档；engine allow-key | deny-key：允不允许控制台读官方版的主密钥（只存态度）。"""
    what = rest[0] if rest else 'show'
    if what == 'setup': return setup_command(as_json)   # 第 4 期："开始使用"做到哪一步了
    if what == 'show':
        info = engine_info(signature=True)
        if as_json: return print(json.dumps({'ok': True, **info}, ensure_ascii=False))
        print(tr('{0} 的 oMLX：{1}', where(LOCAL), info['kind_text']) + (tr('（{0}）', info['how_text']) if info.get('how_text') else ''))
        if info.get('base'): print(tr('数据目录：{0}', info['base']))
        if info.get('port'): print(tr('端口：{0}', info['port']))
        if info['kind'] == 'official':
            if info.get('app'): print(tr('应用：{0}（{1}）', info['app'], {True: tr('官方签名'), False: tr('签名不对'), None: tr('签名核对不了')}[info.get('signed')]))
            print(tr('版本：{0}，{1}', info.get('version') or tr('读不到'), info['verdict_text']) + (tr('：{0}', info['note']) if info.get('note') else ''))
            print(tr('主密钥：{0}', {'allowed': tr('已允许控制台读'), 'denied': tr('没允许，管理功能用不了'), 'ask': tr('还没问过')}[info['key']]))
        return
    if what in ('allow-key', 'deny-key'):
        if MANAGED_HERE: raise RuntimeError(tr('{0} 是自管版，不用授权：控制台本来就用这台的主密钥。', where(LOCAL)))
        save_setting('engine_key', what == 'allow-key')
        return print(ok(tr('已允许控制台读 {0} 的 oMLX 主密钥：每次用时现读，不存副本。', where(LOCAL)) if what == 'allow-key'
                        else tr('已记下不允许：控制台只看状态，加载、卸载、子密钥这些管理功能不能用。')))
    if what == 'models-dir':   # 文件夹路径里可能有等号，不走 opts_of；已经确认过的在最后带 yes=1（不加新的 --选项，理由见 opts_of）
        more = rest[1:]
        sure = more[-1:] == ['yes=1']
        return models_dir_command(more[:-1] if sure else more, sure, as_json)
    extra = engine_extra(what, rest[1:])
    if extra is not None: return print(json.dumps(extra, ensure_ascii=False, **({} if as_json else {'indent': 1})))
    raise RuntimeError('engine show | allow-key | deny-key | install-plan | download | check-file | open-package | autoupdate | source | gateway | networks | models-dir | backup-settings | selfcheck')


# ── 第 3b 期：安装、下载源、升级、更新中心（草案 10.5–10.8，接口见 p3b/CONTRACT.md） ──
# 只管这台。你两台（自管版）：安装界面不出现，更新页 oMLX 一行说"自管版，不提示升级"，没配置更新地址时什么都不读、什么都不写。
DOWNLOADS = ROOT / 'downloads'
UPDATES_DIR = ROOT / 'console-runtime' / 'updates'
PREFS_BACKUP = ROOT / 'console-runtime' / 'omlx-update-prefs.backup.json'
SETTINGS_BACKUPS = ROOT / 'backups' / 'omlx-settings'
SOURCE_NAMES = {'github': 'GitHub', 'modelscope': tr('魔搭')}
SOURCE_PREFS = {'auto': tr('自动'), 'intl': tr('国际'), 'cn': tr('中国')}
DEFAULT_CN_HF = 'https://hf-mirror.com'
USABLE = ('verified', 'works', 'testing')


def opts_of(rest):
    """位置参数里的 key=value（不加新的 --选项：会改掉参数报错时打印的用法）。"""
    return dict(part.split('=', 1) for part in rest if '=' in part)


def update_key():
    """公钥只认 brand.py；那里空着时（还没生成钥匙）才认测试用的环境变量 LMC_UPDATE_PUBKEY，正式版写了公钥就不受环境变量影响。"""
    return brand.UPDATE_PUBLIC_KEY or os.environ.get('LMC_UPDATE_PUBKEY') or ''


def usable_url(url):
    """更新地址：https 的随便哪台；http 只认本机（测试、自建镜像）。按解析后的主机名判断，不按前缀（http://localhost.example.net 这种不算本机）。"""
    try: parts = urllib.parse.urlsplit(url) if isinstance(url, str) else None
    except ValueError: parts = None
    if not parts or not parts.hostname or parts.username or parts.password: return ''
    if parts.scheme == 'https' or (parts.scheme == 'http' and parts.hostname in ('127.0.0.1', 'localhost', '::1')): return url
    return ''


def update_url():
    return usable_url(saved().get('update_url') or brand.UPDATE_URL)


def update_urls():
    """按顺序试的更新地址（0.5.1）：设置里写了 update_url 就只用它（测试、自建镜像）；不然是随程序写的主地址和魔搭上的那一份，
    下载源选"中国"时魔搭在前，选"国际"时只用主地址。清单签了名，哪个地址读到的都一样核。"""
    if saved().get('update_url'): return [u for u in [update_url()] if u]
    main, cn = usable_url(brand.UPDATE_URL), usable_url(getattr(brand, 'UPDATE_URL_CN', ''))
    order = {'cn': [cn, main], 'intl': [main]}.get(source_pref(), [main, cn])
    return [u for u in dict.fromkeys(order) if u]


def manifest_now():
    key = update_key()
    return updates.cached(UPDATES_DIR, key, saved().get('updates_issued')) if key else None


def compat_now():
    """在用的兼容表：联网读到、核过签名、结构对、比随程序发的新的那份，否则随程序发的。返回 (表, 'online'|'bundled')。
    出什么错都退回随程序发的，绝不让状态因为一份清单崩掉。"""
    try: return updates.compat_in_use(omlx_adapter.load_compat(), manifest_now())
    except Exception: return updates.compat_in_use(omlx_adapter.load_compat(), None)


def macos_version():
    return platform.mac_ver()[0] or None


def console_version():
    """控制台自己的版本：应用启动后端时传 LMC_CONSOLE_VERSION；终端里跑时读仓库根的 VERSION。"""
    value = os.environ.get('LMC_CONSOLE_VERSION')
    if not value:
        try: value = (PROJECT.parent / 'VERSION').read_text().strip()
        except OSError: value = None
    return value or None


def mb_text(size):
    return tr('{0} GB', f'{size / 2**30:.1f}') if size >= 10 * 2**30 else tr('{0} MB', round(size / 2**20))


def state_word(state): return ENGINE_WORDS.get(state, state)


def package_row(row, pkg):
    return {'version': row['version'], 'state': row.get('state'), 'state_text': state_word(row.get('state')), 'file': pkg['file'],
            'size': pkg['size'], 'size_text': mb_text(pkg['size']), 'sha256': pkg['sha256'], 'only_tested_on': pkg.get('only_tested_on')}


def mirror_base(table):
    value = ((table or {}).get('sources') or {}).get('modelscope_base')
    return value if isinstance(value, str) and value.startswith('https://') else None


def verified_mark(path):
    """下载、核对过的安装包旁边放一个记号（大小、修改时间、SHA-256），install-plan 看它就知道不用重算。"""
    return path.with_name(path.name + '.verified')


def mark_verified(path, sha256):
    """只给下载目录里的文件放记号（用户选的文件可能在只读的地方，也不在别人的文件夹里留杂文件）；写不了就算了。"""
    path = Path(path)
    if path.parent.resolve() != DOWNLOADS.resolve(): return
    try:
        st = path.stat()
        verified_mark(path).write_text(json.dumps({'size': st.st_size, 'mtime': st.st_mtime, 'sha256': sha256}))
    except OSError:
        pass


def can_install():
    """这台能不能经控制台装、升级官方 oMLX：没装的能装；官方 App 装的能升级。自管版（你两台）、Homebrew 和别的装法不行。"""
    if MANAGED_HERE: raise RuntimeError(tr('{0} 是自管版：oMLX 由控制台自己管，不装官方版。', where(LOCAL)))
    if ENGINE_HERE['kind'] == 'official' and ENGINE_HERE.get('how') != 'app':
        raise RuntimeError(tr('这台的 oMLX 是 {0} 装的，请用它自己的方式升级（Homebrew：brew upgrade omlx）。', ENGINE_HOW.get(ENGINE_HERE.get('how'), '?')))


def already_downloaded(pkg):
    path = DOWNLOADS / pkg['file']
    try:
        mark, st = json.loads(verified_mark(path).read_text()), path.stat()
    except (OSError, ValueError):
        return None
    if not isinstance(mark, dict): return None   # 记号文件被写成别的东西：当没核过
    if mark.get('sha256') == pkg['sha256'].lower() and mark.get('size') == st.st_size == pkg['size'] and mark.get('mtime') == st.st_mtime: return path
    return None


def emit(tag, data):
    print(f'@@{tag} ' + json.dumps(data, ensure_ascii=False), flush=True)


def progress_printer():
    """@@progress 每秒最多两行，最后一行一定打；速度按两次之间算（接着下的第一行不算速度）。"""
    last = {}
    def show(done, total):
        now = time.monotonic()
        if not last:
            last.update(at=now, done=done, shown=now)
            return emit('progress', {'done': done, 'total': total, 'speed': 0})
        if done < total and now - last['shown'] < 0.5: return
        speed = (done - last['done']) / max(now - last['at'], 1e-3)
        last.update(at=now, done=done, shown=now)
        emit('progress', {'done': done, 'total': total, 'speed': int(speed)})
    return show


def failure_text(exc):
    """updates.Failure → 给人看的一句话。"""
    info = exc.info
    return {
        'network': tr('连不上（{0}）', info.get('detail') or '?'),
        'bad_signature': tr('签名对不上，没用它（文件可能被改过）'),
        'bad_manifest': tr('文件格式不对，没用它'),
        'rollback': tr('拿到的比手上的旧，没用它（可能是镜像没更新）'),
        'too_big': tr('文件太大，没用它'),
        'no_source': tr('哪个下载源都连不上。请换个网络、手选来源，或者用"我已经下载好了"。'),
        'size': tr('下载的文件比兼容表里写的大，已删掉。'),
        'incomplete': tr('没下完（下了 {0}），下次接着下。', mb_text(info.get('done') or 0)),
        'sha256': tr('下载的文件和兼容表对不上（SHA-256 不同），已删掉。请换个下载源再试。'),
        'no_backup': tr('没有关之前的备份，没法还原。'),
        'bad_backup': tr('关之前的备份坏了，没法还原；可以在 oMLX 自己的设置里打开自动更新。'),
        'busy': tr('另一个下载正在下这个安装包，等它完。'),
    }.get(exc.code, exc.code)


def install_plan(version=None):
    """没装时三条路要用的事实；version 给了（升级时）就按那一版。"""
    table, _ = compat_now()
    macos = macos_version()
    system = omlx_adapter.system_slot(macos)
    row, pkg = updates.install_package(table, system, version) if system and table else (None, None)
    reason = None
    if table is None: reason = tr('读不到兼容表，不知道该装哪一版。')
    elif not system: reason = tr('这台是 macOS {0}，官方 oMLX 要 macOS 15 起。', macos or tr('读不到'))
    elif not pkg: reason = tr('兼容表里没有 {0} 给这台系统的安装包。', version) if version else tr('兼容表里没有能装在这台的版本。')
    have = already_downloaded(pkg) if pkg else None
    brew_here = any(Path(p).exists() for p in ('/opt/homebrew/bin/brew', '/usr/local/bin/brew'))
    return {'ok': True, 'kind': ENGINE_HERE['kind'], 'macos': macos, 'system': system,
            'target': package_row(row, pkg) if pkg else None, 'reason': reason,
            'sources': [{'id': s, 'name': SOURCE_NAMES[s]} for s, _ in updates.package_sources(pkg, mirror_base(table))] if pkg else [],
            'source_pref': source_pref(),
            'brew': {'installed': brew_here, 'commands': ['brew tap jundot/omlx https://github.com/jundot/omlx', 'brew install jundot/omlx/omlx'],
                     'start': 'brew services start omlx',
                     'note': tr('Homebrew 版要从源码编译，要等一阵；它不带魔搭下载，在中国下载模型请用安装包版。')},
            'downloaded': {'path': str(have), 'version': row['version']} if have else None,
            'apps_dir': '/Applications'}


def engine_download(opts):
    """下载这台该装的（或 version= 指定的）官方安装包，按兼容表核对大小和 SHA-256。"""
    table, _ = compat_now()
    system = omlx_adapter.system_slot(macos_version())
    row, pkg = updates.install_package(table, system, opts.get('version')) if system and table else (None, None)
    can_install()
    if not pkg: raise RuntimeError(tr('兼容表里没有 {0} 给这台系统的安装包。', opts.get('version') or tr('能装的版本')))
    code, _ = omlx_adapter.verdict(row['version'], table)   # 按 verdict 判：列在 bad 里的也不下
    if code not in USABLE: raise RuntimeError(tr('{0} 在兼容表里标的是"{1}"，不下载。', row['version'], state_word(code)))
    candidates = updates.package_sources(pkg, mirror_base(table))
    pref = opts.get('source') or source_pref()
    if pref in SOURCE_NAMES:
        candidates = [c for c in candidates if c[0] == pref]
        if not candidates: raise RuntimeError(tr('{0} 上还没有这个安装包。', SOURCE_NAMES[pref]))
        pref = 'auto'
    try:
        name, url, speeds = updates.choose_source(candidates, pref)
    except updates.Failure as exc:
        raise RuntimeError(failure_text(exc))
    why = tr('、').join(tr('{0} {1}', SOURCE_NAMES[k], tr('{0} MB/s', f'{v / 2**20:.1f}') if v else tr('连不上')) for k, v in speeds.items())
    emit('source', {'id': name, 'name': SOURCE_NAMES[name], 'why': tr('测速：{0}', why) if speeds else None})
    dest = DOWNLOADS / pkg['file']
    try:
        how = updates.download(url, dest, pkg['size'], pkg['sha256'], progress_printer())
    except updates.Failure as exc:
        raise RuntimeError(failure_text(exc))
    mark_verified(dest, pkg['sha256'].lower())
    return {'ok': True, 'path': str(dest), 'version': row['version'], 'resumed': how == 'resumed', 'cached': how == 'cached'}


def check_package(path):
    """核对一个安装包：算 SHA-256，按兼容表认是哪一版、给哪个系统、能不能装。"""
    path = Path(path).expanduser()
    if not path.is_file(): raise RuntimeError(tr('找不到这个文件：{0}', path))
    table, _ = compat_now()
    macos = macos_version()
    system = omlx_adapter.system_slot(macos)
    target = updates.best_target(table, system)
    want = target['version'] if target else '?'
    size = path.stat().st_size
    if not any(pkg.get('size') == size for row in (table or {}).get('versions', []) for pkg in row.get('packages', [])):
        return {'ok': True, 'match': False, 'usable': False, 'version': None, 'path': str(path),
                'message': tr('这个文件不在兼容表里（{0}，大小和哪个安装包都对不上）。控制台适配的是 {1}。', mb_text(size), want)}
    sha = updates.file_sha256(path, progress_printer())
    row, pkg = omlx_adapter.package(table, sha)
    if not pkg:
        return {'ok': True, 'match': False, 'usable': False, 'version': None, 'path': str(path),
                'message': tr('这个文件不在兼容表里（{0}，SHA-256 开头 {1}）。控制台适配的是 {2}。', mb_text(path.stat().st_size), sha[:12], want)}
    code, _ = omlx_adapter.verdict(row['version'], table)
    fits = pkg.get('system') == system
    usable = fits and code in USABLE
    message = None
    if not fits: message = tr('这是给 macOS {0} 的安装包，这台是 macOS {1}。', '15' if pkg.get('system') == 'macos15' else '26', macos or '?')
    elif not usable: message = tr('这是 {0}，控制台适配的是 {1}。', row['version'], want)
    if usable: mark_verified(path, sha)
    return {'ok': True, 'match': True, 'usable': usable, 'version': row['version'], 'state': code, 'state_text': state_word(code),
            'system': pkg.get('system'), 'fits_system': fits, 'path': str(path), 'message': message}


def open_package(path, upgrade=False):
    """核过再打开。先把文件复制（APFS 上是秒级的克隆，不占地方）到只有自己能进的目录、换个随机名，核这一份、打开这一份：
    核完到打开之间，原来那个文件被换掉也没关系。上一次留下的副本先删掉。"""
    can_install()
    source = Path(path).expanduser()
    if not source.is_file(): raise RuntimeError(tr('找不到这个文件：{0}', source))
    staging = DOWNLOADS / '.opened'
    staging.mkdir(parents=True, exist_ok=True)
    os.chmod(staging, 0o700)
    for old in staging.glob('*.dmg'):
        with contextlib.suppress(OSError): old.unlink()
    copy = staging / f'{secrets.token_hex(8)}.dmg'
    if subprocess.run(['/bin/cp', '-c', str(source), str(copy)], capture_output=True).returncode != 0:
        shutil.copyfile(source, copy)   # 不在同一个 APFS 卷上：老实复制
    info = check_package(copy)
    if not info['usable']:
        copy.unlink()
        raise RuntimeError(info['message'] or tr('这个安装包不能装。'))
    if subprocess.run([omlx_adapter.OPEN, str(copy)], capture_output=True).returncode != 0:
        raise RuntimeError(tr('打不开安装包：请在访达里双击它。'))
    steps = ([tr('先在菜单栏的 oMLX 图标里选"Quit oMLX"，让它真退出（关窗口不算）。'),
              tr('在弹出的窗口里，把新的 oMLX 拖到"应用程序"文件夹，选"替换"。'),
              tr('从"应用程序"里打开 oMLX，再回到这里点"我换好了"。')] if upgrade else
             [tr('在弹出的窗口里，把 oMLX 拖到"应用程序"文件夹。'),
              tr('从"应用程序"里打开 oMLX；第一次打开时 macOS 会问要不要打开，选"打开"。'),
              tr('回到控制台：装好以后这里会自动认出来。')])
    return {'ok': True, 'version': info['version'], 'steps': steps}


def prefs_path(): return omlx_adapter.app_support(Path.home()) / updates.PREFS_FILE


def omlx_app_running():
    app = ENGINE_HERE.get('app')
    return bool(app) and subprocess.run(['pgrep', '-f', f'{app}/Contents/MacOS/oMLX'], capture_output=True).returncode == 0


def autoupdate(what):
    """官方 App 自己的自动更新（10.5）：它默认后台下约 900 MB 的新安装包，不经控制台核对。off 关掉检查和后台下载，restore 换回。"""
    if ENGINE_HERE['kind'] != 'official' or ENGINE_HERE.get('how') != 'app':
        return {'ok': True, 'applies': False}
    note = None
    if what == 'off':
        updates.prefs_off(prefs_path(), PREFS_BACKUP)
    elif what == 'restore':
        try: updates.prefs_restore(prefs_path(), PREFS_BACKUP)
        except updates.Failure as exc: raise RuntimeError(failure_text(exc))
    elif what != 'show':
        raise RuntimeError('engine autoupdate show | off | restore')
    running = omlx_app_running()
    if running and what != 'show': note = tr('oMLX 开着：它下次打开时才按新的设置。')
    state = updates.prefs_state(prefs_path())
    good = updates.backup_ok(PREFS_BACKUP)
    return {'ok': True, 'applies': True, **state, 'ours': good and not state['autoCheck'] and not state['backgroundDownload'],
            'can_restore': good, 'app_running': running, 'note': note}


def source_pref():
    value = saved().get('download_source')
    return value if value in SOURCE_PREFS else 'auto'


def official_ready():
    return ENGINE_HERE['kind'] == 'official' and key_consent() is True and health(LOCAL) == 'up'


def hf_endpoint():
    """官方 oMLX 下载模型用的 Hugging Face 地址（空是它的默认 huggingface.co）。global-settings 的回复里有主密钥：只取这一项。"""
    try: value = (api(LOCAL, '/admin/api/global-settings', timeout=5).get('huggingface') or {}).get('endpoint')
    except Exception: return None
    return value if isinstance(value, str) else None


def set_hf_endpoint(value):
    api(LOCAL, '/admin/api/global-settings', {'hf_endpoint': value}, timeout=30)
    if hf_endpoint() != value: raise RuntimeError(tr('改了 oMLX 的下载地址，但核对时不一样，请在 oMLX 的设置里看一眼。'))


def download_source(what, mirror=False):
    """下载源三档（10.8）。cn：下载安装包优先魔搭转存，模型提示在 oMLX 里选"魔搭"，**不改** oMLX 的 Hugging Face 地址（已定第 29 条 ①）。
    cn mirror：用户自己勾了社区镜像，官方版、允许了主密钥、服务开着时才把 HF 地址换成它（原来的记在设置里）。
    换到别的档、cn 不带 mirror、restore：地址是我们改的就换回原来的。"""
    table, _ = compat_now()
    cn = ((table or {}).get('sources') or {}).get('cn_hf_endpoint') or DEFAULT_CN_HF
    note = None
    official_here = ENGINE_HERE['kind'] == 'official'
    if what in SOURCE_PREFS or what == 'restore':
        before = saved().get('hf_endpoint_before')
        if what == 'cn' and mirror:
            if official_ready():
                current = hf_endpoint()
                if current is None: raise RuntimeError(tr('读不到 oMLX 现在的下载地址，没改。'))
                if current != cn:
                    if before is None: save_setting('hf_endpoint_before', current)
                    set_hf_endpoint(cn)
            elif official_here:
                note = tr('只记下了安装包优先用魔搭。要换社区镜像，先允许控制台读主密钥并开着 oMLX，再勾一次。')
        elif isinstance(before, str):
            if official_ready():
                current = hf_endpoint()
                if current is None: raise RuntimeError(tr('读不到 oMLX 现在的下载地址，没换回；原来的地址还记着，稍后再试。'))
                if current == cn: set_hf_endpoint(before)   # 不等于镜像：你在 oMLX 里自己改过，照你的，不再换回
                conf = saved()
                conf.pop('hf_endpoint_before', None)
                save_conf(conf)
            else:
                note = tr('oMLX 的下载地址是控制台改的，要开着 oMLX、允许读主密钥才能换回。')
        save_setting('download_source', 'auto' if what == 'restore' else what)
        if what == 'cn' and not mirror and official_here and not note:
            note = tr('下载模型请在 oMLX 的模型下载页选"魔搭"：官方 oMLX 自带，不经过第三方。')
    elif what != 'show':
        raise RuntimeError('engine source show | auto | intl | cn [mirror] | restore')
    if not official_here: note = note or tr('这台的 oMLX 不是官方版：下载源只影响控制台下载安装包时挑哪个来源。')
    current = hf_endpoint() if official_ready() else None
    changed = isinstance(saved().get('hf_endpoint_before'), str)
    if what == 'cn' and not mirror and current == cn and not changed:
        note = tr('oMLX 的下载地址是你在 oMLX 里自己设成 {0} 的，控制台不动它；要换回请在 oMLX 的设置里改。', cn)
    return {'ok': True, 'pref': source_pref(), 'pref_text': SOURCE_PREFS[source_pref()],
            'hf_endpoint': (current or 'https://huggingface.co') if current is not None else None, 'cn_endpoint': cn,
            'mirror': None if current is None else current == cn,   # 读不到（服务没开、没允许读主密钥）就是不知道
            'mirror_about': tr('{0} 是个人维护的社区镜像，没有官方保障；它同时给文件和校验值，出了错核不出来。', cn),
            'changed_by_us': changed, 'can_restore': changed, 'applies': official_here, 'note': note}


def backup_omlx_settings():
    if ENGINE_HERE['kind'] != 'official' or not ENGINE_HERE.settings or not ENGINE_HERE.settings.is_file():
        raise RuntimeError(tr('这台没有官方 oMLX 的设置文件，不用备份。'))
    return {'ok': True, 'path': str(updates.backup_settings(ENGINE_HERE.settings, SETTINGS_BACKUPS))}


def selfcheck(expect=None):
    """升级后自检（10.6）：重新认一遍这台的 oMLX，签名、版本、服务、主密钥、兼容表。"""
    info = omlx_adapter.detect(ROOT)
    items = []
    def item(name, good, detail=''):
        items.append({'name': name, 'ok': bool(good), 'detail': detail})
    item(tr('认出官方 oMLX'), info['kind'] == 'official', ENGINE_HOW.get(info.get('how') or '', '') or ENGINE_KINDS.get(info['kind'], ''))
    if info.get('app'): item(tr('官方签名'), info.get('signed') is True, info['app'])
    up = health(LOCAL) == 'up'
    running = engine_version(LOCAL) if up and key_consent() is True else None
    version = running or info.get('app_version')
    item(tr('服务开着'), up, tr('端口 {0}', info.get('port')) if up else tr('没开：从"应用程序"里打开 oMLX'))
    item(tr('主密钥能用'), running is not None, '' if running else tr('还没允许控制台读，或服务没开'))
    if expect: item(tr('版本是 {0}', expect), omlx_adapter.same_version(version, expect), version or tr('读不到'))
    code, _ = omlx_adapter.verdict(version, compat_now()[0])
    item(tr('兼容表'), code in USABLE, state_word(code))
    return {'ok': True, 'passed': all(i['ok'] for i in items), 'version': version, 'items': items}


def time_text(ts):
    return time.strftime('%m-%d %H:%M', time.localtime(ts)) if isinstance(ts, (int, float)) else None


def updates_info():
    conf = saved()
    url, key = update_url(), update_key()
    configured, enabled = bool(url and key), conf.get('update_check', True) is not False
    manifest = manifest_now()
    table, source = compat_now()   # 和 engine show 用同一份（出错退回随程序发的）
    error = conf.get('updates_error') if isinstance(conf.get('updates_error'), dict) else None
    last, good = conf.get('updates_last'), conf.get('updates_ok_at')
    if not configured: text = tr('还没配置更新地址（公开发布以后才有），不联网。')
    elif not enabled: text = tr('联网检查关着。')
    elif error: text = tr('上次检查（{0}）没成。', time_text(last) or '?')   # 原因在 check.error，应用另起一行显示
    elif good: text = tr('上次检查：{0}。', time_text(good))
    else: text = tr('还没检查过。')
    days = updates.stale_days(manifest) if configured and manifest else None
    if days is not None and days > updates.STALE_DAYS:   # 镜像一直给同一份旧清单时，别让人以为一切正常
        text += tr('联网读到的清单是 {0} 天前发的，很久没更新了，可能被拦住了。', days)
    cstate, cinfo = updates.console_state(console_version(), manifest)
    engine = engine_info()
    system = omlx_adapter.system_slot(macos_version())
    version = engine.get('version') if engine['kind'] == 'official' else None
    ostate, target = updates.omlx_state(engine['kind'], version, table, system)
    note = engine.get('note')
    if engine['kind'] == 'official' and engine.get('how') != 'app' and target:   # 安装包只给官方 App 装的：Homebrew 的拖一份 App 进来会变成两份 oMLX
        target = None
        note = tr('Homebrew 装的：在终端里运行 brew upgrade omlx 升级。') if engine.get('how') == 'brew' else tr('这台的 oMLX 不是官方 App 装的，请用你装它的方式升级。')
    owords = {'managed': tr('自管版，不提示升级'), 'none': tr('没装'),
              'current': tr('已是推荐的版本') if version and (table or {}).get('recommended') == version else tr('已是兼容表里最新的一版'), 'verified_newer': tr('有已验证的新版'),
              'unverified_newer': tr('有新版，还没验证'), 'newer_untested': tr('比控制台测过的版本新'), 'bad': tr('这一版有已知问题'),
              'old': tr('测试版，请升级到正式版') if omlx_adapter.is_prerelease(version) else tr('太旧，请升级'), 'unknown': tr('读不到版本')}   # 0.5.1 A2：测试版不说"太旧"
    shown = updates.notices(manifest, {'console': console_version(), 'omlx': version, 'macos': macos_version(), 'kind': engine['kind']},
                            conf.get('dismissed_notices') or [])
    return {'ok': True,
            'check': {'enabled': enabled, 'configured': configured, 'last': time_text(last), 'last_ok': (not error) if last else None,
                      'error': (error or {}).get('text'), 'source': source, 'text': text},
            'console': {'version': console_version(), 'latest': (cinfo or {}).get('version'), 'state': cstate,
                        'state_text': {'latest': tr('已是最新'), 'update': tr('有新版'), 'unknown': tr('没检查过')}[cstate],
                        'url': (cinfo or {}).get('url') if cstate == 'update' else None, 'notes': pick((cinfo or {}).get('notes')) if cstate == 'update' else None},
            'omlx': {'kind': engine['kind'], 'version': version, 'state': ostate, 'state_text': owords[ostate],
                     'target': {'version': target['version'], 'state': target.get('state'), 'state_text': state_word(target.get('state'))} if target else None,
                     'note': note},
            'notices': [{'id': n['id'], 'level': n.get('level') if n.get('level') in ('info', 'warn', 'critical') else 'info',
                         'title': pick(n.get('title')) or '', 'body': pick(n.get('body')) or '', 'url': n.get('url') if isinstance(n.get('url'), str) else None}
                        for n in shown]}


def updates_check(mode):
    """联网读一次清单（开着、配置了地址才读；mode：force 立即、launch 距上次一小时以上、平时 24 小时以上）。"""
    conf = saved()
    url, key = update_url(), update_key()
    if (url or update_urls()) and key and conf.get('update_check', True) is not False and updates.due(conf.get('updates_last'), mode):
        save_setting('updates_last', time.time())
        data, failed = None, None
        for one in update_urls() or [url]:   # 一个地址读不到、签名不对、比手上的旧，就试下一个；都不行才记错（记第一个的）
            try:
                data = updates.check_online(one, key, UPDATES_DIR, floor=conf.get('updates_issued'))
                break
            except updates.Failure as exc:
                failed = failed or {'code': exc.code, 'text': failure_text(exc)}
            except Exception as exc:   # 什么错都记下来，免得界面一直显示上一次成功
                failed = failed or {'code': 'error', 'text': str(exc)[:160]}
        if data is None: save_setting('updates_error', failed)
        else:
            conf = saved()
            conf.pop('updates_error', None)
            conf.update(updates_ok_at=time.time(), updates_issued=data['issued'])   # 防回滚的下限：缓存被删了也不让旧清单回来
            save_conf(conf)
    return updates_info()


def updates_command(rest, as_json=False):
    """updates show | check [launch|force] | enable | disable | dismiss <id>：更新中心（10.6、10.7）。"""
    what = rest[0] if rest else 'show'
    if what == 'check': info = updates_check(rest[1] if len(rest) > 1 and rest[1] in ('launch', 'force') else 'daily')
    elif what in ('enable', 'disable'):
        save_setting('update_check', what == 'enable')
        info = updates_info()
    elif what == 'dismiss' and len(rest) > 1:
        gone = list(saved().get('dismissed_notices') or [])
        if rest[1] not in gone: save_setting('dismissed_notices', (gone + [rest[1]])[-200:])
        info = updates_info()
    elif what == 'show': info = updates_info()
    else: raise RuntimeError('updates show | check [launch|force] | enable | disable | dismiss <id>')
    if as_json: return print(json.dumps(info, ensure_ascii=False))
    print(tr('控制台：{0}，{1}', info['console']['version'] or tr('读不到'), info['console']['state_text'])
          + (tr('（新版 {0}：{1}）', info['console']['latest'], info['console']['url'] or '') if info['console']['state'] == 'update' else ''))
    o = info['omlx']
    print(tr('oMLX：{0}', o['state_text']) + (tr('（{0}）', o['version']) if o['version'] else '')
          + (tr('，建议升级到 {0}（{1}）', o['target']['version'], o['target']['state_text']) if o['target'] and o['state'] not in ('none',) else ''))
    print(info['check']['text'])
    for n in info['notices']: print(tr('通知：{0}', n['title']) + (tr('——{0}', n['body']) if n['body'] else ''))


def engine_extra(what, rest):
    """engine 的第 3b 期子命令，返回要打印的 dict。"""
    opts = opts_of(rest)
    if what == 'install-plan': return install_plan(opts.get('version'))
    if what == 'download': return engine_download(opts)
    if what in ('check-file', 'open-package'):
        if not rest: raise RuntimeError(tr('要给安装包的路径。'))
        if what == 'open-package': return open_package(rest[0], rest[1:2] == ['upgrade'])
        can_install()
        return check_package(rest[0])
    if what == 'autoupdate': return autoupdate(rest[0] if rest else 'show')
    if what == 'source': return download_source(rest[0] if rest else 'show', mirror=rest[1:2] == ['mirror'])
    if what == 'gateway': return limits_command(rest[1:]) if rest[:1] == ['limits'] else gateway_command(rest[0] if rest else 'show')
    if what == 'networks': return networks_command(rest)
    if what == 'backup-settings': return backup_omlx_settings()
    if what == 'selfcheck': return selfcheck(opts.get('expect'))
    return None


def status_json(check_worker=True):
    """给ModelTower应用用的机器可读状态；不含任何密钥。"""
    started = time.time()
    s = snapshot(check_worker)
    percent = alert_percent()
    nodes, dsh = {}, dsh_models()
    models = {name: model_meta(name) for name in MODELS}   # 卡片头的说明：先放这台的，副机才有的随后补上
    order = list(MODELS)
    for node, meta in NODES.items():
        with models_of(node):   # 这一台的上下文、开关、内存都按它自己的清单算（2b）
            nodes[node] = node_status(node, meta, s, dsh, percent)
            has = nodes[node]['has'] = node_has(node, s['nodes'][node])   # 这台有哪些模型，界面只列这些
            for name in has:
                if name not in models: models[name], order = model_meta(name), order + [name]
                elif model_meta(name) != models[name]: nodes[node].setdefault('meta', {})[name] = model_meta(name)   # 这台的说法和主机的不一样（③存在模型所在的那台）
    set_wants([node for node in NODES if s['nodes'][node]['health'] == 'up'], True, since=started)   # D1：看到在跑就记"要开"
    h3 = h3_state()
    progress = h3['progress'] if isinstance(h3['progress'], dict) else {}
    busy = any('loading' in n['models'].values() for n in s['nodes'].values())
    return {
        'ok': True, 'time': time.strftime('%H:%M:%S'), 'mode_text': describe(s, keys=False), 'busy': busy,
        'order': list(NODES), 'nodes': nodes, 'models': models, 'model_order': order, 'model_recency': model_recency(order),
        'cache_info': [list(pair) for pair in CACHE_INFO], 'cache_recommended': CACHE_RECOMMENDED,
        'links': [[code, tr(value)] for value, code in zip(LINK_VALUES, LINK_CODES)],   # 改地址时连接方式的选项：[代号, 显示的字]
        'advice': ALERT_RISK + (tr('请让 Ollama 释放，或卸载那台的模型。') if s['ollama'] is not None else tr('请卸载那台的模型。')),
        'tools': {'dsh': has_dsh(), 'ollama': s['ollama'] is not None, 'h3': has_h3()},   # 个人工具检测到才出现（2b）
        **({} if MANAGED_HERE else (lambda engine: {'engine': engine, 'setup': setup_info(nodes[LOCAL], engine)})(engine_info())),   # 第 3、4 期：只有官方版、没装时才有这两项；自管版的状态和以前逐字段相同
        'ollama': None if s['ollama'] is None else
                  [{'name': m['name'], 'size_gb': gib(m.get('size', 0))} for m in s['ollama']],
        'h3': {'page': h3['page'], 'job': h3['job'], 'detail': progress.get('message') or progress.get('stage') or ''},
        'keys': {'note': KEY_NOTE, 'sub_keys': status_sub_keys(), **({'can_disable': True} if can_disable()[0] else {})},   # can_disable：入口开着且在这台（官方版默认）；自管版没开主机模式时没有这个字段
        'pages': {**{node: f"http://127.0.0.1:{meta['port']}/admin" for node, meta in NODES.items()},
                  'chat': f"http://127.0.0.1:{NODES[LOCAL]['port']}/admin/chat"},
    }


def choose_context():
    print(tr('\n上下文 = 一次对话最多能装下多少 token（中文约 1 字 ≈ 1 token）。只影响内存和等待时间，不改模型权重和精度。{0}各设各的；正在跑的当场生效。', EVERY))
    s = snapshot()
    lists = menu_lists()   # 每台按它自己的清单（2b）
    rows = [(node, name) for node, names in lists for name in names]
    for i, (node, name) in enumerate(rows, 1):
        n = s['nodes'][node]
        now = live_context(n, name)
        state = tr('在跑，实时 {0}', ctx_label(now)) if now else tr('没在跑，下次加载用 {0}', ctx_label(node_context(node, name)))
        print(tr('  {0} {1} · {2}（{3}）', i, where(node), MODELS[name]['title'], state))
    switches = [(node, name, key) for node, names in lists for name in names for key in OPTIONS if option_mode(name, key) == 'free']
    print(tr('开关（{0}各设各的；都要重新加载模型才生效，只下线那台的这个模型，服务、控制台、dsh 都不用重启）：', EVERY) if has_dsh()
          else tr('开关（{0}各设各的；都要重新加载模型才生效，只下线那台的这个模型，服务和控制台都不用重启）：', EVERY))
    for i, (node, name, key) in enumerate(switches, len(rows) + 1):
        on = node_options(node, name)[key]
        running = s['nodes'][node]['models'][name] == 'loaded'
        print(tr('  {0} {1} · {2} · {3}（现在{4}，选它改为{5}{6}）', i, where(node), MODELS[name]['title'], OPTIONS[key], (tr('开') if on else tr('关')), (tr('关') if on else tr('开')), (tr('；在跑，会问你什么时候切换') if running else tr('；没在跑，下次加载生效'))))
    caches = list(NODES)
    first_cache = len(rows) + len(switches) + 1
    print(tr('多轮缓存（{0}各设各的；开着时同一份资料再问只要几秒，资料的中间结果会留在那台的硬盘上）：', EVERY))
    for i, node in enumerate(caches, first_cache):
        print(tr('  {0} {1}（{2}）', i, where(node), cache_text(node, s['nodes'][node], s['stats'].get(node, {}))))
    pick = input(tr('调整哪一项？（回车取消）')).strip()
    if pick in {str(i) for i in range(first_cache, first_cache + len(caches))}:
        node = caches[int(pick) - first_cache]
        n, probe = s['nodes'][node], s['stats'].get(node, {})
        print('\n'.join(tr('{0}：{1}', title, text) for title, text in CACHE_INFO))
        choices = ['off', *map(str, cache_options(node, n, probe)), *(['auto'] if official(node) else [])]   # 官方版能选回"自动"（A14）
        for i, word in enumerate(choices, 1):
            print(f"  {i} {tr('关') if word == 'off' else tr('自动') if word == 'auto' else word + ' GB'}{tr('（推荐）') if word == str(CACHE_RECOMMENDED) else ''}{tr('  ← 现在') if cache_word(cache_now(node, n, probe)) == cache_word(0 if word == 'off' else word if word == 'auto' else int(word)) else ''}")
        pick = input(tr('选择：')).strip()
        if pick in {str(i) for i in range(1, len(choices) + 1)}: set_cache(node, choices[int(pick) - 1])
        return
    if pick in {str(i) for i in range(len(rows) + 1, len(rows) + len(switches) + 1)}:
        node, name, key = switches[int(pick) - len(rows) - 1]
        with models_of(node): set_option(node, name, key, not node_options(node, name)[key])
        return
    if pick not in {str(i) for i in range(1, len(rows) + 1)}: return
    node, name = rows[int(pick) - 1]
    model = MODELS[name]
    info = context_info(node, name, s['nodes'][node], s['stats'].get(node, {}).get('total'))
    print('\n'.join(tr('{0}：{1}', title, text) for title, text in option_info(name)['context']))
    print(tr('模型上限 {0}；下面只列模型支持的档位。', ctx_label(info['native'])))
    for i, value in enumerate(info['options'], 1):
        kv = value * model['kv'] / GIB
        wait = value / model['prefill'] / 60
        words = ' · '.join(x for x in (ctx_label(value), info['notes'][str(value)], info['compact'][str(value)]) if x)
        print(tr('  {0} {1}{2}\n      约多占 {3} GB 内存；塞满一次时首字前要等约 {4} 分钟（{5}）', i, words, (tr('  ⚠️ 超安全线') if value in info['over'] else ''), f'{kv:4.1f}', f'{wait:4.1f}', info["marks"][str(value)]))
    pick = input(tr('选择档位：')).strip()
    if pick not in {str(i) for i in range(1, len(info['options']) + 1)}: return
    with models_of(node): set_context(node, name, info['options'][int(pick) - 1])


def latest_log():
    if not MANAGED_HERE:   # 官方版：走管理接口读它自己的日志（日志目录按它的设置，不一定在数据目录里）
        try: r = api(LOCAL, '/admin/api/logs?lines=25', timeout=10)
        except Exception as exc: return tr('（没有日志）'), [tr('（读不到：{0}）', exc)]
        return str(r.get('log_file') or 'server.log'), str(r.get('logs') or '').splitlines()[-25:]
    latest = sorted((STATE / 'logs').glob('*.log'), key=lambda p: p.stat().st_mtime)
    return latest[-1].name if latest else tr('（没有日志）'), latest[-1].read_text(errors='replace').splitlines()[-25:] if latest else []


def logs(target='all'):
    for node in pick_nodes(target):
        if is_local(node): name, text = latest_log()
        elif is_rpc(node):
            try:
                r = rpc_query(node, 'logs', timeout=15)
                name, text = r['name'], r['lines']
            except RuntimeError: name, text = tr('（没有日志）'), [tr('（连不上 {0}）', where(node))]
        else:
            r = ssh_to(node, posix(f'f=$(ls -t "{STATE}"/logs/*.log 2>/dev/null | head -1); echo "$f"; [ -n "$f" ] && tail -25 "$f"'), timeout=15)
            lines = r.stdout.splitlines() if r.returncode == 0 else [tr('（连不上 {0}）', where(node))]
            name, text = Path(lines[0]).name if lines and lines[0] else tr('（没有日志）'), lines[1:]
        print(tr('— {0} · {1} 最后 25 行 —', where(node), name))
        print('\n'.join(text))


DOCTOR = {
    'version': f"{ROOT}/.omlx-venv/bin/python -c \"import importlib.metadata as m; print(m.version('omlx'), m.version('mlx'))\"",
    'digest': f"cd {ROOT}/.omlx-venv/lib/python3.12/site-packages/omlx && find . -name '*.py' -type f | LC_ALL=C sort | xargs shasum -a 256 | shasum -a 256 | cut -c1-12",
    'serve': f'shasum -a 256 {ROOT}/scripts/serve_omlx.py 2>/dev/null | cut -c1-12',
    'listen': f'lsof -nP -iTCP:{OMLX_PORT} -sTCP:LISTEN 2>/dev/null | tail -n +2',
    'models': '; '.join(f'if [ -f {ROOT}/models/{m["id"]}/config.json ]; then echo {name}; fi' for name, m in MODELS.items() if not m.get('auto')),
    'pins': f"grep -c '\"is_pinned\": true' {STATE}/model_settings.json 2>/dev/null || true",
    'links': f'for f in "{SERVING}"/*; do [ -L "$f" ] && basename "$f"; done; true',
    'disk': PROBE['disk'],
    'procs': PROBE['procs'],
    'cache_conf': PROBE['cache_conf'],
}
DOCTOR_MODELS = [name for name, m in MODELS.items() if not m.get('auto')]   # 上面 models 那一项查的是哪些


def own_models(node):
    """这台自己的②里列的模型（住在 models/ 里的，不含 oMLX 自己文件夹里的）；副机没带回自己的②时返回 None。"""
    if is_local(node): facts = HOST_FACTS
    else: facts = node_data(node).get('facts')
    if facts is None: return None if not is_local(node) and node_data(node) else {name: m for name, m in HOST_MODELS.items() if not m.get('auto')}
    return {name: m for name, m in models_for(node).items() if name in facts and not m.get('auto')}


def doctor_official():
    """官方版的体检（3a，草案 10.4）：装没装、签名、版本落在兼容表哪档、数据目录、主密钥、服务、只听本机、固定、硬盘、
    副机连不连得上、Bonjour。自管才有的几项（启动脚本、代码指纹、模型链接、子密钥收权）不查：官方版没有这些，
    子密钥能直接加载模型（契约测试已证实），由入口挡。最后一行格式和 doctor() 一样（应用当结果显示）。"""
    results = []

    def check(title, passed, detail=''):
        passed = bool(passed)
        results.append(passed)
        print((ok if passed else bad)(title + (tr('：{0}', detail) if detail else '')))

    here, info = where(LOCAL), engine_info(signature=True)
    if info['kind'] == 'none':
        check(tr('{0} 装了 oMLX', here), False, tr('没找到：装官方的 oMLX 应用，或用 Homebrew 装'))
    else:
        check(tr('{0} 装了 oMLX（{1}）', here, info.get('how_text', '')), True)
        if info.get('app'):
            check(tr('oMLX 应用是官方签名'), info.get('signed') is True, {False: tr('签名不对，别用这一份，重新从官方下载'), None: tr('核对不了')}.get(info.get('signed'), ''))
        check(tr('oMLX 版本 {0}', info.get('version') or tr('读不到')), info.get('verdict') in ('verified', 'testing', 'works', 'newer', 'unlisted'),
              info['verdict_text'] + (tr('：{0}', info['note']) if info.get('note') else ''))
        check(tr('oMLX 的数据目录在'), bool(info.get('base')), info.get('base') or tr('没找到它的 settings.json：先打开一次 oMLX 应用'))
        check(tr('控制台可以读 oMLX 的主密钥'), info['key'] == 'allowed',
              {'ask': tr('还没问过（engine allow-key）'), 'denied': tr('没允许，加载、卸载这些管理功能用不了（engine allow-key 改）')}.get(info['key'], ''))
        up = health(LOCAL) == 'up'
        check(tr('{0} 的 oMLX 在运行', here), up, '' if up else tr('没有运行（serve 开）'))
        if up:
            listen = [line for line in subprocess.run(['/bin/sh', '-c', f"lsof -nP -iTCP:{NODES[LOCAL]['port']} -sTCP:LISTEN 2>/dev/null | tail -n +2"],
                                                      capture_output=True, text=True, errors='replace', timeout=15).stdout.splitlines() if line.strip()]
            if listen: check(tr('{0} 的 oMLX 只监听本机', here), all('127.0.0.1:' in line for line in listen),
                             '' if all('127.0.0.1:' in line for line in listen) else tr('局域网也能直连它，绕过入口；在 oMLX 设置里把监听地址改回 127.0.0.1'))
            pinned = [mid for mid, row in engine_rows(LOCAL).items() if (row.get('settings') or {}).get('is_pinned')]
            if pinned: check(tr('{0} 有 {1} 个模型被固定，oMLX 启动时会自动加载', here, len(pinned)), True)
        if info.get('base') and Path(info['base']).is_dir():
            limit = official_cache_limit(official_cache_conf())
            free, need = shutil.disk_usage(info['base']).free, max(limit if isinstance(limit, int) else 0, 20)
            check(tr('{0} 硬盘剩余超过 {1} GB', here, need), free > need * GIB, tr('剩 {0}', gb(free)))
        rows = (gateway_conf() or {}).get('disabled_keys')
        if gateway_conf() is None or (rows is not None and not isinstance(rows, (dict, list))):
            check(tr('子密钥的停用名单读得懂'), False, tr('{0} 读不懂：停用的子密钥现在都没被挡住。看一眼这个文件，或删掉里面的 disabled_keys 再重新停用。', ROOT / 'gateway.json'))
        if info.get('gateway') == 'off':
            check(tr('客户端经入口连'), False, tr('入口被关了：客户端直连 oMLX 时，没加载的模型会被点起来（官方版的子密钥也能加载）。在 设置 → oMLX 能打开'))
        elif info.get('gateway') == 'elsewhere':
            check(tr('入口在主机上（主机模式）'), True)
        else:
            check(tr('入口在运行（端口 {0}）', info.get('client_port', '?')), info.get('gateway') == 'running', '' if info.get('gateway') == 'running'
                  else info.get('gateway_note') or tr('开着但没在运行：重开控制台或启动 oMLX 时会再起'))
            if info.get('gateway') == 'running': doctor_gateway_doors(check)
    for node in remotes():
        reach(node)
        up = health(node) == 'up'
        check(tr('{0} 的 oMLX 连得上', where(node)), up, '' if up else tr('连不上：确认 {0} 开着、网络和地址对', where(node)))
    commands = subprocess.run(['ps', '-axo', 'command='], capture_output=True, text=True, errors='replace').stdout.splitlines()
    adverts = [line for line in commands if re.match(r'(\S*/)?dns-sd\s+-[RP]\s', line)]
    check(tr('没有在局域网广播 Bonjour'), not adverts, tr('；').join(adverts)[:120])
    failed = results.count(False)
    if failed and not sys.stdout.isatty(): print(DOCTOR_FAILED)
    print(ok(tr('体检：{0} 项全部通过', len(results))) if not failed else warn(tr('体检：{0} 项里有 {1} 项要看（见上面 ✕ 的行）', len(results), failed)))


def doctor_gateway_doors(check):
    """体检（0.6）：入口只在该听的地址上听——本机那一个，加上勾了、入口说开着的那几扇门；多出来的（尤其是 * 所有网卡）就报。
    勾了网络时再看系统防火墙有没有把入口拦住。"""
    import hub
    conf = gateway_conf()
    pid, port, chosen = hub.gateway_pid(), gateway_port(conf), chosen_networks(conf)
    if not pid or not port: return
    health = gateway_health(conf) or {}
    doors = [d for d in health.get('doors', []) if isinstance(d, dict) and d.get('address')]
    listen = (conf or {}).get('listen', '127.0.0.1')
    expected = {'127.0.0.1': {'127.0.0.1'}, '::1': {'::1'}, 'localhost': {'127.0.0.1', '::1'}, '0.0.0.0': {'*'}, '::': {'*'}}.get(listen, {listen})
    expected |= {d['address'] for d in doors}
    try: out = subprocess.run(['lsof', '-nP', '-a', '-p', str(pid), f'-iTCP:{port}', '-sTCP:LISTEN', '-Fn'], capture_output=True, text=True, errors='replace', timeout=15).stdout
    except (OSError, subprocess.SubprocessError): out = ''
    heard = {line[1:].rsplit(':', 1)[0].strip('[]') for line in out.splitlines() if line.startswith('n')}
    if heard:
        extra = sorted(heard - expected)
        check(tr('入口只在本机和勾了的网络上听'), not extra, tr('多听了 {0}：没勾的地址不该开着。看一眼 {1} 的 listen，或重开入口', tr('、').join(extra), ROOT / 'gateway.json') if extra else '')
    if not chosen: return
    if 'version' not in health:
        check(tr('入口是这一版的'), False, tr('还在跑的是旧版本的入口，不认勾的网络：到 设置 → oMLX 把入口关了再开'))
        return
    if doors: check(tr('入口对 {0} 个网络开着（{1}）', len({d.get('id') for d in doors}), tr('、').join(sorted(d['address'] for d in doors))), True)
    else: check(tr('勾了的网络开着门'), False, tr('勾了 {0} 个网络，现在一个都没连着（或还在认路由器）：连上后会自动打开', len(chosen)))
    state = firewall_state(pid)
    if state == 'blocked':
        check(tr('系统防火墙没有拦入口'), False, tr('防火墙拦住了入口用的 Python，别的设备连不进来：到 系统设置 → 网络 → 防火墙 → 选项，把它改成"允许传入连接"'))
    elif state in ('allowed', 'off'): check(tr('系统防火墙没有拦入口'), True)


def firewall_state(pid):
    """系统防火墙对入口那个进程的态度：'off'（防火墙没开）、'blocked'、'allowed'、None（开着但名单里没有它，或读不出来——第一次有设备连进来时系统会问）。只读。"""
    tool = os.environ.get('LMC_FIREWALL_TOOL') if os.environ.get('LMC_ROOT') else None   # 环境变量只给测试用
    tool = tool or '/usr/libexec/ApplicationFirewall/socketfilterfw'
    def ask(*args):
        try: return subprocess.run([tool, *args], capture_output=True, text=True, errors='replace', timeout=5).stdout
        except (OSError, subprocess.SubprocessError): return ''
    state = ask('--getglobalstate')
    if 'disabled' in state.lower() or 'State = 0' in state: return 'off'
    if 'enabled' not in state.lower(): return None
    try: binary = subprocess.run(['ps', '-p', str(pid), '-o', 'comm='], capture_output=True, text=True, errors='replace', timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError): return None
    if not binary: return None
    answer = ask('--getappblocked', os.path.realpath(binary)).lower()
    if 'blocked' in answer and 'not' not in answer and 'permitted' not in answer: return 'blocked'
    if 'permitted' in answer: return 'allowed'
    return None


def doctor():
    """一键体检：只读（隧道断了会顺手接上），逐项打印 ●/✕，最后一行是结论（应用把最后一行当结果显示）。
    2b 起每项有适用条件（草案 9.7）：单机、没有我们的启动脚本、没存过缓存设置的，不查用不上的那几项。"""
    results = []
    if not MANAGED_HERE: return doctor_official()   # 3a：官方版另一套（自管这条路一个字不变）

    def check(title, passed, detail=''):
        passed = bool(passed)
        results.append(passed)
        print((ok if passed else bad)(title + (tr('：{0}', detail) if detail else '')))

    for name in MODELS:  # 只在对不上时才多一行
        problem = label_problem(name)
        if problem: check(tr('{0} 档案写着满血', MODELS[name]['title']), False, problem)

    def shell(command):
        return subprocess.run(['/bin/sh', '-c', command], capture_output=True, text=True, errors='replace', timeout=30).stdout.strip()

    def address(meta): return tr('{0}（{1} {2}）', meta['ip'], tr(meta['link']), meta['interface'])

    def ifaces(node): return ' '.join(dict.fromkeys(a['interface'] for a in addresses(node)))

    def matched(node, have):
        """have：网口 → 那台此刻的地址。返回登记的地址里此刻对得上的。"""
        return [a for a in addresses(node) if have.get(a['interface']) == a['ip']]

    def read_ips(text):
        return dict(line.split(' ', 1) for line in (text or '').splitlines() if ' ' in line)

    mine = {k: shell(v) for k, v in DOCTOR.items() if k != 'procs'}
    mine['serve'] = hashlib.sha256(SERVE_SCRIPT.read_bytes()).hexdigest()[:12] if SERVE_SCRIPT.exists() else ''
    info = {LOCAL: mine}
    for node in remotes():
        if is_rpc(node):
            try: info[node] = rpc_query(node, 'doctor-data', timeout=60)
            except RuntimeError: info[node] = None
            continue
        # 配了固定地址的才查地址
        script = {**DOCTOR, **({'ip': f"ipconfig getifaddr {NODES[node]['interface']}"} if NODES[node].get('ip') else {})}
        expect = own_models(node)
        if expect is not None and list(expect) != DOCTOR_MODELS: script['models'] = '; '.join(f'if [ -f {ROOT}/models/{m["id"]}/config.json ]; then echo {name}; fi' for name, m in expect.items()) or 'true'
        if len(addresses(node)) > 1: script['ips'] = f'for i in {ifaces(node)}; do echo "$i $(ipconfig getifaddr $i)"; done'   # 登记了几个地址：每个网口都看
        # B1：交给 /bin/sh，links 一项带通配符，zsh 没匹配会整串中止；末尾 true，退出码只说明连没连上，
        # 不跟着最后一项走（网口没地址时 ipconfig 返回 1，地址那一项自己会报"没有地址"）
        try: r = ssh_to(node, posix('; '.join(f"echo '@@{k}'; {v}" for k, v in script.items()) + '; true'), timeout=60)
        except subprocess.TimeoutExpired: r = None
        info[node] = sections(r.stdout) if r and r.returncode == 0 else None

    meta = NODES[LOCAL]
    if len(addresses(LOCAL)) > 1:
        good = matched(LOCAL, read_ips(shell(f'for i in {ifaces(LOCAL)}; do echo "$i $(ipconfig getifaddr $i)"; done')))
        check(tr('{0} 的地址是登记的之一（{1}）', where(LOCAL), tr('、').join(map(address, addresses(LOCAL)))), good,
              tr('现在是 {0}', tr('、').join(map(address, good))) if good else tr('登记的地址一个都不在'))
    elif meta.get('ip'):
        here = shell(f"ipconfig getifaddr {meta['interface']}")
        check(tr('{0} 地址是 {1}', where(LOCAL), address(meta)), here == meta['ip'], here or tr('没有地址'))
    for node in remotes():
        meta, remote, title = NODES[node], info[node], where(node)
        if len(addresses(node)) > 1:
            target = ssh_shown(node).get('hostname', '')
            listed = [a['ip'] for a in addresses(node)]
            check(tr('{0} 的连接设置指向登记的地址之一', title), target in listed, target or tr('没有配置'))
        elif meta.get('ip') and not sole_address(node):   # 只登记一个地址的按登记的连，SSH 配置写什么不要紧
            config = f'-F {shlex.quote(str(SSH_CONFIG))} ' if is_rpc(node) else ''
            target = next((line.split()[1] for line in shell(f"ssh {config}-G {meta['ssh']}").splitlines() if line.startswith('hostname ')), '')
            check(tr('{0} 的连接设置指向 {1}', title, meta['ip']), target == meta['ip'], target or tr('没有配置'))
        check(tr('{0} 能用密钥登录', title), remote is not None, '' if remote else tr('连不上：确认 {0} 开着、网络和地址对；开着 FileVault 的要先解锁', title) + (tr('。{0}', address_note(node)) if address_note(node) else ''))
        if remote is None: continue
        if len(addresses(node)) > 1:
            good = matched(node, remote['ips'] if isinstance(remote.get('ips'), dict) else read_ips(remote.get('ips')))
            used = next((a for a in addresses(node) if a['ip'] == address_now(node)), None)
            check(tr('{0} 的地址是登记的之一（{1}）', title, tr('、').join(map(address, addresses(node)))), good,
                  (tr('现在连的是 {0}', address(used)) if used else tr('现在是 {0}', tr('、').join(map(address, good)))) if good else tr('登记的地址一个都不在'))
        elif meta.get('ip'): check(tr('{0} 地址是 {1}', title, address(meta)), remote.get('ip') == meta['ip'], remote.get('ip') or tr('没有地址'))
        if remote.get('official'):
            # 配对加入的官方版副机（体检 O14）：它的 oMLX 是官方的，和这台自管的版本、补丁、启动脚本本来就不一样，不比；查它自己的几项
            engine = remote.get('engine') if isinstance(remote.get('engine'), dict) else {}
            installed = engine.get('kind') not in (None, 'none')
            check(tr('{0} 装了 oMLX（{1}）', title, engine.get('how_text') or tr('官方版')) if installed else tr('{0} 装了 oMLX', title), installed,
                  '' if installed else tr('没找到：到那台上装官方的 oMLX'))
            if installed:
                check(tr('{0} 的 oMLX 版本 {1}', title, engine.get('version') or tr('读不到')), engine.get('verdict') in ('verified', 'testing', 'works', 'newer', 'unlisted'),
                      str(engine.get('verdict_text') or ''))
                check(tr('{0} 的控制台可以读它的 oMLX 主密钥', title), engine.get('key') == 'allowed',
                      '' if engine.get('key') == 'allowed' else tr('没允许，这台在它上面加载不了模型：到那台运行 engine allow-key，或在它的控制台里点允许'))
            if isinstance(remote.get('free'), int): check(tr('{0} 硬盘剩余超过 {1} GB', title, 20), remote['free'] > 20 * GIB, tr('剩 {0}', gb(remote['free'])))
            check(tr('{0} 的控制台程序和这台一致', title), remote.get('code') == code_hash(), '' if remote.get('code') == code_hash() else tr('重新配对一次就会更新 {0}', title))
            continue
        check(tr('oMLX / MLX 版本{0}一致', EVERY), mine['version'] == remote.get('version'), f"{where(LOCAL)} {mine['version']} · {title} {remote.get('version')}")
        check(tr('oMLX 代码（含本地补丁）{0}一致', EVERY), mine['digest'] and mine['digest'] == remote.get('digest'),
              f"{where(LOCAL)} {mine['digest']} · {title} {remote.get('digest')}")
        check(tr('启动脚本{0}一致', EVERY), mine['serve'] == remote.get('serve'), '' if mine['serve'] == remote.get('serve') else
              tr('要走维护通道更新 {0}', title) if is_rpc(node) else tr('不一致时下次启动 {0} 会自动同步', title))
        if is_rpc(node): check(tr('{0} 的控制台程序和这台一致', title), remote.get('code') == code_hash(), '' if remote.get('code') == code_hash() else tr('要走维护通道更新 {0}', title))
    for node, data in info.items():
        if data is None or data.get('official'): continue   # 官方版副机上面查过了（O14）
        host = where(node)
        have, expect = data.get('models', '').split(), own_models(node)
        if expect:   # 这台②里列的模型文件都在（2b：只查它自己的；还没有模型、没带回②的不查）
            missing = [name for name in expect if name not in have]
            check(tr('{0} 上 {1} 的文件都在', host, tr('、').join(m['title'] for m in expect.values())), not missing,
                  tr('缺 ') + tr('、').join(expect[n]['title'] for n in missing) if missing else '')
        # 链接在、模型却没加载：多半是在管理页改了已加载模型的设置，oMLX 自己卸载了它、没撤链接（MTP 报告 §9.1）。
        # 这时客户端一点就会把它拉回内存，所以算问题。服务没开时不查：启动服务时会清掉全部链接。
        reach(node)
        if health(node) == 'up':
            with models_of(node): live = {MODELS[m]['id'] for m in active(node_models(node))}   # 按这台的清单（2b）
            stale = [x for x in data.get('links', '').split() if x not in live]
            check(tr('{0} 没有多余的模型链接', host), not stale,
                  tr('链着但没加载：') + tr('、').join(stale) + tr('（客户端一点就会把它拉进内存；用 unload <模型> --on {0} 撤掉）', node) if stale else '')
            # 服务没开时 settings.json 不代表什么：下次启动由启动脚本按存的值带上
            cache_live, cache_want = parse_cache_conf(data.get('cache_conf')), node_cache(node)
            if isinstance(saved(node).get('cache_size', {}).get(node), int):   # 没在控制台里设过就不比（2b）：在 oMLX 管理页设的照它的
                check(tr('{0} 的多轮缓存和设置一致', host), cache_live == cache_want,
                      tr('设置 {0} · oMLX {1}（用 cache set {2} 改一次）', cache_word(cache_want), cache_word(cache_live), node) if cache_live != cache_want else cache_word(cache_want))
        listen = [line for line in data.get('listen', '').splitlines() if line.strip()]
        check(tr('{0} 的 oMLX 只监听本机', host), all('127.0.0.1:' in line for line in listen), tr('没有运行') if not listen else '')
        pinned = data.get('pins', '0') not in ('', '0')
        if clear_pins_on_start() or not pinned:
            check(tr('{0} 没有模型被固定（不会开机预加载）', host), not pinned, tr('{0} 个', data.get('pins')))
        else:   # 设了启动时不取消固定：固定是用户自己要的，只提示（9.9 d）
            check(tr('{0} 有 {1} 个模型被固定，oMLX 启动时会自动加载（设置里选了保留）', host, data.get('pins')), True)
        fields = data.get('disk', '').split()
        if len(fields) > 3 and fields[3].isdigit():
            free, need = int(fields[3]) * 1024, max(node_cache(node), 20)   # 门槛：放得下一整份多轮缓存，关着时留 20 GB（2b）
            check(tr('{0} 硬盘剩余超过 {1} GB', host, need), free > need * GIB, tr('剩 {0}', gb(free)))
    for node in remotes():
        remote, title = info[node], where(node)
        if remote is None: continue
        if remote.get('listen') or (remote.get('official') and remote.get('up')):
            check(tr('{0} 到 {1} 的隧道（127.0.0.1:{2}）', where(LOCAL), title, NODES[node]['port']), ensure_tunnel(node) and tunnel_answers(node))
        if remote.get('official'): continue   # 别的推理进程那一项要读它的进程表，官方版副机不报（O14）
        procs = int(remote.get('procs') or 0)   # 用 oMLX 自带分布式推理的人有这些进程是正常的：只提示，不算没过（2b）
        check(tr('{0} 没有别的推理进程占着', title) if not procs else tr('{0} 有 {1} 个别的推理进程（oMLX 分布式推理的；不是你开的就重启那台）', title, procs), True)
    for node in NODES:
        if health(node) != 'up' or not SERVE_SCRIPT.exists(): continue   # 子密钥的限制是我们的启动脚本加的，没用它就不查（2b）
        if (info.get(node) or {}).get('official'): continue   # 官方版副机没有这层限制，由它自己的入口挡（O14）
        passed, detail = ((info.get(node) or {}).get('subkey') or (False, tr('没有数据'))) if is_rpc(node) else subkey_refused(node)
        check(tr('{0} 的子密钥不能加载、卸载模型', where(node)), passed, '' if passed else detail)
    # 只算注册广播（dns-sd -R / -P）；-G、-B 只是查询。直接读进程表，免得 grep 匹配到自己
    commands = subprocess.run(['ps', '-axo', 'command='], capture_output=True, text=True, errors='replace').stdout.splitlines()
    adverts = [line for line in commands if re.match(r'(\S*/)?dns-sd\s+-[RP]\s', line)]
    check(tr('没有在局域网广播 Bonjour'), not adverts, tr('；').join(adverts)[:120])
    failed = results.count(False)
    if failed and not sys.stdout.isatty(): print(DOCTOR_FAILED)   # 给应用；终端里不打
    print(ok(tr('体检：{0} 项全部通过', len(results))) if not failed else warn(tr('体检：{0} 项里有 {1} 项要看（见上面 ✕ 的行）', len(results), failed)))


def tunnel_answers(node):
    """隧道那头真的是 oMLX：走 rpc 时 health() 不经隧道，这里单独试一次。"""
    if not is_rpc(node): return health(node) == 'up'
    try: get(f"http://127.0.0.1:{NODES[node]['port']}/health", timeout=3)
    except Exception: return False
    return True


def subkey_refused(node):
    """(是否被拒, 回应)：拿子密钥去卸载一个不存在的模型。收权生效回 403；没生效回 404（找不到这个模型），碰不到真模型。
    没有子密钥时用一把随机的假密钥，没生效会回 401。密钥只放在请求头里，不打印。"""
    key = next((k['key'] for k in auth_settings().get('sub_keys', [])), 'doctor-' + secrets.token_hex(8))
    try: api(node, '/v1/models/doctor-no-such-model/unload', {}, timeout=10, key=key)
    except Exception as exc:
        code = re.search(r'HTTP (\d+)', str(exc))
        return bool(code) and code[1] == '403', tr('回了 HTTP {0}', code[1]) if code else tr('没有回应')
    return False, tr('没有被拒绝')


def ensure_h3():
    if owned_pid(RUN_DIR / 'h3-ui-pid.json', 'pid', 'scripts/serve_h3.py'):
        print(ok(tr('H3 视频页已经在运行：http://127.0.0.1:8810/')))
        return
    log = (RUN_DIR / 'h3-server-control.log').open('ab')
    process = subprocess.Popen([str(ROOT / '.h3-venv/bin/python'), str(PROJECT / 'scripts/serve_h3.py')],
                               cwd=PROJECT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (RUN_DIR / 'h3-ui-pid.json').write_text(json.dumps({'pid': process.pid}, indent=2))
    time.sleep(1)
    if process.poll() is not None: raise RuntimeError(tr('H3 视频页启动失败，请查看 {0}/h3-server-control.log。', RUN_DIR))
    print(ok(tr('H3 视频页：http://127.0.0.1:8810/（生成视频前请先卸载 {0} 上的文字模型）', where(LOCAL))))


def open_page(node, path='/admin'):
    reach(node)
    if health(node) != 'up': raise RuntimeError(tr('{0} 的 oMLX 没有运行，请先启动服务。', where(node)))
    subprocess.run(['open', f"http://127.0.0.1:{NODES[node]['port']}{path}"])


# ---------- 被控方：rpc 入口调的函数（rpc.py）----------

def local_state(full=True):
    """rpc state：这台的原始状态，怎么算、怎么显示都在控制方。不含密钥：omlx_stats 已剔除，model_settings.json 里没有。
    full 才跑探测（内存、GPU、硬盘这些）；快速刷新不跑。"""
    conf, n = saved(), node_snapshot(LOCAL)
    out = {'node': n, 'omlx_stats': omlx_stats(LOCAL, n), 'engine': engine_settings(LOCAL), 'lock': lock_holder(), 'code': code_hash(),
           'conf': {k: conf.get(k, {}).get(LOCAL, {}) for k in ('contexts', 'options', 'applied', 'weights', 'warmed', 'cache_size')},
           'fallback': {name: conf[name] for name in (*MODELS, 'ssd_cache') if name in conf}}
    if not MANAGED_HERE: out['omlx_port'] = HERE_PORT   # 3a：官方版告诉主机自己的端口（自管照旧不报，主机按 8800）
    auto = {key: model['fact'] for key, model in AUTO.get(LOCAL, {}).items() if isinstance(model.get('fact'), dict)}
    if auto: out['auto'] = auto   # 第 4 期：这台 oMLX 列着、还没记进②的模型（认出来的事实，不含密钥），主机靠它列出这台的卡片
    if full:
        try: text = subprocess.run(['/bin/sh', '-c', probe_script(REMOTE_PARTS)], capture_output=True, text=True, errors='replace', timeout=15).stdout
        except subprocess.TimeoutExpired: text = ''
        out['stats'] = {k: v for k, v in parse_probe(text).items() if k != 'engine'}
        if official(LOCAL): out['stats'].update(official_cache())   # 官方版的缓存在它自己的目录、按它的接口（和 local_stats 一样）
    return out


def doctor_data():
    """rpc doctor-data：控制方体检要比对的那些数，脚本固定在这边，不接受对方传的内容。"""
    shell = lambda command: subprocess.run(['/bin/sh', '-c', command], capture_output=True, text=True, errors='replace', timeout=60).stdout.strip()
    if not MANAGED_HERE:
        # 这台是官方版或没装 oMLX（体检 O14）：自管才有的那些数（代码指纹、启动脚本、模型链接）这台没有，不报；只给控制方要看的几项
        info = engine_info()
        base = info.get('base')
        out = {'official': True, 'code': code_hash(), 'up': health(LOCAL) == 'up',
               'engine': {k: info.get(k) for k in ('kind', 'how_text', 'version', 'verdict', 'verdict_text', 'key')},
               'free': shutil.disk_usage(base).free if base and Path(base).is_dir() else None}
    else:
        out = {k: shell(v) for k, v in DOCTOR.items()}
        out.update(serve=hashlib.sha256(SERVE_SCRIPT.read_bytes()).hexdigest()[:12], code=code_hash(),
                   subkey=subkey_refused(LOCAL) if health(LOCAL) == 'up' else None)
    if NODES[LOCAL].get('interface'): out['ip'] = shell(f"ipconfig getifaddr {NODES[LOCAL]['interface']}")
    if len(addresses(LOCAL)) > 1: out['ips'] = {a['interface']: shell(f"ipconfig getifaddr {a['interface']}") for a in addresses(LOCAL)}
    return out


def activity():
    """rpc activity：运行窗口查这台用。进程只给下面这几类，命令行里的其他东西不外传。"""
    try: s = api(LOCAL, '/api/status', timeout=3)
    except Exception: s = None
    lines = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True, errors='replace').stdout.splitlines()
    procs = [line.strip() for line in lines if re.search(r'omlx[.]cluster[.]inference_worker|(^|/)mlx-serve(\s|$)|rdma_copy', line)]
    keep = ('active_requests', 'waiting_requests', 'models_loading', 'loaded_models')
    return {'status': {k: s.get(k) for k in keep} if s else None, 'procs': procs}


def rpc_subkey_list():
    return list_sub_keys()


def rpc_subkey_create(name, value):
    need_service()
    if sub_key(name): raise RuntimeError(tr('{0} 上已经有名为「{1}」的子密钥了。', where(LOCAL), name))
    api(LOCAL, '/admin/api/sub-keys', {'key': value, 'name': name}, timeout=30)
    return True


def rpc_subkey_revoke(name):
    entry = sub_key(name)
    if entry:
        need_service()
        api(LOCAL, '/admin/api/sub-keys', {'key': entry['key']}, method='DELETE', timeout=30)
    return bool(entry)


# ---------- 添加机器（控制方）和授权（被控方）----------
# 控制方 machine add 生成钥匙、配对串、核对码；被控方的人在自己的桌面上 grants allow（副机 这样不装应用的，把那一行亲手贴进
# authorized_keys）；控制方再 machine verify。没有任何 rpc 能增加授权，"允许"只存在于被控方本机。见草案 §2.3、已定 D1。
HOSTS_FILE = LMC_SSH / 'hosts.json'
HOST_KEY_FILE = Path(os.environ.get('LMC_HOST_KEY') or '/etc/ssh/ssh_host_ed25519_key.pub')  # 环境变量只给测试用
KEY_ID = r'[a-z0-9][a-z0-9-]{0,63}'
PUBKEY = r'ssh-ed25519 [A-Za-z0-9+/]{60,80}={0,2}'
PAIR_TITLE = NODE_FIELDS['title']
LEVEL_WORDS = {'call': tr('只调模型'), 'view': tr('只能看状态'), 'control': tr('能操作'), 'admin': tr('能管理（还能管子密钥、打开这台的 oMLX 管理页、移交主机）'), 'hub': tr('当主机用')}
GRANT_LINE = re.compile(r'restrict,port-forwarding,.*command="[^"]* --key-id (?P<id>' + KEY_ID + r') --level (?P<level>call|view|control|admin|hub)"\s+(?P<key>' + PUBKEY + r')\s+lmc:(?P=id)(?: (?P<title>.*))?')


def lmc_hosts():
    return json.loads(HOSTS_FILE.read_text()) if HOSTS_FILE.exists() else {}


def save_lmc_hosts(hosts):
    """hosts.json 和 ~/.ssh/lmc/config 一起写；config 由它生成，不手改。不动用户自己的 ~/.ssh/config。"""
    LMC_SSH.mkdir(parents=True, exist_ok=True)
    os.chmod(LMC_SSH, 0o700)
    HOSTS_FILE.write_text(json.dumps(hosts, indent=2, ensure_ascii=False))
    text = tr('# ModelTower生成，手改会被覆盖\n')
    for name, h in hosts.items():
        text += (f'Host lmc-{name}\n  HostName {h["ip"]}\n  User {h["user"]}\n  Port {h.get("ssh_port", 22)}\n  IdentityFile "{LMC_SSH / name}"\n'
                 f'  IdentitiesOnly yes\n  UserKnownHostsFile "{LMC_SSH / "known_hosts"}"\n  StrictHostKeyChecking yes\n  BatchMode yes\n'
                 f'  ConnectTimeout 3\n  ServerAliveInterval 5\n  ServerAliveCountMax 3\n  ExitOnForwardFailure yes\n'
                 f'  ControlPath "{control_path(name)}"\n')
    SSH_CONFIG.write_text(text)


def pin_host_key(ip, port):
    """记下对方的主机钥匙，之后连接都核对它。~/.ssh/known_hosts 里已有就用它（更早核对过），没有才现场取，由核对码兜底。"""
    host = ip if port == 22 else f'[{ip}]:{port}'
    known = subprocess.run(['ssh-keygen', '-F', host, '-f', str(Path.home() / '.ssh/known_hosts')], capture_output=True, text=True, errors='replace').stdout
    found = re.search(r'ssh-ed25519 \S+', known)
    if not found:
        scan = subprocess.run(['ssh-keyscan', '-T', '5', '-t', 'ed25519', '-p', str(port), ip], capture_output=True, text=True, errors='replace', timeout=15).stdout
        found = re.search(r'ssh-ed25519 \S+', scan)
    if not found: raise RuntimeError(tr('连不上 {0}，取不到它的主机钥匙。确认它开机、打开了远程登录、地址没写错。', ip))
    path = LMC_SSH / 'known_hosts'
    lines = [line for line in (path.read_text().splitlines() if path.exists() else []) if line.split(' ', 1)[0] != host]
    path.write_text('\n'.join(lines + [f'{host} {found[0]}']) + '\n')
    return found[0]


def pairing_code(pubkey, host_key):
    """12 位核对码，由控制方的钥匙和被控方的主机钥匙一起算出：两边屏幕上一样，就同时证明了钥匙没被调包、连的是对的机器。"""
    # 12 位：6 位的话，有心人几秒就能算出一把核对码相同的钥匙，配对串又是用户自己找地方传的
    digits = f"{int(hashlib.sha256((pubkey.strip() + chr(10) + host_key.strip()).encode()).hexdigest()[:16], 16) % 10**12:012d}"
    return f'{digits[:4]}-{digits[4:8]}-{digits[8:]}'


def grant_line(key_id, level, pubkey, title, python=None, entry=None):
    """authorized_keys 的一行。restrict 关掉一切，只开转发；permitlisten 指向绑不上的 1 号端口，堵住 -R（踩坑 #49）。
    强制命令是 rpc 入口，请求由它校验（scripts/rpc.py）。"""
    python, entry = python or here_python(), entry or ROOT / 'console/scripts/rpc.py'
    # 放行的端口：自管版只有现在这一个（和以前一个字不差）。官方版、还没装的，再加上官方默认的端口：
    # 配对时还没装 oMLX，授权行里只有 8800，装好官方版（8000）以后隧道会被拒，又没有地方重写这一行（第 4 期审查）
    ports = [HERE_PORT] if MANAGED_HERE else sorted({HERE_PORT, omlx_adapter.OFFICIAL_PORT})
    permit = ','.join(f'permitopen="127.0.0.1:{port}"' for port in ports)
    # 路径里有空格（新用户的运行目录在 Application Support 下）就加单引号；早期装法的路径没有空格，这一行和以前一个字不差
    return (f'restrict,port-forwarding,{permit},permitlisten="127.0.0.1:1",'
            f'command="{shlex.quote(str(python))} -E -s {shlex.quote(str(entry))} --key-id {key_id} --level {level}" {pubkey} lmc:{key_id} {title}')


def pair_string(key_id, title, pubkey):
    payload = json.dumps({'id': key_id, 'title': title, 'key': pubkey}, ensure_ascii=False).encode()
    return 'lmc1.' + base64.urlsafe_b64encode(payload).decode()


def parse_pair(text):
    """配对串来自别的机器，写进 authorized_keys 之前每个字段都按白名单查，防止夹带选项或换行。"""
    try:
        if not text.startswith('lmc1.') or len(text) > 1000: raise ValueError
        data = json.loads(base64.urlsafe_b64decode(text[5:].encode()))
        if not (re.fullmatch(KEY_ID, data['id']) and re.fullmatch(PUBKEY, data['key']) and re.fullmatch(PAIR_TITLE, data['title'])): raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RuntimeError(tr('配对串不对：请从对方的控制台重新复制。')) from exc
    return data


def cluster_rows():
    return json.loads(CLUSTER_FILE.read_text())


def save_cluster_rows(data):
    temporary = CLUSTER_FILE.with_name(CLUSTER_FILE.name + '.tmp')
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    os.replace(temporary, CLUSTER_FILE)


def machine_add(name, ip, user, title=None, as_json=False, by_password=False):
    """控制方第一步：生成这台专用的钥匙，给出配对串、核对码，和手工贴进 authorized_keys 用的那一行。对方还没允许之前机器不进机器表。
    没填显示名时，连上以后用那台的系统名称。by_password：不出配对串，改走密码配对（machine_pair_open）。"""
    import hub
    state = hub.load()
    if by_password and state: raise RuntimeError(tr('开了主机模式：加机器请用配对串（机器 → 添加机器 → 生成配对串），由对方在自己的屏幕上点允许。'))
    if state and state['hub'] != LOCAL: raise RuntimeError(tr('启用主机模式后，机器表由主机管：到主机上添加。'))
    # 和 hub.NODE_NAME、rpc.py 的白名单、界面的 Chip.machineNameMax 一样长：在这里先拦，不然收下以后启用主机模式才报"机器表不对"
    if len(name) > NAME_MAX: raise RuntimeError(tr('名字 {0} 太长：{1} 个字符，最多 {2} 个。', name, len(name), NAME_MAX))
    auto_title = not title
    title = title or name.capitalize()
    fields = {'name': name, 'title': title, 'ip': ip}
    wrong = [k for k, v in fields.items() if not re.fullmatch(NODE_FIELDS[k], v)]
    if name == 'all' or name == LOCAL or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', user): wrong.append(tr('name 或账号'))
    if wrong: raise RuntimeError(tr('这些字段不对：{0}。', tr('、').join(wrong)))
    hosts, port = lmc_hosts(), int(os.environ.get('LMC_SSH_PORT') or 22)
    if name in hosts and hosts[name].get('ip') != ip:
        raise RuntimeError(tr('{0} 这个名字已经给了 {1}。换个名字，或者先移除那台。', name, hosts[name].get('ip')))
    if state:
        # 主机模式：每台只授权主机钥匙。先记下要连哪里，对方允许后验证，带签名才进机器表
        if name in state['machines']: raise RuntimeError(tr('{0} 已经在机器表里了。', name))
        pubkey = ' '.join(hub.public_key().split()[:2])
        host_key = pin_host_key(ip, port)   # 连不上就到此为止，不留下一条等对方允许的记录
        code = pairing_code(pubkey, host_key)
        hub.remember_machine(LOCAL, name, {'title': title, 'ip': ip, 'user': user, 'ssh_port': port, 'host_key': host_key})
        pair, line = pair_string('hub', where(LOCAL), pubkey), grant_line('hub', 'hub', pubkey, 'hub')
        if as_json: print(json.dumps({'ok': True, 'name': name, 'code': code, 'pair': pair, 'line': line}, ensure_ascii=False))
        else: print(tr('核对码：{0}\n配对串（在 {1} 的控制台里点"允许新机器"，贴进去，核对码一样才点允许）：\n{2}\n\n对方允许后，在控制台里点验证（要安全芯片确认和最高密码）。', code, title, pair))
        return
    used = [n['port'] for n in NODES.values()] + [h['port'] for h in hosts.values() if 'port' in h]
    entry = {'ip': ip, 'user': user, 'title': title, 'auto_title': auto_title, 'ssh_port': port,
             'port': NODES[name]['port'] if name in NODES else max([8819, *used]) + 1,
             'key_id': hosts.get(name, {}).get('key_id') or (stable_key_id(name) if by_password else f"{LOCAL.replace('_', '-')}-{secrets.token_hex(3)}")}
    save_lmc_hosts({**hosts, name: entry})
    key = LMC_SSH / name
    if not key.exists():
        r = subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', f"lmc:{entry['key_id']}", '-f', str(key)], capture_output=True, text=True, errors='replace')
        if r.returncode: raise RuntimeError(tr('没能生成钥匙：{0}', r.stderr.strip()[:200]))
    pubkey = ' '.join(key.with_name(name + '.pub').read_text().split()[:2])
    if by_password: return machine_pair_open(name, entry, pubkey, as_json)
    code = pairing_code(pubkey, pin_host_key(ip, port))
    pair, line = pair_string(entry['key_id'], where(LOCAL), pubkey), grant_line(entry['key_id'], 'admin', pubkey, where(LOCAL))
    if as_json:
        print(json.dumps({'ok': True, 'name': name, 'code': code, 'pair': pair, 'line': line}, ensure_ascii=False))
        return
    print(tr('核对码：{0}\n配对串（在 {1} 的控制台里点"允许新机器"，贴进去，核对码一样才点允许）：\n{2}\n', code, title, pair))
    print(tr('{0} 不装控制台的话，在它的终端把下面这一行亲手加进 ~/.ssh/authorized_keys：\n{1}\n', title, line))
    print(tr('对方允许后运行：machine verify {0}', name))


# ---------- 密码配对（第 4 期，草案 11.3，已定第 13 条）----------
# 自己的机器：在主机上弹出的终端里输一次对方的 Mac 密码。密码直接交给系统的 ssh，控制台不经手、不落盘，原理同 ssh-copy-id；
# 但装过去的不是全权限的钥匙，是只能走 rpc 的受限授权行。登录后在对方那边跑下面这段：定运行目录、找 Python、放程序、加授权行。
PAIR_BOOT = r'''#!/bin/sh
# ModelTower: password pairing. Runs here when the host's owner logs in with this Mac's own password. It does four things
# only: pick the runtime folder, find a usable Python, put the console program in place (identical files are left alone), and add
# the host key's restricted line to authorized_keys (after a backup). Nothing else is touched.
set -e
S=/Users/Shared/mlx-cluster
if [ -n "$LMC_ROOT" ]; then D="$LMC_ROOT"
elif [ ! -L "$S" ] && [ -O "$S" ] && { [ -e "$S/cluster.json" ] || [ -d "$S/omlx-state" ]; }; then D="$S"
else D="$HOME/Library/Application Support/@@DATA_DIR@@"; fi
ok_python() { [ -x "$1" ] && "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1 </dev/null; }
# Order: this folder's own venv, then the system Python (present once the command line tools are installed), and only then the
# wrapper oMLX rewrites on every start (it stops working if oMLX is removed or moved).
PY=""
if ok_python "$D/.venv/bin/python"; then PY="$D/.venv/bin/python"
elif /usr/bin/xcode-select -p >/dev/null 2>&1 </dev/null && ok_python /usr/bin/python3; then PY=/usr/bin/python3
elif ok_python "$HOME/.omlx/bin/omlx-cluster-python"; then PY="$HOME/.omlx/bin/omlx-cluster-python"; fi
if [ -z "$PY" ]; then echo "@@pair-failed no-python"; exit 3; fi
mkdir -p "$D"
T="$D/console.pair.$$"
rm -rf "$T" "$T.tar"; mkdir -p "$T"
trap 'rm -rf "$T" "$T.tar"' EXIT
/usr/bin/base64 -D > "$T.tar" <<'LMC_PAIR_TAR'
@@TAR@@
LMC_PAIR_TAR
/usr/bin/tar -xf "$T.tar" -C "$T" </dev/null
# Run from the unpacked copy: the program in "$D/console" is replaced (file by file, atomically) only after the checks pass,
# so a refused pairing leaves this Mac's existing program untouched. -I keeps the home folder out of the import path.
LMC_LANG=@@LANG@@ "$PY" -I -B -c 'import sys; sys.path.insert(0, sys.argv[1]); sys.argv = ["clusterctl.py"] + sys.argv[2:]; import clusterctl; clusterctl.main()' "$T/scripts" grants install '@@PAIR@@' admin "$PY" </dev/null
'''
PAIR_REASONS = {
    'no-python': tr('那台上没找到能用的 Python：在那台打开一次 oMLX（它自带一个），或者在那台的终端运行 xcode-select --install 装苹果的命令行工具，再来配对。'),
    'has-manager': tr('那台现在由别的机器管理，一台同一时间只认一个主机：到它的控制台 机器 → 本机授权 吊销原来的，再来配对。'),
    'hub-mode': tr('那台开了主机模式：到它的控制台用配对串加入。'),
    'auth': tr('没连上，或密码不对：确认那台开机、打开了远程登录，地址和账号没写错。'),
    'perms': tr('那台的家目录或 ~/.ssh 文件夹别人也能写，系统不认里面的授权：在那台的终端里运行 chmod go-w ~ ~/.ssh，再来配对。'),
    'error': tr('没配成：终端窗口里有原因，照着处理后再点一次。'),
}


def stable_key_id(name):
    """密码配对用的钥匙编号：同一台主机配同一个名字，每次都一样。移除后再配、主机这边的钥匙丢了再配，对方认得出是同一个主机在换钥匙，
    不会被自己留下的旧授权行当成"别的主机"挡住（第 4 期审查）。由这台 Mac 的硬件编号、本机名和对方的名字一起算，只取 6 位，
    硬件编号本身不外传；读不到就退回随机的。"""
    try: out = subprocess.run(['/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'], capture_output=True, text=True, errors='replace', timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired): out = ''
    found = re.search(r'"IOPlatformUUID" = "([0-9A-Fa-f-]{36})"', out)
    tail = hashlib.sha256(f'{found[1]}:{LOCAL}:{name}'.encode()).hexdigest()[:6] if found else secrets.token_hex(3)
    return f"{LOCAL.replace('_', '-')}-{tail}"


def forget_host_key(ip, port):
    """把这个地址在控制台自己的 known_hosts 里记的指纹去掉：移除机器时、重新做密码配对前。不去掉的话，这个地址换了一台 Mac
    （或那台重装过）以后，ssh 直接拒绝连接，密码配对只会一直报"没连上"。重新配对时 ssh 会再问一次指纹。"""
    host, path = (ip if port == 22 else f'[{ip}]:{port}').lower(), LMC_SSH / 'known_hosts'
    try: lines = path.read_text().splitlines()
    except OSError: return
    kept = [line for line in lines if host not in line.split(' ', 1)[0].lower().split(',')]
    if len(kept) != len(lines): path.write_text('\n'.join(kept) + ('\n' if kept else ''))


def pair_files(name): return {kind: RUN_DIR / f'pair-{name}.{kind}' for kind in ('sh', 'command', 'out', 'result', 'id')}


def machine_pair_open(name, entry, pubkey, as_json=False):
    """生成对方那边要跑的脚本和这边的终端脚本，打开终端。对方的主机指纹这里不预先记：第一次连由 ssh 自己问一句（和平时用 ssh 一样），
    答了才往下输密码；记进控制台自己的 known_hosts，之后的连接都核对它。"""
    files = pair_files(name)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as tar:
        for relative in console_files(): tar.add(PROJECT / relative, arcname=str(relative))
    boot = (PAIR_BOOT.replace('@@DATA_DIR@@', brand.DATA_DIR).replace('@@LANG@@', lang()).replace('@@PAIR@@', pair_string(entry['key_id'], where(LOCAL), pubkey))
            .replace('@@TAR@@', base64.encodebytes(buffer.getvalue()).decode().strip()))
    for path in (files['out'], files['result']): path.unlink(missing_ok=True)
    forget_host_key(entry['ip'], entry['ssh_port'])
    token = secrets.token_hex(4)   # 这一次配对的编号：点了"不等了"又点一次时，旧终端窗口后写的结果不算数
    files['id'].write_text(token)
    files['sh'].write_text(boot)
    files['sh'].chmod(0o600)
    for folder in (LMC_SSH.parent, LMC_SSH): folder.mkdir(mode=0o700, exist_ok=True)
    command = ['ssh', '-F', 'none', '-o', 'PubkeyAuthentication=no', '-o', 'PreferredAuthentications=keyboard-interactive,password', '-o', 'NumberOfPasswordPrompts=3',
               '-o', 'StrictHostKeyChecking=ask', '-o', f"UserKnownHostsFile={LMC_SSH / 'known_hosts'}", '-o', 'HostKeyAlgorithms=ssh-ed25519', '-o', 'ConnectTimeout=10',
               '-p', str(entry['ssh_port']), f"{entry['user']}@{entry['ip']}", '/bin/sh -s']
    title = entry['title']
    intro = tr('添加 {0}：下面先确认一次它的指纹（第一次连才问，输 yes），再输入 {1} 上 {2} 这个账号的 macOS 登录密码。密码直接交给系统的 ssh，控制台不经手。', title, title, entry['user']) \
        + tr('配好后，这台能管理那台：启停服务、加载模型、关机、管子密钥、打开它的 oMLX 管理页；那台的控制台会被允许读它自己的 oMLX 主密钥（只在那台上用，不传到这台）。')
    good, bad = tr('配好了。回到控制台的"添加机器"页，它会自己验证（那一页关了的话，重新打开点"验证"）；这个窗口可以关了。'), tr('没配成。回到控制台看原因，改好后再点一次。')
    files['command'].write_text(f"""#!/bin/zsh
echo {shlex.quote(intro)}
echo
{shlex.join(command)} < {shlex.quote(str(files['sh']))} | tee {shlex.quote(str(files['out']))} | grep -v '^@@'
code=${{pipestatus[1]}}
print -r -- {token} $code > {shlex.quote(str(files['result']) + '.tmp')} && mv -f {shlex.quote(str(files['result']) + '.tmp')} {shlex.quote(str(files['result']))}
echo
if (( code == 0 )); then echo {shlex.quote(good)}; else echo {shlex.quote(bad)}; fi
""")
    files['command'].chmod(0o700)
    subprocess.run(['open', str(files['command'])], check=True, timeout=10)
    if as_json: print(json.dumps({'ok': True, 'name': name, 'terminal': True}, ensure_ascii=False))
    else: print(ok(tr('已打开终端：到那里输入 {0} 的 macOS 登录密码。配好后运行 machine verify {1}。', title, name)))


def machine_pair_status(name, as_json=False):
    """密码配对做到哪了：none 没发起、waiting 终端里还没做完、done 配好了（接着 machine verify）、failed 没配成（reason、message 说原因）。"""
    files, state, reason = pair_files(name), 'none', None
    if name in lmc_hosts() and files['command'].exists():
        state = 'waiting'
        stamp = (files['result'].read_text().split() if files['result'].exists() else []) + ['', '']
        if stamp[0] and files['id'].exists() and stamp[0] == files['id'].read_text().strip():
            code = stamp[1]
            text = files['out'].read_text(errors='replace') if files['out'].exists() else ''
            state = 'done' if code == '0' and '@@pair-ok' in text else 'failed'
            if state == 'failed':
                found = re.search(r'^@@pair-failed (\S+)', text, re.M)
                reason = found[1] if found and found[1] in PAIR_REASONS else 'auth' if code == '255' else 'error'
    out = {'ok': True, 'name': name, 'state': state, **({'reason': reason, 'message': PAIR_REASONS[reason]} if reason else {})}
    if as_json: print(json.dumps(out, ensure_ascii=False))
    else: print({'none': tr('还没发起配对。'), 'waiting': tr('终端里还没做完。'), 'done': tr('配好了，接着运行 machine verify {0}。', name)}.get(state) or PAIR_REASONS[reason])


def grants_install(pair, level='admin', python=None):
    """密码配对时在被控方跑：主机的人用这台的 Mac 密码登录进来，等于这台的主人亲自动手，所以不弹"允许"框（弹了也没人在这台前面点）。
    只认登录会话（有 SSH_CONNECTION）；受限钥匙的 rpc 里没有这条命令，所以已经配进来的机器不能用它给别人加授权。
    一台同一时间只认一个主机（已定第 16 条）：已经有别的机器能管理这台时不替换，停下，让人到这台上先吊销。"""
    if not os.environ.get('SSH_CONNECTION'):
        raise RuntimeError(tr('这条命令只在密码配对时由主机经登录会话调用。在这台自己的屏幕上请用"允许新机器"。'))
    info = parse_pair(pair)
    if info['id'] == 'hub' or level not in ('admin', 'control', 'view', 'call'): raise RuntimeError(tr('级别只能是 {0}。', tr('、').join(('admin', 'control', 'view', 'call'))))
    if python is not None and not (re.fullmatch(r"/[^\x00-\x1f\x7f\"'`$\\]+", python) and os.access(python, os.X_OK)): raise RuntimeError(tr('Python 的路径不对：{0}', python[:200]))
    import hub
    if hub.load():
        print('@@pair-failed hub-mode')
        raise RuntimeError(PAIR_REASONS['hub-mode'])
    for folder in (Path.home(), Path.home() / '.ssh'):   # sshd 默认不认"别人也能写"的家目录和 ~/.ssh 里的授权：配了也登不上，先说清
        if folder.exists() and folder.stat().st_mode & 0o022:
            print('@@pair-failed perms')
            raise RuntimeError(PAIR_REASONS['perms'])
    (Path.home() / '.ssh').mkdir(mode=0o700, exist_ok=True)
    lock = open(Path.home() / '.ssh' / '.lmc-authorized.lock', 'w')   # 两台主机同时配同一台：一个个来（进程结束时放开）
    fcntl.flock(lock, fcntl.LOCK_EX)
    lines = authorized_lines()
    same_id = lambda line: (m := GRANT_LINE.fullmatch(line.strip())) and m['id'] == info['id']
    managers = [m['title'] or m['id'] for line in lines if (m := GRANT_LINE.fullmatch(line.strip())) and m['id'] != info['id'] and info['key'] not in line and m['level'] in ('admin', 'hub')]
    if managers and level == 'admin':
        print('@@pair-failed has-manager')
        raise RuntimeError(tr('这台现在由「{0}」管理，一台同一时间只认一个主机：到这台的控制台 机器 → 本机授权 吊销它，再来配对。', tr('、').join(managers)))
    deploy_console()
    write_authorized([line for line in lines if info['key'] not in line and not same_id(line)]
                     + [grant_line(info['id'], level, info['key'], info['title'], python=Path(python) if python else None, entry=ROOT / 'console/scripts/rpc.py')])
    if not MANAGED_HERE and key_consent() is None and level == 'admin':
        # 这台的主人用自己的密码配进来的：一并允许这台的控制台读它自己的 oMLX 主密钥（只在这台上用，不传给主机）。
        # 没装应用的副机没有地方点"允许"，不允许的话主机在它上面什么都加载不了。已经表过态（允许或不允许）的不动
        save_setting('engine_key', True)
    print('@@pair-ok')
    print(ok(tr('已允许「{0}」{1}这台。', info['title'], LEVEL_WORDS[level])))


def system_title(computer, fallback):
    """对方的系统名称能不能直接当显示名：有引号、$、断行这些，或太长，就用原来的。"""
    return computer if computer and len(computer) <= 40 and re.fullmatch(NODE_FIELDS['title'], computer) else fallback


def machine_verify(name):
    """控制方第二步：试连。通了，新机器进机器表；已在表里的老机器（副机）只报告，切换用 machine use。"""
    import hub
    if hub.load():
        password = hub.read_password()
        missed = hub.add_machine(LOCAL, name, password, hub.read_proof())
        print(ok(tr('已添加 {0}，控制台下次刷新就能看到它的卡片。', name)) + (warn(tr('这些机器这次没通知到：{0}，主机之后会补。', tr('、').join(missed))) if missed else ''))
        return
    hosts = lmc_hosts()
    if name not in hosts: raise RuntimeError(tr('还没有添加 {0}：先运行 machine add。', name))
    r = subprocess.run(['ssh', '-F', str(SSH_CONFIG), f'lmc-{name}', 'ping'], capture_output=True, text=True, errors='replace', timeout=20, stdin=subprocess.DEVNULL)
    if r.returncode == 3:
        print(ok(tr('{0} 允许了这把钥匙，但只能开隧道调模型，不进控制台。', hosts[name]['title'])))
        return
    if r.returncode == 255:
        detail = tr('对方还没有允许这把钥匙') if 'Permission denied' in r.stderr else r.stderr.strip()[:200] or tr('连不上')
        raise RuntimeError(tr('{0} 连不通：{1}。', hosts[name]['title'], detail))
    if r.returncode: raise RuntimeError(tr('{0} 回了错误：{1}', hosts[name]['title'], r.stdout.strip()[:200]))
    info = json.loads(r.stdout)
    if info.get('proto') != RPC_PROTO: raise RuntimeError(tr('{0} 的控制台程序版本不同（协议 {1}，这台是 {2}），先更新它。', hosts[name]['title'], info.get('proto'), RPC_PROTO))
    if info['code'] != code_hash(): print(warn(tr('{0} 的控制台程序和这台不是同一版：能用，但建议更新。', hosts[name]['title'])))
    if name in NODES:
        print(ok(tr('{0} 的受限钥匙连通了。切换用 machine use {1} rpc。', where(name), name)))
        return
    data, h = cluster_rows(), hosts[name]
    title = system_title(info.get('computer'), h['title']) if h.get('auto_title') else h['title']
    row = {'name': name, 'title': title, 'role': '副机', 'port': h['port'], 'ssh': f'lmc-{name}', 'transport': 'rpc'}
    data['nodes'].append(row)
    save_cluster_rows(data)
    print(ok(tr('已添加 {0}，控制台下次刷新就能看到它的卡片。', title)))


def machine_rename(name, title):
    """改名只在主机上做（用户 11-04 定）。dsh 配置里的显示名不跟着改，界面提示要用原来的名字，所以记一份。"""
    if name not in NODES: raise RuntimeError(tr('没有这台：{0}。', name))
    if not re.fullmatch(NODE_FIELDS['title'], title) or len(title) > 40: raise RuntimeError(tr('名字不能带引号、反斜杠、$，也不能超过 40 个字。'))
    import hub
    data = hub.load()
    if data and data['hub'] != LOCAL: raise RuntimeError(tr('改名要在主机上做。'))
    rows = cluster_rows()
    row = next(r for r in rows['nodes'] if r['name'] == name)
    row.setdefault('dsh_title', row['title'])
    row['title'] = title
    save_cluster_rows(rows)
    hosts = lmc_hosts()
    if name in hosts: save_lmc_hosts({**hosts, name: {**hosts[name], 'title': title}})
    if data: hub.rename(LOCAL, name, title, row['dsh_title'])
    print(ok(tr('已改名为 {0}。dsh 里显示的名字不变，要改到 dsh 的配置里改。', title) if has_dsh() else tr('已改名为 {0}。', title)))


def machine_probe(ip):
    """猜这个地址的机器叫什么：问局域网的 mDNS。猜不到就空着，连上以后用它的系统名称。"""
    if not re.fullmatch(NODE_FIELDS['ip'], ip): raise RuntimeError(tr('地址不对。'))
    try: out = subprocess.run(['dig', '-x', ip, '@224.0.0.251', '-p', '5353', '+short', '+time=1', '+tries=1'], capture_output=True, text=True, errors='replace', timeout=4).stdout
    except (OSError, subprocess.TimeoutExpired): out = ''
    first = out.split('\n')[0].strip().removesuffix('.').removesuffix('.local')
    print(json.dumps({'title': first if re.fullmatch(NODE_FIELDS['title'], first or ' ') and first else ''}, ensure_ascii=False))


def machine_use(name, how, assume_yes=False):
    """老机器在两条路之间切换（副机 换钥匙、回滚都用它）。切换要断一下隧道，有请求时不切。"""
    if name not in remotes() or how not in ('rpc', 'shell'): raise RuntimeError(f"machine use {'|'.join(remotes())} rpc|shell")
    data = cluster_rows()
    row = next(r for r in data['nodes'] if r['name'] == name)
    if (how == 'rpc') == (row.get('transport') == 'rpc'): return print(off(tr('{0} 本来就是这样。', where(name))))
    if how == 'rpc' and name not in lmc_hosts(): raise RuntimeError(tr('还没有添加 {0}：先运行 machine add。', name))
    if how == 'shell' and not row.get('legacy_ssh'): raise RuntimeError(tr('{0} 没有记下原来的 SSH 别名，回不去。', where(name)))
    counts = requests_now(name)
    if (counts is None or sum(counts.values())) and not assume_yes: raise RuntimeError(tr('{0} 现在有请求或读不到请求数，切换会打断隧道。答完再切，确认无妨加 --yes。', where(name)))
    stop_tunnel(name)
    if how == 'rpc':
        row.update(legacy_ssh=row['ssh'], ssh=f'lmc-{name}', transport='rpc')
    else:
        row['ssh'] = row.pop('legacy_ssh')
        row.pop('transport')
    save_cluster_rows(data)
    print(ok(tr('{0} 现在走{1}；下次刷新时重开隧道。', where(name), (tr('受限钥匙和 rpc') if how == 'rpc' else tr('原来的全权限 SSH')))))


def machine_remove(name, assume_yes=False):
    import hub
    state = hub.load()
    if state:
        if state['hub'] != LOCAL: raise RuntimeError(tr('启用主机模式后，机器表由主机管：到主机上移除。'))
        if name not in state['machines'] and name in hub.pending_machines():
            hub.forget_machine(name)
            print(ok(tr('已取消添加 {0}。', name)))
            return
        password = hub.read_password()
        missed = hub.remove_machine(LOCAL, name, password, hub.read_proof())
        if name in NODES: stop_tunnel(name)
        print(ok(tr('已移除 {0}。它那边的授权和加密钥匙还留着：到那台的 设置 → 安全 点退出主机模式。机器丢了或卖了，还要在 设置 → 安全 吊销它的安全芯片钥匙。', name)) + (warn(tr('这些机器这次没通知到：{0}，主机之后会补。', tr('、').join(missed))) if missed else ''))
        return
    hosts = lmc_hosts()
    if name == LOCAL or (name not in hosts and name not in NODES): raise RuntimeError(tr('没有可以移除的 {0}。', name))
    if name in NODES and not is_rpc(name): raise RuntimeError(tr('{0} 还在走全权限 SSH，不从这里移除。', where(name)))
    title = hosts.get(name, {}).get('title', name)
    if not (assume_yes or ask(tr('从这台的控制台移除 {0}，并删除连它的钥匙？它上面的模型和服务不受影响。[y/N] ', title))):
        print(tr('已取消。'))
        return
    if name in NODES:
        stop_tunnel(name)
        data = cluster_rows()
        data['nodes'] = [r for r in data['nodes'] if r['name'] != name]
        save_cluster_rows(data)
    save_lmc_hosts({k: v for k, v in hosts.items() if k != name})
    for path in (LMC_SSH / name, LMC_SSH / f'{name}.pub', *pair_files(name).values()): path.unlink(missing_ok=True)
    if hosts.get(name, {}).get('ip'): forget_host_key(hosts[name]['ip'], hosts[name].get('ssh_port', 22))
    print(ok(tr('已移除 {0}。{1} 那边的授权还留着，要在它的控制台吊销，或由 {2} 的主人删除那一行。', title, title, title)))


SYNC_ROW = """import json, os, sys, time
path, name, rows = sys.argv[1], sys.argv[2], json.load(sys.stdin)
text = open(path).read()
data = json.loads(text)
row = next(r for r in data['nodes'] if r['name'] == name)
open(path + '.bak-' + time.strftime('%Y%m%d-%H%M%S'), 'w').write(text)
row.update(ip=rows[0]['ip'], interface=rows[0]['interface'], link=rows[0]['link'], addresses=rows)
open(path + '.tmp', 'w').write(json.dumps(data, indent=2, ensure_ascii=False) + '\\n')
os.replace(path + '.tmp', path)
print('ok')
"""


def machine_addresses(name, as_json=False):
    if name not in NODES: raise RuntimeError(tr('没有这台：{0}。', name))
    rows = addresses(name)
    if as_json: print(json.dumps({'name': name, 'addresses': coded(rows), 'now': address_now(name), 'max': ADDRESS_MAX}, ensure_ascii=False))
    else:
        for i, a in enumerate(rows, 1): print(tr('  {0} {1}（{2} {3}）', i, a['ip'], tr(a['link']), a['interface']))


def machine_set_addresses(name, text):
    """改一台的地址列表（标准输入给 JSON）：网线在前。旧字段 ip / interface / link 跟着第一项，旧版控制台照样能读。
    改的是远程那台，再经 SSH 改它自己机器表里它那一行（D3），先备份；走受限钥匙的要在它自己的控制台改。"""
    import hub
    if name not in NODES: raise RuntimeError(tr('没有这台：{0}。', name))
    if hub.load(): raise RuntimeError(tr('启用主机模式后，地址由主机管，暂时不能在控制台里改。'))
    try: rows = json.loads(text)
    except ValueError: raise RuntimeError(tr('输入不是 JSON。'))
    if isinstance(rows, list): rows = [{**a, 'link': link_value(a.get('link'))} if isinstance(a, dict) and 'link' in a else a for a in rows]   # 应用传代号
    problem = address_problem(rows)
    if problem: raise RuntimeError(tr('地址没改：{0}。', problem))
    rows = [{'ip': a['ip'], 'interface': a['interface'], 'link': a['link']} for a in rows]
    data = cluster_rows()
    shutil.copy2(CLUSTER_FILE, CLUSTER_FILE.with_name(f"{CLUSTER_FILE.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
    row = next(r for r in data['nodes'] if r['name'] == name)
    row.update(ip=rows[0]['ip'], interface=rows[0]['interface'], link=rows[0]['link'], addresses=rows)
    save_cluster_rows(data)
    ADDRESS_FILE.unlink(missing_ok=True)   # 重新挑
    print(ok(tr('{0} 的地址改好了：{1}。', where(name), tr('、').join(a['ip'] for a in rows))))
    if is_local(name): return
    if is_rpc(name): return print(off(tr('{0} 是配对加入的，它的地址要到它自己的控制台改。', where(name))))
    try: r = ssh_to(name, f"{ROOT}/.venv/bin/python -I -c {shlex.quote(SYNC_ROW)} {shlex.quote(str(CLUSTER_FILE))} {shlex.quote(name)}", timeout=20, data=json.dumps(rows))
    except subprocess.TimeoutExpired: r = None
    if r and r.returncode == 0 and r.stdout.strip() == 'ok': print(ok(tr('{0} 自己的机器表也改了（改之前备份了一份）。', where(name))))
    else: print(warn(tr('{0} 自己的机器表没改成（{1}），它的体检会对不上；连上以后再保存一次。', where(name), (r.stderr.strip()[:120] if r else tr('超时')) or tr('连不上'))))


PORTS_SCRIPT = ("networksetup -listallhardwareports | awk -F': ' '/^Hardware Port/{p=$2} /^Device/{print $2 \"|\" p}' | "
                'while IFS="|" read d p; do echo "$d|$p|$(ipconfig getifaddr $d)"; done')


def machine_ports(name):
    """这台有哪些网口、现在各是什么地址，给填地址的人参考。只列网线、Wi-Fi 和有地址的。"""
    if name not in NODES: raise RuntimeError(tr('没有这台：{0}。', name))
    if is_rpc(name): raise RuntimeError(tr('{0} 是配对加入的，这里查不了它的网口；到它自己的控制台看。', where(name)))
    r = run_on(name, posix(PORTS_SCRIPT) if not is_local(name) else PORTS_SCRIPT, timeout=20)
    if r.returncode: raise RuntimeError(offline(name))
    ports = []
    for line in r.stdout.splitlines():
        device, _, rest = line.partition('|')
        port, _, ip = rest.partition('|')
        ip = '' if ip.strip().startswith('169.254.') else ip   # 自分配的地址连不了别的机器
        if re.fullmatch(NODE_FIELDS['interface'], device) and (ip.strip() or port in ('Ethernet', 'Wi-Fi')):
            link = LINK_VALUES[1] if port == 'Wi-Fi' else LINK_VALUES[0] if 'Ethernet' in port else LINK_VALUES[2] if 'Thunderbolt' in port else port
            ports.append({'interface': device, 'port': port, 'link': link_code(link), 'ip': ip.strip()})
    print(json.dumps({'name': name, 'ports': ports}, ensure_ascii=False))


def machine_list(as_json=False):
    hosts = lmc_hosts()
    rows = [{'name': n, 'title': m['title'], 'role': tr(m['role']), 'local': is_local(n), 'transport': 'local' if is_local(n) else m.get('transport', 'shell')} for n, m in NODES.items()]
    import hub
    pending = [{'name': n, 'title': h.get('title', n), 'ip': h.get('ip', '')} for n, h in {**hosts, **hub.pending_machines()}.items() if n not in NODES and isinstance(h, dict)]
    if as_json: print(json.dumps({'machines': rows, 'pending': pending}, ensure_ascii=False))
    else:
        for r in rows: print(tr('{0}（{1}）{2}', r['title'], r['role'], (tr('本机') if r['local'] else {'rpc': tr('受限钥匙'), 'shell': tr('全权限 SSH')}[r['transport']])))
        for r in pending: print(tr('{0}（{1}）已生成配对串，等对方允许', r['title'], r['ip']))


def deploy_console():
    """把 rpc 要用的文件放到运行根的 console/：强制命令指向那里。项目可能在"桌面"文件夹，SSH 进来的进程没有权限读。"""
    target = ROOT / 'console'
    if PROJECT == target: return
    for relative in console_files():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_bytes((PROJECT / relative).read_bytes())
        os.replace(temporary, path)


def machine_install(name, via, assume_yes=False):
    """维护通道：经全权限 SSH 别名 via，把控制台程序装到被控方的 console/，写它自己的机器表，迁移它的设置（D5）。
    机器表和设置文件对方已有就不动。动手前要用户授权；只在换钥匙之前用。"""
    if name not in NODES or is_local(name) or not re.fullmatch(NODE_FIELDS['ssh'], via): raise RuntimeError(tr('machine install {0} <全权限 SSH 别名>', '|'.join(remotes())))
    if not (assume_yes or ask(tr('把控制台程序装到 {0}（经 {1}）？会往它的运行目录写程序文件；它已有的机器表和设置不动。[y/N] ', where(name), via))):
        print(tr('已取消。'))
        return
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as tar:
        for relative in console_files(): tar.add(PROJECT / relative, arcname=str(relative))
    target, mine = ROOT / 'console', NODES[name]
    r = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', via, f'mkdir -p "{target}" && tar -xf - -C "{target}"'], input=buffer.getvalue(), capture_output=True, timeout=120)
    if r.returncode: raise RuntimeError(tr('没能把程序装到 {0}：{1}', where(name), r.stderr.decode(errors='replace')[:200]))
    row = {k: mine[k] for k in ('name', 'title', 'role', 'ip', 'interface', 'link', 'addresses', 'mdns') if k in mine}
    table = json.dumps({'nodes': [{**row, 'port': mine.get('omlx_port', OMLX_PORT)}]}, indent=2, ensure_ascii=False) + '\n'
    conf = saved()
    theirs = {k: {name: conf.get(k, {}).get(name, {})} for k in ('contexts', 'options', 'applied', 'weights', 'warmed')}
    if name in conf.get('cache_size', {}): theirs['cache_size'] = {name: conf['cache_size'][name]}
    theirs.update({k: conf[k] for k in (*MODELS, 'ssd_cache', 'memory_alert_percent') if k in conf})
    for path, content in ((ROOT / 'cluster.json', table), (SETTINGS, json.dumps(theirs, indent=2))):
        r = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', via, f'[ -e "{path}" ] && echo kept || {{ cat > "{path}" && echo written; }}'], input=content, capture_output=True, text=True, errors='replace', timeout=30)
        if r.returncode: raise RuntimeError(tr('没能写 {0} 的 {1}：{2}', where(name), path.name, r.stderr[:200]))
        print(tr('{0}：{1}', path.name, (tr('已写') if 'written' in r.stdout else tr('对方已有，没动'))))
    r = subprocess.run(['ssh', '-o', 'BatchMode=yes', via, f'cd "{target}" && {ROOT}/.venv/bin/python -E -s -c "import sys; sys.path.insert(0, \'scripts\'); import clusterctl as c; print(c.code_hash(), c.LOCAL)"'], capture_output=True, text=True, errors='replace', timeout=30)
    if r.stdout.split()[:2] != [code_hash(), name]: raise RuntimeError(tr('{0} 上的程序自查不对：{1}', where(name), (r.stdout + r.stderr)[:300]))
    print(ok(tr('{0} 的控制台程序已装好（{1}），它还在走原来的路，等授权后切换。', where(name), code_hash())))


def authorized_path(): return Path.home() / '.ssh/authorized_keys'


def authorized_lines():
    path = authorized_path()
    return path.read_bytes().decode().rstrip('\n').split('\n') if path.exists() and path.stat().st_size else []


def write_authorized(lines):
    """先留一份当天的备份，再整体替换，权限 600。"""
    path = authorized_path()
    path.parent.mkdir(mode=0o700, exist_ok=True)
    backup = path.with_name(f"authorized_keys.bak-{time.strftime('%Y%m%d')}")
    if path.exists() and not backup.exists(): shutil.copy2(path, backup)
    temporary = path.with_name(f'.authorized_keys.{os.getpid()}.tmp')
    temporary.write_text('\n'.join(lines) + '\n')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def grants(as_json=False):
    rows, others = [], 0
    for line in authorized_lines():
        m = GRANT_LINE.fullmatch(line.strip())
        if m: rows.append({'id': m['id'], 'level': m['level'], 'title': m['title'] or m['id']})
        elif line.strip() and not line.lstrip().startswith('#'): others += 1
    if as_json: print(json.dumps({'grants': rows, 'others': others}, ensure_ascii=False))
    else:
        for r in rows: print(tr('{0}（{1}）  编号 {2}', r['title'], LEVEL_WORDS[r['level']], r['id']))
        print(tr('另外还有 {0} 行不是控制台加的（全权限，比如维护通道）。', others) if others else '')
        if not rows: print(tr('还没有授权给别的机器。'))


def confirm_on_screen(text):
    """弹系统对话框，只有坐在这台前面的人能点。SSH 进来的会话弹不出来，所以远程走不通这一步；Claude 也不点（已定 D1）。"""
    cancel, allow, title = tr('取消'), tr('允许'), tr('ModelTower')
    script = f'display dialog {json.dumps(text, ensure_ascii=False)} buttons {{"{cancel}", "{allow}"}} default button "{cancel}" with title "{title}" with icon caution giving up after 300'
    r = subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True, text=True, errors='replace')
    return r.returncode == 0 and f'button returned:{allow}' in r.stdout and 'gave up:true' not in r.stdout


def grants_allow(pair, level='admin'):
    info = parse_pair(pair)
    is_hub_key = info['id'] == 'hub'
    if is_hub_key: level = 'hub'   # 主机钥匙：每台只认它，级别固定，下面的对话框写明白
    if level not in LEVEL_WORDS: raise RuntimeError(tr('级别只能是 {0}。', tr('、').join(LEVEL_WORDS)))
    code = pairing_code(info['key'], ' '.join(HOST_KEY_FILE.read_text().split()[:2]))
    title = info['title']
    same_id = lambda line: (m := GRANT_LINE.fullmatch(line.strip())) and m['id'] == info['id']
    existing = [line for line in authorized_lines() if same_id(line) and info['key'] not in line]
    head = tr('让「{0}」当这台的主机吗？它能启停、加载、改设置，关机、移交这些最高操作还要安全芯片确认。', title) if is_hub_key else tr('允许「{0}」{1}这台吗？', title, LEVEL_WORDS[level])
    warning = tr('\n\n这会替换这台现在的主机钥匙，原来的主机就连不上这台了。') if is_hub_key and existing else ''
    if not confirm_on_screen(tr('{0}{1}\n\n核对码 {2}：要和「{3}」屏幕上的一样，不一样就点取消。', head, warning, code, title)):
        raise RuntimeError(tr('没有增加授权。'))
    deploy_console()
    if is_hub_key:
        import hub
        hub.clear_left()
    write_authorized([line for line in authorized_lines() if info['key'] not in line and not same_id(line)]
                     + [grant_line(info['id'], level, info['key'], title, entry=ROOT / 'console/scripts/rpc.py')])
    print(ok(tr('已允许「{0}」{1}这台。回到它的控制台，点"验证"。', title, LEVEL_WORDS[level])))


def disconnect_sessions():
    """断开这台上所有经 SSH 钥匙进来的非终端连接（隧道、rpc、复用连接）。不区分钥匙，最简单可靠；别的控制方会自动重连。返回断了几条。"""
    me, parents, cut = os.environ.get('USER') or subprocess.run(['id', '-un'], capture_output=True, text=True, errors='replace').stdout.strip(), {os.getpid(), os.getppid()}, 0
    for line in subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True, errors='replace').stdout.splitlines():
        pid, _, command = line.strip().partition(' ')
        if re.fullmatch(rf'sshd(-session)?: {re.escape(me)}@notty', command.strip()) and int(pid) not in parents:
            try: os.kill(int(pid), signal.SIGTERM); cut += 1
            except OSError: pass
    return cut


def grants_revoke(key_id, assume_yes=False):
    """吊销：删掉那一行，再断开已有连接。sshd 只在握手时查钥匙，光删行不断连接，隧道还通（踩坑 #50）。"""
    lines = authorized_lines()
    found = [line for line in lines if (m := GRANT_LINE.fullmatch(line.strip())) and m['id'] == key_id]
    if not found: raise RuntimeError(tr('没有编号 {0} 的授权。', key_id))
    title = GRANT_LINE.fullmatch(found[0].strip())['title'] or key_id
    if not (assume_yes or ask(tr('吊销「{0}」？它连这台的隧道和控制会立刻断开；别的控制方也会断一下，自动重连，正在流式输出的请求会中断。[y/N] ', title))):
        print(tr('已取消。'))
        return
    write_authorized([line for line in lines if line not in found])
    print(ok(tr('已吊销「{0}」，断开了 {1} 条连接。', title, disconnect_sessions())))


def help_text():
    """菜单 h 的说明。机器和模型从机器表、档案取，数字键和 menu_text() 一致。"""
    local, others = where(LOCAL), [where(n) for n in remotes()]
    keys, first = [], 1
    for i, (node, names) in enumerate(menu_lists()):
        count = len(names)
        keys.append(tr('{0} 用 ', where(node)) + ' / '.join(f'{first + j} {MODELS[t]["title"]}' for j, t in enumerate(names)) + tr(' / {0} 卸载', (first + count)) if i == 0
                    else tr('{0} 用 {1}–{2} 加载、{3} 卸载', where(node), first, (first + count - 1), (first + count)))
        first += count + 1
    ssd_word = {'locked': tr('锁定为开'), 'free': tr('可选'), 'na': tr('不适用')}  # 也是说明里的先后
    ssd = tr('，').join(f"{MODELS[n]['title']} {ssd_word[option_mode(n, 'ssd')]}" for n in sorted(MODELS, key=lambda n: list(ssd_word).index(option_mode(n, 'ssd'))))
    defaults = tr('、').join(f"{m['title']} {ctx_label(m['context'])}" for m in MODELS.values())
    sources = tr('和').join(tr('「oMLX · {0}」（127.0.0.1:{1}', where(n), NODES[n]['port']) + (tr('）') if is_local(n) else tr('，经隧道）')) for n in NODES)
    return tr('\n【怎么用】\n  · {0}（{1}）各有一个 oMLX 服务，各自单机跑模型。按 s 启动{2}的服务（不会自动加载模型），\n    不用了按 x 一键关闭（先卸载模型）。\n  · 每台通常放一个模型：{3}。\n    那台已经有一个在线时再选另一个：放得下就一起跑，放不下会先问你，确认后卸载前一个再加载。同一个模型可以{4}都开。\n  · 每个模型两个开关（c）：MTP（先猜后核对，答案质量不变，写代码快约 40–90%）、表放 SSD（{5}）。{6}各设各的。都要重新加载模型才生效，只下线那台的这个模型（约 1 分钟），\n    服务、控制台、dsh 都不用重启；模型正在跑时会问你"切换"（立刻中断正在答的请求）、"稍后（下次加载生效）"还是取消。\n  · 0 退出控制台：服务和模型保持原样。\n【状态说明】\n  ● 正常  ○ 未启动/未加载  ▲ 需要注意  ✕ 出错\n  · {7}。\n  · Ollama 只在 {8} 上，和 {9} 的 oMLX 共用内存；放不下时控制台会询问是否让它先释放。\n    已用（和活动监视器同一算法，含 Ollama）超过安全线（总内存的 90%），或系统内存压力变高时，出现红色的"内存超线"；\n    还没超、但对话塞满上下文后会超时，出现黄色的"内存提醒"。\n  · 上下文{10}各设各的（默认 {11}）；越大能塞的资料越多，但塞满时首字要等得久。\n    改了以后当场生效，不用重新加载；选项里写了推荐、压缩点和实测情况，超内存安全线的会标出来。\n  · 多轮缓存（c）{12}各设各的：关、50、100（推荐）、200、500 GB。同一份资料接着问时首字从几十秒降到几秒；\n    代价是资料的中间结果会留在那台的硬盘上。改了不用重启服务，那台的模型会卸载，再加载回来。\n【客户端接入（dsh）】\n  · dsh 里有{13} oMLX 来源：{14}。\n    按"机器 · 模型"选；选到那台没加载的模型会报"找不到模型"，到这里加载后再用。\n  · 密钥（k）：{15}共用一把主密钥和同一组子密钥；每个客户端各用一把子密钥。复制后 30 秒自动清空剪贴板。\n  · {16}的服务都只监听各自的本机，局域网其他设备访问不到。\n【出问题时】\n  · 先按 r 刷新，再按 l 看日志、d 体检；{17}\n', EVERY, tr('、').join(f"{where(n)} {tr(NODES[n]['role'])}" for n in NODES), EVERY, tr('；').join(keys), EVERY, ssd, EVERY, tr('；').join(m['help'] for m in MODELS.values()), local, local, EVERY, defaults, EVERY, EVERY.replace('两台', '两个').replace('这台', '一个').replace(' 台', ' 个'), sources, EVERY, EVERY, (tr('、').join(others) + tr(' 连不上多半是没开机、没登录或网络断了。') if others else ''))


PAGE_KEYS = 'ajmnpqtuwyz'  # 各台管理页的菜单键，按机器表的顺序取；a、j 是原来 主机、副机 的


def menu_lists():
    """终端菜单每台列哪些模型：这台有的（2b 每台一份清单），按顺序编号。"""
    out = []
    for node in NODES:
        with models_of(node): out.append((node, node_has(node)))
    return out


def scoped(node, action):
    """菜单里按某台做事：换成那台的清单再做。"""
    def go():
        with models_of(node): return action()
    return go


def menu_text():
    rows, number = [], 1
    for node, names in menu_lists():
        items = [tr('{0} 加载 {1}', (number + i), MODELS[name]['title']) for i, name in enumerate(names)] + [tr('{0} 卸载', (number + len(names)))]
        rows.append(f" {where(node):<6} " + '   '.join(items))
        number += len(names) + 1
    pages = '   '.join(tr('{0} {1} 管理页', key, where(node)) for key, node in zip(PAGE_KEYS, NODES))
    if has_h3() and has_ollama():
        return (tr(' 服务   s 启动{0}的 oMLX          x 关闭{1}的 oMLX（先卸载模型）\n', EVERY, EVERY) + '\n'.join(rows) + tr('\n 其他   k 密钥   c 上下文/开关/缓存   l 日志   {0}   v H3 视频页\n        o 让 Ollama 释放内存   d 一键体检   r 刷新   h 说明   0 退出（服务和模型保持原样）', pages))
    extra = '   '.join(([tr('v H3 视频页')] if has_h3() else []) + ([tr('o 让 Ollama 释放内存')] if has_ollama() else []))   # 2b：检测到才列
    return (tr(' 服务   s 启动{0}的 oMLX          x 关闭{1}的 oMLX（先卸载模型）\n', EVERY, EVERY) + '\n'.join(rows) + tr('\n 其他   k 密钥   c 上下文/开关/缓存   l 日志   {0}\n        {1}d 一键体检   r 刷新   h 说明   0 退出（服务和模型保持原样）', pages, extra + '   ' if extra else ''))


def menu_actions():
    actions, number = {'s': serve, 'x': shutdown, 'k': key_menu, 'c': choose_context, 'l': logs, 'v': ensure_h3,
                       'o': release_ollama, 'd': doctor, 'h': lambda: print(help_text())}, 1
    for node, names in menu_lists():
        for name in names:
            actions[str(number)] = scoped(node, lambda name=name, node=node: start(name, node))
            number += 1
        actions[str(number)] = lambda node=node: unload_all(node)
        number += 1
    for key, node in zip(PAGE_KEYS, NODES): actions[key] = lambda node=node: open_page(node)
    return actions


_NAMES = '|'.join(MODELS)
_WARM = '|'.join(n for n, m in MODELS.items() if m.get('warmup'))
USAGE = tr('命令行（ModelTower应用也通过它操作，逻辑只有这一份）。机器：{0} | all，不写就是 all：\n  status [--json] [--no-worker]          状态；--json 给应用用，不含密钥\n  poweroff <机器>|all [--yes]            先停 oMLX 再关机；all 是全部关机（只在主机上，副机先关）；开机只能到机器上按电源键\n  unlock <机器>                          打开终端，经 SSH 解锁开机后等待中的机器（密码直接输给系统）\n  autostart [机器]                       连得上、服务没开的都启动，不加载模型（打开控制台时用）；写了副机是它刚回来时用\n  up [机器] / down [机器] / cycle [机器] [--reload]   启动、关闭、重启 oMLX，副机的控制台跟着开关\n  admin-session <机器>   给应用用：换一个管理页会话\n  prepare   这台 Mac 的准备情况（远程登录、断电自动开机、防休眠、允许关机）\n  service show | write                   设备服务（隧道重连、主机同步）的常驻项；只生成文件，由用户本人加载\n  hub view | init | line | join | password | candidates | handover <机器> | apply | approve <机器> | reject <机器> | takeover [--force] | sync\n                                         主机角色（见 scripts/hub.py）；要最高密码的都从标准输入读，不收参数\n  chip view | state | log | payload … | body … | apply | receive | request | accept | forget <凭证>\n                                         安全芯片确认（见 scripts/chip.py）；签名由应用做，命令行签不了\n  serve [机器] | stop [机器] [--yes] | restart [机器] [--yes]\n                                         启动服务 | 先卸载模型再停服务 | 卸载、停止再启动（不自动加载模型）\n  start {1} --on {2} [--yes] [--free-ollama] [--wait] [--keep | --replace]\n                                         在一台上加载；放得下就留着别的模型一起跑，放不下才切换。--yes 表示已确认切换，\n                                         --keep 只许留着（放不下报错），--replace 一定切换，--free-ollama 表示已同意请 Ollama 释放内存\n  unload [{3}|all] [--on 机器] [--wait]\n                                         --wait：先等那台的请求答完再卸载（最长 10 分钟，等的时候可以停止）\n  requests {4}                   那台此刻在算、排队的请求数（JSON）\n  selftest {5} --on {6}\n  progress [--json] [--no-notify]        卡住报警：有请求在跑但 4 分钟没有新进度就报"卡住"并发一次系统通知\n  context <机器> <模型> <档位>            设置这台这个模型的上下文（正在跑的当场生效，不用重新加载）\n  option <机器> <模型> mtp|ssd on|off [--yes | --later] [--free-ollama]\n                                         模型开关：没在跑的只保存；正在跑的要重新加载（只下线这台的这个模型），\n                                         --yes 立刻切换（会中断正在答的请求），--later 只保存、下次加载生效，都不写就当场问\n  warmup {7} --on 机器              手动预热 {8} 的 MTP（第一次开时会自动做；缓存被挤掉后又忽快忽慢时用）\n  cache set <机器> off|{9} [--yes] [--free-ollama] | cache clear [机器] [--yes]\n                                         这台的多轮缓存（关或上限 GB；不用重启服务，模型会卸载再加载回来）| 清空缓存（那台要先卸载模型；不可恢复）\n  ollama free [--yes]                    请 Ollama 卸载它当前加载的模型（内存超线时的补救）\n  doctor                                 一键体检：地址、SSH、版本和补丁、模型文件、多余链接、监听、隧道、硬盘、残留进程\n  logs [机器]                            oMLX 日志最后 25 行\n  key copy | key reset [--yes]           复制主密钥 | 重置主密钥（{10}一起）\n  subkey list [--json] | create <名称> | copy <名称> | revoke <名称> [--yes]\n  machine list [--json] | add <名字> <地址> <账号> [显示名] [--json] | verify <名字> | use <名字> rpc|shell [--yes] | remove <名字> [--yes]\n                                         添加、切换、移除被控机（控制方这边）\n  machine addresses <名字> [--json] | addresses <名字> set | ports <名字>\n                                         这台登记的地址（网线在前；set 从标准输入读 JSON）| 它有哪些网口\n  machine install <名字> <全权限 SSH 别名>     经维护通道把控制台程序装到对方，只在换钥匙之前用\n  grants [list] [--json] | allow <配对串> [call|view|control] | revoke <编号> [--yes]\n                                         这台授权给了谁：允许要在桌面上亲手点，吊销同时断开已有连接', ' | '.join(NODES), _NAMES, NODE_CHOICES, _NAMES, NODE_CHOICES, _NAMES, NODE_CHOICES, _WARM, _WARM.replace('deepseek', 'DeepSeek'), '|'.join(map(str, CACHE_SIZES)), EVERY)


def run(args, parser):
    """按某一台做事的命令（加载、自检、预热、开关、上下文），先换成那台的模型清单；要的模型这台 oMLX 列着、清单里还没有，现认（2b）。"""
    act, rest, on = args.action, args.args, args.on
    node = (on if act in ('start', 'selftest', 'warmup') and on in NODES
            else rest[0] if act in ('option', 'context', 'note') and len(rest) >= 3 and rest[0] in NODES
            else rest[1] if act == 'cache' and rest[:1] == ['set'] and len(rest) == 3 and rest[1] in NODES else None)
    if node is None: return run_action(args, parser)
    with models_of(node):
        want = rest[1] if act in ('option', 'context', 'note') else rest[0] if rest and act != 'cache' else None
        if want and want not in MODELS and re.fullmatch(MODEL_KEY, want):
            if is_rpc(node): rpc_state(node)   # 配对加入的副机：它自己认出来的模型随状态带回来（第 4 期）；主密钥不发给它，这边认不了
            else:
                if official(node): official_key()   # 官方版还没允许读主密钥时先说清这一点，不报"不认识这个模型"
                discover(node, model_rows(node))
        return run_action(args, parser)


def run_action(args, parser):
    act, rest, on = args.action, args.args, args.on
    def one():
        # 起停类命令的机器写位置参数，也认 --on：以前 --on 被悄悄忽略，stop --on mini 会把每一台都停掉（体检 11-15）
        if rest and on and rest[0] != on: parser.error(tr('机器写了两个：{0} 和 --on {1}，只写一个。', rest[0], on))
        return rest[0] if rest else on
    names = '|'.join(MODELS)
    if act == 'status':
        if args.json: print(json.dumps(status_json(not args.no_worker), ensure_ascii=False))
        else: status(not args.no_worker)
    elif act in {'serve', 'ui'}: serve(one())
    elif act == 'stop': shutdown(one(), args.yes)
    elif act == 'poweroff':
        if on: parser.error(tr('poweroff、unlock 的机器写在命令后面，不用 --on。'))   # 关机和解锁照旧只认位置参数
        if one() == 'all': poweroff_all(args.yes)
        else: poweroff(one(), args.yes)
    elif act == 'unlock':
        if on: parser.error(tr('poweroff、unlock 的机器写在命令后面，不用 --on。'))
        unlock(one())
    elif act == 'autostart': autostart(one())
    elif act == 'up': up(one())
    elif act == 'down': down(one(), args.yes)
    elif act == 'cycle': cycle(one(), args.reload, args.yes)
    elif act == 'admin-session': admin_session(one())
    elif act == 'prepare': prepare_state()
    elif act == 'engine': engine_command(rest, args.json)
    elif act == 'updates': updates_command(rest, args.json)
    elif act == 'hub': hub_command(rest, args)
    elif act == 'chip': chip_command(rest)
    elif act == 'service': service_command(rest)
    elif act == 'restart': restart(one(), args.yes)
    elif act == 'progress':
        out = progress(not args.no_notify)
        print(json.dumps(out, ensure_ascii=False) if args.json else out['text'])
    elif act == 'doctor': doctor()
    elif act == 'selftest':
        if len(rest) != 1 or rest[0] not in MODELS or on not in NODES: parser.error(f'selftest {names} --on {NODE_CHOICES}')
        selftest(rest[0], on)
    elif act == 'start':
        if len(rest) != 1 or rest[0] not in MODELS or on not in NODES: parser.error(f'start {names} --on {NODE_CHOICES}')
        if args.keep and args.replace: parser.error(tr('--keep 和 --replace 只能选一个'))
        start(rest[0], on, args.yes, args.free_ollama, args.wait, args.keep, args.replace)
    elif act == 'option':
        if args.wait: parser.error(tr('option 不等请求答完：切换就是立刻卸载、重新加载。想保住正在答的请求，等它们答完再运行，或用 --later 只保存'))
        if len(rest) != 4 or rest[3] not in ('on', 'off') or (args.yes and args.later):
            parser.error(f'option {NODE_CHOICES} {names} mtp|ssd on|off [--yes | --later]')
        when = 'now' if args.yes else 'later' if args.later else None
        set_option(rest[0], rest[1], rest[2], rest[3] == 'on', when, args.free_ollama)
    elif act == 'warmup':
        if len(rest) != 1 or rest[0] not in MODELS or on not in NODES: parser.error(f'warmup {_WARM} --on {NODE_CHOICES}')
        warmup_command(rest[0], on)
    elif act == 'unload':
        every = {name for node in NODES for name in models_for(node)}   # 各台清单合起来：只在副机上有的模型也能卸
        if len(rest) == 1 and rest[0] not in {'all', *every} and re.fullmatch(MODEL_KEY, rest[0]):
            for node in pick_nodes(on):
                with models_of(node): discover(node, model_rows(node))
            every = {name for node in NODES for name in models_for(node)}
        if len(rest) > 1 or (rest and rest[0] not in {'all', *every}): parser.error(tr('unload [{0}|all] [--on 机器]', names))
        if rest and rest[0] in every:
            for node in pick_nodes(on):
                with models_of(node):
                    if rest[0] not in MODELS: continue
                    if args.wait and (is_rpc(node) or node_models(node).get(rest[0]) != 'off'): wait_idle(node)   # 没加载它的那台不用等（体检 11-15）
                    unload(rest[0], node)
        else: unload_all(on, wait=args.wait)
    elif act == 'requests':
        if len(rest) != 1 or rest[0] not in NODES: parser.error(f'requests {NODE_CHOICES}')
        print(json.dumps(requests_now(rest[0])))
    elif act == 'context':
        if len(rest) == 2 and rest[0] in MODELS:  # 旧写法（各台共用）：各台一起改，给旧版控制台应用用
            for node in NODES: set_context(node, *rest)
        elif len(rest) == 3: set_context(*rest)
        else: parser.error(tr('context {0} {1} <档位>', NODE_CHOICES, names))
    elif act == 'note':
        if len(rest) < 3: parser.error(tr('note {0} <模型> set | reset title|note|recommended|all', NODE_CHOICES))
        note_command(rest[0], rest[1], rest[2:], sys.stdin.read() if rest[2] == 'set' else '')
    elif act == 'cache':
        if rest[:1] == ['set'] and len(rest) == 3: set_cache(rest[1], rest[2], args.yes, args.free_ollama)
        elif rest[:1] == ['clear'] and len(rest) <= 2:
            if len(rest) == 2 and on and rest[1] != on: parser.error(tr('机器写了两个：{0} 和 --on {1}，只写一个。', rest[1], on))
            clear_cache(rest[1] if len(rest) == 2 else on or 'all', args.yes)
        else: parser.error(tr('cache set {0} off|{1} | cache clear [机器]', NODE_CHOICES, '|'.join(map(str, CACHE_SIZES))))
    elif act == 'ollama':
        if rest != ['free']: parser.error('ollama free')
        release_ollama(args.yes)
    elif act == 'copy-key': copy_key()
    elif act == 'logs': logs(one())
    elif act == 'key':
        if rest == ['copy']: copy_key()
        elif rest == ['reset']: reset_main_key(args.yes)
        elif rest == ['reveal']: reveal_key()
        else: parser.error('key copy | key reset')
    elif act == 'subkey':
        if rest in ([], ['list']):
            keys = list_sub_keys()
            if args.json: print(json.dumps(keys, ensure_ascii=False))
            else: print('\n'.join(f"{k['name'] or tr('（未命名）')}  {k['hint']}  {k['created_at'][:19]}" for k in keys) or tr('还没有子密钥。'))
        elif rest == ['where']: print(json.dumps(key_coverage(), ensure_ascii=False))
        elif len(rest) == 2 and rest[0] == 'create-print': create_sub_key(rest[1], show=True)
        elif len(rest) == 2 and rest[0] == 'create': create_sub_key(rest[1])
        elif len(rest) == 2 and rest[0] == 'copy': copy_key(rest[1])
        elif len(rest) == 2 and rest[0] == 'revoke': revoke_sub_key(rest[1], args.yes)
        elif len(rest) == 2 and rest[0] in ('disable', 'enable'): set_sub_key_disabled(rest[1], rest[0] == 'disable')
        elif len(rest) == 2 and rest[0] == 'reveal': reveal_key(rest[1])
        else: parser.error(tr('subkey list | where | create <名称> | copy <名称> | revoke <名称> | disable <名称> | enable <名称>'))
    elif act == 'machine':
        if rest in ([], ['list']): machine_list(args.json)
        elif rest[0] == 'add' and len(rest) in (4, 5): machine_add(*rest[1:], as_json=args.json)
        elif rest[0] == 'pair' and len(rest) in (4, 5): machine_add(*rest[1:], as_json=args.json, by_password=True)
        elif rest[0] == 'pair-status' and len(rest) == 2: machine_pair_status(rest[1], args.json)
        elif rest[0] == 'check' and len(rest) == 2: machine_check(rest[1], args.json)
        elif rest[0] == 'verify' and len(rest) == 2: machine_verify(rest[1])
        elif rest[0] == 'rename' and len(rest) == 3: machine_rename(rest[1], rest[2])
        elif rest[0] == 'probe' and len(rest) == 2: machine_probe(rest[1])
        elif rest[0] == 'use' and len(rest) == 3: machine_use(rest[1], rest[2], args.yes)
        elif rest[0] == 'remove' and len(rest) == 2: machine_remove(rest[1], args.yes)
        elif rest[0] == 'install' and len(rest) == 3: machine_install(rest[1], rest[2], args.yes)
        elif rest[0] == 'addresses' and len(rest) == 2: machine_addresses(rest[1], args.json)
        elif rest[0] == 'addresses' and len(rest) == 3 and rest[2] == 'set': machine_set_addresses(rest[1], sys.stdin.read())
        elif rest[0] == 'ports' and len(rest) == 2: machine_ports(rest[1])
        else: parser.error(tr('machine list | add <名字> <地址> <账号> [显示名] | verify <名字> | use <名字> rpc|shell | remove <名字> | install <名字> <SSH 别名> | addresses <名字> [set] | ports <名字>'))
    elif act == 'grants':
        if rest in ([], ['list']): grants(args.json)
        elif rest[0] == 'allow' and len(rest) in (2, 3): grants_allow(*rest[1:])
        elif rest[0] == 'install' and len(rest) in (2, 3, 4): grants_install(*rest[1:])
        elif rest[0] == 'revoke' and len(rest) == 2: grants_revoke(rest[1], args.yes)
        else: parser.error(tr('grants list | allow <配对串> [call|view|control] | revoke <编号>'))


def main():
    if sys.argv[1:2] == ['_clear-clipboard']:
        return clear_clipboard_later(sys.argv[2] if len(sys.argv) > 2 else '')
    parser = argparse.ArgumentParser(description=tr('ModelTower（oMLX，{0} 各自单机）', ' / '.join(map(where, NODES))) if len(NODES) > 1 else tr('ModelTower（oMLX，{0}）', where(LOCAL)), epilog=USAGE,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', nargs='?', choices=['status', 'serve', 'ui', 'stop', 'restart', 'start', 'unload', 'selftest',
                                                      'context', 'cache', 'ollama', 'doctor', 'key', 'subkey', 'copy-key', 'logs',
                                                      'progress', 'requests', 'option', 'warmup', 'note', 'machine', 'grants', 'poweroff', 'unlock', 'autostart', 'hub', 'chip', 'service', 'up', 'down', 'cycle', 'admin-session', 'prepare', 'engine', 'updates'])
    parser.add_argument('args', nargs='*')
    parser.add_argument('--on', choices=[*NODES, 'all'], help=tr('在哪台机器上（start、selftest 必填；unload 不写就是{0}）', EVERY))
    parser.add_argument('--yes', action='store_true', help=tr('不再询问：需要时自动启动服务、确认切换、确认关闭、吊销、重置；option 表示立刻切换'))
    parser.add_argument('--later', action='store_true', help=tr('option：模型正在跑时只保存，下次加载生效'))
    parser.add_argument('--free-ollama', action='store_true', help=tr('Ollama 占着内存、放不下时，允许请它先释放'))
    parser.add_argument('--wait', action='store_true', help=tr('unload、start 时先等那台的请求答完（最长 10 分钟）'))
    parser.add_argument('--keep', action='store_true', help=tr('start：留着这台别的模型，放不下就报错'))
    parser.add_argument('--replace', action='store_true', help=tr('start：先卸载这台别的模型'))
    parser.add_argument('--json', action='store_true', help=tr('status、subkey list 输出 JSON'))
    parser.add_argument('--no-worker', action='store_true', help=tr('status 不检查 {0}，更快', tr('、').join(map(where, remotes()))))
    parser.add_argument('--force', action='store_true', help=tr('hub takeover：主机还连得上时也接管'))
    parser.add_argument('--no-notify', action='store_true', help=tr('progress 只报告，不发系统通知'))
    parser.add_argument('--reload', action='store_true', help=tr('cycle：重启后把原来在跑的模型加载回来'))
    args = parser.parse_args()
    if args.action:
        sys.stdout.reconfigure(line_buffering=True)  # 应用逐行显示加载进度
        try: run(args, parser)
        except Exception as exc:
            if args.json: print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
            else: print(bad(tr('操作未完成：{0}', exc)), file=sys.stderr)
            sys.exit(1)
        return
    while True:
        if TTY: print('\033[2J\033[H', end='')
        status()
        print(menu_text())
        menu = menu_actions()   # 和刚印出来的菜单同一份清单（副机的清单可能刚带回来）
        try: choice = input(tr('请选择：')).strip().lower()
        except EOFError: return
        if choice == '0': return
        if choice in {'r', ''}: continue
        try: menu[choice]()
        except KeyError: print(tr('请输入菜单里的编号或字母。'))
        except subprocess.CalledProcessError: print(bad(tr('操作未完成：启动脚本报错，请看上面的输出或按 l 看日志。')))
        except Exception as exc: print(bad(tr('操作未完成：{0}', exc)))
        release_lock()
        try: input(tr('\n按回车返回菜单…'))
        except EOFError: return


if __name__ == '__main__':
    main()
