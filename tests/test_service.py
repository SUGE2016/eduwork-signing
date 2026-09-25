import contextlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import service
from sign_macos import validate_zip, validate_app


@contextlib.contextmanager
def workspace():
    old = Path.cwd()
    with tempfile.TemporaryDirectory() as folder:
        os.chdir(folder)
        try:
            yield Path(folder)
        finally:
            os.chdir(old)


class InputTests(unittest.TestCase):
    def asset(self, extension):
        return {'name': 'EduWork-0.3.6-dev.20260921.2-macos-arm64-electron.' + extension, 'state': 'uploaded'}

    def test_prefers_dmg_and_rejects_ambiguity(self):
        dmg, archive = self.asset('dmg'), self.asset('zip')
        self.assertEqual(service.select_asset({'draft': False, 'assets': [archive, dmg]}), dmg)
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            service.select_asset({'draft': False, 'assets': [dmg, dmg.copy()]})
        self.assertIsNone(service.select_asset({'draft': True, 'assets': [dmg]}))

    def test_source_is_not_arbitrary(self):
        with patch.dict(os.environ, {'SOURCE_REPO': 'attacker/EduWork'}):
            with self.assertRaises(ValueError):
                service.source()

    def test_zip_path_traversal(self):
        with workspace():
            for name in ('../identity.p12', '/tmp/overwrite', 'a\\..\\escape'):
                with zipfile.ZipFile('input.zip', 'w') as archive:
                    archive.writestr(name, b'x')
                with self.assertRaises(ValueError):
                    validate_zip('input.zip')

    def test_zip_link_write_and_escape(self):
        with workspace():
            for target, extra in (('../../escape', None), ('real', 'app/link/file')):
                with zipfile.ZipFile('input.zip', 'w') as archive:
                    link = zipfile.ZipInfo('app/link')
                    link.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(link, target)
                    if extra:
                        archive.writestr(extra, 'bad')
                with self.assertRaises(ValueError):
                    validate_zip('input.zip')

    def test_normal_framework_symlink(self):
        with workspace():
            with zipfile.ZipFile('input.zip', 'w') as archive:
                archive.writestr('EduWork.app/Versions/A/binary', 'content')
                link = zipfile.ZipInfo('EduWork.app/Versions/Current')
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(link, 'A')
            validate_zip('input.zip')

    def test_app_symlink_cannot_escape(self):
        with workspace():
            app = Path('EduWork.app'); app.mkdir()
            (app / 'outside').symlink_to('/tmp')
            with self.assertRaisesRegex(ValueError, 'symlink'):
                validate_app(app)


class StateTests(unittest.TestCase):
    def state(self):
        return {'schema': 1, 'release_id': 123, 'source_repo': 'ECNU/EduWork', 'tag': 'signed-456',
                'signed_name': 'test.dmg', 'signed_sha256': service.hashlib.sha256(b'test').hexdigest(),
                'phase': 'awaiting-notary', 'submission_id': 'existing-id', 'notary_name': 'unique.dmg'}

    def test_pending_notary_does_not_resubmit(self):
        with workspace(), patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/signing'}):
            state = self.state()
            def download(repository, asset, destination):
                destination.parent.mkdir(exist_ok=True)
                destination.write_bytes(b'test')
            with patch.object(service, 'api', return_value={'draft': True, 'assets': [{'name': 'test.dmg'}]}), \
                 patch.object(service, 'load_state', return_value=state), \
                 patch.object(service, 'download', side_effect=download), \
                 patch.object(service, 'run', return_value=json.dumps({'status': 'In Progress'})) as command, \
                 patch.object(service, 'output') as output:
                service.resume(123)
                self.assertEqual(command.call_count, 1)
                self.assertEqual(command.call_args.args[2], 'info')
                output.assert_called_once_with('ready', 'false')

    def test_different_file_cannot_be_published(self):
        with workspace(), patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/signing'}):
            state = self.state(); state['phase'] = 'ready'
            Path('verification.json').write_text(json.dumps({'passed': True, 'release_id': 123, 'sha256': 'wrong'}))
            with patch.object(service, 'api', return_value={}) as api, patch.object(service, 'load_state', return_value=state):
                with self.assertRaisesRegex(ValueError, 'exact file'):
                    service.publish(123)
                self.assertEqual(api.call_count, 1)

    def test_failed_draft_does_not_retry_signing(self):
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/signing'}), \
             patch.object(service, 'api', return_value=[{'id': 1, 'draft': True, 'tag_name': 'signed-2'}]), \
             patch.object(service, 'load_state', return_value={'phase': 'failed'}), \
             patch.object(service, 'output') as output:
            service.discover()
            self.assertIn(unittest.mock.call('task', 'none'), output.call_args_list)


if __name__ == '__main__':
    unittest.main()
