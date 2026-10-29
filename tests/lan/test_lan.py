"""Offline migration failure injection; no production services or credentials."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('lan', Path(__file__).resolve().parents[2] / 'scripts/lan.py')
lan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lan)
HASH = '$2a$14$' + 'a' * 53


class LanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.d = lan.Deployment(self.root, 'compose.local.yaml')
        self.d.local.mkdir(mode=0o700)
        health_patch = patch.object(self.d, 'wait_old_health')
        self.health = health_patch.start()
        self.addCleanup(health_patch.stop)
        self.conf = dict(zip(lan.KEYS, ['192.168.9.202', '8000', 'lanadmin', HASH,
                                      'example/app@sha256:' + 'a' * 64, 'app', '1073741824', '10m', '3']))

    def tearDown(self):
        self.tmp.cleanup()

    def fixture(self):
        for name in lan.PATHS:
            path = self.root / name
            if name in ['.env', 'config.json']:
                path.write_text('{}')
            else:
                path.mkdir()
        (self.root / 'compose.local.yaml').write_text('services: {}')
        with sqlite3.connect(self.root / 'data/app.sqlite3') as conn:
            conn.execute('CREATE TABLE tasks (enabled INTEGER, cron TEXT, is_running INTEGER)')
            conn.execute('INSERT INTO tasks VALUES (0, NULL, 0)')
            conn.execute('CREATE TABLE price_snapshots (id INTEGER)')
            conn.execute('INSERT INTO price_snapshots VALUES (1)')

    def old_container(self):
        return {
            'Id': 'ours', 'Image': 'sha256:installed', 'Name': '/app',
            'State': {'Running': True},
            'Config': {'Labels': {'com.docker.compose.service': 'app',
                                  'com.docker.compose.project.working_dir': str(self.root),
                                  'com.docker.compose.project.config_files': str(self.d.old)}},
            'Mounts': [{'Type': 'bind', 'RW': True, 'Source': str(self.root / p),
                        'Destination': '/app/' + p} for p in lan.PATHS],
            'HostConfig': {'ShmSize': 1073741824, 'LogConfig': {
                'Type': 'json-file', 'Config': {'max-size': '20m', 'max-file': '5'}},
                'PortBindings': {'8000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8000'}]}},
        }

    def preflight_with(self, container, *, address='192.168.9.202', extra=None):
        old_app = {'init': True, 'environment': {'APP_DATABASE_FILE': '/app/data/app.sqlite3'},
                   'labels': {'com.centurylinklabs.watchtower.enable': 'false'}}
        old_app.update(extra or {})
        model = {'services': {'app': old_app}, 'networks': {'default': {'name': 'goofish-monitor_default'}}}
        with patch.object(self.d, 'containers', return_value=[container]), patch.object(self.d, 'docker', return_value=json.dumps([{'RepoDigests': [self.conf['APP_IMAGE']]}])), patch.object(self.d, 'compose', return_value=json.dumps(model)) as compose, patch.object(lan, 'run', return_value=json.dumps([{'addr_info': [{'local': address}]}])), patch.object(self.d, 'check_port'):
            result = self.d.preflight()
            compose.assert_called_once_with(False, 'config', '--format', 'json')
            return result

    def test_preflight_uses_actual_compose_and_preserves_runtime_options(self):
        self.fixture()
        conf, _, _, active = self.preflight_with(self.old_container())
        self.assertFalse(active)
        self.assertEqual(conf['APP_IMAGE'], self.conf['APP_IMAGE'])
        self.assertEqual(conf['APP_SHM_SIZE'], '1073741824')
        self.assertEqual((conf['APP_LOG_MAX_SIZE'], conf['APP_LOG_MAX_FILE']), ('20m', '5'))

    def test_wrong_compose_source_rejected(self):
        self.fixture()
        c = self.old_container()
        c['Config']['Labels']['com.docker.compose.project.config_files'] = str(self.root / 'docker-compose.yaml')
        with self.assertRaisesRegex(lan.Failure, 'specified standalone'):
            self.preflight_with(c)

    def test_missing_mount_or_wrong_local_ip_rejected(self):
        self.fixture()
        c = self.old_container()
        c['Mounts'].pop()
        with self.assertRaisesRegex(lan.Failure, 'mounts differ'):
            self.preflight_with(c)
        with self.assertRaisesRegex(lan.Failure, 'not assigned'):
            self.preflight_with(self.old_container(), address='192.168.9.201')

    def test_extra_old_command_is_not_silently_dropped(self):
        self.fixture()
        with self.assertRaisesRegex(lan.Failure, 'unsupported settings'):
            self.preflight_with(self.old_container(), extra={'command': ['custom']})

    def test_explicit_rollback_only_changes_own_services(self):
        self.fixture()
        initial = lan.snapshot(self.root)
        rollback = self.root / 'rollback.compose.json'
        lan.save(rollback, '{"services":{"app":{"ports":[{"published":"8000"}]}}}')
        with patch.object(self.d, 'compose') as compose, patch.object(lan, 'run') as run:
            self.d.restore({'rollback_compose': str(rollback)})
            self.assertEqual(compose.call_args_list, [unittest.mock.call(True, 'stop'),
                                                      unittest.mock.call(True, 'rm', '-f', 'gateway')])
            command = run.call_args[0][0]
            self.assertEqual(command[command.index('-p') + 1], 'goofish-monitor')
            self.assertIn(str(rollback), command)
            self.assertIn('never', command)
            self.assertNotIn('-v', command)
            self.health.assert_called_once_with('8000')
        self.assertEqual(lan.snapshot(self.root), initial)

    def test_credentials_idempotent_and_dollars_preserved(self):
        with patch.object(self.d, 'docker', return_value=HASH) as docker:
            self.d.credentials(self.conf)
            original = self.d.creds.read_bytes()
            self.d.credentials(self.conf)
            self.assertEqual(docker.call_count, 1)
            self.assertEqual(self.d.creds.read_bytes(), original)
            self.assertEqual(self.d.settings()['GATEWAY_PASSWORD_HASH'], HASH)
            self.assertEqual(self.d.creds.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.d.config.stat().st_mode & 0o777, 0o600)

    def test_partial_credentials_not_overwritten(self):
        lan.save(self.d.creds, '{}')
        with self.assertRaises(lan.Failure):
            self.d.credentials(self.conf)
        self.assertEqual(self.d.creds.read_text(), '{}')

    def test_unsafe_bind_and_hash_rejected(self):
        for address in ['0.0.0.0', '127.0.0.1', '8.8.8.8', '::']:
            with self.assertRaises(lan.Failure):
                lan.validate_settings(dict(self.conf, LAN_BIND_IP=address))
        with self.assertRaises(lan.Failure):
            lan.validate_settings(dict(self.conf, GATEWAY_PASSWORD_HASH='plaintext'))

    def test_snapshot_detects_changes_and_scheduled_tasks(self):
        self.fixture()
        initial = lan.snapshot(self.root)
        (self.root / 'state/session.json').write_text('{"test": true}')
        self.assertNotEqual(lan.snapshot(self.root), initial)
        with sqlite3.connect(self.root / 'data/app.sqlite3') as conn:
            conn.execute("UPDATE tasks SET enabled=1, cron='* * * * *'")
        with self.assertRaises(lan.Failure):
            lan.snapshot(self.root)

    def test_other_container_port_conflict(self):
        foreign = [{'Id': 'foreign', 'HostConfig': {'PortBindings': {'80/tcp': [
            {'HostPort': '8000', 'HostIp': '127.0.0.1'}]}}}]
        with patch.object(self.d, 'docker', side_effect=['foreign', json.dumps(foreign)]):
            with self.assertRaisesRegex(lan.Failure, 'another container'):
                self.d.check_port(self.conf, [])

    def test_native_port_conflict(self):
        with patch.object(self.d, 'docker', return_value=''), patch.object(lan, 'run', return_value='LISTEN 0 128 192.168.9.202:8000 0.0.0.0:*'):
            with self.assertRaisesRegex(lan.Failure, 'outside this project'):
                self.d.check_port(self.conf, [])

    def test_own_old_port_allowed(self):
        own = {'Id': 'ours', 'HostConfig': {'PortBindings': {'8000/tcp': [
            {'HostPort': '8000', 'HostIp': '127.0.0.1'}]}}}
        with patch.object(self.d, 'docker', side_effect=['ours', json.dumps([own])]), patch.object(lan, 'run', return_value='LISTEN 0 128 127.0.0.1:8000 0.0.0.0:*'):
            self.d.check_port(self.conf, [own])

    def test_repeat_apply_does_not_touch_credentials_or_stop(self):
        with patch.object(self.d, 'preflight', return_value=(self.conf, {}, {}, True)), patch.object(self.d, 'verify') as verify, patch.object(self.d, 'compose') as compose:
            self.d.apply()
            verify.assert_called_once()
            compose.assert_not_called()

    def test_backup_failure_restarts_original_without_up(self):
        self.fixture()
        original = lan.snapshot(self.root)
        with patch.object(self.d, 'preflight', return_value=(self.conf, {}, original, False)), patch.object(self.d, 'compose', return_value='{"services":{"app":{"ports":[{"published":"8000"}]}}}') as compose, patch.object(lan.tarfile, 'open', side_effect=OSError('injected backup failure')), patch.object(lan, 'run') as run:
            with self.assertRaises(OSError):
                self.d.apply()
            self.assertIn('up', run.call_args[0][0])
            self.assertNotIn(unittest.mock.call(True, 'up', '-d', '--pull', 'never', '--no-build'), compose.call_args_list)
        self.assertEqual(lan.snapshot(self.root), original)

    def test_validation_failure_restarts_original(self):
        self.fixture()
        initial = lan.snapshot(self.root)
        with patch.object(self.d, 'preflight', return_value=(self.conf, {}, initial, False)), patch.object(self.d, 'compose', return_value='{"services":{"app":{"ports":[{"published":"8000"}]}}}'), patch.object(self.d, 'credentials', return_value=self.conf), patch.object(self.d, 'validate', side_effect=lan.Failure('injected invalid config')), patch.object(lan, 'run') as run:
            with self.assertRaises(lan.Failure):
                self.d.apply()
            self.assertIn('up', run.call_args[0][0])
        self.assertEqual(lan.snapshot(self.root), initial)

    def test_verification_failure_rolls_back_without_data_restore(self):
        self.fixture()
        initial = lan.snapshot(self.root)
        with patch.object(self.d, 'preflight', return_value=(self.conf, {}, initial, False)), patch.object(self.d, 'compose', return_value='{"services":{"app":{"ports":[{"published":"8000"}]}}}'), patch.object(self.d, 'credentials', return_value=self.conf), patch.object(self.d, 'validate'), patch.object(self.d, 'verify', side_effect=lan.Failure('injected 502')), patch.object(self.d, 'restore') as restore:
            with self.assertRaises(lan.Failure):
                self.d.apply()
            restore.assert_called_once()
            backup = Path(restore.call_args[0][0]['backup'])
            self.assertEqual((backup / 'preserved.tar.gz').stat().st_mode & 0o777, 0o600)
        self.assertEqual(lan.snapshot(self.root), initial)


if __name__ == '__main__':
    unittest.main()
