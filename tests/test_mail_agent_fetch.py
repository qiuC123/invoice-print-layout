from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
from typing import Any

import pytest

from invoice_print_layout import mail_agent_fetch as fetcher
from invoice_print_layout.mail163 import Mail163Settings, MailImportError


def result(payload: bytes = b'original synthetic bytes') -> dict[str, Any]:
    return {'uidvalidity': '123', 'messages': [{
        'uid': 1, 'subject': '合成测试发票', 'sender': 'test@example.invalid', 'text': 'synthetic',
        'message_id': '<1@synthetic>', 'issues': [],
        'files': [{'name': 'invoice.pdf', 'payload': payload, 'source': 'mail_attachment'}],
    }], 'more': False}


def valid_worker(settings: Mail163Settings, parameters: dict[str, Any], output: str) -> None:
    assert settings.address == 'synthetic@163.com'
    assert set(parameters) == {'since', 'after_uid', 'uidvalidity', 'limit'}
    fetcher._write_result(Path(output), result())


def hung_worker(settings: Mail163Settings, parameters: dict[str, Any], output: str) -> None:
    time.sleep(30)


def failed_worker(settings: Mail163Settings, parameters: dict[str, Any], output: str) -> None:
    fetcher._write_manifest(Path(output), {'ok': False})


def partial_worker(settings: Mail163Settings, parameters: dict[str, Any], output: str) -> None:
    (Path(output) / 'part-0000.bin').write_bytes(b'partial result')


def outside_worker(settings: Mail163Settings, parameters: dict[str, Any], output: str) -> None:
    data = result()
    data['messages'][0]['files'] = [{'name': 'invoice.pdf', 'source': 'mail_attachment',
        'payload_file': '../outside.bin', 'payload_size': 1, 'payload_sha256': hashlib.sha256(b'a').hexdigest()}]
    fetcher._write_manifest(Path(output), {'ok': True, 'result': data})


def checksum_worker(settings: Mail163Settings, parameters: dict[str, Any], output: str) -> None:
    fetcher._write_result(Path(output), result())
    (Path(output) / 'part-0000.bin').write_bytes(b'x' * len(b'original synthetic bytes'))


def over_limit_worker(settings: Mail163Settings, parameters: dict[str, Any], output: str) -> None:
    data = result()
    data['messages'] *= 2
    fetcher._write_result(Path(output), data)


def call(worker: fetcher.FetchWorker, **kwargs: Any) -> dict[str, Any]:
    return fetcher.fetch_with_deadline(Mail163Settings('synthetic@163.com'),
                                       since=date(2026, 10, 21), worker=worker, **kwargs)


def test_success_returns_original_payload_and_removes_owned_temp_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original = tempfile.TemporaryDirectory
    created: list[Path] = []
    def temporary(*args: Any, **kwargs: Any) -> Any:
        value = original(*args, dir=tmp_path, **kwargs)
        created.append(Path(value.name))
        return value
    monkeypatch.setattr(fetcher.tempfile, 'TemporaryDirectory', temporary)
    response = call(valid_worker, timeout=8)
    assert response == result()
    assert created and all(not path.exists() for path in created)
    assert list(tmp_path.iterdir()) == []


def test_hard_deadline_terminates_hung_process_and_cleans_results(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original = tempfile.TemporaryDirectory
    created: list[Path] = []
    def temporary(*args: Any, **kwargs: Any) -> Any:
        value = original(*args, dir=tmp_path, **kwargs)
        created.append(Path(value.name))
        return value
    monkeypatch.setattr(fetcher.tempfile, 'TemporaryDirectory', temporary)
    before = time.monotonic()
    with pytest.raises(MailImportError, match='超时'):
        call(hung_worker, timeout=.5)
    assert time.monotonic() - before < 6
    assert created and all(not path.exists() for path in created)


def test_stop_event_interrupts_hung_fetch_and_confirms_nothing() -> None:
    stop = threading.Event()
    timer = threading.Timer(.5, stop.set)
    timer.start()
    try:
        before = time.monotonic()
        with pytest.raises(MailImportError, match='停止'):
            call(hung_worker, stop=stop, timeout=20)
        assert time.monotonic() - before < 6
    finally:
        timer.cancel()


def test_already_stopped_never_starts_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(fetcher.multiprocessing, 'get_context', lambda _: pytest.fail('worker started after stop'))
    with pytest.raises(MailImportError, match='停止'):
        call(valid_worker, stop=stop)


@pytest.mark.parametrize('worker', [failed_worker, partial_worker, outside_worker, checksum_worker])
def test_failed_partial_or_tampered_results_are_never_returned(worker: fetcher.FetchWorker) -> None:
    with pytest.raises(MailImportError) as raised:
        call(worker, timeout=8)
    assert 'outside.bin' not in str(raised.value)


def test_manifest_message_count_cannot_exceed_requested_limit() -> None:
    with pytest.raises(MailImportError):
        call(over_limit_worker, limit=1, timeout=8)


def test_child_reads_vault_itself_and_never_writes_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from invoice_print_layout import mail163, mail_agent_source
    seen: list[str] = []
    code = 'SYNTHETIC_PRIVATE_CODE'
    monkeypatch.setattr(mail163, 'read_auth_code', lambda address: code)
    def fake(settings: Mail163Settings, credential: str, **kwargs: Any) -> dict[str, Any]:
        seen.append(credential)
        return result()
    monkeypatch.setattr(mail_agent_source, 'fetch_messages', fake)
    fetcher._fetch_worker(Mail163Settings('synthetic@163.com'),
                          {'since': '2026-10-21', 'after_uid': 0, 'uidvalidity': '', 'limit': 20}, str(tmp_path))
    assert seen == [code]
    assert fetcher._read_result(tmp_path, 20) == result()
    assert all(code.encode() not in path.read_bytes() for path in tmp_path.iterdir())


def test_child_failure_manifest_contains_no_transport_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from invoice_print_layout import mail163
    def fail(address: str) -> str:
        raise RuntimeError('SYNTHETIC_PRIVATE_CODE signed URL')
    monkeypatch.setattr(mail163, 'read_auth_code', fail)
    fetcher._fetch_worker(Mail163Settings('synthetic@163.com'),
                          {'since': '2026-10-21', 'after_uid': 0, 'uidvalidity': '', 'limit': 20}, str(tmp_path))
    assert json.loads((tmp_path / 'manifest.json').read_text(encoding='utf-8')) == {'ok': False}


def test_parent_enforces_aggregate_result_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher._write_result(tmp_path, result())
    monkeypatch.setattr(fetcher, 'MAX_RESULT_BYTES', 1)
    with pytest.raises(ValueError):
        fetcher._read_result(tmp_path, 20)


@pytest.mark.parametrize('changes', [{'timeout': 0}, {'timeout': float('inf')}, {'timeout': 121},
                                    {'limit': 0}, {'after_uid': -1}, {'uidvalidity': 'not-valid'}])
def test_invalid_bounds_rejected_before_process_start(changes: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetcher.multiprocessing, 'get_context', lambda _: pytest.fail('invalid config started process'))
    with pytest.raises(MailImportError):
        call(valid_worker, **changes)
