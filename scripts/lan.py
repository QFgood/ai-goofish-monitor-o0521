#!/usr/bin/env python3
"""Fail-closed, standalone LAN migration. No application API mutations."""
import argparse
import base64
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DOCKER = ['docker', '--host=unix:///var/run/docker.sock']
PROJECT = 'goofish-monitor'
CADDY = 'caddy:2.11.2-alpine@sha256:834468128c7696cec0ceea6172f7d692daf645ae51983ca76e39da54a97c570d'
PATHS = ['.env', 'data', 'state', 'prompts', 'logs', 'images', 'jsonl', 'price_history', 'config.json']
KEYS = ['LAN_BIND_IP', 'LAN_PORT', 'GATEWAY_USERNAME', 'GATEWAY_PASSWORD_HASH', 'APP_IMAGE',
        'APP_CONTAINER_NAME', 'APP_SHM_SIZE', 'APP_LOG_MAX_SIZE', 'APP_LOG_MAX_FILE']


class Failure(Exception):
    pass


def run(args, *, input=None):
    # Never echo child output: Compose errors may contain interpolated secrets.
    env = os.environ.copy()
    if args[0] == 'docker':
        for key in ['DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH']:
            env.pop(key, None)
    proc = subprocess.run(args, input=input, text=True, capture_output=True, env=env)
    if proc.returncode:
        raise Failure(f'Command failed: {args[0]} {args[1] if len(args) > 1 else ""}; output suppressed (may contain secrets)')
    return proc.stdout


def private(path):
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise Failure(f'Require regular file with mode 600: {path}')


def save(path, value):
    # Exclusive creation also prevents accidental credential/config replacement.
    with path.open('x', encoding='utf-8') as stream:
        os.chmod(path, 0o600)
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def read_env(path):
    private(path)
    result = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r"([A-Z_]+)='([^'\n]*)'", line)
        if not match or match[1] not in KEYS or match[1] in result:
            raise Failure('Invalid gateway.env; use single-quoted values, no interpolation')
        result[match[1]] = match[2]
    if set(result) != set(KEYS):
        raise Failure('Incomplete gateway.env')
    validate_settings(result)
    return result


def validate_settings(env):
    addr = ipaddress.ip_address(env['LAN_BIND_IP'])
    if addr.version != 4 or not addr.is_private or addr.is_unspecified or addr.is_loopback or addr.is_multicast:
        raise Failure('LAN_BIND_IP must be a specific private LAN IPv4 address')
    if not 1 <= int(env['LAN_PORT']) <= 65535:
        raise Failure('Invalid LAN_PORT')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', env['GATEWAY_USERNAME']):
        raise Failure('Unsafe gateway username')
    if not re.fullmatch(r'\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}', env['GATEWAY_PASSWORD_HASH']):
        raise Failure('Expected bcrypt password hash')
    if not re.fullmatch(r'[^\s]+@sha256:[0-9a-f]{64}', env['APP_IMAGE']):
        raise Failure('APP_IMAGE must be pinned by digest')


def snapshot(root):
    db = root / 'data/app.sqlite3'
    if not db.is_file() or db.is_symlink():
        raise Failure('Existing data/app.sqlite3 required; refusing initialization')
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as conn:
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise Failure('SQLite quick_check failed')
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        counts = {name: conn.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in tables}
        # The application starts its scheduler during boot. Refuse automatic scraping.
        if 'tasks' not in tables:
            raise Failure('Unrecognized database schema')
        if conn.execute("SELECT count(*) FROM tasks WHERE is_running != 0 OR (enabled != 0 AND coalesce(cron, '') != '')").fetchone()[0]:
            raise Failure('Running/scheduled tasks exist. Stop/pause them in the existing UI before migration; script does not edit tasks')
    hashes = {}
    for name in ['.env', 'config.json', 'state', 'prompts']:
        target = root / name
        for file in ([target] if target.is_file() else sorted(target.rglob('*'))):
            if file.is_symlink():
                raise Failure('Symlinks in preserved configuration are unsupported')
            if file.is_file():
                hashes[str(file.relative_to(root))] = hashlib.sha256(file.read_bytes()).hexdigest()
    return {'counts': counts, 'hashes': hashes}


class Deployment:
    def __init__(self, root, old):
        self.root = root.resolve()
        self.old = (self.root / old).resolve()
        self.local = self.root / '.lan'
        self.config = self.local / 'gateway.env'
        self.creds = self.local / 'credentials.json'
        self.journal = self.local / 'migration.json'

    def docker(self, *args, input=None):
        return run([*DOCKER, *args], input=input)

    def compose(self, lan, *args):
        # Ignore caller's Compose and interpolation overrides; keep .env as-is.
        env = os.environ.copy()
        for key in [*KEYS, 'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'COMPOSE_FILE', 'COMPOSE_PROJECT_NAME', 'COMPOSE_PROFILES', 'COMPOSE_ENV_FILES']:
            env.pop(key, None)
        command = [*DOCKER, 'compose', '--project-directory', str(self.root), '-p', PROJECT]
        if lan:
            command += ['--env-file', str(self.config)]
        command += ['-f', str(self.root / 'compose.lan.yaml' if lan else self.old), *args]
        proc = subprocess.run(command, env=env, text=True, capture_output=True)
        if proc.returncode:
            raise Failure('Compose command failed; output suppressed to protect configuration')
        return proc.stdout

    def containers(self):
        ids = self.docker('ps', '-aq', '--filter', f'label=com.docker.compose.project={PROJECT}').split()
        return json.loads(self.docker('inspect', *ids)) if ids else []

    def settings(self):
        return read_env(self.config)

    def preflight(self):
        if not self.old.is_file() or self.old.parent != self.root:
            raise Failure('Explicit old Compose must exist in deployment root')
        for name in PATHS:
            p = self.root / name
            if not p.exists() or p.is_symlink():
                raise Failure(f'Missing/unsafe preserved path: {name}; no paths will be initialized')
            if p.is_file() != (name in ['.env', 'config.json']):
                raise Failure(f'Wrong path type: {name}')
        items = self.containers()
        apps = [c for c in items if c['Config']['Labels'].get('com.docker.compose.service') == 'app']
        if len(apps) != 1 or not apps[0]['State']['Running']:
            raise Failure('Exactly one running app in goofish-monitor required')
        app = apps[0]
        labels = app['Config']['Labels']
        active_lan = Path(labels.get('com.docker.compose.project.config_files', '')) == self.root / 'compose.lan.yaml'
        if labels.get('com.docker.compose.project.working_dir') != str(self.root):
            raise Failure('Container working directory does not match --directory')
        rollback_file = None
        if self.journal.exists():
            private(self.journal)
            rollback_file = json.loads(self.journal.read_text())['rollback_compose']
        if not active_lan and labels.get('com.docker.compose.project.config_files') not in {str(self.old), rollback_file}:
            raise Failure('Running app was not created by the specified standalone old Compose')
        if {c['Config']['Labels'].get('com.docker.compose.service') for c in items} - {'app', 'gateway'}:
            raise Failure('Unexpected services in project; manual review required')
        if not active_lan and any(c['Config']['Labels'].get('com.docker.compose.service') == 'gateway' for c in items):
            raise Failure('Unexpected existing gateway')
        expected = {('/app/' + (name if name != '.env' else '.env')): str(self.root / name) for name in PATHS}
        mounts = {m['Destination']: m['Source'] for m in app['Mounts'] if m['Type'] == 'bind' and m['RW']}
        if mounts != expected or len(app['Mounts']) != len(expected):
            raise Failure('Installed mounts differ from required paths or have unsupported extras')
        image = json.loads(self.docker('image', 'inspect', app['Image']))[0]
        digests = image.get('RepoDigests') or []
        if not digests:
            raise Failure('Installed image has no repository digest; refusing upgrade/rebuild')
        conf = self.settings() if self.config.exists() else {
            'LAN_BIND_IP': '192.168.9.202', 'LAN_PORT': '8000',
            'APP_IMAGE': digests[0], 'APP_CONTAINER_NAME': app['Name'].lstrip('/'),
            'APP_SHM_SIZE': str(app['HostConfig']['ShmSize']),
            'APP_LOG_MAX_SIZE': app['HostConfig']['LogConfig']['Config'].get('max-size', '10m'),
            'APP_LOG_MAX_FILE': app['HostConfig']['LogConfig']['Config'].get('max-file', '3'),
        }
        if conf['APP_IMAGE'] not in digests:
            raise Failure('Configured app digest differs from installed image')
        if app['HostConfig']['LogConfig']['Type'] != 'json-file':
            raise Failure('Only existing json-file logging is supported')
        if set(app['HostConfig']['LogConfig']['Config']) - {'max-size', 'max-file'}:
            raise Failure('Unsupported existing log options')
        # Do not silently drop deployment-specific command/security/environment settings.
        old_config = json.loads(self.compose(active_lan, 'config', '--format', 'json'))
        old_app = old_config['services']['app']
        allowed = {'image', 'container_name', 'pull_policy', 'init', 'ports', 'env_file', 'environment',
                   'volumes', 'restart', 'shm_size', 'logging', 'labels', 'networks', 'expose'}
        if set(old_app) - allowed or set(old_config['services']) - {'app', 'gateway'}:
            raise Failure('Old Compose has unsupported settings; review before migration')
        networks = old_config.get('networks', {})
        if set(networks) != {'default'} or any(k not in {'name'} for k in networks['default']):
            raise Failure('Custom/internal/external network is unsupported')
        if not old_app.get('init'):
            raise Failure('Existing app must use init for process reaping')
        if old_app.get('labels', {}).get('com.centurylinklabs.watchtower.enable') != 'false':
            raise Failure('Existing app must already exclude Watchtower')
        if set(old_app.get('labels', {})) - {'com.centurylinklabs.watchtower.enable'}:
            raise Failure('Additional app labels require manual preservation')
        if not active_lan:
            ports = app['HostConfig'].get('PortBindings') or {}
            if ports != {'8000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': conf['LAN_PORT']}]}:
                raise Failure('Expected only old 127.0.0.1 LAN_PORT:8000 binding')
        addresses = json.loads(run(['ip', '-j', '-4', 'addr', 'show']))
        if conf['LAN_BIND_IP'] not in {a['local'] for i in addresses for a in i.get('addr_info', [])}:
            raise Failure('Target LAN_BIND_IP is not assigned to this host')
        self.check_port(conf, items)
        before = snapshot(self.root)
        if self.config.exists():
            self.validate(old_app)
        print('Preflight OK: actual Compose, mounts, fixed image, local IP, port and data checked')
        return conf, old_app, before, active_lan

    def check_port(self, conf, project_items):
        # Inspect running containers across ALL Docker projects.
        ids = self.docker('ps', '-q').split()
        all_items = json.loads(self.docker('inspect', *ids)) if ids else []
        own = {c['Id'] for c in project_items}
        for c in all_items:
            for bindings in (c['HostConfig'].get('PortBindings') or {}).values():
                for b in bindings or []:
                    if b['HostPort'] == conf['LAN_PORT'] and c['Id'] not in own:
                        raise Failure('LAN_PORT is published by another container; no services changed')
        # ss detects native listeners too. Existing project's sole listener is permitted.
        lines = run(['ss', '-H', '-ltn', 'sport', '=', ':' + conf['LAN_PORT']]).splitlines()
        for line in lines:
            endpoint = line.split()[3]
            permitted = ('127.0.0.1:' + conf['LAN_PORT'], conf['LAN_BIND_IP'] + ':' + conf['LAN_PORT'])
            if endpoint not in permitted:
                raise Failure('Conflicting host listener; no services changed')
            matching = any(b['HostPort'] == conf['LAN_PORT'] and b['HostIp'] == endpoint.rsplit(':', 1)[0]
                           for c in project_items for bs in (c['HostConfig'].get('PortBindings') or {}).values() for b in (bs or []))
            if not matching:
                raise Failure('Port is occupied outside this project')

    def credentials(self, conf):
        if self.config.exists() or self.creds.exists():
            if not (self.config.exists() and self.creds.exists()):
                raise Failure('Partial credentials; refusing regeneration; recover existing files manually')
            private(self.creds)
            return self.settings()
        password = secrets.token_urlsafe(36)
        hashed = self.docker('run', '--rm', '-i', '--network', 'none', CADDY,
                             'caddy', 'hash-password', '--algorithm', 'bcrypt', input=password + '\n').strip()
        conf = dict(conf, GATEWAY_USERNAME='lanadmin', GATEWAY_PASSWORD_HASH=hashed)
        validate_settings(conf)
        save(self.creds, json.dumps({'username': conf['GATEWAY_USERNAME'], 'password': password}) + '\n')
        save(self.config, ''.join(f"{k}='{conf[k]}'\n" for k in KEYS))
        return conf

    def validate(self, old_app=None):
        conf = self.settings()
        model = json.loads(self.compose(True, 'config', '--format', 'json'))
        app, gateway = model['services']['app'], model['services']['gateway']
        if app.get('ports') or gateway['ports'] != [{'mode': 'ingress', 'host_ip': conf['LAN_BIND_IP'],
                                                    'target': 80, 'published': conf['LAN_PORT'], 'protocol': 'tcp'}]:
            raise Failure('Unexpected expanded published ports')
        # `compose config` escapes dollars for a reusable Compose document.
        if gateway['environment']['GATEWAY_PASSWORD_HASH'] != conf['GATEWAY_PASSWORD_HASH'].replace('$', '$$'):
            raise Failure('Compose altered bcrypt hash')
        if old_app and app['environment'] != old_app.get('environment', {}):
            raise Failure('Application environment differs; refusing to lose existing settings')
        actual_hash = self.compose(True, 'run', '--rm', '--no-deps', 'gateway',
                                   'printenv', 'GATEWAY_PASSWORD_HASH').strip()
        if actual_hash != conf['GATEWAY_PASSWORD_HASH']:
            raise Failure('Container received an altered bcrypt hash')
        self.compose(True, 'run', '--rm', '--no-deps', 'gateway', 'caddy', 'validate', '--config', '/etc/caddy/Caddyfile')

    def verify(self, baseline=None):
        conf = self.settings()
        private(self.creds)
        credential = json.loads(self.creds.read_text())
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(path, password=None):
            headers = {}
            if password is not None:
                token = base64.b64encode((credential['username'] + ':' + password).encode()).decode()
                headers['Authorization'] = 'Basic ' + token
            req = urllib.request.Request(f"http://{conf['LAN_BIND_IP']}:{conf['LAN_PORT']}" + path, headers=headers)
            try:
                with opener.open(req, timeout=5) as response:
                    return response.status
            except urllib.error.HTTPError as exc:
                return exc.code
        for _ in range(30):
            try:
                if request('/health', credential['password']) == 200:
                    break
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(1)
        else:
            raise Failure('Authenticated health check failed')
        for path in ['/', '/api/accounts', '/api/tasks', '/auth/status', '/ws', '/assets/test.js', '/docs', '/openapi.json', '/health']:
            if request(path) != 401 or request(path, 'wrong-' + secrets.token_hex(8)) != 401:
                raise Failure('Gateway authentication check failed')
        if request('/', credential['password']) != 200:
            raise Failure('Authenticated application page failed')
        items = self.containers()
        app = next(c for c in items if c['Config']['Labels'].get('com.docker.compose.service') == 'app')
        if app['HostConfig'].get('PortBindings'):
            raise Failure('Application still publishes ports')
        gateway = next(c for c in items if c['Config']['Labels'].get('com.docker.compose.service') == 'gateway')
        if gateway['HostConfig']['PortBindings'] != {'80/tcp': [{'HostIp': conf['LAN_BIND_IP'], 'HostPort': conf['LAN_PORT']}]}:
            raise Failure('Gateway has unexpected host binding')
        if baseline is None and self.journal.exists():
            baseline = json.loads(self.journal.read_text())['baseline']
        if baseline is not None and snapshot(self.root) != baseline:
            raise Failure('Database counts or preserved configuration changed; no automatic data restore')
        print('Verification OK: authentication, upstream, bindings, counts and configuration')

    def wait_old_health(self, port):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(30):
            try:
                with opener.open(f'http://127.0.0.1:{port}/health', timeout=3) as response:
                    if response.status == 200:
                        return
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(1)
        raise Failure('Old app recovery health check failed; restricted backup retained')

    def start_original(self, rollback):
        private(rollback)
        original = json.loads(rollback.read_text())
        port = original['services']['app']['ports'][0]['published']
        run([*DOCKER, 'compose', '--project-directory', str(self.root), '-p', PROJECT,
             '-f', str(rollback), 'up', '-d', '--pull', 'never', '--no-build', 'app'])
        self.wait_old_health(port)

    def restore(self, journal):
        # Stop only this project's services; never restore database by default.
        self.compose(True, 'stop')
        self.compose(True, 'rm', '-f', 'gateway')
        self.start_original(Path(journal['rollback_compose']))
        print('Old app restored and health checked; database untouched. Keep backups and compose.local.yaml.')

    def apply(self):
        conf, old_app, before, active_lan = self.preflight()
        if active_lan:
            self.verify()
            print('Already migrated; credentials and configuration retained')
            return
        if self.journal.exists():
            raise Failure('Previous migration journal exists; inspect/rollback before retrying')
        # Resolve while old service is up. This is sensitive and is saved only under 0700/0600.
        original = json.loads(self.compose(False, 'config', '--format', 'json'))
        original['services']['app']['image'] = conf['APP_IMAGE']
        original['services']['app']['pull_policy'] = 'never'
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        backup_root = self.root / '.lan-backups'
        if backup_root.is_symlink():
            raise Failure('Backup directory must not be a symlink')
        backup = backup_root / stamp
        backup.mkdir(parents=True, mode=0o700)
        os.chmod(backup.parent, 0o700)
        rollback = backup / 'rollback.compose.json'
        save(rollback, json.dumps(original))
        journal = None
        try:
            self.compose(False, 'stop', 'app')
            before = snapshot(self.root)
            archive = backup / 'preserved.tar.gz'
            with archive.open('xb') as stream:
                os.chmod(archive, 0o600)
                with tarfile.open(fileobj=stream, mode='w:gz') as tar:
                    for name in PATHS + [self.old.name]:
                        tar.add(self.root / name, arcname=name, recursive=True)
            with tarfile.open(archive) as tar:
                tar.getmembers()  # force archive read before switching services
            save(backup / 'baseline.json', json.dumps(before))
            conf = self.credentials(conf)
            self.validate(old_app)
            journal = {'rollback_compose': str(rollback), 'baseline': before, 'backup': str(backup)}
            save(self.journal, json.dumps(journal))
            self.compose(True, 'up', '-d', '--pull', 'never', '--no-build')
            self.verify(before)
        except BaseException:
            try:
                if journal:
                    self.restore(journal)
                else:
                    self.start_original(rollback)
            except Exception:
                print('CRITICAL: automatic recovery failed; use restricted rollback.compose.json with docker compose -p goofish-monitor', file=sys.stderr)
            raise
        print(f'LAN gateway configured: http://{conf["LAN_BIND_IP"]}:{conf["LAN_PORT"]}; backup: {backup}')
        print(f'Credentials: {self.creds} (0600; never print to logs). External LAN acceptance still required.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['precheck', 'apply', 'verify', 'rollback'])
    parser.add_argument('--directory', default='/home/o0521/ai-goofish-monitor')
    parser.add_argument('--old-compose', default='compose.local.yaml')
    args = parser.parse_args()
    os.umask(0o077)
    deployment = Deployment(Path(args.directory), args.old_compose)
    if not deployment.root.is_dir():
        raise Failure('Deployment directory missing')
    deployment.local.mkdir(mode=0o700, exist_ok=True)
    if deployment.local.is_symlink() or stat.S_IMODE(deployment.local.stat().st_mode) != 0o700:
        raise Failure('.lan must be a real directory with mode 700')
    with (deployment.local / 'lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure('Another LAN operation is running') from None
        if args.action == 'precheck':
            deployment.preflight()
        elif args.action == 'apply':
            # Pull only Caddy; installed app is never updated.
            deployment.docker('pull', CADDY)
            deployment.apply()
        elif args.action == 'verify':
            deployment.validate()
            deployment.verify()
        else:
            private(deployment.journal)
            deployment.restore(json.loads(deployment.journal.read_text()))


if __name__ == '__main__':
    try:
        main()
    except (Failure, OSError, ValueError, KeyError, sqlite3.Error) as exc:
        # Do not stringify arbitrary exceptions: they can contain credentials or data.
        print(str(exc) if isinstance(exc, Failure) else f'Operation failed ({type(exc).__name__}); private files retained', file=sys.stderr)
        sys.exit(1)
