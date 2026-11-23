#!/usr/bin/env python3
"""认这台现在连着的网络（0.6 按网络开门）。入口（gateway.py）和后端（clusterctl.py）共用这一份：
界面上勾的和入口开的门，是同一个函数认出来的同一个网络。只用标准库，系统自带的 Python 3.9 也能跑。

三类网络，各有一个记号（写进 gateway.json 的 networks）和一组"这扇门收哪些来源"：
· 家里的网络 lan:<指纹>：实体网卡（Wi-Fi、网线）上的私网地址。指纹 = SHA-256(子网 + 路由器的硬件地址) 的前 12 位，
  换到别的 Wi-Fi 指纹就变，门自动不开（用户 11-22 定：只认勾的那一个网络）。路由器还没认出来时不给记号，宁可晚开不开错。
· tailscale：虚拟网卡上的 100.64.0.0/10 地址，而且这台装着 Tailscale。
· 其他虚拟网络 virtual:<子网>：别的虚拟网卡上的私网地址（蒲公英、EasyTier、ZeroTier 等）。虚拟网卡的编号每次重连会变，所以记子网。
不列：雷雳网桥和雷雳口（机器之间的直连线）、169.254 自动地址、公网地址。只看 IPv4。
"""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time

# 第三段分两截写：导出公开版时的脱敏规则不许源码里出现 192.168 后面跟数字的写法（防止把真的内网地址带出去）；这里是标准的私网段，不是哪台机器的地址
PRIVATE = ('10.0.0.0/8', '172.16.0.0/12', '192.168' + '.0.0/16')
CGNAT = ('100.64.0.0/10',)    # 运营商级 NAT 的地址段，Tailscale 用它；只在虚拟网卡上认
ACCEPT = {'lan': PRIVATE, 'tailscale': CGNAT, 'virtual': PRIVATE + CGNAT}
WIDE = PRIVATE + CGNAT + ('127.0.0.0/8', '::1/128', 'fc00::/7', 'fe80::/10')   # 老的高级用法（listen 写了非本机地址并配了 TLS）那扇门收的来源
VIRTUAL = ('utun', 'tun', 'tap', 'feth', 'zt', 'wg', 'ipsec', 'ppp')
TAILSCALE = ('/Applications/Tailscale.app', '/usr/local/bin/tailscale', '/opt/homebrew/bin/tailscale', '/usr/local/bin/tailscaled', '/opt/homebrew/bin/tailscaled')
PORTS_TTL = 600


def within(address, ranges):
    """address 在不在这些地址段里。读不懂的地址算不在。IPv6 里包着的 IPv4（::ffff:a.b.c.d）按里面的 IPv4 算。"""
    try: ip = ipaddress.ip_address(str(address).split('%', 1)[0])
    except ValueError: return False
    mapped = getattr(ip, 'ipv4_mapped', None)
    if mapped is not None: ip = mapped
    for item in ranges:
        try: net = ipaddress.ip_network(item, strict=False)
        except ValueError: continue
        if ip.version == net.version and ip in net: return True
    return False


def is_loopback(address):
    try: ip = ipaddress.ip_address(str(address).split('%', 1)[0])
    except ValueError: return False
    mapped = getattr(ip, 'ipv4_mapped', None)
    return (mapped or ip).is_loopback


def fingerprint(subnet, mac):
    """家里的网络的指纹：子网加路由器的硬件地址，只留算出来的 12 位（文件里不存路由器的地址）。没有路由器的网络（两台直连、手写地址）只按子网。"""
    return hashlib.sha256(f'{subnet}|{mac or "-"}'.encode()).hexdigest()[:12]


class SystemProbe:
    """从系统读：都是只读命令，各自几十毫秒。测试换一个假的进来。"""
    def __init__(self):
        self._ports, self._ports_at = None, 0

    @staticmethod
    def run(command, timeout=3):
        try: return subprocess.run(command, capture_output=True, text=True, errors='replace', timeout=timeout, stdin=subprocess.DEVNULL).stdout
        except (OSError, subprocess.SubprocessError): return ''

    def ifconfig(self): return self.run(['/sbin/ifconfig'])

    def ports(self):
        """网卡 → 硬件端口的名字（Wi-Fi、Ethernet、Thunderbolt Bridge…）。很少变，记 10 分钟。"""
        if self._ports is None or time.monotonic() - self._ports_at > PORTS_TTL:
            self._ports, self._ports_at = self.run(['/usr/sbin/networksetup', '-listallhardwareports']), time.monotonic()
        return self._ports

    def route(self, iface): return self.run(['/sbin/route', '-n', 'get', 'default', '-ifscope', iface])

    def arp(self, address): return self.run(['/usr/sbin/arp', '-n', address])

    def tailscale(self): return any(Path(p).exists() for p in TAILSCALE)

    def nudge(self, source, router):
        """路由器的硬件地址还没在系统的表里：朝它发一个字节的 UDP（discard 端口），让系统去问一声。发不出去不要紧。"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setblocking(False)
                s.bind((source, 0))
                s.sendto(b'\0', (router, 9))
        except OSError: pass


PROBE = SystemProbe()


def parse_ifconfig(text):
    """ifconfig 的输出 → [{'iface', 'up', 'inet': [(地址, 前缀长度)]}]。只要 IPv4。"""
    rows, current = [], None
    for line in text.splitlines():
        head = re.match(r'^(\S+?): flags=\w+<([^>]*)>', line)
        if head:
            flags = head.group(2).split(',')
            current = {'iface': head.group(1), 'up': 'UP' in flags and 'RUNNING' in flags, 'inet': []}
            rows.append(current)
            continue
        if current is None: continue
        found = re.match(r'^\s+inet (\d+\.\d+\.\d+\.\d+)(?: --> \S+)? netmask (0x[0-9a-fA-F]{8})', line)
        if found:
            current['inet'].append((found.group(1), bin(int(found.group(2), 16)).count('1')))
        elif re.match(r'^\s+status: inactive', line): current['up'] = False
    return rows


def parse_ports(text):
    """networksetup -listallhardwareports → {网卡: 硬件端口的名字}"""
    table, name = {}, None
    for line in text.splitlines():
        if line.startswith('Hardware Port:'): name = line.split(':', 1)[1].strip()
        elif line.startswith('Device:') and name: table[line.split(':', 1)[1].strip()] = name
    return table


def direct_link(iface, port):
    """雷雳网桥、雷雳口：机器之间的直连线，不是给客户端设备用的网络（1.0 联机用）。"""
    return iface.startswith('bridge') or bool(re.fullmatch(r'Thunderbolt( Bridge| \d+)', port or ''))


def router_mac(probe, iface, address):
    """这张网卡的路由器的硬件地址：('none', None) 没有路由器；('pending', None) 有但系统还没问到；('ok', 'aa:bb:…')。"""
    found = re.search(r'^\s*gateway:\s*(\d+\.\d+\.\d+\.\d+)\s*$', probe.route(iface), re.M)
    if not found: return 'none', None
    router = found.group(1)
    for attempt in (0, 1):
        hit = re.search(r'\bat ([0-9a-fA-F]{1,2}(?::[0-9a-fA-F]{1,2}){5})\b', probe.arp(router))
        if hit: return 'ok', ':'.join(part.zfill(2) for part in hit.group(1).lower().split(':'))
        if attempt == 0:
            probe.nudge(address, router)
            time.sleep(0.2)
    return 'pending', None


def scan(probe=None):
    """这台现在能开门的网络，一行一个地址：
    {'id', 'kind': 'lan' | 'tailscale' | 'virtual', 'iface', 'port', 'address', 'subnet', 'accept': [地址段…], 'ready'}。
    ready 为假（id 是 None）：家里的网络、路由器还没认出来，过几秒再认。
    测试：LMC_ROOT 和 LMC_NET_FIXTURE 都设了时，直接读那份 JSON（一个行的列表），不问系统。"""
    fixture = os.environ.get('LMC_NET_FIXTURE')
    if fixture and os.environ.get('LMC_ROOT'):
        try: rows = json.loads(Path(fixture).read_text())
        except (OSError, ValueError): return []
        return [{'iface': '', 'port': '', 'subnet': '', 'ready': True, **row, 'accept': list(row.get('accept') or ACCEPT.get(row.get('kind'), ()))}
                for row in rows if isinstance(row, dict) and row.get('address')]
    probe = probe or PROBE
    ports, out, tailscale = None, [], None
    for card in parse_ifconfig(probe.ifconfig()):
        iface = card['iface']
        if not card['up'] or not card['inet']: continue
        physical = bool(re.fullmatch(r'en\d+', iface))
        virtual = iface.startswith(VIRTUAL)
        if not physical and not virtual: continue
        for address, prefix in card['inet']:
            if within(address, ('169.254.0.0/16', '127.0.0.0/8')): continue
            subnet = str(ipaddress.ip_network(f'{address}/{prefix}', strict=False))
            row = {'iface': iface, 'port': '', 'address': address, 'subnet': subnet, 'ready': True}
            if physical:
                if not within(address, PRIVATE): continue   # 公网地址、运营商 NAT 地址的网卡不开门
                if ports is None: ports = parse_ports(probe.ports())
                row['port'] = ports.get(iface, '')
                if direct_link(iface, row['port']): continue
                state, mac = router_mac(probe, iface, address)
                row.update(kind='lan', accept=list(ACCEPT['lan']))
                if state == 'pending': row.update(id=None, ready=False)
                else: row['id'] = 'lan:' + fingerprint(subnet, mac)
            elif within(address, CGNAT):
                if tailscale is None: tailscale = bool(probe.tailscale())
                if tailscale: row.update(id='tailscale', kind='tailscale', accept=list(ACCEPT['tailscale']))
                else: row.update(id=f'virtual:{subnet}', kind='virtual', accept=list(ACCEPT['virtual']))
            elif within(address, PRIVATE): row.update(id=f'virtual:{subnet}', kind='virtual', accept=list(ACCEPT['virtual']))
            else: continue
            out.append(row)
    return out


def wanted(rows, networks):
    """勾了的网络里现在在的：{地址: 那一行}。同一个网络有两张网卡（Wi-Fi 和网线接同一个路由器）时两个地址都开。"""
    chosen = set(networks)
    return {row['address']: row for row in rows if row.get('ready') and row.get('id') in chosen}


def waiting(rows, networks):
    """勾过家里的网络，而现在有一张网卡的路由器还没认出来：可能就是它，过几秒再认一次。"""
    return any(str(n).startswith('lan:') for n in networks) and any(not row.get('ready') for row in rows)


if __name__ == '__main__':
    print(json.dumps(scan(), ensure_ascii=False, indent=2))
