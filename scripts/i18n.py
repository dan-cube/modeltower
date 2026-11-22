#!/usr/bin/env python3
"""后端的中英文。中文当键，英文在 i18n_en.json，查不到显示中文，不会崩。只用标准库（gateway.py 也用）。
语言：应用启动时传 LMC_LANG；跨机器时 rpc.py 按请求前缀设；终端里看 LANG，开头是 zh 或没设就是中文。"""
import json, os
from pathlib import Path

TABLE_FILE = Path(__file__).with_name('i18n_en.json')
_table = None


def lang():
    value = os.environ.get('LMC_LANG')
    if value in ('zh', 'en'): return value
    system = os.environ.get('LANG', '')
    return 'en' if system and not system.startswith('zh') else 'zh'


def tr(text, *args):
    """tr('关机 {0}？', name)。没有参数时不做 format，文字里的大括号不用转义。"""
    global _table
    if lang() == 'en':
        if _table is None:
            try: _table = json.loads(TABLE_FILE.read_text(encoding='utf-8'))
            except (OSError, ValueError): _table = {}
        text = _table.get(text) or text
    return text.format(*args) if args else text
