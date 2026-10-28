from __future__ import annotations

import ctypes
import os
import re
import time
import unicodedata
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path


ALIASES = {
    "username": {"user", "username", "name", "fullname", "registeredname", "registeredto",
                 "registrationname", "licensedto", "login", "اسم", "الاسم", "اسمالمستخدم"},
    "email": {"email", "emailaddress", "useremail", "البريد", "البريدالالكتروني"},
    "password": {"password", "pass", "passwd", "pwd", "registrationpassword",
                 "confirmpassword", "repeatpassword", "كلمةالمرور", "الباسورد", "باسورد"},
    "serial": {"serial", "serialnumber", "serialno", "sn", "key", "license", "licence",
               "licensekey", "licencekey", "registrationkey", "registrationcode",
               "activationkey", "activationcode", "productkey", "unlockcode", "regcode", "code",
               "السيريال", "سيريال", "مفتاحالتفعيل", "مفتاحالترخيص", "الرقمالتسلسلي"},
    "company": {"company", "companyname", "organization", "organisation", "الشركة", "اسمالشركة"},
}


def normalized_label(text: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


def field_kind(label: str) -> str | None:
    normalized = normalized_label(label)
    for suffix in ("required", "optional"):
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)]
    for prefix in ("pleaseenteryour", "enteryour", "pleaseenter", "enter", "your"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    for kind, aliases in ALIASES.items():
        if normalized in aliases:
            return kind
    return None


def read_document(path: Path) -> str:

    with path.open("rb") as stream:
        data = stream.read(2 * 1024 * 1024)
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("mbcs" if os.name == "nt" else "cp1252", errors="replace")


@dataclass
class Credentials:

    values: dict[str, str] = field(default_factory=dict, repr=False)
    conflicts: tuple[str, ...] = ()


def _document_values(path: Path, content: str) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    pending: str | None = None
    explicit_username = False
    for line in content.splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line:
            continue
        match = re.match(r"^(?:[-*•]\s*)?([^:=]{1,65})\s*[:=]\s*(.*?)\s*$", line)
        if match:
            kind = field_kind(match[1])
            if kind == "username" and normalized_label(match[1]) not in {"name", "fullname"}:
                explicit_username = True
            value = match[2].strip()
            pending = kind if kind and not value else None
            if not kind or not value:
                continue
        elif pending:
            kind, value, pending = pending, line, None
        else:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value and len(value) <= 1024 and not any(ord(char) < 32 for char in value):
            result.setdefault(kind, set()).add(value)


    nonempty = [line.strip() for line in content.splitlines() if line.strip()]
    credential_document = path.stem.casefold()=='sn' or any(word in path.stem.casefold() for word in ("serial", "license", "licence", "key", "code"))
    if not result and len(nonempty) == 1 and credential_document:
        value = nonempty[0]
        if 6 <= len(value) <= 512 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+/*@=-]*", value):
            result["serial"] = {value}


    if not result and len(nonempty) == 2 and credential_document:
        name, value = nonempty
        if (2 <= len(name) <= 100 and not any(char in name for char in ":=")
            and 6 <= len(value) <= 512 and any(char.isdigit() for char in value)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+/-]*", value)):
            result = {"username": {name}, "serial": {value}}

    if set(result) == {"username"} and not explicit_username:
        result.pop("username")
    return result


def credentials_for_installer(documents: list[tuple[Path, str]], installer: Path) -> Credentials:

    candidates: dict[str, tuple[int, set[str]]] = {}
    for path, content in documents:


        document_vegas = "vegas" in path.stem.casefold()
        installer_vegas = "vegas" in installer.stem.casefold()
        if document_vegas and not installer_vegas:
            continue
        if installer_vegas and "after" in path.stem.casefold():
            continue
        common = 0
        for left, right in zip(path.parent.parts, installer.parent.parts):
            if left.casefold() != right.casefold():
                break
            common += 1
        distance = len(path.parent.parts) + len(installer.parent.parts) - 2 * common

        sibling_keys=(path.parent.name.casefold() in {'key','keys','serial','serials','license','licenses','registration'}
                      and path.parent.parent in installer.parent.parents)
        if path.parent != installer.parent and path.parent not in installer.parent.parents and not sibling_keys:
            continue
        for kind, values in _document_values(path, content).items():
            previous = candidates.get(kind)
            if previous is None or distance < previous[0]:
                candidates[kind] = (distance, set(values))
            elif distance == previous[0]:
                previous[1].update(values)
    return Credentials(
        {kind: next(iter(values)) for kind, (_, values) in candidates.items() if len(values) == 1},
        tuple(sorted(kind for kind, (_, values) in candidates.items() if len(values) > 1)),
    )


@dataclass
class Control:
    handle: int
    parent: int
    class_name: str
    text: str = field(repr=False)
    rect: tuple[int, int, int, int]
    style: int

    @property
    def is_edit(self) -> bool:
        return "edit" in self.class_name.casefold()


class WindowsForms:


    def __init__(self):
        if os.name != "nt":
            raise OSError("Installer autofill is available on Windows only.")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumWindows.argtypes = (self.callback_type, wintypes.LPARAM)
        self.user.EnumChildWindows.argtypes = (wintypes.HWND, self.callback_type, wintypes.LPARAM)
        self.user.IsWindowVisible.argtypes = (wintypes.HWND,)
        self.user.IsWindowEnabled.argtypes = (wintypes.HWND,)
        self.user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        self.user.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        self.user.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        self.user.GetParent.argtypes = (wintypes.HWND,)
        self.user.GetParent.restype = wintypes.HWND
        self.user.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
        self.user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        self.user.SendMessageTimeoutW.argtypes = (
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
        )
        self.user.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        self.kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        self.kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.GetProcessTimes.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        )

    def send(self, hwnd: int, message: int, wparam: int = 0, lparam: int = 0) -> int | None:
        result = ctypes.c_size_t()
        ok = self.user.SendMessageTimeoutW(hwnd, message, wparam, lparam, 0x0002 | 0x0001,
                                          300, ctypes.byref(result))
        return result.value if ok else None

    def text(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(4096)
        self.send(hwnd, 0x000D, len(buffer), ctypes.cast(buffer, ctypes.c_void_p).value)
        return buffer.value

    def process_identity(self, pid: int) -> int | None:
        process = self.kernel.OpenProcess(0x1000, False, pid)
        if not process:
            return None
        try:
            creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if self.kernel.GetProcessTimes(process, ctypes.byref(creation), ctypes.byref(exit_time),
                                           ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return creation.dwLowDateTime | creation.dwHighDateTime << 32
        finally:
            self.kernel.CloseHandle(process)
        return None

    def processes(self) -> dict[int, tuple[int, str]]:
        class Entry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                        ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
                        ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                        ("parent", wintypes.DWORD), ("priority", ctypes.c_long),
                        ("flags", wintypes.DWORD), ("exe", wintypes.WCHAR * 260)]
        self.kernel.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
        self.kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
        snapshot = self.kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        found = {}
        try:
            entry = Entry()
            entry.size = ctypes.sizeof(entry)
            more = self.kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                found[entry.pid] = (entry.parent, entry.exe)
                more = self.kernel.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            self.kernel.CloseHandle(snapshot)
        return found

    def windows(self, pids: set[int]) -> list[int]:
        result = []
        def collect(hwnd, _param):
            pid = wintypes.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and self.user.IsWindowVisible(hwnd):
                result.append(hwnd)
            return True
        self.user.EnumWindows(self.callback_type(collect), 0)
        return result

    def controls(self, window: int) -> list[Control]:
        result = []
        def collect(hwnd, _param):
            if not self.user.IsWindowVisible(hwnd) or not self.user.IsWindowEnabled(hwnd):
                return True
            name = ctypes.create_unicode_buffer(256)
            self.user.GetClassNameW(hwnd, name, len(name))
            rect = wintypes.RECT()
            self.user.GetWindowRect(hwnd, ctypes.byref(rect))
            result.append(Control(hwnd, self.user.GetParent(hwnd), name.value, self.text(hwnd),
                                  (rect.left, rect.top, rect.right, rect.bottom),
                                  self.user.GetWindowLongPtrW(hwnd, -16)))
            return True
        self.user.EnumChildWindows(window, self.callback_type(collect), 0)
        return result

    def set_text(self, hwnd: int, value: str) -> bool:
        buffer = ctypes.create_unicode_buffer(value)
        return self.send(hwnd, 0x000C, 0, ctypes.cast(buffer, ctypes.c_void_p).value) == 1


def recognized_fields(controls: list[Control]) -> list[tuple[Control, str]]:

    labels = [(control, field_kind(control.text)) for control in controls if not control.is_edit]
    labels = [(control, kind) for control, kind in labels if kind and "button" not in control.class_name.casefold()]
    result = []
    for edit in (control for control in controls if control.is_edit):
        ex, ey, er, eb = edit.rect
        candidates = []
        for label, kind in labels:
            if label.parent != edit.parent:
                continue
            lx, ly, lr, lb = label.rect
            center_gap = abs((ly + lb) / 2 - (ey + eb) / 2)
            if lr <= ex + 12 and center_gap <= max(16, (eb - ey) / 2):
                candidates.append((center_gap * 5 + max(0, ex - lr), kind))
            elif lb <= ey + 3 and 0 <= ey - lb <= 45 and lx < er and lr > ex:
                candidates.append((100 + ey - lb, kind))
        if candidates:
            candidates.sort()
            if len(candidates) == 1 or candidates[0][0] < candidates[1][0]:
                result.append((edit, candidates[0][1]))
        elif edit.style & 0x20:
            result.append((edit, "password"))
    return result


FORWARD_BUTTONS = {"next", "install", "finish", "continue", "register", "activate", "submit",
                   "التالي", "تثبيت", "انهاء", "إنهاء", "متابعة", "تسجيل", "تفعيل"}


class InstallerAssistant:
    def __init__(self, pid: int, credentials: Credentials, log=lambda _message: None,
                 help_callback=lambda: None, allow_advance=True):
        self.forms = WindowsForms()
        self.allow_advance = allow_advance
        self.root_pid = pid
        identity = self.forms.process_identity(pid)
        if identity is None:
            raise OSError("Cannot access the launched installer process for autofill.")
        self.tracked = {pid: identity}
        self.credentials = credentials
        self.log = log
        self.help_callback = help_callback
        self.filled: dict[int, str] = {}
        self.user_fields: set[int] = set()
        self.advanced: set[tuple] = set()
        self.reported_fields: set[str] = set()
        self.stalled_since = time.monotonic()
        self.warned = False
        self.pages = {}

    def poll(self) -> bool:
        snapshot = self.forms.processes()
        live = {pid for pid, identity in self.tracked.items()
                if pid in snapshot and self.forms.process_identity(pid) == identity}

        parents = set(self.tracked)
        while True:
            added = set()
            for pid, (parent, name) in snapshot.items():
                if parent not in parents or pid in self.tracked:
                    continue
                if name.casefold() in {"afterfx.exe", "chrome.exe", "msedge.exe", "firefox.exe", "iexplore.exe"}:
                    continue
                identity = self.forms.process_identity(pid)
                if identity is not None and identity >= self.tracked[parent]:
                    self.tracked[pid] = identity
                    live.add(pid)
                    added.add(pid)
            if not added:
                break
            parents = added
        windows = self.forms.windows(live)
        for window in windows:
            self._fill_window(window)
        if windows and not self.warned and time.monotonic() - self.stalled_since > 20:
            self.log("Installer needs attention: its current page could not be completed automatically.")
            self.warned = True
            self.help_callback()

        child_installers = {pid for pid in live - {self.root_pid}
                            if any(token in snapshot[pid][1].casefold()
                                   for token in ("setup", "install", "msiexec", ".tmp"))}
        return bool(windows or child_installers)

    def _fill_window(self, window: int) -> None:
        controls = self.forms.controls(window)
        signature = (window, tuple((control.handle, control.class_name,
                                    "" if control.is_edit else control.text)
                                   for control in controls))
        page_changed = self.pages.get(window) != signature
        self.pages[window] = signature
        fields = recognized_fields(controls)
        assignments: dict[int, tuple[str, str]] = {}
        for control, kind in fields:
            value = self.credentials.values.get(kind)
            if value:
                assignments[control.handle] = (kind, value)

        serial_boxes = [control for control, kind in fields if kind == "serial"]
        if len(serial_boxes) > 1:
            rows: dict[int, list[Control]] = {}
            for control in serial_boxes:
                row = next((key for key in rows if abs(key - control.rect[1]) < 8), control.rect[1])
                rows.setdefault(row, []).append(control)
            for row in rows.values():
                if len(row) < 2:
                    continue
                chunks = re.split(r"[-\s]+", self.credentials.values.get("serial", ""))
                row.sort(key=lambda control: control.rect[0])
                for control in row:
                    assignments.pop(control.handle, None)
                if len(chunks) == len(row) and all(chunks):
                    for control, chunk in zip(row, chunks):
                        assignments[control.handle] = ("serial", chunk)

        blocked = False
        newly_filled = set()
        for control in (item for item in controls if item.is_edit):
            assignment = assignments.get(control.handle)
            if assignment:
                kind, value = assignment
                if control.handle in self.user_fields:continue


                if control.text:
                    if self.filled.get(control.handle)!=control.text:self.user_fields.add(control.handle)
                    continue
                if control.handle in self.filled and not page_changed:
                    continue
                if control.style & 0x20:
                    length=self.forms.send(control.handle,0x000E)
                    if length:
                        if control.handle not in self.filled:self.user_fields.add(control.handle)
                        continue
                limit = self.forms.send(control.handle, 0x00D5)
                if limit is not None and 0 < limit < len(value):
                    blocked = True
                    continue

                if (self.filled.get(control.handle) != value or page_changed
                        or (not control.text and not control.style & 0x20)):
                    if not self.forms.set_text(control.handle, value):
                        blocked = True
                        continue
                    self.filled[control.handle] = value
                    newly_filled.add(kind)
            elif not control.text:
                blocked = True
        if newly_filled:
            unseen = newly_filled - self.reported_fields
            if unseen:
                self.log("Filled installer fields from README: " + ", ".join(sorted(unseen)))
                self.reported_fields.update(unseen)
            self.stalled_since = time.monotonic()
            self.warned = False

        if not self.allow_advance or blocked or signature in self.advanced:
            return
        buttons = [control for control in controls
                   if "button" in control.class_name.casefold()
                   and normalized_label(control.text) in FORWARD_BUTTONS]

        if len(buttons) == 1:
            self.advanced.add(signature)
            self.forms.send(buttons[0].handle, 0x00F5)
            self.stalled_since = time.monotonic()
            self.warned = False
            self.log("Advanced installer step automatically.")


def wait_with_autofill(process, credentials: Credentials, log, help_callback=lambda: None) -> int:

    try:
        assistant = InstallerAssistant(process.pid, credentials, log, help_callback)
    except OSError:
        log("Automatic form access is unavailable; complete the visible installer manually.")
        help_callback()
        return process.wait()
    exited_since = None
    automation_failed = False
    while True:
        try:
            has_windows = assistant.poll() if not automation_failed else False
        except Exception:

            log("Automatic form entry stopped; complete the installer manually.")
            help_callback()
            automation_failed = True
            has_windows = False
        code = process.poll()
        if code is not None:
            if exited_since is None:
                exited_since = time.monotonic()
            if not has_windows and time.monotonic() - exited_since >= 2:
                return code
        time.sleep(0.25)
