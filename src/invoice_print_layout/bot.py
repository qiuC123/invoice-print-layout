from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tomllib
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import keyring

from invoice_print_layout.delivery import DeliveryError, create_delivery_bundle, outputs_for_processing_date
from invoice_print_layout.mail163 import MailImportError, MailImportSummary
from invoice_print_layout.memory import format_bytes, refresh_memory_status
from invoice_print_layout.models import BatchSummary, OutcomeStatus
from invoice_print_layout.storage import ensure_workspace, move_to_review, read_person_name, read_history
from invoice_print_layout.reliability import save_json, read_json
from invoice_print_layout.takeout import process_takeout_batch
from invoice_print_layout.courier import process_courier_batch
from invoice_print_layout.workbench import ExpenseStore, CATEGORIES
from invoice_print_layout.workflow import process_workspace


KEYRING_SERVICE = "invoice-print-layout-feishu"
MAX_FILE_BYTES = 30 * 1024 * 1024
_APP_ID_PATTERN = re.compile(r"cli_[A-Za-z0-9]{8,}")
_ALLOWED_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".zip"}
HELP_COMMANDS = frozenset({"帮助", "指令", "指令合集", "菜单", "?", "？", "help", "/help"})
COMMAND_HELP = """票据机器人 · 指令合集

【最常用】
工作台：查看本机工作台入口和事项登记方法。
登记 酒店 500 两晚住宿：先登记支出，可使用材料采购、酒店、高铁、外卖、打车、顺丰等类别。随后上传材料，晚到材料用“关联 事项编号”追加。
结束登记：退出事项收件，不删除事项和材料。
打车：检查今天的163邮件，下载并自动处理打车PDF；没有新附件时按提示手动上传，再发“处理”。
外卖：下载今天的淘宝闪购发票，等待你上传订单截图；全部上传完后发“处理”。
顺丰：一起下载今天的顺丰快递和顺丰同城发票及明细，自动识别配对，返回一份总PDF和MD。
处理：开始处理当前材料；优先续办可恢复的中断任务。
汇总：合并今天已处理的票据；也可发“汇总 昨天”或“汇总 2026-09-04”。按北京时间的处理日期筛选，不查邮箱、不重复计费。

【查看与恢复】
状态：查看当前批次、已收文件数、待补发数量。
补发：重试未送达的结果，不重新处理或计费。
取消：结束当前收件批次，原文件保留；不撤销结果或清空待补发任务。
记忆：获取历史费用摘要和清理候选，不删除文件。
帮助：再次查看本合集，也可以发送“菜单”“指令”或一个问号 ?。

【操作路线】
打车 → 等待总PDF和MD；需要补材料时按提示上传 → 处理。
外卖 → 等待下载完成 → 上传订单截图 → 处理。
顺丰 → 等待总PDF和MD；没有新附件时上传快递或同城的发票和明细PDF → 处理。

每条指令单独发送，不加“开始”。一次只操作一个收件批次。
每类票据的成功结果是本轮总PDF + MD费用清单；合并同一天的历史结果请发“汇总”。
最终总PDF打印设置：A4、1×1（每张纸1页）。票据已在PDF内拼好，不要再次选择1×2。
电脑须开机、登录并联网；电脑关机或休眠时无法处理。"""


class BotError(ValueError):
    """Raised when a bot operation cannot be completed safely."""


def parse_summary_date(command: str, today: date | None = None) -> date:
    current = today or datetime.now(timezone(timedelta(hours=8))).date()
    argument = command.removeprefix("汇总").strip()
    if not argument or argument == "今天":
        return current
    if argument == "昨天":
        return current - timedelta(days=1)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", argument):
        raise BotError("请发送“汇总”“汇总 昨天”或“汇总 YYYY-MM-DD”，例如“汇总 2026-09-04”。")
    try:
        target = date.fromisoformat(argument)
    except ValueError as exc:
        raise BotError("日期无效，请使用真实日期，例如“汇总 2026-09-04”。") from exc
    if target > current:
        raise BotError("不能汇总未来日期，请选择今天或更早的处理日期。")
    return target


class BatchKind(StrEnum):
    RIDE = "打车"
    TAKEOUT = "外卖"
    EXPRESS = "顺丰"
    SAME_CITY = "同城"


@dataclass(frozen=True)
class FeishuBotSettings:
    app_id: str
    owner_open_id: str | None = None


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    chat_id: str
    chat_type: str
    sender_open_id: str
    sender_type: str
    message_type: str
    text: str | None = None
    resource_key: str | None = None
    file_name: str | None = None


@dataclass(frozen=True)
class DownloadedResource:
    file_name: str
    data: bytes


@dataclass(frozen=True)
class BotBatch:
    batch_id: str
    kind: BatchKind
    directory: Path
    files: tuple[Path, ...]


class BotGateway(Protocol):
    def reply_text(self, message_id: str, text: str) -> None: ...

    def download_resource(
        self,
        message_id: str,
        resource_key: str,
        resource_type: str,
        fallback_name: str,
    ) -> DownloadedResource: ...

    def send_file(self, chat_id: str, path: Path) -> None: ...


MailImporter = Callable[[], MailImportSummary]
TakeoutMailImporter = Callable[[Path], MailImportSummary]
CourierMailImporter = Callable[[Path, str], MailImportSummary]


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_bot_settings(config_path: Path, settings: FeishuBotSettings) -> None:
    lines = ["[feishu]", f"app_id = {_toml_string(settings.app_id)}"]
    if settings.owner_open_id:
        lines.append(f"owner_open_id = {_toml_string(settings.owner_open_id)}")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temporary, config_path)


def configure_bot(
    config_path: Path,
    app_id: str,
    app_secret: str,
) -> FeishuBotSettings:
    normalized_id = app_id.strip()
    normalized_secret = app_secret.strip()
    if not _APP_ID_PATTERN.fullmatch(normalized_id):
        raise BotError("飞书 App ID 格式异常，应以 cli_ 开头")
    if len(normalized_secret) < 16 or any(char.isspace() for char in normalized_secret):
        raise BotError("飞书 App Secret 格式异常")

    previous_owner: str | None = None
    if config_path.exists():
        try:
            previous = read_bot_settings(config_path)
            if previous.app_id == normalized_id:
                previous_owner = previous.owner_open_id
        except BotError:
            previous_owner = None

    try:
        keyring.set_password(KEYRING_SERVICE, normalized_id, normalized_secret)
    except keyring.errors.KeyringError as exc:
        raise BotError(f"无法保存飞书 App Secret：{exc}") from exc

    settings = FeishuBotSettings(normalized_id, previous_owner)
    write_bot_settings(config_path, settings)
    return settings


def read_bot_settings(config_path: Path) -> FeishuBotSettings:
    if not config_path.exists():
        raise BotError("尚未配置飞书机器人，请先运行 bot setup")
    try:
        with config_path.open("rb") as stream:
            data = tomllib.load(stream)
        section = data.get("feishu")
        if not isinstance(section, dict):
            raise BotError("飞书机器人配置不完整，请重新运行 bot setup")
        app_id = section.get("app_id")
        owner = section.get("owner_open_id")
        if not isinstance(app_id, str) or not _APP_ID_PATTERN.fullmatch(app_id):
            raise BotError("飞书机器人配置不完整，请重新运行 bot setup")
        if owner is not None and (not isinstance(owner, str) or not owner.strip()):
            raise BotError("飞书机器人绑定信息无效")
        return FeishuBotSettings(app_id, owner.strip() if isinstance(owner, str) else None)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BotError(f"无法读取飞书机器人配置：{exc}") from exc


def read_bot_secret(app_id: str) -> str:
    try:
        value = keyring.get_password(KEYRING_SERVICE, app_id)
    except keyring.errors.KeyringError as exc:
        raise BotError(f"无法读取飞书 App Secret：{exc}") from exc
    if not value:
        raise BotError("未找到飞书 App Secret，请重新运行 bot setup")
    return value


def new_pairing_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


class BotInbox:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.root = self.workspace_root / "机器人收件箱"
        self.state_path = self.root / "state.json"
        self.ledger_path = self.root / "message_ids.jsonl"
        self.root.mkdir(parents=True, exist_ok=True)

    def _read_state(self) -> dict[str, str]:
        if not self.state_path.exists():
            return {}
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BotError(f"机器人批次状态损坏：{exc}") from exc
        if not isinstance(value, dict):
            raise BotError("机器人批次状态损坏")
        return {str(key): str(item) for key, item in value.items()}

    def _write_state(self, value: dict[str, str]) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    def current(self) -> BotBatch | None:
        state = self._read_state()
        batch_id = state.get("batch_id")
        raw_kind = state.get("kind")
        if not batch_id or not raw_kind:
            return None
        try:
            kind = BatchKind(raw_kind)
        except ValueError as exc:
            raise BotError("机器人批次类型无效") from exc
        directory = self.root / batch_id
        files_dir = directory / "files"
        if not directory.is_dir() or not files_dir.is_dir():
            raise BotError("机器人当前批次目录不存在")
        files = tuple(sorted(path for path in files_dir.iterdir() if path.is_file()))
        return BotBatch(batch_id, kind, directory, files)

    def start(self, kind: BatchKind, request_id: str = '') -> BotBatch:
        active = self.current()
        if active is not None:
            if request_id and read_json(active.directory / 'metadata.json').get('request_id') == request_id:
                return active
            raise BotError(
                f"已有{active.kind.value}批次 {active.batch_id}，请先发送“处理”或“取消”"
            )
        batch_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        directory = self.root / batch_id
        (directory / "files").mkdir(parents=True)
        (directory / "metadata.json").write_text(
            json.dumps(
                {
                    "batch_id": batch_id,
                    "kind": kind.value,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "request_id": request_id,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self._write_state({"batch_id": batch_id, "kind": kind.value})
        return BotBatch(batch_id, kind, directory, ())

    def add_file(self, resource: DownloadedResource, message_id: str) -> tuple[Path, bool]:
        batch = self.current()
        if batch is None:
            raise BotError("还没有开始批次，请先发送“打车”“外卖”或“顺丰”")
        if not resource.data:
            raise BotError("收到的是空文件")
        if len(resource.data) > MAX_FILE_BYTES:
            raise BotError("文件超过30MB，飞书机器人暂不接收")

        safe_name = Path(resource.file_name).name
        suffix = Path(safe_name).suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            raise BotError("只接收 PDF、JPG、JPEG、PNG 或 WEBP 文件")
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", safe_name).strip(" .")
        if not safe_name:
            safe_name = f"附件{suffix}"

        digest = hashlib.sha256(resource.data).hexdigest()
        for existing in batch.files:
            if hashlib.sha256(existing.read_bytes()).hexdigest() == digest:
                return existing, True
        manifest_path = batch.directory / "manifest.jsonl"
        if manifest_path.exists():
            for line in manifest_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("sha256") == digest:
                    return batch.directory / "files" / str(entry["saved_name"]), True

        destination = batch.directory / "files" / safe_name
        counter = 2
        while destination.exists():
            destination = destination.with_name(f"{destination.stem}_{counter}{suffix}")
            counter += 1
        temporary = batch.directory / 'download.tmp'
        temporary.write_bytes(resource.data)
        os.replace(temporary, destination)
        with manifest_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(
                    {
                        "message_id": message_id,
                        "original_name": resource.file_name,
                        "saved_name": destination.name,
                        "sha256": digest,
                        "size": len(resource.data),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        return destination, False

    def clear_current(self, destination_name: str) -> Path:
        batch = self.current()
        if batch is None:
            raise BotError("当前没有批次")
        destination_root = self.root / destination_name
        destination_root.mkdir(parents=True, exist_ok=True)
        destination = destination_root / batch.batch_id
        if destination.exists():
            destination = destination.with_name(f"{batch.batch_id}_{uuid.uuid4().hex[:6]}")
        shutil.move(str(batch.directory), destination)
        self._write_state({})
        return destination

    def is_seen(self, message_id: str) -> bool:
        if not self.ledger_path.exists():
            return False
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("message_id") == message_id:
                return True
        return False

    def mark_seen(self, message_id: str) -> None:
        with self.ledger_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(
                    {
                        "message_id": message_id,
                        "handled_at": datetime.now().isoformat(timespec="seconds"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())


class BotController:
    def __init__(
        self,
        workspace_root: Path,
        config_path: Path,
        settings: FeishuBotSettings,
        pairing_code: str | None,
        gateway: BotGateway,
        mail_importer: MailImporter | None = None,
        takeout_mail_importer: TakeoutMailImporter | None = None,
        courier_mail_importer: CourierMailImporter | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.config_path = config_path
        self.settings = settings
        self.pairing_code = pairing_code
        self.gateway = gateway
        self.mail_importer = mail_importer
        self.takeout_mail_importer = takeout_mail_importer
        self.courier_mail_importer = courier_mail_importer
        self.inbox = BotInbox(self.workspace_root)
        self.jobs = self.workspace_root / '机器人任务'
        self.jobs.mkdir(parents=True, exist_ok=True)

    def _job_path(self, message: IncomingMessage) -> Path:
        return self.jobs / (hashlib.sha256(message.message_id.encode()).hexdigest() + '.json')

    def _run_processing(self, message: IncomingMessage, runner: Callable[[], BatchSummary]) -> BatchSummary:
        path = self._job_path(message)
        job = read_json(path)
        history_path = ensure_workspace(self.workspace_root).history
        if not job or job.get('phase') == 'downloading':
            paths = ensure_workspace(self.workspace_root)
            batch = self.inbox.current()
            inputs = list(paths.incoming.iterdir()) + (list(batch.files) if batch else [])
            job = {'message': asdict(message), 'phase': 'processing',
                   'batch_id': batch.batch_id if batch else None,
                   'input_hashes': [hashlib.sha256(file.read_bytes()).hexdigest() for file in inputs if file.is_file()],
                   'baseline': [item.get('output') for item in read_history(history_path)]}
            save_json(path, job)
        # A completed ledger entry is authoritative: never reprocess these invoices.
        recovered = [item for item in read_history(history_path)
                     if item.get('output') not in job['baseline'] and
                     (item.get('invoice_hash') in job.get('input_hashes', []) or
                      item.get('trip_hash') in job.get('input_hashes', []))]
        if recovered:
            summary = BatchSummary()
            for item in recovered:
                output = str(item['output'])
                summary.add(OutcomeStatus.SUCCESS, Path(output).name, output)
            summary.add(OutcomeStatus.NEEDS_REVIEW, '中断恢复',
                        '已恢复归档成功的结果；剩余附件保留在待处理或机器人收件箱，请核对后继续。')
        else:
            summary = runner()
        job.update(phase='result', outcomes=[asdict(item) for item in summary.outcomes])
        save_json(path, job)
        return summary

    def _saved_summary(self, job: dict[str, object]) -> BatchSummary:
        summary = BatchSummary()
        outcomes = job.get('outcomes', [])
        assert isinstance(outcomes, list)
        for item in outcomes:
            summary.add(OutcomeStatus(item['status']), item['label'], item['message'])
        return summary

    def resume_deliveries(self) -> None:
        failed = 0
        for path in sorted(self.jobs.glob('*.json')):
            job = read_json(path)
            if job.get('phase') in {'result', 'bundle'}:
                message = IncomingMessage(**job['message'])
                if message.sender_open_id == self.settings.owner_open_id:
                    try:
                        self._send_process_result(message, self._saved_summary(job))
                    except Exception:
                        failed += 1
        if failed:
            raise BotError(f'{failed} 份交付暂未发送成功，已保留，将继续自动补发')

    def _reply(self, message: IncomingMessage, text: str) -> None:
        self.gateway.reply_text(message.message_id, text)

    def _bind_if_requested(self, message: IncomingMessage) -> bool:
        if self.settings.owner_open_id:
            return False
        text = (message.text or "").strip()
        match = re.fullmatch(r"绑定\s*(\d{6})", text)
        if not match or self.pairing_code is None or match.group(1) != self.pairing_code:
            self._reply(message, "机器人尚未绑定，请在电脑上查看六位绑定码")
            return True
        self.settings = replace(self.settings, owner_open_id=message.sender_open_id)
        write_bot_settings(self.config_path, self.settings)
        self.pairing_code = None
        self._reply(message, "绑定成功。发送“打车”“外卖”或“顺丰”创建批次；发送“帮助”查看指令合集。")
        return True

    def handle(self, message: IncomingMessage) -> None:
        if self.inbox.is_seen(message.message_id):
            return
        if message.sender_type != "user" or message.chat_type != "p2p":
            self._reply(message, "第一版只支持本人和机器人的单聊消息")
            self.inbox.mark_seen(message.message_id)
            return
        if self._bind_if_requested(message):
            self.inbox.mark_seen(message.message_id)
            return
        if message.sender_open_id != self.settings.owner_open_id:
            self._reply(message, "无权使用此机器人")
            self.inbox.mark_seen(message.message_id)
            return

        job = read_json(self._job_path(message))
        if job.get('phase') == 'processing':
            records = read_history(ensure_workspace(self.workspace_root).history)
            if any(item.get('output') not in job['baseline'] and
                   (item.get('invoice_hash') in job.get('input_hashes', []) or
                    item.get('trip_hash') in job.get('input_hashes', [])) for item in records):
                summary = self._run_processing(message, BatchSummary)
                self._send_process_result(message, summary)
                self.inbox.mark_seen(message.message_id)
                return
        if job.get('phase') in {'result', 'bundle', 'sent'}:
            self._send_process_result(message, self._saved_summary(job))
            self.inbox.mark_seen(message.message_id)
            return

        if message.message_type == "text":
            self._handle_text(message)
        elif message.message_type in {"image", "file"}:
            self._handle_resource(message)
        else:
            self._reply(message, "暂不支持这种消息，请发送文字、图片或PDF文件")
        self.inbox.mark_seen(message.message_id)

    def _handle_text(self, message: IncomingMessage) -> None:
        command = re.sub(r"\s+", " ", (message.text or "").strip())
        if command == '工作台' or command.startswith(('登记 ', '关联 ')) or command == '结束登记':
            self._handle_workbench(message, command)
            return
        if command in {'打车', '外卖', '开始 外卖', '顺丰', '同城', '顺丰同城', '取消'}:
            ExpenseStore(self.workspace_root).select(message.sender_open_id, None)
        if command in HELP_COMMANDS:
            self._reply(message, COMMAND_HELP)
            return
        if command.startswith("汇总"):
            self._handle_summary_command(message, command)
            return
        if command == '补发':
            self.resume_deliveries()
            self._reply(message, '已检查待交付结果，未成功发送的文件已补发；已送达的文件不会重复发送。')
            return
        if command == "打车":
            self._handle_ride_command(message)
            return
        if command in {"外卖", "开始 外卖"}:
            self._handle_takeout_command(message)
            return
        if command in {"顺丰", "同城", "顺丰同城"}:
            self._handle_courier_command(message, BatchKind.EXPRESS)
            return
        if command == "状态":
            pending = sum(read_json(path).get('phase') in {'result', 'bundle'} for path in self.jobs.glob('*.json'))
            active = self.inbox.current()
            if active is None:
                self._reply(message, f"当前没有批次。待补发 {pending} 份交付。")
            else:
                self._reply(
                    message,
                    f"当前{active.kind.value}批次 {active.batch_id}，已接收 {len(active.files)} 个文件。待补发 {pending} 份交付。",
                )
            return
        if command == "记忆":
            self._handle_memory_command(message)
            return
        if command == "取消":
            destination = self.inbox.clear_current("已取消")
            self._reply(message, f"批次已取消，原文件仍保留在：{destination}")
            return
        if command == "处理":
            # Resume the original interrupted request before starting another accounting run.
            for path in ([] if self._job_path(message).exists() else sorted(self.jobs.glob('*.json'))):
                job = read_json(path)
                if job.get('phase') in {'processing', 'downloading'}:
                    original = IncomingMessage(**job['message'])
                    if original.sender_open_id == self.settings.owner_open_id and original.message_id != message.message_id:
                        self.handle(original)
                        return
            self._process_current(message)
            return
        self._reply(message, "无法识别该指令。发送“帮助”查看可用指令。")

    def _handle_summary_command(self, message: IncomingMessage, command: str) -> None:
        try:
            target = parse_summary_date(command)
            outputs = outputs_for_processing_date(ensure_workspace(self.workspace_root), target)
        except (BotError, DeliveryError, OSError, ValueError) as exc:
            self._reply(message, f"暂无法汇总：{exc}。原票据和当前收件批次未改动。")
            return
        if not outputs:
            self._reply(message, f"{target.isoformat()} 没有已完成的票据（按处理日期筛选）。本次未检查邮箱，也未生成空文件；当前收件批次保持不变。")
            return
        summary = BatchSummary()
        for output in outputs:
            summary.add(OutcomeStatus.SUCCESS, output.name, str(output))
        # Persist the absolute date and selected packages before delivery, so a retry
        # after midnight cannot turn '昨天' into a different set of invoices.
        save_json(self._job_path(message), {
            'message': asdict(message), 'phase': 'result',
            'report_date': target.isoformat(),
            'outcomes': [asdict(item) for item in summary.outcomes],
        })
        self._send_process_result(message, summary)

    def _handle_memory_command(self, message: IncomingMessage) -> None:
        paths = ensure_workspace(self.workspace_root)
        try:
            status = refresh_memory_status(paths)
        except (OSError, ValueError) as exc:
            raise BotError(f"无法生成长期索引摘要：{exc}") from exc
        self._reply(
            message,
            f"长期索引已更新：{status.record_count} 个票据组，"
            f"金额合计 {status.total_amount:.2f} 元，"
            f"字段待补全 {status.unresolved_count} 条。\n"
            f"超过7天的工作记忆清理候选 {len(status.cleanup_candidates)} 个，"
            f"共 {format_bytes(status.cleanup_size_bytes)}。"
            "本次未删除任何文件。",
        )
        self.gateway.send_file(message.chat_id, status.summary_path)

    def _handle_ride_command(self, message: IncomingMessage) -> None:
        active = self.inbox.current()
        if active is not None:
            if read_json(active.directory / 'metadata.json').get('request_id') == message.message_id:
                self._reply(message, f'已恢复{active.kind.value}批次，已接收 {len(active.files)} 个文件。发送“处理”继续。')
                return
            if active.kind is not BatchKind.RIDE or active.files:
                raise BotError(
                    f"已有{active.kind.value}批次 {active.batch_id}，请先发送“处理”或“取消”"
                )
            self.inbox.clear_current("已替换")

        if self.mail_importer is None:
            batch = self.inbox.start(BatchKind.RIDE, message.message_id)
            self._reply(
                message,
                f"已开始打车批次 {batch.batch_id}。请继续发送PDF，完成后发送“处理”。",
            )
            return

        paths = ensure_workspace(self.workspace_root)
        resuming = bool(read_json(self._job_path(message)))
        if any(paths.incoming.iterdir()) and not resuming:
            raise BotError("“待处理”目录中已有文件，为避免混批，未检查邮箱")
        person_name = read_person_name(paths.config)
        if person_name is None:
            raise BotError("尚未设置输出姓名，请先在电脑运行一次 process")

        job_path = self._job_path(message)
        if not resuming:
            save_json(job_path, {'phase': 'downloading', 'message': asdict(message)})
        try:
            imported = (MailImportSummary(downloaded=1) if read_json(job_path).get('phase') == 'processing'
                        else self.mail_importer())
        except MailImportError as exc:
            raise BotError(f"今日邮箱检查失败：{exc}") from exc

        if imported.downloaded == 0 and not any(paths.incoming.iterdir()):
            batch = self.inbox.start(BatchKind.RIDE, message.message_id)
            save_json(job_path, {'phase': 'waiting', 'message': asdict(message)})
            self._reply(
                message,
                "今天没有找到新的发票或行程 PDF。"
                f"重复 {imported.duplicates}，忽略 {imported.ignored}，"
                f"无效 PDF {imported.invalid_pdf}。\n"
                f"已开始手动打车批次 {batch.batch_id}，也可直接发送 PDF，然后发送“处理”。",
            )
            return

        summary = self._run_processing(message, lambda: process_workspace(paths.root, person_name)[1])
        self._send_process_result(
            message,
            summary,
            prefix=f"已从今日邮件下载 {imported.downloaded} 个 PDF。",
        )

    def _handle_takeout_command(self, message: IncomingMessage) -> None:
        batch = self.inbox.start(BatchKind.TAKEOUT, message.message_id)
        imported = MailImportSummary()
        warning = ""
        if self.takeout_mail_importer is not None:
            try:
                imported = self.takeout_mail_importer(batch.directory / "files")
            except MailImportError as exc:
                warning = f"\n今日邮箱发票下载失败：{exc}。可直接把发票文件发给我。"
        current = self.inbox.current()
        assert current is not None
        self._reply(
            message,
            f"已开始外卖批次 {batch.batch_id}，从今日邮箱取得 {imported.downloaded} 个发票文件。"
            f"请发送订单截图，完成后发送“处理”。当前共 {len(current.files)} 个文件。"
            f"{warning}",
        )

    def _handle_courier_command(self, message: IncomingMessage, kind: BatchKind) -> None:
        batch = self.inbox.start(kind, message.message_id)
        job_path = self._job_path(message)
        job = read_json(job_path)
        if self.courier_mail_importer is not None and job.get('phase') != 'processing':
            save_json(job_path, {'phase': 'downloading', 'message': asdict(message), 'batch_id': batch.batch_id})
            try:
                self.courier_mail_importer(batch.directory / 'files', kind.value)
            except MailImportError as exc:
                raise BotError(f"今日{kind.value}邮箱下载失败：{exc}。已下载文件保留，发送“处理”重试。") from exc
        current = self.inbox.current()
        assert current is not None
        if current.files or job.get('phase') == 'processing':
            self._process_current(message)
        else:
            save_json(job_path, {'phase': 'waiting', 'message': asdict(message), 'batch_id': batch.batch_id})
            self._reply(message, f"今天没有新的{kind.value}PDF附件。已开始手动收件，请发送电子发票及配套运单明细PDF，全部上传后发送“处理”。")

    def _handle_workbench(self, message: IncomingMessage, command: str) -> None:
        store = ExpenseStore(self.workspace_root)
        if command == '工作台':
            self._reply(message, '电脑工作台：http://127.0.0.1:8765 （仅这台电脑可打开）。\n手机可发送：登记 酒店 500 两晚住宿\n或：登记 材料采购 128 螺丝及胶水\n随后发送图片/PDF；在电脑工作台分类并核对。晚到材料发送“关联 事项编号”后追加。发送“结束登记”退出收件。')
            return
        if command == '结束登记':
            store.select(message.sender_open_id, None)
            self._reply(message, '已退出事项收件，所有事项和材料保留。')
            return
        if self.inbox.current() is not None:
            raise BotError('当前有打印批次，请先处理或取消，再登记／关联工作台事项')
        try:
            if command.startswith('登记 '):
                parts = command.split(' ', 3)
                if len(parts) != 4 or parts[1] not in CATEGORIES:
                    raise ValueError('格式：登记 类别 金额 事项名称，例如：登记 材料采购 128 螺丝及胶水')
                item = store.create({'category': parts[1], 'amount': parts[2], 'title': parts[3]}, source='feishu:' + message.message_id)
            else:
                item = store.get(command.removeprefix('关联 ').strip())
            if item['stage'] != 'draft':
                raise ValueError('请先在工作台退回待提交，再补材料')
            store.select(message.sender_open_id, item['id'])
        except ValueError as exc:
            raise BotError(str(exc)) from exc
        self._reply(message, f"事项：{item['title']}\n编号：{item['id']}\n已进入该事项收件，可随时发送图片或PDF（每份20MB以内）。材料不会自动提交报销，请到工作台分类并核对。发送“结束登记”退出。")

    def _handle_resource(self, message: IncomingMessage) -> None:
        if not message.resource_key:
            raise BotError("消息缺少飞书资源标识")
        fallback = message.file_name or (
            f"{message.message_id}.jpg" if message.message_type == "image" else "附件.bin"
        )
        resource = self.gateway.download_resource(
            message.message_id,
            message.resource_key,
            message.message_type,
            fallback,
        )
        store = ExpenseStore(self.workspace_root)
        selected = store.selected(message.sender_open_id)
        if selected and self.inbox.current() is None:
            try:
                item = store.add_attachment(selected, resource.file_name, resource.data)
            except ValueError as exc:
                raise BotError(str(exc)) from exc
            self._reply(message, f"已收进事项「{item['title']}」，共{len(item['attachments'])}份材料。请到工作台标记材料用途；不影响其他事项。")
            return
        path, duplicate = self.inbox.add_file(resource, message.message_id)
        batch = self.inbox.current()
        assert batch is not None
        label = "重复文件，已忽略" if duplicate else "已接收"
        self._reply(message, f"{label}：{path.name}。本批共 {len(batch.files)} 个文件。")

    def _process_current(self, message: IncomingMessage) -> None:
        batch = self.inbox.current()
        if batch is None:
            raise BotError("当前没有批次，请先发送“打车”“外卖”或“顺丰”")
        if not batch.files and read_json(self._job_path(message)).get('phase') != 'processing':
            raise BotError("当前批次没有文件")
        paths = ensure_workspace(self.workspace_root)

        if batch.kind in {BatchKind.EXPRESS, BatchKind.SAME_CITY}:
            person = read_person_name(paths.config)
            if person is None:
                raise BotError("尚未设置输出姓名，请先在电脑运行一次 process")
            summary = self._run_processing(message, lambda: process_courier_batch(paths.root, list(batch.files), person)[1])
            self.inbox.clear_current("已处理")
            self._send_process_result(message, summary)
            return

        if batch.kind is BatchKind.TAKEOUT:
            takeout_person = read_person_name(paths.config)
            if takeout_person is None:
                raise BotError("尚未设置输出姓名，请先在电脑运行一次 process")
            summary = self._run_processing(message, lambda: process_takeout_batch(paths.root, list(batch.files), takeout_person)[1])
            self.inbox.clear_current("已处理")
            self._send_process_result(message, summary)
            return

        unsupported = any(path.suffix.lower() != ".pdf" for path in batch.files)
        if unsupported:
            reason = (
                "机器人已成功接收文件；打车批次只支持可识别的 PDF，"
                "为避免错误配对，本批次转入人工检查。"
            )
            review = move_to_review(paths, list(batch.files), reason, category="机器人批次")
            self.inbox.clear_current("已处理")
            self._reply(message, f"本批次需要检查：{reason}\n位置：{review}")
            return

        resuming = bool(read_json(self._job_path(message)))
        if any(paths.incoming.iterdir()) and not resuming:
            raise BotError("“待处理”目录中已有文件，请先处理或移走后再发送“处理”")
        person_name = read_person_name(paths.config)
        if person_name is None:
            raise BotError("尚未设置输出姓名，请先在电脑运行一次 process")

        def run_ride() -> BatchSummary:
            if not any(paths.incoming.iterdir()):
                for source in batch.files:
                    shutil.copy2(source, paths.incoming / source.name)
            return process_workspace(paths.root, person_name)[1]
        summary = self._run_processing(message, run_ride)
        self.inbox.clear_current("已处理")

        self._send_process_result(message, summary)

    def _send_process_result(
        self,
        message: IncomingMessage,
        summary: BatchSummary,
        *,
        prefix: str = "",
    ) -> None:
        path = self._job_path(message)
        job = read_json(path)
        if job.get('phase') == 'sent':
            return
        if not job:
            job = {'message': asdict(message), 'phase': 'result',
                   'outcomes': [asdict(item) for item in summary.outcomes]}
            save_json(path, job)
        batch = self.inbox.current()
        if batch and job.get('batch_id') == batch.batch_id:
            self.inbox.clear_current('已处理')
        successful_outputs = [
            Path(outcome.message.split("；", 1)[0])
            for outcome in summary.outcomes
            if outcome.status is OutcomeStatus.SUCCESS
        ]
        bundle = None
        if successful_outputs and 'files' not in job:
            paths = ensure_workspace(self.workspace_root)
            try:
                if job.get('report_date'):
                    bundle = create_delivery_bundle(
                        paths, successful_outputs,
                        report_date=date.fromisoformat(job['report_date']),
                    )
                else:
                    bundle = create_delivery_bundle(paths, successful_outputs)
            except (DeliveryError, OSError, ValueError) as exc:
                raise BotError(f"票据已经处理并归档，但总 PDF 或费用清单生成失败：{exc}") from exc
            job.update(phase='bundle', files=[str(bundle.pdf_path), str(bundle.markdown_path)], sent_files=[])
            save_json(path, job)

        lead = f"{prefix}\n" if prefix else ""
        if not job.get('notice_sent'):
            notes = '\n'.join(item.message for item in summary.outcomes if item.label == '中断恢复')
            self._reply(
                message,
                (f"{job['report_date']} 汇总完成：{summary.success_count} 组已完成票据，按处理日期筛选；仅重新汇总，不重复计费，当前收件批次保持不变。"
                 if job.get('report_date') else
                 f"{lead}处理完成：成功 {summary.success_count}，需要检查 {summary.review_count}，重复 {summary.duplicate_count}。")
                + ("\n最终总PDF打印设置：A4、1×1（每张纸1页）。票据已在PDF内拼好，不要再次选择1×2。" if job.get('files') else '')
                + ('\n' + notes if notes else ''),
            )
            job['notice_sent'] = True
            save_json(path, job)
        for filename in job.get('files', []):
            if filename not in job.get('sent_files', []):
                self.gateway.send_file(message.chat_id, Path(filename))
                job.setdefault('sent_files', []).append(filename)
                save_json(path, job)
        job['phase'] = 'sent'
        save_json(path, job)
