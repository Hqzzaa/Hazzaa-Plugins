import hashlib
import json
import os
import queue
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
from installed_products import (DeploymentStore,file_record,record_deployment,installed_inventory,
    remove_recorded_files,safe_file,command_arguments,run_vendor_uninstaller)
from ae_archive_deployer import ArchiveDeployer


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.plugins=self.root/'Host/Plug-ins';self.scripts=self.root/'Host/Scripts'
        self.plugins.mkdir(parents=True);self.scripts.mkdir()
        self.roots={'plugins':[self.plugins],'scripts':[self.scripts],'cep':[self.root/'CEP']}
        self.store=DeploymentStore(self.root/'Installed')
        self.env=patch('installed_products.data_root',return_value=self.root/'Data');self.env.start();self.addCleanup(self.env.stop)

    def write(self,path,content=b'fixture'):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content);return path

    def row(self,name,files,category='Plug-ins',ident=None):
        return {'id':ident or name,'name':name,'category':category,'files':files,'method':'Recorded files'}

    def entry(self,path,root,anchor=True):
        return {'root':'plugins' if root==self.plugins else 'scripts','relative':path.relative_to(root).as_posix(),
            'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'anchor':anchor}

    def inventory(self,index=(),receipts=(),vendors=()):
        return installed_inventory(self.roots,index,receipts=receipts,vendors=vendors)

    def test_receipt_round_trip_merge_and_reinstall_without_duplicate(self):
        effect=self.write(self.plugins/'Effect.aex');asset=self.write(self.plugins/'effect/data.bin')
        self.store.record('Effect','Plug-ins',[file_record(effect,self.plugins)])
        self.store.record('Effect','Plug-ins',[file_record(effect,self.plugins),file_record(asset,self.plugins)])
        records=self.store.records();self.assertEqual(len(records),1);self.assertEqual(len(records[0]['files']),2)
        self.assertNotIn(b'Effect.aex',next(self.store.directory.glob('*.bin')).read_bytes())

    def test_receipts_show_only_present_files_and_sort(self):
        z=self.write(self.plugins/'Z.aex');a=self.write(self.scripts/'Alpha.jsx')
        self.store.record('Zulu','Plug-ins',[file_record(z,self.plugins)])
        self.store.record('Alpha','Scripts',[file_record(a,self.scripts)])
        rows=self.inventory(receipts=self.store.records());self.assertEqual([r['name'] for r in rows],['Alpha','Zulu'])
        z.unlink();rows=self.inventory(receipts=self.store.records());self.assertEqual([r['name'] for r in rows],['Alpha'])

    def test_legacy_index_needs_an_actual_matching_anchor(self):
        effect=self.write(self.plugins/'Effect.aex');asset=self.write(self.plugins/'data.bin')
        package={'id':'effect','name':'Effect Package','files':[self.entry(effect,self.plugins),self.entry(asset,self.plugins,False)]}
        rows=self.inventory([package]);self.assertEqual(rows[0]['name'],'Effect Package');self.assertEqual(len(rows[0]['files']),2)
        effect.unlink();self.assertEqual(self.inventory([package]),[])

    def test_legacy_modified_assets_are_preserved(self):
        effect=self.write(self.plugins/'Effect.aex');asset=self.write(self.plugins/'data.bin')
        package={'id':'effect','name':'Effect','files':[self.entry(effect,self.plugins),self.entry(asset,self.plugins,False)]}
        asset.write_bytes(b'personalized')
        rows=self.inventory([package]);self.assertEqual(len(rows[0]['files']),1)
        remove_recorded_files(rows,rows);self.assertTrue(asset.is_file());self.assertFalse(effect.exists())

    def test_shared_assets_between_two_legacy_packages_are_not_deleted(self):
        a=self.write(self.plugins/'A.aex');b=self.write(self.plugins/'B.aex');shared=self.write(self.plugins/'SDK.dll')
        packages=[{'id':name,'name':name,'files':[self.entry(path,self.plugins),self.entry(shared,self.plugins,False)]} for name,path in [('A',a),('B',b)]]
        rows=self.inventory(packages);self.assertEqual(len(rows),2)
        self.assertTrue(all(len(r['files'])==2 for r in rows))
        remove_recorded_files([rows[0]],rows)
        self.assertTrue(shared.is_file());self.assertTrue(b.is_file());self.assertFalse(a.exists())

    def test_remove_both_owners_removes_shared_file_once(self):
        path=self.write(self.plugins/'shared.dll');record=file_record(path,self.plugins)
        rows=[self.row('A',[record]),self.row('B',[record])]
        backup=remove_recorded_files(rows,rows);self.assertFalse(path.exists())
        self.assertEqual(len(list(backup.glob('*.backup'))),1)

    def test_backup_exact_files_and_preserve_unrelated_folder_contents(self):
        path=self.write(self.plugins/'Package/Effect.aex',b'original');other=self.write(self.plugins/'Package/personal.txt')
        row=self.row('Package',[file_record(path,self.plugins)])
        backup=remove_recorded_files([row],[row]);self.assertFalse(path.exists());self.assertTrue(other.is_file())
        self.assertEqual(next(backup.glob('*.backup')).read_bytes(),b'original')

    def test_empty_owned_subdirectory_removed_but_host_root_retained(self):
        path=self.write(self.plugins/'Package/Effect.aex');row=self.row('Package',[file_record(path,self.plugins)])
        remove_recorded_files([row],[row]);self.assertFalse(path.parent.exists());self.assertTrue(self.plugins.is_dir())

    def test_changed_file_aborts_before_any_removal(self):
        a=self.write(self.plugins/'A.aex');b=self.write(self.plugins/'B.aex')
        row=self.row('Package',[file_record(a,self.plugins),file_record(b,self.plugins)])
        b.write_bytes(b'updated')
        with self.assertRaises(OSError):remove_recorded_files([row],[row])
        self.assertTrue(a.is_file());self.assertTrue(b.is_file())

    def test_readonly_owned_file_removed_with_backup(self):
        path=self.write(self.plugins/'ReadOnly.aex');row=self.row('Package',[file_record(path,self.plugins)]);path.chmod(0o444)
        self.assertIsNotNone(remove_recorded_files([row],[row]));self.assertFalse(path.exists())

    def test_failed_delete_restores_previously_removed_file(self):
        a=self.write(self.plugins/'A.aex');b=self.write(self.plugins/'B.aex')
        row=self.row('Package',[file_record(a,self.plugins),file_record(b,self.plugins)])
        original=Path.unlink
        def unlink(path,*args,**kwargs):
            if path==b:raise PermissionError('Locked file')
            return original(path,*args,**kwargs)
        with patch.object(Path,'unlink',unlink),self.assertRaises(PermissionError):remove_recorded_files([row],[row])
        self.assertTrue(a.is_file());self.assertTrue(b.is_file())

    def test_path_escape_and_root_itself_are_rejected(self):
        for path in [self.plugins,self.root/'Other.aex',self.plugins/'../Other.aex']:
            with self.subTest(path=path),self.assertRaises(ValueError):safe_file(path,self.plugins)

    def test_builtin_adobe_effects_are_omitted(self):
        self.write(self.plugins/'Builtin.aex')
        with patch('installed_products._adobe_builtin',return_value=True):self.assertEqual(self.inventory(),[])

    def test_generic_detected_script_and_plugin_are_separate(self):
        self.write(self.plugins/'Effect.aex');self.write(self.scripts/'Custom.jsxbin');self.write(self.plugins/'Other.dll')
        rows=self.inventory();self.assertEqual({r['category'] for r in rows},{'Plug-ins','Scripts'})
        self.assertEqual(len(rows),2)

    def test_msi_uninstaller_uses_product_code_without_shell(self):
        guid='{12345678-1234-1234-1234-123456789abc}'
        args=command_arguments('MsiExec.exe /I'+guid)
        self.assertEqual(args[1:],['/x',guid,'/qn','/norestart'])
        self.assertIsNone(command_arguments('cmd.exe /c del something'))
        self.assertIsNone(command_arguments('powershell.exe -Command whatever'))
        self.assertIsNone(command_arguments('MsiExec.exe /I invalid'))

    def test_vendor_uninstaller_quoted_path_and_arguments(self):
        exe=self.write(self.root/'Vendor Folder/uninstall.exe',b'MZfixture')
        command='"'+str(exe)+'" /quiet'
        self.assertEqual(command_arguments(command),[str(exe),'/quiet'])
        row={'name':'Vendor','command':[str(exe),'/quiet']}
        with patch('installed_products.subprocess.Popen') as popen:
            popen.return_value.wait.return_value=0
            run_vendor_uninstaller(row)
            self.assertFalse(popen.call_args.kwargs['shell'])
        with patch('installed_products.subprocess.Popen') as popen:
            popen.return_value.wait.return_value=5
            with self.assertRaises(OSError):run_vendor_uninstaller(row)

    def test_vendor_row_takes_precedence_over_manual_suite_files(self):
        path=self.write(self.plugins/'Continuum Plug-ins/BCC_Blur.aex')
        vendor={'id':'vendor','name':'Boris Continuum 12','category':'Plug-ins','family':'bcc-continuum-2019','files':[],
                'command':['uninstall.exe'],'method':'Vendor uninstaller (all hosts)'}
        rows=self.inventory(vendors=[vendor]);self.assertEqual(len(rows),1);self.assertEqual(len(rows[0]['files']),1)

    def test_record_plugin_script_and_panel_companions(self):
        effect=self.write(self.plugins/'Effect.aex');script=self.write(self.scripts/'Panel.jsx')
        cep=self.write(self.root/'CEP/Extension/CSXS/manifest.xml')
        record_deployment('Example',[(effect,self.plugins)],[(script,self.scripts)],
            [(cep.parent.parent,self.root/'CEP')],store=self.store)
        rows=self.inventory(receipts=self.store.records());self.assertEqual(len(rows),2)
        self.assertEqual(sorted(len(r['files']) for r in rows),[1,2])

    def test_uninstall_ui_categories_selection_and_footer(self):
        root=tk.Tk();self.addCleanup(root.destroy)
        with patch('ae_archive_deployer.ActivationStore') as store:
            store.return_value.records.return_value=[];app=ArchiveDeployer(root)
        rows=[self.row('Alpha',[],ident='a'),self.row('Zulu',[],'Scripts','z')]
        with patch('uninstall_ui.installed_inventory',return_value=rows):
            app.open_uninstall();window=app.uninstall_window
            deadline=time.monotonic()+3
            while window.running and time.monotonic()<deadline:root.update();time.sleep(.01)
        self.assertEqual([r['id'] for r in window.visible()],['a'])
        window.selected['a'].set(True);window.set_category('Scripts')
        self.assertTrue(window.selected['a'].get());self.assertEqual([r['id'] for r in window.visible()],['z'])
        window.query.set('missing');self.assertEqual(window.visible(),[])
        window.root.geometry('700x480');root.update()
        self.assertLessEqual(window.remove_button.winfo_rooty()+window.remove_button.winfo_height(),window.root.winfo_rooty()+window.root.winfo_height())
        self.assertGreater(app.uninstall_button.winfo_y(),root.winfo_height()*.9)
        window.close()


if __name__=='__main__':unittest.main()
