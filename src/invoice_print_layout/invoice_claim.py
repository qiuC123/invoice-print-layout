"""Bounded adapter for the verified Shanghai tax QR delivery endpoint."""
from __future__ import annotations

from datetime import datetime
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit, urlencode
import urllib.request

from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.mail163 import _NoRedirect
from invoice_print_layout.workbench import MAX_BYTES

BASE = 'https://dppt.shanghai.chinatax.gov.cn:8443'
PREFIX = '/kpfw/fpjfzz/v1/'


def request(endpoint: str, values: dict[str, Any], *, get: bool = False) -> bytes:
    url = BASE + PREFIX + endpoint
    payload = None if get else json.dumps(values).encode()
    if get:
        url += '?' + urlencode(values)
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json', 'User-Agent': 'invoice-print-layout/0.2'})
    with urllib.request.build_opener(_NoRedirect()).open(req, timeout=25) as response:
        result: bytes = response.read(MAX_BYTES + 1)
    if len(result) > MAX_BYTES:
        raise ValueError('文件超过20MB')
    return result


def claim(queue: IntakeQueue, url: str, project_id: str = '') -> dict[str, Any]:
    parsed = urlsplit(url)
    if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('请提供有效领票网页链接')
    fallback = {'status': 'manual', 'url': url, 'message': '请打开领票入口完成领取，再上传原始PDF；二维码不是发票。'}
    if parsed.scheme != 'https' or parsed.hostname != 'dppt.shanghai.chinatax.gov.cn' or parsed.port not in {None, 8443}:
        return fallback
    match = re.fullmatch(r'/v/([A-Za-z0-9_]{10,150})', parsed.path)
    if not match or parsed.query or parsed.fragment:
        return fallback
    try:
        info = json.loads(request('queryFpjcxxb', {'Cs': match[1]}))
        if str(info.get('code')) != '1':
            return fallback
        number = str(info['fphm'])
        if not re.fullmatch(r'\d{8,30}', number):
            return fallback
        args = {'Fphm': number, 'Kprq': datetime.strptime(info['kprq'], '%Y-%m-%d %H:%M:%S').strftime('%Y%m%d%H%M%S')}
        ready = json.loads(request('queryFpmxNum', args))
        args.update(Wjgs='PDF', Jym=str(info['jym']), Czsj=str(int(time.time() * 1000)))
        if ready.get('fileName'):
            args['fileName'] = str(ready['fileName'])
        logged = json.loads(request('saveDzfpxzjlxx', args))
        if str(logged.get('code')) != '1':
            return fallback
        pdf = request('exportDzfpwjEwm', args, get=True)
        if not pdf.startswith(b'%PDF'):
            return fallback
        entry = queue.receive(number + '.pdf', pdf, source='hotel_qr', project_id=project_id)
        return {'status': 'received', 'entry': entry, 'message': '原始PDF已进入统一收件，尚未核对或提交。'}
    except (ValueError, KeyError, OSError):
        return {**fallback, 'message': '自动领取未完成，可能已过期或需要登录。请打开原入口领取；已收材料保留。'}
