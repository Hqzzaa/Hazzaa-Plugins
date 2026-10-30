from pathlib import Path, PurePosixPath
import os
import re
import struct
import xml.etree.ElementTree as ET

from app_updater import _no_links
from deployment_plan import Copy, deploy_files, digest

INSTALLER = 'nitrate.pp-3.56b.exe'
UTILITIES = {'FCUtilities_v3.exe', 'FCUtilities_Helper_v2.exe', 'FCGatherLogs_v3.bat'}
DIRECTORIES = {'Profiles', 'Profiles/5D', 'ResourcesNITRATE', 'ResourcesNITRATE/v2ViewLUTs'}


def read_qt_manifest(path):
    data = Path(path).read_bytes()
    if not 4 <= len(data) <= 1024 * 1024:
        raise ValueError('Invalid FilmConvert resource manifest size.')
    count = struct.unpack_from('>I', data)[0]
    if not 0 < count <= 5000:
        raise ValueError('Invalid FilmConvert resource manifest count.')
    offset, paths = 4, []
    for _ in range(count):
        if offset + 4 > len(data):
            raise ValueError('Truncated FilmConvert resource manifest.')
        length = struct.unpack_from('>I', data, offset)[0]
        offset += 4
        if not 0 < length <= 8192 or length % 2 or offset + length > len(data):
            raise ValueError('Invalid FilmConvert resource path length.')
        text = data[offset:offset + length].decode('utf-16-be')
        offset += length
        prefix = '@RELOCATABLE_PATH@/'
        if not text.startswith(prefix):
            raise ValueError('Unexpected FilmConvert resource path.')
        text = text[len(prefix):]
        parts = text.split('/')
        if any(not p or p in {'.', '..'} or p[-1] in '. ' or
               re.search(r'[\\:\x00-\x1f<>"|?*]', p) or
               re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', p)
               for p in parts):
            raise ValueError('Unsafe FilmConvert resource path.')
        if text in paths:
            raise ValueError('Duplicate FilmConvert resource path.')
        paths.append(text)
    if offset != len(data):
        raise ValueError('Unexpected FilmConvert resource manifest trailer.')
    return paths


def runtime_destination():
    return Path(os.environ.get('ProgramData', r'C:\ProgramData')) / 'RubberMonkey'


def reusable_installation(installer, shared_root):
    if Path(installer).name.casefold() != INSTALLER or find_installation() is None:
        return False
    host = Path(shared_root) / 'FilmConvertNitrate.aex'
    if not host.is_file():
        return False
    _no_links(host)
    from deployment_plan import validate_plugin
    try:
        validate_plugin(host)
    except (OSError, ValueError):
        return False
    return True


def _candidates():
    candidates = []
    if os.name == 'nt':
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', 0, winreg.KEY_READ | view) as base:
                        for index in range(winreg.QueryInfoKey(base)[0]):
                            try:
                                with winreg.OpenKey(base, winreg.EnumKey(base, index)) as key:
                                    name = winreg.QueryValueEx(key, 'DisplayName')[0]
                                    if not isinstance(name, str) or 'nitrate' not in name.casefold():
                                        continue
                                    for field in ('InstallLocation', 'InstallPath', 'UninstallString', 'DisplayIcon'):
                                        try:
                                            location = winreg.QueryValueEx(key, field)[0]
                                        except OSError:
                                            continue
                                        if not isinstance(location, str) or not location.strip():
                                            continue
                                        location = os.path.expandvars(location.strip())
                                        if field in {'UninstallString', 'DisplayIcon'}:
                                            match = re.match(r'^"([^"]+)"|^(.*?\.exe)(?:,\d+|\s+-|\s+/|$)', location, re.I)
                                            if not match:
                                                continue
                                            path = Path(match[1] or match[2]).parent
                                        else:
                                            path = Path(location.strip('"'))
                                        if path.is_absolute():
                                            candidates += [path, path / 'PremierePro']
                            except OSError:
                                continue
                except OSError:
                    continue
    base = Path(os.environ.get('ProgramW6432', os.environ.get('ProgramFiles', r'C:\Program Files')))
    candidates.append(base / 'FilmConvert/PremierePro')
    return list(dict.fromkeys(candidates))


def find_installation(program_root=None):
    for candidate in ([Path(program_root)] if program_root is not None else _candidates()):
        _no_links(candidate)
        component_file = candidate / 'components.xml'
        if not component_file.is_file():
            continue
        _no_links(component_file)
        if component_file.stat().st_size > 1024 * 1024:
            raise ValueError('FilmConvert component metadata is too large.')
        tree = ET.parse(component_file)
        packages = {p.findtext('Name'): p.findtext('Version') for p in tree.findall('Package')}
        if packages.get('com.filmconvert.plugin') == '3.56' and packages.get('com.filmconvert.resources') == '3.56':
            return candidate
    return None


def runtime_plan(program_root=None, data_root=None):
    source = find_installation(program_root)
    if source is None:
        raise OSError('FilmConvert Nitrate 3.56 runtime installation was not found. Complete its installer first.')
    target = Path(data_root) if data_root is not None else runtime_destination()
    _no_links(target)
    copies, required = [], []
    metadata = source / 'installerResources/com.filmconvert.resources'
    for group in ('Profiles', 'ResourcesNITRATE'):
        manifest = metadata / ('3.56' + group + '.txt')
        _no_links(manifest)
        paths = read_qt_manifest(manifest)
        group_files = []
        for relative in paths:
            if PurePosixPath(relative).parts[0] != group:
                raise ValueError('Unexpected FilmConvert resource group.')
            if relative in DIRECTORIES:
                continue
            suffixes = {'.tin'} if group == 'Profiles' else {'.bin', '.tun', '.emograin', '.xml', '.rtf'}
            if Path(relative).suffix.casefold() not in suffixes:
                raise ValueError('Unsupported FilmConvert runtime file: ' + relative)
            group_files.append(relative)
        if not group_files:
            raise OSError('FilmConvert resource manifest has no files: ' + group)
        for relative in group_files:
            src, dst = source / relative, target / relative
            _no_links(src)
            _no_links(dst)
            required.append(dst)
            if src.is_file() and src.stat().st_size:
                copies.append(Copy(src, dst))
            elif not dst.is_file() or not dst.stat().st_size:
                raise OSError('FilmConvert runtime file is missing: ' + relative)
    manifest = metadata / '3.56content.txt'
    _no_links(manifest)
    utilities = read_qt_manifest(manifest)
    if set(utilities) != UTILITIES | {'post_install.bat'}:
        raise ValueError('Unexpected FilmConvert support utility manifest.')
    for name in sorted(UTILITIES):
        src, dst = source / name, target / name
        _no_links(src)
        _no_links(dst)
        required.append(dst)
        if src.is_file() and src.stat().st_size:
            copies.append(Copy(src, dst))
        elif not dst.is_file() or not dst.stat().st_size:
            raise OSError('FilmConvert support utility is missing: ' + name)
    return copies, required


def needs_repair(program_root=None, data_root=None):
    if find_installation(program_root) is None:
        return False
    copies, required = runtime_plan(program_root, data_root)
    target = Path(data_root) if data_root is not None else runtime_destination()
    return any(not (target / name).is_dir() for name in ('Licenses', 'CMProfiles')) or any(not p.is_file() or not p.stat().st_size for p in required) or any(
        not item.destination.is_file() or item.destination.stat().st_size != item.source.stat().st_size
        for item in copies)


def complete_runtime(installer, copy_file, *, program_root=None, data_root=None, log=lambda message: None):
    if Path(installer).name.casefold() != INSTALLER:
        return []
    copies, required = runtime_plan(program_root, data_root)
    target = Path(data_root) if data_root is not None else runtime_destination()
    for name in ('Licenses', 'CMProfiles'):
        folder = target / name
        _no_links(folder)
        folder.mkdir(parents=True, exist_ok=True)
    changed = deploy_files(copies, copy_file)
    for item in copies:
        if not item.destination.is_file() or digest(item.source) != digest(item.destination):
            raise OSError('FilmConvert runtime verification failed: ' + item.destination.name)
    if any(not p.is_file() or not p.stat().st_size for p in required):
        raise OSError('FilmConvert runtime installation is incomplete.')
    log(f'FilmConvert support files verified: {len(required)}; repaired: {len(changed)}; destination: {target}')
    return required
