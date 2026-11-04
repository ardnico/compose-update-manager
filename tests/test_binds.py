import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('svc_update_binds', Path(__file__).parents[1] / 'svc_update.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class TestBinds(unittest.TestCase):
    def test_rw_bind_archived_and_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / 'app' / 'data'
            data.mkdir(parents=True)
            (data / 'record.txt').write_text('before')
            backup_dir = root / 'backups'
            backup_dir.mkdir()
            mod.check_bind_source(data, backup_dir, [root / 'app'], True)
            archive = root / 'data.tar'
            mod.archive_bind(data, archive)
            mod.verify_bind_archive(archive)
            (data / 'record.txt').write_text('after')
            (data / 'new.txt').write_text('new version')
            mod.replace_bind_from_archive(archive, data, 'directory')
            self.assertEqual((data / 'record.txt').read_text(), 'before')
            self.assertFalse((data / 'new.txt').exists())

    def test_file_bind_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'settings.conf'
            source.write_text('original')
            archive = root / 'settings.tar'
            mod.archive_bind(source, archive)
            source.write_text('changed')
            mod.replace_bind_from_archive(archive, source, 'file')
            self.assertEqual(source.read_text(), 'original')

    def test_rw_requires_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / 'data'
            data.mkdir()
            with self.assertRaisesRegex(mod.UpdateError, 'allowed_bind_roots'):
                mod.check_bind_source(data, root / 'backups', [], True)
            self.assertEqual(mod.check_bind_source(data, root / 'backups', [], False), data)

    def test_reject_nested_binds_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / 'data'
            data.mkdir()
            (data / 'link').symlink_to('/etc/passwd')
            with self.assertRaisesRegex(mod.UpdateError, 'Symlink'):
                mod.ensure_plain_tree(data)

    def test_realistic_compose_allows_build_on_other_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'versions.env').write_text('NGINX_VERSION=1.27.0\n')
            app_data = root / 'project' / 'data'
            app_data.mkdir(parents=True)
            nginx_conf = root / 'nginx.conf'
            nginx_conf.write_text('server {}')
            svc = {'_dir': root, 'compose_service': 'nginx', 'version_variable': 'NGINX_VERSION', 'image': 'nginx'}
            config = {'services': {'app': {'image': 'kakeibo-app:latest', 'build': {'context': '.'},
                       'volumes': [{'type': 'bind', 'source': str(app_data), 'target': '/app/data'}]},
                       'nginx': {'image': 'nginx:1.27.0',
                       'volumes': [{'type': 'bind', 'source': str(nginx_conf),
                                    'read_only': True, 'target': '/etc/nginx/conf.d/default.conf'}]}},
                      'volumes': {}}
            with patch.object(mod, 'compose', side_effect=lambda *args, **kw: 'cid'), \
                 patch.object(mod, 'cmd', return_value=json.dumps([{'State': {'Running': True}}])):
                result = mod.inspect_stack(svc, config, root / 'backup', [root / 'project'])
            self.assertEqual(result['volumes'], [])
            # Only nginx is stopped; app's writable data must NOT be backed up or restored.
            self.assertEqual(len(result['binds']), 1)
            self.assertFalse(result['binds'][0]['writable'])
            self.assertEqual(result['binds'][0]['source'], str(nginx_conf))


if __name__ == '__main__':
    unittest.main()
