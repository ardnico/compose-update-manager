import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('svc_update_new', Path(__file__).parents[1] / 'svc_update.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class VersionPolicies(unittest.TestCase):
    def test_policies(self):
        tags = ['1.27.1-alpine', '1.27.2-alpine', '1.28.0-alpine', '1.28.3-alpine',
                '1.29.0-alpine', '2.0.0-alpine', '2.0.2-alpine', '3.1.0-alpine',
                '1.27.3', '1.30.0-bookworm', 'latest', '1.28.0-alpine3.20']
        self.assertEqual(mod.next_version('1.27.0-alpine', tags, 'patch'), '1.27.1-alpine')
        self.assertEqual(mod.next_version('1.27.0-alpine', tags, 'minor'), '1.28.0-alpine')
        self.assertEqual(mod.next_version('1.27.0-alpine', tags, 'major'), '2.0.0-alpine')
        self.assertEqual(mod.next_version('1.27.0-alpine', tags, 'any'), '1.27.1-alpine')
        self.assertEqual(mod.next_version('1.27.0-alpine', tags, 'latest'), '3.1.0-alpine')
        self.assertIsNone(mod.next_version('1.27.0-alpine3.20', ['1.27.1-alpine'], 'patch'))
        self.assertEqual(mod.next_version('1.27.0', tags, 'patch'), '1.27.3')

    def test_versions_are_numeric_not_lexical(self):
        self.assertEqual(mod.next_version('1.9.9', ['1.10.0', '1.9.10'], 'any'), '1.9.10')
        self.assertEqual(mod.next_version('1.9.9', ['1.10.0', '1.9.10'], 'minor'), '1.10.0')
        self.assertIsNone(mod.next_version('1.9.9', ['1.9.9-alpine'], 'patch'))
        for invalid in ['latest', 'v1.2.3', '1.2', '1.02.3', '1.2.3-rc+dev']:
            with self.subTest(invalid=invalid), self.assertRaises(mod.UpdateError):
                mod.parse_version(invalid)
        with self.assertRaises(mod.UpdateError):
            mod.next_version('1.2.3', [], 'nonsense')


class ComposeFiles(unittest.TestCase):
    def test_compose_filename_auto_and_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'docker-compose.yml').write_text('services: {}\n')
            self.assertEqual(mod.select_compose_files(root, {}), [root / 'docker-compose.yml'])
            (root / 'compose.yaml').write_text('services: {}\n')
            self.assertEqual(mod.select_compose_files(root, {}), [root / 'compose.yaml'])
            self.assertEqual(mod.select_compose_files(root, {'compose_file': 'docker-compose.yml'}),
                             [root / 'docker-compose.yml'])
            (root / 'custom.yaml').write_text('services: {}\n')
            self.assertEqual(mod.select_compose_files(root, {'compose_files': ['custom.yaml', 'docker-compose.yml']}),
                             [root / 'custom.yaml', root / 'docker-compose.yml'])
            for selector in [{'compose_file': '../outside.yml'}, {'compose_file': '/etc/compose.yml'},
                             {'compose_file': 'missing.yml'}, {'compose_files': []},
                             {'compose_files': ['compose.yaml', 'compose.yaml']}]:
                with self.subTest(selector=selector), self.assertRaises(mod.UpdateError):
                    mod.select_compose_files(root, selector)

    def test_compose_passes_explicit_files_and_nondependency_args(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ['.env', 'versions.env', 'docker-compose.yml', 'extra.yml']:
                (root / name).write_text('')
            svc = {'_dir': root, 'compose_files': ['docker-compose.yml', 'extra.yml']}
            with patch.object(mod, 'cmd', return_value='') as mock:
                mod.compose(svc, 'up', '-d', '--no-deps', 'nginx')
                cmd = mock.call_args.args[0]
                self.assertEqual(cmd.count('-f'), 2)
                self.assertEqual(cmd[cmd.index('-f') + 1], str(root / 'docker-compose.yml'))
                self.assertEqual(cmd[-4:], ['up', '-d', '--no-deps', 'nginx'])


class TargetOnly(unittest.TestCase):
    def test_shared_rw_bind_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'versions.env').write_text('NGINX_VERSION=1.2.3\n')
            data = root / 'app' / 'data'
            data.mkdir(parents=True)
            svc = {'_dir': root, 'compose_service': 'nginx', 'version_variable': 'NGINX_VERSION', 'image': 'nginx'}
            conf = {'services': {'nginx': {'image': 'nginx:1.2.3', 'volumes': [
                {'type': 'bind', 'source': str(data), 'target': '/data'}]},
                'app': {'volumes': [{'type': 'bind', 'source': str(data), 'target': '/data'}]}}, 'volumes': {}}
            with self.assertRaisesRegex(mod.UpdateError, 'overlaps'):
                mod.inspect_stack(svc, conf, root / 'backups', [root / 'app'])

    def test_target_only_restart_update_and_early_recover(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'versions.env').write_text('NGINX_VERSION=1.2.3\n')
            svc = {'_name': 'nginx', '_dir': root, 'compose_service': 'nginx', 'version_variable': 'NGINX_VERSION', 'image': 'nginx'}
            conf = {'_backup_root': root / 'backups', 'global': {}}
            calls = []
            def fake_compose(_svc, *args, **kwargs):
                calls.append(args)
                if args[0] == 'stop':
                    return ''
                return 'cid'
            with patch.object(mod, 'compose', side_effect=fake_compose), \
                 patch.object(mod, 'compose_config', return_value={'services': {}}), \
                 patch.object(mod, 'inspect_stack', return_value={'volumes': [], 'binds': []}), \
                 patch.object(mod, 'verify_candidate'), \
                 patch.object(mod, 'cmd', return_value='sha256:test'), \
                 patch.object(mod, 'backup', return_value=root / 'dummy'), \
                 patch.object(mod, 'health'), \
                 patch.object(mod, 'emit'):
                mod.do_upgrade(conf, svc, '1.2.4', apply=True)
            self.assertIn(('stop', 'nginx'), calls)
            self.assertIn(('up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'nginx'), calls)
            self.assertNotIn(('down',), calls)
            self.assertEqual(mod.current_version(svc), '1.2.4')

    def test_format3_restore_touches_target_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old_versions = 'NGINX_VERSION=1.2.3\n'
            (root / 'versions.env').write_text('NGINX_VERSION=1.2.4\n')
            path = root / 'archive'
            path.mkdir()
            (path / 'versions.env').write_text(old_versions)
            manifest = {'format': 3, 'scope': 'target', 'service': 'nginx',
                        'compose_service': 'nginx', 'old_version': '1.2.3',
                        'old_image_id': 'sha256:old', 'versions_sha256': mod.sha256(path / 'versions.env'),
                        'volumes': [], 'binds': []}
            (path / 'manifest.json').write_text(json.dumps(manifest))
            svc = {'_name': 'nginx', '_dir': root, 'compose_service': 'nginx', 'image': 'nginx'}
            events = []
            with patch.object(mod, 'compose', side_effect=lambda _svc, *args, **kwargs: events.append(args)), \
                 patch.object(mod, 'cmd', return_value='sha256:old'), \
                 patch.object(mod, 'health'):
                mod.restore(svc, path)
            self.assertEqual((root / 'versions.env').read_text(), old_versions)
            self.assertEqual(events, [('stop', 'nginx'),
                ('up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'nginx')])

    def test_old_backup_rejected_before_any_changes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'manifest.json').write_text(json.dumps({'format': 2, 'service': 'nginx'}))
            svc = {'_name': 'nginx', 'compose_service': 'nginx'}
            with patch.object(mod, 'compose') as run:
                with self.assertRaisesRegex(mod.UpdateError, 'Incompatible backup scope'):
                    mod.restore(svc, root)
                run.assert_not_called()

if __name__ == '__main__':
    unittest.main()
