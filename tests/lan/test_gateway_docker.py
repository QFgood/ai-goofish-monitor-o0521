"""Real Caddy tests, opt-in: RUN_LAN_DOCKER_TESTS=1 python -m unittest discover -s tests/lan -v."""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
CADDY = 'caddy:2.11.2-alpine@sha256:834468128c7696cec0ceea6172f7d692daf645ae51983ca76e39da54a97c570d'
UPSTREAM = 'python:3.12-alpine'
SERVER = r'''
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64, hashlib
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.headers.get('Upgrade', '').lower() == 'websocket':
            key = self.headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', base64.b64encode(hashlib.sha1(key.encode()).digest()).decode())
            self.end_headers()
            self.wfile.flush()
            first, second = self.rfile.read(2)
            mask = self.rfile.read(4)
            data = self.rfile.read(second & 127)
            payload = bytes(v ^ mask[i % 4] for i, v in enumerate(data))
            self.wfile.write(bytes([0x81, len(payload)]) + payload)
            self.wfile.flush()
            self.close_connection = True
            return
        self.send_response(200)
        self.send_header('Content-Length', '14')
        self.end_headers()
        self.wfile.write(b'test-upstream!')
ThreadingHTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
'''


def docker(*args, input=None):
    proc = subprocess.run(['docker', '--host=unix:///var/run/docker.sock', *args], input=input, text=True, capture_output=True)
    if proc.returncode:
        raise RuntimeError('Docker test command failed (output suppressed)')
    return proc.stdout


@unittest.skipUnless(os.environ.get('RUN_LAN_DOCKER_TESTS') == '1', 'requires opt-in local Docker')
class GatewayDockerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='lan-gateway-test-')
        cls.path = Path(cls.tmp.name)
        cls.suffix = secrets.token_hex(5)
        cls.network = 'lan-test-' + cls.suffix
        cls.app = cls.network + '-upstream'
        cls.gateway = cls.network + '-gateway'
        cls.password = secrets.token_urlsafe(32) + '$test'
        cls.hashed = docker('run', '--rm', '-i', '--network', 'none', CADDY,
                            'caddy', 'hash-password', input=cls.password + '\n').strip()
        cls.auth = 'Basic ' + base64.b64encode(('lanadmin:' + cls.password).encode()).decode()
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        (cls.path / 'server.py').write_text(SERVER)
        shutil.copy(ROOT / 'deploy/lan/Caddyfile', cls.path / 'Caddyfile')
        try:
            docker('network', 'create', cls.network)
            docker('run', '-d', '--name', cls.app, '--network', cls.network, '--network-alias', 'app',
                   '-v', f'{cls.path}/server.py:/server.py:ro', UPSTREAM, 'python', '/server.py')
            docker('run', '-d', '--name', cls.gateway, '--network', cls.network,
                   '-p', '127.0.0.1::80', '-e', 'GATEWAY_USERNAME=lanadmin',
                   '-e', 'GATEWAY_PASSWORD_HASH=' + cls.hashed, '-e', 'NO_PROXY=app,localhost,127.0.0.1',
                   '-v', f'{cls.path}/Caddyfile:/etc/caddy/Caddyfile:ro', CADDY)
            inspected = json.loads(docker('inspect', cls.gateway))[0]
            cls.port = int(inspected['NetworkSettings']['Ports']['80/tcp'][0]['HostPort'])
            for _ in range(40):
                try:
                    if cls.request('/health', cls.auth)[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(.25)
            else:
                raise RuntimeError('Test gateway readiness failed')
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        docker('rm', '-f', cls.gateway, cls.app)
        docker('network', 'rm', cls.network)
        cls.tmp.cleanup()

    @classmethod
    def request(cls, path, auth=None):
        req = urllib.request.Request(f'http://127.0.0.1:{cls.port}' + path)
        if auth:
            req.add_header('Authorization', auth)
        try:
            with cls.opener.open(req, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_all_paths_require_authentication_and_reach_upstream(self):
        for path in ['/', '/api/accounts', '/api/tasks', '/auth/status', '/ws', '/static/test', '/assets/test.js', '/docs', '/openapi.json', '/health']:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 401)
                wrong = 'Basic ' + base64.b64encode(b'lanadmin:wrong').decode()
                self.assertEqual(self.request(path, wrong)[0], 401)
                self.assertEqual(self.request(path, self.auth), (200, b'test-upstream!'))

    def test_authenticated_websocket_upgrade_and_echo(self):
        # Upgrade requests must also be challenged before reaching the upstream.
        for authorization in ['', 'Authorization: Basic bGFuYWRtaW46d3Jvbmc=\r\n']:
            with socket.create_connection(('127.0.0.1', self.port), timeout=5) as denied:
                denied.sendall(('GET /ws HTTP/1.1\r\nHost: localhost\r\n' + authorization +
                                'Connection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\n' +
                                'Sec-WebSocket-Key: dGVtcG9yYXJ5LXRlc3Qta2V5\r\n\r\n').encode())
                self.assertIn(b'401 Unauthorized', denied.recv(4096))
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as conn:
            key = base64.b64encode(secrets.token_bytes(16)).decode()
            req = f'GET /ws HTTP/1.1\r\nHost: localhost\r\nAuthorization: {self.auth}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n'
            conn.sendall(req.encode())
            response = b''
            while b'\r\n\r\n' not in response:
                block = conn.recv(4096)
                self.assertTrue(block)
                response += block
            self.assertIn(b'101 Switching Protocols', response)
            payload, mask = b'lan-test', secrets.token_bytes(4)
            conn.sendall(bytes([0x81, 0x80 | len(payload)]) + mask + bytes(v ^ mask[i % 4] for i, v in enumerate(payload)))
            expected = bytes([0x81, len(payload)]) + payload
            received = b''
            while len(received) < len(expected):
                block = conn.recv(4096)
                self.assertTrue(block)
                received += block
            self.assertEqual(received, expected)

    def test_caddy_validate_and_compose_hash_and_port(self):
        docker('exec', self.gateway, 'caddy', 'validate', '--config', '/etc/caddy/Caddyfile')
        fixture = self.path / 'compose'
        fixture.mkdir()
        shutil.copy(ROOT / 'compose.lan.yaml', fixture / 'compose.lan.yaml')
        (fixture / '.env').write_text('TEST_APPLICATION_SETTING=preserved\n')
        env = fixture / 'gateway.env'
        env.write_text("APP_IMAGE='example/app@sha256:" + 'a' * 64 + "'\nGATEWAY_USERNAME='lanadmin'\nGATEWAY_PASSWORD_HASH='" + self.hashed + "'\n")
        os.chmod(env, 0o600)
        output = docker('compose', '--project-directory', str(fixture), '--env-file', str(env),
                        '-f', str(fixture / 'compose.lan.yaml'), 'config', '--format', 'json')
        model = json.loads(output)
        self.assertEqual(model['name'], 'goofish-monitor')
        app, gateway = model['services']['app'], model['services']['gateway']
        self.assertNotIn('ports', app)
        self.assertEqual(gateway['environment']['GATEWAY_PASSWORD_HASH'], self.hashed.replace('$', '$$'))
        self.assertEqual(gateway['ports'], [{'mode': 'ingress', 'host_ip': '192.168.9.202', 'target': 80, 'published': '8000', 'protocol': 'tcp'}])
        self.assertFalse(model['networks']['default'].get('internal', False))
        self.assertEqual(app['environment']['APP_DATABASE_FILE'], '/app/data/app.sqlite3')
        self.assertEqual(len(app['volumes']), 9)
        # Test the actual deployment validator, not only the separate assertions above.
        import test_lan
        generated = dict(zip(test_lan.lan.KEYS, ['192.168.9.202', '8000', 'lanadmin', self.hashed,
                         'example/app@sha256:' + 'a' * 64, 'app', '1073741824', '10m', '3']))
        deployment = test_lan.lan.Deployment(fixture, 'compose.local.yaml')
        deployment.local.mkdir(mode=0o700)
        test_lan.lan.save(deployment.config, ''.join(f"{k}='{generated[k]}'\n" for k in test_lan.lan.KEYS))
        (fixture / 'deploy/lan').mkdir(parents=True)
        shutil.copy(ROOT / 'deploy/lan/Caddyfile', fixture / 'deploy/lan/Caddyfile')
        project = self.network + '-config'
        from unittest.mock import patch
        try:
            with patch.object(test_lan.lan, 'PROJECT', project):
                deployment.validate(app)
        finally:
            docker('network', 'rm', project + '_default')
