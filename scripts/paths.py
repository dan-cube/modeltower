"""项目里的固定位置。脚本统一从这里取，目录再调整时只改这一处。

项目根是 git 仓库根；代码在 01-软件开发，测试与验收数据在 05-存档/测试数据。
运行时文件（oMLX 状态、控制台的 pid 与日志、机器表、设置）不进项目文件夹：早期装法在 /Users/Shared/mlx-cluster，
新用户在 ~/Library/Application Support/<brand.DATA_DIR>/，见 runtime_root()。
"""
import hashlib
import os
from pathlib import Path
import stat

CODE_DIR = Path(__file__).resolve().parent.parent          # 01-软件开发
REPO_DIR = CODE_DIR.parent                                  # 项目根
REPORTS = REPO_DIR / '05-存档' / '测试数据'                 # 测试、验收、基准数据（入库）
SHARED_ROOT = Path('/Users/Shared/mlx-cluster')   # 早期装法（自管版 oMLX 的两台）：里面有机器表或 omlx-state 才用它


def runtime_root(env=os.environ, shared=SHARED_ROOT, home=None, uid=None):
    """运行时根（第 4 期，已定第 28 条 g）：测试用的 LMC_ROOT → 早期装法的共享目录（已经在用、而且是自己的才算）→ 每个用户自己的一份。
    新用户的那份在 ~/Library/Application Support/ 下，不要管理员权限；路径里有空格，拼进命令行的地方都要加引号。
    共享目录要属于这个账号才用：/Users/Shared 人人能写，别的账号可以先建好它、放进自己的设置（体检 11-15）。"""
    if env.get('LMC_ROOT'): return Path(env['LMC_ROOT'])
    try:
        info = os.lstat(shared)   # lstat：别的账号在 /Users/Shared 下放一个指向别处的符号链接，不认
        mine = stat.S_ISDIR(info.st_mode) and info.st_uid == (os.getuid() if uid is None else uid)
        if mine and ((shared / 'cluster.json').exists() or (shared / 'omlx-state').exists()): return shared
    except OSError: pass
    from brand import DATA_DIR
    return (home or Path.home()) / 'Library' / 'Application Support' / DATA_DIR


RUNTIME = runtime_root()  # 运行时根；LMC_ROOT 只给测试用
RUN_DIR = RUNTIME / 'console-runtime'                       # 控制台自己的 pid 与日志（不入库）


def control_path(name):
    """SSH 复用连接的套接字放哪。macOS 上这种路径最长 104 字节，主连接建的时候还要加 17 字节的临时后缀，所以最多留 86。
    早期装法的运行目录够短，照旧放 console-runtime；新用户的运行目录在 ~/Library/Application Support/ 下太深，放不下
    （隧道会起不来），改放 ~/.ssh/lmc/；账号名特别长时再换成短名字。"""
    lmc = Path.home() / '.ssh' / 'lmc'
    for path in (RUN_DIR / f'ctl-{name}.sock', lmc / f'ctl-{name}.sock'):
        if len(str(path).encode()) <= 86: return path
    return lmc / f'c-{hashlib.sha256(name.encode()).hexdigest()[:10]}'

CONFIG_DIR = CODE_DIR / 'config'                            # 随代码走的配置：出厂说明、机器表范例（入库）
FAMILIES_DIR = CONFIG_DIR / 'model-families'                # 模型说明①：出厂说明，按家族（入库，进代码指纹）
MODEL_DATA = Path(os.environ.get('LMC_MODEL_DATA') or RUNTIME)  # 模型说明②③：model-facts.json、model-notes.json（每台一份，不入库）；环境变量只给测试用
CLUSTER_FILE = Path(os.environ.get('LMC_CLUSTER_FILE') or RUNTIME / 'cluster.json')  # 这台知道的机器，各台不同（不入库）；环境变量只给测试用
