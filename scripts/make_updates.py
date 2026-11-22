#!/usr/bin/env python3
"""发布时做联网更新用的清单 updates.json（草案 10.7）：兼容表、控制台最新版、通知合成一份。做完由发布者用 sign_updates.py 签名，
清单和 .sig 一起放到 brand.py 的 UPDATE_URL、UPDATE_URL_CN 两个地址下。只读兼容表、只写输出目录，不联网、不碰钥匙串。

  make_updates.py <兼容表.json> <控制台版本> <输出目录> [发布时间，如 2026-11-22T08:00:00Z，不写就是现在]

兼容表用要发布的那一份（公开仓库里的 config/engine-compat.json）。换 oMLX 版本时的顺序：先把新安装包转存到魔搭并核对通过，
再改兼容表、做清单、签名、发布——下载源选"中国"的用户只从魔搭下，表先发出去他们就下不到。"""
import json
from pathlib import Path
import sys
import time

import brand
import updates


def build(table, version, issued=None):
    issued = issued or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    data = {'schema': 1, 'issued': issued, 'compat': table,
            'console': {'version': version, 'url': f"{brand.HOMEPAGE.rstrip('/')}/releases/tag/v{version}"}, 'notices': []}
    raw = (json.dumps(data, ensure_ascii=False, indent=1) + '\n').encode('utf-8')
    updates.parse_manifest(raw)   # 格式不对就不出：签了名的坏清单用户端也不认，白发
    return raw


def main(argv):
    if len(argv) not in (3, 4):
        print(__doc__)
        return 2
    table = json.loads(Path(argv[0]).read_text(encoding='utf-8'))
    if not updates.compat_ok(table): raise SystemExit('兼容表的结构不对，没有做清单。')
    if not updates._key(argv[1]): raise SystemExit(f'版本号读不懂：{argv[1]}')
    try: raw = build(table, argv[1], argv[3] if len(argv) == 4 else None)
    except updates.Failure as exc: raise SystemExit(f'清单格式不对（{exc.code}），没有写出。发布时间要写成 2026-11-22T08:00:00Z 这样。')
    out = Path(argv[2])
    out.mkdir(parents=True, exist_ok=True)
    (out / updates.MANIFEST).write_bytes(raw)
    print(f'已写出：{out / updates.MANIFEST}（{len(raw)} 字节，issued {json.loads(raw)["issued"]}，控制台 {argv[1]}，兼容表 {table.get("updated")}）')
    print(f'下一步由发布者签名：sign_updates.py sign {out / updates.MANIFEST}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
