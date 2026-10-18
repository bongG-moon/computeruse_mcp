"""Screenshot crop IPC belongs to a session and leaves no normal-run residue."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import image_pixels
from image_pixels import crop_png, crop_regions, target_from_region
from operations import OperationError
from test_image_steps import png


class CaptureScopeContractTests(unittest.TestCase):
    def test_all_entry_points_forward_session_scope(self):
        source = png(100, 100)
        region = {'x': 20, 'y': 30, 'width': 16, 'height': 16}
        runtime = SimpleNamespace(run_dir='unused-by-memory-decoder')
        with mock.patch.object(image_pixels, '_native_crop', return_value=None) as native:
            cropped = crop_regions(source, [region], runtime=runtime)[0]
            self.assertEqual(native.call_args.kwargs, {'runtime': runtime, 'run_dir': None})
            self.assertEqual(crop_png(source, region, run_dir='session-path'), cropped)
            self.assertEqual(native.call_args.kwargs, {'runtime': None, 'run_dir': 'session-path'})
            target = target_from_region(source, region, runtime=runtime)
            self.assertEqual(native.call_args.kwargs, {'runtime': runtime, 'run_dir': None})
            self.assertEqual(target['template_png'], cropped)

    def test_non_session_runtime_uses_memory_without_global_temp(self):
        region = {'x': 20, 'y': 30, 'width': 16, 'height': 16}
        with mock.patch.object(image_pixels.tempfile, 'TemporaryDirectory') as temporary:
            result = target_from_region(png(100, 100), region, runtime=SimpleNamespace())
            temporary.assert_not_called()
        self.assertEqual(result['width'], 16)

    def test_conflicting_scope_rejected(self):
        with self.assertRaises(OperationError) as raised:
            image_pixels._native_crop('', [], runtime=SimpleNamespace(), run_dir='explicit')
        self.assertEqual(raised.exception.code, 'unsafe_store')


@unittest.skipUnless(os.name == 'nt', 'The optional native decoder is Windows-only.')
class NativeCaptureScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run_dir = self.root / 'runs' / 'session123'
        self.helper = self.root / 'helper.exe'
        self.helper.write_bytes(b'not executed; subprocess is replaced by a bounded test decoder')
        self.source = png(100, 100)
        self.region = {'x': 20, 'y': 30, 'width': 16, 'height': 16}
        with mock.patch.object(image_pixels, '_native_crop', return_value=None):
            self.expected = crop_png(self.source, self.region)
        self.calls = []

    def fake_decode(self, argv, **kwargs):
        request, response = Path(argv[2]), Path(argv[3])
        self.calls.append((request, response))
        self.assertEqual(request.parent.parent, self.run_dir / 'image-crops')
        self.assertRegex(request.parent.name, r'^crop-[a-z0-9_]{8}$')
        self.assertEqual(request.name, 'request.json')
        self.assertEqual(response.name, 'response.json')
        document = json.loads(request.read_text(encoding='utf-8'))
        self.assertEqual(document['screenshot_png'], self.source)
        self.assertEqual(document['regions'], [self.region])
        self.assertEqual(kwargs['timeout'], 4)
        response.write_text(json.dumps({'nonce': document['nonce'], 'status': 'cropped',
                                        'crops': [self.expected]}), encoding='utf-8')
        return SimpleNamespace(returncode=0)

    def run_crop(self, **kwargs):
        return image_pixels._native_crop(self.source, [self.region], helper=self.helper, **kwargs)

    def assert_clean(self):
        self.assertEqual(list((self.run_dir / 'image-crops').iterdir()), [])
        for request, response in self.calls:
            self.assertFalse(request.exists())
            self.assertFalse(response.exists())
            self.assertFalse(request.parent.exists())

    def test_session_capture_normal_completion_removes_request_and_response(self):
        with mock.patch.object(image_pixels.subprocess, 'run', side_effect=self.fake_decode):
            result = self.run_crop(runtime=SimpleNamespace(run_dir=self.run_dir))
        self.assertEqual(result, [self.expected])
        self.assert_clean()

    def test_explicit_run_directory_uses_same_cleanup(self):
        with mock.patch.object(image_pixels.subprocess, 'run', side_effect=self.fake_decode):
            self.assertEqual(self.run_crop(run_dir=self.run_dir), [self.expected])
        self.assert_clean()

    def test_timeout_and_invalid_response_remove_screenshot_files(self):
        def failure(argv, **kwargs):
            self.fake_decode(argv, **kwargs)
            if failure.kind == 'timeout':
                raise subprocess.TimeoutExpired(argv, 4)
            Path(argv[3]).write_text('{invalid response', encoding='utf-8')
            return SimpleNamespace(returncode=0)
        for kind in ('timeout', 'invalid'):
            with self.subTest(kind=kind):
                failure.kind = kind
                with mock.patch.object(image_pixels.subprocess, 'run', side_effect=failure):
                    with self.assertRaises(OperationError):
                        self.run_crop(run_dir=self.run_dir)
                self.assert_clean()

    def test_relative_parent_and_stream_paths_are_rejected_before_launch(self):
        for path in (Path('relative'), self.run_dir / '..' / 'escape',
                     self.run_dir / 'other:stream', Path(r'\\server\share\run')):
            with self.subTest(path=str(path)), mock.patch.object(image_pixels.subprocess, 'run') as launch:
                with self.assertRaises(OperationError) as raised:
                    self.run_crop(run_dir=path)
                self.assertEqual(raised.exception.code, 'unsafe_store')
                launch.assert_not_called()

    def test_parent_reparse_point_is_rejected_before_launch(self):
        self.run_dir.mkdir(parents=True)
        original = Path.lstat

        def reparse(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == self.run_dir.parent:
                return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink,
                    st_file_attributes=getattr(info, 'st_file_attributes', 0) | 0x400)
            return info

        with mock.patch.object(Path, 'lstat', reparse), mock.patch.object(image_pixels.subprocess, 'run') as launch:
            with self.assertRaises(OperationError) as raised:
                self.run_crop(run_dir=self.run_dir)
            self.assertEqual(raised.exception.code, 'unsafe_store')
            launch.assert_not_called()
        self.assertFalse((self.run_dir / 'image-crops').exists())

    def test_response_hardlink_is_not_read_and_original_is_preserved(self):
        original = self.root / 'do-not-touch.json'
        original.write_text('unrelated data', encoding='utf-8')

        def linked_response(argv, **kwargs):
            self.calls.append((Path(argv[2]), Path(argv[3])))
            os.link(original, argv[3])
            return SimpleNamespace(returncode=0)

        with mock.patch.object(image_pixels.subprocess, 'run', side_effect=linked_response):
            with self.assertRaises(OperationError) as raised:
                self.run_crop(run_dir=self.run_dir)
            self.assertEqual(raised.exception.code, 'unsafe_store')
        self.assertEqual(original.read_text(encoding='utf-8'), 'unrelated data')
        self.assert_clean()

    def test_helper_hardlink_is_rejected_before_launch(self):
        os.link(self.helper, self.root / 'helper-alias.exe')
        with mock.patch.object(image_pixels.subprocess, 'run') as launch:
            with self.assertRaises(OperationError) as raised:
                self.run_crop(run_dir=self.run_dir)
            self.assertEqual(raised.exception.code, 'unsafe_store')
            launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
