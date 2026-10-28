import os
import re
import json
from pathlib import Path

BCC_LIBRARIES=('Continuum_3DObjects_AE.dll','Continuum_AE_16Bit.dll',
               'Continuum_AE_8Bit.dll','Continuum_AE_Float.dll')


def program_files():
    return Path(os.environ.get('ProgramW6432',os.environ.get('ProgramFiles',r'C:\Program Files')))


def bcc_registry_candidates():

    if os.name!='nt':return []
    import winreg
    found=[]
    for hive in (winreg.HKEY_LOCAL_MACHINE,winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY,winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive,r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',0,winreg.KEY_READ|view) as base:
                    for index in range(winreg.QueryInfoKey(base)[0]):
                        try:
                            with winreg.OpenKey(base,winreg.EnumKey(base,index)) as key:
                                name=winreg.QueryValueEx(key,'DisplayName')[0]
                                if not isinstance(name,str):continue
                                if 'continuum' not in name.casefold():continue
                                try:version=winreg.QueryValueEx(key,'DisplayVersion')[0]
                                except OSError:version=''
                                if not (str(version).startswith('12.') or '2019' in name or re.search(r'\b12\b',name)):continue
                                for value in ('InstallLocation','InstallPath','InstallDir','UninstallString','DisplayIcon'):
                                    try:text=winreg.QueryValueEx(key,value)[0]
                                    except OSError:continue
                                    if not isinstance(text,str) or not text.strip():continue
                                    text=os.path.expandvars(text.strip())
                                    if value in {'UninstallString','DisplayIcon'}:
                                        text=text.split('"',2)[1] if text.startswith('"') else text.split(' /',1)[0].split(',',1)[0]
                                        path=Path(text).parent
                                    else:path=Path(text.strip('"'))
                                    if path.is_absolute():found.append(path)
                        except (OSError,ValueError,TypeError):continue
            except OSError:continue
    return list(dict.fromkeys(found))


def _bcc_location_file():
    return Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'Hazzaa Plugins/Settings/bcc12-location.json'


def bcc_library_directory(*,registry=None,extra_roots=()):

    roots=[]
    try:
        value=json.loads(_bcc_location_file().read_text())
        saved=Path(value['path'])
        if saved.is_absolute() and saved.name.casefold()=='lib' and saved.is_dir():return saved
    except (OSError,ValueError,KeyError,TypeError):pass
    roots+=list(bcc_registry_candidates() if registry is None else registry)
    roots+=list(extra_roots)
    roots.append(program_files()/'BorisFX/ContinuumAE/12')
    seen=set()
    for root in roots:
        root=Path(root)
        for candidate in (root,root/'lib',root/'12/lib',root/'ContinuumAE/12/lib',root/'BorisFX/ContinuumAE/12/lib'):
            key=str(candidate.absolute()).casefold()
            if key in seen:continue
            seen.add(key)
            if candidate.name.casefold()=='lib' and candidate.is_dir() and any((candidate/name).is_file() for name in BCC_LIBRARIES):
                return candidate.absolute()
    return None


def choose_bcc_library(folder):

    folder=Path(folder).absolute()
    library=None

    for candidate in (folder,folder/'lib',folder/'12/lib',folder/'ContinuumAE/12/lib',folder/'BorisFX/ContinuumAE/12/lib'):
        if candidate.name.casefold()=='lib' and candidate.is_dir():
            library=candidate;break
    if library is None:raise ValueError('Select the Boris lib folder or its parent folder.')
    path=_bcc_location_file();path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'path':str(library)}),encoding='utf-8')
    return library


def complete_bcc12(extracted,installer,copy_binary,shared_root=None):

    if installer.name.casefold()!='continuum_2019_adobe_12_0_0_windows.exe':
        return []
    base=program_files()
    library=bcc_library_directory()
    if library is None:
        raise OSError('BCC library folder was not found. Use BROWSE LIB in Setup Assistance.')
    vendor=library.parent
    shared=(Path(shared_root) if shared_root is not None else base/'Adobe/Common/Plug-ins/7.0/MediaCore')/'Continuum Plug-ins'


    source=vendor/'plugins64'/'Continuum Plug-ins'
    if source.is_dir():
        for file in source.rglob('*'):
            if file.is_file():copy_binary(file,shared/file.relative_to(source))
    effects=list(shared.rglob('*.aex')) if shared.is_dir() else []
    if not effects:
        raise OSError('BCC setup finished but no Continuum AEX was found in the Adobe shared Plug-ins directory.')
    outputs=[shared]
    for name in BCC_LIBRARIES:


        destination=library/name
        if not destination.is_file() or destination.stat().st_size == 0:
            raise OSError('BCC is missing a required vendor library: '+name)
        outputs.append(destination)
    return outputs


def pixel_sorter_hint(plugin_names):
    if any(name.casefold() in {'aepixelsorter.aex','aepixelsorter2.aex'} for name in plugin_names):
        return 'Restart After Effects, then look under Effects > Pixel Sorting. Registration is separate from file installation.'
    return None


def verify_rsmb5_outputs(installer,shared_root=None):

    if installer.name.casefold()!='rsmb5ae-5.1.8-windows-installer.exe':
        return []
    directory=(Path(shared_root) if shared_root is not None else program_files()/'Adobe/Common/Plug-ins/7.0/MediaCore')/'RSMB5AE'
    outputs=[directory/name for name in ('RSMB_64.aex','RSMBPro_64.aex','RSMBProVectorInput_64.aex')]
    missing=[path.name for path in outputs if not path.is_file() or path.stat().st_size==0]
    if missing:
        raise OSError('RSMB setup finished but required host files are missing: '+', '.join(missing))
    return outputs


def verify_known_setup_outputs(installer,shared_root=None):

    shared=Path(shared_root) if shared_root is not None else program_files()/'Adobe/Common/Plug-ins/7.0/MediaCore'
    name=installer.name.casefold()
    if name=='magic_bullet_suite_13.0.11_setup.exe':
        expected=['Magic Bullet Colorista/Colorista-IV.aex','Magic Bullet Cosmo/MBCosmo-II.aex',
                  'Magic Bullet Denoiser/Denoiser-III.aex','Magic Bullet Mojo/MBMojo-II.aex',
                  'Magic Bullet Renoiser/MBRenoiser.aex','MBFilm/MBFilm.aex','MBLooks/Looks3_64.aex']
        outputs=[shared/item for item in expected]
    elif name=='boris fx sapphire ae 2020.02 ce.exe' or (name=='setup.exe' and installer.parent.name.casefold()=='sapphire'):
        outputs=list((shared/'Sapphire Plug-ins').rglob('*.aex'))
    elif name=='bao plugins bundle 2025.5.exe':
        outputs=list((shared/'BAO').rglob('*.aex'))
    elif name=='nitrate.pp-3.56b.exe':
        outputs=[p for p in shared.rglob('*.aex') if 'nitrate' in str(p.relative_to(shared)).casefold()]
    else:
        return None
    if not outputs or any(not p.is_file() or p.stat().st_size==0 for p in outputs):
        raise OSError('Setup finished but its expected Adobe host files are missing: '+installer.name)
    from deployment_plan import validate_plugin
    for path in outputs:
        validate_plugin(path)
    return outputs
