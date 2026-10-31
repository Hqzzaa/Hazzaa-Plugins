import base64
import io
import json
import subprocess
import tempfile
import tkinter as tk
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import app_control as control
from ae_archive_deployer import ArchiveDeployer
from control_ui import ControlController


class ControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script="""const c=require('node:crypto');const k=c.generateKeyPairSync('rsa',{modulusLength:2048});const n=Buffer.from(k.publicKey.export({format:'jwk'}).n,'base64url').toString('base64');function sign(p){const b=Buffer.from(JSON.stringify(p));return{payload:b.toString('base64'),signature:c.sign('sha256',b,k.privateKey).toString('base64')}}const p={schema:1,app:'hazzaa-plugins-control',revision:0,enabled:true,message:'Available'};process.stdout.write(JSON.stringify({modulus:n,on:sign(p),off:sign({...p,revision:1,enabled:false,message:'Maintenance'}),resume:sign({...p,revision:2}),conflict:sign({...p,revision:1}),invalid:[sign({...p,enabled:'yes'}),sign({...p,revision:-1}),sign({...p,message:'x'.repeat(301)}),sign({...p,app:'other-app'})]}));"""
        cls.fixture=json.loads(subprocess.check_output(['node','-e',script],text=True))

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'Control/policy.json'
        trust=patch.object(control,'MODULUS_B64',self.fixture['modulus'])
        trust.start();self.addCleanup(trust.stop)

    def cache(self,name):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.path.write_text(json.dumps(self.fixture[name]))

    def fetch(self,name):
        response=io.BytesIO(json.dumps(self.fixture[name]).encode())
        return patch('app_control.urllib.request.build_opener',return_value=SimpleNamespace(open=lambda *a,**kw:response))

    def test_signed_enabled_and_disabled_policies(self):
        self.assertTrue(control.verify_policy(self.fixture['on'])['enabled'])
        self.assertFalse(control.verify_policy(self.fixture['off'])['enabled'])

    def test_tampered_policy_cannot_disable_the_application(self):
        bad=dict(self.fixture['on']);data=json.loads(base64.b64decode(bad['payload']))
        data['enabled']=False;bad['payload']=base64.b64encode(json.dumps(data).encode()).decode()
        with self.assertRaises(ValueError):control.verify_policy(bad)

    def test_signed_invalid_scope_types_and_lengths_are_rejected(self):
        for policy in self.fixture['invalid']:
            with self.subTest(policy=policy['payload']):
                with self.assertRaises(ValueError):control.verify_policy(policy)

    def test_policy_is_cached_and_recovers_with_a_new_revision(self):
        with self.fetch('off'):self.assertFalse(control.refresh_policy(self.path)['enabled'])
        self.assertFalse(control.cached_policy(self.path)['enabled'])
        with self.fetch('resume'):self.assertTrue(control.refresh_policy(self.path)['enabled'])
        self.assertEqual(control.cached_policy(self.path)['revision'],2)

    def test_replay_of_old_enabled_policy_cannot_undo_a_disabled_state(self):
        self.cache('off')
        for name in ('on','conflict'):
            with self.fetch(name):
                with self.assertRaises(ValueError):control.refresh_policy(self.path)
        self.assertFalse(control.cached_policy(self.path)['enabled'])

    def test_invalid_cached_bytes_are_not_trusted(self):
        self.path.parent.mkdir(parents=True);self.path.write_text('{"enabled":false}')
        self.assertIsNone(control.cached_policy(self.path))

    def test_cache_write_failure_does_not_discard_a_valid_disabled_response(self):
        with self.fetch('off'),patch.object(Path,'write_bytes',side_effect=PermissionError('No cache access')):
            self.assertFalse(control.refresh_policy(self.path)['enabled'])

    def test_paused_controller_retains_state_offline_and_can_be_reenabled(self):
        root=tk.Tk();self.addCleanup(root.destroy)
        with patch('ae_archive_deployer.ActivationStore') as store:
            store.return_value.records.return_value=[];app=ArchiveDeployer(root)
        root.update_idletasks()
        disabled=control.verify_policy(self.fixture['off'])
        with patch('control_ui.cached_policy',return_value=disabled),patch.object(ControlController,'check'):
            app.control=ControlController(app)
        app.control.receive((None,'offline'))
        self.assertFalse(app._operation_allowed());self.assertTrue(app.download_cancel.is_set())
        self.assertEqual(app.control.panel.winfo_manager(),'place')
        app.control.receive((control.verify_policy(self.fixture['resume']),None))
        self.assertTrue(app._operation_allowed());self.assertEqual(app.control.panel.winfo_manager(),'')

    def test_paused_actions_and_queued_setups_do_not_launch_work(self):
        app=object.__new__(ArchiveDeployer);app.control=SimpleNamespace(blocked=True);app.events=Queue()
        for name in ('open_library','open_uninstall','open_splash_customizer','browse_target','collect_user_presets','restore_script_registry','select_archives','process_archives'):
            getattr(app,name)()
        self.assertIsNone(app.start_library_install([]))
        installer=Path(self.temp.name)/'setup.exe';installer.write_bytes(b'Inno Setup')
        _,run,_=app._prepare_setup_package([installer],installer.parent,installer.parent,[],None,Mock(),True)
        with patch('ae_archive_deployer.execute_setup') as execute:
            with self.assertRaisesRegex(RuntimeError,'disabled by the owner'):run()
            execute.assert_not_called()

    def test_paused_new_batch_does_not_extract_or_modify_files(self):
        app=object.__new__(ArchiveDeployer);app.control=SimpleNamespace(blocked=True);app.events=Queue()
        with patch('ae_archive_deployer.extract_archive') as extract:
            app._process_worker([Path('never-open.zip')],Path(self.temp.name),True)
            extract.assert_not_called()


if __name__=='__main__':unittest.main()
