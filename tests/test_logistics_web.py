import json
import threading
from http.client import HTTPConnection

from invoice_print_layout.workbench_web import make_server


def test_local_overview_routes_and_origin_protection(tmp_path):
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    try:
        client.request('GET', '/api/logistics')
        response = client.getresponse()
        assert response.status == 200
        data = json.loads(response.read())
        assert data['transport_enabled'] is False
        assert data['tasks'] == data['outbox'] == data['projects'] == []
        for route, expected in [('/logistics', 'logistics.js'), ('/logistics.js', 'loadLogistics'), ('/logistics.css', 'body')]:
            client.request('GET', route)
            response = client.getresponse()
            assert response.status == 200 and expected in response.read().decode()
        client.request('GET', '/api/logistics', headers={'Origin': 'https://untrusted.invalid'})
        response = client.getresponse()
        assert response.status == 403
        response.read()
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_project_and_group_api_persist_and_require_token(tmp_path):
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = HTTPConnection('127.0.0.1', server.server_port, timeout=5)

    def request(method, path, payload=None, token=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Workbench-Token'] = token
        client.request(method, path, json.dumps(payload) if payload is not None else None, headers)
        response = client.getresponse()
        return response.status, json.loads(response.read())

    try:
        status, data = request('GET', '/api/logistics')
        token = data['token']
        assert request('POST', '/api/logistics/projects', {'name': '项目一'})[0] == 403
        status, first = request('POST', '/api/logistics/projects', {'name': '项目一', 'site_name': '测试作业地点'}, token)
        assert status == 200 and first['site_name'] == '测试作业地点'
        assert request('POST', '/api/logistics/projects', {'name': '无效地点', 'site_name': ['invalid']}, token)[0] == 400
        status, second = request('POST', '/api/logistics/projects', {'name': '项目二'}, token)
        assert status == 200
        assert request('POST', '/api/logistics/projects', {'name': '项目一'}, token)[0] == 400
        status, group = request('POST', '/api/logistics/groups', {
            'project_id': first['id'], 'name': '测试群', 'category': 'site'}, token)
        assert status == 200 and group['status'] == 'unbound'
        assert request('GET', '/api/logistics/inbox?project_id=' + first['id'])[1]['groups'][0]['name'] == '测试群'
        assert request('GET', '/api/logistics/inbox?project_id=' + second['id'])[1]['groups'] == []
        assert request('GET', '/api/logistics/inbox')[1]['messages'] == []
        assert request('POST', '/api/logistics/groups/category', {
            'project_id': second['id'], 'id': group['id'], 'category': 'meals'}, token)[0] == 400
        assert request('POST', '/api/logistics/groups/category', {
            'project_id': first['id'], 'id': group['id'], 'category': 'meals'}, token)[0] == 200
        assert request('GET', '/api/logistics/inbox?project_id=' + first['id'])[1]['groups'][0]['category'] == 'meals'
        status, snapshot = request('GET', '/api/logistics')
        assert len(snapshot['projects']) == 2
        assert snapshot['projects'][0]['site_name'] == '测试作业地点'
        assert snapshot['outbox'] == snapshot['tasks'] == []
        todo_path = '/api/logistics/todos'
        assert request('POST', todo_path, {'project_id': first['id'], 'title': '核对照片'})[0] == 403
        status, todo = request('POST', todo_path, {'project_id': first['id'], 'title': '核对照片'}, token)
        assert status == 200
        assert request('GET', todo_path + '?project_id=' + first['id'])[1]['items'] == [todo]
        assert request('GET', todo_path + '?project_id=' + second['id'])[1]['items'] == []
        assert request('POST', todo_path, {**todo, 'project_id': second['id'], 'status': 'done'}, token)[0] == 400
        assert request('POST', todo_path, {**todo, 'status': 'done'}, token)[1]['status'] == 'done'
        assert request('POST', todo_path, {**todo, 'title': '过期覆盖'}, token)[0] == 400
        assert request('GET', todo_path + '?project_id=missing')[0] == 404
        assert request('GET', '/api/logistics/inbox?scope=support&project_id=' + first['id'])[1]['messages'] == []
        scheduled = {'project_id': first['id'], 'title': '每日飞检照片', 'kind': 'scheduled', 'schedule': {
            'enabled': True, 'times': ['18:00'], 'group_ids': ['123@chatroom'],
            'start_on': '2050-09-27', 'end_on': '2050-10-05'}}
        assert request('POST', todo_path, scheduled, token)[0] == 400
        assert request('POST', '/api/photos/settings', {'project_id': first['id'], 'account': 'wxid_test',
            'groups': [{'id': '123@chatroom', 'name': '照片来源群'}], 'auto_classify': True}, token)[0] == 200
        status, daily = request('POST', todo_path, scheduled, token)
        assert status == 200 and daily['schedule']['times'] == ['18:00']
        assert request('GET', todo_path + '?project_id=' + first['id'])[1]['photo_groups'][0]['name'] == '照片来源群'
        assert request('POST', todo_path, {**daily, 'project_id': second['id']}, token)[0] == 400
        assert request('POST', todo_path, {**daily, 'schedule': {**daily['schedule'], 'enabled': False}}, token)[0] == 200
        removal = {'project_id': first['id'], 'id': daily['id'], 'revision': daily['revision'] + 1}
        assert request('POST', todo_path + '/delete', removal)[0] == 403
        assert request('POST', todo_path + '/delete', {**removal, 'project_id': second['id']}, token)[0] == 400
        status, deleted = request('POST', todo_path + '/delete', removal, token)
        assert status == 200 and deleted['deleted_at']
        assert daily['id'] not in {t['id'] for t in request('GET', todo_path + '?project_id=' + first['id'])[1]['items']}
        status, restored = request('POST', todo_path + '/restore', {**removal, 'revision': deleted['revision']}, token)
        assert status == 200 and not restored['deleted_at'] and restored['schedule']['enabled'] is False
        assert request('GET', '/api/logistics/inbox?project_id=missing')[0] == 404
        assert request('POST', '/api/logistics/messages/assign', {
            'key': 'missing', 'project_id': first['id'], 'category': 'meals'}, token)[0] == 400
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
