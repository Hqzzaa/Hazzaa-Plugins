import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from queue import Queue
from unittest.mock import Mock,patch

from ae_archive_deployer import ArchiveDeployer
from installer_automation import Control,Credentials,InstallerAssistant,WindowsForms
from installer_help import ExtractionWorkspace
from installer_interaction import advance_bcc_license_page,requires_input
from installer_runner import InstallerExit,SetupHandoff,run_installer
from installer_workflow import execute_setup
from setup_dispatcher import SetupDispatcher
from test_support import plugin_bytes

ROOT=Path(__file__).resolve().parent


class FakeHandoff:
    handed_off=True
    def __init__(self):self.closed=threading.Event();self.waited=threading.Event()
    def wait_until_closed(self):
        self.waited.set()
        if not self.closed.wait(8):raise TimeoutError('Test handoff was not closed')
        return 1638


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        env=patch.dict(os.environ,{'LOCALAPPDATA':str(self.root/'Local'),'APPDATA':str(self.root/'Roaming')})
        env.start();self.addCleanup(env.stop)

    def until(self,condition,seconds=5):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if condition():return
            time.sleep(.02)
        self.fail('Timed out waiting for the test condition')

    def setup_file(self,name='setup.exe'):
        path=self.root/name;path.write_bytes(b'Inno Setup fixture');return path

    def dispatcher(self):
        manager=SetupDispatcher();self.addCleanup(manager.close);return manager

    def manual(self):
        manual=FakeHandoff();self.addCleanup(manual.closed.set);return manual

    def test_workflow_returns_manual_immediately_without_verify_or_retry(self):
        manual=self.manual();runner=Mock(return_value=manual);verify=Mock()
        result,evidence=execute_setup(self.setup_file(),silent=True,credentials=Credentials(),
            log=lambda _:None,attention=Mock(),verify=verify,runner=runner)
        self.assertIs(result,manual);self.assertIsNone(evidence)
        runner.assert_called_once();verify.assert_not_called()

    def test_assisted_failure_never_reopens_a_completed_wizard(self):
        for code in (2,5,1638):
            runner=Mock(return_value=InstallerExit(code,True))
            with self.subTest(code=code),self.assertRaises(RuntimeError):
                execute_setup(self.setup_file(),silent=True,credentials={},log=lambda _:None,attention=Mock(),runner=runner)
            runner.assert_called_once()

    def test_assisted_postcondition_failure_does_not_restart_setup(self):
        runner=Mock(return_value=InstallerExit(0,True))
        with self.assertRaises(OSError):execute_setup(self.setup_file(),silent=True,credentials={},log=lambda _:None,
            attention=Mock(),runner=runner,verify=Mock(side_effect=OSError('Missing files')))
        runner.assert_called_once()

    def test_queue_returns_on_handoff_and_holds_the_next_setup(self):
        manager=self.dispatcher();manual=self.manual();started=[]
        first=ExtractionWorkspace();second=ExtractionWorkspace()
        with first as folder:
            first_data=Path(folder)/'needed.bin';first_data.write_bytes(b'first')
            ticket,state=manager.submit('one','Manual setup',lambda:(manual,None),first)
            self.assertEqual(state,'manual');self.assertTrue(manual.waited.wait(1))
        with second as folder:
            second_data=Path(folder)/'needed.bin';second_data.write_bytes(b'second')
            def run():started.append(True);self.assertTrue(second_data.exists());return InstallerExit(0),{}
            next_ticket,next_state=manager.submit('two','Next setup',run,second)
            self.assertEqual(next_state,'queued')
        self.assertTrue(first_data.exists());self.assertTrue(second_data.exists());self.assertEqual(started,[])
        manual.closed.set();self.until(lambda:manager.pending_count==0)
        self.assertEqual(started,[True]);self.assertEqual(next_ticket.state,'finished')
        self.until(lambda:not Path(first.name).exists() and not Path(second.name).exists())

    def test_duplicate_binary_is_not_started_while_user_finishes_it(self):
        manager=self.dispatcher();manual=self.manual();run=Mock(return_value=(manual,None))
        first=ExtractionWorkspace();second=ExtractionWorkspace()
        with first:ticket,state=manager.submit('same-binary','Magic Bullet',run,first)
        with second:duplicate,state=manager.submit('same-binary','Magic Bullet',run,second)
        self.assertIs(duplicate,ticket);self.assertEqual(state,'duplicate');run.assert_called_once()
        self.assertFalse(Path(second.name).exists());self.assertTrue(Path(first.name).exists())
        manual.closed.set();self.until(lambda:manager.pending_count==0)

    def test_lost_manual_tracking_stops_later_setups_and_preserves_sources(self):
        manager=self.dispatcher();manual=self.manual();first=ExtractionWorkspace();second=ExtractionWorkspace()
        def lost():manual.closed.wait(3);raise OSError('Tracking unavailable')
        manual.wait_until_closed=lost;finished=Mock()
        with first:manager.submit('one','Manual',lambda:(manual,None),first,finished)
        run=Mock(return_value=(0,{}))
        with second:manager.submit('two','Queued',run,second)
        manual.closed.set();self.until(lambda:manager.pending_count==0)
        run.assert_not_called();self.assertIsInstance(finished.call_args.args[2],OSError)
        self.assertTrue(Path(first.name).exists());self.assertTrue(Path(second.name).exists())
        for workspace in (first,second):workspace.keep=False;workspace._cleanup_if_unused()

    def test_batch_installs_another_plugin_before_manual_setup_finishes(self):
        manual_archive=self.root/'manual.zip';loose_archive=self.root/'loose.zip';queued_archive=self.root/'queued.zip'
        with zipfile.ZipFile(manual_archive,'w') as output:output.writestr('Magic_Bullet_Setup.exe',b'Inno Setup one')
        with zipfile.ZipFile(loose_archive,'w') as output:output.writestr('Independent.aex',plugin_bytes())
        with zipfile.ZipFile(queued_archive,'w') as output:output.writestr('OtherSetup.exe',b'Inno Setup two')
        app=object.__new__(ArchiveDeployer);app.events=Queue();manual=self.manual();calls=[]
        app.setup_dispatcher=SetupDispatcher(lambda msg:app.events.put(('log',msg)),lambda kind,value:app.events.put((kind,value)))
        self.addCleanup(app.setup_dispatcher.close)
        def runner(command,directory,**kwargs):
            calls.append(Path(command[0]).name)
            if len(calls)==1:
                kwargs['on_attention']('visible','Complete the visible wizard.')
                return manual
            return InstallerExit(0)
        target=self.root/'AE/Support Files/Plug-ins'
        with patch('ae_archive_deployer.run_installer',side_effect=runner),patch('ae_archive_deployer.verify_known_setup_outputs',return_value=None):
            start=time.monotonic();app._process_worker([manual_archive,loose_archive,queued_archive],target,True,script_registry=False)
            self.assertLess(time.monotonic()-start,2)
            self.assertTrue((target/'Independent.aex').exists())
            self.assertEqual(calls,['Magic_Bullet_Setup.exe'])
            done=[value for kind,value in app.events.queue if kind=='done'][-1]
            self.assertEqual(done['failures'],0);self.assertEqual(done['pending_setups'],2)
            manual.closed.set();self.until(lambda:app.setup_dispatcher.pending_count==0)
        self.assertEqual(calls,['Magic_Bullet_Setup.exe','OtherSetup.exe'])
        self.assertFalse(any(kind=='setup_finished' and value[1] for kind,value in app.events.queue if 'Magic_Bullet' in str(value)))

    def test_closing_app_preserves_unstarted_setup_files(self):
        manager=self.dispatcher();manual=self.manual();first=ExtractionWorkspace();second=ExtractionWorkspace()
        with first:manager.submit('first','Manual',lambda:(manual,None),first)
        run=Mock(return_value=(0,{}))
        with second:manager.submit('second','Queued',run,second)
        manager.close();manual.closed.set();self.until(lambda:manager.pending_count==0)
        run.assert_not_called();self.assertTrue(Path(first.name).exists());self.assertTrue(Path(second.name).exists())
        for workspace in (first,second):workspace.keep=False;workspace._cleanup_if_unused()

    def test_missing_runtime_is_queued_while_other_loose_plugins_continue(self):
        archives=[]
        for name,member,data in [('manual','Setup.exe',b'Inno Setup'),
                ('runtime','NeedsRuntime.aex',plugin_bytes()),('independent','Independent.aex',plugin_bytes())]:
            path=self.root/(name+'.zip')
            with zipfile.ZipFile(path,'w') as output:output.writestr(member,data)
            archives.append(path)
        manual=self.manual();app=object.__new__(ArchiveDeployer);app.events=Queue()
        app.setup_dispatcher=SetupDispatcher();self.addCleanup(app.setup_dispatcher.close)
        def missing(plugins):return {'vcruntime140.dll'} if any(p.name=='NeedsRuntime.aex' for p in plugins) else set()
        target=self.root/'Plug-ins';runtime_calls=[]
        def prerequisites(plugins,*args,**kwargs):
            if any(p.name=='NeedsRuntime.aex' for p in plugins):runtime_calls.append(True)
        with patch('ae_archive_deployer.run_installer',return_value=manual),patch('ae_archive_deployer.missing_runtime_files',side_effect=missing),patch('ae_archive_deployer.ensure_prerequisites',side_effect=prerequisites):
            app._process_worker(archives,target,True,script_registry=False)
            self.assertTrue((target/'Independent.aex').exists());self.assertFalse((target/'NeedsRuntime.aex').exists())
            self.assertEqual(runtime_calls,[])
            manual.closed.set();self.until(lambda:app.setup_dispatcher.pending_count==0)
        self.assertEqual(runtime_calls,[True]);self.assertTrue((target/'NeedsRuntime.aex').exists())

    def test_user_entered_text_and_user_cleared_field_are_never_replaced(self):
        forms=Mock();forms.process_identity.return_value=10
        label=Control(1,10,'Static','Serial number:',(10,10,140,34),0)
        edit=Control(2,10,'Edit','USER-ENTERED-123',(150,10,370,34),0)
        forms.controls.return_value=[label,edit]
        forms.send.return_value=32767
        with patch('installer_automation.WindowsForms',return_value=forms):
            assistant=InstallerAssistant(100,Credentials({'serial':'ARCHIVE-OTHER-456'}),allow_advance=False)
        assistant._fill_window(10);edit.text='';assistant._fill_window(10)
        forms.set_text.assert_not_called()

    def test_automatically_filled_field_is_not_refilled_after_user_clears_it(self):
        forms=Mock();forms.process_identity.return_value=10;forms.send.return_value=32767
        label=Control(1,10,'Static','Serial:',(10,10,140,34),0)
        edit=Control(2,10,'Edit','',(150,10,370,34),0)
        forms.controls.return_value=[label,edit];forms.set_text.return_value=True
        with patch('installer_automation.WindowsForms',return_value=forms):assistant=InstallerAssistant(100,Credentials({'serial':'DEMO-123'}),allow_advance=False)
        assistant._fill_window(10);assistant._fill_window(10)
        forms.set_text.assert_called_once_with(2,'DEMO-123')

    def test_progress_only_page_is_not_manual_input(self):
        forms=Mock();forms.controls.return_value=[Control(1,10,'msctls_progress32','',(0,0,100,20),0),
            Control(2,10,'Button','Cancel',(0,30,100,50),0)]
        self.assertFalse(requires_input(forms,10))
        forms.controls.return_value.append(Control(3,10,'Button','Next >',(120,30,200,50),0))
        self.assertTrue(requires_input(forms,10))

    def test_only_the_exact_bcc_unlicensed_page_advances_once(self):
        forms=Mock();forms.text.return_value='Boris FX License Tool'
        forms.controls.return_value=[Control(1,10,'Static','Continuum is not licensed.',(0,0,300,30),0),
            Control(2,10,'Button','Complete installation without entering a license',(0,40,400,70),9),
            Control(3,10,'Button','Next >',(300,80,400,110),0)]
        forms.send.return_value=1;completed=set()
        self.assertTrue(advance_bcc_license_page(forms,10,completed));self.assertFalse(advance_bcc_license_page(forms,10,completed))
        self.assertEqual([c for c in forms.send.call_args_list if c.args==(3,0xF5)],[( (3,0xF5), {})])

    def test_other_vendor_or_other_license_page_is_not_clicked(self):
        forms=Mock();forms.text.return_value='Other Vendor License Tool';forms.controls.return_value=[]
        self.assertFalse(advance_bcc_license_page(forms,10,set()));forms.send.assert_not_called()

    def test_native_manual_handoff_returns_before_next_is_clicked(self):
        output=self.root/'native-manual.json';attention=Mock()
        handoff=run_installer([sys.executable,str(ROOT/'native_installer_fixture.py'),str(output)],self.root,
            silent=True,credentials=Credentials({'username':'Demo','password':'DEMO-PASS','serial':'AAA-BBB-CCC'}),
            log=lambda _:None,on_attention=attention,handoff_on_attention=True,timeout=8)
        self.assertIsInstance(handoff,SetupHandoff);self.assertFalse(output.exists());self.assertIsNone(handoff.process.poll())
        try:
            forms=WindowsForms();pid=handoff.process.pid
            def submitted():
                for hwnd in forms.windows({pid}):
                    for control in forms.controls(hwnd):
                        if control.text=='Next >':forms.send(control.handle,0xF5)
                return output.exists()
            self.until(submitted)
            for hwnd in forms.windows({pid}):
                for control in forms.controls(hwnd):
                    if control.text=='Install':forms.send(control.handle,0xF5)
            handoff.wait_until_closed();self.assertTrue(json.loads(output.read_text())['installed'])
            attention.assert_called_once()
        finally:
            if handoff.process.poll() is None:handoff.job.terminate();handoff.process.wait(timeout=5)
            handoff.job.close()

    def test_native_progress_completes_without_manual_handoff(self):
        output=self.root/'progress.json';attention=Mock()
        result=run_installer([sys.executable,str(ROOT/'native_installer_fixture.py'),str(output),'--progress-only'],self.root,
            silent=True,credentials={},log=lambda _:None,on_attention=attention,handoff_on_attention=True,timeout=8)
        self.assertEqual(result,0);attention.assert_not_called();self.assertTrue(output.exists())

    def test_native_bcc_option_is_selected_and_next_clicked_automatically(self):
        output=self.root/'bcc-option.json';attention=Mock()
        result=run_installer([sys.executable,str(ROOT/'native_installer_fixture.py'),str(output),'--bcc-license'],self.root,
            silent=True,credentials={},log=lambda _:None,on_attention=attention,handoff_on_attention=True,timeout=8)
        self.assertEqual(result,0);attention.assert_not_called()
        self.assertEqual(json.loads(output.read_text()),{'without_license':True,'next_clicked':True})


if __name__=='__main__':unittest.main()
