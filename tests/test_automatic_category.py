import json
import threading

import pytest

from invoice_print_layout.automatic_category import AutomaticCategory
from invoice_print_layout import expense_classifier as classifier
from invoice_print_layout.workbench import ExpenseStore


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = ExpenseStore(tmp_path)
    automatic = AutomaticCategory(store, threading.Lock())
    monkeypatch.setattr(classifier, 'collect', lambda store, item: ({'goods': item['note'], 'purpose': ''}, []))
    calls = []
    def decide(evidence):
        calls.append(evidence)
        return {'category': '材料采购', 'source': 'jev', 'confidence': .93, 'reason': 'synthetic'}
    monkeypatch.setattr(classifier, 'decide', decide)
    def create(note='synthetic item'):
        return store.create({'title': 'test', 'note': note, 'amount': '10', 'category': '其他'})
    return store, automatic, calls, create


def test_auto_save_only_category_and_reuse_after_restart(setup):
    store, auto, calls, create = setup
    item = create()
    assert auto.statuses([item])[item['id']]['pending']
    result = auto.run(item['id'])
    assert result['saved'] and result['category'] == '材料采购'
    saved = store.get(item['id'])
    for key in ['note', 'amount', 'title', 'project', 'stage', 'attachments']:
        assert saved[key] == item[key]
    assert not saved['verified']
    auto = AutomaticCategory(store, threading.Lock())
    assert not auto.statuses([saved])[item['id']]['pending']
    assert auto.run(item['id'])['saved'] and len(calls) == 1
    other = create()
    assert auto.run(other['id'])['source'] == 'cache'
    assert len(calls) == 1
    different = create('different purpose or goods')
    auto.run(different['id'])
    assert len(calls) == 2


def test_human_correction_overrides_cached_model_without_retraining(setup):
    store, auto, calls, create = setup
    item = create()
    auto.run(item['id'])
    before = store.get(item['id'])
    after = store.update(item['id'], {**before, 'category': '酒店'})
    auto.feedback(before, after)
    assert auto.run(create()['id'])['source'] == 'experience'
    assert len(calls) == 1
    assert not auto.statuses([after])[item['id']]['pending']
    # A conflicting human confirmation prevents automatic reuse.
    second = create()
    auto.run(second['id'])
    before = store.get(second['id'])
    after = store.update(second['id'], {**before, 'category': '外卖'})
    auto.feedback(before, after)
    result = auto.run(create()['id'])
    assert result['status'] == 'needs_info' and not result['saved']


def test_empty_evidence_is_not_learned(setup, monkeypatch):
    store, auto, calls, create = setup
    monkeypatch.setattr(classifier, 'decide', lambda _: {'category': None, 'source': 'local'})
    item = create('')
    auto.run(item['id'])
    before = store.get(item['id'])
    after = store.update(item['id'], {**before, 'category': '酒店'})
    auto.feedback(before, after)
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM category_experience').fetchone()[0] == 0
    assert not auto.run(create('')['id'])['saved']


@pytest.mark.parametrize('kind', ['warnings', 'unknown', 'failure'])
def test_incomplete_or_failed_results_stay_pending_review_without_loop(setup, monkeypatch, kind):
    store, auto, calls, create = setup
    item = create()
    if kind == 'warnings':
        monkeypatch.setattr(classifier, 'collect', lambda *args: ({'goods': 'test'}, ['missing attachment']))
    if kind == 'unknown':
        monkeypatch.setattr(classifier, 'decide', lambda _: {'category': None, 'source': 'jev'})
    if kind == 'failure':
        def failure(*args): raise ValueError('private network secret')
        monkeypatch.setattr(classifier, 'collect', failure)
    result = auto.run(item['id'])
    assert not result['saved'] and store.get(item['id']) == item
    assert not auto.statuses([item])[item['id']]['pending']
    assert 'private network secret' not in json.dumps(result)
    after = store.update(item['id'], {**item, 'note': 'new goods'})
    assert auto.statuses([after])[item['id']]['pending']


def test_stale_human_edits_and_serial_gate(setup, monkeypatch):
    store, auto, calls, create = setup
    item = create()
    def changed(*args):
        assert auto.run(item['id'])['status'] == 'busy'
        store.update(item['id'], {**item, 'category': '酒店'})
        return {'goods': 'test'}, []
    monkeypatch.setattr(classifier, 'collect', changed)
    assert auto.run(item['id'])['status'] == 'stale'
    assert store.get(item['id'])['category'] == '酒店'
    assert json.loads(auto.record(item['id'])['result'])['status'] == 'manual'


def test_compare_and_save_never_overwrites_newer_fields(setup):
    store, auto, calls, create = setup
    item = create()
    key = auto.input_key(item)
    store.update(item['id'], {**item, 'note': 'new note'})
    assert not auto.save_category(item, '酒店', key)
    assert store.get(item['id'])['note'] == 'new note'


def test_failed_reclassification_keeps_retry_after_new_evidence(setup, monkeypatch):
    store, auto, calls, create = setup
    item = create()
    auto.run(item['id'])
    saved = store.get(item['id'])
    store.update(item['id'], {**saved, 'note': 'new evidence'})
    monkeypatch.setattr(classifier, 'decide', lambda _: {'category': None, 'source': 'local'})
    assert not auto.run(item['id'])['saved']
    current = store.get(item['id'])
    assert not auto.statuses([current])[item['id']]['pending']
    changed = store.update(item['id'], {**current, 'note': 'more evidence'})
    assert auto.statuses([changed])[item['id']]['pending']


def test_manual_known_category_and_cancelled_items_are_not_overwritten(setup):
    store, auto, calls, create = setup
    item = create()
    store.update(item['id'], {**item, 'category': '酒店'})
    assert auto.run(item['id'])['status'] == 'skipped'
    item = create()
    store.transition(item['id'], 'cancelled')
    assert auto.run(item['id'])['status'] == 'skipped'
    assert not calls


def test_http_auto_requires_token_and_persists_category_and_feedback(setup, tmp_path):
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    from invoice_print_layout.workbench_web import make_server
    store, auto, calls, create = setup
    item = create()
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(base+'/api/state') as response: state = json.load(response)
        assert state['category_auto_ready'] and state['classification'][item['id']]['pending']
        def post(route, body, token=state['token']):
            with urlopen(Request(base+'/api/'+route, data=json.dumps(body).encode(), headers={'X-Workbench-Token':token})) as response:
                return json.load(response)
        with pytest.raises(HTTPError) as exc: post('category/auto', {'id':item['id']}, '')
        assert exc.value.code == 403
        assert post('category/auto', {'id':item['id']})['saved']
        saved = store.get(item['id'])
        assert saved['category'] == '材料采购' and not saved['verified']
        post('update', {**saved, 'category':'酒店'})
        second = create()
        assert post('category/auto', {'id':second['id']})['source'] == 'experience'
        assert store.get(second['id'])['category'] == '酒店'
    finally:
        server.shutdown(); server.server_close(); thread.join()
