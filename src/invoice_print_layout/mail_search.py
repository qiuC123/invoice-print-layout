"""Read-only mail discovery; candidates are separate from confirmed expense files."""
from __future__ import annotations

import base64
import hashlib
import html.parser
import importlib
import io
import re
import uuid
import zipfile
import imaplib
import urllib.parse
import urllib.request
import urllib.error
from datetime import date, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, cast

import pymupdf
from PIL import Image

from invoice_print_layout import mail163
from invoice_print_layout.reliability import read_json, save_json
from invoice_print_layout.storage import ensure_workspace
from invoice_print_layout.workbench import ExpenseStore, MAX_BYTES


class MailContent(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.images: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == 'a' and values.get('href'):
            self.links.append(str(values['href']))
        if tag == 'img' and values.get('src'):
            self.images.append(str(values['src']))

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def downloadable(url: str) -> bool:
    """Only known file endpoints, never arbitrary mail-provided network targets."""
    p = urllib.parse.urlparse(url)
    if p.scheme != 'https' or p.username or p.password or p.netloc != p.hostname:
        return False
    host = p.hostname or ''
    return (host.endswith('.aliyuncs.com') and Path(p.path).suffix.lower() in {'.pdf', '.png', '.jpg', '.jpeg', '.zip'}
            or host in {'www.fapiao.com', 'fapiao.com'} and p.path in {
                '/dzfp-web/pdf/download', '/DownLoad/downloadController/download'})


def download(url: str) -> bytes:
    if not downloadable(url):
        raise ValueError('此领票地址需要在原邮件中打开')
    # Every redirect is revalidated; login pages and arbitrary hosts are not followed.
    opener = urllib.request.build_opener(mail163._NoRedirect())
    try:
        for _ in range(4):
            try:
                with opener.open(urllib.request.Request(url, headers={'User-Agent': 'invoice-print-layout/0.2'}), timeout=20) as r:
                    if int(r.headers.get('Content-Length', '0')) > MAX_BYTES:
                        raise ValueError('发票超过20MB')
                    payload = cast(bytes, r.read(MAX_BYTES + 1))
                break
            except urllib.error.HTTPError as exc:
                location = urllib.parse.urljoin(url, exc.headers.get('Location', ''))
                if exc.code not in {301, 302, 303, 307, 308} or not downloadable(location):
                    raise ValueError('领票跳转需要人工处理') from exc
                url = location
        else:
            raise ValueError('领票跳转次数过多')
    except (OSError, ValueError) as exc:
        # Signed URLs and tokens must not enter HTTP error messages.
        raise ValueError('下载失败或链接已过期，请在原邮件中重新领取') from exc
    if not payload or len(payload) > MAX_BYTES:
        raise ValueError('文件为空或超过20MB')
    return payload


def file_kind(payload: bytes) -> str:
    if not payload or len(payload) > MAX_BYTES:
        raise ValueError('文件为空或超过20MB')
    if payload.startswith(b'%PDF-'):
        try:
            with pymupdf.open(stream=payload, filetype='pdf') as doc:  # type: ignore[no-untyped-call]
                if doc.needs_pass or not len(doc):
                    raise ValueError('PDF需要密码或没有页面')
        except Exception as exc:
            raise ValueError('PDF无法读取') from exc
        return '.pdf'
    try:
        with Image.open(io.BytesIO(payload)) as im:
            kind = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}.get(im.format or '')
            if not kind:
                raise ValueError('不是支持的发票文件')
            im.verify()
        return kind
    except Exception as exc:
        raise ValueError('返回的不是PDF或图片，可能需要登录或重新领取') from exc


def files_from(payload: bytes, name: str) -> list[tuple[str, bytes]]:
    if payload.startswith(b'PK'):
        if len(payload) > MAX_BYTES:
            raise ValueError('压缩包超过20MB')
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as z:
                entries = z.infolist()
                if len(entries) > 30 or sum(e.file_size for e in entries) > MAX_BYTES:
                    raise ValueError('压缩包文件过多或解压后超过20MB')
                result = []
                for e in entries:
                    if e.is_dir() or Path(e.filename).suffix.lower() not in {'.pdf', '.png', '.jpg', '.jpeg', '.webp'}:
                        continue
                    body = z.read(e)
                    kind = file_kind(body)
                    result.append((Path(e.filename.replace('\\', '/')).stem + kind, body))
                if not result:
                    raise ValueError('压缩包中没有可用的PDF或图片')
                return result
        except (zipfile.BadZipFile, RuntimeError) as exc:
            raise ValueError('压缩包损坏或需要密码') from exc
    return [(Path(name.replace('\\', '/')).stem[:100] + file_kind(payload), payload)]


def match_reasons(item: dict[str, Any], text: str) -> list[str]:
    compact = re.sub(r'\s+', '', text)
    reasons = []
    order = str(item.get('order_number', '')).strip()
    if order and re.search(r'(?<!\d)' + re.escape(order) + r'(?!\d)', compact):
        reasons.append('订单号一致')
    merchant = re.sub(r'\s+', '', str(item.get('merchant', '')))
    if len(merchant) >= 3 and merchant in compact:
        reasons.append('出现相同商家名称')
    amount = str(item.get('amount', ''))
    if amount and re.search(r'(?<![\d.])' + re.escape(amount) + r'(?!\d)', compact):
        reasons.append('出现金额' + amount + '元（仅线索）')
    return reasons


def qr_value(payload: bytes) -> str:
    try:
        cv = importlib.import_module('cv2')
        np = importlib.import_module('numpy')
        with Image.open(io.BytesIO(payload)) as im:
            if im.width * im.height > 16_000_000:
                return ''
        pixels = cv.imdecode(np.frombuffer(payload, dtype=np.uint8), cv.IMREAD_GRAYSCALE)
        if pixels is None:
            return ''
        for scale in (1, 3, 5):
            enlarged = cv.resize(pixels, None, fx=scale, fy=scale, interpolation=cv.INTER_NEAREST)
            enlarged = cv.copyMakeBorder(enlarged, 40, 40, 40, 40, cv.BORDER_CONSTANT, value=255)
            value, _, _ = cv.QRCodeDetector().detectAndDecode(enlarged)
            if value:
                return str(value)
        return ''
    except Exception:
        return ''


def _safe_link(url: str) -> str:
    p = urllib.parse.urlparse(url)
    if p.scheme in {'https', 'http'} and p.hostname and not p.username and not p.password:
        return url
    return ''


def candidate_file(store: ExpenseStore, search_id: str, candidate_id: str) -> tuple[Path, str]:
    manifest = _manifest(store, search_id)
    c = next((c for c in manifest['candidates'] if c['id'] == candidate_id), None)
    if not c or not c.get('file'):
        raise ValueError('候选文件不存在')
    folder = store.root / '发票查找' / search_id
    path = (folder / c['file']).resolve()
    if not path.is_relative_to(folder.resolve()) or not path.is_file():
        raise ValueError('候选文件不可用')
    if hashlib.sha256(path.read_bytes()).hexdigest() != c['digest']:
        raise ValueError('候选文件已变化，请重新查找')
    return path, str(c['name'])


def _manifest(store: ExpenseStore, search_id: str) -> dict[str, Any]:
    if not re.fullmatch('[a-f0-9]{32}', search_id):
        raise ValueError('无效的查找记录')
    result = read_json(store.root / '发票查找' / search_id / 'result.json')
    if not result:
        raise ValueError('查找记录不存在')
    return result


def associate(store: ExpenseStore, item_id: str, search_id: str, candidate_id: str,
              role: str = 'invoice') -> dict[str, Any]:
    if role not in {'invoice', 'trip', 'detail', 'other'}:
        raise ValueError('请选择发票、行程单、费用明细或其他材料')
    manifest = _manifest(store, search_id)
    if manifest['item_id'] != item_id:
        raise ValueError('候选不属于当前事项的查找，请重新查找')
    item = store.get(item_id)
    if item['stage'] != 'draft':
        raise ValueError('只有待提交事项可以关联发票')
    baseline = manifest['item_snapshot']
    if any(item.get(k) != baseline.get(k) for k in ('project', 'amount_cents', 'merchant', 'order_number')):
        raise ValueError('事项已修改，请重新查找后确认')
    c = next((c for c in manifest['candidates'] if c['id'] == candidate_id), None)
    if not c or c['status'] != 'downloaded':
        raise ValueError('该候选尚未取得发票文件')
    path, name = candidate_file(store, search_id, candidate_id)
    with store.connect() as db:
        used = db.execute('SELECT item_id FROM attachments WHERE digest=?', (c['digest'],)).fetchall()
    if any(r['item_id'] != item_id for r in used):
        raise ValueError('此文件已关联其他事项，请先核对是否重复费用')
    if used:
        return {'ok': True, 'duplicate': True}
    store.add_attachment(item_id, name, path.read_bytes(), role)
    c['associated_item_id'] = item_id
    c['associated_role'] = role
    save_json(store.root / '发票查找' / search_id / 'result.json', manifest)
    return {'ok': True, 'duplicate': False}


def search_invoices(store: ExpenseStore, item_id: str, start: str, end: str, *,
                    imap_factory: mail163.ImapFactory | None = None,
                    downloader: mail163.UrlDownloader = download) -> dict[str, Any]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last or (last - first).days > 365 or last > date.today():
        raise ValueError('请选择不超过366天且不晚于今天的日期范围')
    item = store.get(item_id)
    if item['stage'] != 'draft':
        raise ValueError('请在待提交事项中查找发票')
    paths = ensure_workspace(store.workspace)
    settings = mail163.read_mail_settings(paths.mail_config)
    client = mail163._connect(settings, mail163.read_auth_code(settings.address),
                             imap_factory or cast(mail163.ImapFactory, imaplib.IMAP4_SSL))
    search_id = uuid.uuid4().hex
    folder = store.root / '发票查找' / search_id
    folder.mkdir(parents=True)
    candidates: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen: set[str] = set()
    result: dict[str, Any] = {'search_id': search_id, 'item_id': item_id, 'item_title': item['title'],
                             'item_snapshot': {k: item.get(k) for k in ('project', 'amount_cents', 'merchant', 'order_number')},
                             'start': start, 'end': end, 'candidates': candidates, 'warnings': warnings,
                             'scanned': 0, 'invoice_mails': 0, 'truncated': False}

    def add(source: dict[str, Any], status: str, name: str, text: str,
            body: bytes | None = None, link: str = '', detail: str = '') -> None:
        if body is not None:
            digest = hashlib.sha256(body).hexdigest()
            if digest in seen:
                return
            seen.add(digest)
        else:
            digest = ''
        key = uuid.uuid4().hex
        c: dict[str, Any] = {'id': key, 'name': name[:200], 'status': status, 'source': source,
                              'detail': detail, 'link': _safe_link(link), 'reasons': match_reasons(item, text),
                              'digest': digest, 'file': ''}
        c['suggested_role'] = 'trip' if '行程' in name else 'detail' if any(w in name for w in ('运单', '订单详情', '存根')) else 'invoice'
        if body is not None:
            suffix = file_kind(body)
            c['file'] = key + suffix
            (folder / c['file']).write_bytes(body)
            c['preview'] = f'/mail-candidate/{search_id}/{key}'
        candidates.append(c)

    def add_files(source: dict[str, Any], payload: bytes, name: str, text: str) -> None:
        for filename, body in files_from(payload, name):
            evidence = text
            if body.startswith(b'%PDF-'):
                with pymupdf.open(stream=body, filetype='pdf') as doc:  # type: ignore[no-untyped-call]
                    evidence += '\n' + '\n'.join(str(doc[n].get_text()) for n in range(min(20, len(doc))))
            add(source, 'downloaded', filename, evidence, body)

    try:
        status, data = client.uid('SEARCH', None, 'SINCE', mail163._imap_date(first),
                                  'BEFORE', mail163._imap_date(last + timedelta(days=1)))
        if status != 'OK' or not data or not isinstance(data[0], bytes):
            raise ValueError('邮箱查询失败，请重试')
        uids = data[0].split()
        result['truncated'] = len(uids) > 300
        if result['truncated']:
            warnings.append('本次仅检查范围内最新300封邮件，请缩小日期范围继续查找')
        for uid in reversed(uids[-300:]):
            status, parts = client.uid('FETCH', uid, '(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])')
            if status != 'OK':
                warnings.append('部分邮件头读取失败，请重试')
                continue
            head = BytesParser(policy=policy.default).parsebytes(mail163._raw_message(parts))
            result['scanned'] += 1
            subject = str(head.get('subject', ''))
            if not any(word in subject for word in ('发票', '开票', '票据', '行程')):
                continue
            status, parts = client.uid('FETCH', uid, '(BODY.PEEK[])')
            if status != 'OK':
                warnings.append('部分发票邮件读取失败，请重试')
                continue
            raw = mail163._raw_message(parts)
            if len(raw) > 30 * 1024 * 1024:
                warnings.append('一封邮件超过30MB，请在邮箱中领取')
                continue
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            result['invoice_mails'] += 1
            source = {'subject': subject[:300], 'sender': str(msg.get('from', ''))[:300],
                      'date': str(msg.get('date', ''))[:100], 'uid': uid.decode('ascii'), 'folder': settings.folder}
            parser = MailContent()
            plain = []
            for part in msg.walk():
                if part.get_content_type() in {'text/html', 'text/plain'}:
                    content = str(part.get_content())
                    if part.get_content_type() == 'text/html':
                        parser.feed(content)
                    else:
                        plain.append(content)
            text = subject + '\n' + '\n'.join(plain + parser.text)
            before = len(candidates)
            handled_file = False
            for part in msg.iter_attachments():
                name = part.get_filename() or ''
                if Path(name).suffix.lower() not in {'.pdf', '.png', '.jpg', '.jpeg', '.webp', '.zip'}:
                    continue
                try:
                    add_files(source, cast(bytes, part.get_payload(decode=True)), name, text)
                    handled_file = True
                except (ValueError, OSError) as exc:
                    add(source, 'needs_action', name, text, detail=str(exc))
            for url in dict.fromkeys(parser.links):
                if downloadable(url):
                    try:
                        order = mail163._takeout_links(msg)[0]
                        filename = '淘宝发票_' + order if order else '发票通' if 'fapiao.com' in url else '邮件发票'
                        add_files(source, downloader(url), filename, text)
                        handled_file = True
                    except (ValueError, OSError):
                        add(source, 'needs_action', '领票链接', text, link=url,
                            detail='未取得可用发票：链接可能过期、需要登录，或文件不受支持。请打开原链接领取后上传。')
            # Inline data and CID QR images are decoded locally; remote ad images are not fetched.
            cid_parts = {str(p.get('Content-ID', '')).strip('<>'): p for p in msg.walk() if p.get('Content-ID')}
            for src in parser.images:
                body = None
                try:
                    if src.startswith('data:image/') and ';base64,' in src and len(src) < 4 * 1024 * 1024:
                        body = base64.b64decode(src.split(';base64,', 1)[1], validate=True)
                    elif src.startswith('cid:') and src[4:] in cid_parts:
                        body = cast(bytes, cid_parts[src[4:]].get_payload(decode=True))
                    if body:
                        value = qr_value(body)
                        if value and not handled_file:
                            add(source, 'needs_action', '扫码领取发票', text, body, link=value,
                                detail='已解析邮件二维码；请打开领取页面，或用微信扫描下方二维码。取得发票后上传到本事项。')
                except (ValueError, OSError):
                    continue
            if len(candidates) == before and not handled_file:
                # A duplicate downloaded file still counts as successfully handled.
                direct = any(downloadable(url) for url in parser.links)
                if not direct:
                    links = [url for url in parser.links if _safe_link(url) and any(w in url.lower() for w in ('invoice', 'fapiao', 'download'))]
                    add(source, 'needs_action', '需要打开邮件领取', text, link=links[0] if links else '',
                        detail='没有可直接收取的发票文件，请在原邮件打开领票页面或扫码；领取后在事项内上传。')
        candidates.sort(key=lambda c: ('订单号一致' in c['reasons'], len(c['reasons'])), reverse=True)
        save_json(folder / 'result.json', result)
        return result
    finally:
        try:
            client.logout()
        except (OSError, imaplib.IMAP4.error):
            pass
