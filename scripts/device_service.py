#!/usr/bin/env python3
"""设备服务：界面可以关，这些事不能跟着停（用户 11-03 定，顺带解决 1.1-3 隧道只有控制台开着才重连）。

每 10 秒：接上断了的隧道；启用主机模式时同步任期（发现自己被换了就退下来）、入口跟着主机走。
不启动 oMLX、不加载模型：开机不自启是用户定的（决策台账 D3），启动由人在中控里点。
没有桌面会话也能跑，常驻项由用户本人安装（clusterctl service），这里不写系统设置。
"""
from i18n import tr
import argparse
import json
import signal
import sys
import time

import clusterctl as c
import hub


def tick():
    """一轮。哪一步出错都不能让服务退出，下一轮再试。返回做了什么，测试用。"""
    report = {}
    try:
        for node in c.remotes():
            if not c.port_open(c.NODES[node]['port']): report[node] = bool(c.ensure_tunnel(node))
    except Exception as exc:
        report['tunnel_error'] = str(exc)
    try:
        if hub.load(): report['hub'] = hub.sync(c.LOCAL)
    except Exception as exc:
        report['hub_error'] = str(exc)
    return report


def main():
    parser = argparse.ArgumentParser(description=tr('设备服务'))
    parser.add_argument('--once', action='store_true', help=tr('只跑一轮，打印结果'))
    parser.add_argument('--every', type=int, default=10)
    args = parser.parse_args()
    if args.once:
        print(json.dumps(tick(), ensure_ascii=False))
        return
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        tick()
        time.sleep(args.every)


if __name__ == '__main__':
    main()
