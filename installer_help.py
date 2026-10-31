import hashlib
import os
import shutil
import stat
import tempfile
import threading
from pathlib import Path
from installer_automation import _document_values


class ExtractionWorkspace:

    def __init__(self):
        self.name=tempfile.mkdtemp(prefix='ae_archive_');self.keep=False
        self._lock=threading.Lock();self._leases=0;self._owner_closed=False;self._cleaned=False
    def __enter__(self):return self.name
    def __exit__(self,*_):
        with self._lock:
            self._owner_closed=True
            self._cleanup_if_unused()

    def retain(self):
        with self._lock:self._leases+=1

    def release(self):
        with self._lock:
            self._leases=max(0,self._leases-1)
            self._cleanup_if_unused()

    def _cleanup_if_unused(self):
        if self._owner_closed and not self._leases and not self.keep and not self._cleaned:
            path=Path(self.name).resolve()
            if path.parent!=Path(tempfile.gettempdir()).resolve() or not path.name.startswith('ae_archive_'):
                raise ValueError('Unexpected extraction cleanup path.')
            def writable(function,target,error):
                os.chmod(target,stat.S_IREAD|stat.S_IWRITE)
                function(target)
            shutil.rmtree(path,onexc=writable)
            self._cleaned=True


def help_values(documents,credentials,installer=None):

    values={key:[value] for key,value in getattr(credentials,'values',{}).items()}
    for path,content in documents:
        path=Path(path)
        if installer is not None:
            parent=Path(installer).parent
            keys=(path.parent.name.casefold() in {'key','keys','serial','serials','license','licenses','registration'}
                  and path.parent.parent in parent.parents)
            if path.parent!=parent and path.parent not in parent.parents and not keys:continue
            if 'vegas' in path.stem.casefold() and 'vegas' not in Path(installer).stem.casefold():continue
        if path.stem.casefold() not in {'sn','key','keys','serial','serials','license','licence','registration','credentials'}:continue
        parsed=_document_values(path,content)
        for kind,candidates in parsed.items():
            values.setdefault(kind,[]).extend(v for v in sorted(candidates) if v not in values.get(kind,[]))
        if not parsed:
            import re
            tokens=[line.strip() for line in content.splitlines()]
            tokens=[v for v in tokens if 10<=len(v)<=512 and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+/*@=-]*',v)
                    and any(c.isdigit() for c in v) and any(c.isalpha() for c in v)]
            values.setdefault('serial',[]).extend(v for v in tokens if v not in values.get('serial',[]))
    return {key:candidates for key,candidates in values.items() if candidates}


def setup_steps(name,reason=''):
    name=name.casefold()
    steps=[]
    if 'magic_bullet' in name or 'magic bullet' in name:
        steps=['1. Product rows already filled? Click Next.',
               '2. Otherwise paste your serial, click Submit, then Next.',
               '3. Click Install, then Finish.']
    elif name=='continuum_2019_adobe_12_0_0_windows.exe':
        steps=['1. Choose Complete installation without entering a license, then Next.',
               '2. Finish Setup. OPEN FILES and OPEN LIB show the two folders.']
    elif 'continuum' in name:
        steps=['Complete this installer only if you selected manual installation.',
               'This package installs its own files. No external file copying is required.']
    elif 'sapphire' in name:
        steps=['1. Complete Setup: Next > Install > Finish.',
               '2. Restart After Effects when Setup finishes.']
    else:
        steps=['1. Complete Setup: Next > Install > Finish.',
               '2. Use COPY below if Setup asks for registration details.']
    if '1638' in reason or 'another version' in reason.casefold():
        steps.insert(0,'Version conflict (1638): choose Repair/Modify if Setup offers it.')
    return '\n'.join(steps)


def bcc_attachment_folder(root):

    from product_deployment import BCC_LIBRARIES
    root=Path(root)
    serials=[p for p in root.rglob('*') if p.is_dir() and p.name.casefold()=='serial']
    if serials:return sorted(serials,key=lambda p:(-len(p.parts),str(p).casefold()))[0]
    groups={}
    names={name.casefold() for name in BCC_LIBRARIES}
    for path in root.rglob('*'):
        if path.is_file() and not path.is_symlink() and path.name.casefold() in names:
            groups.setdefault(path.parent,set()).add(path.name.casefold())
    if not groups:return root
    return sorted(groups,key=lambda p:(-len(groups[p]),-sum(part.casefold()=='serial' for part in p.parts),-len(p.parts),str(p).casefold()))[0]


def preserve_bcc_attachments(extracted,archive):

    extracted,archive=Path(extracted),Path(archive)
    if not any(p.name.casefold()=='continuum_2019_adobe_12_0_0_windows.exe' for p in extracted.rglob('*.exe')):
        return None
    candidates=[p for p in extracted.rglob('*') if p.is_file() and p.suffix.casefold() not in {'.exe','.msi','.zip','.rar','.cmd','.bat','.ps1','.vbs'} and p.name!='.extracted']
    if not candidates:return None
    with archive.open('rb') as stream:ident=hashlib.file_digest(stream,'sha256').hexdigest()
    root=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'Hazzaa Plugins/Installer Files'/('BCC-'+ident[:16])
    root.mkdir(parents=True,exist_ok=True)
    for source in candidates:
        relative=source.relative_to(extracted)
        if any(part in {'.','..'} for part in relative.parts) or source.is_symlink():continue
        target=root/relative
        from app_updater import _no_links
        _no_links(target)
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():
            with source.open('rb') as a,target.open('rb') as b:
                if hashlib.file_digest(a,'sha256').digest()==hashlib.file_digest(b,'sha256').digest():continue
        pending=target.with_suffix(target.suffix+'.pending')
        shutil.copyfile(source,pending)
        pending.replace(target)
    return root
