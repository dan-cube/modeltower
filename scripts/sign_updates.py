#!/usr/bin/env python3
"""发布时给 updates.json 签名（草案 10.7、已定第 28 条 f）。私钥放发布者的登录钥匙串，签名时现取，不落盘、不进 git；
公开的那一半写进 brand.py 的 UPDATE_PUBLIC_KEY。只在发布的那台 Mac 上、由发布者本人跑。

  sign_updates.py new-key        生成一对钥匙：私钥存进登录钥匙串，打印公钥。要用户同意后才跑；已经有了就不覆盖。
                                 生成后请另存一份离线备份（钥匙串访问 → 找到这一项 → 显示密码 → 抄到离线的地方）。
  sign_updates.py public         打印钥匙串里那把私钥对应的公钥
  sign_updates.py sign <文件>    签名，写出 <文件>.sig（base64），并用公钥核一遍
  sign_updates.py verify <文件> <公钥>   核签名（不碰钥匙串）

私钥经 `security -i` 的标准输入交给钥匙串，不出现在命令行参数里（别的用户 ps 看不到）。"""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys

import brand
import signing
import updates

SERVICE = 'local.mlx-cluster.console.update-signing'   # 固定的名字，不跟 brand.BUNDLE_ID 走：改了应用名还是同一把钥匙（体检 11-15；钥匙还没生成过）
ACCOUNT = brand.PUBLISHER


def secret():
    r = subprocess.run(['/usr/bin/security', 'find-generic-password', '-s', SERVICE, '-a', ACCOUNT, '-w'], capture_output=True, text=True)
    if r.returncode != 0: raise SystemExit(f'钥匙串里没有 {SERVICE}（{ACCOUNT}）。先 new-key。')
    seed = bytes.fromhex(r.stdout.strip())
    if len(seed) != 32: raise SystemExit('钥匙串里那一项不是 32 字节的私钥。')
    return seed


def main(argv):
    what = argv[0] if argv else ''
    if what == 'new-key':
        if subprocess.run(['/usr/bin/security', 'find-generic-password', '-s', SERVICE, '-a', ACCOUNT], capture_output=True).returncode == 0:
            raise SystemExit('钥匙串里已经有一把，不覆盖。要换钥匙先在"钥匙串访问"里删掉那一项（旧版控制台就认不出新签的清单了）。')
        seed = os.urandom(32)
        # -T "" ：不给任何程序免确认读它，每次签名取私钥时钥匙串都会弹框问
        command = f'add-generic-password -s "{SERVICE}" -a "{ACCOUNT}" -l "{brand.APP_NAME} 更新签名私钥" -T "" -w {seed.hex()}\n'
        r = subprocess.run(['/usr/bin/security', '-i'], input=command, capture_output=True, text=True)
        if r.returncode != 0 or secret() != seed: raise SystemExit('存进钥匙串没成功。')
        print('已存进登录钥匙串。公钥（写进 brand.py 的 UPDATE_PUBLIC_KEY）：')
        print(signing.public_key(seed).hex())
        print('请另存一份私钥的离线备份。')
    elif what == 'public':
        print(signing.public_key(secret()).hex())
    elif what == 'sign' and len(argv) > 1:
        path = Path(argv[1])
        raw = path.read_bytes()
        data = updates.parse_manifest(raw)   # 格式不对就不签
        # issued 写错成未来的日期，发出去以后正确的清单全会被当成回滚，收不回来：签之前核一遍（体检 11-15）
        import calendar, time
        try: then = calendar.timegm(time.strptime(data['issued'], '%Y-%m-%dT%H:%M:%SZ'))
        except ValueError: raise SystemExit(f'issued 不是一个真的时间：{data["issued"]}')
        if then > time.time() + 86400: raise SystemExit(f'issued 比现在晚一天以上：{data["issued"]}。写错了的话改掉再签。')
        seed = secret()
        sig = base64.b64encode(signing.sign(seed, raw)).decode()
        if not updates.verify(raw, sig, signing.public_key(seed).hex()): raise SystemExit('签完核不过，没写出。')
        Path(str(path) + '.sig').write_text(sig + '\n')
        print(f'已签：{path}.sig（issued {json.loads(raw)["issued"]}）')
    elif what == 'verify' and len(argv) > 2:
        path = Path(argv[1])
        ok = updates.verify(path.read_bytes(), Path(str(path) + '.sig').read_text(), argv[2])
        print('签名对' if ok else '签名不对')
        return 0 if ok else 1
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
