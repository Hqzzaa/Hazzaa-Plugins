import io
import os
import struct
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from queue import Queue
from unittest.mock import patch

from ae_archive_deployer import ArchiveDeployer, extract_archive, _safe_destination
from ae_paths import discover, normalize_target
from deployment_plan import validate_plugin, plugin_plan, script_plan, deploy_files, Copy
from installer_runner import run_installer
from product_deployment import complete_bcc12, BCC_LIBRARIES
from test_support import plugin_bytes
from ae_archive_deployer import install_binary
import runtime_dependencies as runtime
from splash_customizer import apply_splash_image, restore_splash_backup
from ae_organization import copy_preset_file
from PIL import Image


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        registry=patch('product_deployment.bcc_registry_candidates',return_value=[])
        registry.start();self.addCleanup(registry.stop)
        self.environment = patch.dict(os.environ, {'LOCALAPPDATA':str(self.root/'local'), 'APPDATA':str(self.root/'roaming')})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def plugin(self,name='Effect.aex',payload=b'fixture'):
        path=self.root/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(plugin_bytes(payload))
        return path

    def test_invalid_and_truncated_and_32bit_plugins(self):
        good=self.plugin()
        validate_plugin(good)
        variants=[b'not executable',plugin_bytes(machine=0x14c),plugin_bytes()[:100],plugin_bytes()[:700]]
        missing=bytearray(plugin_bytes());struct.pack_into('<II',missing,152+112+16,0,0);variants.append(bytes(missing))
        for data in variants:
            with self.subTest(length=len(data)):
                good.write_bytes(data)
                self.assertRaises(ValueError,validate_plugin,good)

    def test_discovery_uses_registry_custom_drive_and_rejects_stale_backups(self):
        for year in (2019,2021,2026):
            host=self.root/f'Other Disk/Applications/AE {year}/Support Files'
            host.mkdir(parents=True);(host/'AfterFX.exe').touch()
        candidates=[(str(y),self.root/f'Other Disk/Applications/AE {y}') for y in (2019,2021,2026)]
        candidates.append(('2025',self.root/'deleted host'))
        found=discover(self.root/'empty',registry=candidates,extra_roots=[])
        self.assertEqual([year for year,_ in found],['2026','2021','2019'])
        for _,path in found:self.assertTrue((path.parent/'AfterFX.exe').is_file())
        fake=self.root/'Adobe/Adobe After Effects 2024/Support Files';fake.mkdir(parents=True)
        self.assertEqual(discover(fake.parent.parent,registry=[],extra_roots=[]),[])

    def test_relative_target_rejected_and_host_root_normalized(self):
        self.assertRaises(ValueError,normalize_target,Path('relative/path'))
        support=self.root/'Adobe After Effects 2021/Support Files';support.mkdir(parents=True)
        (support/'AfterFX.exe').touch()
        for selected in (support.parent,support,support/'AfterFX.exe',support/'Plug-ins'):
            self.assertEqual(normalize_target(selected),support/'Plug-ins')

    def test_conflicting_archive_versions_fail_before_any_deployment(self):
        first=self.plugin('one/Effect.aex','first')
        second=self.plugin('two/Effect.aex','different')
        self.assertRaisesRegex(ValueError,'Ambiguous archive',plugin_plan,[first,second],self.root/'target')
        self.assertFalse((self.root/'target').exists())

    def test_companion_directory_is_merged_without_renaming(self):
        archive=self.root/'bundle.zip'
        with zipfile.ZipFile(archive,'w') as out:
            out.writestr('Product/Effect.aex',plugin_bytes())
            out.writestr('Product/Textures/preset.json','{}')
            out.writestr('Product/SDK.dll','SDK')
        app=object.__new__(ArchiveDeployer);app.events=Queue()
        target=self.root/'host/Support Files/Plug-ins'
        for _ in range(3):app._process_worker([archive],target,True,script_registry=False)
        self.assertEqual(len(list(target.rglob('*.aex'))),1)
        self.assertEqual(len(list(target.rglob('preset.json'))),1)
        self.assertFalse(any('(2)' in p.name for p in target.rglob('*')))
        self.assertFalse(any(value.get('failures') for kind,value in app.events.queue if kind=='done'))

    def test_auto_sway_panel_keeps_its_ffx_assets(self):
        script=self.root/'AutoSway.jsxbin';script.write_bytes(b'compiled script fixture')
        assets=self.root/'AutoSway_ffx';assets.mkdir();(assets/'Swing.ffx').write_bytes(b'preset')
        plan=script_plan([script],[],self.root/'Scripts')
        self.assertEqual({str(p.destination.relative_to(self.root/'Scripts')) for p in plan},
                         {'ScriptUI Panels\\AutoSway.jsxbin','ScriptUI Panels\\AutoSway_ffx\\Swing.ffx'})

    def test_legacy_duplicate_and_flat_signal_are_backed_up_outside_host(self):
        target=self.root/'Plug-ins';target.mkdir()
        (target/'signal.aex').write_bytes(plugin_bytes('old'))
        (target/'signal (2).aex').write_bytes(plugin_bytes('duplicate'))
        archive=self.root/'signal.zip'
        with zipfile.ZipFile(archive,'w') as out:out.writestr('signal.aex',plugin_bytes('new'))
        app=object.__new__(ArchiveDeployer);app.events=Queue()
        app._process_worker([archive],target,True,script_registry=False)
        self.assertEqual((target/'Effects/signal.aex').read_bytes(),plugin_bytes('new'))
        self.assertFalse((target/'signal.aex').exists())
        self.assertFalse((target/'signal (2).aex').exists())
        self.assertEqual(len(list((self.root/'local/Hazzaa Plugins/Backups/Duplicates').glob('*.backup'))),2)

    def test_zip_links_and_windows_aliases_are_rejected(self):
        for name in ['CON','nul.txt','com1.aex','folder./Effect.aex','folder /Effect.aex','A:stream','a?.aex']:
            self.assertRaises(ValueError,_safe_destination,self.root,name)
        archive=self.root/'link.zip'
        with zipfile.ZipFile(archive,'w') as out:
            info=zipfile.ZipInfo('symlink');info.external_attr=0o120777 << 16
            out.writestr(info,'../outside')
        warnings=extract_archive(archive,self.root/'out')
        self.assertTrue(warnings);self.assertFalse((self.root/'out/symlink').exists())

    def test_zip_case_alias_cannot_choose_which_plugin_wins(self):
        archive=self.root/'collision.zip'
        with zipfile.ZipFile(archive,'w') as out:
            out.writestr('Effect.aex',plugin_bytes('first'))
            out.writestr('effect.aex',plugin_bytes('second'))
        self.assertRaisesRegex(ValueError,'colliding Windows',extract_archive,archive,self.root/'out')

    def test_copy_failure_restores_the_previous_sdk_and_plugin(self):
        sdk=self.root/'archive/SDK.dll';sdk.parent.mkdir();sdk.write_bytes(b'new SDK')
        plugin=self.plugin('archive/Effect.aex','new effect')
        target=self.root/'target';target.mkdir()
        (target/'SDK.dll').write_bytes(b'old SDK')
        (target/'Effect.aex').write_bytes(plugin_bytes('old effect'))
        def failing_copy(source,destination):
            if source==plugin:raise PermissionError('Fixture: AEX is locked')
            return install_binary(source,destination)
        plan=[Copy(sdk,target/'SDK.dll'),Copy(plugin,target/'Effect.aex')]
        self.assertRaisesRegex(OSError,'previous loose files were restored',deploy_files,plan,failing_copy)
        self.assertEqual((target/'SDK.dll').read_bytes(),b'old SDK')
        self.assertEqual((target/'Effect.aex').read_bytes(),plugin_bytes('old effect'))
        self.assertFalse(list(target.glob('*.hazzaa-pending')))

    def test_readonly_archive_files_do_not_make_installed_plugins_readonly(self):
        source=self.plugin('archive/AEPixelSorter2.aex')
        source.chmod(0o444)
        target=self.root/'Plug-ins/AEPixelSorter2.aex'
        install_binary(source,target)
        self.assertTrue(target.stat().st_mode & stat.S_IWRITE)
        self.assertFalse(source.stat().st_mode & stat.S_IWRITE)
        target.chmod(0o444)
        deploy_files([Copy(source,target)],install_binary)
        self.assertTrue(target.stat().st_mode & stat.S_IWRITE)

    def test_readonly_previous_binaries_and_existing_backups_can_be_reinstalled(self):
        source=self.plugin('archive/Effect.aex','new')
        target=self.root/'Plug-ins/Effect.aex';target.parent.mkdir()
        for _ in range(2):
            target.write_bytes(plugin_bytes('old'));target.chmod(0o444)
            install_binary(source,target)
            self.assertEqual(target.read_bytes(),plugin_bytes('new'))
        self.assertEqual(len(list((self.root/'local').rglob('*.backup'))),1)

    def test_readonly_source_preset_creates_an_editable_user_copy(self):
        source=self.root/'source.ffx';source.write_bytes(b'preset');source.chmod(0o444)
        destination=self.root/'User Presets/example.ffx'
        self.assertTrue(copy_preset_file(source,destination))
        self.assertTrue(destination.stat().st_mode & stat.S_IWRITE)
        self.assertFalse(source.stat().st_mode & stat.S_IWRITE)

    def test_readonly_splash_dll_and_backup_can_be_updated_and_restored(self):
        dll=self.root/'AfterFXLib.dll';dll.write_bytes(b'original DLL fixture');dll.chmod(0o444)
        resource=[{'name':'AE_SPLASH','language':1033,'data':b'fixture','size':(8,8)}]
        def write(path,*_args):
            self.assertTrue(path.stat().st_mode & stat.S_IWRITE)
            path.write_bytes(b'updated DLL fixture')
            return 1
        with patch('splash_customizer._afterfx_is_running',return_value=False),patch('splash_customizer.read_splash_resources',return_value=resource),patch('splash_customizer._write_resources',side_effect=write):
            backup,count=apply_splash_image(dll,Image.new('RGB',(8,8)))
            self.assertEqual(count,1);self.assertEqual(backup.read_bytes(),b'original DLL fixture')
            self.assertEqual(dll.read_bytes(),b'updated DLL fixture')
            dll.chmod(0o444)
            restore_splash_backup(dll)
            self.assertEqual(dll.read_bytes(),b'original DLL fixture')
            self.assertTrue(dll.stat().st_mode & stat.S_IWRITE)

    def test_runtime_detection_does_not_execute_plugin_code(self):
        system=self.root/'System32';system.mkdir()
        with patch.object(runtime,'imports',return_value={'vcruntime140.dll','vulkan-1.dll','kernel32.dll'}):
            self.assertEqual(runtime.missing_runtime_files([self.root/'fixture.aex'],system),{'vcruntime140.dll','vulkan-1.dll'})
            (system/'vcruntime140.dll').touch();(system/'vulkan-1.dll').touch()
            self.assertEqual(runtime.missing_runtime_files([self.root/'fixture.aex'],system),set())

    def test_prerequisite_download_is_not_run_if_its_signature_fails(self):
        response=io.BytesIO(b'unsigned binary fixture')
        with patch.object(runtime.urllib.request,'urlopen',return_value=response),patch.object(runtime,'microsoft_signed',return_value=False),patch.object(runtime,'run_installer') as run:
            self.assertRaisesRegex(ValueError,'valid Microsoft signature',runtime.download_vc14,self.root/'download.exe')
            run.assert_not_called()

    def test_missing_vc_runtime_is_installed_and_checked_again(self):
        with patch.object(runtime,'missing_runtime_files',side_effect=[{'msvcp140.dll'},set()]),patch.object(runtime,'download_vc14') as download,patch.object(runtime,'run_installer',return_value=0) as run:
            runtime.ensure_prerequisites([self.root/'effect.aex'],lambda _:None)
            download.assert_called_once()
            self.assertIn('/quiet',run.call_args.args[0])
            self.assertIn('/norestart',run.call_args.args[0])

    def test_missing_gpu_runtime_is_not_reported_as_success(self):
        with patch.object(runtime,'missing_runtime_files',return_value={'vulkan-1.dll'}),patch.object(runtime,'download_vc14') as download:
            self.assertRaisesRegex(OSError,'GPU driver',runtime.ensure_prerequisites,[self.root/'effect.aex'],lambda _:None)
            download.assert_not_called()

    def test_explicit_registry_plugin_directory_is_preserved(self):
        support=self.root/'Custom Adobe/Support Files';support.mkdir(parents=True);(support/'AfterFX.exe').touch()
        explicit=self.root/'Other Drive/AE Extensions'
        found=discover(self.root/'not-default',registry=[('2021',explicit,support)],extra_roots=[])
        self.assertEqual(found,[('2021',explicit)])

    def test_bcc_never_replaces_vendor_libraries_from_an_archive_patch_folder(self):
        base=self.root/'Program Files';vendor=base/'BorisFX/ContinuumAE/12'
        effect=base/'Adobe/Common/Plug-ins/7.0/MediaCore/Continuum Plug-ins/BCC.aex'
        effect.parent.mkdir(parents=True);effect.write_bytes(plugin_bytes())
        archive=self.root/'archive/Crack';archive.mkdir(parents=True)
        for name in BCC_LIBRARIES:
            (archive/name).write_bytes(b'archive replacement')
            actual=vendor/'lib'/name;actual.parent.mkdir(parents=True,exist_ok=True);actual.write_bytes(b'vendor file')
        with patch.dict(os.environ,{'ProgramW6432':str(base)}),patch('ae_archive_deployer.install_binary') as copy:
            complete_bcc12(archive.parent,Path('Continuum_2019_Adobe_12_0_0_Windows.exe'),copy)
            copy.assert_not_called()
        for name in BCC_LIBRARIES:self.assertEqual((vendor/'lib'/name).read_bytes(),b'vendor file')

    @unittest.skipUnless(os.name=='nt','Windows job process accounting')
    def test_native_bootstrapper_exit_does_not_end_child_install(self):
        output=self.root/'child-finished.txt'
        child=f"import time,pathlib;time.sleep(.8);pathlib.Path({str(output)!r}).write_text('done')"
        parent=f'import subprocess,sys;subprocess.Popen([sys.executable,"-c",{child!r}])'
        code=run_installer([sys.executable,'-c',parent],self.root,silent=True,credentials={},log=lambda _:None,timeout=8)
        self.assertEqual(code,0);self.assertEqual(output.read_text(),'done')

    @unittest.skipUnless(os.name=='nt','Windows job process accounting')
    def test_native_failing_setup_child_is_not_success(self):
        parent='import subprocess,sys;subprocess.Popen([sys.executable,"-c","import time;time.sleep(.7);raise SystemExit(7)"])'
        messages=[]
        code=run_installer([sys.executable,'-c',parent],self.root,silent=True,credentials={},log=messages.append,timeout=8)
        self.assertEqual(code,7);self.assertTrue(any('code 7' in message for message in messages))

    def test_setup_errors_are_captured_without_exposing_registration_values(self):
        messages=[]
        command=[sys.executable,'-c',"import sys;print('must accept EULA DEMO-SECRET-123');sys.exit(2)"]
        code=run_installer(command,self.root,silent=True,credentials={'serial':'DEMO-SECRET-123'},log=messages.append,timeout=8)
        self.assertEqual(code,2);self.assertIn('must accept EULA',' '.join(messages))
        self.assertNotIn('DEMO-SECRET-123',' '.join(messages))

    def test_setup_timeout_is_failure_and_does_not_wait_forever(self):
        started=time.monotonic()
        with self.assertRaises(TimeoutError):
            run_installer([sys.executable,'-c','import time;time.sleep(20)'],self.root,
                          silent=True,credentials={},log=lambda _:None,timeout=.3)
        self.assertLess(time.monotonic()-started,4)


if __name__=='__main__':unittest.main()
