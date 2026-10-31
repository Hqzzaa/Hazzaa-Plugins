from __future__ import annotations

import ctypes
import os
import subprocess
import tempfile
import time
from ctypes import wintypes


def expected_helper_exit(command, image, code):
    return (code == 2 and os.path.basename(command[0]).casefold() ==
            'borisfx continuum 2026.5 adobe v19.5.6.exe' and
            os.path.normcase(os.path.abspath(image or '.')) in
            {os.path.normcase(os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), folder, 'net.exe'))
             for folder in ('System32','SysWOW64')})


class Job:

    def __init__(self, process):
        self.handle = None
        if os.name != 'nt':
            return
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        self.kernel.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)
        self.kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self.kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        self.kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD))
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = self.kernel.CreateJobObjectW(None, None)
        if handle and self.kernel.AssignProcessToJobObject(handle, int(process._handle)):
            self.handle = handle
        elif handle:
            self.kernel.CloseHandle(handle)

    def active(self):
        if not self.handle:
            return 0
        class Accounting(ctypes.Structure):
            _fields_ = [('user', ctypes.c_longlong), ('kernel', ctypes.c_longlong),
                        ('period_user', ctypes.c_longlong), ('period_kernel', ctypes.c_longlong),
                        ('faults', wintypes.DWORD), ('total', wintypes.DWORD),
                        ('active', wintypes.DWORD), ('terminated', wintypes.DWORD)]
        info = Accounting()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.active

    def terminate(self):
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 1460)

    def pids(self):
        if not self.handle:
            return []

        buffer = ctypes.create_string_buffer(8 + 4096 * ctypes.sizeof(ctypes.c_size_t))
        if not self.kernel.QueryInformationJobObject(self.handle, 3, buffer, len(buffer), None):
            raise ctypes.WinError(ctypes.get_last_error())
        count = ctypes.c_uint32.from_buffer(buffer, 4).value
        return [ctypes.c_size_t.from_buffer(buffer, 8 + i*ctypes.sizeof(ctypes.c_size_t)).value for i in range(count)]

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class InstallerExit(int):

    def __new__(cls,code,assisted=False,child_codes=()):
        value=super().__new__(cls,code)
        value.assisted=assisted;value.child_codes=tuple(child_codes)
        return value


class SetupHandoff:

    handed_off=True
    def __init__(self,process,job):
        self.process=process;self.job=job

    def wait_until_closed(self):
        if os.name=='nt' and not self.job.handle:


            self.process.wait()
            raise OSError('Setup child tracking is unavailable.')
        try:
            while self.process.poll() is None or self.job.active():time.sleep(.2)
        finally:self.job.close()


def run_installer(command, directory, *, silent, credentials, log, timeout=1800,
                  on_attention=None,handoff_on_attention=False):

    options = {}
    if silent:
        options['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        if os.name == 'nt':
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            options['startupinfo'] = startup
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(command, cwd=str(directory), shell=False,
                                   stdout=output, stderr=output, **options)
        job = Job(process)
        if os.name == 'nt' and not job.handle:

            log('Process-tree tracking unavailable; installer output verification is required.')
        forms = None
        if os.name == 'nt':
            from installer_automation import WindowsForms
            forms = WindowsForms()
        tracked = {process.pid}
        child_handles = {}
        child_images = {}
        visible_since = None
        needed_ui = False
        transferred=False
        automated_pages=set()
        assistant = None
        if on_attention is not None and forms:
            try:
                from installer_automation import InstallerAssistant, Credentials
                values = credentials if isinstance(credentials,dict) else getattr(credentials,'values',{})
                assistant=InstallerAssistant(process.pid,Credentials(values or {}),log,allow_advance=False)
            except OSError:
                log('Installer form entry is unavailable; manual fields remain visible.')
        child_codes=[]
        deadline = time.monotonic() + timeout
        try:
            if not silent and handoff_on_attention:
                transferred=True
                return SetupHandoff(process,job)
            while process.poll() is None or job.active():
                if job.handle:
                    for pid in job.pids():
                        tracked.add(pid)
                        if pid != process.pid and pid not in child_handles:
                            handle = job.kernel.OpenProcess(0x1000, False, pid)
                            if handle:
                                child_handles[pid] = handle
                                image=ctypes.create_unicode_buffer(32768)
                                length=wintypes.DWORD(len(image))
                                if job.kernel.QueryFullProcessImageNameW(handle,0,image,ctypes.byref(length)):
                                    child_images[pid]=image.value
                if time.monotonic() > deadline:
                    job.terminate()
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=10)
                    raise TimeoutError('Setup exceeded its time limit; installation was not verified.')
                if silent and forms:
                    snapshot = forms.processes()
                    parents = set(tracked)
                    while True:
                        children = {pid for pid, (parent, _) in snapshot.items() if parent in parents} - tracked
                        if not children:
                            break
                        tracked.update(children)
                        parents = children
                    windows = forms.windows(tracked & snapshot.keys())
                    from installer_interaction import advance_bcc_license_page,requires_input
                    advanced=False
                    if handoff_on_attention:
                        for window in windows:
                            if advance_bcc_license_page(forms,window,automated_pages):
                                advanced=True
                                log('Boris: selected installation without a license and clicked Next. This does not activate the product.')
                    visible=not advanced and any(requires_input(forms,window) for window in windows)
                    if assistant is not None and not needed_ui:
                        try:assistant.poll()
                        except Exception:
                            assistant=None
                            log('Installer field entry stopped; complete the visible fields manually.')
                    if visible:
                        visible_since = visible_since or time.monotonic()
                        if not needed_ui and time.monotonic() - visible_since >= (0 if handoff_on_attention else 2):
                            needed_ui = True
                            log('SETUP NEEDS ATTENTION: this installer displayed a window despite its silent flags.')
                            if on_attention is not None:

                                forms.user.ShowWindow.argtypes=(wintypes.HWND,ctypes.c_int)
                                forms.user.SetForegroundWindow.argtypes=(wintypes.HWND,)
                                for window in windows:forms.user.ShowWindow(window,9)
                                if windows:forms.user.SetForegroundWindow(windows[-1])
                                on_attention('visible','The installer requires a manual step. Complete its visible wizard; do not start a second setup.')
                                if handoff_on_attention:
                                    transferred=True
                                    return SetupHandoff(process,job)
                                log('ASSISTED INSTALL: waiting for the existing wizard to finish.')
                    else:
                        visible_since = None
                time.sleep(.1)
            code = process.wait()
            for pid, handle in child_handles.items():
                child_code = wintypes.DWORD()
                if job.kernel.GetExitCodeProcess(handle, ctypes.byref(child_code)) and child_code.value not in (0,1641,3010):
                    child_codes.append(child_code.value)
                    expected=expected_helper_exit(command,child_images.get(pid),child_code.value)
                    identity=os.path.basename(child_images.get(pid,'')) or str(pid)
                    log(f'{"Non-fatal setup helper" if expected else "Setup child process"} ({identity}) exited with code {child_code.value}.')
                    if code in (0,1641,3010) and not expected:
                        code = child_code.value
        except Exception as error:


            try:still_running=process.poll() is None or bool(job.active())
            except OSError:still_running=True
            if still_running:
                error.retry_safe=False
            raise
        finally:
            for handle in child_handles.values():
                job.kernel.CloseHandle(handle)
            if not transferred:job.close()
        if code not in (0, 1641, 3010):
            output.seek(0, 2)
            size = output.tell()
            output.seek(max(0, size - 8000))
            message = output.read().decode('utf-8', errors='replace')
            values = (credentials if isinstance(credentials, dict) else getattr(credentials, 'values', {})) or {}
            for value in sorted(values.values(), key=len, reverse=True):
                if value:
                    message = message.replace(value, '<redacted>')
            if message.strip():
                log('Setup error: '+message.strip())
        if needed_ui and on_attention is None:
            raise RuntimeError('Unattended setup was not achieved. Review the visible installer; no silent-success status was recorded.')
        return InstallerExit(code,needed_ui,child_codes)
