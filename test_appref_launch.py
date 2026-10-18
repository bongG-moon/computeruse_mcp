"""ClickOnce launch references: read-only discovery and explicit EXE binding.

All dispatch is mocked. These tests never install or open a ClickOnce app.
"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import programs
from program_launch import appref_launch_profile, create_program, inspect_appref, validate_launch_profile
import test_program_registration as registration_fixture


REFERENCE = ('https://example.test/apps/Business.application?menu=test#'
             'Business.application, Culture=neutral, PublicKeyToken=0123456789abcdef, processorArchitecture=msil')


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.reference = self.root / 'Business.appref-ms'
        self.reference.write_text(REFERENCE, encoding='utf-16')

    def test_unicode_reference_is_data_with_no_network_or_process(self):
        with patch('subprocess.Popen') as process:
            value = inspect_appref(str(self.reference))
        process.assert_not_called()
        self.assertEqual(value['name'], 'Business')
        self.assertEqual(value['deployment_address'], REFERENCE.split('#')[0])
        self.assertFalse(value['network_accessed'])
        self.assertFalse(value['application_launched'])
        self.assertFalse(value['handler_is_control_target'])

    def test_supported_text_encodings_and_local_offline_deployment(self):
        for encoding in ('utf-16', 'utf-16-le', 'utf-8-sig'):
            with self.subTest(encoding=encoding):
                self.reference.write_text(REFERENCE, encoding=encoding)
                self.assertEqual(inspect_appref(str(self.reference))['name'], 'Business')
        local = REFERENCE.replace('https://example.test/apps/Business.application?menu=test',
                                  'file:///C:/Local%20Fixture/Business.application')
        self.reference.write_text(local, encoding='utf-16')
        self.assertTrue(inspect_appref(str(self.reference))['deployment_address'].startswith('file:///'))

    def test_mage_app_identity_is_supported_independent_of_reference_filename(self):
        self.reference.write_text(REFERENCE.replace('#Business.application,', '#Some Other Application.app,'), encoding='utf-16')
        info = inspect_appref(str(self.reference))
        self.assertEqual(info['name'], 'Some Other Application')
        self.assertFalse(info['handler_is_control_target'])

    def test_reference_is_not_an_executable_and_dispatches_once(self):
        profile = appref_launch_profile(str(self.reference))
        launch, process = Mock(), Mock()
        result = create_program({'exe': str(self.root / 'ActualWindow.exe'), 'launch': profile},
                                process, uri_launcher=launch)
        launch.assert_called_once_with(str(self.reference), 'open')
        process.assert_not_called()
        self.assertIsNone(result.pid)
        self.assertEqual(result.launch_kind, 'appref')

    def test_changed_deleted_or_replaced_reference_never_dispatches(self):
        profile = appref_launch_profile(str(self.reference))
        for content in (REFERENCE.replace('menu=test', 'menu=changed'), 'MZ not a ClickOnce reference'):
            self.reference.write_text(content, encoding='utf-16')
            launch = Mock()
            with self.assertRaises(ValueError):
                create_program({'launch': profile}, uri_launcher=launch)
            launch.assert_not_called()
        self.reference.unlink()
        with self.assertRaises((ValueError, OSError)):
            create_program({'launch': profile}, uri_launcher=launch)
        launch.assert_not_called()

    def test_launch_failure_never_retries_or_runs_control_executable(self):
        launch, process = Mock(side_effect=OSError('association unavailable')), Mock()
        with self.assertRaises(OSError):
            create_program({'exe': 'must-not-run.exe', 'launch': appref_launch_profile(str(self.reference))},
                           process, uri_launcher=launch)
        self.assertEqual(launch.call_count, 1)
        process.assert_not_called()

    def test_config_validation_is_portable_but_launch_requires_the_file(self):
        profile = appref_launch_profile(str(self.reference))
        self.reference.unlink()
        self.assertEqual(validate_launch_profile(profile), profile)
        with self.assertRaises((OSError, ValueError)):
            create_program({'launch': profile}, uri_launcher=Mock())

    def test_malformed_or_command_references_rejected(self):
        for value in ('MZ executable', 'cmd /c calc', REFERENCE + '\nanything',
                      REFERENCE.replace('https://example.test', 'shell://anything'),
                      REFERENCE.replace('Business.application?', 'Business.exe?'),
                      REFERENCE.replace('https://example.test/apps/Business.application?menu=test',
                                        'file://remote/share/Business.application'),
                      REFERENCE.replace('0123456789abcdef', 'invalid'),
                      REFERENCE + ', PublicKeyToken=0123456789abcdef',
                      REFERENCE.replace('https://example.test', 'https://user:password@example.test')):
            with self.subTest(value=value):
                self.reference.write_text(value, encoding='utf-16')
                with self.assertRaises(ValueError): inspect_appref(str(self.reference))
        self.reference.write_bytes(b'A' * 65537)
        with self.assertRaises(ValueError): inspect_appref(str(self.reference))

    def test_wrong_extension_relative_and_unc_paths_rejected(self):
        for value in ('Business.appref-ms', r'\\server\share\Business.appref-ms',
                      str(self.reference.with_suffix('.exe')), str(self.reference) + ':stream'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError): inspect_appref(value)

    def test_hardlink_reference_rejected(self):
        alias = self.root / 'Alias.appref-ms'
        try:
            alias.hardlink_to(self.reference)
        except PermissionError:
            self.skipTest('This Windows token cannot create hard links')
        with self.assertRaises(ValueError): inspect_appref(str(alias))

    def test_linked_path_rejected_before_read(self):
        with patch('consent._plain_chain', return_value=False), patch.object(Path, 'open') as read:
            with self.assertRaises(ValueError): inspect_appref(str(self.reference))
        read.assert_not_called()


class ApprefRegistrationTests(unittest.TestCase):
    write = registration_fixture.RegistrationTests.write
    call = registration_fixture.RegistrationTests.call
    row = registration_fixture.RegistrationTests.row

    def setUp(self):
        registration_fixture.RegistrationTests.setUp(self)
        self.reference = self.root / 'Business.appref-ms'
        self.reference.write_text(REFERENCE, encoding='utf-16')
        self.manager.registration.candidate_source = lambda: [self.row()]

    def test_reference_alone_returns_clear_candidates_and_does_not_write(self):
        before = self.config_path.read_bytes()
        with patch('subprocess.Popen') as process:
            answer = self.call(launch_appref=str(self.reference))
        process.assert_not_called()
        self.assertEqual(answer['status'], 'choose_program_window')
        self.assertEqual(answer['reference']['name'], 'Business')
        self.assertFalse(answer['saved'])
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(answer['next_arguments']['launch_appref'], str(self.reference))

    def test_selected_existing_business_window_is_the_only_control_binding(self):
        answer = self.call(launch_appref=str(self.reference))
        saved = self.call(launch_appref=str(self.reference), candidate_id=answer['candidates'][0]['candidate_id'])
        self.assertEqual(saved['status'], 'registered')
        self.assertEqual(saved['program']['exe'], str(self.app))
        self.assertEqual(saved['program']['launch'], appref_launch_profile(str(self.reference)))
        self.assertEqual(saved['program']['control_exes'], [])
        self.assertFalse(saved['reconnect_required'])
        self.assertEqual(self.call(launch_appref=str(self.reference))['status'], 'already_present')

    def test_candidate_stale_is_not_rebound_by_same_name(self):
        answer = self.call(launch_appref=str(self.reference))
        self.manager.registration.candidate_source = lambda: [self.row(creation_time=31)]
        result = self.call(launch_appref=str(self.reference), candidate_id=answer['candidates'][0]['candidate_id'])
        self.assertEqual(result['diagnostic']['code'], 'candidate_stale')
        self.assertEqual(len(json.loads(self.config_path.read_text())['programs']), 1)

    def test_incompatible_launch_options_rejected_without_config_changes(self):
        before = self.config_path.read_bytes()
        for extra in ({'launch_uri': 'https://example.test/Other.application'}, {'arguments': ['--x']},
                      {'working_directory': str(self.root)}, {'builtin': 'notepad'}):
            result = self.call(exe=str(self.app), launch_appref=str(self.reference), **extra)
            self.assertEqual(result['diagnostic']['code'], 'invalid_launch')
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_cli_preview_requires_separate_executable_and_pins_reference(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = programs.main(['preview-add', '--config', str(self.config_path), '--exe', str(self.app),
                                    '--name', 'ClickOnce test', '--launch-appref', str(self.reference)])
        self.assertEqual(result, 0)
        value = json.loads(output.getvalue())
        self.assertEqual(value['program']['launch'], appref_launch_profile(str(self.reference)))
        self.assertEqual(value['program']['exe'], str(self.app))


if __name__ == '__main__':
    unittest.main()
