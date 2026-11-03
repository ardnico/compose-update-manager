import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import io
import json

spec = importlib.util.spec_from_file_location('svc_update', Path(__file__).parents[1] / 'svc_update.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class TestUpdater(unittest.TestCase):
    def test_hub_tags_registry_pagination(self):
        class Response:
            def __init__(self, data, link=''):
                self.data = json.dumps(data).encode()
                self.headers = {'Link': link}
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def read(self, *_):
                return self.data
        calls = []
        def fake_open(request, timeout=0):
            calls.append(request.full_url)
            if 'auth.docker.io' in request.full_url:
                return Response({'token': 'test-token'})
            self.assertEqual(request.get_header('Authorization'), 'Bearer test-token')
            if 'last=' not in request.full_url:
                return Response({'tags': ['1.28.0']}, '</v2/library/nginx/tags/list?n=1000&last=1.28.0>; rel="next"')
            return Response({'tags': ['1.28.1']})
        with patch.object(mod.urllib.request, 'urlopen', side_effect=fake_open):
            self.assertEqual(mod.hub_tags('nginx'), ['1.28.0', '1.28.1'])
        self.assertEqual(len(calls), 3)

    def test_next_patch(self):
        tags = ['1.28.4', '1.28.2', '1.29.0', '1.28.1-alpine', 'latest', '1.28.0']
        self.assertEqual(mod.next_patch('1.28.0', tags), '1.28.2')
        self.assertIsNone(mod.next_patch('1.28.4', tags))
        self.assertIsNone(mod.next_patch('1.27.9', ['1.28.0']))

    def test_env_replace(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'versions.env'
            p.write_text('# comment\nNGINX_VERSION=1.28.0\nDB_VERSION=11.4.0\n')
            mod.replace_env(p, 'NGINX_VERSION', '1.28.2')
            self.assertEqual(mod.parse_env(p)['NGINX_VERSION'], '1.28.2')
            self.assertIn('DB_VERSION=11.4.0', p.read_text())

    def test_reject_env_ambiguous(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'versions.env'
            p.write_text('NGINX_VERSION="latest"\n')
            with self.assertRaises(mod.UpdateError):
                mod.parse_env(p)

    def test_atomic_replace_missing_variable(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'versions.env'
            p.write_text('DB_VERSION=11.4.0\n')
            with self.assertRaises(mod.UpdateError):
                mod.replace_env(p, 'NGINX_VERSION', '1.28.2')
            self.assertEqual(p.read_text(), 'DB_VERSION=11.4.0\n')

if __name__ == '__main__':
    unittest.main()
