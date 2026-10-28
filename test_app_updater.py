import base64
import io
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
from unittest.mock import Mock, patch

import app_updater as up
from ae_archive_deployer import ArchiveDeployer

ROOT=Path(__file__).resolve().parent


class UpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture=tempfile.TemporaryDirectory()
        folder=Path(cls.fixture.name)
        cls.payload=b'MZ'+b'non-executable-update-test-fixture'*100
        cls.release={'schema':1,'app':'hazzaa-plugins','version':'2099.1.1.1',
            'bytes':len(cls.payload),'sha256':__import__('hashlib').sha256(cls.payload).hexdigest(),
            'path':'/hazzaa-updates/v1/files/2099.1.1.1/Hazzaa-Plugins.exe','notes':'Test release'}
        (folder/'payload.json').write_text(json.dumps(cls.release),encoding='utf-8')
        result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','RemoteSigned','-File',str(ROOT/'sign_test_manifest.ps1'),
            '-Payload',str(folder/'payload.json'),'-OutputFile',str(folder/'signed.json')],capture_output=True,text=True)
        if result.returncode:raise RuntimeError(result.stderr)
        fixture=json.loads((folder/'signed.json').read_text())
        cls.envelope=fixture['envelope']
        cls.trust=patch.object(up,'MODULUS_B64',fixture['modulus'])
        cls.trust.start()
        cls.addClassCleanup(cls.trust.stop)

    @classmethod
    def tearDownClass(cls):cls.fixture.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.update_root=self.root/'Updates'
        self.patch=patch.object(up,'updates_root',return_value=self.update_root)
        self.patch.start();self.addCleanup(self.patch.stop)
        self.target=self.root/'App Folder'/"Hazzaa user's app.exe"
        self.target.parent.mkdir();self.target.write_bytes(b'MZ-original-app-data')

    def prepare(self,data=None):
        stream=io.BytesIO(self.payload if data is None else data)
        with patch.object(up,'_open',return_value=stream):
            return up.prepare_update(self.envelope,self.target)

    def test_native_cng_accepts_valid_publisher_signature(self):
        self.assertEqual(up.verify_manifest(self.envelope),self.release)

    def test_every_single_byte_signature_change_is_rejected(self):
        original=base64.b64decode(self.envelope['signature'])
        for index in range(len(original)):
            modified=bytearray(original);modified[index]^=1
            with self.subTest(index=index),self.assertRaises(ValueError):
                up.verify_manifest({**self.envelope,'signature':base64.b64encode(modified).decode()})

    def test_modified_payload_and_unknown_encoding_are_rejected(self):
        for envelope in [{},[],{**self.envelope,'payload':'!'},
            {**self.envelope,'payload':base64.b64encode(b'{"changed":true}').decode()},
            {**self.envelope,'signature':base64.b64encode(b'x').decode()}]:
            with self.subTest(envelope_type=type(envelope)),self.assertRaises((ValueError,TypeError)):
                up.verify_manifest(envelope)

    def test_version_comparison_is_numeric(self):
        self.assertGreater(up.version_tuple('2026.10.28.10'),up.version_tuple('2026.10.28.9'))
        for bad in ['',None,'1.0','2026.10.28.1.exe','2026.10.28.-1']:
            with self.subTest(value=bad),self.assertRaises(ValueError):up.version_tuple(bad)

    def test_check_rejects_downgrade_and_equal_versions(self):
        for current,expected in [('2099.1.1.2',False),('2099.1.1.1',False),('2026.10.28.1',True)]:
            with patch.object(up,'VERSION',current),patch.object(up,'_open',return_value=io.BytesIO(json.dumps(self.envelope).encode())):
                self.assertEqual(bool(up.check_update()),expected)

    def test_no_insecure_redirect(self):
        with self.assertRaises(ValueError):up._HttpsOnly().redirect_request(None,None,302,'',{},'http://example.test/update')

    def test_response_size_cap(self):
        with patch.object(up,'_open',return_value=io.BytesIO(b'x'*(up.MAX_MANIFEST+1))),self.assertRaises(ValueError):
            up.check_update()

    def test_truncated_oversized_corrupt_downloads_keep_existing_app(self):
        original=self.target.read_bytes()
        for data in [self.payload[:-1],self.payload+b'x',b'MZ'+b'X'*(len(self.payload)-2)]:
            with self.subTest(size=len(data)),self.assertRaises(ValueError):self.prepare(data)
            self.assertEqual(self.target.read_bytes(),original)
            self.assertFalse(list(self.update_root.glob('update-*')))

    def test_network_failure_does_not_change_app(self):
        original=self.target.read_bytes()
        with patch.object(up,'_open',side_effect=OSError('Offline')),self.assertRaises(OSError):
            up.prepare_update(self.envelope,self.target)
        self.assertEqual(self.target.read_bytes(),original)

    def test_prepare_rejects_current_version_and_non_exe(self):
        with patch.object(up,'VERSION',self.release['version']),self.assertRaises(ValueError):
            up.prepare_update(self.envelope,self.target)
        with self.assertRaises(ValueError):up.prepare_update(self.envelope,self.root/'file.txt')

    def test_successful_replace_backup_and_health_receipt(self):
        job=self.prepare();old=self.target.read_bytes()
        def launch(args):
            self.assertEqual(Path(args[0]),self.target)
            path,record,_,_=up.load_job(args[2])
            (path.parent/'healthy').write_text(record['token'])
            return Mock(poll=lambda:None)
        self.assertTrue(up.apply_update(job,launch=launch,timeout=.5))
        self.assertEqual(self.target.read_bytes(),self.payload)
        self.assertEqual((job.parent/'previous.exe').read_bytes(),old)
        self.assertTrue(json.loads((job.parent/'result.json').read_text())['ok'])

    def test_failed_start_rolls_back_exact_original(self):
        job=self.prepare();old=self.target.read_bytes()
        with self.assertRaises(OSError):up.apply_update(job,launch=lambda args:Mock(poll=lambda:1),timeout=.1)
        self.assertEqual(self.target.read_bytes(),old)
        self.assertTrue(json.loads((job.parent/'result.json').read_text())['rollback'])

    def test_missing_health_stops_child_and_rolls_back(self):
        job=self.prepare();child=Mock();child.poll.return_value=None
        with self.assertRaises(OSError):up.apply_update(job,launch=lambda args:child,timeout=.1)
        child.terminate.assert_called_once()
        self.assertEqual(self.target.read_bytes(),b'MZ-original-app-data')

    def test_target_changed_after_download_is_not_overwritten(self):
        job=self.prepare();self.target.write_bytes(b'MZ-newer-separate-update')
        with self.assertRaises(ValueError):up.apply_update(job,launch=Mock(),timeout=.1)
        self.assertEqual(self.target.read_bytes(),b'MZ-newer-separate-update')

    def test_job_tampering_and_candidate_corruption_are_rejected(self):
        job=self.prepare();(job.parent/'new.exe').write_bytes(b'MZ-broken')
        with self.assertRaises(ValueError):up.apply_update(job,launch=Mock(),timeout=.1)
        self.assertEqual(self.target.read_bytes(),b'MZ-original-app-data')
        moved=self.root/'job.json';moved.write_bytes(job.read_bytes())
        with self.assertRaises(ValueError):up.load_job(moved)

    def test_cancelled_handoff_never_replaces_original(self):
        job=self.prepare();(job.parent/'cancel').write_text('cancel')
        with self.assertRaises(OSError):up.apply_update(job,launch=Mock(),timeout=.1)
        self.assertEqual(self.target.read_bytes(),b'MZ-original-app-data')

    def test_real_windows_file_lock_waits_and_then_replaces(self):
        import ctypes
        job=self.prepare()
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_void_p]
        kernel.CreateFileW.restype=ctypes.c_void_p
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=kernel.CreateFileW(str(self.target),0x80000000,1,None,3,0,None)
        self.assertNotEqual(handle,ctypes.c_void_p(-1).value)
        timer=threading.Timer(.4,lambda:kernel.CloseHandle(handle));timer.start()
        def launch(args):
            record=json.loads(job.read_text());(job.parent/'healthy').write_text(record['token'])
            return Mock(poll=lambda:None)
        started=time.monotonic()
        try:self.assertTrue(up.apply_update(job,launch=launch,timeout=2))
        finally:timer.join()
        self.assertGreaterEqual(time.monotonic()-started,.4)

    def test_locked_exe_timeout_keeps_original(self):
        job=self.prepare()
        with patch.object(up.os,'replace',side_effect=PermissionError('locked')),self.assertRaises(OSError):
            up.apply_update(job,launch=Mock(),timeout=.01)
        self.assertEqual(self.target.read_bytes(),b'MZ-original-app-data')

    def test_startup_health_requires_matching_executable_and_version(self):
        job=self.prepare();self.target.write_bytes(self.payload)
        with patch.object(up,'VERSION',self.release['version']),patch.object(sys,'executable',str(self.target)):
            up.acknowledge_update(job)
        self.assertTrue((job.parent/'healthy').is_file())
        with self.assertRaises(ValueError):up.acknowledge_update(job)

    def test_cache_cleanup_keeps_latest_backup_and_active_job(self):
        self.update_root.mkdir()
        for name,ok in [('old',True),('latest',True),('failed',False),('active',None)]:
            folder=self.update_root/('update-'+name);folder.mkdir()
            if ok is not None:
                result=folder/'result.json';result.write_text(json.dumps({'ok':ok}))
                age=9*86400 if name!='latest' else 8*86400
                os.utime(result,(time.time()-age,time.time()-age))
        up.cleanup_updates()
        self.assertEqual(sorted(p.name for p in self.update_root.iterdir()),['update-active','update-latest'])

    def test_tk_button_position_notice_retry_and_busy_guard(self):
        root=tk.Tk();self.addCleanup(root.destroy)
        with patch('ae_archive_deployer.ActivationStore') as store:
            store.return_value.records.return_value=[]
            app=ArchiveDeployer(root)
        root.geometry('920x650');root.update()
        ui=app.updater
        self.assertGreater(ui.button.winfo_y(),root.winfo_height()*.9)
        self.assertGreater(ui.button.winfo_x(),root.winfo_width()*.7)
        self.assertEqual(ui.button['text'],'CHECK UPDATE')
        with patch('update_ui.messagebox.showinfo') as info:
            app.busy=True;ui.handle('update_checked',((self.envelope,self.release),False))
            ui.notify_if_idle();info.assert_not_called()
            app.busy=False;ui.notify_if_idle();info.assert_called_once()
        with patch('update_ui.messagebox.showinfo') as info:
            app.busy=True;ui.install();info.assert_called_once();self.assertFalse(ui.installing)
        ui.handle('update_check_error',('Offline',False))
        self.assertEqual(ui.button['text'],'UPDATE')
        app.busy=False

    def test_ready_handoff_exits_tk_without_querying_destroyed_widgets(self):
        root=tk.Tk()
        with patch('ae_archive_deployer.ActivationStore') as store:
            store.return_value.records.return_value=[]
            app=ArchiveDeployer(root)
        stage=self.root/'handoff';stage.mkdir();(stage/'ready').write_text('token')
        path=stage/'job.json';helper=Mock()
        app.events.put(('update_handoff',(path,helper,time.monotonic()+30)))
        with patch('update_ui.load_job',return_value=(path,{'token':'token'},None,None)):
            app._poll_events()
        self.assertTrue(app._update_exiting)


if __name__=='__main__':unittest.main()
