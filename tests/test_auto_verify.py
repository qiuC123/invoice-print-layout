"""Only synthetic originals are used for automatic verification boundaries."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from invoice_print_layout.agent_content import ContentTools
from invoice_print_layout.agent_jobs import AgentLoop
from invoice_print_layout.auto_verify import assess, automatic_verification_valid, scan, source_signature
from invoice_print_layout.organize_settings import OrganizeSettings
from invoice_print_layout.review_return import return_to_review
from tests.test_invoice_agent import environment, material, start


def prepared(environment: Any, *, enabled: bool = False) -> tuple[Any, str]:
    jobs, tools, project = environment
    OrganizeSettings(tools.store.root).update({'auto_verify': enabled})
    entries = [material(tools, project, 'receipt', '17', 'synthetic-receipt'),
               material(tools, project, 'payment', '17', 'synthetic-payment')]
    with tools.store.connect() as db:
        for entry in entries:
            entry['field_sources'] = {'amount': 'native_pdf'}
            tools.matching._write_queue(db, entry)
    done = AgentLoop(jobs).run(start(environment, entries)['id'], tools)
    assert done['state'] == 'ready' and len(done['item_ids']) == 1
    return tools.store, done['item_ids'][0]


def mutate(store: Any, ident: str, **updates: Any) -> None:
    with store.connect() as db:
        data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (ident,)).fetchone()['data'])
        data.update(updates)
        data['agent_content']['base'] = ContentTools._base(data)
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data), ident))


def decision(store: Any, ident: str) -> dict[str, Any]:
    with store.connect() as db:
        return assess(store, db, db.execute('SELECT id,source,data FROM expenses WHERE id=?', (ident,)).fetchone())


def test_finish_automatically_verifies_with_rule_sources_and_audit_once(environment: Any) -> None:
    store, ident = prepared(environment, enabled=True)
    item = store.get(ident)
    assert item['verified'] and item['ready'] and not item.get('review_requested')
    assert item['verification']['mode'] == 'automatic'
    assert item['verification']['rule_version'] == 'auto-verify-v1'
    assert all(item['verification']['checks'].values())
    assert len(item['verification']['sources']) == 2
    assert all(source['source'] == 'native_pdf' and len(source['sha256']) == 64
               for source in item['verification']['sources'])
    assert sum('自动核对：' in event['message'] for event in item['events']) == 1
    assert scan(store) == []


def test_automatic_candidate_preserves_registration_authorization_snapshot(environment: Any) -> None:
    store, ident = prepared(environment)
    with store.connect() as db:
        group = json.loads(db.execute('SELECT data FROM material_matches').fetchone()['data'])
        effects = [json.loads(row['data']) for row in db.execute('SELECT data FROM invoice_agent_effects')]
    created = next(effect for effect in effects if effect.get('item_id') == ident)
    snapshot = group['aggregate_after']['data']
    assert snapshot['auto_verification_candidate'] == store.get(ident)['auto_verification_candidate']
    assert created['content_base'] == ContentTools._base(snapshot)


@pytest.mark.parametrize('updates,check', [
    ({'stage': 'submitted'}, 'draft'), ({'deleted': True}, 'draft'),
    ({'merged_into': 'other'}, 'draft'), ({'returned_to_synthesis': True}, 'draft'),
    ({'amount_pending': True}, 'amount'), ({'amount_cents': 1800}, 'amount'),
    ({'category': '其他'}, 'category'), ({'agent_review_reason': '合成疑点'}, 'no_questions'),
    ({'field_conflicts': [{'field': 'amount'}]}, 'no_questions'),
    ({'ai_analysis': {'status': 'review'}}, 'no_questions'),
    ({'recognition_status': 'review'}, 'no_questions'),
    ({'ambiguous': True}, 'no_questions'),
])
def test_each_expense_boundary_keeps_pending(environment: Any, updates: Any, check: str) -> None:
    store, ident = prepared(environment)
    mutate(store, ident, **updates)
    assert not decision(store, ident)['checks'][check]
    OrganizeSettings(store.root).update({'auto_verify': True})
    assert scan(store) == []
    with store.connect() as db:
        assert not json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (ident,)).fetchone()['data'])['verified']


def test_materials_are_required_without_using_missing_acknowledgement(environment: Any) -> None:
    store, ident = prepared(environment)
    with store.connect() as db:
        db.execute("DELETE FROM attachments WHERE item_id=? AND role='payment'", (ident,))
    assert not decision(store, ident)['checks']['materials']
    OrganizeSettings(store.root).update({'auto_verify': True})
    assert scan(store) == []


@pytest.mark.parametrize('change,check', [
    ('missing_amount_source', 'amount'), ('changed_fields', 'association'),
    ('field_conflict', 'no_questions'), ('recognition_review', 'no_questions'),
    ('source_review', 'no_questions'), ('source_issue', 'no_questions'),
])
def test_each_source_boundary_keeps_pending(environment: Any, change: str, check: str) -> None:
    store, ident = prepared(environment)
    with store.connect() as db:
        row = db.execute('SELECT id,data FROM intake_queue ORDER BY id LIMIT 1').fetchone()
        entry = json.loads(row['data'])
        if change == 'missing_amount_source':
            entry['field_sources'].pop('amount')
        elif change == 'changed_fields':
            entry['fields']['title'] = 'source changed'
        elif change == 'field_conflict':
            entry['field_conflicts'] = [{'field': 'amount'}]
        elif change == 'recognition_review':
            entry['ai_analysis'] = {'status': 'review'}
        elif change == 'source_review':
            entry['status'] = 'review'
        else:
            entry['issues'] = ['金额冲突']
        db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(entry), row['id']))
        data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (ident,)).fetchone()['data'])
        if change != 'changed_fields':
            entries = [json.loads(record['data']) for record in db.execute('SELECT data FROM intake_queue')]
            data['auto_verification_candidate']['source_signature'] = source_signature(entries)
        data['agent_content']['base'] = ContentTools._base(data)
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data), ident))
    assert not decision(store, ident)['checks'][check]


@pytest.mark.parametrize('change', ['automatic', 'proof', 'source_ids'])
def test_unsupported_or_nonunique_association_keeps_pending(environment: Any, change: str) -> None:
    store, ident = prepared(environment)
    data = store.get(ident)['auto_verification_candidate']
    data[change] = False if change == 'automatic' else [] if change == 'source_ids' else ''
    mutate(store, ident, auto_verification_candidate=data)
    assert not decision(store, ident)['checks']['association']


def test_explicit_review_does_not_clear_registration_default(environment: Any) -> None:
    store, ident = prepared(environment)
    assert decision(store, ident)['eligible']
    with store.connect() as db:
        db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (ident, '2030-01-01', '人工要求重新核对'))
    assert not decision(store, ident)['checks']['no_questions']


def test_service_scan_waits_until_organizer_finishes(environment: Any) -> None:
    store, ident = prepared(environment)
    with store.connect() as db:
        db.execute("UPDATE invoice_agent_jobs SET state='running'")
    assert not decision(store, ident)['checks']['organizer_finished']
    OrganizeSettings(store.root).update({'auto_verify': True})
    assert scan(store) == []


def test_revoke_stays_pending_and_manual_verification_still_works(environment: Any) -> None:
    store, ident = prepared(environment, enabled=True)
    item = store.get(ident)
    returned = return_to_review(store, [ident], {ident: item['version']}, item['project_id'])['items'][0]
    assert not returned['verified'] and returned['review_requested']
    assert scan(store) == []
    manual = store.transition(ident, 'verify', expected_version=returned['version'])
    assert manual['verified'] and manual['ready']
    assert manual.get('verification', {}).get('mode') != 'automatic'


def test_manual_verification_is_untouched_by_scan(environment: Any) -> None:
    store, ident = prepared(environment)
    manual = store.transition(ident, 'verify')
    OrganizeSettings(store.root).update({'auto_verify': True})
    assert scan(store) == []
    assert store.get(ident)['events'] == manual['events']


def test_disabled_switch_and_legacy_settings_default(environment: Any, tmp_path: Path) -> None:
    store, ident = prepared(environment)
    assert not scan(store) and not store.get(ident)['verified']
    assert OrganizeSettings(tmp_path).read()['auto_verify']
    config = OrganizeSettings(tmp_path)
    config.path.parent.mkdir(parents=True, exist_ok=True)
    config.path.write_text(json.dumps({'version': 1, 'auto_after_upload': True, 'use_model': False}), encoding='utf-8')
    assert config.read() == {'auto_after_upload': True, 'use_model': False, 'auto_verify': True}
    assert config.update({'auto_verify': False})['auto_verify'] is False
    with pytest.raises(ValueError):
        config.update({'auto_verify': 'true'})


def test_changed_original_or_expense_invalidates_automatic_review(environment: Any) -> None:
    store, ident = prepared(environment, enabled=True)
    with store.connect() as db:
        raw = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (ident,)).fetchone()['data'])
        assert automatic_verification_valid(store, db, raw)
        raw['amount_cents'] += 1
        assert not automatic_verification_valid(store, db, raw)
        raw['amount_cents'] -= 1
        original = Path(db.execute('SELECT path FROM attachments WHERE item_id=? LIMIT 1', (ident,)).fetchone()['path'])
        original.write_bytes(b'synthetic changed original')
        assert not automatic_verification_valid(store, db, raw)
