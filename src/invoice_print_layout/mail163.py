from __future__ import annotations

import hashlib
import html.parser
import imaplib
import json
import os
import re
import ssl
import tomllib
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Protocol, cast

import keyring

from invoice_print_layout.storage import (
    WorkspacePaths,
    append_history,
    read_history,
    safe_filename_part,
)


IMAP_HOST = "imap.163.com"
IMAP_PORT = 993
KEYRING_SERVICE = "invoice-print-layout:163"
_MAIL_KEYWORDS = ("发票", "行程")
_TAKEOUT_SUBJECT_MARKERS = ("淘宝闪购", "淘宝闪购平台订单发票")
_TAKEOUT_ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".pdf", ".zip"}
_MAX_REMOTE_FILE_BYTES = 30 * 1024 * 1024
_IMAP_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


class MailImportError(RuntimeError):
    """Raised when mail import cannot proceed safely."""


class ImapConnection(Protocol):
    def login(self, user: str, password: str) -> tuple[str, list[bytes]]: ...

    def xatom(self, name: str, *args: str) -> tuple[str, list[bytes]]: ...

    def select(
        self, mailbox: str = "INBOX", readonly: bool = False
    ) -> tuple[str, list[bytes]]: ...

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]: ...

    def logout(self) -> tuple[str, list[bytes]]: ...


ImapFactory = Callable[..., ImapConnection]
UrlDownloader = Callable[[str], bytes]


@dataclass(frozen=True)
class Mail163Settings:
    address: str
    folder: str = "INBOX"


@dataclass
class MailImportSummary:
    downloaded: int = 0
    duplicates: int = 0
    ignored: int = 0
    invalid_pdf: int = 0
    link_downloaded: int = 0
    image_downloaded: int = 0
    link_failures: list[dict[str, str]] = field(default_factory=list)


class _HrefParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.anchors: list[tuple[str, str]] = []
        self._anchor_href = ""
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.hrefs.append(value)
                self._anchor_href = value
                self._anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._anchor_href:
            self._anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._anchor_href:
            self.anchors.append((self._anchor_href, "".join(self._anchor_text)))
            self._anchor_href = ""
            self._anchor_text = []


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


def validate_163_address(address: str) -> str:
    normalized = address.strip().lower()
    if not re.fullmatch(r"[a-z0-9._%+-]+@163\.com", normalized):
        raise MailImportError("请输入完整的 163 邮箱地址，例如 name@163.com")
    return normalized


def _validate_folder(folder: str) -> str:
    normalized = folder.strip() or "INBOX"
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", normalized):
        raise MailImportError("邮箱文件夹暂只支持英文字母、数字及 . _ / -")
    return normalized


def write_mail_settings(path: Path, settings: Mail163Settings) -> None:
    address = settings.address.replace("\\", "\\\\").replace('"', '\\"')
    folder = settings.folder.replace("\\", "\\\\").replace('"', '\\"')
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        f'[mail163]\naddress = "{address}"\nfolder = "{folder}"\n',
        encoding="utf-8",
    )
    os.replace(temporary, path)


def read_mail_settings(path: Path) -> Mail163Settings:
    if not path.exists():
        raise MailImportError("尚未配置 163 邮箱，请先运行 mail setup")
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
        section = data.get("mail163")
        if not isinstance(section, dict):
            raise MailImportError("163 邮箱配置不完整，请重新运行 mail setup")
        address = section.get("address")
        folder = section.get("folder", "INBOX")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise MailImportError(f"邮箱配置无法读取：{exc}") from exc
    if not isinstance(address, str) or not isinstance(folder, str) or not folder.strip():
        raise MailImportError("163 邮箱配置不完整，请重新运行 mail setup")
    return Mail163Settings(validate_163_address(address), _validate_folder(folder))


def validate_auth_code(auth_code: str) -> str:
    normalized = auth_code.strip()
    if (
        len(normalized) < 6
        or len(normalized) > 64
        or any(character.isspace() for character in normalized)
    ):
        raise MailImportError("客户端授权码格式异常，请只粘贴网易生成的授权密码")
    return normalized


def save_auth_code(address: str, auth_code: str) -> None:
    normalized = validate_auth_code(auth_code)
    try:
        keyring.set_password(KEYRING_SERVICE, address, normalized)
    except keyring.errors.KeyringError as exc:
        raise MailImportError(f"无法写入 Windows 凭据管理器：{exc}") from exc


def read_auth_code(address: str) -> str:
    try:
        value = keyring.get_password(KEYRING_SERVICE, address)
    except keyring.errors.KeyringError as exc:
        raise MailImportError(f"无法读取 Windows 凭据管理器：{exc}") from exc
    if not value:
        raise MailImportError("未找到客户端授权码，请重新运行 mail setup")
    return value


def configure_mail(path: Path, address: str, folder: str, auth_code: str) -> Mail163Settings:
    settings = Mail163Settings(validate_163_address(address), _validate_folder(folder))
    save_auth_code(settings.address, auth_code)
    write_mail_settings(path, settings)
    return settings


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _selected_uidvalidity(client: ImapConnection) -> str:
    """Read the selected mailbox epoch; missing scope never grants authority.

    Legacy callers may still collect valid files when an IMAP implementation
    omits this response. Such files deliberately retain only legacy metadata.
    """
    response = getattr(client, 'response', None)
    if not callable(response):
        return ''
    try:
        name, values = response('UIDVALIDITY')
        if name not in {'UIDVALIDITY', 'OK'} or not isinstance(values, (list, tuple)) or len(values) != 1:
            return ''
        raw = values[0]
        value = raw.decode('ascii') if isinstance(raw, bytes) else raw
        if (not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,10}', value) or
                not 0 < int(value) <= 2**32 - 1):
            return ''
        return str(int(value))
    except (imaplib.IMAP4.error, OSError, ValueError, TypeError, UnicodeError):
        return ''


def _manual_mail_origin(settings: Mail163Settings, epoch: str, message_uid: bytes,
                        message: EmailMessage) -> dict[str, Any]:
    """Create scoped provenance only from this fetch's server/header values."""
    if not epoch:
        return {}
    try:
        uid_text = message_uid.decode('ascii')
    except UnicodeError:
        return {}
    if not re.fullmatch(r'[0-9]{1,10}', uid_text) or not 0 < int(uid_text) <= 2**32 - 1:
        return {}
    message_id = str(message.get('Message-ID', ''))
    if len(message_id) > 1000 or any(char in message_id for char in '\r\n\0'):
        return {}

    def identity(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
        return _sha256_bytes(encoded)

    account = identity([IMAP_HOST, settings.address, settings.folder])
    uid = int(uid_text)
    return {'host': IMAP_HOST, 'address': settings.address, 'account_id': account,
            'mailbox': settings.folder, 'uidvalidity': epoch, 'uid': uid,
            'message_id': message_id, 'message_key': identity([account, epoch, uid])}


def _known_pdf_hashes(paths: WorkspacePaths) -> set[str]:
    hashes: set[str] = set()
    for root in (paths.incoming, paths.archived, paths.review):
        for pdf_path in root.rglob("*.pdf"):
            try:
                hashes.add(_sha256_bytes(pdf_path.read_bytes()))
            except OSError as exc:
                raise MailImportError(f"无法检查已有附件：{pdf_path.name}：{exc}") from exc
    return hashes


def _imap_date(value: date) -> str:
    return f"{value.day:02d}-{_IMAP_MONTHS[value.month - 1]}-{value.year:04d}"


def _raw_message(fetch_data: Iterable[Any]) -> bytes:
    for item in fetch_data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    raise MailImportError("163 邮箱返回了无法解析的邮件内容")


def _pdf_attachments(message: EmailMessage) -> list[tuple[str, bytes]]:
    subject = str(message.get("subject", ""))
    candidates: list[tuple[str, bytes]] = []
    for part in message.iter_attachments():
        filename = part.get_filename() or "附件.pdf"
        is_pdf = part.get_content_type() == "application/pdf" or filename.lower().endswith(".pdf")
        if not is_pdf or not any(keyword in subject + filename for keyword in _MAIL_KEYWORDS):
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            candidates.append((filename, payload))
    return candidates


def _unique_destination(directory: Path, filename: str) -> Path:
    source = Path(safe_filename_part(Path(filename).name))
    stem = source.stem or "邮件附件"
    suffix = source.suffix if source.suffix.lower() == ".pdf" else ".pdf"
    candidate = directory / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


def _unique_any_destination(directory: Path, filename: str) -> Path:
    source = Path(safe_filename_part(Path(filename).name))
    candidate = directory / source.name
    counter = 2
    while candidate.exists():
        candidate = directory / f"{source.stem}_{counter}{source.suffix}"
        counter += 1
    return candidate


def _takeout_links(message: EmailMessage) -> tuple[str, list[str]]:
    subject = str(message.get("subject", ""))
    if not any(marker in subject for marker in _TAKEOUT_SUBJECT_MARKERS):
        return "", []
    body_parts: list[str] = []
    links: list[str] = []
    for part in message.walk():
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        try:
            content = str(part.get_content())
        except (LookupError, UnicodeError):
            continue
        body_parts.append(content)
        if part.get_content_type() == "text/html":
            parser = _HrefParser()
            parser.feed(content)
            links.extend(parser.hrefs)
    body = "\n".join(body_parts)
    order_numbers = set(re.findall(r"(?<!\d)(\d{16,24})(?!\d)", body))
    order_number = next(iter(order_numbers)) if len(order_numbers) == 1 else ""
    allowed: list[str] = []
    for link in links:
        if _is_takeout_file_url(link):
            allowed.append(link)
    return order_number, list(dict.fromkeys(allowed))


def _is_takeout_file_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = (parsed.hostname or "").lower()
        return (parsed.scheme == "https" and parsed.netloc.lower() == hostname
                and not parsed.username and not parsed.password
                and hostname.endswith(".aliyuncs.com")
                and Path(parsed.path).suffix.lower() in _TAKEOUT_ALLOWED_SUFFIXES)
    except ValueError:
        return False


def _takeout_material_links(message: EmailMessage) -> tuple[str, list[str], list[str]]:
    """Find invoice anchors, excluding unrelated images and navigation links."""
    subject = str(message.get("subject", ""))
    if not any(marker in subject for marker in _TAKEOUT_SUBJECT_MARKERS):
        return "", [], []
    body_parts: list[str] = []
    candidates: list[str] = []
    for part in message.walk():
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        try:
            content = str(part.get_content())
        except (LookupError, UnicodeError):
            continue
        body_parts.append(content)
        if part.get_content_type() == "text/html":
            parser = _HrefParser()
            parser.feed(content)
            candidates.extend(url for url, label in parser.anchors
                              if "发票" in label or "invoice" in urllib.parse.urlparse(url).path.lower())
        else:
            for line in content.splitlines():
                for url in re.findall(r'https?://[^\s<>"\']+', line):
                    url = url.rstrip("。，,；;）)")
                    if "发票" in line or "invoice" in urllib.parse.urlparse(url).path.lower():
                        candidates.append(url)
    numbers = set(re.findall(r"(?<!\d)(\d{16,24})(?!\d)", "\n".join(body_parts)))
    order_number = next(iter(numbers)) if len(numbers) == 1 else ""
    allowed: list[str] = []
    unsupported: list[str] = []
    for link in dict.fromkeys(candidates):
        if _is_takeout_file_url(link):
            allowed.append(link)
        else:
            unsupported.append(link)
    return order_number, allowed, unsupported


def _save_mail_material(
    paths: WorkspacePaths,
    directory: Path,
    filename: str,
    payload: bytes,
    known_hashes: set[str],
    summary: MailImportSummary,
    metadata: dict[str, Any],
) -> None:
    digest = _sha256_bytes(payload)
    if digest in known_hashes:
        summary.duplicates += 1
        return
    saved_path = _unique_any_destination(directory, filename)
    temporary = paths.staging / f"mail-material-{digest}.tmp"
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, saved_path)
        append_history(paths.mail_history, {
            "attachment_hash": digest,
            "attachment_name": saved_path.name,
            "imported_at": date.today().isoformat(),
            "saved_to": str(saved_path),
            **metadata,
        })
    finally:
        temporary.unlink(missing_ok=True)
    known_hashes.add(digest)
    summary.downloaded += 1
    summary.link_downloaded += 1
    if saved_path.suffix.lower() != ".pdf":
        summary.image_downloaded += 1


def _import_mail_link_materials(
    message: EmailMessage,
    message_uid: str,
    paths: WorkspacePaths,
    directory: Path,
    known_hashes: set[str],
    summary: MailImportSummary,
    downloader: UrlDownloader,
    mail_origin: dict[str, Any] | None = None,
) -> bool:
    # Lazy import avoids the mail_search -> mail163 module dependency at load time.
    from invoice_print_layout.mail_search import files_from
    from invoice_print_layout.receipt_intake import validate_image

    order_number, links, unsupported = _takeout_material_links(message)
    metadata = {
        "kind": "taobao_takeout_link",
        "source_kind": "mail_link",
        "message_uid": message_uid,
        "subject": str(message.get("Subject", "")),
        "order_number": order_number,
    }
    for link in unsupported:
        summary.link_failures.append({**metadata, "source_url": link,
                                      "reason": "此发票链接暂不支持自动下载，请在原邮件中领取"})
    for index, link in enumerate(links, start=1):
        try:
            payload = downloader(link)
            filename = f"淘宝闪购_{order_number or ('邮件' + message_uid)}_{index}"
            materials = files_from(payload, filename)
            # Apply the intake's exact image limits before saving any member.
            # A ZIP with one unsupported image must not persist earlier members
            # and then be acknowledged while receive() rejects its contents.
            for name, body in materials:
                if Path(name).suffix.lower() != ".pdf":
                    validate_image(name, body)
            for name, body in materials:
                _save_mail_material(paths, directory, name, body, known_hashes, summary,
                                    {**metadata, "source_url": link,
                                     **({'mail_origin_v1': mail_origin} if mail_origin else {})})
        except (MailImportError, OSError, ValueError, RuntimeError):
            # Never expose a signed URL through an exception string, and never
            # mark failed downloads successful in the hash history.
            summary.link_failures.append({**metadata, "source_url": link,
                                          "reason": "发票链接下载或文件校验失败，请重试或在原邮件中重新领取"})
    return bool(links or unsupported)


def _download_takeout_url(url: str) -> bytes:
    if not _is_takeout_file_url(url):
        raise MailImportError("此淘宝发票地址暂不支持自动下载，请在原邮件中领取")
    request = urllib.request.Request(url, headers={"User-Agent": "invoice-print-layout/0.2"})
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=30) as response:
            if not _is_takeout_file_url(response.geturl()):
                raise MailImportError("淘宝发票下载发生了不安全的跳转")
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > _MAX_REMOTE_FILE_BYTES:
                raise MailImportError("淘宝发票文件超过30MB")
            payload = cast(bytes, response.read(_MAX_REMOTE_FILE_BYTES + 1))
    except (OSError, ValueError) as exc:
        raise MailImportError("淘宝发票链接下载失败或已过期，请在原邮件中重新领取") from exc
    if len(payload) > _MAX_REMOTE_FILE_BYTES:
        raise MailImportError("淘宝发票文件超过30MB")
    if not payload:
        raise MailImportError("淘宝发票链接返回空文件")
    return payload


def _connect(
    settings: Mail163Settings,
    auth_code: str,
    imap_factory: ImapFactory,
) -> ImapConnection:
    client: ImapConnection | None = None
    try:
        client = imap_factory(
            IMAP_HOST,
            IMAP_PORT,
            ssl_context=ssl.create_default_context(),
            timeout=30,
        )
        status, _ = client.login(settings.address, auth_code)
        if status != "OK":
            raise MailImportError("163 邮箱登录失败，请检查邮箱地址和客户端授权码")
        status, _ = client.xatom(
            "ID",
            '("name" "invoice-print-layout" "version" "0.2.0" "vendor" "local")',
        )
        if status != "OK":
            raise MailImportError("163 邮箱拒绝客户端身份信息")
        status, _ = client.select(settings.folder, readonly=True)
        if status != "OK":
            raise MailImportError(f"无法只读打开邮箱文件夹：{settings.folder}")
        return client
    except MailImportError:
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
        raise
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
        raise MailImportError(f"无法连接 163 邮箱：{exc}") from exc


def test_connection(settings: Mail163Settings, auth_code: str, *,
                    imap_factory: ImapFactory | None = None) -> dict[str, Any]:
    """Only login, identify and select read-only; never fetch or mark a message."""
    try:
        client = _connect(settings, auth_code, imap_factory or cast(ImapFactory, imaplib.IMAP4_SSL))
    except Exception:
        return {'ok': False, 'message': '连接失败，请检查163邮箱、文件夹和客户端授权码。'}
    try:
        client.logout()
    except Exception:
        pass
    return {'ok': True, 'message': '连接成功，已只读打开邮箱文件夹。'}


def import_pdf_attachments(
    paths: WorkspacePaths,
    settings: Mail163Settings,
    auth_code: str,
    days: int = 30,
    *,
    today: date | None = None,
    imap_factory: ImapFactory | None = None,
    courier_kind: str | None = None,
    destination: Path | None = None,
    all_invoice_types: bool = False,
    include_link_materials: bool = False,
    downloader: UrlDownloader | None = None,
) -> MailImportSummary:
    """Collect PDF attachments, optionally including known invoice body links.

    The opt-in is for the material desk: validated link images remain original
    images for the intake QR/claim flow. Legacy PDF-only callers keep their
    existing behavior, and a failed link is reported rather than acknowledged.
    """
    if courier_kind not in {None, "顺丰", "同城"}:
        raise MailImportError("不支持的快递类型")
    if days < 1 or days > 365:
        raise MailImportError("检索天数必须在 1 到 365 之间")
    settings = Mail163Settings(
        validate_163_address(settings.address),
        _validate_folder(settings.folder),
    )
    factory = imap_factory or cast(ImapFactory, imaplib.IMAP4_SSL)
    summary = MailImportSummary()
    known_hashes = _known_pdf_hashes(paths)
    target_directory = destination or paths.incoming
    target_directory.mkdir(parents=True, exist_ok=True)
    known_hashes.update(_sha256_bytes(p.read_bytes()) for p in target_directory.glob("*.pdf"))
    if include_link_materials:
        for directory in {paths.incoming, paths.archived, paths.review, target_directory}:
            known_hashes.update(_sha256_bytes(p.read_bytes()) for p in directory.rglob("*")
                                if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})
    since = (today or date.today()) - timedelta(days=days - 1)
    client = _connect(settings, auth_code, factory)
    try:
        epoch = _selected_uidvalidity(client)
        status, search_data = client.uid("SEARCH", None, "SINCE", _imap_date(since))
        if status != "OK" or not search_data or not isinstance(search_data[0], bytes):
            raise MailImportError("163 邮箱邮件检索失败")
        message_uids = search_data[0].split()
        for message_uid in message_uids:
            status, fetch_data = client.uid("FETCH", message_uid, "(BODY.PEEK[])")
            if status != "OK":
                raise MailImportError("163 邮箱邮件读取失败")
            message = BytesParser(policy=policy.default).parsebytes(_raw_message(fetch_data))
            mail_origin = _manual_mail_origin(settings, epoch, message_uid, message)
            identity = str(message.get("Subject", "")) + " " + " ".join(
                part.get_filename() or "" for part in message.walk()
            )
            detected = "同城" if "顺丰同城" in identity else "顺丰" if "顺丰" in identity else None
            if not all_invoice_types and not (detected == courier_kind or (courier_kind == "顺丰" and detected == "同城")):
                summary.ignored += 1
                continue
            attachments = _pdf_attachments(message)
            has_links = False
            if include_link_materials:
                has_links = _import_mail_link_materials(
                    message, message_uid.decode("ascii", errors="replace"), paths,
                    target_directory, known_hashes, summary, downloader or _download_takeout_url,
                    mail_origin,
                )
            if not attachments and not has_links:
                summary.ignored += 1
                continue
            for filename, payload in attachments:
                if b"%PDF-" not in payload[:1024]:
                    summary.invalid_pdf += 1
                    continue
                digest = _sha256_bytes(payload)
                if digest in known_hashes:
                    summary.duplicates += 1
                    continue
                saved_path = _unique_destination(target_directory, filename)
                temporary = paths.staging / f"mail-{digest}.tmp"
                temporary.write_bytes(payload)
                os.replace(temporary, saved_path)
                append_history(
                    paths.mail_history,
                    {
                        "attachment_hash": digest,
                        "attachment_name": filename,
                        "imported_at": date.today().isoformat(),
                        "message_uid": message_uid.decode("ascii", errors="replace"),
                        "subject": str(message.get("Subject", "")),
                        "source_kind": "mail_attachment",
                        "saved_to": str(saved_path),
                        **({'mail_origin_v1': mail_origin} if mail_origin else {}),
                    },
                )
                known_hashes.add(digest)
                summary.downloaded += 1
        return summary
    except MailImportError:
        raise
    except (imaplib.IMAP4.error, OSError, ValueError) as exc:
        raise MailImportError(f"导入邮件附件失败：{exc}") from exc
    finally:
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def import_takeout_invoice_links(
    destination: Path,
    paths: WorkspacePaths,
    settings: Mail163Settings,
    auth_code: str,
    days: int = 1,
    *,
    today: date | None = None,
    imap_factory: ImapFactory | None = None,
    downloader: UrlDownloader | None = None,
) -> MailImportSummary:
    """Download Taobao takeout invoice files linked by today's notification mail."""
    if days < 1 or days > 365:
        raise MailImportError("检索天数必须在 1 到 365 之间")
    destination.mkdir(parents=True, exist_ok=True)
    settings = Mail163Settings(
        validate_163_address(settings.address), _validate_folder(settings.folder)
    )
    factory = imap_factory or cast(ImapFactory, imaplib.IMAP4_SSL)
    fetch_url = downloader or _download_takeout_url
    summary = MailImportSummary()
    known_hashes = {
        str(entry.get("attachment_hash"))
        for entry in read_history(paths.mail_history)
        if entry.get("attachment_hash")
    }
    known_hashes.update(
        _sha256_bytes(path.read_bytes()) for path in destination.iterdir() if path.is_file()
    )
    since = (today or date.today()) - timedelta(days=days - 1)
    client = _connect(settings, auth_code, factory)
    try:
        status, search_data = client.uid("SEARCH", None, "SINCE", _imap_date(since))
        if status != "OK" or not search_data or not isinstance(search_data[0], bytes):
            raise MailImportError("163 邮箱邮件检索失败")
        for message_uid in search_data[0].split():
            status, fetch_data = client.uid("FETCH", message_uid, "(BODY.PEEK[])")
            if status != "OK":
                raise MailImportError("163 邮箱邮件读取失败")
            message = BytesParser(policy=policy.default).parsebytes(_raw_message(fetch_data))
            order_number, links = _takeout_links(message)
            if not links:
                summary.ignored += 1
                continue
            if not order_number:
                raise MailImportError("淘宝发票通知中未找到唯一订单号")
            for index, link in enumerate(links, start=1):
                payload = fetch_url(link)
                digest = _sha256_bytes(payload)
                if digest in known_hashes:
                    summary.duplicates += 1
                    continue
                suffix = Path(urllib.parse.urlparse(link).path).suffix.lower()
                destination_path = _unique_any_destination(
                    destination, f"{order_number}_{index}{suffix}"
                )
                temporary = paths.staging / f"takeout-mail-{digest}.tmp"
                temporary.write_bytes(payload)
                os.replace(temporary, destination_path)
                append_history(
                    paths.mail_history,
                    {
                        "attachment_hash": digest,
                        "attachment_name": destination_path.name,
                        "imported_at": date.today().isoformat(),
                        "kind": "taobao_takeout",
                        "message_uid": message_uid.decode("ascii", errors="replace"),
                        "order_number": order_number,
                        "saved_to": str(destination_path),
                    },
                )
                known_hashes.add(digest)
                summary.downloaded += 1
        return summary
    except MailImportError:
        raise
    except (imaplib.IMAP4.error, OSError, ValueError) as exc:
        raise MailImportError(f"导入淘宝外卖发票失败：{exc}") from exc
    finally:
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
