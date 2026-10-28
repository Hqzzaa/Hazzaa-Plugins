from __future__ import annotations
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import re
from pathlib import Path
import time

from installer_automation import _document_values


def _activation_values(path,content):
    values=_document_values(Path(path),content)


    if not any(key=='serial' or key.startswith('code ') for key in values):
        lines=[line.strip() for line in content.splitlines() if line.strip()]
        candidates=set()
        for before,value in zip(lines,lines[1:]):
            if (re.search(r'(?i)\b(serial|registration code|activation code|licen[cs]e key)\b',before)
                and 6<=len(value)<=512
                and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+/*@=-]*',value)
                and (any(c.isdigit() for c in value) or '-' in value or '*' in value)):
                candidates.add(value)
        if len(candidates)==1:
            values['serial']=candidates
    return {key:next(iter(value)) for key,value in values.items() if len(value)==1}


def activation_only(record):

    if not isinstance(record,dict) or record.get('inside_ae') is not True:
        return None
    docs=[];seen=set()
    for doc in record.get('documents',[]):
        if not isinstance(doc,dict):
            continue
        values=doc.get('values') or _activation_values(doc.get('name',''),doc.get('text',''))
        if not isinstance(values,dict):
            continue
        values={key:value for key,value in values.items()
                if isinstance(value,str) and value and not any(ord(c)<32 for c in value)
                and (key in {'username','email','company','organization','password','serial'} or key.startswith('code '))}
        if not any(key=='serial' or key.startswith('code ') for key in values):
            continue
        identity=tuple(sorted(values.items()))
        if identity in seen:
            continue
        seen.add(identity)
        docs.append({'name':doc.get('name','Code'), 'values':values,
                     'text':'\n'.join(key.upper()+': '+value for key,value in values.items())})
    if not docs:
        return None
    return {**record,'documents':docs,'auto_open':True}


def make_record(product, archive, plugins, documents, root, inside_ae=False):
    docs=[]
    seen=set()
    for path,content in documents:

        if content in seen:
            continue
        seen.add(content)
        values=_activation_values(path,content)
        docs.append({'name':str(path.relative_to(root)), 'text':content,
                     'values':values})
    if not docs:
        return None
    ident=hashlib.sha256((product+'\0'+archive).encode('utf-8')).hexdigest()
    return activation_only({'id':ident,'product':product,'archive':archive,'plugins':sorted(set(plugins)),
            'inside_ae':inside_ae,'documents':docs,'updated':int(time.time()),
            'auto_open':False})


class Blob(ctypes.Structure):
    _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data, decrypt=False):
    if os.name!='nt':
        raise OSError('Activation storage requires Windows data protection.')
    crypt=ctypes.WinDLL('crypt32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.LocalFree.argtypes=[ctypes.c_void_p]
    kernel.LocalFree.restype=ctypes.c_void_p
    source_buffer=ctypes.create_string_buffer(data)
    source=Blob(len(data),ctypes.cast(source_buffer,ctypes.POINTER(ctypes.c_ubyte)))
    output=Blob()
    fn=crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,
                 ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    fn.restype=wintypes.BOOL
    if not fn(ctypes.byref(source),None,None,None,None,1,ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.data,output.size)
    finally:
        kernel.LocalFree(output.data)


class ActivationStore:
    def __init__(self,directory=None):
        self.directory=Path(directory) if directory else Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData'/'Local')))/'Hazzaa Plugins'/'Activation'

    def save(self,record):
        if len(record['id'])!=64 or any(c not in '0123456789abcdef' for c in record['id']):
            raise ValueError('Invalid activation receipt ID.')
        encrypted=_dpapi(json.dumps(record,ensure_ascii=False).encode('utf-8'))
        self.directory.mkdir(parents=True,exist_ok=True)
        target=self.directory/(record['id']+'.bin')
        pending=target.with_suffix('.pending')
        pending.write_bytes(encrypted)
        pending.replace(target)

    def records(self):
        records=[]
        if self.directory.is_dir():
            for path in self.directory.glob('*.bin'):
                try:
                    if path.stat().st_size>16*1024**2:
                        continue
                    value=json.loads(_dpapi(path.read_bytes(),decrypt=True))
                    if isinstance(value,dict) and value.get('id')==path.stem and isinstance(value.get('documents'),list):
                        filtered=activation_only(value)
                        if filtered:
                            records.append(filtered)
                except (OSError,ValueError,KeyError,TypeError):
                    continue
        return sorted(records,key=lambda value:value.get('updated',0),reverse=True)
