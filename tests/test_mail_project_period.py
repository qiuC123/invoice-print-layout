"""Synthetic originals and fake IMAP exercise project-time mail routing only."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
import json
from pathlib import Path
import threading
from typing import Any

import pytest

from invoice_print_layout import mail_agent, mail_agent_fetch, mail_agent_source
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.mail163 import Mail163Settings
from invoice_print_layout.mail_project_dates import attachment_dates
from invoice_print_layout.workbench import ExpenseStore
from tests.test_mail_agent import pdf, StubAgent
from tests.test_mail_agent_fetch import result
from tests.test_mail_agent_source import SourceImap


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    store = ExpenseStore(tmp_path)
    projects = ExpenseProjects(store)
    settings = [Mail163Settings('sample@163.com')]
    monkeypatch.setattr(mail_agent, 'read_mail_settings', lambda path: settings[0])
    monkeypatch.setattr(mail_agent, 'read_auth_code', lambda address: pytest.fail('no real credentials'))
    service = mail_agent.MailAgentService(store, StubAgent(store))
    kicks: list[bool] = []
    monkeypatch.setattr(service, 'kick', lambda: kicks.append(True))
    return service, projects, settings, kicks


def persist(service: Any, settings: Mail163Settings, uid: int, *, subject: str = '发票',
            sent_date: str = '2026-10-12', texts: list[str] | None = None, image: bool = False,
            config: dict[str, Any] | None = None) -> dict[str, Any]:
    account = {'id': service._account_key(settings), 'uidvalidity': '1'}
    files = ([{'name': 'image.png', 'payload': b'synthetic-image', 'source': 'attachment'}] if image else
             [{'name': f'{index}.pdf', 'payload': pdf(text), 'source': 'attachment'}
              for index, text in enumerate(texts or ['无日期合成材料'])])
    service._persist_message(account, {'uid': uid, 'subject': subject, 'text': '', 'sent_date': sent_date,
                                      'files': files, 'issues': []}, config or {'default_project_id': ''})
    return service._messages(account['id'])[-1]


def test_route_name_then_date_then_default_and_legacy_string_return(env: Any) -> None:
    service, projects, settings, _ = env
    named = projects.create('名称项目')
    dated = projects.create('日期项目', start_date='2026-10-01', end_date='2026-10-31')
    default = projects.create('兜底项目')
    config = {'default_project_id': default['id']}
    assert service._route('名称项目发票', config, ['2026-10-12']) == named['id']
    assert service._route('普通发票', config, ['2026-10-12']) == dated['id']
    assert service._route('普通发票', config, ['2026-11-12']) == default['id']
    assert service._route('普通发票', {'default_project_id': ''}) == ''
    saved = persist(service, settings[0], 1, subject='名称项目', config=config)
    assert saved['route_reason'] == 'name'


def test_overlapping_dates_do_not_guess_but_unique_name_still_wins(env: Any) -> None:
    service, projects, _, _ = env
    first = projects.create('项目甲', start_date='2026-10-01', end_date='2026-10-31')
    projects.create('项目乙', start_date='2026-10-10', end_date='2026-10-20')
    config = {'default_project_id': ''}
    assert service._route('', config, ['2026-10-12']) == ''
    assert service._route('项目甲', config, ['2026-10-12']) == first['id']
    assert service._route('项目甲和项目乙', config, ['2026-10-12']) == ''


def test_reimbursed_project_excluded_only_from_date_route_and_open_end_is_unbounded(env: Any) -> None:
    service, projects, _, _ = env
    reimbursed = projects.create('已报销项目', start_date='2026-10-01')
    with service.store.connect() as db:
        db.execute('INSERT INTO expense_project_reimbursement VALUES (?,?,?,?)',
                   (reimbursed['id'], 'reimbursed', 1, 'synthetic'))
    ongoing = projects.create('进行中项目', start_date='2026-10-01')
    config = {'default_project_id': ''}
    assert service._route('', config, ['2099-12-31']) == ongoing['id']
    assert service._route('已报销项目', config, ['2099-12-31']) == reimbursed['id']
    assert service._route('', config, ['2026-09-30']) == ''


def test_all_attachment_dates_must_fit_one_project_and_replace_sent_date(env: Any) -> None:
    service, projects, settings, _ = env
    projects.create('十月项目', start_date='2026-10-01', end_date='2026-10-31')
    november = projects.create('十一月项目', start_date='2026-11-01', end_date='2026-11-30')
    saved = persist(service, settings[0], 1, texts=['开票日期：2026年11月02日'])
    assert saved['project_id'] == november['id'] and saved['route_reason'] == 'date'
    assert saved['route_dates'] == ['2026-11-02'] and saved['sent_date'] == '2026-10-12'
    cross = persist(service, settings[0], 2, texts=['开票日期：2026年10月12日', '开票日期：2026年11月02日'])
    assert cross['status'] == 'unassigned' and cross['project_id'] == ''
    assert cross['route_dates'] == ['2026-10-12', '2026-11-02']
    visible = service.status()['messages'][0]
    assert visible['route_dates'] == cross['route_dates'] and visible['route_reason'] == ''


@pytest.mark.parametrize('image', [True, False])
def test_unreadable_pdf_or_image_falls_back_to_sent_date(env: Any, image: bool) -> None:
    service, projects, settings, _ = env
    project = projects.create('秋季项目', start_date='2026-10-12', end_date='2026-10-12')
    saved = persist(service, settings[0], 1, image=image)
    assert saved['project_id'] == project['id'] and saved['route_dates'] == ['2026-10-12']


@pytest.mark.parametrize('text,expected', [
    ('电子发票（铁路电子客票）\n开票日期：2026年11月02日\n2026年10月12日08:30开', ['2026-10-12']),
    ('电子发票（铁路电子客票）\n开票日期：2026年11月02日', []),
    ('滴滴出行行程单\n申请日期：2026-11-02\n行程起止日期：2026-10-10至2026-10-12',
     ['2026-10-10', '2026-10-12']),
    ('滴滴出行行程单\n申请日期：2026-11-02', []),
    ('电子发票\n开票日期：2026年10月12日', ['2026-10-12']),
])
def test_native_pdf_date_parser_never_requires_amount_or_uses_trip_application_date(
        tmp_path: Path, text: str, expected: list[str]) -> None:
    path = tmp_path / 'synthetic.pdf'
    payload = pdf(text)
    path.write_bytes(payload)
    assert attachment_dates(path) == expected
    assert path.read_bytes() == payload


def test_reroute_only_eligible_current_mail_and_kicks_once_without_rematching_name(env: Any) -> None:
    service, projects, settings, kicks = env
    project = projects.create('日期项目')
    named = projects.create('名称项目')
    records = [persist(service, settings[0], uid, subject='名称项目' if uid == 1 else '普通发票')
               for uid in range(1, 10)]
    # Start from durable unassigned rows; this tests rerouting without rerunning name matching.
    for record in records:
        service._message_update(record['id'], status='unassigned', project_id='', route_reason='')
    changes = [{}, {'route_dates': [], 'sent_date': '2026-10-12'}, {'job_id': 'already-started'},
               {'material_ids': ['existing-material']}, {'project_id': named['id']}, {'status': 'processing'},
               {'status': 'done'}, {'route_dates': [], 'sent_date': ''}, {'route_dates': ['2026-11-12']}]
    for record, change in zip(records, changes):
        service._message_update(record['id'], **change)
    foreign = persist(service, Mail163Settings('other@163.com'), 1)
    before = {m['id']: m for m in service._messages()}
    projects.set_period(project['id'], '2026-10-01', '2026-10-31', project['project_revision'])
    assert service.reroute_unassigned() == 2
    assert kicks == [True]
    for record in records[:2]:
        saved = service._message(record['id'])
        assert (saved['project_id'], saved['status'], saved['route_reason']) == (project['id'], 'waiting_agent', 'date')
        assert saved['dispatch_attempts'] == 0 and saved['retry_at'] == 0.0
    for record in [*records[2:], foreign]:
        assert service._message(record['id']) == before[record['id']]
    assert service.reroute_unassigned() == 0 and kicks == [True]


def test_reroute_uses_account_scoped_default_but_skips_legacy_rows_without_dates(env: Any) -> None:
    service, projects, settings, _ = env
    target = projects.create('兜底项目')
    saved = persist(service, settings[0], 1)
    legacy = persist(service, settings[0], 2, sent_date='')
    service.configure({'default_project_id': target['id']})
    assert service.reroute_unassigned() == 1
    assert service._message(saved['id'])['route_reason'] == 'default'
    assert service._message(legacy['id'])['status'] == 'unassigned'


def test_reroute_with_missing_mail_configuration_returns_zero(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, _, kicks = env
    monkeypatch.setattr(service, '_settings', lambda: (_ for _ in ()).throw(ValueError('not configured')))
    assert service.reroute_unassigned() == 0 and not kicks


@pytest.mark.parametrize('header', ['Thu, 29 Oct 2026 23:30:00 -1200', 'bad date', ''])
def test_real_fetch_parses_date_header_locally_using_fake_readonly_imap(header: str) -> None:
    message = EmailMessage()
    message['Subject'] = '合成发票'
    # Prefix raw header so malformed values are exercised instead of EmailMessage normalizing them.
    message.set_content('synthetic')
    raw = (f'Date: {header}\r\n'.encode() if header else b'') + message.as_bytes()
    client = SourceImap({b'1': raw})
    fetched = mail_agent_source.fetch_messages(Mail163Settings('sample@163.com'), 'fake-code', since=date(2026, 10, 1),
                                                imap_factory=lambda *args, **kwargs: client)
    expected = datetime(2026, 10, 29, 23, 30, tzinfo=timezone(-timedelta(hours=12))).astimezone().date().isoformat()
    assert fetched['messages'][0]['sent_date'] == (expected if header.startswith('Thu') else '')
    assert client.readonly and client.logged_out
    assert 'fake-code' not in json.dumps(fetched)


@pytest.mark.parametrize('value', [None, 3, [], '2026-02-30', '20261012', '2026-1-2'])
def test_manifest_rejects_invalid_sent_date(tmp_path: Path, value: Any) -> None:
    data = result()
    data['messages'][0]['sent_date'] = value
    mail_agent_fetch._write_result(tmp_path, data)
    with pytest.raises((ValueError, TypeError)):
        mail_agent_fetch._read_result(tmp_path, 20)


def test_manifest_legacy_date_missing_remains_compatible(tmp_path: Path) -> None:
    mail_agent_fetch._write_result(tmp_path, result())
    assert mail_agent_fetch._read_result(tmp_path, 20) == result()


def test_reroute_lock_does_not_overwrite_concurrent_manual_assignment(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    service, projects, settings, _ = env
    dated = projects.create('时间项目', start_date='2026-10-01')
    manual = projects.create('人工项目')
    saved = persist(service, settings[0], 1, sent_date='')
    service._message_update(saved['id'], route_dates=['2026-10-12'])
    entered, release = threading.Event(), threading.Event()
    original = service._route_details
    def blocked(*args: Any) -> Any:
        entered.set()
        assert release.wait(3)
        return original(*args)
    monkeypatch.setattr(service, '_route_details', blocked)
    rerouted: list[int] = []
    errors: list[str] = []
    rerouter = threading.Thread(target=lambda: rerouted.append(service.reroute_unassigned()))
    def assign() -> None:
        try:
            service.assign(saved['id'], manual['id'])
        except ValueError as error:
            errors.append(str(error))
    writer = threading.Thread(target=assign)
    rerouter.start()
    assert entered.wait(3)
    writer.start()
    release.set()
    rerouter.join(3)
    writer.join(3)
    assert not rerouter.is_alive() and not writer.is_alive()
    assert rerouted == [1] and len(errors) == 1
    assert service._message(saved['id'])['project_id'] == dated['id']
