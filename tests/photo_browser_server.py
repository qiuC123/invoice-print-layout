"""Disposable HTTP fixture for the photo UI regression; no real user data."""
import io
import json
import tempfile
from pathlib import Path

from PIL import Image

from invoice_print_layout import site_photos
from invoice_print_layout import photo_briefs
from invoice_print_layout.bot import FeishuBotSettings
from invoice_print_layout.logistics import LogisticsStore
from invoice_print_layout.workbench_web import make_server

with tempfile.TemporaryDirectory(prefix='photo-ui-') as directory:
    photo_briefs.read_bot_settings = lambda _: FeishuBotSettings('cli_ui_fixture', 'ou_ui_fixture')
    photo_briefs.read_bot_secret = lambda _: 'fixture-secret-not-real'
    photo_briefs.PhotoBriefs.send = lambda self, item: 'om_fixture'
    workspace = Path(directory)
    server = make_server(workspace, 0)
    logistics = LogisticsStore(workspace / '后勤/tasks.sqlite3')
    projects = [logistics.create_project(name)['id'] for name in ['照片测试甲', '照片测试乙']]
    with logistics.connect() as db:
        project = json.loads(db.execute('SELECT payload FROM projects WHERE id=?', (projects[1],)).fetchone()[0])
        project['schedule'] = {'phases': [{'key': 'build', 'name': '搭建', 'starts_on': '2026-09-26', 'ends_on': '2026-09-30'}]}
        db.execute('UPDATE projects SET payload=? WHERE id=?', (json.dumps(project), projects[1]))
    incoming = workspace / '导入'
    incoming.mkdir()
    for name, color in [('one.png', 'green'), ('two.png', 'blue')]:
        buffer = io.BytesIO()
        Image.new('RGB', (150, 100), color).save(buffer, format='PNG')
        (incoming / name).write_bytes(buffer.getvalue())
    (incoming / 'broken.jpg').write_bytes(b'broken')
    auto_file = workspace / 'auto.png'
    Image.new('RGB', (150, 100), 'red').save(auto_file)
    site_photos.read_receipt = lambda payload, *args: payload
    site_photos.rows_of = lambda payload: (['施工内容：照片测试乙', '拍摄时间：2026.09.26 10:31', '地点：南昌博览中心', '赣A·A0001']
        if payload == auto_file.read_bytes() else ['拍摄时间：2026.09.24 10:31', '地点：测试工厂', '赣A·A0001'])
    site_photos.classify = lambda evidence: {'model': 'jev-test-fixture', 'answers': {
        k: {'choice': v, 'confidence': .99} for k, v in [('project', 'belongs'), ('kind', 'vehicle'), ('place', 'site')]}}
    print(json.dumps({'base': f'http://127.0.0.1:{server.server_port}', 'projects': projects,
                      'incoming': str(incoming), 'workspace': str(workspace), 'auto_file': str(auto_file)}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
