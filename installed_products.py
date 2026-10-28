from __future__ import annotations
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path
import pefile
from activation_details import _dpapi

SCRIPT_EXT={'.jsx','.jsxbin','.jsxinc'}
VENDORS=[('magic-bullet','Magic Bullet',r'magic\s+bullet'),
         ('bcc-continuum-2019','BCC Continuum',r'\bcontinuum\b'),
         ('sapphire','Sapphire',r'\bsapphire\b'),
         ('rsmb','RSMB',r'\brsmb\b|reel\s*smart'),
         ('nitrate','FilmConvert Nitrate',r'filmconvert.*nitrate|\bnitrate\b'),
         ('bao','BAO',r'\bbao\s+plugins?\b')]


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def data_root():
    return Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'Hazzaa Plugins'


def safe_file(path,root):
    path,root=Path(os.path.abspath(path)),Path(os.path.abspath(root))
    if path==root or root not in path.parents:
        raise ValueError('Uninstall path escapes its recorded installation folder.')
    for part in (path,*path.parents):
        if part.exists() and (part.is_symlink() or (hasattr(part,'is_junction') and part.is_junction())):
            raise ValueError('Linked installation paths cannot be removed automatically.')
    if path.is_file():return path
    return None


def file_record(path,root,expected=None):
    path=safe_file(path,root)
    if path is None:return None
    return {'path':str(path),'root':str(Path(root).absolute()),'sha256':expected or digest(path)}


class DeploymentStore:
    def __init__(self,directory=None):self.directory=Path(directory) if directory else data_root()/'Installed'

    def records(self):
        result=[]
        for path in self.directory.glob('*.bin'):
            try:
                if path.stat().st_size>16*1024**2:continue
                record=json.loads(_dpapi(path.read_bytes(),decrypt=True))
                if (record.get('id')==path.stem and isinstance(record.get('name'),str)
                    and record.get('category') in {'Plug-ins','Scripts'} and isinstance(record.get('files'),list)):
                    result.append(record)
            except (OSError,ValueError,KeyError,TypeError):continue
        return result

    def record(self,name,category,files):
        if not files:return


        ident=hashlib.sha256((name+'\0'+category+'\0'+files[0]['root']).encode()).hexdigest()
        previous=next((r for r in self.records() if r['id']==ident),None)
        merged={f['path'].casefold():f for f in (previous or {}).get('files',[])}
        merged.update({f['path'].casefold():f for f in files})
        record={'id':ident,'name':name,'category':category,'files':list(merged.values()),'updated':int(time.time())}
        self.directory.mkdir(parents=True,exist_ok=True)
        pending=self.directory/(ident+'.pending')
        pending.write_bytes(_dpapi(json.dumps(record).encode()))
        pending.replace(self.directory/(ident+'.bin'))


def record_deployment(name,plugin_copies,script_copies,extensions=(),store=None):
    store=store or DeploymentStore()
    for category,copies in [('Plug-ins',plugin_copies),('Scripts',script_copies)]:
        files=[]
        for path,root in copies:
            record=file_record(path,root)
            if record:files.append(record)
        if category=='Scripts':
            for folder,root in extensions:
                for path in Path(folder).rglob('*'):
                    if path.is_file():
                        record=file_record(path,root)
                        if record:files.append(record)
        store.record(name,category,files)


def command_arguments(command):

    shell=ctypes.WinDLL('shell32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32')
    shell.CommandLineToArgvW.argtypes=[ctypes.c_wchar_p,ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype=ctypes.POINTER(ctypes.c_wchar_p)
    kernel.LocalFree.argtypes=[ctypes.c_void_p]
    count=ctypes.c_int();argv=shell.CommandLineToArgvW(command,ctypes.byref(count))
    if not argv:raise ValueError('Invalid vendor uninstall command.')
    try:args=[argv[i] for i in range(count.value)]
    finally:kernel.LocalFree(argv)
    if not args:return None
    exe=Path(os.path.expandvars(args[0]))
    if exe.name.casefold()=='msiexec.exe':
        ids=[re.search(r'\{[a-f0-9-]{36}\}',arg,re.I) for arg in args[1:]]
        guid=next((m.group() for m in ids if m),None)
        if not guid:return None
        return [str(Path(os.environ.get('WINDIR',r'C:\Windows'))/'System32/msiexec.exe'),'/x',guid,'/qn','/norestart']
    if (not exe.is_absolute() or not exe.is_file() or exe.suffix.casefold()!='.exe'
        or exe.name.casefold() in {'cmd.exe','powershell.exe','pwsh.exe','rundll32.exe','wscript.exe','cscript.exe'}):return None
    return [str(exe),*args[1:]]


def vendor_uninstallers():
    if os.name!='nt':return []
    import winreg
    result=[];seen=set()
    for hive in (winreg.HKEY_LOCAL_MACHINE,winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY,winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive,r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',0,winreg.KEY_READ|view) as base:
                    for i in range(winreg.QueryInfoKey(base)[0]):
                        try:
                            with winreg.OpenKey(base,winreg.EnumKey(base,i)) as key:
                                name=winreg.QueryValueEx(key,'DisplayName')[0]
                                family=next((f for f in VENDORS if re.search(f[2],name,re.I)),None)
                                if not family:continue
                                command=None
                                for value in ('QuietUninstallString','UninstallString'):
                                    try:command=command_arguments(winreg.QueryValueEx(key,value)[0])
                                    except (OSError,ValueError,TypeError):continue
                                    if command:break
                                ident='vendor-'+hashlib.sha256((name+str(command)).encode()).hexdigest()
                                if command and ident not in seen:
                                    seen.add(ident);result.append({'id':ident,'name':name,'category':'Plug-ins',
                                        'files':[],'command':command,'family':family[0],'method':'Vendor uninstaller (all hosts)'})
                        except (OSError,ValueError,TypeError):continue
            except OSError:continue
    return result


def _adobe_builtin(path):
    try:
        pe=pefile.PE(str(path),fast_load=True)
        try:
            pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_RESOURCE']])
            for group in getattr(pe,'FileInfo',[]):
                for info in group:
                    for table in getattr(info,'StringTable',[]):
                        company=table.entries.get(b'CompanyName',b'').decode(errors='ignore').casefold()
                        if 'adobe' in company:return True
        finally:pe.close()
    except (OSError,pefile.PEFormatError):pass
    return False


def _files_below(root):

    root=Path(root)
    if not root.is_dir():return
    for current,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=[name for name in dirs if not Path(current,name).is_symlink() and not Path(current,name).is_junction()]
        for name in files:
            path=Path(current,name)
            if not path.is_symlink():yield path


def installed_inventory(roots,package_index=(),receipts=None,vendors=None):

    rows=list(vendor_uninstallers() if vendors is None else vendors)
    covered=set()

    for receipt in (DeploymentStore().records() if receipts is None else receipts):
        files=[]
        for record in receipt['files']:
            try:
                if safe_file(record['path'],record['root']):files.append(record)
            except (OSError,ValueError):continue
        if files:
            rows.append({**receipt,'files':files,'method':'Recorded files + companions'})
            covered.update(f['path'].casefold() for f in files)


    for package in package_index:
        for root_kind in ('plugins','scripts','cep'):
            descriptions=[f for f in package['files'] if f['root']==root_kind]
            if not descriptions:continue
            for root in roots.get(root_kind,[]):
                files=[];anchor=False
                for entry in descriptions:
                    path=Path(root)/entry['relative']
                    try:
                        candidate=safe_file(path,root)
                        if candidate and (not entry.get('anchor') or str(candidate).casefold() not in covered) and candidate.stat().st_size==entry['bytes'] and digest(candidate)==entry['sha256']:
                            files.append(file_record(candidate,root,entry['sha256']))
                            anchor=anchor or entry.get('anchor',False)
                    except (OSError,ValueError):continue
                if anchor:
                    ident=hashlib.sha256((package['id']+str(root)).encode()).hexdigest()
                    rows.append({'id':ident,'name':package['name'],'category':'Scripts' if root_kind!='plugins' else 'Plug-ins',
                        'files':files,'method':'Verified library files + companions'})
                    covered.update(f['path'].casefold() for f in files)


    for kind in ('plugins','scripts','cep'):
        for root in roots.get(kind,[]):
            for path in _files_below(root):
                if str(path.absolute()).casefold() in covered:continue
                extension=path.suffix.casefold()
                is_plugin=kind=='plugins' and extension=='.aex'
                is_script=kind=='scripts' and extension in SCRIPT_EXT and 'startup' not in (p.casefold() for p in path.relative_to(root).parts[:-1])
                if not (is_plugin or is_script):continue
                try:
                    if is_plugin and _adobe_builtin(path):continue
                    if is_script and extension=='.jsx' and re.search(r'copyright[^\n]{0,100}adobe',path.read_text(errors='ignore')[:8192],re.I):continue
                    record=file_record(path,root)
                except (OSError,ValueError):continue
                if record:
                    ident=hashlib.sha256(str(path.absolute()).encode()).hexdigest()
                    rows.append({'id':ident,'name':path.stem,'category':'Plug-ins' if is_plugin else 'Scripts',
                        'files':[record],'method':'Detected file only; untracked companions are preserved'})
                    covered.add(record['path'].casefold())


    for vendor in [r for r in rows if r.get('command')]:
        pattern=next(f[2] for f in VENDORS if f[0]==vendor['family'])
        for row in list(rows):
            if row is not vendor and not row.get('command') and (re.search(pattern,row['name'],re.I) or any(re.search(pattern,f['path'],re.I) for f in row['files'])):
                vendor['files']+=row['files'];rows.remove(row)
    return sorted(rows,key=lambda r:(r['name'].casefold(),r['id']))


def remove_recorded_files(selected,all_rows,log=lambda message:None):

    selected_ids={r['id'] for r in selected}
    shared={f['path'].casefold() for row in all_rows if row['id'] not in selected_ids for f in row['files']}
    records={f['path'].casefold():f for row in selected for f in row['files'] if not row.get('command')}
    records={key:f for key,f in records.items() if key not in shared}
    planned=[]
    for record in records.values():
        path=safe_file(record['path'],record['root'])
        if not path:continue
        if digest(path)!=record['sha256']:
            raise OSError('A recorded file changed after installation or scanning; it was preserved: '+str(path))
        planned.append((path,record))
    if not planned:return None
    backup_root=data_root()/'Removed'/uuid.uuid4().hex
    backup_root.mkdir(parents=True)
    saved=[];removed=[]
    try:
        for index,(path,record) in enumerate(planned):
            backup=backup_root/(str(index)+'-'+path.name+'.backup')
            shutil.copyfile(path,backup)
            if digest(backup)!=record['sha256']:raise OSError('Uninstall backup verification failed.')
            saved.append((path,backup,record))
        (backup_root/'files.json').write_text(json.dumps([{'original':str(p),'backup':str(b),'sha256':r['sha256']} for p,b,r in saved],indent=2))
        for path,backup,record in saved:
            if digest(path)!=record['sha256']:raise OSError('A file changed during uninstall: '+str(path))
            if not path.stat().st_mode & stat.S_IWRITE:path.chmod(0o644)
            path.unlink();removed.append((path,backup,record));log('REMOVED: '+str(path))
    except Exception:
        for path,backup,_ in reversed(removed):
            if not path.exists():shutil.copyfile(backup,path)
        raise

    for path,_,record in saved:
        parent=path.parent;root=Path(record['root'])
        while parent!=root and root in parent.parents:
            try:parent.rmdir()
            except OSError:break
            parent=parent.parent
    if shared:log('Shared files used by unselected products were preserved.')
    log('Uninstall recovery backup: '+str(backup_root))
    return backup_root


def run_vendor_uninstaller(row,log=lambda message:None):

    args=command_arguments(subprocess.list2cmdline(row['command']))
    if args!=row['command']:raise ValueError('The vendor uninstaller is no longer available.')
    process=subprocess.Popen(args,shell=False)
    try:code=process.wait(timeout=1800)
    except subprocess.TimeoutExpired:raise OSError('The vendor uninstaller is still running; complete its window before trying again.')
    if code not in (0,1641,3010):raise OSError('Vendor uninstaller exited with code '+str(code))
    log('Vendor uninstaller completed: '+row['name'])
