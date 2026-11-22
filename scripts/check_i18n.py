#!/usr/bin/env python3
"""中英文检查：新写的中文有没有包 tr()、L()，英文表全不全，占位符对不对。发版前跑，有问题退出码 1。
中文当键，改了措辞英文就会退回中文，这里会列出来。
用法：check_i18n.py <含 scripts/ 和 app/ 的目录>
      check_i18n.py --wrap <文件>   把没包的中文包上（后端按语法树，界面按字符串）"""
import ast, json, plistlib, re, sys
from pathlib import Path

CJK = re.compile(r'[　-〿㐀-鿿＀-￯]')
# 这些函数的参数不是给人看的：日志（用户贴进 issue，别人要能搜）、正则、字符串操作
LOG_CALLS = {'warning', 'info', 'error', 'debug', 'exception'}
SKIP_CALLS = {'note', 'log', 'compile', 'fullmatch', 'match', 'search', 'sub', 'findall',
              'startswith', 'endswith', 'split', 'rsplit', 'partition', 'rpartition', 'strip', 'lstrip', 'rstrip', 'replace', 'find', 'index', 'count', 'removeprefix', 'removesuffix'}
# 这些名字的值不翻：发给模型的题，预热的键跟着它，改了要重新预热
SKIP_NAMES = {'WARMUP_PROMPT', 'CONTEXT_WORDS', 'LINK_VALUES', 'DEFAULT_ETA'}   # LINK_VALUES：机器表里存的连接方式，显示时才翻；DEFAULT_ETA 本身写了中英两份
# 字典里这些键的值存进文件、发给模型或给程序看
SKIP_KEYS = {'role', 'content', 'where'}
# 程序靠这些短语认后端的话（应用用 L() 查，后端的句子要包含同一个英文）
SHARED = ('操作未完成：', '还连得上')   # 2a 起卸载开始、体检结果、推荐档位都用机读标记或字段，不再认这几个字
# 英文表里可以留中文的：语言自己的名字
KEEP_CJK = {'简体中文'}
SCRIPTS = ('clusterctl.py', 'hub.py', 'chip.py', 'rpc.py', 'gateway.py', 'device_service.py')
SWIFT = ('ConsoleApp.swift', 'ChipKey.swift')
FAMILY_SKIP = {'说明', 'match', 'family'}   # 出厂说明里不给人看的字段


# 路径和命令不是文字
PATHLIKE = re.compile(r'\.app\b|\.py$')


def wrap_py(source):
    """(新源码, 键列表)。已经包过的只记键。"""
    raw = source.encode()
    tree = ast.parse(source)
    starts = [0]
    for line in raw.splitlines(keepends=True): starts.append(starts[-1] + len(line))
    def at(line, col): return starts[line - 1] + col
    def span(n): return at(n.lineno, n.col_offset), at(n.end_lineno, n.end_col_offset)
    parent = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n): parent[c] = n
    doc = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            for s in n.body:
                if isinstance(s, ast.Expr) and isinstance(s.value, (ast.Constant, ast.JoinedStr)): doc.add(s.value)
    keys = []

    def skipped(n):
        if n in doc: return True
        text = n.value if isinstance(n, ast.Constant) else ''.join(v.value for v in n.values if isinstance(v, ast.Constant))
        if PATHLIKE.search(text): return True
        p, child = parent.get(n), n
        while p is not None:
            if isinstance(p, ast.Compare): return True
            if isinstance(p, ast.Dict):
                if child in p.keys: return True
                key = p.keys[p.values.index(child)] if child in p.values else None
                if isinstance(key, ast.Constant) and key.value in SKIP_KEYS: return True
            if isinstance(p, ast.Call):
                f = p.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', '')
                base = getattr(f.value, 'id', '') if isinstance(f, ast.Attribute) else ''
                if child is f: return False
                if name == 'tr':
                    if p.args and child is p.args[0] and isinstance(n, ast.Constant): keys.append(n.value)
                    return True
                if name in SKIP_CALLS or (name in LOG_CALLS and base in ('log', 'logging', 'logger')): return True
                return False
            if isinstance(p, ast.Assign) and {t.id for t in p.targets if isinstance(t, ast.Name)} & SKIP_NAMES: return True
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module, ast.Lambda, ast.FormattedValue, ast.JoinedStr)): return False
            child, p = p, parent.get(p)
        return False

    def is_target(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, str): return bool(CJK.search(n.value)) and not isinstance(parent.get(n), ast.JoinedStr)
        if isinstance(n, ast.JoinedStr): return any(isinstance(v, ast.Constant) and CJK.search(v.value) for v in n.values)
        return False

    def render_range(a, b, container):
        found = []
        def visit(n):
            for c in ast.iter_child_nodes(n):
                if is_target(c):
                    if skipped(c):
                        if isinstance(c, ast.JoinedStr): visit(c)
                    else: found.append(c)
                else: visit(c)
        visit(container)
        out, pos = [], a
        for n in sorted(found, key=lambda x: span(x)[0]):
            s, e = span(n)
            if s < pos or e > b: continue
            out.append(raw[pos:s].decode())
            out.append(render_target(n))
            pos = e
        out.append(raw[pos:b].decode())
        return ''.join(out)

    def render_target(n):
        if isinstance(n, ast.Constant):
            keys.append(n.value)
            return f'tr({n.value!r})'
        key, args = '', []
        for v in n.values:
            if isinstance(v, ast.Constant): key += v.value.replace('{', '{{').replace('}', '}}')
            else:
                key += '{' + str(len(args)) + '}'
                a, b = span(v.value)
                text = render_target(v.value) if is_target(v.value) and not skipped(v.value) else render_range(a, b, ast.Expression(v.value))
                if v.conversion != -1 or v.format_spec is not None:
                    s, e = span(v)
                    args.append("f'" + raw[s:e].decode() + "'")
                elif isinstance(v.value, (ast.Name, ast.Attribute, ast.Subscript, ast.Call, ast.Constant)): args.append(text)
                else: args.append('(' + text + ')')
        keys.append(key)
        return f"tr({key!r}, {', '.join(args)})"

    body = render_range(0, len(raw), tree)
    ast.parse(body)
    return body, keys


def add_import(text):
    if 'from i18n import tr' in text: return text
    tree = ast.parse(text)
    first = tree.body[0]
    line = first.end_lineno if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) else 0
    rows = text.split('\n')
    rows.insert(line, 'from i18n import tr')
    return '\n'.join(rows)


class SwiftWrapper:
    """自己认 Swift 的字符串（含插值里嵌的），注释不动。键：字面文字原样，插值换成 %@，% 写成 %%，和 Localize.swift 运行时算的一样。"""
    ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', '0': '\0', '"': '"', "'": "'", '\\': '\\'}
    SKIP = ('# ModelTower（管理版）生成',)    # 写进配置文件的注释

    def __init__(self, text):
        self.text, self.keys = text, []

    def literal(self, i):
        t = self.text
        if t.startswith('"""', i):
            j = t.index('"""', i + 3) + 3
            return t[i:j], j
        j, key, parts = i + 1, '', ['"']
        while t[j] != '"':
            c = t[j]
            if c == '\\':
                n = t[j + 1]
                if n == '(':
                    expr, end = self.interpolation(j + 2)
                    parts.append('\\(' + expr + ')')
                    key += '%@'
                    j = end
                    continue
                if n == 'u':
                    end = t.index('}', j)
                    key += chr(int(t[j + 3:end], 16))
                    parts.append(t[j:end + 1])
                    j = end + 1
                    continue
                key += self.ESCAPES[n]
                parts.append(t[j:j + 2])
                j += 2
                continue
            key += '%%' if c == '%' else c
            parts.append(c)
            j += 1
        parts.append('"')
        out = ''.join(parts)
        if CJK.search(key) and not any(s in key for s in self.SKIP):
            self.keys.append(key)
            if t[max(0, i - 2):i] == 'L(': return out, j + 1     # 已经包过，只记键
            return 'L(' + out + ')', j + 1
        return out, j + 1

    def interpolation(self, i):
        t, out, depth = self.text, [], 1
        while True:
            c = t[i]
            if c == '"':
                piece, i = self.literal(i)
                out.append(piece)
                continue
            if c == '(': depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0: return ''.join(out), i + 1
            out.append(c)
            i += 1

    def run(self):
        t, out, i = self.text, [], 0
        while i < len(t):
            if t.startswith('//', i):
                j = t.find('\n', i)
                j = len(t) if j < 0 else j
                out.append(t[i:j]); i = j
            elif t.startswith('/*', i):
                j = t.index('*/', i) + 2
                out.append(t[i:j]); i = j
            elif t.startswith('#"', i):
                j = t.index('"#', i + 2) + 2
                out.append(t[i:j]); i = j
            elif t[i] == '"':
                piece, i = self.literal(i)
                out.append(piece)
            else:
                out.append(t[i]); i += 1
        return ''.join(out)


def wrap_swift(text):
    w = SwiftWrapper(text)
    return w.run(), w.keys


def family_problems(folder):
    """①出厂说明的文字都写成 {zh, en}，两种都要有；中文不能落在 {zh, en} 外面（那样英文界面会露中文）。"""
    problems = []
    def walk(v, where):
        if isinstance(v, dict) and 'zh' in v and set(v) <= {'zh', 'en'}:
            if not v.get('en'): problems.append(f'{where} 没写英文：{v["zh"][:50]!r}')
            elif placeholders(v['zh'], 'backend') != placeholders(v['en'], 'backend'): problems.append(f'{where} 中英文的占位不一样：{v["zh"][:50]!r}')
        elif isinstance(v, str):
            if CJK.search(v): problems.append(f'{where} 有中文不在 {{zh, en}} 里：{v[:50]!r}')
        elif isinstance(v, list):
            for x in v: walk(x, where)
        elif isinstance(v, dict):
            for x in v.values(): walk(x, where)
    for path in sorted(Path(folder).glob('*.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        for field, value in data.items():
            if field not in FAMILY_SKIP: walk(value, f'{path.name}.{field}')
    return problems


def placeholders(text, kind):
    if kind == 'ui': return sorted(re.findall(r'%(?:\d+\$)?@', re.sub(r'%%', '', text)), key=str).__len__()
    return sorted(re.findall(r'\{[a-z0-9_]*\}', text.replace('{{', '').replace('}}', '')))


def check(root):
    root, problems = Path(root), []
    backend, ui = [], []
    app = root / 'app/ConsoleApp' if (root / 'app/ConsoleApp').is_dir() else root / 'app'    # 仓库里多一层
    for name in SCRIPTS:
        text = (root / 'scripts' / name).read_text()
        new, keys = wrap_py(text)
        backend += keys
        if new != text: problems.append(f'{name} 有没包 tr() 的中文：运行 check_i18n.py --wrap {name}')
    for name in SWIFT:
        text = (app / name).read_text()
        new, keys = wrap_swift(text)
        ui += keys
        if new != text: problems.append(f'{name} 有没包 L() 的中文：运行 check_i18n.py --wrap {name}')
    problems += family_problems(root / 'config/model-families')
    backend += ['主机', '副机']   # 机器表里的角色，显示时才查
    tables = {'backend': json.loads((root / 'scripts/i18n_en.json').read_text()),
              'ui': plistlib.loads((app / 'en.lproj/Localizable.strings').read_bytes())}
    for kind, wanted in (('backend', backend), ('ui', ui)):
        table, wanted = tables[kind], sorted(set(wanted))
        missing = [k for k in wanted if not table.get(k)]
        print(f'{kind}：{len(wanted)} 条，翻了 {len(wanted) - len(missing)} 条，覆盖 {100 * (len(wanted) - len(missing)) / max(len(wanted), 1):.1f}%；表里多余 {len(set(table) - set(wanted))} 条')
        problems += [f'{kind} 没翻：{k[:70]!r}' for k in missing[:40]]
        for key in wanted:
            value = table.get(key)
            if not value: continue
            if placeholders(key, kind) != placeholders(value, kind): problems.append(f'{kind} 占位符不一样：{key[:60]!r} → {value[:60]!r}')
            if CJK.search(value) and key not in KEEP_CJK: problems.append(f'{kind} 英文里有中文：{key[:50]!r} → {value[:50]!r}')
    # 应用靠这些短语认后端的话，两边的英文要对得上
    for phrase in SHARED:
        english = tables['ui'].get(phrase) or tables['backend'].get(phrase)
        if not english: continue
        for key, value in tables['backend'].items():
            if phrase in key and phrase != '推荐' and english.strip() not in value: problems.append(f'短语 {phrase!r} 的英文 {english!r} 不在：{value[:70]!r}')
    # 应用名和权限说明：开发语言是 en，中文系统查不到就退回英文
    for folder in ('en.lproj', 'zh-Hans.lproj'):
        path = app / folder / 'InfoPlist.strings'
        plist = plistlib.loads(path.read_bytes()) if path.exists() else {}
        for key in ('CFBundleName', 'CFBundleDisplayName', 'NSLocalNetworkUsageDescription'):
            if not plist.get(key): problems.append(f'{folder}/InfoPlist.strings 缺 {key}')
    for line in problems: print('FAIL ', line)
    print('通过' if not problems else f'{len(problems)} 个问题')
    return 1 if problems else 0


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--wrap':
        path = Path(sys.argv[2])
        text = path.read_text()
        new = wrap_swift(text)[0] if path.suffix == '.swift' else add_import(wrap_py(text)[0])
        path.write_text(new)
        print(path.name, '包好了')
        sys.exit(0)
    sys.exit(check(sys.argv[1]))
