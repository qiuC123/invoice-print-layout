from __future__ import annotations

import json
import logging
import threading
import time
import hashlib
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetMessageResourceRequest,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from invoice_print_layout.bot import (
    BotController,
    BotError,
    DownloadedResource,
    FeishuBotSettings,
    IncomingMessage,
)
from invoice_print_layout.mail163 import (
    MailImportSummary,
    import_pdf_attachments,
    import_takeout_invoice_links,
    read_auth_code,
    read_mail_settings,
)
from invoice_print_layout.storage import ensure_workspace
from invoice_print_layout.reliability import DurableQueue, single_instance


class FeishuGateway:
    def __init__(self, client: Any) -> None:
        self.client = client

    @staticmethod
    def _require_success(response: Any, operation: str) -> None:
        if not response.success():
            raise BotError(
                f"飞书{operation}失败：code={response.code}，msg={response.msg}，"
                f"log_id={response.get_log_id()}"
            )

    def reply_text(self, message_id: str, text: str) -> None:
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self.client.im.v1.message.reply(request)
        self._require_success(response, "回复消息")

    def download_resource(
        self,
        message_id: str,
        resource_key: str,
        resource_type: str,
        fallback_name: str,
    ) -> DownloadedResource:
        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(resource_key)
            .type(resource_type)
            .build()
        )
        response = self.client.im.v1.message_resource.get(request)
        self._require_success(response, "下载附件")
        if response.file is None:
            raise BotError("飞书下载附件成功，但响应中没有文件内容")
        data = response.file.read()
        name = response.file_name or fallback_name
        if resource_type == "image" and Path(name).suffix.lower() not in {
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
        }:
            name = fallback_name
        return DownloadedResource(Path(name).name, data)

    def send_file(self, chat_id: str, path: Path) -> None:
        file_type = "pdf" if path.suffix.lower() == ".pdf" else "stream"
        with path.open("rb") as stream:
            upload_request = (
                CreateFileRequest.builder()
                .request_body(
                    CreateFileRequestBody.builder()
                    .file_type(file_type)
                    .file_name(path.name)
                    .file(stream)
                    .build()
                )
                .build()
            )
            upload_response = self.client.im.v1.file.create(upload_request)
        self._require_success(upload_response, "上传结果文件")
        if upload_response.data is None or not upload_response.data.file_key:
            raise BotError("飞书上传结果文件成功，但没有返回 file_key")

        send_request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .uuid(hashlib.sha256((chat_id + str(path.resolve()) + hashlib.sha256(path.read_bytes()).hexdigest()).encode()).hexdigest()[:32])
                .msg_type("file")
                .content(
                    json.dumps(
                        {"file_key": upload_response.data.file_key},
                        ensure_ascii=False,
                    )
                )
                .build()
            )
            .build()
        )
        send_response = self.client.im.v1.message.create(send_request)
        self._require_success(send_response, "发送结果文件")


def decode_event(data: P2ImMessageReceiveV1) -> IncomingMessage | None:
    event = data.event
    if event is None or event.message is None or event.sender is None:
        return None
    message = event.message
    sender_id = event.sender.sender_id
    if sender_id is None or not sender_id.open_id:
        return None
    if not message.message_id or not message.chat_id or not message.message_type:
        return None

    try:
        content = json.loads(message.content or "{}")
    except json.JSONDecodeError:
        content = {}
    if not isinstance(content, dict):
        content = {}

    text: str | None = None
    resource_key: str | None = None
    file_name: str | None = None
    if message.message_type == "text":
        raw_text = content.get("text")
        text = raw_text if isinstance(raw_text, str) else ""
    elif message.message_type == "image":
        raw_key = content.get("image_key")
        resource_key = raw_key if isinstance(raw_key, str) else None
        file_name = f"{message.message_id}.jpg"
    elif message.message_type == "file":
        raw_key = content.get("file_key")
        raw_name = content.get("file_name")
        resource_key = raw_key if isinstance(raw_key, str) else None
        file_name = raw_name if isinstance(raw_name, str) else "附件.bin"

    return IncomingMessage(
        message_id=message.message_id,
        chat_id=message.chat_id,
        chat_type=message.chat_type or "unknown",
        sender_open_id=sender_id.open_id,
        sender_type=event.sender.sender_type or "unknown",
        message_type=message.message_type,
        text=text,
        resource_key=resource_key,
        file_name=file_name,
    )


def is_lodging_message(message: IncomingMessage, owner: str | None) -> bool:
    return (message.message_type == 'text' and (message.text or '').strip().startswith('住宿')
            and message.sender_type == 'user' and message.chat_type == 'p2p'
            and owner is not None and message.sender_open_id == owner)


def run_feishu_bot(
    workspace_root: Path,
    config_path: Path,
    settings: FeishuBotSettings,
    app_secret: str,
    pairing_code: str | None,
    debug: bool = False,
) -> None:
    with single_instance(workspace_root):
        _run_feishu_bot(workspace_root, config_path, settings, app_secret, pairing_code, debug)


def _run_feishu_bot(
    workspace_root: Path,
    config_path: Path,
    settings: FeishuBotSettings,
    app_secret: str,
    pairing_code: str | None,
    debug: bool = False,
) -> None:
    log_level = lark.LogLevel.DEBUG if debug else lark.LogLevel.WARNING
    client = (
        lark.Client.builder()
        .app_id(settings.app_id)
        .app_secret(app_secret)
        .log_level(log_level)
        .build()
    )
    gateway = FeishuGateway(client)
    paths = ensure_workspace(workspace_root)

    def import_today_mail() -> MailImportSummary:
        mail_settings = read_mail_settings(paths.mail_config)
        auth_code = read_auth_code(mail_settings.address)
        return import_pdf_attachments(
            paths,
            mail_settings,
            auth_code,
            days=1,
        )

    def import_today_takeout_mail(destination: Path) -> MailImportSummary:
        mail_settings = read_mail_settings(paths.mail_config)
        auth_code = read_auth_code(mail_settings.address)
        return import_takeout_invoice_links(
            destination,
            paths,
            mail_settings,
            auth_code,
            days=1,
        )

    def import_today_courier_mail(destination: Path, kind: str) -> MailImportSummary:
        mail_settings = read_mail_settings(paths.mail_config)
        return import_pdf_attachments(
            paths, mail_settings, read_auth_code(mail_settings.address), days=1,
            courier_kind=kind, destination=destination,
        )

    controller = BotController(
        workspace_root,
        config_path,
        settings,
        pairing_code,
        gateway,
        import_today_mail,
        import_today_takeout_mail,
        import_today_courier_mail,
    )
    work_queue = DurableQueue(paths.root / '机器人任务' / 'messages.sqlite3')

    # Opt-in owner-only lodging uses a separate durable queue and worker.
    lodging_config = paths.root / 'lodging.json'
    lodging_queue: DurableQueue | None = None
    lodging_service: Any = None
    if lodging_config.exists():
        try:
            from invoice_print_layout.lodging import LodgingService
            from invoice_print_layout.lodging_remote import FeishuLodgingBackend, jev_intent
            config = json.loads(lodging_config.read_text(encoding='utf-8'))
            if config.get('enabled') is True and settings.owner_open_id:
                if not all(isinstance(config.get(k), str) and config[k] for k in ('base_token', 'table_id')):
                    raise ValueError('Invalid lodging configuration')
                lodging_client = lark.Client.builder().app_id(settings.app_id).app_secret(app_secret).timeout(15).log_level(lark.LogLevel.ERROR).build()
                lodging_service = LodgingService(paths.root / '住宿任务', settings.owner_open_id,
                    FeishuLodgingBackend(lodging_client, config['base_token'], config['table_id']),
                    jev_intent if config.get('jev_enabled') is True else None)
                lodging_queue = DurableQueue(paths.root / '住宿任务' / 'messages.sqlite3')
        except Exception:
            lodging_queue = None
            logging.error('住宿模块配置或存储不可用，住宿未启用；原发票收件继续运行')

    def handle_event(data: P2ImMessageReceiveV1) -> None:
        incoming = decode_event(data)
        if incoming is not None:
            if lodging_queue is not None and is_lodging_message(incoming, settings.owner_open_id):
                lodging_queue.put(incoming.message_id, asdict(incoming))
                return
            work_queue.put(incoming.message_id, asdict(incoming))

    def lodging_worker() -> None:
        assert lodging_queue is not None
        while True:
            queued = lodging_queue.next()
            if queued is None:
                time.sleep(1)
                continue
            message_id, payload = queued
            try:
                reply = lodging_service.handle(payload['sender_open_id'], payload['chat_id'], message_id, payload['text'])
                gateway.reply_text(message_id, reply)
                lodging_queue.done(message_id)
            except Exception:
                if lodging_queue.fail(message_id):
                    logging.error('住宿消息处理未完成，消息ID=%s；原任务已保留', message_id)
                    try:
                        gateway.reply_text(message_id, '住宿任务暂未完成，原任务已保留，请维护者核对。不要重复新建同一笔住宿。')
                    except Exception:
                        logging.error('住宿失败通知暂未送达，消息ID=%s', message_id)

    def supervised_lodging_worker() -> None:
        try:
            lodging_worker()
        except BaseException:
            logging.error('住宿工作线程退出，交由服务恢复')
            os._exit(1)

    if lodging_queue is not None:
        threading.Thread(target=supervised_lodging_worker, name='lodging-worker', daemon=True).start()

    def worker() -> None:
        last_delivery = 0.0
        while True:
            if time.monotonic() - last_delivery >= 30:
                try:
                    controller.resume_deliveries()
                except Exception:
                    logging.exception('补发暂未成功，将在30秒后重试')
                last_delivery = time.monotonic()
            queued = work_queue.next()
            if queued is None:
                time.sleep(1)
                continue
            message_id, payload = queued
            incoming = IncomingMessage(**payload)
            try:
                controller.handle(incoming)
                work_queue.done(message_id)
            except Exception as exc:
                logging.exception("飞书机器人处理消息失败")
                if work_queue.fail(message_id):
                    try:
                        detail = str(exc) if isinstance(exc, BotError) else '请查看电脑日志'
                        gateway.reply_text(message_id, f'处理暂未完成：{detail}。原附件已保留；发送“处理”继续，结果文件发送失败可用“补发”。')
                    except Exception:
                        logging.exception("飞书机器人发送错误提示失败")

    def supervised_worker() -> None:
        try:
            worker()
        except BaseException:
            logging.exception('工作线程退出，交由后台服务重启')
            os._exit(1)

    threading.Thread(target=supervised_worker, name="invoice-bot-worker", daemon=True).start()
    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handle_event)
        .build()
    )
    ws_client = lark.ws.Client(
        settings.app_id,
        app_secret,
        event_handler=event_handler,
        log_level=log_level,
    )
    ws_client.start()
