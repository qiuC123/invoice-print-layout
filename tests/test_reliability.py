from pathlib import Path
from typing import Any

import pytest

from invoice_print_layout.reliability import DurableQueue, single_instance, read_json
from invoice_print_layout.bot import BotController, BotError, FeishuBotSettings
from invoice_print_layout.storage import ensure_workspace, write_person_name, read_history
from tests.test_bot import FakeGateway, message
from tests.helpers import make_trip_pdf, make_invoice_pdf


def test_queue_recovers_inflight_message_and_deduplicates(tmp_path: Path) -> None:
    path = tmp_path / 'queue.sqlite3'
    queue = DurableQueue(path)
    queue.put('one', {'text': '打车'})
    queue.put('one', {'text': '重复'})
    assert queue.next() == ('one', {'text': '打车'})
    restarted = DurableQueue(path)
    assert restarted.next() == ('one', {'text': '打车'})
    restarted.done('one')
    assert DurableQueue(path).next() is None


def test_queue_preserves_order_during_retry(tmp_path: Path) -> None:
    queue = DurableQueue(tmp_path / 'queue.sqlite3')
    queue.put('one', {})
    queue.put('two', {})
    assert queue.next() == ('one', {})
    assert not queue.fail('one')
    assert queue.next() is None  # Second message must not overtake its batch-start command.
    assert not queue.fail('one')
    assert queue.fail('one')
    assert queue.next() == ('two', {})


def test_single_instance_releases_lock(tmp_path: Path) -> None:
    with single_instance(tmp_path):
        with pytest.raises(RuntimeError, match='已经在运行'):
            with single_instance(tmp_path):
                pass
    with single_instance(tmp_path):
        pass


def test_delivery_retry_sends_only_missing_file_without_reprocessing(tmp_path: Path) -> None:
    paths = ensure_workspace(tmp_path / 'workspace')
    write_person_name(paths.config, 'Test')
    gateway = FakeGateway()
    settings = FeishuBotSettings('cli_abcdefgh1234', 'ou_owner')

    def importer() -> Any:
        from invoice_print_layout.mail163 import MailImportSummary
        make_trip_pdf(paths.incoming / 'trip.pdf')
        make_invoice_pdf(paths.incoming / 'invoice.pdf')
        return MailImportSummary(downloaded=2)

    class FailMarkdown(FakeGateway):
        def send_file(self, chat_id: str, path: Path) -> None:
            if path.suffix == '.md':
                raise BotError('模拟发送失败')
            super().send_file(chat_id, path)

    failing = FailMarkdown()
    controller = BotController(paths.root, paths.root / 'feishu.toml', settings, None, failing, importer)
    incoming = message('om_job', text='打车')
    with pytest.raises(BotError, match='模拟'):
        controller.handle(incoming)
    assert [path.suffix for _, path in failing.sent_files] == ['.pdf']
    assert len(read_history(paths.history)) == 1
    restarted = BotController(paths.root, paths.root / 'feishu.toml', settings, None, gateway, importer)
    restarted.resume_deliveries()
    restarted.handle(incoming)
    assert [path.suffix for _, path in gateway.sent_files] == ['.md']
    assert len(read_history(paths.history)) == 1
    assert len(list(paths.delivery.glob('*.pdf'))) == 1
    assert read_json(restarted._job_path(incoming))['phase'] == 'sent'


def test_batch_start_is_idempotent_after_restart(tmp_path: Path) -> None:
    from invoice_print_layout.bot import BotInbox, BatchKind
    inbox = BotInbox(tmp_path)
    first = inbox.start(BatchKind.TAKEOUT, 'om_start')
    restored = BotInbox(tmp_path)
    assert restored.start(BatchKind.TAKEOUT, 'om_start').batch_id == first.batch_id
    with pytest.raises(BotError):
        restored.start(BatchKind.TAKEOUT, 'om_other')


def test_partial_mail_download_is_reused_on_retry(tmp_path: Path) -> None:
    from invoice_print_layout.mail163 import MailImportSummary, MailImportError
    paths = ensure_workspace(tmp_path / 'workspace')
    write_person_name(paths.config, 'Test')
    calls = 0

    def importer() -> MailImportSummary:
        nonlocal calls
        calls += 1
        if calls == 1:
            make_trip_pdf(paths.incoming / 'trip.pdf')
            raise MailImportError('temporary network error')
        assert (paths.incoming / 'trip.pdf').exists()
        make_invoice_pdf(paths.incoming / 'invoice.pdf')
        return MailImportSummary(downloaded=1)

    gateway = FakeGateway()
    controller = BotController(paths.root, paths.root / 'bot.toml',
        FeishuBotSettings('cli_abcdefgh1234', 'ou_owner'), None, gateway, importer)
    incoming = message('om_partial', text='打车')
    with pytest.raises(BotError):
        controller.handle(incoming)
    controller.handle(incoming)
    assert len(read_history(paths.history)) == 1
    assert len(gateway.sent_files) == 2


def test_crash_after_accounting_recovers_without_running_processor(tmp_path: Path) -> None:
    from invoice_print_layout.reliability import save_json
    from invoice_print_layout.workflow import process_workspace
    from dataclasses import asdict
    import hashlib
    paths = ensure_workspace(tmp_path / 'workspace')
    write_person_name(paths.config, 'Test')
    trip = make_trip_pdf(paths.incoming / 'trip.pdf')
    make_invoice_pdf(paths.incoming / 'invoice.pdf')
    gateway = FakeGateway()
    controller = BotController(paths.root, paths.root / 'bot.toml',
        FeishuBotSettings('cli_abcdefgh1234', 'ou_owner'), None, gateway)
    incoming = message('om_crash', text='处理')
    save_json(controller._job_path(incoming), {
        'phase': 'processing', 'message': asdict(incoming), 'baseline': [],
        'input_hashes': [hashlib.sha256(trip.read_bytes()).hexdigest()],
    })
    process_workspace(paths.root, 'Test')
    # Inputs have been archived; the active batch need not survive for delivery recovery.
    controller.handle(incoming)
    assert len(read_history(paths.history)) == 1
    assert len(gateway.sent_files) == 2
