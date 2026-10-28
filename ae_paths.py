from __future__ import annotations

import os
import re
from pathlib import Path

YEARS = tuple(str(year) for year in range(2026, 2018, -1))

MAJOR_YEARS = {16: '2019', 17: '2020', 18: '2021', 22: '2022',
               23: '2023', 24: '2024', 25: '2025', 26: '2026'}


def registry_installations():

    if os.name != 'nt':
        return []
    import winreg
    found = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\Adobe\After Effects', 0, winreg.KEY_READ | view) as key:
                    for index in range(winreg.QueryInfoKey(key)[0]):
                        version = winreg.EnumKey(key, index)
                        major = re.match(r'(\d+)', version)
                        year = MAJOR_YEARS.get(int(major[1])) if major else None
                        if not year:
                            continue
                        with winreg.OpenKey(key, version) as settings:
                            values = {}
                            for name in ('InstallPath', 'Path', 'PluginInstallPath'):
                                try:
                                    value, kind = winreg.QueryValueEx(settings, name)
                                    if isinstance(value, str) and value:
                                        values[name] = Path(os.path.expandvars(value.strip('"')))
                                except OSError:
                                    pass
                            hosts = [support_directory(path) for path in values.values()]
                            host = next((path for path in hosts if path is not None), None)
                            if host:


                                found.append((year, values.get('PluginInstallPath',host/'Plug-ins'), host))
            except OSError:
                continue
    return found


def support_directory(path: Path) -> Path | None:

    path = Path(path)
    if path.name.casefold() == 'afterfx.exe':
        path = path.parent
    if path.name.casefold() in {'plug-ins', 'plugins', 'scripts'}:
        path = path.parent
    if path.name.casefold() != 'support files':
        path = path / 'Support Files'
    return path if (path / 'AfterFX.exe').is_file() else None


def discover(root: Path, *, registry=None, extra_roots=None):

    roots = [Path(root)]
    if extra_roots is None and os.name == 'nt':
        from ae_organization import local_drives
        roots += [Path(os.environ.get('ProgramW6432', r'C:\Program Files')) / 'Adobe']
        for drive in local_drives():
            roots += [drive/'Adobe', drive/'Program Files'/'Adobe', drive/'Programs'/'Adobe']
    else:
        roots += list(extra_roots or [])
    candidates = list(registry_installations() if registry is None else registry)
    for directory in dict.fromkeys(roots):
        try:
            for folder in directory.iterdir():
                if not folder.is_dir() or not folder.name.casefold().startswith('adobe after effects'):
                    continue
                match = re.search(r'\b(20\d{2})\b', folder.name)
                if match and match[1] in YEARS:
                    candidates.append((match[1], folder))
        except OSError:
            continue
    found = {}
    for candidate in candidates:
        year, folder = candidate[:2]
        support = support_directory(candidate[2] if len(candidate)>2 else folder)
        if year in YEARS and support is not None:
            target = Path(folder) if len(candidate)>2 else support/'Plug-ins'
            found.setdefault(str(target.resolve()).casefold(), (year, target))
    return sorted(found.values(), key=lambda item: (-int(item[0]), str(item[1]).casefold()))


def normalize_target(path: Path) -> Path:

    path = Path(path).expanduser()
    if not path.is_absolute():
        raise ValueError('Choose an absolute target directory.')
    support = support_directory(path)
    return support/'Plug-ins' if support else path


def support_for_target(target):
    support = support_directory(target)
    if support:
        return support
    for candidate in registry_installations():
        if len(candidate)>2 and candidate[1].resolve()==Path(target).resolve():
            return candidate[2]
    return None


def common_plugin_directory(target=None):

    fallback=Path(os.environ.get('ProgramW6432',os.environ.get('ProgramFiles',r'C:\Program Files')))/'Adobe/Common/Plug-ins/7.0/MediaCore'
    if os.name!='nt':
        return fallback
    year=re.search(r'\b(20\d{2})\b',str(target or ''))
    import winreg
    candidates=[]
    for view in (winreg.KEY_WOW64_64KEY,winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'SOFTWARE\Adobe\After Effects',0,winreg.KEY_READ|view) as root:
                for index in range(winreg.QueryInfoKey(root)[0]):
                    version=winreg.EnumKey(root,index)
                    major=re.match(r'(\d+)',version)
                    candidate_year=MAJOR_YEARS.get(int(major[1])) if major else None
                    if not candidate_year or (year and candidate_year!=year[1]):
                        continue
                    try:
                        with winreg.OpenKey(root,version) as key:
                            value,_=winreg.QueryValueEx(key,'CommonPluginInstallPath')
                            path=Path(os.path.expandvars(value.strip('"')))
                            if path.is_absolute():candidates.append((candidate_year,path))
                    except (OSError,TypeError):
                        pass
        except OSError:
            pass
    return sorted(candidates,key=lambda item:item[0],reverse=True)[0][1] if candidates else fallback
