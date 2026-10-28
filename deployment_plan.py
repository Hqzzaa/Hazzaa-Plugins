from __future__ import annotations

import hashlib
import re
import struct
import shutil
import tempfile
import stat
from dataclasses import dataclass
from pathlib import Path
import pefile


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_plugin(path):

    path = Path(path)
    with path.open('rb') as stream:
        header = stream.read(64)
        if len(header) < 64 or header[:2] != b'MZ':
            raise ValueError(f'{path.name}: not a Windows plug-in executable.')
        offset = struct.unpack_from('<I', header, 60)[0]
        if offset > path.stat().st_size - 24:
            raise ValueError(f'{path.name}: truncated executable header.')
        stream.seek(offset)
        pe = stream.read(24)
        if pe[:4] != b'PE\0\0' or struct.unpack_from('<H', pe, 4)[0] != 0x8664:
            raise ValueError(f'{path.name}: After Effects 2019-2026 needs a Windows x64 AEX.')
        sections, optional_size = struct.unpack_from('<H', pe, 6)[0], struct.unpack_from('<H', pe, 20)[0]
        stream.seek(optional_size, 1)
        for _ in range(sections):
            section = stream.read(40)
            if len(section) != 40:
                raise ValueError(f'{path.name}: truncated section table.')
            size, start = struct.unpack_from('<II', section, 16)
            if size and start + size > path.stat().st_size:
                raise ValueError(f'{path.name}: truncated executable section.')
    try:
        pe = pefile.PE(str(path), fast_load=True)
        try:
            pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_RESOURCE']])
            resources = getattr(pe, 'DIRECTORY_ENTRY_RESOURCE', None)
            pipl = [entry for entry in resources.entries if str(entry.name).casefold() == 'pipl'] if resources else []
            if not pipl:
                raise ValueError(f'{path.name}: missing Adobe PiPL resource; host loading cannot be verified.')
            found = False
            for entry in pipl:
                for name in entry.directory.entries:
                    for language in name.directory.entries:
                        data = language.data.struct
                        offset = pe.get_offset_from_rva(data.OffsetToData)
                        if not data.Size or offset + data.Size > path.stat().st_size:
                            raise ValueError(f'{path.name}: damaged Adobe PiPL resource.')
                        found = True
            if not found:
                raise ValueError(f'{path.name}: empty Adobe PiPL resource.')
        finally:
            pe.close()
    except (pefile.PEFormatError, AttributeError, KeyError, IndexError) as error:
        raise ValueError(f'{path.name}: invalid Windows plug-in resources.') from error


@dataclass(frozen=True)
class Copy:
    source: Path
    destination: Path


def plugin_plan(plugins, target, excluded=lambda _p: False):

    planned = {}
    for asset in sorted(plugins, key=lambda p: (len(p.parts), str(p).casefold())):
        directory = Path(target)/'Effects' if asset.name.casefold() == 'signal.aex' else Path(target)
        files = [(asset, directory/asset.name)]
        for sibling in asset.parent.iterdir():
            if sibling == asset or excluded(sibling):
                continue
            if sibling.is_file() and sibling.suffix.casefold() in {'.dll', '.dat', '.bin', '.json', '.ofp', '.png', '.jpg', '.jpeg', '.lic', '.key'}:
                files.append((sibling, directory/sibling.name))
            elif sibling.is_dir():
                children = [p for p in sibling.rglob('*') if p.is_file() and not excluded(p)]

                if any(p.suffix.casefold() in {'.aex', '.exe', '.msi', '.jsx', '.jsxbin', '.jsxinc', '.zip', '.rar'} for p in children):
                    continue
                files += [(p, directory/sibling.name/p.relative_to(sibling)) for p in children]
        for source, destination in files:
            key = str(destination).casefold()
            previous = planned.get(key)
            if previous:
                if digest(previous.source) != digest(source):
                    raise ValueError(f'Ambiguous archive: different files target {destination.name}. Select one product version.')
            else:
                planned[key] = Copy(source, destination)

    return sorted(planned.values(), key=lambda item: (item.destination.suffix.casefold() == '.aex', str(item.destination).casefold()))


def numbered_duplicates(target, names):

    names = {name.casefold() for name in names}
    for path in Path(target).rglob('*'):
        if path.is_file() and path.suffix.casefold() in {'.aex', '.dll'}:
            canonical = re.sub(r' \(\d+\)$', '', path.stem) + path.suffix
            if canonical.casefold() in names and canonical.casefold() != path.name.casefold():
                yield path


def obsolete_plugin_copies(target, copies):

    destinations = {str(item.destination.resolve()).casefold() for item in copies}
    names = {item.destination.name.casefold() for item in copies if item.destination.suffix.casefold() == '.aex'}
    for path in Path(target).rglob('*'):
        if not path.is_file() or path.suffix.casefold() != '.aex':
            continue
        canonical = re.sub(r' \(\d+\)$', '', path.stem) + path.suffix
        if canonical.casefold() in names and str(path.resolve()).casefold() not in destinations:
            yield path


PANEL_SCRIPTS = {'autosway.jsxbin', 'autosway1.91.jsxbin', 'afphairrig_03_51.jsx',
                 'auto nulls 2064.jsxbin', 'createlayersfaster.jsx'}


def script_plan(scripts, folders, target):

    files = []
    for script in scripts:
        panel = script.name.casefold() in PANEL_SCRIPTS
        directory = Path(target)/'ScriptUI Panels' if panel else Path(target)
        files.append(Copy(script, directory/script.name))

        for sibling in script.parent.iterdir():
            if sibling.is_dir() and (sibling.name.casefold().endswith(('_ffx', '_assets', '_resources'))
                                     or sibling.name.casefold() in {'assets', 'resources', 'lib', 'libs', 'images', 'icons'}):
                files += [Copy(p, directory/sibling.name/p.relative_to(sibling))
                          for p in sibling.rglob('*') if p.is_file()]
    for folder in folders:
        destination = Path(target) if folder.name.casefold() == 'scripts' else Path(target)/('ScriptUI Panels' if folder.name.casefold() in {'scriptui', 'panels', 'scriptui panels'} else folder.name)
        files += [Copy(p, destination/p.relative_to(folder)) for p in folder.rglob('*')
                  if p.is_file() and p.suffix.casefold() not in {'.exe', '.msi', '.zip', '.rar', '.reg'}]
    planned = {}
    for item in files:
        key = str(item.destination).casefold()
        if key in planned and digest(planned[key].source) != digest(item.source):
            raise ValueError('Conflicting script files: '+item.destination.name)
        planned.setdefault(key, item)
    return list(planned.values())


def deploy_files(copies, copy_file):

    changed = []
    attempted = []
    with tempfile.TemporaryDirectory(prefix='hazzaa_rollback_') as temp:
        originals = {}
        for index,item in enumerate(copies):
            destination=item.destination
            if (destination.is_file() and destination.stat().st_mode & stat.S_IWRITE
                    and digest(destination)==digest(item.source)):
                continue
            original=None
            if destination.exists():
                original=Path(temp)/str(index)
                shutil.copy2(destination,original)
            originals[destination]=original
            changed.append(item)
        try:
            for item in changed:
                attempted.append(item.destination)
                copy_file(item.source,item.destination)
        except Exception as error:
            rollback_errors=[]
            for destination in reversed(attempted):
                try:
                    original=originals[destination]
                    if original is None:
                        destination.unlink(missing_ok=True)
                    else:
                        copy_file(original,destination)
                except OSError:
                    rollback_errors.append(str(destination))
            if rollback_errors:
                raise OSError('Deployment failed; Windows also blocked rollback of: '+', '.join(rollback_errors)) from error
            raise OSError('Deployment failed; previous loose files were restored: '+str(error)) from error
    return {item.destination for item in changed}
