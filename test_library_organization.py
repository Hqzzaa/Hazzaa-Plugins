import json
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from activation_details import ActivationStore, activation_only, make_record
from ae_archive_deployer import ArchiveDeployer
from plugin_library import LibraryClient, available_items, library_category


class LibraryOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)

    def item(self,ident,name,kind='Plugin'):
        return dict(id=ident,name=name,kind=kind,bytes=64,filename=ident+'.zip',sha256='a'*64)

    def test_sections_and_alphabetic_order_exclude_retired_package(self):
        items=[self.item('z','Zebra'),self.item('flow','Flow','Panel'),self.item('alpha','alpha'),
               self.item('instant-4k','Instant 4K'),self.item('auto','AutoSway','Script'),
               self.item('alias','Instant4K')]
        clean=available_items(items)
        self.assertEqual([i['name'] for i in clean],['alpha','AutoSway','Flow','Zebra'])
        self.assertEqual([i['name'] for i in clean if library_category(i)=='Scripts'],['AutoSway','Flow'])
        self.assertEqual(library_category(self.item('setup','Bundle','Installer')),'Plug-ins')

    def test_client_filters_a_legacy_server_catalog(self):
        items=[self.item('z','Zulu'),self.item('instant-4k','Instant 4K'),self.item('a','Alpha')]
        with patch.object(LibraryClient,'_json',return_value={'schema':1,'items':items}):
            self.assertEqual([i['id'] for i in LibraryClient().catalog()],['a','z'])

    def test_ui_sections_preserve_selections_and_select_only_visible_rows(self):
        root=tk.Tk();self.addCleanup(root.destroy)
        with patch('ae_archive_deployer.ActivationStore') as store:
            store.return_value.records.return_value=[]
            app=ArchiveDeployer(root)
        items=[self.item('z','Zebra'),self.item('flow','Flow','Panel'),self.item('a','Alpha'),
               self.item('instant-4k','Instant 4K'),self.item('auto','AutoSway','Script')]
        with patch.object(LibraryClient,'catalog',return_value=items):
            app.open_library();window=app.library_window
            deadline=time.monotonic()+2
            while window.loading and time.monotonic()<deadline:
                root.update();time.sleep(.01)
        self.assertEqual([i['id'] for i in window.visible_items()],['a','z'])
        self.assertNotIn('instant-4k',window.selected)
        window.mark(True)
        window.set_category('Scripts')
        self.assertEqual([i['id'] for i in window.visible_items()],['auto','flow'])
        self.assertTrue(window.selected['a'].get());self.assertFalse(window.selected['flow'].get())
        window.query.set('flow');window.mark(True)
        self.assertTrue(window.selected['flow'].get());self.assertFalse(window.selected['auto'].get())
        window.mark(False);self.assertTrue(window.selected['a'].get())
        window.query.set('');window.root.geometry('660x480');root.update()
        self.assertLessEqual(window.install_button.winfo_rooty()+window.install_button.winfo_height(),
                             window.root.winfo_rooty()+window.root.winfo_height())

    def test_only_inside_ae_codes_are_stored_without_instruction_text(self):
        docs=[(self.folder/'readme.txt','Copy files. Open setup and click Next.'),
              (self.folder/'serial.txt','Username: Example User\nSerial: DEMO-1234-5678\nDelete this instruction body.')]
        self.assertIsNone(make_record('Setup','setup.zip',[],docs,self.folder,False))
        record=make_record('Example Effect','effect.zip',['Example.aex'],docs,self.folder,True)
        self.assertEqual(len(record['documents']),1)
        self.assertEqual(record['documents'][0]['values']['serial'],'DEMO-1234-5678')
        self.assertNotIn('instruction',record['documents'][0]['text'])
        self.assertNotIn('Next',record['documents'][0]['text'])
        self.assertTrue(record['auto_open'])

    def test_following_serial_format_is_detected_but_paths_are_not_codes(self):
        record=make_record('Example Effect','effect.zip',['Example.aex'],
            [(self.folder/'Instruction.txt','Copy the AEX to Plug-ins.\nUse the following serial to activate the software\nDEMO-1234-5678\nexample.org')],self.folder,True)
        self.assertEqual(record['documents'][0]['values']['serial'],'DEMO-1234-5678')
        bad=make_record('Example','effect.zip',['Example.aex'],
            [(self.folder/'readme.txt','Serial installation instructions\nC:\\Program Files\\Adobe\\2026')],self.folder,True)
        self.assertIsNone(bad)

    def test_old_encrypted_receipts_do_not_reintroduce_installer_readmes(self):
        docs=[{'name':'readme.txt','text':'Setup steps and general instructions','values':{}},
              {'name':'sn.txt','text':'unnecessary README text','values':{'serial':'DEMO-1234'}}]
        old={'id':'a'*64,'product':'Example','archive':'old.zip','plugins':['Example.aex'],
             'documents':docs,'inside_ae':True,'updated':1,'auto_open':True}
        setup={**old,'id':'b'*64,'inside_ae':False}
        store=ActivationStore(self.folder/'receipts');store.save(old);store.save(setup)
        records=store.records()
        self.assertEqual(len(records),1)
        self.assertEqual(len(records[0]['documents']),1)
        self.assertEqual(records[0]['documents'][0]['text'],'SERIAL: DEMO-1234')
        self.assertIsNone(activation_only(setup))

    def test_activation_ui_has_no_readme_box_and_hides_installer_receipts(self):
        root=tk.Tk();self.addCleanup(root.destroy)
        with patch('ae_archive_deployer.ActivationStore') as store:
            store.return_value.records.return_value=[]
            app=ArchiveDeployer(root)
        record=make_record('Example','effect.zip',['Example.aex'],
            [(self.folder/'sn.txt','DEMO-1234')],self.folder,True)
        app.activation_records={record['id']:record,'legacy-setup':{**record,'id':'b'*64,'inside_ae':False}}
        app.open_activation(record['id']);window=app.activation_window
        self.assertEqual(window.products.size(),1)
        self.assertFalse(hasattr(window,'text'))
        self.assertEqual(window.current_document['values'],{'serial':'DEMO-1234'})
        window.copy(window.current_document['values']['serial'])
        self.assertEqual(root.clipboard_get(),'DEMO-1234')


if __name__=='__main__':unittest.main()
