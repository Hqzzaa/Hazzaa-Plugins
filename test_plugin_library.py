from test_support import plugin_bytes
import hashlib
import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from queue import Queue
from unittest.mock import patch
import tkinter as tk
import time

from plugin_library import LibraryClient, DownloadCancelled
from ae_archive_deployer import ArchiveDeployer, find_assets, find_cep_extensions
from installer_automation import credentials_for_installer


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.content = b'an archive for download verification'
        self.item = dict(id='fixture', name='Fixture', filename='fixture.zip', kind='Plugin',
                         sha256=hashlib.sha256(self.content).hexdigest(), bytes=len(self.content))

    def test_catalog_validation_and_path_rejection(self):
        client = LibraryClient()
        with patch.object(client, '_json', return_value={'schema':1, 'items':[self.item]}):
            self.assertEqual(client.catalog(), [self.item])
        for field, value in [('filename','../escape.zip'), ('filename','C:\\escape.zip'),
                             ('id','../../x'), ('sha256','broken'), ('bytes',True), ('bytes',0)]:
            with self.subTest(field=field, value=value), patch.object(client, '_json', return_value={'schema':1,'items':[{**self.item,field:value}]}):
                self.assertRaises(ValueError, client.catalog)
        with patch.object(client, '_json', return_value={'schema':1, 'items':[self.item,self.item]}):
            self.assertRaises(ValueError, client.catalog)

    def test_verified_download_and_wrong_hash_cleanup(self):
        client=LibraryClient()
        progress=[]
        with patch.object(client,'_json',return_value={'ticket':'valid-ticket'}), patch('urllib.request.urlopen',return_value=Response(self.content)):
            path=client.download(self.item,self.root,threading.Event(),progress.append)
        self.assertEqual(path.read_bytes(),self.content)
        path.unlink()
        with patch.object(client,'_json',return_value={'ticket':'valid-ticket'}), patch('urllib.request.urlopen',return_value=Response(b'corrupt')):
            self.assertRaises(ValueError,client.download,self.item,self.root,threading.Event(),lambda _:None)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_cancel_during_download_removes_partial_file(self):
        cancel=threading.Event()
        client=LibraryClient()
        def progress(_):cancel.set()
        with patch.object(client,'_json',return_value={'ticket':'valid-ticket'}), patch('urllib.request.urlopen',return_value=Response(self.content)):
            self.assertRaises(DownloadCancelled,client.download,self.item,self.root,cancel,progress)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_extension_preserves_helpers_and_does_not_run_them(self):
        archive=self.root/'panel.zip'
        with zipfile.ZipFile(archive,'w') as out:
            out.writestr('Panel/CSXS/manifest.xml','<ExtensionManifest><HostList><Host Name="AEFT"/></HostList></ExtensionManifest>')
            out.writestr('Panel/jsx/main.jsx','script')
            out.writestr('Panel/bin/ffmpeg.exe','helper')
        app=object.__new__(ArchiveDeployer)
        app.events=Queue()
        with patch.dict('os.environ',{'APPDATA':str(self.root/'roaming')}),patch('subprocess.Popen') as launch:
            app._process_worker([archive],self.root/'Plug-ins',False,script_registry=False)
        launch.assert_not_called()
        self.assertEqual((self.root/'roaming/Adobe/CEP/extensions/Panel/bin/ffmpeg.exe').read_text(),'helper')
        self.assertFalse((self.root/'Scripts/main.jsx').exists())
        messages=[]
        while not app.events.empty():messages.append(app.events.get())
        self.assertFalse(any('ERROR:' in str(value) for _,value in messages),messages)

    def test_nested_ae_archive_and_support_assets_and_duplicate_plugins(self):
        nested=io.BytesIO()
        with zipfile.ZipFile(nested,'w') as out:
            out.writestr('Effect/Effect.aex',plugin_bytes('plugin'))
            out.writestr('Effect/Textures/image.png',b'texture')
            out.writestr('Copy/Effect.aex',plugin_bytes('plugin'))
            out.writestr('__MACOSX/._Effect.aex',b'metadata')
        archive=self.root/'outer.zip'
        with zipfile.ZipFile(archive,'w') as out:
            out.writestr('RSMB AE.zip',nested.getvalue())
            out.writestr('RSMB Vegas.zip',b'not a valid ZIP and must be skipped')
        target=self.root/'Plug-ins'
        target.mkdir()
        app=object.__new__(ArchiveDeployer);app.events=Queue()
        app._process_worker([archive],target,False,script_registry=False)
        self.assertEqual(len(list(target.glob('*.aex'))),1)
        self.assertEqual((target/'Textures/image.png').read_bytes(),b'texture')

    def test_library_ui_selection_search_and_visible_buttons(self):
        root=tk.Tk()
        self.addCleanup(root.destroy)
        app=ArchiveDeployer(root)
        root.geometry('920x650')
        items=[{**self.item,'id':f'fixture-{i}','name':f'Fixture {i}'} for i in range(25)]
        with patch.object(LibraryClient,'catalog',return_value=items):
            app.open_library()
            window=app.library_window
            for _ in range(15):
                root.update()
                if len(window.items)==25:break
                time.sleep(.02)
        self.assertEqual(len(window.items),25)
        window.mark(True)
        self.assertEqual(sum(v.get() for v in window.selected.values()),25)
        window.query.set('Fixture 10')
        self.assertEqual(len(window.rows.winfo_children()),1)
        window.mark(False)
        self.assertFalse(window.selected['fixture-10'].get())
        self.assertEqual(sum(v.get() for v in window.selected.values()),24)
        root.update()
        self.assertTrue(window.install_button.winfo_viewable())
        self.assertLess(window.install_button.winfo_rooty()+window.install_button.winfo_height(),window.root.winfo_rooty()+window.root.winfo_height())
        for button in (app.library_button,):
            self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),root.winfo_rootx()+root.winfo_width())

    def test_legacy_after_code_document_is_read_and_vegas_key_ignored(self):
        archive=self.root/'legacy.zip'
        nested=io.BytesIO()
        with zipfile.ZipFile(nested,'w') as out:
            out.writestr('RSMB5AE-installer.exe',b'BitRock fixture')
        with zipfile.ZipFile(archive,'w') as out:
            out.writestr('RSMB5 AE.zip',nested.getvalue())
            out.writestr('After code.txt','Example User\nDEMO-1234-5678')
            out.writestr('Vegas code.txt','Other User\nWRONG-9999-8888')
            out.writestr('RSMB5 Vegas.zip',b'must not be opened')
        app=object.__new__(ArchiveDeployer);app.events=Queue()
        with patch('ae_archive_deployer.subprocess.Popen') as launch,patch('ae_archive_deployer.wait_with_autofill',return_value=0) as assisted:
            launch.return_value.wait.return_value=0
            app._process_worker([archive],self.root/'Plug-ins',True,script_registry=False)
        self.assertEqual(launch.call_count,1)
        command=launch.call_args.args[0]
        self.assertEqual(Path(command[0]).name,'RSMB5AE-installer.exe')
        self.assertNotIn('/S',command)
        self.assertIn('unattended',command)
        assisted.assert_not_called()
        events=[]
        while not app.events.empty():events.append(app.events.get())
        records=[value for kind,value in events if kind=='activation']
        self.assertEqual(records,[], 'RSMB setup credentials are used by setup, not shown as an AE registration popup')
        self.assertFalse(any(kind in {'text_popup','credentials_help'} for kind,_ in events))
        for kind,value in events:
            if kind=='log':
                self.assertNotIn('DEMO-1234-5678',str(value))
                self.assertNotIn('ERROR:',str(value))
        result=credentials_for_installer([(self.root/'After code.txt','Example User\nDEMO-1234-5678'),(self.root/'Vegas code.txt','Other User\nWRONG-9999-8888')],self.root/'RSMB5AE-installer.exe')
        self.assertEqual(result.values['serial'],'DEMO-1234-5678')


if __name__=='__main__':unittest.main()
