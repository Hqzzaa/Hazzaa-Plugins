import json
import io
import os
import tempfile
import tkinter as tk
import unittest
import zipfile
from pathlib import Path
from queue import Queue
from unittest.mock import patch,Mock

from activation_details import ActivationStore,make_record
from ae_archive_deployer import ArchiveDeployer,install_binary
from splash_customizer import SplashCustomizer
from PIL import Image
from silent_install import silent_command


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def record(self,name='Deep Glow 2',code='DEMO-1234-5678'):
        return make_record(name,name+'.zip',['DeepGlow2.aex'],
                           [(self.root/'readme.txt','Serial: '+code)],self.root,True)

    def test_named_receipt_preserves_docs_without_mixing_products(self):
        a=self.record();b=self.record('Other Plug-in','OTHER-5678-1234')
        self.assertNotEqual(a['id'],b['id'])
        self.assertEqual(a['documents'][0]['values']['serial'],'DEMO-1234-5678')
        self.assertNotIn('OTHER',json.dumps(a))
        duplicate=make_record('Deep Glow 2','archive.zip',['DeepGlow2.aex'],
            [(self.root/'readme.txt','Serial: DEMO-1234'),(self.root/'Win/readme.txt','Serial: DEMO-1234')],self.root,True)
        self.assertEqual(len(duplicate['documents']),1)

    def test_windows_encrypted_receipt_survives_restart(self):
        if os.name!='nt':self.skipTest('Windows DPAPI')
        store=ActivationStore(self.root/'receipts');record=self.record()
        store.save(record)
        blob=next(store.directory.glob('*.bin')).read_bytes()
        self.assertNotIn(b'DEMO-1234-5678',blob)
        self.assertEqual(ActivationStore(store.directory).records(),[record])

    def test_one_activation_window_reused_and_copy_is_for_selected_product(self):
        root=tk.Tk();self.addCleanup(root.destroy)
        with patch('ae_archive_deployer.ActivationStore') as store:
            store.return_value.records.return_value=[]
            app=ArchiveDeployer(root)
        self.assertTrue(app.silent_var.get())
        a=self.record();b=self.record('Other Plug-in','OTHER-5678-1234')
        app.activation_records={a['id']:a,b['id']:b}
        app.open_activation(a['id']);window=app.activation_window
        self.assertIn('Deep Glow 2',window.product_label.cget('text'))
        window.copy(window.current_document['values']['serial'])
        self.assertEqual(root.clipboard_get(),'DEMO-1234-5678')
        app.open_activation(b['id']);self.assertIs(app.activation_window,window)
        self.assertIn('Other Plug-in',window.product_label.cget('text'))
        self.assertNotIn('DEMO-1234-5678',json.dumps(window.current_document))
        self.assertFalse(hasattr(window,'text'),'Activation has no README text box')
        root.update()
        for child in app.library_button.master.winfo_children():
            self.assertLessEqual(child.winfo_x()+child.winfo_width(),child.master.winfo_width())
        self.assertEqual(len([w for w in root.winfo_children() if isinstance(w,tk.Toplevel)]),1)

    def test_unknown_engine_opens_manual_fallback_without_inside_ae_readme_popup(self):
        archive=self.root/'magic-bullet.zip'
        with zipfile.ZipFile(archive,'w') as out:
            out.writestr('Key/key.txt','ABCD-1234-5678-90\n\nWXYZ-9876-5432-10\nThanks')
            out.writestr('Setup/setup.exe',b'unknown installer')
        app=object.__new__(ArchiveDeployer);app.events=Queue()
        with patch('ae_archive_deployer.run_installer',return_value=0) as launch:
            app._process_worker([archive],self.root/'Plug-ins',True,script_registry=False,
                                product_names={archive:'Magic Bullet Suite'})
        launch.assert_called_once()
        self.assertFalse(launch.call_args.kwargs['silent'])
        events=list(app.events.queue)
        records=[value for kind,value in events if kind=='activation']
        self.assertEqual(records,[], 'Installer-only keys must not appear in inside-AE activation')
        self.assertFalse(any(kind in {'text_popup','credentials_help'} for kind,_ in events))
        self.assertTrue(any(kind=='setup_help' for kind,_ in events))

    def test_supported_silent_modes_and_unknown_never_guesses(self):
        for marker,engine,flag in [(b'Inno Setup','Inno Setup','/VERYSILENT'),
            (b'BitRock','InstallBuilder','unattended'),(b'NullsoftInst','NSIS','/S'),
            (b'Qt Installer Framework accept-licenses confirm-command','Qt IFW','install')]:
            with self.subTest(engine=engine):
                exe=self.root/'setup.exe';exe.write_bytes(marker)
                command,detected=silent_command(exe)
                self.assertEqual(detected,engine);self.assertIn(flag,command)
        exe.write_bytes(b'unknown')
        self.assertRaises(ValueError,silent_command,exe)
        self.assertIn('/qn',silent_command(self.root/'setup.msi')[0])

    def test_revision_eula_flag_is_added_only_to_revision_installers(self):
        for name in ['RSMB5AE-5.1.8-windows-installer.exe','Twixtor8AE-windows-installer.exe']:
            exe=self.root/name;exe.write_bytes(b'BitRock installer')
            command,_=silent_command(exe)
            self.assertEqual(command[command.index('--acceptEULA')+1],'1')
        other=self.root/'OtherProduct.exe';other.write_bytes(b'BitRock installer')
        self.assertNotIn('--acceptEULA',silent_command(other)[0])
        exe=self.root/'RSMB5AE-installer.exe';exe.write_bytes(b'BitRock installer')
        command,_=silent_command(exe,{'username':'Example User','serial':'DEMO-1234-5678'})
        self.assertEqual(command[command.index('--regName')+1],'Example User')
        self.assertEqual(command[command.index('--regKey')+1],'DEMO-1234-5678')

    def test_dependencies_keep_exact_names_and_back_up_previous_versions(self):
        source=self.root/'source/IrisBlurSDK.dll';source.parent.mkdir();source.write_bytes(b'new SDK')
        target=self.root/'Plug-ins/IrisBlurSDK.dll';target.parent.mkdir();target.write_bytes(b'old SDK')
        with patch.dict(os.environ,{'LOCALAPPDATA':str(self.root/'local')}):
            self.assertTrue(install_binary(source,target))
            self.assertFalse(install_binary(source,target))
        self.assertEqual(target.read_bytes(),b'new SDK')
        self.assertFalse((target.parent/'IrisBlurSDK (2).dll').exists())
        backups=list((self.root/'local').rglob('*.backup'))
        self.assertEqual(len(backups),1);self.assertEqual(backups[0].read_bytes(),b'old SDK')

    def test_readme_only_receipt_does_not_request_automatic_popup(self):
        record=make_record('Deep Glow 2','dg2.zip',['DeepGlow2.aex'],
                           [(self.root/'readme.txt','Copy DeepGlow2.aex and IrisBlurSDK.dll into Plug-ins.')],self.root,True)
        self.assertIsNone(record)
        self.assertTrue(self.record()['auto_open'])

    def test_activation_opens_code_document_instead_of_generic_readme(self):
        root=tk.Tk();self.addCleanup(root.destroy)
        with patch('ae_archive_deployer.ActivationStore') as store:
            store.return_value.records.return_value=[]
            app=ArchiveDeployer(root)
        record=make_record('AE Pixel Sorter 2','pixel.zip',['AEPixelSorter2.aex'],
            [(self.root/'readme.txt','Copy the AEX into Plug-ins.'),
             (self.root/'sn.txt','DEMO*USER*1234567890')],self.root,True)
        app.activation_records[record['id']]=record;app.open_activation(record['id'])
        self.assertEqual(Path(app.activation_window.current_document['name']).name,'sn.txt')
        self.assertEqual(app.activation_window.current_document['values']['serial'],'DEMO*USER*1234567890')

    def test_splash_text_accepts_real_newlines_and_legacy_newline_escape(self):
        root=tk.Tk();self.addCleanup(root.destroy)
        png=io.BytesIO();Image.new('RGBA',(768,512),'black').save(png,format='PNG')
        resource={'name':'AE_SPLASH','language':1033,'size':(768,512),'data':png.getvalue()}
        with patch('splash_customizer.read_splash_resources',return_value=[resource]):
            editor=SplashCustomizer(root,self.root/'AfterFXLib.dll',lambda _:None,lambda _:None)
        editor.custom_text_editor.insert('1.0','HAZZAA\nPLUGINS')
        editor._text_edited();root.update()
        self.assertEqual(editor._current_overlay().text,'HAZZAA\nPLUGINS')
        editor.custom_text_var.set('ONE\\nTWO')
        self.assertEqual(editor.custom_text_editor.get('1.0','end-1c'),'ONE\nTWO')


if __name__=='__main__':unittest.main()
