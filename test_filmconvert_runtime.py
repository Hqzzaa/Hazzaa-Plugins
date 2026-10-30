import os
from pathlib import Path
import shutil
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from queue import Queue

from ae_archive_deployer import ArchiveDeployer
from filmconvert_runtime import (INSTALLER, UTILITIES, complete_runtime,
    find_installation, needs_repair, read_qt_manifest, runtime_plan, runtime_destination)


def manifest(paths):
    encoded = [('@RELOCATABLE_PATH@/' + name).encode('utf-16-be') for name in paths]
    return struct.pack('>I', len(paths)) + b''.join(struct.pack('>I', len(item)) + item for item in encoded)


class FilmConvertRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vendor = self.root / 'Vendor/PremierePro'
        self.data = self.root / 'Data/RubberMonkey'
        self.metadata = self.vendor / 'installerResources/com.filmconvert.resources'
        self.metadata.mkdir(parents=True)
        packages = ''.join('<Package><Name>' + name + '</Name><Version>3.56</Version></Package>'
                           for name in ['com.filmconvert.plugin', 'com.filmconvert.resources'])
        (self.vendor / 'components.xml').write_text('<Packages>' + packages + '</Packages>')
        self.groups = {'Profiles': ['Profiles', 'Profiles/5D', 'Profiles/5D/5D_5207.tin'],
                       'ResourcesNITRATE': ['ResourcesNITRATE', 'ResourcesNITRATE/fcl.bin',
                                           'ResourcesNITRATE/fcCamList_v085.xml']}
        for group, paths in self.groups.items():
            (self.metadata / ('3.56' + group + '.txt')).write_bytes(manifest(paths))
            for relative in paths:
                path = self.vendor / relative
                if path.suffix:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(('data:' + relative).encode())
                else:
                    path.mkdir(parents=True, exist_ok=True)
        (self.metadata / '3.56content.txt').write_bytes(manifest(sorted(UTILITIES | {'post_install.bat'})))
        for name in UTILITIES | {'post_install.bat'}:
            (self.vendor / name).write_bytes(('vendor:' + name).encode())

    def complete(self, copier=shutil.copyfile):
        def copy(source, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            return copier(source, target)
        return complete_runtime(Path(INSTALLER), copy, program_root=self.vendor, data_root=self.data)

    def test_actual_qt_big_endian_manifest_format(self):
        self.assertEqual(read_qt_manifest(self.metadata / '3.56Profiles.txt'), self.groups['Profiles'])

    def test_repeat_auto_install_repairs_existing_version_without_any_setup_process(self):
        from test_support import plugin_bytes
        shared = self.root / 'Custom Adobe/MediaCore'
        shared.mkdir(parents=True)
        (shared / 'FilmConvertNitrate.aex').write_bytes(plugin_bytes())
        installer = self.root / INSTALLER
        installer.write_bytes(b'not launched')
        app = object.__new__(ArchiveDeployer)
        app.events = Queue()
        _, run, _ = app._prepare_setup_package([installer], self.root, shared,
            [], None, Mock(), True, 'Nitrate')
        with patch('filmconvert_runtime._candidates', return_value=[self.vendor]), \
             patch.dict(os.environ, {'ProgramData': str(self.data.parent)}), \
             patch('ae_archive_deployer.execute_setup') as execute, \
             patch('ae_archive_deployer.run_installer') as runner:
            for attempt in range(3):
                outcome, evidence = run()
                self.assertEqual(int(outcome), 0)
                self.assertEqual(evidence['unverified'], 0)
            execute.assert_not_called()
            runner.assert_not_called()
        self.assertTrue((self.data / 'ResourcesNITRATE/fcl.bin').is_file())
        self.assertFalse(any(kind == 'setup_help' for kind, _ in app.events.queue))

    def test_existing_version_detection_requires_valid_host_and_reviewed_version(self):
        from filmconvert_runtime import reusable_installation
        from test_support import plugin_bytes
        shared = self.root / 'Shared'
        shared.mkdir()
        host = shared / 'FilmConvertNitrate.aex'
        with patch('filmconvert_runtime._candidates', return_value=[self.vendor]):
            self.assertFalse(reusable_installation(Path(INSTALLER), shared))
            host.write_bytes(b'incomplete')
            self.assertFalse(reusable_installation(Path(INSTALLER), shared))
            host.write_bytes(plugin_bytes())
            self.assertTrue(reusable_installation(Path(INSTALLER), shared))
            self.assertFalse(reusable_installation(Path('different.exe'), shared))
            (self.vendor / 'components.xml').write_text('<Packages/>')
            self.assertFalse(reusable_installation(Path(INSTALLER), shared))

    def test_repeat_install_repair_error_never_opens_another_wizard(self):
        app = object.__new__(ArchiveDeployer)
        app.events = Queue()
        installer = self.root / INSTALLER
        installer.write_bytes(b'fixture')
        _, run, _ = app._prepare_setup_package([installer], self.root, self.root / 'Shared',
            [], None, Mock(), True, 'Nitrate')
        with patch('ae_archive_deployer.reusable_filmconvert_installation', return_value=True), \
             patch('ae_archive_deployer.complete_filmconvert_runtime', side_effect=PermissionError('Denied')), \
             patch('ae_archive_deployer.execute_setup') as execute:
            with self.assertRaises(PermissionError):
                run()
            execute.assert_not_called()

    def test_unattended_install_finalizes_profiles_resources_and_utilities(self):
        self.assertTrue(needs_repair(self.vendor, self.data))
        paths = self.complete()
        self.assertEqual(len(paths), 6)
        self.assertFalse(needs_repair(self.vendor, self.data))
        self.assertTrue((self.vendor / 'ResourcesNITRATE/fcl.bin').is_file())
        self.assertFalse((self.data / 'post_install.bat').exists())

    def test_license_data_is_never_changed_or_deployed(self):
        license_file = self.data / 'Licenses/owned.fkv'
        license_file.parent.mkdir(parents=True)
        license_file.write_bytes(b'private local fixture')
        (self.vendor / 'license.key').write_bytes(b'not runtime data')
        with patch('subprocess.Popen') as launch:
            self.complete()
        launch.assert_not_called()
        self.assertEqual(license_file.read_bytes(), b'private local fixture')
        self.assertFalse((self.data / 'license.key').exists())

    def test_idempotent_and_official_moved_source_files_are_supported(self):
        self.complete()
        for path in self.vendor.rglob('*'):
            if path.is_file() and ('installerResources' not in path.parts) and path.name != 'components.xml':
                path.unlink()
        self.assertFalse(needs_repair(self.vendor, self.data))
        self.assertEqual(len(self.complete()), 6)

    def test_missing_required_file_fails_before_any_destination_write(self):
        (self.vendor / 'ResourcesNITRATE/fcl.bin').unlink()
        with self.assertRaisesRegex(OSError, 'runtime file is missing'):
            self.complete()
        self.assertFalse(self.data.exists())

    def test_missing_utility_fails_before_any_destination_write(self):
        (self.vendor / 'FCUtilities_v3.exe').unlink()
        with self.assertRaisesRegex(OSError, 'support utility is missing'):
            self.complete()
        self.assertFalse(self.data.exists())

    def test_corrupt_existing_resource_is_repaired(self):
        self.complete()
        file = self.data / 'ResourcesNITRATE/fcl.bin'
        file.write_bytes(b'broken')
        self.assertTrue(needs_repair(self.vendor, self.data))
        self.complete()
        self.assertEqual(file.read_bytes(), (self.vendor / 'ResourcesNITRATE/fcl.bin').read_bytes())

    def test_copy_failure_rolls_back_existing_data(self):
        self.complete()
        original = {p: p.read_bytes() for p in self.data.rglob('*') if p.is_file()}
        (self.vendor / 'Profiles/5D/5D_5207.tin').write_bytes(b'updated profile fixture')
        (self.vendor / 'ResourcesNITRATE/fcl.bin').write_bytes(b'updated resource fixture')
        count = 0
        def failing(source, target):
            nonlocal count
            count += 1
            if count == 2:
                raise PermissionError('Locked destination')
            shutil.copyfile(source, target)
        with self.assertRaisesRegex(OSError, 'previous loose files were restored'):
            self.complete(failing)
        self.assertEqual({p: p.read_bytes() for p in self.data.rglob('*') if p.is_file()}, original)

    def test_other_product_installer_never_triggers_relocation(self):
        copy = Mock()
        self.assertEqual(complete_runtime(Path('Sapphire.exe'), copy, program_root=self.vendor, data_root=self.data), [])
        copy.assert_not_called()

    def test_unreviewed_versions_are_not_guessed(self):
        path = self.vendor / 'components.xml'
        path.write_text(path.read_text().replace('3.56', '9.99'))
        self.assertIsNone(find_installation(self.vendor))
        self.assertFalse(needs_repair(self.vendor, self.data))
        with self.assertRaises(OSError):
            self.complete()

    def test_unsafe_manifest_paths_and_unexpected_payload_are_rejected(self):
        paths = ['../escape.bin', '/absolute.bin', 'ResourcesNITRATE/../escape.bin',
                 'ResourcesNITRATE/C:evil.bin', 'ResourcesNITRATE\\evil.bin',
                 'ResourcesNITRATE/NUL.bin', 'ResourcesNITRATE/evil.bin ',
                 'ResourcesNITRATE/run.exe', 'Licenses/private.fkv', 'Profiles/other.dll']
        file = self.metadata / '3.56ResourcesNITRATE.txt'
        for name in paths:
            with self.subTest(name=name):
                file.write_bytes(manifest([name]))
                with self.assertRaises(ValueError):
                    self.complete()
                self.assertFalse(self.data.exists())

    def test_truncated_duplicate_oversized_manifests_fail(self):
        good = manifest(['ResourcesNITRATE/fcl.bin'])
        samples = [b'', good[:-1], good + b'x', struct.pack('>I', 999999),
                   manifest(['ResourcesNITRATE/fcl.bin'] * 2),
                   struct.pack('>II', 1, 3) + b'abc']
        file = self.metadata / '3.56ResourcesNITRATE.txt'
        for data in samples:
            with self.subTest(size=len(data)):
                file.write_bytes(data)
                with self.assertRaises(ValueError):
                    read_qt_manifest(file)

    def test_manual_completion_repairs_once_without_relaunching_setup(self):
        app = object.__new__(ArchiveDeployer)
        app.events = Queue()
        installer = self.root / 'Nitrate.PP-3.56b.exe'
        installer.write_bytes(b'Qt fixture')
        handoff = SimpleNamespace(handed_off=True, process=SimpleNamespace(returncode=0))
        key, run, finished = app._prepare_setup_package([installer], self.root, self.root / 'Shared',
            [], None, Mock(), True, 'FilmConvert Nitrate')
        with patch('ae_archive_deployer.execute_setup', return_value=(handoff, None)) as execute, \
             patch('ae_archive_deployer.complete_filmconvert_runtime', return_value=[]) as repair:
            outcome, _ = run()
            repair.assert_not_called()
            finished(outcome, None, None)
        execute.assert_called_once()
        repair.assert_called_once()

    def test_cancelled_manual_setup_is_not_finalized(self):
        app = object.__new__(ArchiveDeployer)
        app.events = Queue()
        installer = self.root / 'Nitrate.PP-3.56b.exe'
        installer.write_bytes(b'Qt fixture')
        _, _, finished = app._prepare_setup_package([installer], self.root, self.root / 'Shared',
            [], None, Mock(), True, 'Nitrate')
        with patch('ae_archive_deployer.complete_filmconvert_runtime') as repair:
            finished(SimpleNamespace(handed_off=True, process=SimpleNamespace(returncode=1)), None, None)
        repair.assert_not_called()

    def test_programdata_and_custom_vendor_location_are_resolved_per_machine(self):
        with patch.dict(os.environ, {'ProgramData': str(self.root / 'Different drive data')}), \
             patch('filmconvert_runtime._candidates', return_value=[self.vendor]):
            self.assertEqual(find_installation(), self.vendor)
            self.assertEqual(runtime_destination(), self.root / 'Different drive data/RubberMonkey')
            self.assertTrue(needs_repair())
            paths = complete_runtime(Path(INSTALLER), lambda a,b: (b.parent.mkdir(parents=True,exist_ok=True), shutil.copyfile(a,b)))
            self.assertTrue(all(p.is_relative_to(runtime_destination()) for p in paths))
            self.assertFalse(needs_repair())

    def test_missing_empty_runtime_directories_are_recreated_without_touching_license(self):
        self.complete()
        (self.data / 'CMProfiles').rmdir()
        self.assertTrue(needs_repair(self.vendor, self.data))
        self.complete()
        self.assertTrue((self.data / 'CMProfiles').is_dir())
        self.assertTrue((self.data / 'Licenses').is_dir())

    def test_host_aex_alone_never_passes_nitrate_postcondition(self):
        from product_deployment import verify_known_setup_outputs
        from test_support import plugin_bytes
        shared = self.root / 'Shared'
        shared.mkdir()
        (shared / 'FilmConvertNitrate.aex').write_bytes(plugin_bytes())
        with patch('filmconvert_runtime._candidates', return_value=[self.vendor]), \
             patch.dict(os.environ, {'ProgramData': str(self.root / 'MissingData')}):
            with self.assertRaisesRegex(OSError, 'support files are missing'):
                verify_known_setup_outputs(Path(INSTALLER), shared)

    def test_startup_repair_completes_through_ui_event_without_claiming_host_loading(self):
        app = object.__new__(ArchiveDeployer)
        app.events = Queue()
        app.busy = False
        app.setup_dispatcher = SimpleNamespace(pending_count=0)
        with patch('ae_archive_deployer.filmconvert_needs_repair', return_value=True), \
             patch('ae_archive_deployer.complete_filmconvert_runtime', return_value=[]) as repair, \
             patch('ae_archive_deployer.threading.Thread', side_effect=lambda **kw: SimpleNamespace(start=kw['target'])):
            app._repair_installed_support_files()
        repair.assert_called_once()
        self.assertIn(('support_runtime_done', (True, None)), list(app.events.queue))

    def test_manual_finalization_failure_is_logged_without_starting_another_wizard(self):
        app = object.__new__(ArchiveDeployer)
        app.events = Queue()
        installer = self.root / 'Nitrate.PP-3.56b.exe'
        installer.write_bytes(b'Qt fixture')
        _, _, finished = app._prepare_setup_package([installer], self.root, self.root / 'Shared',
            [], None, Mock(), True, 'Nitrate')
        with patch('ae_archive_deployer.complete_filmconvert_runtime', side_effect=PermissionError('Denied')), \
             patch('ae_archive_deployer.run_installer') as runner:
            finished(SimpleNamespace(handed_off=True, process=SimpleNamespace(returncode=0)), None, None)
        runner.assert_not_called()
        self.assertTrue(any(kind == 'setup_finished' and not value[1] for kind,value in app.events.queue))


if __name__ == '__main__':
    unittest.main()
