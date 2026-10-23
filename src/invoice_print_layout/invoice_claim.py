"""Bounded adapters for supported official Shanghai/Jiangxi tax QR delivery."""
from __future__ import annotations

from datetime import datetime
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit, urlencode
import urllib.request

import pymupdf

from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.documents import parse_invoice_number
from invoice_print_layout.mail163 import _NoRedirect
from invoice_print_layout.workbench import MAX_BYTES

BASE = 'https://dppt.shanghai.chinatax.gov.cn:8443'
TAX_BASES = {'dppt.shanghai.chinatax.gov.cn': BASE,
             'dppt.jiangxi.chinatax.gov.cn': 'https://dppt.jiangxi.chinatax.gov.cn:8443'}
PREFIX = '/kpfw/fpjfzz/v1/'
ENDPOINTS = {'queryFpjcxxb', 'queryFpmxNum', 'saveDzfpxzjlxx', 'exportDzfpwjEwm'}


def request(endpoint: str, values: dict[str, Any], *, get: bool = False, base: str = BASE) -> bytes:
    if base not in TAX_BASES.values() or endpoint not in ENDPOINTS:
        raise ValueError('不支持的税务领票接口')
    url = base + PREFIX + endpoint
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
    if parsed.scheme != 'https' or parsed.hostname not in TAX_BASES or parsed.port not in {None, 8443}:
        return fallback
    match = re.fullmatch(r'/v/([A-Za-z0-9_]{10,150})', parsed.path)
    if not match or parsed.query or parsed.fragment:
        return fallback
    try:
        base = TAX_BASES[parsed.hostname]
        def fetch(endpoint: str, values: dict[str, Any], *, get: bool = False) -> bytes:
            # Keep the original Shanghai adapter's public call shape compatible.
            if base == BASE:
                return request(endpoint, values, get=get)
            return request(endpoint, values, get=get, base=base)
        info = json.loads(fetch('queryFpjcxxb', {'Cs': match[1]}))
        if not isinstance(info, dict) or str(info.get('code')) != '1':
            return fallback
        number = str(info['fphm'])
        if not re.fullmatch(r'\d{8,30}', number):
            return fallback
        qr_number = re.match(r'\d+_(\d{8,30})_', match[1])
        if qr_number and qr_number[1] != number:
            return {**fallback, 'message': '领票返回号码与二维码不一致，请在原入口人工核对。'}
        args = {'Fphm': number, 'Kprq': datetime.strptime(info['kprq'], '%Y-%m-%d %H:%M:%S').strftime('%Y%m%d%H%M%S')}
        ready = json.loads(fetch('queryFpmxNum', args))
        # Both ready (1) and not-yet-generated (3) proceed to the verified
        # save/export sequence. Its success and the actual PDF are still checked.
        if not isinstance(ready, dict) or ('code' in ready and str(ready['code']) not in {'1', '3'}):
            return fallback
        args.update(Wjgs='PDF', Jym=str(info['jym']), Czsj=str(int(time.time() * 1000)))
        if ready.get('fileName'):
            args['fileName'] = str(ready['fileName'])
        logged = json.loads(fetch('saveDzfpxzjlxx', args))
        if not isinstance(logged, dict) or str(logged.get('code')) != '1':
            return fallback
        pdf = fetch('exportDzfpwjEwm', args, get=True)
        if not pdf.startswith(b'%PDF'):
            return fallback
        with pymupdf.open(stream=pdf, filetype='pdf') as document:  # type: ignore[no-untyped-call]
            if document.needs_pass or not document.page_count:
                return fallback
            text = '\n'.join(page.get_text() for page in list(document)[:10])
            compact = re.sub(r'\s+', '', text)
            if (not any(label in compact.upper() for label in ('电子发票', '增值税', 'ELECTRONICINVOICE')) or
                    parse_invoice_number(text) != number):
                return {**fallback, 'message': '领取文件未能对应二维码中的发票号码，请打开原入口核对；二维码不是发票。'}
        entry = queue.receive(number + '.pdf', pdf, source='hotel_qr', project_id=project_id)
        return {'status': 'received', 'entry': entry, 'message': '原始PDF已进入统一收件，尚未核对或提交。'}
    except (ValueError, KeyError, TypeError, OSError, RuntimeError):
        return {**fallback, 'message': '自动领取未完成，可能已过期或需要登录。请打开原入口领取；已收材料保留。'}
