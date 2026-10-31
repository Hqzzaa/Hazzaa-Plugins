from test_support import plugin_bytes
import os
import tempfile
import unittest
import zipfile
import tkinter as tk
from pathlib import Path
from queue import Queue
from unittest.mock import patch

from activation_details import make_record
from ae_archive_deployer import ArchiveDeployer,install_binary
from product_deployment import complete_bcc12,BCC_LIBRARIES,verify_rsmb5_outputs


class ProductDeploymentTests(unittest.TestCase):
    def test_continuum_expected_helper_failure_is_narrowly_scoped(self):
        from installer_runner import expected_helper_exit
        installer=Path('BorisFX Continuum 2026.5 Adobe v19.5.6.exe')
        helper=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/net.exe'
        self.assertTrue(expected_helper_exit([str(installer)],str(helper),2))
        for command,image,code in [(['other.exe'],str(helper),2),
                                   ([str(installer)],str(helper),5),
                                   ([str(installer)],str(helper.with_name('msiexec.exe')),2),
                                   ([str(installer)],str(Path('untrusted/net.exe')),2),
                                   ([str(installer)],None,2)]:
            self.assertFalse(expected_helper_exit(command,image,code))

    def test_continuum_2026_verifies_its_actual_shared_host_directory(self):
        from product_deployment import verify_known_setup_outputs
        with tempfile.TemporaryDirectory() as temp:
            shared=Path(temp)/'Custom Adobe/MediaCore'
            host=shared/'BorisFX/Continuum/BCCFastFilmGlow.aex'
            host.parent.mkdir(parents=True)
            host.write_bytes(plugin_bytes())
            installer=Path('BorisFX Continuum 2026.5 Adobe v19.5.6.exe')
            self.assertEqual(verify_known_setup_outputs(installer,shared),[host])

    def test_continuum_2026_does_not_accept_legacy_host_files_as_new_installation(self):
        from product_deployment import verify_known_setup_outputs
        with tempfile.TemporaryDirectory() as temp:
            shared=Path(temp)
            old=shared/'Continuum Plug-ins/BCCFastFilmGlow.aex'
            old.parent.mkdir()
            old.write_bytes(plugin_bytes())
            with self.assertRaisesRegex(OSError,'expected Adobe host files are missing'):
                verify_known_setup_outputs(Path('BorisFX Continuum 2026.5 Adobe v19.5.6.exe'),shared)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        environment=patch.dict(os.environ,{'LOCALAPPDATA':str(self.root/'local')})
        environment.start();self.addCleanup(environment.stop)
        registry=patch('product_deployment.bcc_registry_candidates',return_value=[])
        registry.start();self.addCleanup(registry.stop)

    def test_pixel_sn_with_asterisks_and_exact_version_target(self):
        archive=self.root/'pixel.zip';key='DEMO*KEY*USER*1234567890'
        with zipfile.ZipFile(archive,'w') as out:
            out.writestr('aepixelsorter_2.0.4/AEPixelSorter2.aex',plugin_bytes('plugin bytes'))
            out.writestr('aepixelsorter_2.0.4/sn.txt',key)
            out.writestr('readme.txt','Copy the AEX into the host Plug-ins folder.')
        target=self.root/'Adobe After Effects 2021/Support Files/Plug-ins'
        app=object.__new__(ArchiveDeployer);app.events=Queue()
        app._process_worker([archive],target,True,script_registry=False,product_names={archive:'AE Pixel Sorter 2.0.4'})
        self.assertEqual((target/'AEPixelSorter2.aex').read_bytes(),plugin_bytes('plugin bytes'))
        events=list(app.events.queue)
        record=next(value for kind,value in events if kind=='activation')
        document=next(d for d in record['documents'] if Path(d['name']).name=='sn.txt')
        self.assertEqual(document['values']['serial'],key)
        self.assertTrue(record['auto_open'])
        logs='\n'.join(value for kind,value in events if kind=='log')
        self.assertIn('VERIFIED FILE:',logs);self.assertIn('Pixel Sorting',logs)
        self.assertNotIn(key,logs)

    def test_bcc_vendor_libs_and_shared_host_effects_are_distinct_destinations(self):
        extracted=self.root/'archive';extracted.mkdir()
        for name in BCC_LIBRARIES:
            path=self.root/'Program Files/BorisFX/ContinuumAE/12/lib'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(name.encode())
        base=self.root/'Program Files'
        vendor_effect=base/'BorisFX/ContinuumAE/12/plugins64/Continuum Plug-ins/BCCBlur.aex'
        vendor_effect.parent.mkdir(parents=True);vendor_effect.write_bytes(b'effect')
        with patch.dict(os.environ,{'ProgramW6432':str(base),'LOCALAPPDATA':str(self.root/'local')}):
            outputs=complete_bcc12(extracted,Path('Continuum_2019_Adobe_12_0_0_Windows.exe'),install_binary)
        self.assertEqual(len(outputs),5)
        self.assertEqual((base/'Adobe/Common/Plug-ins/7.0/MediaCore/Continuum Plug-ins/BCCBlur.aex').read_bytes(),b'effect')
        for name in BCC_LIBRARIES:
            self.assertEqual((base/'BorisFX/ContinuumAE/12/lib'/name).read_bytes(),name.encode())

    def test_empty_bcc_installation_is_not_reported_as_success(self):
        with patch.dict(os.environ,{'ProgramW6432':str(self.root/'empty')}):
            self.assertRaises(OSError,complete_bcc12,self.root,Path('Continuum_2019_Adobe_12_0_0_Windows.exe'),install_binary)

    def test_rsmb_requires_all_three_active_cc_host_files(self):
        installer=Path('RSMB5AE-5.1.8-windows-installer.exe')
        base=self.root/'Program Files'
        with patch.dict(os.environ,{'ProgramW6432':str(base)}):
            self.assertRaises(OSError,verify_rsmb5_outputs,installer)
            target=base/'Adobe/Common/Plug-ins/7.0/MediaCore/RSMB5AE';target.mkdir(parents=True)
            for name in ['RSMB_64.aex','RSMBPro_64.aex','RSMBProVectorInput_64.aex']:(target/name).write_bytes(b'fixture')
            self.assertEqual(len(verify_rsmb5_outputs(installer)),3)

    def test_running_after_effects_blocks_install_before_downloading(self):
        app=object.__new__(ArchiveDeployer);app.busy=False
        with patch.object(app,'_validated_target',return_value=self.root/'Plug-ins'),patch.object(app,'_installation_preflight',return_value=False),patch('ae_archive_deployer.threading.Thread') as thread:
            app.start_library_install([{'id':'fixture'}])
        thread.assert_not_called()

    def test_target_write_failure_blocks_download_and_is_not_reported_as_success(self):
        root=tk.Tk();self.addCleanup(root.destroy);root.withdraw()
        with patch('ae_archive_deployer.ActivationStore') as store:
            store.return_value.records.return_value=[]
            app=ArchiveDeployer(root)
        app.target_var.set(str(self.root/'custom'))
        with patch('ae_archive_deployer.tempfile.NamedTemporaryFile',side_effect=PermissionError('Target is not writable')),patch('ae_archive_deployer.messagebox.showerror') as error,patch('ae_archive_deployer.threading.Thread') as thread:
            self.assertFalse(app.start_library_install([{'id':'fixture'}]))
        error.assert_called_once();thread.assert_not_called()
        app.events.put(('done',{'failures':2}));app._poll_events()
        self.assertIn('INSTALLATION FAILED',app.archive_label.cget('text'))


if __name__=='__main__':unittest.main()
