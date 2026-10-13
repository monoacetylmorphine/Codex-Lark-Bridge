"""用 Docker 替身验证发布事务；不连接主机、不读取生产凭据。"""

import copy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
SOURCE = (ROOT / 'deploy.sh').read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]


class DeployTests(unittest.TestCase):
    def setUp(self):
        namespace = {'__name__': 'deploy_test'}
        exec(compile(SOURCE, str(ROOT / 'deploy.sh'), 'exec'), namespace)
        self.code = namespace
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        (self.bundle / 'deploy.sh').write_text((ROOT / 'deploy.sh').read_text())
        (self.bundle / 'compose.production.yaml').write_text('test fixture')
        self.data = self.root / 'data'
        self.data.mkdir()
        (self.root / 'codex').mkdir()
        (self.data / '.env').write_text('')  # 测试不需要也不生成凭据。
        (self.data / 'config.json').write_text(json.dumps({
            'storage': {'databasePath': '~/.codex_bridge/state.sqlite'},
            'mcp': {'transport': 'streamable-http'},
        }))
        with closing(sqlite3.connect(self.data / 'state.sqlite')) as db:
            db.execute('CREATE TABLE example (value INTEGER)')
            db.execute('INSERT INTO example VALUES (42)')
            db.commit()
        host = {'project': 'bridge-app', 'host_codex_dir': str(self.root / 'codex'),
                'host_dir': str(self.data), 'mcp_port': 8000}
        (self.state / 'host.json').write_text(json.dumps(host))
        self.manifest = {'revision': 'a' * 40, 'platform': 'linux/arm64', 'images': {
            service: 'ghcr.io/test/codex-lark-bridge/' + service + '@sha256:' + 'b' * 64
            for service in ('bridge', 'mcp_service')}}
        self.write_manifest()
        self.enterContext(patch.dict(os.environ, {
            'DEPLOY_ROOT': str(self.state), 'DEPLOY_SCRIPT_DIR': str(self.bundle),
            'ROLLBACK_COMPATIBLE': 'false',
        }))
        self.calls = []
        self.failure = None
        self.foreign = False
        self.old = False
        self.code['run'] = self.docker

    def write_manifest(self):
        (self.bundle / 'release.json').write_text(json.dumps(self.manifest))

    def docker(self, args, **kwargs):
        self.calls.append(args)
        if args[1] == 'info':
            return 'linux/aarch64\n'
        if args[1] == 'ps':
            return 'container\n' if self.foreign or self.old else ''
        if args[1] == 'inspect':
            if args[2] == 'container':
                return json.dumps([{'Config': {'Labels': {'com.docker.compose.project':
                    'foreign' if self.foreign else 'bridge-app'}},
                    'State': {'Running': self.foreign, 'ExitCode': 0}}])
            return json.dumps([{'Architecture': 'arm64', 'Os': 'linux', 'Config': {
                'Labels': {'org.opencontainers.image.revision': self.manifest['revision']}}}])
        if args[1] == 'compose':
            operation = args[8]
            if operation == self.failure or (operation == 'up' and self.failure == 'up-always'):
                if self.failure != 'up-always':
                    self.failure = None
                raise subprocess.CalledProcessError(1, args)
        return ''

    def previous(self):
        previous = self.state / 'old'
        previous.mkdir()
        (previous / 'compose.production.yaml').write_text('test fixture')
        receipt = {'bundle': str(previous), 'release': copy.deepcopy(self.manifest),
                   'host_hash': hashlib.sha256((self.state / 'host.json').read_bytes()).hexdigest()}
        (self.state / 'current.json').write_text(json.dumps(receipt))
        self.old = True
        return receipt

    def operations(self):
        return [args[8] for args in self.calls if args[1] == 'compose']

    def test_success_records_version_and_consistent_backup(self):
        self.code['main']()
        receipt = json.loads((self.state / 'current.json').read_text())
        with closing(sqlite3.connect(Path(receipt['bundle']) / 'state.backup.sqlite')) as db:
            self.assertEqual(db.execute('SELECT value FROM example').fetchone(), (42,))
        self.assertEqual(receipt['release'], self.manifest)
        self.assertEqual((Path(receipt['bundle']) / 'deploy.sh').read_text(),
                         (ROOT / 'deploy.sh').read_text())
        self.assertFalse((self.state / '.deploy-lock').exists())

    def test_missing_variable_stops_before_docker(self):
        with patch.dict(os.environ, DEPLOY_ROOT=''):
            with self.assertRaisesRegex(ValueError, 'DEPLOY_ROOT'):
                self.code['main']()
        self.assertEqual(self.calls, [])

    def test_mutable_tag_is_rejected(self):
        self.manifest['images']['bridge'] = 'ghcr.io/test/bridge:latest'
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'digest'):
            self.code['main']()
        self.assertEqual(self.calls, [])

    def test_running_foreign_project_blocks_takeover(self):
        self.foreign = True
        with self.assertRaisesRegex(ValueError, '首次切换'):
            self.code['main']()
        self.assertNotIn('stop', self.operations())

    def test_pull_failure_does_not_stop_old_service(self):
        self.previous()
        self.failure = 'pull'
        with self.assertRaises(subprocess.CalledProcessError):
            self.code['main']()
        self.assertNotIn('stop', self.operations())

    def test_compatible_failure_rolls_back_but_reports_failure(self):
        receipt = self.previous()
        self.failure = 'up'
        with patch.dict(os.environ, ROLLBACK_COMPATIBLE='true'):
            with self.assertRaises(subprocess.CalledProcessError):
                self.code['main']()
        self.assertEqual(self.operations()[-3:], ['up', 'stop', 'up'])
        self.assertEqual(json.loads((self.state / 'current.json').read_text()), receipt)

    def test_incompatible_failure_stops_without_rollback(self):
        self.previous()
        self.failure = 'up'
        with self.assertRaises(subprocess.CalledProcessError):
            self.code['main']()
        self.assertEqual(self.operations().count('up'), 1)
        self.assertEqual(self.operations()[-1], 'stop')

    def test_backup_failure_restarts_old_containers(self):
        self.previous()
        with patch('sqlite3.connect', side_effect=sqlite3.OperationalError('backup failed')):
            with self.assertRaises(sqlite3.OperationalError):
                self.code['main']()
        self.assertEqual(self.operations()[-2:], ['stop', 'start'])
        self.assertNotIn('up', self.operations())

    def test_lock_blocks_concurrent_deployment(self):
        (self.state / '.deploy-lock').write_text('another process')
        with self.assertRaises(FileExistsError):
            self.code['main']()
        self.assertEqual(self.calls, [])

    def test_rollback_failure_is_not_reported_as_success(self):
        receipt = self.previous()
        self.failure = 'up-always'
        with patch.dict(os.environ, ROLLBACK_COMPATIBLE='true'):
            with self.assertRaises(subprocess.CalledProcessError):
                self.code['main']()
        self.assertEqual(self.operations()[-3:], ['up', 'stop', 'up'])
        self.assertEqual(json.loads((self.state / 'current.json').read_text()), receipt)

    def test_host_changes_block_implicit_migration(self):
        self.previous()
        path = self.state / 'host.json'
        host = json.loads(path.read_text())
        host['mcp_port'] = 8001
        path.write_text(json.dumps(host))
        with self.assertRaisesRegex(ValueError, 'host.json'):
            self.code['main']()
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
