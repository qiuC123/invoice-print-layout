import io
import json
import zipfile
from pathlib import Path
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest
from PIL import Image

from invoice_print_layout.daily_reports import DailyReports, digest, inputs
from invoice_print_layout.logistics import LogisticsStore
from invoice_print_layout.site_photos import SitePhotos
from invoice_print_layout.workbench_web import make_server


@pytest.fixture
def setup(tmp_path):
    store = LogisticsStore(tmp_path / '后勤/tasks.sqlite3')
    photos = SitePhotos(tmp_path, store)
    pid = store.create_project('Test')['id']
    def render(deck, folder):
        for n in range(1, 9):
            Image.new('RGB', (16, 9), 'white').save(folder / f'slide-{n}.png')
        return {'opened': True, 'slides': 8, 'items': [{'pictures': 4 if n == 6 else 0} for n in range(1, 9)]}
    reports = DailyReports(tmp_path, photos, render)
    root = reports.root(pid) / '模板'
    root.mkdir(parents=True)
    slots = [{'relationship': f'rId{i}', 'caption': f'original{i}', 'place': place}
             for i, place in enumerate(['工厂', '现场'] * 2)]
    with zipfile.ZipFile(root / 'template.pptx', 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types><Default Extension="png" ContentType="image/png"/></Types>')
        archive.writestr('locked-first-three', b'untouched')
        archive.writestr('text', '<a:t>26</a:t><a:t>keep work and plan</a:t>')
        archive.writestr('photo', '<s xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">' + ''.join(f'<a:t>original{i}</a:t>' for i in range(4)) + '</s>')
        archive.writestr('rels', '<Relationships>' + ''.join(f'<Relationship Id="rId{i}" Target="old{i}"/>' for i in range(4)) + '</Relationships>')
    config = {'sha256': digest(root / 'template.pptx'), 'name': 'unloading', 'first_day': '2026-09-26',
              'default_start': '08:30', 'default_end': '12:00', 'fixed_content': 'fixed',
              'slots': slots, 'text_fields': {'text': [{'index': 0, 'expected': '26', 'field': 'day'}]},
              'photo_part': 'photo', 'photo_rel_part': 'rels', 'slide_count': 8, 'photo_page': 6}
    (root / 'template.json').write_text(json.dumps(config), encoding='utf-8')
    for n, slot in enumerate(slots):
        add_photo(photos, pid, n, slot['place'])
    body = {'date': '2026-09-26', 'start': '08:30', 'end': '12:00', 'prior_hours': '0', 'confirmed': True}
    return reports, photos, pid, body


def add_photo(photos, pid, n, place, **fields):
    stream = io.BytesIO()
    Image.new('RGB', (16, 12), (n * 20, 50, 60)).save(stream, format='PNG')
    row = photos.receive(pid, 'head.png', stream.getvalue(), automatic=False)
    row.update(state='saved', fields={'kind': '车辆', 'movement': '进场', 'trip_date': '2026-09-26',
               'captured_date': '2026-09-24' if place == '工厂' else '2026-09-26',
               'plate': 'TEST-A' if n < 2 else 'TEST-B', 'place': place, 'label': '车头', **fields})
    photos.put(pid, row)
    return row


def test_pairs_factory_earlier_day_and_preserves_fixed_parts(setup):
    reports, photos, pid, body = setup
    match = reports.match(pid, body['date'])
    assert not match['issues'] and all(s['selected'] for s in match['slots'])
    assert [s['plate'] for s in match['slots']] == ['TEST-A'] * 2 + ['TEST-B'] * 2
    result = reports.generate(pid, body)
    assert result['values']['hours'] == result['values']['cumulative'] == '3.5'
    file = reports.file(pid, result['id'], 'report.pptx')
    with zipfile.ZipFile(file) as archive:
        assert archive.read('locked-first-three') == b'untouched'
        assert b'keep work and plan' in archive.read('text')
        for n, slot in enumerate(match['slots']):
            assert archive.read(f'ppt/media/daily-report-{n+1}.png') == photos.file(pid, slot['selected']).read_bytes()
    assert 'data:image/png;base64' in reports.file(pid, result['id'], 'preview.html').read_text(encoding='utf-8')
    assert len(reports.view(pid)['reports']) == 1
    file.write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='校验'): reports.file(pid, result['id'], 'report.pptx')


def test_ambiguous_photos_require_explicit_valid_selection(setup):
    reports, photos, pid, body = setup
    extra = add_photo(photos, pid, 4, '工厂', plate='TEST-A')
    match = reports.match(pid, body['date'])
    assert len(match['slots'][0]['candidates']) == 2 and not match['slots'][0]['selected']
    with pytest.raises(ValueError, match='选择唯一'): reports.generate(pid, body)
    body['selections'] = {'0': extra['id']}
    assert reports.generate(pid, body)['id']
    body['selections'] = {'0': match['slots'][1]['selected']}
    with pytest.raises(ValueError, match='选择唯一'): reports.generate(pid, body)


@pytest.mark.parametrize('change', [{'label': '车尾'}, {'captured_date': '2026-09-25'}, {'trip_date': '2026-09-25'}])
def test_wrong_angle_capture_or_trip_date_cannot_fill_site_slot(setup, change):
    reports, photos, pid, body = setup
    row = photos.get(pid, reports.match(pid, body['date'])['slots'][1]['selected'])
    row['fields'].update(change)
    photos.put(pid, row)
    assert not reports.match(pid, body['date'])['slots'][1]['candidates']
    with pytest.raises(ValueError, match='补图'): reports.generate(pid, body)


def test_project_pending_and_capacity_isolation(setup):
    reports, photos, pid, body = setup
    other = LogisticsStore(photos.workspace / '后勤/tasks.sqlite3').create_project('Other')['id']
    add_photo(photos, other, 5, '现场', plate='FOREIGN')
    assert not reports.view(other)['available']
    row = add_photo(photos, pid, 6, '现场', plate='PENDING')
    row['state'] = 'pending'; photos.put(pid, row)
    assert not reports.match(pid, body['date'])['issues']
    row['state'] = 'saved'; photos.put(pid, row)
    with pytest.raises(ValueError, match='需要2辆'): reports.generate(pid, body)


@pytest.mark.parametrize('native', [{'opened': False}, {'opened': True, 'slides': 8, 'items': [{'pictures': 0}] * 8}])
def test_native_failure_blocks_history_and_download(setup, native):
    reports, _, pid, body = setup
    reports.renderer = lambda *_: native
    with pytest.raises(ValueError, match='检查未通过'): reports.generate(pid, body)
    assert not reports.view(pid)['reports']
    key = next((reports.root(pid) / '结果').iterdir()).name
    with pytest.raises(ValueError, match='尚未通过'): reports.file(pid, key, 'report.pptx')


def test_reclassification_during_render_blocks_delivery(setup):
    reports, photos, pid, body = setup
    native = reports.renderer
    def changed(deck, folder):
        row = photos.get(pid, reports.match(pid, body['date'])['slots'][0]['selected'])
        row['revision'] += 1; photos.put(pid, row)
        return native(deck, folder)
    reports.renderer = changed
    with pytest.raises(ValueError, match='分类发生变化'): reports.generate(pid, body)


def test_dates_hours_and_explicit_fixed_content_confirmation(setup):
    reports, _, pid, body = setup
    config = reports.config(pid)
    values = inputs(config, {**body, 'date': '2026-09-30', 'prior_hours': '12.5', 'end': '18:00'})
    assert (values['ordinal'], values['hours'], values['cumulative'], values['next_month'], values['next_day']) == ('五', '9.5', '22', '10', '01')
    for changes in [{'end': '08:00'}, {'prior_hours': 'NaN'}, {'prior_hours': ''}, {'date': '2026-09-25'}]:
        with pytest.raises(ValueError): inputs(config, {**body, **changes})
    with pytest.raises(ValueError, match='确认'): reports.generate(pid, {**body, 'confirmed': False})
    for key, name in [('../outside', 'report.pptx'), ('a'*32, 'receipt.json')]:
        with pytest.raises(ValueError): reports.file(pid, key, name)
    (reports.root(pid) / '模板/template.pptx').write_bytes(b'tampered')
    with pytest.raises(ValueError, match='模板校验'): reports.view(pid)


def test_http_endpoints_csrf_project_scope_and_validated_download(setup, monkeypatch):
    reports, photos, pid, body = setup
    monkeypatch.setattr('invoice_print_layout.workbench_web.DailyReports', lambda *_: reports)
    server = make_server(photos.workspace, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(base + '/api/daily-reports?project=' + pid) as response:
            state = json.load(response)
        assert state['available']
        def post(action, token):
            request = Request(base + '/api/photos/' + action, data=json.dumps({**body, 'project_id': pid}).encode(),
                              headers={'Content-Type': 'application/json', 'X-Workbench-Token': token})
            with urlopen(request) as response: return json.load(response)
        with pytest.raises(HTTPError) as blocked: post('daily-generate', '')
        assert blocked.value.code == 403
        assert len(post('daily-match', state['token'])['slots']) == 4
        result = post('daily-generate', state['token'])
        with urlopen(base + result['preview']) as response:
            assert response.headers['Content-Type'].startswith('text/html') and b'data:image/png' in response.read()
        with urlopen(base + result['download']) as response: assert response.read().startswith(b'PK')
        other = LogisticsStore(photos.workspace / '后勤/tasks.sqlite3').create_project('other')['id']
        with pytest.raises(HTTPError): urlopen(base + result['download'].replace(pid, other))
    finally:
        server.shutdown(); server.server_close(); thread.join(2)
