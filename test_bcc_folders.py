import json
import os
import sys
import tempfile
import tkinter as tk
import unittest
import zipfile
from pathlib import Path
from queue import Queue
from unittest.mock import Mock,patch

from ae_archive_deployer import ArchiveDeployer,install_binary
from installer_help import bcc_attachment_folder,setup_steps
from product_deployment import (BCC_LIBRARIES,bcc_registry_candidates,bcc_library_directory,
    choose_bcc_library,complete_bcc12)


class BccFolderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        environment=patch.dict(os.environ,{'LOCALAPPDATA':str(self.root/'Local'),
            'ProgramW6432':str(self.root/'Default Program Files')})
        environment.start();self.addCleanup(environment.stop)
        registry=patch('product_deployment.bcc_registry_candidates',return_value=[])
        self.registry=registry.start();self.addCleanup(registry.stop)

    def lib(self,name):
        folder=self.root/name/'lib';folder.mkdir(parents=True)
        for filename in BCC_LIBRARIES:(folder/filename).write_bytes(b'vendor fixture')
        return folder

    def test_deep_serial_folder_is_selected_without_fixed_username_or_hash(self):
        for name in ('BCC-random-one','BCC-another-hash'):
            root=self.root/name;leaf=root/'Different Wrapper/serial/serial';leaf.mkdir(parents=True)
            self.assertEqual(bcc_attachment_folder(root),leaf)

    def test_other_wrapper_names_use_the_folder_with_runtime_attachments(self):
        root=self.root/'cache';leaf=root/'Wrapper/Included/Files';leaf.mkdir(parents=True)
        for name in BCC_LIBRARIES:(leaf/name).write_bytes(b'fixture')
        (root/'Read Me.txt').write_text('Instructions')
        self.assertEqual(bcc_attachment_folder(root),leaf)

    def test_registry_install_folder_on_a_custom_layout_is_used(self):
        lib=self.lib('Other Drive/Applications/My Boris/12')
        self.registry.return_value=[lib.parent]
        self.assertEqual(bcc_library_directory(),lib)

    def test_uninstall_registry_reader_handles_both_views_and_only_version_12(self):
        chosen=self.root/'Custom Vendor/12'
        entries={'v12':{'DisplayName':'Boris Continuum 2019 Adobe','DisplayVersion':'12.0.0','InstallLocation':str(chosen)},
                 'v13':{'DisplayName':'Boris Continuum 2020 Adobe','DisplayVersion':'13.0.0','InstallLocation':str(self.root/'v13')},
                 'invalid':{'DisplayName':123}}
        class Key:
            def __init__(self,data=None):self.data=data
            def __enter__(self):return self
            def __exit__(self,*_):pass
        views=[]
        def open_key(hive,name,*args):
            if name.startswith('SOFTWARE'):views.append(args[-1]);return Key()
            return Key(entries[name])
        def query(key,name):
            if name not in key.data:raise FileNotFoundError(name)
            return key.data[name],1
        fake=Mock(HKEY_LOCAL_MACHINE=1,HKEY_CURRENT_USER=2,KEY_WOW64_64KEY=4,KEY_WOW64_32KEY=8,KEY_READ=1)
        fake.OpenKey=open_key;fake.QueryInfoKey=lambda key:(len(entries),0,0)
        fake.EnumKey=lambda key,index:list(entries)[index];fake.QueryValueEx=query
        with patch.dict(sys.modules,{'winreg':fake}):found=bcc_registry_candidates()
        self.assertEqual(found,[chosen]);self.assertEqual(set(views),{5,9})

    def test_browse_choice_is_saved_and_wins_over_an_older_install(self):
        old=self.lib('Old/12');chosen=self.lib('Chosen Vendor Folder/12')
        self.registry.return_value=[old.parent]
        self.assertEqual(choose_bcc_library(chosen.parent),chosen)
        self.assertEqual(bcc_library_directory(),chosen)
        saved=json.loads((self.root/'Local/Hazzaa Plugins/Settings/bcc12-location.json').read_text())
        self.assertEqual(saved['path'],str(chosen))

    def test_browse_accepts_empty_lib_and_rejects_an_unrelated_folder(self):
        folder=self.root/'Chosen/lib';folder.mkdir(parents=True)
        self.assertEqual(choose_bcc_library(folder),folder)
        self.assertEqual(bcc_library_directory(),folder)
        unrelated=self.root/'unrelated';unrelated.mkdir()
        with self.assertRaises(ValueError):choose_bcc_library(unrelated)

    def test_completion_uses_custom_vendor_lib_and_host_files(self):
        lib=self.lib('Custom Disk/Chosen Boris/12');self.registry.return_value=[lib.parent]
        effect=lib.parent/'plugins64/Continuum Plug-ins/BCC_Fixture.aex'
        effect.parent.mkdir(parents=True);effect.write_bytes(b'host file fixture')
        shared=self.root/'Custom Adobe/MediaCore'
        outputs=complete_bcc12(self.root/'archive',Path('Continuum_2019_Adobe_12_0_0_Windows.exe'),install_binary,shared)
        self.assertEqual(outputs[1:],[lib/name for name in BCC_LIBRARIES])
        self.assertEqual((shared/'Continuum Plug-ins/BCC_Fixture.aex').read_bytes(),b'host file fixture')

    def test_instructions_are_at_most_four_short_lines(self):
        for name in ('Continuum_Setup.exe','Sapphire.exe','Magic_Bullet_Setup.exe','Other.exe'):
            text=setup_steps(name,'child reported 1638: another version is installed')
            self.assertLessEqual(len(text.splitlines()),4)
            self.assertNotIn('License Control',text)
            self.assertNotIn('No serial',text)
            self.assertNotIn('prerequisite',text)

    def test_sapphire_success_reports_files_and_restart_without_not_checked(self):
        archive=self.root/'sapphire.zip'
        with zipfile.ZipFile(archive,'w') as output:
            output.writestr('Boris FX Sapphire AE 2020.02 CE.exe',b'Inno Setup fixture')
        app=object.__new__(ArchiveDeployer);app.events=Queue()
        with patch('ae_archive_deployer.run_installer',return_value=0),patch('ae_archive_deployer.verify_known_setup_outputs',return_value=[Path('fixture.aex')]*274):
            app._process_worker([archive],self.root/'Plug-ins',True,script_registry=False)
        logs='\n'.join(value for kind,value in app.events.queue if kind=='log')
        self.assertIn('Verified 274 plug-in file(s). Restart After Effects.',logs)
        self.assertNotIn('not yet checked',logs)

    def test_worker_emits_inner_attachment_and_actual_lib_once_each(self):
        archive=self.root/'bcc.zip';lib=self.lib('Chosen/12')
        with zipfile.ZipFile(archive,'w') as output:
            output.writestr('BCC Plugin/Continuum_2019_Adobe_12_0_0_Windows.exe',b'Inno Setup fixture')
            output.writestr('BCC Plugin/serial/serial/'+BCC_LIBRARIES[0],b'attachment fixture')
        app=object.__new__(ArchiveDeployer);app.events=Queue()
        with patch('ae_archive_deployer.run_installer',return_value=0),patch('ae_archive_deployer.complete_bcc12',return_value=[self.root/'host']),patch('ae_archive_deployer.bcc_library_directory',return_value=lib):
            app._process_worker([archive],self.root/'Plug-ins',True,script_registry=False)
        folders=[Path(value) for kind,value in app.events.queue if kind=='included_files']
        self.assertEqual(len(folders),2);self.assertEqual(folders[0].parts[-2:],('serial','serial'));self.assertEqual(folders[1],lib)
        self.assertEqual((lib/BCC_LIBRARIES[0]).read_bytes(),b'vendor fixture')

    def test_popup_has_no_error_paragraph_and_both_folder_buttons_work(self):
        root=tk.Tk();self.addCleanup(root.destroy)
        with patch('ae_archive_deployer.ActivationStore') as store:
            store.return_value.records.return_value=[];app=ArchiveDeployer(root)
        source=self.root/'serial/serial';source.mkdir(parents=True);lib=self.lib('Selected/12')
        payload={'name':'Continuum_Setup.exe','reason':'A very long diagnostic paragraph about 1638. '*12,
            'phase':'retry','values':{},'attachments':str(source)}
        app.events.put(('setup_help',payload));app._poll_events();window=app.setup_help_windows[payload['name']]
        text=window.message.get('1.0','end');self.assertNotIn('very long',text);self.assertLessEqual(len(text.strip().splitlines()),3)
        self.assertEqual(window.fields.winfo_children(),[])
        with patch('installer_support_ui.os.startfile') as opened,patch('installer_support_ui.bcc_library_directory',return_value=lib):
            window.open_files();window.open_lib()
        self.assertEqual([call.args[0] for call in opened.call_args_list],[str(source),str(lib)])
        window.root.geometry('700x350');root.update()
        for button in (window.files_button,window.lib_button,window.browse_button):
            self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),window.root.winfo_rooty()+window.root.winfo_height())
        window.finished(False,'another very long failure paragraph'*20)
        self.assertEqual(window.status['text'],'SETUP STOPPED — COPY LOG FOR DETAILS')

    def test_repeated_automatic_folder_events_do_not_open_duplicate_windows(self):
        root=tk.Tk();self.addCleanup(root.destroy)
        with patch('ae_archive_deployer.ActivationStore') as store:
            store.return_value.records.return_value=[];app=ArchiveDeployer(root)
        folder=self.root/'Files';folder.mkdir()
        with patch('ae_archive_deployer.os.startfile') as opened:
            app.events.put(('included_files',str(folder)));app.events.put(('included_files',str(folder)))
            app._poll_events()
        opened.assert_called_once_with(str(folder))


if __name__=='__main__':unittest.main()
