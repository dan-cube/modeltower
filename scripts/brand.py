"""应用名、Bundle ID、发布者集中在这一处（设备版通用化草案 9.1 第 4 条）。构建脚本、后端、应用都从这里取。
真正改名（ModelTower、新的 Bundle ID）在第 6 期：改之前要实测安全芯片的钥匙、应用偏好、本地网络权限、副机上 open -b 找不找得到。"""
import shlex, sys

APP_NAME = 'ModelTower'          # 应用包和窗口的名字（英文界面显示 Localizable 里的译名）
BUNDLE_ID = 'io.github.dan-cube.modeltower'
EXECUTABLE = 'ModelTower'
SERVICE_LABEL = 'io.github.dan-cube.modeltower.device'   # 设备常驻项的 launchd 标签
PUBLISHER = 'dan-cube'               # 已定第 23 条
HOMEPAGE = 'https://github.com/dan-cube/modeltower'
DATA_DIR = 'ModelTower'              # 新用户的运行目录名（~/Library/Application Support/ 下；已定第 21、28 条 g）。早期装法的两台不用它
# 这个软件用过的两种名字（Bundle ID，进程名）：内部早期装法的和公开版的。两种互相当副机时，开关对方的控制台窗口两种都试
KNOWN_APPS = (('local.mlx-cluster.console', 'LocalModelConsole'), ('io.github.dan-cube.modeltower', 'ModelTower'))
# 第 3b 期：联网读签了名的兼容表、通知、控制台新版（草案 10.7）。地址和公钥第 6 期建公开仓库、生成钥匙（要用户同意）后才填；
# 空着就什么都不读。设置文件的 update_url 能改地址（测试、自建镜像用）；公钥只认这里（测试用环境变量 LMC_UPDATE_PUBKEY）
UPDATE_URL = 'https://raw.githubusercontent.com/dan-cube/modeltower/main/updates/'
UPDATE_URL_CN = 'https://www.modelscope.cn/models/skyeyes1999/omlx-release-copy/resolve/master/modeltower-updates/'                   # 0.5.1：同一份清单在魔搭上的地址（国内读 GitHub 不稳）。清单是签了名的，从哪读都一样核签名
UPDATE_PUBLIC_KEY = 'd2df0d06a43430e0ede436ca6967afd4f2153a758becb9637318b2725f9fda34'

if __name__ == '__main__' and sys.argv[1:] == ['--shell']:   # 给构建脚本：eval "$(python brand.py --shell)"
    for key, value in list(globals().items()):
        if key.isupper() and isinstance(value, str): print(f'{key}={shlex.quote(value)}')
