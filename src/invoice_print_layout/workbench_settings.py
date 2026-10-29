"""Settings for the existing organizer and 163 mailbox, without exposing secrets."""
from __future__ import annotations

import threading
from typing import Any

from invoice_print_layout import mail163
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.glm_invoice_ai import GlmInvoiceAI
from invoice_print_layout.invoice_ai_settings import InvoiceAISettings
from invoice_print_layout.mail_agent import MailAgentService
from invoice_print_layout.organize_settings import OrganizeSettings
from invoice_print_layout.workbench import ExpenseStore


class SettingsError(ValueError):
    """Only locally written, credential-free messages may reach settings responses."""


class WorkbenchSettings:
    def __init__(self, store: ExpenseStore, mail: MailAgentService, ai: GlmInvoiceAI, *,
                 imap_factory: mail163.ImapFactory | None = None) -> None:
        self.store, self.mail, self.ai = store, mail, ai
        self.invoice_ai = InvoiceAISettings(store.root)
        self.organize = OrganizeSettings(store.root)
        self.imap_factory = imap_factory
        self.lock = threading.Lock()

    def organize_status(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        config = self.invoice_ai.public() if config is None else config
        status = self.ai.public_status()
        selected = next(row for row in config['providers'] if row['id'] == config['active'])
        # Keep the public mask when the model contains another provider's saved key.
        model = selected['model'] if '••••' in selected['model'] else status['model']
        return {**self.organize.read(), 'model_available': status['available'],
                'model_label': selected['name'] + ' · ' + model if status['available'] else ''}

    @staticmethod
    def _saved_auth(address: str) -> str:
        try:
            return mail163.read_auth_code(address)
        except Exception:
            return ''

    def mail_status(self) -> dict[str, Any]:
        status = self.mail.status()
        result = {key: status[key] for key in ('address', 'enabled', 'interval_seconds', 'since',
                  'approved_since', 'default_project_id', 'last_checked_at', 'last_error')}
        return {**result, 'folder': status['folder'] or 'INBOX',
                'auth_saved': bool(status['address'] and self._saved_auth(status['address'])),
                'projects': [{'id': row['id'], 'name': row['name']} for row in ExpenseProjects(self.store).list()]}

    def public(self) -> dict[str, Any]:
        config = self.invoice_ai.public()
        return {**config, 'organize': self.organize_status(config), 'mail': self.mail_status()}

    @staticmethod
    def _account(body: dict[str, Any]) -> tuple[mail163.Mail163Settings, str]:
        if (not isinstance(body.get('address'), str) or not isinstance(body.get('folder', 'INBOX'), str)
                or not isinstance(body.get('auth_code', ''), str)):
            raise SettingsError('邮箱账号设置无效')
        try:
            settings = mail163.Mail163Settings(mail163.validate_163_address(body['address']),
                                               mail163._validate_folder(body.get('folder', 'INBOX')))
            code = body.get('auth_code', '').strip()
            if code:
                code = mail163.validate_auth_code(code)
        except mail163.MailImportError:
            raise SettingsError('请检查163邮箱地址、文件夹和客户端授权码格式') from None
        return settings, code

    def save_mail(self, body: dict[str, Any]) -> dict[str, Any]:
        if set(body) - {'address', 'folder', 'auth_code', 'enabled', 'interval_seconds', 'since', 'default_project_id'}:
            raise SettingsError('邮箱账号设置无效')
        settings, code = self._account(body)
        changes = {key: body[key] for key in ('enabled', 'interval_seconds', 'since', 'default_project_id') if key in body}
        with self.lock:
            value = {**self.mail._config(), **changes}
            try:
                self.mail._validate_config(value)
            except (ValueError, TypeError):
                if (isinstance(value.get('since'), str) and isinstance(value.get('approved_since'), str)
                        and len(value['since']) == 10 and value['since'] < value['approved_since']):
                    raise SettingsError('收件日期不能早于已批准的起始日期') from None
                raise SettingsError('请检查自动收件开关、间隔和起始日期') from None
            if value['default_project_id'] and not any(row['id'] == value['default_project_id']
                                                      for row in ExpenseProjects(self.store).list()):
                raise SettingsError('请选择有效默认项目，或留空自动判断')
            try:
                old_address = mail163.read_mail_settings(self.store.workspace / 'mail163.toml').address
            except (mail163.MailImportError, OSError):
                old_address = ''
            if not code and settings.address != old_address and not self._saved_auth(settings.address):
                raise SettingsError('请填写这个邮箱的客户端授权码')
            if code:
                try:
                    mail163.save_auth_code(settings.address, code)
                except Exception:
                    raise SettingsError('客户端授权码保存失败，请检查Windows凭据管理器') from None
            mail163.write_mail_settings(self.store.workspace / 'mail163.toml', settings)
            self.mail.configure(changes)
        return self.mail_status()

    def test_mail(self, body: dict[str, Any]) -> dict[str, Any]:
        if set(body) - {'address', 'folder', 'auth_code'}:
            raise SettingsError('邮箱测试设置无效')
        settings, code = self._account(body)
        code = code or self._saved_auth(settings.address)
        if not code:
            raise SettingsError('请填写这个邮箱的客户端授权码')
        return mail163.test_connection(settings, code, imap_factory=self.imap_factory)
