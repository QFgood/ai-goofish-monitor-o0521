import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import src.app as application
from src.api import auth


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('WEB_AUTH_ENABLED','true')
    monkeypatch.setenv('APP_DATABASE_FILE', str(tmp_path / 'app.sqlite3'))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'offline-fixture-session-secret-32bytes-minimum')
    monkeypatch.setattr(auth.config.settings, 'web_username', 'fixture-admin')
    monkeypatch.setattr(auth.config.settings, 'web_password', 'offline-fixture-password')
    auth._attempts.clear()
    return TestClient(application.app)


def login(client):
    return client.post('/auth/status', json={'username': 'fixture-admin', 'password': 'offline-fixture-password'})


def test_api_reads_and_writes_require_server_auth_not_local_storage(client):
    assert client.get('/api/market/tasks').status_code == 401
    assert client.post('/api/market/tasks', json={'task_name':'x','keyword':'x'}).status_code == 401
    assert client.post('/api/market/revisits/run').status_code == 401
    assert client.get('/api/tasks').status_code == 401  # original routes also protected
    response = login(client)
    assert response.status_code == 200
    cookie = response.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=strict' in cookie
    assert 'password' not in response.text
    assert client.get('/auth/session').json()['authenticated']
    assert client.get('/api/market/tasks').status_code == 200
    client.post('/auth/logout')
    assert client.get('/api/market/tasks').status_code == 401


def test_weak_credentials_bad_signature_and_cross_site_writes_rejected(client, monkeypatch):
    monkeypatch.setattr(auth.config.settings, 'web_password', 'admin123')
    assert login(client).status_code == 503
    monkeypatch.setattr(auth.config.settings, 'web_password', 'offline-fixture-password')
    assert login(client).status_code == 200
    assert client.post('/api/market/tasks/pause-all', headers={'Origin':'https://untrusted.example'}).status_code == 403
    client.cookies.clear()
    client.cookies.set(auth.COOKIE, 'tampered.invalid')
    assert client.get('/api/market/tasks').status_code == 401


def test_market_task_crud_validation_and_missing_records(client):
    login(client)
    payload = {'task_name':'Mac mini 市场追踪','keyword':'Mac mini M6 16GB','notifications':False}
    created = client.post('/api/market/tasks', json=payload)
    assert created.status_code == 201
    task_id = created.json()['task']['id']
    assert created.json()['task']['mode'] == 'market_tracking'
    assert client.patch(f'/api/market/tasks/{task_id}', json={'enabled':False}).status_code == 200
    assert client.get('/api/market/tasks').json()['items'][0]['enabled'] == 0
    assert client.delete(f'/api/market/tasks/{task_id}').json()['history_retained']
    assert client.get('/api/market/tasks').json()['items'] == []
    assert client.post('/api/market/tasks', json={**payload,'tracking_days':0}).status_code == 422
    assert client.post('/api/market/tasks', json={**payload,'account_state_file':'../../secrets.json'}).status_code == 422
    assert client.get('/api/market/trends?start=2026-10-03&end=2026-10-01').status_code == 422
    assert client.get('/api/market/items?limit=1001').status_code == 422
    assert client.get('/api/market/items/missing/history').status_code == 404
    assert client.get('/api/market/stats').json()['sold_7d']['median_price'] is None
    assert client.get('/api/market/export').status_code == 200


def test_websocket_requires_session_and_same_origin(client):
    with pytest.raises(WebSocketDisconnect) as failure:
        with client.websocket_connect('/ws'):
            pass
    assert failure.value.code == 4401
    login(client)
    with client.websocket_connect('/ws') as websocket:
        websocket.send_text('offline-fixture')
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/ws', headers={'Origin':'https://untrusted.example'}):
            pass


def test_expired_session_and_login_rate_limit(client, monkeypatch):
    token = auth.issue_session('fixture-admin')
    client.cookies.set(auth.COOKIE, token)
    original = auth.time.time
    with monkeypatch.context() as clock:
        clock.setattr(auth.time, 'time', lambda: original() + auth.TTL + 1)
        assert client.get('/api/market/tasks').status_code == 401
    client.cookies.clear()
    for _ in range(8):
        assert client.post('/auth/status', json={'username':'fixture-admin','password':'incorrect'}).status_code == 401
    assert login(client).status_code == 429


def test_populated_csv_streams_across_database_pages(client):
    from src.infrastructure.persistence.market_repository import MarketRepository
    login(client)
    with MarketRepository().connection(True) as conn:
        conn.executemany('''INSERT INTO market_items(item_id,title,item_url,first_seen_at,last_seen_at)
            VALUES(?,?,?,?,?)''', [(str(700000+i),'offline fixture',f'https://www.goofish.com/item?id={700000+i}',
                 '2026-10-01T08:30:00+08:00','2026-10-01T08:30:00+08:00') for i in range(501)])
    import csv, io
    response=client.get('/api/market/export')
    assert response.status_code==200
    rows=list(csv.DictReader(io.StringIO(response.text.lstrip('\ufeff'))))
    assert len(rows)==501
    assert len({r['item_id'] for r in rows})==501


def test_home_mode_skips_password_but_still_blocks_cross_site_writes(client,monkeypatch):
    monkeypatch.setenv('WEB_AUTH_ENABLED','false')
    client.cookies.clear()
    session=client.get('/auth/session').json()
    assert session['authentication_required'] is False
    assert client.get('/api/tasks').status_code==200
    assert client.post('/api/market/tasks/pause-all',headers={'Origin':'https://untrusted.example'}).status_code==403
    with client.websocket_connect('/ws') as socket:
        socket.send_text('home fixture')
