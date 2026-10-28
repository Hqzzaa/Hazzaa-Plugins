import mmap
import re
from pathlib import Path


def silent_command(installer, credentials=None):
    installer=Path(installer)
    if installer.suffix.casefold()=='.msi':
        return ['msiexec','/i',str(installer),'/qn','/norestart'],'MSI'
    with installer.open('rb') as file:
        if installer.stat().st_size==0:
            raise ValueError('Empty installer.')
        with mmap.mmap(file.fileno(),0,access=mmap.ACCESS_READ) as data:


            header=data[:min(len(data),32*1024**2)].lower()
    if b'qt installer framework' in header and b'confirm-command' in header and b'accept-licenses' in header:
        return [str(installer),'--accept-licenses','--default-answer','--confirm-command','install'],'Qt IFW'
    if b'bitrock' in header or b'installbuilder' in header:
        command=[str(installer),'--mode','unattended','--unattendedmodeui','none']


        revision_package=re.match(r'^(rsmb|twixtor|deflicker|denoise|remap|rematch|relens|reflex|regrade|fields?kit|revision)',installer.name,re.I)
        if revision_package:
            command += ['--acceptEULA','1']
            values=(credentials if isinstance(credentials,dict) else getattr(credentials,'values',None)) or {}


            if values.get('username') and values.get('serial'):
                command += ['--regName',values['username'],'--regKey',values['serial']]
        return command,'InstallBuilder'
    if b'inno setup' in header:
        return [str(installer),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-'],'Inno Setup'
    if b'nullsoftinst' in header or b'nullsoft install system' in header:
        return [str(installer),'/S'],'NSIS'
    raise ValueError('Silent installer type is not recognized. Setup was not launched. Uncheck AUTO INSTALL only if you want to run its wizard manually.')
