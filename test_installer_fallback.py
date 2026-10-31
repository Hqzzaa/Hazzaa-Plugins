import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from installer_automation import Credentials,credentials_for_installer,WindowsForms
from installer_runner import InstallerExit,run_installer
from installer_workflow import execute_setup,exit_explanation
from installer_help import help_values,preserve_bcc_attachments,setup_steps,ExtractionWorkspace
from ae_archive_deployer import ArchiveDeployer

ROOT=Path(__file__).resolve().parent


class InstallerFallbackTests(unittest.TestCase):
    def test_native_transient_status_is_not_handed_off_as_a_manual_wizard(self):
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'status.json';attention=Mock()
            result=run_installer([sys.executable,str(ROOT/'native_installer_fixture.py'),str(output),'--transient-status'],Path(temp),
                silent=True,credentials={},log=lambda _:None,on_attention=attention,handoff_on_attention=True,timeout=8)
            self.assertEqual(result,0);self.assertFalse(result.assisted)
            self.assertFalse(getattr(result,'handed_off',False));attention.assert_not_called()
    def test_continuum_2026_parent_success_with_helper_codes_is_verified_once(self):
        with tempfile.TemporaryDirectory() as temp:
            installer=Path(temp)/'BorisFX Continuum 2026.5 Adobe v19.5.6.exe'
            installer.write_bytes(b'Inno Setup')
            runner=Mock(return_value=InstallerExit(2,False,[2,2],parent_code=0))
            verify=Mock(return_value=['host.aex']);attention=Mock()
            outcome,evidence=execute_setup(installer,silent=True,credentials={},log=lambda _:None,
                attention=attention,verify=verify,runner=runner)
            self.assertEqual(outcome,0);self.assertEqual(outcome.parent_code,0)
            self.assertFalse(outcome.assisted);self.assertEqual(evidence,['host.aex'])
            runner.assert_called_once();verify.assert_called_once();attention.assert_not_called()

    def test_continuum_2026_errors_never_open_a_normal_installer(self):
        with tempfile.TemporaryDirectory() as temp:
            installer=Path(temp)/'BorisFX Continuum 2026.5 Adobe v19.5.6.exe'
            installer.write_bytes(b'Inno Setup')
            for result in [InstallerExit(2,False,[2],parent_code=2),
                           InstallerExit(1603,False,[2,1603],parent_code=0),1602,5]:
                with self.subTest(code=int(result)):
                    runner=Mock(return_value=result);attention=Mock();verify=Mock()
                    with self.assertRaises(RuntimeError):
                        execute_setup(installer,silent=True,credentials={},log=lambda _:None,
                            attention=attention,verify=verify,runner=runner)
                    runner.assert_called_once();attention.assert_not_called();verify.assert_not_called()

    def test_continuum_2026_missing_host_files_fail_without_a_manual_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            installer=Path(temp)/'BorisFX Continuum 2026.5 Adobe v19.5.6.exe'
            installer.write_bytes(b'Inno Setup')
            runner=Mock(return_value=InstallerExit(2,False,[2],parent_code=0))
            attention=Mock();verify=Mock(side_effect=OSError('Missing files'))
            with self.assertRaises(OSError):
                execute_setup(installer,silent=True,credentials={},log=lambda _:None,
                    attention=attention,verify=verify,runner=runner)
            runner.assert_called_once();attention.assert_not_called();verify.assert_called_once()

    def test_continuum_2026_has_no_legacy_copy_instructions_or_lib_buttons(self):
        name='BorisFX Continuum 2026.5 Adobe v19.5.6.exe'
        self.assertNotIn('OPEN LIB',setup_steps(name))
        self.assertNotIn('without entering a license',setup_steps(name))
        root=tk.Tk();self.addCleanup(root.destroy)
        with patch('ae_archive_deployer.ActivationStore') as store:
            store.return_value.records.return_value=[];app=ArchiveDeployer(root)
        from installer_support_ui import SetupHelpWindow
        window=SetupHelpWindow(app,{'name':name,'reason':'Manual installation selected','values':{}})
        self.assertEqual(window.lib_button.winfo_manager(),'')
        self.assertEqual(window.browse_button.winfo_manager(),'')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)
        self.environment=patch.dict(os.environ,{'LOCALAPPDATA':str(self.folder/'Local')})
        self.environment.start();self.addCleanup(self.environment.stop)

    def exe(self,name='Setup.exe',marker=b'Inno Setup'):
        path=self.folder/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(marker);return path

    def test_all_reviewed_setup_families_retry_once_without_silent_flags(self):
        names=['Continuum_2019_Adobe_12_0_0_Windows.exe','Magic_Bullet_Suite_13.0.11_Setup.exe',
               'Boris FX Sapphire AE 2020.02 CE.exe','RSMB5AE-5.1.8-windows-installer.exe',
               'BAO Plugins Bundle 2025.5.exe','Nitrate.PP-3.56b.exe','setup.exe','Install.msi']
        for name in names:
            for code in (2,5,1638,1618):
                with self.subTest(name=name,code=code):
                    path=self.exe(name);runner=Mock(side_effect=[code,0]);verify=Mock(return_value='verified');attention=Mock()
                    result,verified=execute_setup(path,silent=True,credentials=Credentials(),log=lambda _:None,
                        attention=attention,verify=verify,runner=runner)
                    self.assertEqual(result,0);self.assertTrue(result.assisted);self.assertEqual(verified,'verified')
                    self.assertEqual(runner.call_count,2)
                    self.assertFalse(runner.call_args.kwargs['silent'])
                    self.assertNotIn('/VERYSILENT',runner.call_args.args[0]);self.assertNotIn('/qn',runner.call_args.args[0])
                    verify.assert_called_once();attention.assert_called_once()

    def test_two_failures_do_not_loop_or_claim_success(self):
        runner=Mock(side_effect=[5,5]);verify=Mock()
        with self.assertRaises(RuntimeError):execute_setup(self.exe(),silent=True,credentials={},log=lambda _:None,attention=Mock(),verify=verify,runner=runner)
        self.assertEqual(runner.call_count,2);verify.assert_not_called()

    def test_successful_visible_wizard_is_assisted_without_a_second_setup(self):
        runner=Mock(return_value=InstallerExit(0,True));attention=Mock()
        result,_=execute_setup(self.exe(),silent=True,credentials={},log=lambda _:None,attention=attention,runner=runner)
        self.assertTrue(result.assisted);runner.assert_called_once();attention.assert_not_called()

    def test_file_verification_failure_also_opens_manual_fallback(self):
        runner=Mock(return_value=0);verify=Mock(side_effect=[OSError('Missing host files'),['host.aex']])
        result,files=execute_setup(self.exe(),silent=True,credentials={},log=lambda _:None,attention=Mock(),runner=runner,verify=verify)
        self.assertTrue(result.assisted);self.assertEqual(files,['host.aex']);self.assertEqual(runner.call_count,2)

    def test_user_cancel_is_not_relaunched(self):
        runner=Mock(return_value=1602)
        with self.assertRaises(RuntimeError):execute_setup(self.exe(),silent=True,credentials={},log=lambda _:None,attention=Mock(),runner=runner)
        runner.assert_called_once()

    def test_running_setup_tracking_error_never_starts_a_duplicate(self):
        error=OSError('Process tracking interrupted');error.retry_safe=False
        runner=Mock(side_effect=error);attention=Mock()
        with self.assertRaises(OSError):execute_setup(self.exe(),silent=True,credentials={},log=lambda _:None,attention=attention,runner=runner)
        runner.assert_called_once();attention.assert_called_once()

    def test_tracking_failure_workspace_can_outlive_the_worker(self):
        workspace=ExtractionWorkspace()
        with workspace as name:
            path=Path(name)/'setup-data.bin';path.write_bytes(b'fixture');workspace.keep=True
        self.assertTrue(path.is_file())
        __import__('shutil').rmtree(name)
        workspace=ExtractionWorkspace()
        with workspace as name:pass
        self.assertFalse(Path(name).exists())

    def test_readonly_extracted_payload_is_cleaned_after_setup(self):
        workspace=ExtractionWorkspace()
        with workspace as name:
            path=Path(name)/'read-only.bin';path.write_bytes(b'fixture');path.chmod(0o444)
        self.assertFalse(Path(name).exists())

    def test_unknown_engine_opens_only_one_normal_wizard(self):
        runner=Mock(return_value=0)
        result,_=execute_setup(self.exe(marker=b'unknown'),silent=True,credentials={},log=lambda _:None,attention=Mock(),runner=runner)
        runner.assert_called_once();self.assertFalse(runner.call_args.kwargs['silent']);self.assertTrue(result.assisted)

    def test_manual_mode_never_attempts_silent_flags(self):
        runner=Mock(return_value=0)
        execute_setup(self.exe(),silent=False,credentials={},log=lambda _:None,attention=Mock(),runner=runner)
        self.assertEqual(runner.call_args.args[0],[str(self.folder/'Setup.exe')])

    def test_sibling_key_folder_is_scoped_to_current_product(self):
        setup=self.exe('ProductA/Setup/Setup.exe')
        docs=[(self.folder/'ProductA/Key/serial.txt','Serial: DEMO-A-1234'),
              (self.folder/'ProductB/Key/serial.txt','Serial: WRONG-B-1234')]
        values=credentials_for_installer(docs,setup)
        self.assertEqual(values.values['serial'],'DEMO-A-1234')
        self.assertEqual(help_values(docs,values,setup)['serial'],['DEMO-A-1234'])

    def test_multiple_keys_remain_separate_copy_fields_without_instructions(self):
        docs=[(self.folder/'Key/key.txt','DEMO-ABCD-123456\n\nDEMO-EFGH-789012\nThanks. Run these instructions.')]
        values=help_values(docs,Credentials(),self.folder/'Setup/setup.exe')
        self.assertEqual(values,{'serial':['DEMO-ABCD-123456','DEMO-EFGH-789012']})

    def test_bcc_without_serial_does_not_invent_one(self):
        self.assertEqual(help_values([(self.folder/'Read Me.txt','Complete the installer and copy the included files.')],Credentials()),{})
        self.assertNotIn('No serial',setup_steps('Continuum_2019_Adobe_12_0_0_Windows.exe'))

    def test_included_files_survive_extraction_and_never_go_to_vendor_folder(self):
        extracted=self.folder/'extracted';extracted.mkdir()
        (extracted/'Continuum_2019_Adobe_12_0_0_Windows.exe').write_bytes(b'fixture')
        files=extracted/'Attachments';files.mkdir();(files/'Library.dll').write_bytes(b'owned backup fixture')
        (files/'do-not-run.cmd').write_text('do not run')
        archive=self.folder/'backup.rar';archive.write_bytes(b'archive fixture')
        destination=preserve_bcc_attachments(extracted,archive)
        self.assertTrue((destination/'Attachments/Library.dll').is_file())
        self.assertFalse((destination/'Attachments/do-not-run.cmd').exists())
        __import__('shutil').rmtree(extracted)
        self.assertEqual((destination/'Attachments/Library.dll').read_bytes(),b'owned backup fixture')
        self.assertFalse((self.folder/'Program Files/BorisFX').exists())

    def test_1638_help_does_not_blame_host_path_or_delete_runtimes(self):
        self.assertIn('product or prerequisite',exit_explanation(1638))
        self.assertIn('Repair/Modify',setup_steps('Sapphire.exe',exit_explanation(1638)))

    def test_help_window_is_reused_and_has_only_credential_fields(self):
        root=tk.Tk();self.addCleanup(root.destroy)
        with patch('ae_archive_deployer.ActivationStore') as store:
            store.return_value.records.return_value=[];app=ArchiveDeployer(root)
        data={'name':'Magic_Bullet_Setup.exe','reason':'Manual step','phase':'visible',
              'values':{'serial':['DEMO-123456']},'attachments':None}
        app.events.put(('setup_help',data));app._poll_events();window=app.setup_help_windows[data['name']]
        app.events.put(('setup_help',{**data,'reason':'Continue the existing wizard'}));app._poll_events()
        self.assertIs(window,app.setup_help_windows[data['name']]);window.copy('DEMO-123456')
        self.assertEqual(root.clipboard_get(),'DEMO-123456')
        app.events.put(('setup_finished',(data['name'],False,'A test error')));app._poll_events()
        self.assertIn('SETUP STOPPED',window.status['text'])

    def test_native_visible_setup_keeps_payload_alive_and_returns_assisted(self):
        output=self.folder/'native.json';callbacks=[];messages=[]
        def attention(phase,reason):
            callbacks.append((phase,reason))


            forms=WindowsForms()
            snapshot=forms.processes()


            pid=owned[0]
            def finish():
                deadline=time.monotonic()+4
                while time.monotonic()<deadline and not output.exists():
                    for hwnd in forms.windows({pid}):
                        for control in forms.controls(hwnd):
                            if control.text=='Next >':forms.send(control.handle,0xF5)
                    time.sleep(.1)
                while time.monotonic()<deadline:
                    for hwnd in forms.windows({pid}):
                        for control in forms.controls(hwnd):
                            if control.text=='Install':forms.send(control.handle,0xF5);return
                    time.sleep(.1)
            threading.Thread(target=finish,daemon=True).start()
        original=subprocess.Popen;owned=[]
        def start(*args,**kwargs):
            process=original(*args,**kwargs);owned.append(process.pid);return process
        with patch('installer_runner.subprocess.Popen',side_effect=start):
            result=run_installer([sys.executable,str(ROOT/'native_installer_fixture.py'),str(output)],self.folder,
                silent=True,credentials=Credentials({'username':'Example','password':'DEMO-PASS','serial':'AAA-BBB-CCC'}),
                log=messages.append,on_attention=attention,timeout=12)
        self.assertEqual(result,0);self.assertTrue(result.assisted);self.assertEqual(len(callbacks),1)
        self.assertTrue(json.loads(output.read_text())['installed'])
        self.assertNotIn('DEMO-PASS',' '.join(messages))


if __name__=='__main__':unittest.main()
