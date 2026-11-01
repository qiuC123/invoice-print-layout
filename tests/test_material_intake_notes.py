"""Receipt comments and existing mail subjects are display-only intake metadata."""
from __future__ import annotations

import json
import io
from pathlib import Path
from typing import Any

from PIL import Image
import pytest

from invoice_print_layout.material_matching import _mail_subject_note
from tests.helpers import make_invoice_pdf
from tests.test_material_matching import setup, queued, revisions


@pytest.mark.parametrize('subject,expected', [
    (' Fwd: Re：转发: 回复：  合成餐费 ', '合成餐费'),
    ('fw：RE:合成材料采购', '合成材料采购'),
    ('轉發：回覆:合成项目', '合成项目'),
    ('Re: Fwd：', ''), ('', ''), (None, ''),
    ('Review: re: 不是前缀', 'Review: re: 不是前缀'),
    ('合成备注：下午领票', '合成备注：下午领票'),
])
def test_subject_note_removes_only_leading_reply_forward_prefixes(subject: Any, expected: str) -> None:
    assert _mail_subject_note(subject) == expected


def test_app_note_is_displayed_without_entering_expense_fields(setup: Any) -> None:
    matcher, project, _ = setup
    voucher = queued(matcher, project, 'Synthetic photo', 'receipt', '14',
                     intake_note='和合成甲方吃饭', source='upload')
    payment = queued(matcher, project, 'Synthetic payment', 'payment', '14')
    card = next(row for row in matcher.board(project)['vouchers'] if row['id'] == voucher['id'])
    assert card['intake_note'] == '和合成甲方吃饭'
    assert not card['fields'].get('note')
    result = matcher.match('q:' + voucher['id'], [payment['id']], project,
                           revisions=revisions(voucher, payment))
    assert not result['item'].get('note')
    assert not result['item'].get('intake_note')
    assert matcher.intake.get(voucher['id'])['intake_note'] == '和合成甲方吃饭'


def test_existing_mail_agent_material_reads_associated_subject_without_rewriting_queue(setup: Any) -> None:
    matcher, project, other_project = setup
    mail = queued(matcher, project, 'Existing mail', 'invoice', '14', source='mail_agent', batch='message-a')
    unrelated = queued(matcher, project, 'Unrelated mail', 'invoice', '15', source='mail_agent', batch='message-b')
    upload = queued(matcher, project, 'Ordinary upload', 'receipt', '16')
    with matcher.store.connect() as db:
        db.execute('CREATE TABLE mail_agent_messages (id TEXT PRIMARY KEY,data TEXT NOT NULL)')
        for key, subject, project_id, ids in [
            ('message-a', 'Fwd：回复: 合成邮件备注', project, [mail['id']]),
            ('wrong-batch', '不得串到其他邮件', project, [unrelated['id']]),
            ('message-b', '不得串到其他项目', other_project, [unrelated['id']]),
            ('message-upload', '上传材料不借邮件备注', project, [upload['id']]),
        ]:
            db.execute('INSERT INTO mail_agent_messages VALUES (?,?)',
                       (key, json.dumps({'subject': subject, 'project_id': project_id, 'material_ids': ids})))
        db.execute('INSERT INTO mail_agent_messages VALUES (?,?)', ('malformed', '{'))
        before = list(db.execute('SELECT data FROM intake_queue ORDER BY rowid'))
    cards = {card['id']: card for card in matcher.board(project)['vouchers']}
    assert cards[mail['id']]['intake_note'] == '合成邮件备注'
    assert not cards[unrelated['id']]['intake_note']
    assert not cards[upload['id']]['intake_note']
    assert all(not card['fields'].get('note') for card in cards.values())
    with matcher.store.connect() as db:
        assert [tuple(row) for row in db.execute('SELECT data FROM intake_queue ORDER BY rowid')] == [tuple(row) for row in before]


def test_legacy_manual_mail_subject_and_empty_subjects(setup: Any) -> None:
    matcher, project, _ = setup
    mail = queued(matcher, project, 'Manual mail', 'receipt', '14', source='mail',
                  mail_context={'subject': 'Re: 转发：合成旧邮件备注'})
    empty = queued(matcher, project, 'Empty mail', 'receipt', '15', source='mail',
                   mail_context={'subject': 'Re：'})
    upload = queued(matcher, project, 'Upload', 'receipt', '16',
                    mail_context={'subject': '不会借用邮箱上下文'})
    cards = {card['id']: card for card in matcher.board(project)['vouchers']}
    assert cards[mail['id']]['intake_note'] == '合成旧邮件备注'
    assert cards[empty['id']]['intake_note'] == cards[upload['id']]['intake_note'] == ''


def _claim_mail_qr(matcher: Any, project: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                   *, source: str, batch: str = '', subject: str = '') -> tuple[dict[str, Any], dict[str, Any]]:
    image = io.BytesIO()
    Image.new('RGB', (40, 60), 'white').save(image, format='PNG')
    parent = matcher.intake.receive('synthetic-mail-qr.png', image.getvalue(), source=source,
                                    project_id=project, batch=batch)
    if subject:
        parent['mail_context'] = {'subject': subject}
        with matcher.store.connect() as db:
            matcher._write_queue(db, parent)
    pdf = make_invoice_pdf(tmp_path / 'synthetic-claimed-invoice.pdf').read_bytes()
    monkeypatch.setattr('invoice_print_layout.mail_search.qr_value', lambda *_: 'https://synthetic.invalid/qr')
    monkeypatch.setattr('invoice_print_layout.invoice_claim.claim', lambda receiver, _url, project_id: {
        'status': 'received', 'entry': receiver.receive('claimed.pdf', pdf, source='hotel_qr', project_id=project_id)})
    return parent, matcher.intake.claim_batch_qr(parent['id'])


def test_mail_agent_qr_pdf_displays_message_subject(setup: Any, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    matcher, project, _ = setup
    parent, replacement = _claim_mail_qr(matcher, project, tmp_path, monkeypatch,
                                        source='mail_agent', batch='synthetic-qr-message')
    assert replacement['source'] == 'hotel_qr' and replacement['batch'] == 'synthetic-qr-message'
    assert replacement['source_entry_id'] == parent['id']
    with matcher.store.connect() as db:
        db.execute('CREATE TABLE mail_agent_messages (id TEXT PRIMARY KEY,data TEXT NOT NULL)')
        db.execute('INSERT INTO mail_agent_messages VALUES (?,?)', ('synthetic-qr-message', json.dumps({
            'subject': 'Fwd: 回复：合成领票邮件备注', 'project_id': project, 'material_ids': [replacement['id']]})))
    before = matcher.intake.get(replacement['id'])
    card = next(row for row in matcher.board(project)['other'] if row['id'] == replacement['id'])
    assert card['intake_note'] == '合成领票邮件备注' and not card['fields'].get('note')
    assert matcher.intake.get(replacement['id']) == before


def test_legacy_manual_mail_qr_pdf_uses_unique_parent_subject_without_writing(setup: Any, tmp_path: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    matcher, project, other = setup
    parent, replacement = _claim_mail_qr(matcher, project, tmp_path, monkeypatch,
                                        source='mail', subject='Fw：Re: 合成历史领票备注')
    before = matcher.intake.get(replacement['id'])
    note = lambda: next(row for row in matcher.board(project)['other'] if row['id'] == replacement['id'])['intake_note']
    assert note() == '合成历史领票备注'
    assert matcher.intake.get(replacement['id']) == before
    parent = matcher.intake.get(parent['id'])
    parent['project_id'] = ''  # Older carriers may stay unassigned after their PDF is assigned.
    with matcher.store.connect() as db:
        matcher._write_queue(db, parent)
    assert note() == '合成历史领票备注'
    # Historical replacements can retain more than one explicit carrier link.
    foreign = queued(matcher, other, 'Foreign legacy carrier', 'other', None, source='mail',
                     mail_context={'subject': '跨项目主题不能借用'})
    same = queued(matcher, project, 'Another legacy carrier', 'other', None, source='mail',
                  mail_context={'subject': '回复：合成历史领票备注'})
    replacement['source_entry_ids'] = [same['id'], foreign['id']]
    with matcher.store.connect() as db:
        matcher._write_queue(db, replacement)
    assert note() == '合成历史领票备注'  # The singular link and same-theme plural link agree.
    same['mail_context']['subject'] = '另一封不同备注'
    with matcher.store.connect() as db:
        matcher._write_queue(db, same)
    assert note() == ''
    replacement['intake_note'] = 'App 已确认的收件备注优先'
    with matcher.store.connect() as db:
        matcher._write_queue(db, replacement)
    assert note() == 'App 已确认的收件备注优先'
    assert not matcher.intake.get(replacement['id'])['fields'].get('note')
