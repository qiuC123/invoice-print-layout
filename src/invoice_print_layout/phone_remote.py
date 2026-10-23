"""An opt-in, per-user Tailscale node for the phone HTTP listener only.

The official signed Windows binaries are prepared separately. No system VPN,
router rule, public Funnel, account credential, or second ledger is created.
"""
from __future__ import annotations

import ipaddress
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

PIPE = r'\\.\pipe\invoice-assistant-private'


def private_origin(status: dict[str, Any]) -> str:
    node = status.get('Self') or {}
    name = str(node.get('DNSName', '')).rstrip('.').lower()
    if not re.fullmatch(r'[a-z0-9-]+\.[a-z0-9-]+\.ts\.net', name):
        raise ValueError('私人网络尚未分配设备地址，请完成登录后刷新')
    ips = node.get('TailscaleIPs') or []
    try:
        valid = any(ipaddress.ip_address(ip) in ipaddress.ip_network('100.64.0.0/10')
                    for ip in ips if ':' not in ip)
    except ValueError:
        valid = False
    if status.get('BackendState') != 'Running' or not valid:
        raise ValueError('私人网络尚未连接，请完成登录后刷新')
    return 'http://' + name


class PrivateNetwork:
    def __init__(self, workspace: Path) -> None:
        self.root = workspace.resolve() / 'phone-remote-runtime'
        self.cli = self.root / 'bin' / 'tailscale.exe'
        self.daemon = self.root / 'bin' / 'tailscaled.exe'
        self.login: subprocess.Popen[bytes] | None = None
        self.pipe = PIPE + '-' + hashlib.sha256(str(workspace.resolve()).casefold().encode()).hexdigest()[:16]
        saved = self.root / 'pipe.txt'
        if saved.is_file():
            name = saved.read_text(encoding='utf-8-sig').strip()
            if re.fullmatch(re.escape(PIPE) + r'(?:-[a-f0-9]{16})?', name):
                self.pipe = name

    def run(self, *args: str) -> str:
        try:
            result = subprocess.run([str(self.cli), '--socket=' + self.pipe, *args],
                                    capture_output=True, text=True, encoding='utf-8', timeout=8,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError('私人连接组件未响应，请重试或重新打开发票助手') from exc
        if result.returncode:
            raise ValueError('私人连接操作未完成，请刷新连接状态或查看本机连接日志')
        return result.stdout

    def raw_status(self) -> dict[str, Any]:
        value = json.loads(self.run('status', '--json'))
        if not isinstance(value, dict):
            raise ValueError('私人连接状态无效')
        return value

    def status(self) -> dict[str, Any]:
        if not self.cli.is_file() or not self.daemon.is_file():
            return {'installed': False, 'state': 'NotInstalled', 'ready': False}
        try:
            raw = self.raw_status()
            value: dict[str, Any] = {'installed': True, 'state': raw.get('BackendState', 'Stopped'),
                                     'ready': False}
            auth = str(raw.get('AuthURL') or '')
            if re.fullmatch(r'https://login\.tailscale\.com/a/[a-zA-Z0-9]+', auth):
                value['auth_url'] = auth
            if raw.get('BackendState') == 'Running':
                value.update(ready=True, origin=private_origin(raw))
            return value
        except (ValueError, OSError):
            return {'installed': True, 'state': 'Stopped', 'ready': False}

    def background(self, executable: Path, args: list[str], name: str) -> subprocess.Popen[bytes]:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / (name + '.stdout.log')).open('ab') as stdout, (
                self.root / (name + '.stderr.log')).open('ab') as stderr:
            return subprocess.Popen([str(executable), *args], stdin=subprocess.DEVNULL,
                                    stdout=stdout, stderr=stderr,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

    def connect(self) -> dict[str, Any]:
        current = self.status()
        if not current['installed']:
            raise ValueError('请先运行 scripts/prepare-phone-remote.ps1 安装专用连接组件')
        if current['ready'] or current.get('auth_url'):
            return current
        if current['state'] == 'Stopped':
            # A working daemon may simply be disconnected; only launch if its
            # local pipe is unavailable. Never install a machine-wide service.
            try:
                self.raw_status()
            except ValueError:
                state = self.root / 'state'
                state.mkdir(parents=True, exist_ok=True)
                self.background(self.daemon, ['--windows-mode=dev', '--tun=userspace-networking',
                                '--socket=' + self.pipe, '--port=0', '--no-logs-no-support',
                                '--statedir=' + str(state)], 'daemon')
        if self.login is None or self.login.poll() is not None:
            self.login = self.background(self.cli, ['--socket=' + self.pipe, 'up', '--hostname=invoice-desktop',
                                         '--accept-dns=false', '--accept-routes=false', '--unattended=true'], 'login')
        return self.status()

    def expose(self, port: int) -> str:
        origin = private_origin(self.raw_status())
        existing = json.loads(self.run('serve', 'status', '--json'))
        expected = {'TCP': {'80': {'HTTP': True}}, 'Web': {
            origin.removeprefix('http://') + ':80': {'Handlers': {'/': {'Proxy': f'http://127.0.0.1:{port}'}}}}}
        if existing and {key: val for key, val in existing.items() if val} != expected:
            raise ValueError('专用连接已有转发配置，请先关闭手机连接后重试')
        try:
            self.run('serve', '--bg', '--http=80', f'http://127.0.0.1:{port}')
            config = json.loads(self.run('serve', 'status', '--json'))
            # Unknown listeners or any Funnel flag are refused, even if a
            # future CLI changes defaults. This node belongs to this feature.
            actual = {key: val for key, val in config.items() if val}
            if actual != expected:
                raise ValueError('私人转发配置与预期不符，已关闭连接')
        except ValueError:
            self.close()
            raise
        return origin

    def close(self) -> None:
        self.run('serve', '--http=80', 'off')
