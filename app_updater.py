from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from build_info import VERSION
from update_trust import MODULUS_B64, EXPONENT

UPDATE_ORIGIN = 'https://hazzaa.space'
MANIFEST_URL = UPDATE_ORIGIN + '/hazzaa-updates/v1/manifest.json'
MAX_MANIFEST = 32768


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}\.\d{1,2}\.\d{1,2}\.\d{1,5}', value):
        raise ValueError('Invalid update version.')
    return tuple(map(int, value.split('.')))


def _verify_signature(data: bytes, signature: bytes, modulus_b64=None, exponent_value=None):

    if os.name != 'nt':
        raise OSError('Windows is required to verify app updates.')
    modulus = base64.b64decode(MODULUS_B64 if modulus_b64 is None else modulus_b64, validate=True)
    number = EXPONENT if exponent_value is None else exponent_value
    exponent = number.to_bytes((number.bit_length()+7)//8, 'big')
    if len(signature) != len(modulus):
        raise ValueError('Invalid update signature.')
    blob = struct.pack('<6I', 0x31415352, len(modulus)*8, len(exponent), len(modulus), 0, 0)+exponent+modulus
    cng = ctypes.WinDLL('bcrypt')
    handle, key = ctypes.c_void_p(), ctypes.c_void_p()
    cng.BCryptOpenAlgorithmProvider.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    cng.BCryptImportKeyPair.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
    cng.BCryptVerifySignature.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
    cng.BCryptDestroyKey.argtypes = [ctypes.c_void_p]
    cng.BCryptCloseAlgorithmProvider.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    class Padding(ctypes.Structure):
        _fields_ = [('algorithm', ctypes.c_wchar_p)]
    try:
        if cng.BCryptOpenAlgorithmProvider(ctypes.byref(handle), 'RSA', None, 0) != 0:
            raise OSError('Cannot open Windows signature verifier.')
        if cng.BCryptImportKeyPair(handle, None, 'RSAPUBLICBLOB', ctypes.byref(key), blob, len(blob), 0) != 0:
            raise OSError('Cannot load the publisher public key.')
        digest = hashlib.sha256(data).digest()
        if cng.BCryptVerifySignature(key, ctypes.byref(Padding('SHA256')), digest, len(digest), signature, len(signature), 2) != 0:
            raise ValueError('Update signature verification failed.')
    finally:
        if key: cng.BCryptDestroyKey(key)
        if handle: cng.BCryptCloseAlgorithmProvider(handle, 0)


def verify_manifest(envelope):
    if not isinstance(envelope, dict) or set(envelope) != {'payload', 'signature'}:
        raise ValueError('Invalid update manifest.')
    try:
        data = base64.b64decode(envelope['payload'], validate=True)
        signature = base64.b64decode(envelope['signature'], validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError('Invalid update signature encoding.') from error
    if not 0 < len(data) <= MAX_MANIFEST:
        raise ValueError('Update manifest is too large.')
    _verify_signature(data, signature)
    release = json.loads(data)
    if not isinstance(release, dict) or release.get('schema') != 1 or release.get('app') != 'hazzaa-plugins':
        raise ValueError('Unsupported app update.')
    version_tuple(release.get('version'))
    if (type(release.get('bytes')) is not int or not 1024 <= release['bytes'] <= 512*1024**2
            or not isinstance(release.get('sha256'), str) or not re.fullmatch('[a-f0-9]{64}', release['sha256'])
            or release.get('path') != '/hazzaa-updates/v1/files/'+release['version']+'/Hazzaa-Plugins.exe'
            or not isinstance(release.get('notes', ''), str) or len(release.get('notes', '')) > 3000):
        raise ValueError('Invalid update metadata.')
    return release


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != 'https':
            raise ValueError('An insecure update redirect was rejected.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url):
    request = urllib.request.Request(url, headers={'User-Agent':'HazzaaPlugins/'+VERSION, 'Cache-Control':'no-cache'})
    return urllib.request.build_opener(_HttpsOnly()).open(request, timeout=20)


def check_update():
    with _open(MANIFEST_URL) as response:
        data = response.read(MAX_MANIFEST+1)
    if len(data) > MAX_MANIFEST:
        raise ValueError('Update response is too large.')
    envelope = json.loads(data)
    release = verify_manifest(envelope)
    return (envelope, release) if version_tuple(release['version']) > version_tuple(VERSION) else None


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_file(path, release):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size != release['bytes'] or file_hash(path) != release['sha256']:
        raise ValueError('Update download verification failed. The installed app was not changed.')
    with path.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise ValueError('The update is not a Windows executable.')


def updates_root():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'AppData/Local')))/'Hazzaa Plugins/Updates'


def _no_links(path):

    for part in (path, *path.parents):
        if part.exists() and (part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction())):
            raise ValueError('Linked app/update paths are not supported.')


def prepare_update(envelope, target, progress=lambda percent: None):
    release = verify_manifest(envelope)
    if version_tuple(release['version']) <= version_tuple(VERSION):
        raise ValueError('This update is not newer than the running app.')
    target = Path(target).absolute()
    _no_links(target)
    if target.suffix.casefold() != '.exe' or not target.is_file():
        raise ValueError('The running Windows EXE could not be located.')

    with tempfile.NamedTemporaryFile(prefix='.hazzaa-update-probe-', dir=target.parent):
        pass
    root = updates_root(); root.mkdir(parents=True, exist_ok=True); _no_links(root)
    stage = Path(tempfile.mkdtemp(prefix='update-', dir=root))
    candidate = stage/'new.exe'
    try:
        digest, total = hashlib.sha256(), 0
        with _open(UPDATE_ORIGIN+release['path']) as response, candidate.open('wb') as output:
            while chunk := response.read(256*1024):
                total += len(chunk)
                if total > release['bytes']:
                    raise ValueError('Update download exceeds its signed size.')
                digest.update(chunk); output.write(chunk)
                progress(min(100, int(total*100/release['bytes'])))
            output.flush(); os.fsync(output.fileno())
        verify_file(candidate, release)


        helper = stage/'updater.exe'
        shutil.copyfile(target, helper)
        old_hash = file_hash(target)
        if file_hash(helper) != old_hash:
            raise ValueError('Could not prepare the update helper.')
        job = {'schema':1, 'target':str(target), 'old_sha256':old_hash, 'parent_pid':os.getpid(),
               'envelope':envelope, 'token':uuid.uuid4().hex}
        job_path = stage/'job.json'
        job_path.write_text(json.dumps(job), encoding='utf-8')
        return job_path
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def load_job(job_path):
    path = Path(job_path).absolute()
    root = updates_root().absolute()
    _no_links(path)
    if path.name != 'job.json' or path.parent.parent != root or not path.parent.name.startswith('update-'):
        raise ValueError('Invalid update working directory.')
    if path.stat().st_size > 65536:
        raise ValueError('Invalid update job size.')
    job = json.loads(path.read_text(encoding='utf-8'))
    if (job.get('schema') != 1 or not isinstance(job.get('target'), str)
            or not re.fullmatch('[a-f0-9]{64}', str(job.get('old_sha256', '')))
            or not re.fullmatch('[a-f0-9]{32}', str(job.get('token', '')))
            or type(job.get('parent_pid')) is not int or job['parent_pid'] <= 0):
        raise ValueError('Invalid update job.')
    target = Path(job['target'])
    _no_links(target)
    if not target.is_absolute() or target.suffix.casefold() != '.exe' or target.parent == path.parent:
        raise ValueError('Invalid update target.')
    release = verify_manifest(job['envelope'])
    return path, job, target, release


def independent_environment():
    return {**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT':'1'}


def cleanup_updates():

    root = updates_root()
    if not root.is_dir():
        return
    _no_links(root)
    finished = []
    for stage in root.glob('update-*'):
        try:
            _no_links(stage)
            result = stage/'result.json'
            if stage.is_dir() and result.is_file():
                finished.append((result.stat().st_mtime, stage, json.loads(result.read_text(encoding='utf-8'))))
        except (OSError, ValueError):
            continue
    successes = sorted((row for row in finished if row[2].get('ok')), reverse=True)
    newest = successes[0][1] if successes else None
    for modified, stage, _ in finished:
        if stage != newest and time.time()-modified > 7*86400:
            shutil.rmtree(stage, ignore_errors=True)


def launch_helper(job_path):
    path, _, _, _ = load_job(job_path)
    return subprocess.Popen([str(path.parent/'updater.exe'), '--apply-update', str(path)],
        cwd=str(path.parent), env=independent_environment(), close_fds=True)


def _atomic_replace(candidate, target, timeout=75, cancel_file=None):
    deadline = time.monotonic()+timeout
    while True:
        if cancel_file is not None and cancel_file.exists():
            raise OSError('The update was cancelled before the app exited.')
        try:
            os.replace(candidate, target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise OSError('Windows is still using the app EXE. Close other Hazzaa windows and try again.')
            time.sleep(.25)


def apply_update(job_path, *, launch=None, timeout=75):

    path, job, target, release = load_job(job_path)
    stage = path.parent
    result_path = stage/'result.json'
    installed = False
    backup = stage/'previous.exe'
    started = None
    try:
        if file_hash(target) != job['old_sha256']:
            raise ValueError('The original EXE changed while the update was downloading.')
        verify_file(stage/'new.exe', release)
        shutil.copyfile(target, backup)
        if file_hash(backup) != job['old_sha256']:
            raise ValueError('The app backup could not be verified.')


        replacement = target.parent/('.hazzaa-'+job['token']+'.new')
        shutil.copyfile(stage/'new.exe', replacement)
        verify_file(replacement, release)
        (stage/'ready').write_text(job['token'], encoding='ascii')
        _atomic_replace(replacement, target, timeout, stage/'cancel')
        installed = True
        verify_file(target, release)
        launcher = launch or (lambda args: subprocess.Popen(args, cwd=str(target.parent), env=independent_environment(), close_fds=True))
        started = launcher([str(target), '--update-health', str(path)])
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            health = stage/'healthy'
            if health.is_file() and health.read_text(encoding='ascii') == job['token']:
                result_path.write_text(json.dumps({'ok':True, 'version':release['version']}), encoding='utf-8')
                (stage/'new.exe').unlink(missing_ok=True)
                return True
            if started is not None and started.poll() is not None:
                break
            time.sleep(.2)
        raise OSError('The new app did not confirm startup; restoring the previous version.')
    except Exception as error:
        rollback = False
        if installed and backup.is_file() and file_hash(backup) == job['old_sha256']:
            if started is not None and started.poll() is None:
                started.terminate()
                try: started.wait(timeout=10)
                except subprocess.TimeoutExpired: started.kill(); started.wait(timeout=10)
            restore = target.parent/('.hazzaa-'+job['token']+'.restore')
            shutil.copyfile(backup, restore)
            _atomic_replace(restore, target, timeout)
            rollback = file_hash(target) == job['old_sha256']
            if rollback and launch is None:
                subprocess.Popen([str(target)], cwd=str(target.parent), env=independent_environment(), close_fds=True)
        result_path.write_text(json.dumps({'ok':False, 'rollback':rollback, 'error':str(error)}), encoding='utf-8')
        raise
    finally:
        for suffix in ('new', 'restore'):
            (target.parent/('.hazzaa-'+job['token']+'.'+suffix)).unlink(missing_ok=True)


def acknowledge_update(job_path):
    path, job, target, release = load_job(job_path)
    if release['version'] != VERSION or Path(sys.executable).resolve() != target.resolve():
        raise ValueError('The restarted app does not match this update.')
    verify_file(target, release)
    (path.parent/'healthy').write_text(job['token'], encoding='ascii')


def helper_main(job_path):
    try:
        apply_update(job_path)
        return 0
    except Exception as error:
        ctypes.windll.user32.MessageBoxW(None, str(error), 'HAZZAA UPDATE', 0x10)
        return 1
