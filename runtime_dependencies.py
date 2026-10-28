import json
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path

import pefile
from installer_runner import run_installer

VC14_URL = 'https://aka.ms/vc14/vc_redist.x64.exe'
VC14_NAMES = {'msvcp140.dll','vcruntime140.dll','vcruntime140_1.dll','vcomp140.dll',
              'msvcp140_1.dll','msvcp140_2.dll','concrt140.dll'}


def imports(path):
    pe = pefile.PE(str(path),fast_load=True)
    try:
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT'],
                                             pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT']])
        return {entry.dll.decode('ascii',errors='replace').casefold()
                for name in ('DIRECTORY_ENTRY_IMPORT','DIRECTORY_ENTRY_DELAY_IMPORT')
                for entry in getattr(pe,name,[])}
    finally:
        pe.close()


def missing_runtime_files(plugins, system_directory=None):
    system = Path(system_directory or (Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32'))
    required = set()
    for plugin in plugins:
        required.update(imports(plugin) & (VC14_NAMES | {'vulkan-1.dll'}))
    return {name for name in required if not (system/name).is_file()}


def microsoft_signed(path):

    script = "$ErrorActionPreference='Stop'; Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security'); $s=Get-AuthenticodeSignature -LiteralPath $env:HAZZAA_VERIFY_FILE; @{valid=($s.Status -eq 'Valid');subject=$s.SignerCertificate.Subject}|ConvertTo-Json -Compress"
    env = dict(os.environ, HAZZAA_VERIFY_FILE=str(Path(path).resolve()))


    env.pop('PSModulePath',None)
    powershell=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(powershell),'-NoProfile','-NonInteractive','-Command',script],
                            env=env,capture_output=True,text=True,timeout=90,
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),check=True)
    value = json.loads(result.stdout)
    return bool(value.get('valid') and 'O=Microsoft Corporation' in (value.get('subject') or ''))


def download_vc14(destination):
    request = urllib.request.Request(VC14_URL,headers={'User-Agent':'Hazzaa-Plugins/2026'})
    with urllib.request.urlopen(request,timeout=60) as response, Path(destination).open('wb') as output:
        total = 0
        while block := response.read(1024*1024):
            total += len(block)
            if total > 64*1024**2:
                raise ValueError('Visual C++ download exceeded its expected size limit.')
            output.write(block)
    if not microsoft_signed(destination):
        raise ValueError('Visual C++ download did not have a valid Microsoft signature. It was not run.')


def ensure_prerequisites(plugins, log, *, automatic=True):
    if os.name != 'nt' or not plugins:
        return
    missing = missing_runtime_files(plugins)
    if missing & VC14_NAMES:
        if not automatic:
            raise OSError('Missing Microsoft Visual C++ x64 runtime: '+', '.join(sorted(missing & VC14_NAMES)))
        log('Installing missing Microsoft Visual C++ x64 runtime from Microsoft...')
        with tempfile.TemporaryDirectory(prefix='hazzaa_vc14_') as temp:
            installer = Path(temp)/'vc_redist.x64.exe'
            download_vc14(installer)
            code = run_installer([str(installer),'/install','/quiet','/norestart'],installer.parent,
                                 silent=True,credentials={},log=log,timeout=600)
            if code not in (0,1638,1641,3010,0x80070666):
                raise OSError(f'Microsoft Visual C++ setup failed (code {code}).')
        missing = missing_runtime_files(plugins)
    if missing:
        reason = 'Update the GPU driver to supply its Vulkan runtime.' if 'vulkan-1.dll' in missing else 'Runtime installation did not provide the required DLLs.'
        raise OSError('Missing prerequisite: '+', '.join(sorted(missing))+'. '+reason)
    log('Required Visual C++ / Vulkan runtime files are present.')
