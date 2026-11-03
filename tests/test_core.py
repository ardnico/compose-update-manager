import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('svc_update', Path(__file__).parents[1] / 'svc_update.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class TestUpdater(unittest.TestCase):
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
