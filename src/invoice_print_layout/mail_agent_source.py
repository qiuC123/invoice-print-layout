"""Bounded read-only IMAP source for the automatic organizer.

This module returns original file bytes and mail provenance only.  It never
writes a cursor, imports old directories, changes mail flags or creates fees.
"""
from __future__ import annotations

import hashlib
import imaplib
import re
from datetime import date
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, cast

from invoice_print_layout import mail163
from invoice_print_layout.mail163 import ImapFactory, Mail163Settings, MailImportError
from invoice_print_layout.mail_search import MailContent, download, downloadable, files_from
from invoice_print_layout.receipt_intake import validate_image


MAX_MESSAGES = 50
MAX_MESSAGE_BYTES = 30 * 1024 * 1024
MAX_MATERIAL_BYTES = 20 * 1024 * 1024
MAX_BATCH_BYTES = 40 * 1024 * 1024
MAX_FILES = 20
MAX_LINKS = 10
MAX_MIME_PARTS = 100
MAX_TEXT_CHARS = 20_000
MAX_UID = 2**32 - 1
_SUFFIXES = {'.pdf', '.png', '.jpg', '.jpeg', '.webp', '.zip'}
_SIZE = re.compile(rb'\bRFC822\.SIZE\s+(\d+)\b', re.IGNORECASE)
_URL = re.compile(r'https?://[^\s<>"\']+')


def _positive_number(value: Any) -> int:
    if isinstance(value, bytes):
        value = value.decode('ascii', errors='strict')
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,10}', value):
        raise MailImportError('邮箱返回了无效的邮件标识，请稍后重试')
    number = int(value)
    if number < 1 or number > MAX_UID:
        raise MailImportError('邮箱返回了无效的邮件标识，请稍后重试')
    return number


def _uidvalidity(client: mail163.ImapConnection) -> str:
    response = getattr(client, 'response', None)
    if not callable(response):
        raise MailImportError('邮箱未提供 UIDVALIDITY，未读取邮件，请检查连接后重试')
    name, values = response('UIDVALIDITY')
    if name not in {'UIDVALIDITY', 'OK'} or not isinstance(values, (list, tuple)) or len(values) != 1:
        raise MailImportError('邮箱未提供有效的 UIDVALIDITY，未读取邮件，请稍后重试')
    return str(_positive_number(values[0]))


def _message_size(parts: Any) -> int:
    if not isinstance(parts, (list, tuple)) or len(parts) > MAX_MIME_PARTS:
        raise MailImportError('邮件大小读取失败，未继续读取原件，请稍后重试')
    sizes: set[int] = set()
    for part in parts:
        metadata = part[0] if isinstance(part, tuple) and part else part
        if isinstance(metadata, bytes):
            sizes.update(int(value) for value in _SIZE.findall(metadata))
    if len(sizes) != 1:
        raise MailImportError('邮件大小读取失败，未继续读取原件，请稍后重试')
    return sizes.pop()


def _allowed_link(url: str) -> bool:
    try:
        return downloadable(url)
    except ValueError:
        return False


def _message_files(raw: bytes, uid: int) -> dict[str, Any]:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    subject = str(message.get('Subject', ''))[:500]
    result: dict[str, Any] = {
        'uid': uid, 'subject': subject, 'sender': str(message.get('From', ''))[:500],
        'text': '', 'message_id': str(message.get('Message-ID', ''))[:500], 'files': [], 'issues': [],
    }
    issues: list[str] = result['issues']
    materials: list[dict[str, Any]] = result['files']
    parts = list(message.walk())
    if len(parts) > MAX_MIME_PARTS:
        issues.append('邮件内容部分过多，请在原邮箱中领取材料后上传。')
        return result
    parser = MailContent()
    text_parts: list[str] = []
    links: list[str] = []
    for part in parts:
        if part.get_content_type() not in {'text/html', 'text/plain'} or part.get_content_disposition() == 'attachment':
            continue
        try:
            content = str(part.get_content())
            if part.get_content_type() == 'text/html':
                parser.feed(content)
            else:
                text_parts.append(content)
                links.extend(value.rstrip('。，,；;）)') for value in _URL.findall(content))
        except (LookupError, UnicodeError, ValueError):
            issues.append('邮件正文编码无法读取，请在原邮箱中检查。')
    text = '\n'.join(text_parts + parser.text)
    # Signed claim links are transport data, not evidence for a project or goods.
    # Keep them out of later visible mail summaries and model prompts.
    result['text'] = _URL.sub('[邮件链接]', text)[:MAX_TEXT_CHARS]
    if len(text) > MAX_TEXT_CHARS:
        issues.append('邮件正文过长，已限制自动读取范围，请检查原邮件。')
    links = list(dict.fromkeys(links + parser.links))
    digests: set[str] = set()
    material_bytes = 0

    def add(payload: bytes, name: str, source: str) -> None:
        nonlocal material_bytes
        entries = files_from(payload, name)
        # Validate the entire source first: a rejected ZIP member must not leave
        # an apparently complete prefix of the same attachment.
        for filename, body in entries:
            if Path(filename).suffix.lower() != '.pdf':
                validate_image(filename, body)
        fresh: list[dict[str, Any]] = []
        fresh_hashes: set[str] = set()
        fresh_size = 0
        for filename, body in entries:
            digest = hashlib.sha256(body).hexdigest()
            if digest in digests or digest in fresh_hashes:
                continue
            fresh.append({'name': filename, 'payload': body, 'source': source})
            fresh_hashes.add(digest)
            fresh_size += len(body)
        if len(materials) + len(fresh) > MAX_FILES or material_bytes + fresh_size > MAX_MATERIAL_BYTES:
            raise ValueError('本封邮件材料超过自动处理上限')
        materials.extend(fresh)
        digests.update(fresh_hashes)
        material_bytes += fresh_size

    attachments = list(message.iter_attachments())
    if len(attachments) > MAX_FILES:
        issues.append('一封邮件的附件超过20份，请拆分邮件或手动上传。')
        return result
    for part in attachments:
        name = part.get_filename() or {
            'application/pdf': '电子发票.pdf', 'image/jpeg': '邮件图片.jpg',
            'image/png': '邮件图片.png', 'image/webp': '邮件图片.webp',
            'application/zip': '邮件附件.zip', 'application/x-zip-compressed': '邮件附件.zip',
        }.get(part.get_content_type(), '')
        if Path(name).suffix.lower() not in _SUFFIXES:
            continue
        try:
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes) or not payload or len(payload) > MAX_MATERIAL_BYTES:
                raise ValueError('邮件附件为空或过大')
            add(payload, name, 'mail_attachment')
        except (ValueError, OSError, RuntimeError, LookupError, UnicodeError):
            issues.append('邮件附件无效、过大或超过自动处理上限，请在原邮箱中检查后上传。')
    if len(links) > MAX_LINKS:
        issues.append('邮件链接过多，已限制自动领取数量，请检查原邮件。')
    unsupported = False
    for link in links[:MAX_LINKS]:
        if not _allowed_link(link):
            unsupported = unsupported or any(word in link.lower() for word in ('invoice', 'fapiao', 'download'))
            continue
        try:
            add(download(link), '邮件发票', 'mail_link')
        except (ValueError, OSError, RuntimeError, MailImportError):
            issues.append('正文发票链接未取得可用文件，可能已过期或需要登录，请在原邮件中领取后上传。')
    if unsupported:
        issues.append('邮件中有暂不支持自动领取的发票链接，请在原邮件中领取后上传。')
    invoice_mail = any(word in subject + text for word in ('发票', '开票', '票据', '行程单', '购买凭证', '付款凭证'))
    if not materials and (unsupported or invoice_mail) and not issues:
        issues.append('邮件中没有可直接收取的材料，可能需要扫码或打开领票页面，请在原邮件中领取后上传。')
    result['issues'] = list(dict.fromkeys(issues))
    return result


def fetch_messages(
    settings: Mail163Settings,
    auth_code: str,
    *,
    since: date,
    after_uid: int = 0,
    uidvalidity: str = '',
    limit: int = 20,
    imap_factory: ImapFactory | None = None,
) -> dict[str, Any]:
    """Return one bounded batch, leaving durable acknowledgement to the caller.

    A changed UIDVALIDITY restarts inside ``since`` instead of applying another
    epoch's UID. Every successful return includes all selected UIDs, including
    ordinary mail with no materials. A transport failure aborts the whole batch
    so a caller cannot accidentally acknowledge a missing message.
    """
    if (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_MESSAGES
            or isinstance(after_uid, bool) or not isinstance(after_uid, int) or not 0 <= after_uid <= MAX_UID
            or not isinstance(since, date)):
        raise MailImportError('自动收件范围无效，请检查起始日期和收件数量')
    if uidvalidity:
        try:
            _positive_number(uidvalidity)
        except (MailImportError, UnicodeError):
            raise MailImportError('保存的邮箱标识无效，请重新检查收件配置') from None
    client: mail163.ImapConnection | None = None
    try:
        checked = Mail163Settings(mail163.validate_163_address(settings.address), mail163._validate_folder(settings.folder))
        client = mail163._connect(checked, auth_code, imap_factory or cast(ImapFactory, imaplib.IMAP4_SSL))
        epoch = _uidvalidity(client)
        floor = after_uid if epoch == uidvalidity else 0
        if floor == MAX_UID:
            return {'uidvalidity': epoch, 'messages': [], 'more': False}
        # UID ranges ending in * can return the highest old UID when no new UID
        # exists. Numeric post-filtering below is therefore mandatory.
        status, data = client.uid('SEARCH', None, 'SINCE', mail163._imap_date(since), 'UID', f'{floor + 1}:*')
        if status != 'OK' or not data or not isinstance(data[0], bytes) or len(data[0]) > 2 * 1024 * 1024:
            raise MailImportError('自动收件检索失败，请稍后重试')
        uids = sorted({_positive_number(value) for value in data[0].split()} - {0})
        uids = [value for value in uids if value > floor]
        result: dict[str, Any] = {'uidvalidity': epoch, 'messages': [], 'more': len(uids) > limit}
        batch_bytes = 0
        for uid in uids[:limit]:
            identity = str(uid).encode('ascii')
            status, size_data = client.uid('FETCH', identity, '(RFC822.SIZE)')
            if status != 'OK':
                raise MailImportError('自动收件读取邮件大小失败，请稍后重试')
            size = _message_size(size_data)
            if size > MAX_MESSAGE_BYTES:
                result['messages'].append({
                    'uid': uid, 'subject': '', 'sender': '', 'text': '', 'message_id': '', 'files': [],
                    'issues': ['一封邮件超过30MB，未下载正文，请在原邮箱中领取材料后上传。'],
                })
                continue
            if size <= 0:
                raise MailImportError('邮箱返回了空邮件，未确认本轮收件，请稍后重试')
            status, fetch_data = client.uid('FETCH', identity, '(BODY.PEEK[])')
            if status != 'OK':
                raise MailImportError('自动收件读取邮件失败，请稍后重试')
            raw = mail163._raw_message(fetch_data)
            if not raw or len(raw) > MAX_MESSAGE_BYTES:
                raise MailImportError('邮箱返回的邮件大小异常，未确认本轮收件，请稍后重试')
            parsed = _message_files(raw, uid)
            message_bytes = sum(len(entry['payload']) for entry in parsed['files'])
            if batch_bytes + message_bytes > MAX_BATCH_BYTES:
                # This UID was inspected but is not acknowledged.  The next
                # batch starts here again, using the last returned UID only.
                result['more'] = True
                break
            result['messages'].append(parsed)
            batch_bytes += message_bytes
        return result
    except (MailImportError, imaplib.IMAP4.error, OSError, ValueError, TypeError, UnicodeError):
        # Raw transport messages can contain server diagnostics or signed links.
        # Do not leak credentials/links through exceptions or their cause chain.
        raise MailImportError('自动收件未完成，请检查邮箱连接和收件配置后重试；尚未确认本轮邮件。') from None
    finally:
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
