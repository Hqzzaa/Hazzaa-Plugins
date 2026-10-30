from __future__ import annotations

import ctypes
import hashlib
import math
import os
import queue
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from splash_customizer import SplashCustomizer
from installer_automation import credentials_for_installer, read_document, wait_with_autofill
from ae_organization import (
    SUPPORTED_YEARS, apply_registry_changes, copy_preset_file, find_preset_folders,
    is_user_presets, local_drives, merge_presets, parse_adobe_registry, preset_directory,
    registry_backup_directory, restore_registry_backup,
)
from plugin_library import DownloadCancelled, LibraryClient
from plugin_library_ui import LibraryWindow
from activation_details import ActivationStore, make_record
from activation_ui import ActivationWindow
from silent_install import silent_command
from product_deployment import complete_bcc12, pixel_sorter_hint, verify_rsmb5_outputs, verify_known_setup_outputs,bcc_library_directory
from installer_automation import WindowsForms
from ae_paths import discover, normalize_target, common_plugin_directory, support_for_target
from build_info import TITLE, VERSION
from deployment_plan import plugin_plan, script_plan, validate_plugin, obsolete_plugin_copies, deploy_files
from installer_runner import run_installer
from runtime_dependencies import ensure_prerequisites,missing_runtime_files,VC14_NAMES
from installer_runner import InstallerExit
from update_ui import UpdateController
from app_updater import helper_main, acknowledge_update
from installed_products import DeploymentStore, record_deployment
from uninstall_ui import UninstallWindow
from installer_workflow import execute_setup
from installer_help import help_values,preserve_bcc_attachments,bcc_attachment_folder,ExtractionWorkspace
from installer_support_ui import SetupHelpWindow
from setup_dispatcher import SetupDispatcher
from filmconvert_runtime import complete_runtime as complete_filmconvert_runtime, needs_repair as filmconvert_needs_repair
from filmconvert_runtime import reusable_installation as reusable_filmconvert_installation

try:
    import rarfile
except ImportError:
    rarfile = None


ADOBE_ROOT = Path(r"C:\Program Files\Adobe")
FONT = ("Courier New", 11, "bold")
BLACK = "#000000"
PANEL = "#202020"
PANEL_DARK = "#151515"
WHITE = "#FFFFFF"
SNOW = "#D8D8D8"
PLUGIN_SUFFIXES = {".aex"}
SCRIPT_SUFFIXES = {".jsx", ".jsxbin", ".jsxinc"}

TEXT_HINTS = ("readme", "serial", "info", "license", "licence", "credentials", "registration", "code", "instruction", "activation", "key")
PARTICLE_COUNT = 260
PARTICLE_FRAME_MS = 33


def resource_path(name: str) -> Path:

    if hasattr(sys, "_MEIPASS"):

        bundle_root = Path(sys._MEIPASS) / "assets"
    else:
        bundle_root = Path(__file__).resolve().parent / "assets"
    return bundle_root / name


def configure_rar_backend() -> bool:

    if rarfile is None:
        return False

    candidates = [
        resource_path("UnRAR.exe"),
        Path(r"C:\Program Files\WinRAR\UnRAR.exe"),
        Path(r"C:\Program Files (x86)\WinRAR\UnRAR.exe"),
    ]
    path_tool = shutil.which("unrar") or shutil.which("UnRAR")
    if path_tool:
        candidates.insert(0, Path(path_tool))

    executable = next((candidate for candidate in candidates if candidate.is_file()), None)
    try:


        config = getattr(rarfile, "config", rarfile)
        if executable:
            config.UNRAR_TOOL = str(executable)
        rarfile.tool_setup(force=True)
        return True
    except Exception:
        return False


def detect_ae_installations(root: Path = ADOBE_ROOT) -> list[tuple[str, Path]]:

    return discover(root, registry=None if root == ADOBE_ROOT else [],
                    extra_roots=None if root == ADOBE_ROOT else [])


def _safe_destination(root: Path, member_name: str) -> Path:

    clean_name = member_name.replace("\\", "/")


    for part in clean_name.split('/'):
        if not part or part == '.':
            continue
        if (part != part.rstrip(' .') or any(ord(c) < 32 for c in part)
                or re.search(r'[<>"|?*]', part)
                or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part)):
            raise ValueError(f'Unsafe Windows archive name: {member_name}')
    relative = Path(clean_name)
    if relative.is_absolute() or any(part == ".." or ":" in part for part in relative.parts):
        raise ValueError(f"Unsafe archive path: {member_name}")
    destination = (root / relative).resolve()
    if destination != root.resolve() and root.resolve() not in destination.parents:
        raise ValueError(f"Unsafe archive path: {member_name}")
    return destination


def extract_archive(archive_path: Path, destination: Path) -> list[str]:

    warnings: list[str] = []
    seen_files = set()

    def member_target(member_name: str) -> Path | None:
        try:
            return _safe_destination(destination, member_name)
        except ValueError as error:
            warnings.append(f"Skipped unsafe entry {member_name!r}: {error}")
            return None

    suffix = archive_path.suffix.casefold()
    if suffix == ".zip":
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                target = member_target(info.filename)
                if target is None:
                    continue
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    if (info.external_attr >> 16) & 0o170000 == 0o120000:
                        warnings.append(f'Skipped archive link: {info.filename}')
                        continue
                    key = str(target).casefold()
                    if key in seen_files:
                        raise ValueError('Archive contains colliding Windows file paths: '+info.filename)
                    seen_files.add(key)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
        return warnings

    if suffix == ".rar":
        if not configure_rar_backend():
            raise RuntimeError("The bundled RAR reader could not start. Rebuild or reinstall the complete application package.")
        with rarfile.RarFile(archive_path) as archive:
            entries = archive.infolist()


            all_safe = True
            for info in entries:
                if info.is_symlink() or getattr(info, "file_redir", None):
                    all_safe = False
                    break
                try:
                    checked = _safe_destination(destination, info.filename)
                except ValueError:
                    all_safe = False
                    break
                if not info.isdir():
                    key = str(checked).casefold()
                    if key in seen_files:
                        raise ValueError('Archive contains colliding Windows file paths: '+info.filename)
                    seen_files.add(key)
            if all_safe and entries:
                destination.mkdir(parents=True, exist_ok=True)
                backend = getattr(getattr(rarfile, "config", rarfile), "UNRAR_TOOL")
                result = subprocess.run([str(backend), "x", "-idq", "-o+", "-p-",
                    str(archive_path.resolve()), str(destination.resolve()) + os.sep],
                    capture_output=True, timeout=900, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if result.returncode != 0:
                    raise RuntimeError(f"RAR extraction failed (code {result.returncode}). The archive may be damaged or password protected.")
                return warnings
            seen_files.clear()
            for info in entries:
                if info.is_symlink() or getattr(info, "file_redir", None):
                    warnings.append(f"Skipped archive link: {info.filename}")
                    continue
                target = member_target(info.filename)
                if target is None:
                    continue
                if info.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    key = str(target).casefold()
                    if key in seen_files:
                        raise ValueError('Archive contains colliding Windows file paths: '+info.filename)
                    seen_files.add(key)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
        return warnings

    raise ValueError(f"Unsupported archive type: {archive_path.suffix}")


def unique_destination(path: Path) -> Path:

    if not path.exists():
        return path
    for number in range(2, 10000):
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not find an unused name for {path.name}")


def install_binary(source: Path, destination: Path) -> bool:

    destination.parent.mkdir(parents=True,exist_ok=True)
    with source.open('rb') as stream:
        digest=hashlib.file_digest(stream,'sha256').hexdigest()
    if destination.exists():
        with destination.open('rb') as stream:
            old_digest=hashlib.file_digest(stream,'sha256').hexdigest()
        if old_digest==digest:
            if not destination.stat().st_mode & stat.S_IWRITE:
                destination.chmod(0o644)
            return False
        backup_root=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData'/'Local')))/'Hazzaa Plugins'/'Backups'/'Dependencies'
        backup_root.mkdir(parents=True,exist_ok=True)
        backup=backup_root/(old_digest+'-'+destination.name+'.backup')
        if not backup.exists():
            shutil.copyfile(destination,backup)
        else:
            with backup.open('rb') as stream:
                if hashlib.file_digest(stream,'sha256').hexdigest()!=old_digest:
                    raise OSError('The existing rollback backup does not match its recorded hash: '+str(backup))

    staging=destination.with_name(destination.name+'.hazzaa-pending')
    try:
        if staging.exists():
            staging.chmod(0o644)


        shutil.copyfile(source,staging)
        staging.chmod(0o644)
        with staging.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=digest:
                raise OSError(f'Copied file verification failed: {destination.name}')
        if destination.exists() and not destination.stat().st_mode & stat.S_IWRITE:
            destination.chmod(0o644)
        os.replace(staging,destination)
        with destination.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=digest:
                raise OSError(f'Installed file verification failed: {destination.name}')
    finally:
        staging.unlink(missing_ok=True)
    return True


def script_destination_directory(plugin_target: Path) -> Path:

    target_name = plugin_target.name.casefold()
    if target_name in {"plug-ins", "plugins"}:

        return plugin_target.parent / "Scripts"
    if target_name == "scripts":
        return plugin_target

    support = support_for_target(plugin_target)
    return support/'Scripts' if support else plugin_target / "Scripts"


def merge_directory_contents(source: Path, destination: Path) -> None:

    destination.mkdir(parents=True, exist_ok=True)
    for child in list(source.iterdir()):
        target = destination / child.name
        if child.is_dir() and target.is_dir():
            merge_directory_contents(child, target)
        else:
            shutil.move(str(child), str(unique_destination(target)))
    source.rmdir()


def find_assets(extracted: Path) -> tuple[list[Path], list[Path], list[Path], list[Path], list[Path]]:

    plugin_files: list[Path] = []
    script_files: list[Path] = []
    installers: list[Path] = []
    text_files: list[Path] = []
    extensions = find_cep_extensions(extracted)
    for path in extracted.rglob("*"):
        if not path.is_file() or ignored_platform_path(path, extracted):
            continue
        if any(folder in path.parents for folder in extensions):
            continue
        suffix = path.suffix.casefold()
        if suffix in PLUGIN_SUFFIXES:
            plugin_files.append(path)
        elif suffix in SCRIPT_SUFFIXES:
            script_files.append(path)
        elif suffix in {".exe", ".msi"}:
            installers.append(path)
        elif suffix in {".txt", ".text"} and (path.stem.casefold()=='sn' or any(hint in path.stem.casefold() for hint in TEXT_HINTS)):
            if "vegas" not in path.stem.casefold():
                text_files.append(path)


    script_dirs: list[Path] = []
    for folder in extracted.rglob("*"):
        if ignored_platform_path(folder, extracted) or any(parent == folder or parent in folder.parents for parent in extensions):
            continue
        if not folder.is_dir() or folder.name.casefold() not in {"scripts", "scriptui panels", "scriptui", "panels"}:
            continue
        if any(file.suffix.casefold() in SCRIPT_SUFFIXES for file in folder.rglob("*")):
            script_dirs.append(folder)
    script_dirs.sort(key=lambda p: len(p.parts))
    selected_dirs: list[Path] = []
    for folder in script_dirs:
        if not any(parent == folder or parent in folder.parents for parent in selected_dirs):
            selected_dirs.append(folder)
    standalone_scripts = [p for p in script_files if not any(d == p or d in p.parents for d in selected_dirs)]
    return plugin_files, standalone_scripts, selected_dirs, installers, text_files


def ignored_platform_path(path, root):

    return any(part.casefold() in {"__macosx", "mac", "macos", "macosx", "vegas"}
               or part.casefold().startswith("._") for part in path.relative_to(root).parts)


def find_cep_extensions(root):
    folders = []
    for manifest in root.rglob("manifest.xml"):
        if manifest.parent.name.casefold() != "csxs" or ignored_platform_path(manifest, root):
            continue
        try:
            if manifest.stat().st_size > 1024 * 1024:
                continue
            tree = ET.parse(manifest)
            if any(host.attrib.get("Name") == "AEFT" for host in tree.iter("Host")):
                folders.append(manifest.parent.parent)
        except (OSError, ET.ParseError):
            continue
    return folders


def extract_nested_archives(root):

    seen = set()
    for depth in range(3):
        archives = [p for p in root.rglob("*") if p.is_file() and p.suffix.casefold() in {".zip", ".rar"}
                    and p not in seen and not ignored_platform_path(p, root)
                    and "vegas" not in p.stem.casefold()]
        if not archives:
            break
        for archive in archives:
            if len(seen) >= 32:
                raise ValueError("Too many nested archives.")
            seen.add(archive)
            destination = archive.parent / (archive.stem + "_contents")
            destination.mkdir(exist_ok=True)
            extract_archive(archive, destination)


class ArchiveDeployer:


    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(TITLE)
        self.root.geometry("1000x720")
        self.root.minsize(920, 650)
        self.root.configure(bg=BLACK)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.cursor_path = resource_path("bat_cursor.ani")
        if not self.cursor_path.is_file():
            self.cursor_path = resource_path("bat_cursor.cur")
        self.cursor_spec = f"@{self.cursor_path}"
        self.cat_photo = tk.PhotoImage(master=self.root, file=str(resource_path("cat_logo.png"))).subsample(2, 2)
        try:
            self.root.iconbitmap(default=str(resource_path("app_icon.ico")))
        except tk.TclError:
            pass
        self.root.iconphoto(True, self.cat_photo)

        self.events: queue.Queue = queue.Queue()
        self.archives: list[Path] = []
        self.busy = False
        self.preset_scan_running = False
        self.preset_cancel = threading.Event()
        self.download_cancel = threading.Event()
        self.downloading = False
        self.library_window = None
        self.activation_store = ActivationStore()
        self.deployment_store = DeploymentStore()
        self.activation_records = {record['id']:record for record in self.activation_store.records()}
        self.activation_window = None
        self.uninstall_window = None
        self.setup_help_windows = {}
        self.opened_installer_folders = set()
        self.setup_dispatcher = SetupDispatcher(
            lambda message:self.events.put(('log',message)),lambda kind,value:self.events.put((kind,value)))
        self._setup_queue_failed=False
        self._snow_job = self._events_job = None
        self.root.bind("<Destroy>", self._cleanup_jobs, add="+")
        self.snow: list[dict[str, float]] = []
        self._build_ui()
        self.updater = UpdateController(self)
        try:
            log_root = Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'Hazzaa Plugins/Logs'
            log_root.mkdir(parents=True, exist_ok=True)
            self.log_path = log_root/(time.strftime('%Y%m%d-%H%M%S')+f'-{os.getpid()}.log')
        except OSError:
            self.log_path = None
        self._log('HAZZAA BUILD '+VERSION)
        self._apply_cursor(self.root)
        self.root.update_idletasks()
        self._set_dark_titlebar()
        self._animate_snow()
        self._poll_events()
        if getattr(sys, 'frozen', False):
            self.root.after(300, self._repair_installed_support_files)

    def _repair_installed_support_files(self):
        if self.busy or self.setup_dispatcher.pending_count:
            self.root.after(1000, self._repair_installed_support_files)
            return
        self.busy = True
        def work():
            repaired, error = False, None
            try:
                if filmconvert_needs_repair():
                    self.events.put(('log', 'Repairing incomplete FilmConvert support files...'))
                    complete_filmconvert_runtime(Path('Nitrate.PP-3.56b.exe'), install_binary,
                        log=lambda message: self.events.put(('log', message)))
                    repaired = True
            except (OSError, ValueError, ET.ParseError) as problem:
                error = str(problem)
            self.events.put(('support_runtime_done', (repaired, error)))
        threading.Thread(target=work, daemon=True).start()

    def _apply_cursor(self, widget: tk.Widget) -> None:

        try:
            widget.configure(cursor=self.cursor_spec)
        except (tk.TclError, AttributeError):
            try:
                widget.configure(cursor=f"@{resource_path('bat_cursor.cur')}")
            except (tk.TclError, AttributeError):
                pass
        for child in widget.winfo_children():
            self._apply_cursor(child)

    def _cleanup_jobs(self, event):
        if event.widget is self.root:
            if hasattr(self,'setup_dispatcher'):self.setup_dispatcher.close()
            for job in (self._snow_job, self._events_job):
                if job is not None:
                    try:
                        self.root.after_cancel(job)
                    except tk.TclError:
                        pass

    def _set_dark_titlebar(self, window: tk.Toplevel | tk.Tk | None = None) -> None:

        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
            user32.GetParent.argtypes = [ctypes.c_void_p]
            user32.GetParent.restype = ctypes.c_void_p
            dwmapi.DwmSetWindowAttribute.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint
            ]
            hwnd = (window or self.root).winfo_id()
            top_level = user32.GetParent(hwnd) or hwnd
            enabled = ctypes.c_int(1)
            black = ctypes.c_uint(0x000000)
            white = ctypes.c_uint(0xFFFFFF)
            for attribute in (19, 20):
                dwmapi.DwmSetWindowAttribute(top_level, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled))
            dwmapi.DwmSetWindowAttribute(top_level, 35, ctypes.byref(black), ctypes.sizeof(black))
            dwmapi.DwmSetWindowAttribute(top_level, 36, ctypes.byref(white), ctypes.sizeof(white))
        except (AttributeError, OSError, tk.TclError):

            pass

    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Snow.Horizontal.TProgressbar", troughcolor=PANEL_DARK,
                        background=WHITE, bordercolor=PANEL_DARK, lightcolor=WHITE,
                        darkcolor=WHITE)

        self.canvas = tk.Canvas(self.root, bg=BLACK, highlightthickness=0)
        self.canvas.place(relx=0, rely=0, relwidth=1, relheight=1)

        for _ in range(PARTICLE_COUNT):
            particle = {
                "x": random.uniform(0, 760),
                "y": random.uniform(0, 570),
                "speed": random.uniform(45, 110),
                "drift": random.uniform(-9, 9),
                "phase": random.uniform(0, math.tau),
                "size": random.choice((1.5, 2.0, 2.5, 3.0)),
            }
            size = particle["size"]
            particle["item"] = self.canvas.create_oval(
                particle["x"], particle["y"], particle["x"] + size, particle["y"] + size,
                fill=SNOW, outline="", tags="snow"
            )
            self.snow.append(particle)

        panel = tk.Frame(self.root, bg=BLACK, padx=24, pady=22)
        panel.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.88, relheight=0.84)
        heading = tk.Frame(panel, bg=BLACK)
        heading.pack(fill="x", pady=(0, 16))
        tk.Label(heading, image=self.cat_photo, bg=BLACK, bd=0).pack(side="left", padx=(0, 10))
        self._label(heading, "HAZZAA AFTER EFFECTS DEPLOYER").pack(side="left")
        self._button(heading, "SPLASH IMAGE", self.open_splash_customizer).pack(side="right")
        self._button(heading, "ACTIVATION", self.open_activation).pack(side="right", padx=8)

        row = tk.Frame(panel, bg=BLACK)
        row.pack(fill="x", pady=4)
        self._label(row, "VERSION").pack(side="left", padx=(0, 12))
        self.version_var = tk.StringVar()
        self.installations = detect_ae_installations()
        self.version_paths = {year: ADOBE_ROOT / f"Adobe After Effects {year}" / "Support Files" / "Plug-ins"
                              for year in SUPPORTED_YEARS}


        for year, path in reversed(self.installations):
            self.version_paths[year] = path
        used_years = set()
        for year, path in self.installations:
            if year in used_years:
                self.version_paths[f'{year} | {path.parent.parent}'] = path
            used_years.add(year)
        version_names = list(SUPPORTED_YEARS)
        version_names += [label for label in self.version_paths if ' | ' in label]
        self.version_menu = tk.OptionMenu(row, self.version_var, *version_names, command=self._version_changed)
        self.version_menu.configure(bg=PANEL, fg=WHITE, activebackground=PANEL, activeforeground=WHITE,
                                    highlightthickness=0, relief="flat", font=FONT)
        self.version_menu["menu"].configure(bg=PANEL, fg=WHITE, activebackground="#444444", activeforeground=WHITE, font=FONT)
        self.version_menu.pack(side="left", fill="x", expand=True)
        default_version = self.installations[0][0] if self.installations else "2026"
        self.version_var.set(default_version)
        self.target_path = self.version_paths[default_version]

        self.target_var = tk.StringVar(value=str(self.target_path) if self.target_path else "")
        target_row = tk.Frame(panel, bg=BLACK)
        target_row.pack(fill="x", pady=(9, 14))
        self._label(target_row, "TARGET").pack(side="left", padx=(0, 12))
        target_entry = tk.Entry(target_row, textvariable=self.target_var, bg=PANEL_DARK, fg=WHITE,
                                insertbackground=WHITE, relief="flat", font=FONT)
        target_entry.pack(side="left", fill="x", expand=True, ipady=7)
        self._button(target_row, "BROWSE TARGET FOLDER", self.browse_target).pack(side="left", padx=(8, 0))

        action_row = tk.Frame(panel, bg=BLACK)
        action_row.pack(fill="x", pady=4)
        self._button(action_row, "SELECT FROM PC", self.select_archives).pack(side="left")
        self.library_button = self._button(action_row, "ONLINE LIBRARY", self.open_library)
        self.library_button.pack(side="left", padx=(8, 0))
        self._button(action_row, "PROCESS", self.process_archives).pack(side="left", padx=8)
        self.silent_var = tk.BooleanVar(value=True)
        tk.Checkbutton(action_row, text="AUTO INSTALL", variable=self.silent_var,
                       bg=BLACK, fg=WHITE, activebackground=BLACK, activeforeground=WHITE,
                       selectcolor=PANEL, font=FONT, relief="flat").pack(side="left", padx=(8, 0))

        tools_row = tk.Frame(panel, bg=BLACK)
        tools_row.pack(fill="x", pady=(7, 4))
        self.preset_button = self._button(tools_row, "COLLECT USER PRESETS", self.collect_user_presets)
        self.preset_button.pack(side="left")
        self._button(tools_row, "RESTORE REGISTRY", self.restore_script_registry).pack(side="left", padx=8)
        self.registry_var = tk.BooleanVar(value=True)
        tk.Checkbutton(tools_row, text="SCRIPT REGISTRY", variable=self.registry_var,
                       bg=BLACK, fg=WHITE, activebackground=BLACK, activeforeground=WHITE,
                       selectcolor=PANEL, font=FONT, relief="flat").pack(side="left")
        self._button(tools_row,'COPY LOG',self.copy_log).pack(side='right')
        self.uninstall_button = self._button(self.root,'UNINSTALL',self.open_uninstall)
        self.uninstall_button.place(relx=.02,rely=.98,anchor='sw')
        self.setup_status=self._label(self.root,'')
        self.setup_status.place(relx=.5,rely=.98,anchor='s')

        self.archive_label = self._label(panel, "NO ARCHIVES SELECTED")
        self.archive_label.pack(anchor="w", pady=(9, 5))
        self.progress = ttk.Progressbar(panel, mode="determinate", style="Snow.Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(0, 10))
        self.log = tk.Text(panel, height=12, bg=PANEL_DARK, fg=WHITE, insertbackground=WHITE,
                           relief="flat", wrap="word", font=FONT, state="disabled")
        self.log.pack(fill="both", expand=True)

    def _label(self, parent: tk.Widget, text: str) -> tk.Label:
        return tk.Label(parent, text=text, bg=BLACK, fg=WHITE, font=FONT, anchor="w")

    def _button(self, parent: tk.Widget, text: str, command) -> tk.Button:
        return tk.Button(parent, text=text, command=command, bg=PANEL, fg=WHITE,
                         activebackground="#444444", activeforeground=WHITE,
                         relief="flat", bd=0, padx=12, pady=8, font=FONT, cursor="hand2")

    def _version_changed(self, version: str) -> None:
        if version in self.version_paths:
            self.target_path = self.version_paths[version]
            self.target_var.set(str(self.target_path))

    def open_splash_customizer(self) -> None:

        version = self.version_var.get()
        plugin_path = self.version_paths.get(version)
        if not plugin_path:
            messagebox.showerror("After Effects not found", "Select an installed After Effects version first.")
            return
        support_files = support_for_target(plugin_path) or plugin_path.parent
        candidates = (support_files / "AfterFXLib.dll", support_files / "AfterFX.dll")
        dll_path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if dll_path is None:
            messagebox.showerror("Splash resource not found",
                                 f"Could not find AfterFXLib.dll or AfterFX.dll in:\n{support_files}")
            return
        try:
            SplashCustomizer(self.root, dll_path, self._apply_cursor, self._set_dark_titlebar)
        except Exception as error:
            messagebox.showerror("Splash image unavailable", str(error))

    def browse_target(self) -> None:
        selected = filedialog.askdirectory(title="Select Plug-ins Target Folder", mustexist=True)
        if selected:
            self.target_path = Path(selected)
            self.target_var.set(selected)

    def collect_user_presets(self) -> None:
        if self.preset_scan_running:
            self.preset_cancel.set()
            return
        if self.busy:
            return
        destination = preset_directory(self.version_var.get()[:4])
        self.busy = self.preset_scan_running = True
        self.preset_cancel.clear()
        self.preset_button.configure(text="CANCEL PRESET SCAN")
        self.progress.configure(mode="indeterminate")
        self.progress.start(15)
        self._log(f"Collecting User Presets into {destination}")
        threading.Thread(target=self._preset_worker, args=(destination,), daemon=True).start()

    def _preset_worker(self, destination: Path) -> None:
        copied = folders = 0
        try:
            for source in find_preset_folders(local_drives(), destination, self.preset_cancel.is_set,
                    lambda count: self.events.put(("log", f"Preset search: {count} folders checked..."))):
                folders += 1
                try:
                    count = merge_presets(source, destination, self.preset_cancel.is_set,
                        lambda file, error: self.events.put(("log", f"Could not copy preset file {file}: {error}")))
                    copied += count
                    self.events.put(("log", f"Presets: {count} file(s) copied from {source}"))
                except OSError as error:
                    self.events.put(("log", f"Could not copy presets from {source}: {error}"))
            state = "cancelled" if self.preset_cancel.is_set() else "complete"
            self.events.put(("log", f"Preset collection {state}: {copied} new file(s), {folders} source folder(s)."))
        except OSError as error:
            self.events.put(("log", f"Preset collection failed: {error}"))
        finally:
            self.events.put(("preset_done", None))

    def restore_script_registry(self) -> None:
        if self.busy:
            return
        directory = registry_backup_directory()
        path = filedialog.askopenfilename(parent=self.root, title="Restore Adobe script settings",
            initialdir=str(directory if directory.exists() else Path.home()),
            filetypes=[("HAZZAA registry backup", "*.json")])
        if path:
            try:
                restore_registry_backup(Path(path))
                self._log("Adobe script registry settings restored.")
            except (OSError, ValueError, KeyError) as error:
                messagebox.showerror("Registry restore", str(error), parent=self.root)

    def select_archives(self) -> None:
        if self.busy:
            return
        selected = filedialog.askopenfilenames(title="Select Archive Files",
                     filetypes=[("Archives", "*.zip *.rar"), ("ZIP archives", "*.zip"), ("RAR archives", "*.rar")])
        if selected:
            self.archives = [Path(item) for item in selected]
            self.archive_label.configure(text=f"{len(self.archives)} ARCHIVE(S) SELECTED")
            self._log(f"Selected {len(self.archives)} archive(s).")

    def _validated_target(self):
        target_text = self.target_var.get().strip()
        if not target_text:
            messagebox.showerror("Target required", "Select a target Plug-ins folder first.")
            return
        try:
            target = normalize_target(Path(target_text))
        except ValueError as error:
            messagebox.showerror('Invalid target', str(error), parent=self.root)
            return
        self.target_var.set(str(target))
        default_target = self.version_paths.get(self.version_var.get())
        detected_paths = {path for _,path in self.installations}
        if default_target is not None and target == default_target and target not in detected_paths and not (target.parent/'AfterFX.exe').is_file():
            messagebox.showerror("Version not installed",
                "This After Effects version is not installed at its default location.\n"
                "Choose an installed version or use Browse Target Folder.", parent=self.root)
            return
        try:
            target.mkdir(parents=True, exist_ok=True)
            if not target.is_dir():
                raise OSError("The selected target is not a directory.")


            with tempfile.NamedTemporaryFile(prefix='.hazzaa-write-test-',dir=target,delete=True):
                pass
        except OSError as error:
            messagebox.showerror("Target unavailable", f"Cannot access target folder:\n{error}\n\nTry a writable folder or run with appropriate permissions.")
            return
        return target

    def _host_is_open(self):
        if os.name!='nt':return False
        try:
            return any(name.casefold()=='afterfx.exe' for _,name in WindowsForms().processes().values())
        except OSError:
            return False

    def _installation_preflight(self):
        if self._host_is_open():
            self._log('INSTALLATION NOT STARTED: Close After Effects, then try again. New effects cannot be verified while the host is running.')
            self.archive_label.configure(text='CLOSE AFTER EFFECTS BEFORE INSTALLING')
            return False
        self._log('Target Plug-ins directory: '+self.target_var.get())
        return True

    def process_archives(self) -> None:
        if self.busy:
            return
        if not self.archives:
            messagebox.showerror("Archives required", "Select at least one ZIP or RAR archive.")
            return
        target = self._validated_target()
        if target is None:
            return
        if not self._installation_preflight():
            return

        self.busy = True
        self.progress.configure(maximum=len(self.archives), value=0)
        worker = threading.Thread(target=self._process_worker,
            args=(list(self.archives), target, self.silent_var.get(),
                  preset_directory(self.version_var.get()[:4]), self.registry_var.get()), daemon=True)
        worker.start()

    def open_library(self):
        if self.downloading:
            self.download_cancel.set()
            self.library_button.configure(text="CANCELLING...")
            return
        if self.library_window is not None and self.library_window.root.winfo_exists():
            self.library_window.root.lift()
            return
        self.library_window = LibraryWindow(self)

    def open_uninstall(self):
        if self.uninstall_window is not None and self.uninstall_window.root.winfo_exists():
            self.uninstall_window.root.lift();return
        import json
        try:
            index=json.loads(resource_path('installed-package-index.json').read_text(encoding='utf-8'))['items']
        except (OSError,ValueError,KeyError):index=[]
        self.uninstall_window=UninstallWindow(self,index)

    def start_library_install(self, items):
        if self.busy:
            messagebox.showinfo("Operation running", "Wait for the current operation to finish.", parent=self.root)
            return False
        target = self._validated_target()
        if target is None:
            return False
        if not self._installation_preflight():
            return False
        self.busy = self.downloading = True
        self.download_cancel.clear()
        self.library_button.configure(text="CANCEL DOWNLOAD")
        self.progress.configure(maximum=sum(item["bytes"] for item in items), value=0)
        self.archive_label.configure(text=f"DOWNLOADING {len(items)} ITEM(S)")
        args = (list(items), target, self.silent_var.get(), preset_directory(self.version_var.get()[:4]), self.registry_var.get())
        threading.Thread(target=self._library_worker, args=args, daemon=True).start()
        return True

    def _library_worker(self, items, target, silent, presets_target, script_registry):
        try:
            with tempfile.TemporaryDirectory(prefix="hazzaa_download_") as temp:
                client, completed, paths = LibraryClient(), 0, []
                for item in items:
                    self.events.put(("log", f"Downloading {item['name']}..."))
                    last_update = 0.0
                    def progress(received):
                        nonlocal last_update
                        now = time.monotonic()
                        if now - last_update >= 0.1 or received == item["bytes"]:
                            self.events.put(("progress", completed + received))
                            last_update = now
                    paths.append(client.download(item, Path(temp), self.download_cancel, progress))
                    completed += item["bytes"]
                    self.events.put(("log", f"Verified download: {item['name']}"))
                if self.download_cancel.is_set():
                    raise DownloadCancelled()
                self.events.put(("library_install", len(paths)))
                self._process_worker(paths, target, silent, presets_target, script_registry,
                                     {path:item['name'] for path,item in zip(paths,items)})
        except DownloadCancelled:
            self.events.put(("log", "Download cancelled. Temporary downloads removed."))
            self.events.put(("done", None))
        except Exception as exc:
            self.events.put(("log", f"Library error: {exc}"))
            self.events.put(("done", None))

    def _prepare_setup_package(self,installers,extracted,shared_target,documents,attachments,deploy_loose,silent,product_name=None,runtime_plugins=()):

        digest=hashlib.sha256()
        for installer in installers:
            with installer.open('rb') as stream:digest.update(hashlib.file_digest(stream,'sha256').digest())
        key=digest.hexdigest();current={'id':key,'name':installers[0].name,'installer':installers[0]}
        def run():
            if runtime_plugins:ensure_prerequisites(runtime_plugins,lambda message:self.events.put(('log',message)),automatic=silent)
            unverified=assisted=0
            for installer in installers:
                ident=key+':'+installer.name.casefold();current.update(id=ident,name=installer.name,installer=installer)
                credentials=credentials_for_installer(documents,installer)
                values=help_values(documents,credentials,installer)
                def attention(phase,reason,ident=ident,name=installer.name,values=values):
                    self.events.put(('setup_help',{'id':ident,'name':name,'phase':phase,'reason':reason,
                        'product':product_name,'values':values,'attachments':str(attachments) if attachments else None}))
                def verify(installer=installer):
                    complete_filmconvert_runtime(installer, install_binary,
                        log=lambda message: self.events.put(('log', message)))
                    return (complete_bcc12(extracted,installer,install_binary,shared_target),
                        verify_rsmb5_outputs(installer,shared_target),verify_known_setup_outputs(installer,shared_target))
                if silent and reusable_filmconvert_installation(installer,shared_target):
                    self.events.put(('log','Nitrate 3.56 is already installed. Automatically repairing and verifying its files; no setup wizard is needed.'))
                    evidence=verify()
                    outcome=InstallerExit(0)
                else:
                    outcome,evidence=execute_setup(installer,silent=silent,credentials=credentials,
                        log=lambda message:self.events.put(('log',message)),attention=attention,verify=verify,runner=run_installer)
                if getattr(outcome,'handed_off',False):
                    self.events.put(('setup_handed_off',ident))
                    return outcome,None
                bcc,rsmb,outputs=evidence
                for path in bcc+rsmb:self.events.put(('log','Verified host file location: '+str(path)))
                if outputs is not None:self.events.put(('log',f'Verified {len(outputs)} plug-in file(s). Restart After Effects.'))
                elif not bcc and not rsmb:
                    unverified+=1;self.events.put(('log','SETUP EXIT VERIFIED ONLY: host-file check required.'))
                assisted+=bool(getattr(outcome,'assisted',False))
                self.events.put(('setup_finished',(ident,True,'')))
            deploy_loose()
            return outcome,{'unverified':unverified,'assisted':assisted}
        def finished(outcome,evidence,error):
            if error is not None:
                self.events.put(('log','SETUP STOPPED: '+current['name']+': '+str(error)))
                self.events.put(('setup_finished',(current['id'],False,str(error))))
            elif getattr(outcome,'handed_off',False):
                if current['name'].casefold() == 'nitrate.pp-3.56b.exe' and getattr(getattr(outcome,'process',None),'returncode',None) == 0:
                    try:
                        complete_filmconvert_runtime(current['installer'], install_binary,
                            log=lambda message: self.events.put(('log', message)))
                    except (OSError,ValueError,ET.ParseError) as problem:
                        self.events.put(('log','FILMCONVERT SUPPORT FILES INCOMPLETE: '+str(problem)))
                        self.events.put(('setup_finished',(current['id'],False,str(problem))))
                        return
                self.events.put(('setup_manual_closed',current['id']))
            if attachments:
                library=bcc_library_directory()
                if library:self.events.put(('included_files',str(library)))
        return key,run,finished

    def _process_worker(self, archives: list[Path], target: Path, silent: bool,
                        presets_target: Path | None = None, script_registry: bool = True,
                        product_names: dict | None = None) -> None:
        failures = 0
        unverified_setups = 0
        assisted_setups = 0
        pending_setups = 0
        dispatcher=getattr(self,'setup_dispatcher',None)
        if dispatcher is None or not dispatcher.pending_count:self._setup_queue_failed=False
        shared_target = common_plugin_directory(target)
        self.events.put(('log','HAZZAA BUILD '+VERSION))
        self.events.put(('log','Selected Plug-ins: '+str(target)))
        self.events.put(('log','Adobe shared MediaCore: '+str(shared_target)))
        for index, archive_path in enumerate(archives, 1):
            self.events.put(("log", f"Processing {archive_path.name}..."))
            try:
                workspace=ExtractionWorkspace()
                with workspace as temp_name:
                    extracted = Path(temp_name)
                    warnings = extract_archive(archive_path, extracted)
                    for warning in warnings:
                        self.events.put(("log", warning))
                    extract_nested_archives(extracted)
                    extension_folders = find_cep_extensions(extracted)
                    plugin_files, script_files, script_dirs, installers, text_files = find_assets(extracted)


                    for plugin in plugin_files:
                        validate_plugin(plugin)
                    plugin_copies = plugin_plan(plugin_files, target,
                        lambda p: ignored_platform_path(p, extracted))
                    script_target = script_destination_directory(target)
                    script_copies = script_plan(script_files, script_dirs, script_target)
                    dispatcher=getattr(self,'setup_dispatcher',None)
                    setup_occupied=dispatcher is not None and dispatcher.pending_count
                    runtime_deferred=bool(silent and setup_occupied and plugin_files and missing_runtime_files(plugin_files)&VC14_NAMES)
                    if not runtime_deferred:
                        ensure_prerequisites(plugin_files, lambda message: self.events.put(('log',message)), automatic=silent and not setup_occupied)

                    documents = []
                    for text_file in text_files:
                        try:
                            content = read_document(text_file)
                        except OSError as error:
                            self.events.put(("log", f"Could not read {text_file.name}: {error}"))
                            continue
                        documents.append((text_file, content))
                    record = make_record((product_names or {}).get(archive_path,archive_path.stem),
                        archive_path.name,[p.name for p in plugin_files],documents,extracted,
                        inside_ae=bool(plugin_files or script_files or script_dirs or extension_folders) and not installers)

                    if script_registry:
                        changes = []
                        rejected = 0
                        registry_texts = [content for _, content in documents]
                        for reg_file in extracted.rglob("*.reg"):
                            registry_texts.append(read_document(reg_file))
                        for content in registry_texts:
                            parsed, skipped = parse_adobe_registry(content)
                            changes.extend(parsed)
                            rejected += skipped
                        if changes:
                            try:
                                backup = apply_registry_changes(changes)
                                self.events.put(("log", f"Applied Adobe script registry settings. Backup: {backup}"))
                            except (OSError, ValueError) as error:
                                failures += 1
                                self.events.put(("log", f"Registry settings could not be applied: {error}"))
                        if rejected:
                            self.events.put(("log", f"Skipped {rejected} unsupported registry entry/section(s)."))

                    preset_roots = [folder for folder in extracted.rglob("*")
                                    if folder.is_dir() and is_user_presets(folder)]
                    if presets_target is not None:
                        for folder in preset_roots:
                            count = merge_presets(folder, presets_target)
                            self.events.put(("log", f"Copied {count} User Presets file(s) to {presets_target}"))
                        for preset in extracted.rglob("*.ffx"):
                            if not any(folder in preset.parents for folder in preset_roots):
                                if copy_preset_file(preset, presets_target / preset.name):
                                    self.events.put(("log", f"Copied preset {preset.name} to {presets_target}"))

                    def deploy_loose(plugin_copies=plugin_copies,script_copies=script_copies,
                            extension_folders=extension_folders,target=target,script_target=script_target,
                            archive_path=archive_path,record=record,product_names=product_names):


                        for folder in extension_folders:
                            extension_target = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "Adobe" / "CEP" / "extensions"
                            extension_target.mkdir(parents=True, exist_ok=True)
                            destination = extension_target / folder.name
                            for source in folder.rglob('*'):
                                if source.is_file():
                                    install_binary(source, destination/source.relative_to(folder))
                            self.events.put(("log", f"Installed AE panel {folder.name} → {destination}"))
                        changed_files = deploy_files(plugin_copies + script_copies, install_binary)
                        try:
                            cep_root=Path(os.environ.get('APPDATA',str(Path.home()/'AppData/Roaming')))/'Adobe/CEP/extensions'
                            if hasattr(self,'deployment_store'):
                                record_deployment((product_names or {}).get(archive_path,archive_path.stem),
                                [(item.destination,target) for item in plugin_copies],
                                [(item.destination,script_target) for item in script_copies],
                                [(cep_root/folder.name,cep_root) for folder in extension_folders],store=self.deployment_store)
                        except (OSError,ValueError) as error:
                            self.events.put(('log','Uninstall receipt could not be saved: '+str(error)))
                        for item in plugin_copies + script_copies:
                            self.events.put(('log', ('VERIFIED FILE: ' if item.destination in changed_files else 'ALREADY VERIFIED: ')+str(item.destination)))


                        for duplicate in list(obsolete_plugin_copies(target, plugin_copies)):
                            with duplicate.open('rb') as stream:
                                old_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
                            backup_root = Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'Hazzaa Plugins/Backups/Duplicates'
                            backup_root.mkdir(parents=True, exist_ok=True)
                            backup = backup_root/(old_hash+'-'+duplicate.name+'.backup')
                            install_binary(duplicate, backup)
                            duplicate.unlink()
                            self.events.put(('log',f'Backed up legacy duplicate outside After Effects: {duplicate.name} -> {backup}'))

                        if record:


                            self.events.put(('activation',record))


                    attachments = preserve_bcc_attachments(extracted,archive_path) if installers else None
                    if attachments:
                        attachments=bcc_attachment_folder(attachments)
                        self.events.put(('included_files',str(attachments)))
                    if installers:
                        key,run,finished=ArchiveDeployer._prepare_setup_package(self,installers,extracted,shared_target,
                            documents,attachments,deploy_loose,silent,(product_names or {}).get(archive_path,archive_path.stem),
                            tuple(plugin_files) if runtime_deferred else ())
                        dispatcher=getattr(self,'setup_dispatcher',None)
                        if dispatcher is not None:
                            ticket,state=dispatcher.submit(key,(product_names or {}).get(archive_path,archive_path.stem),
                                run,workspace,finished)
                            if state=='failed':failures+=1
                            elif state in {'manual','queued','duplicate'}:pending_setups+=1
                            else:
                                evidence=ticket.verified or {}
                                unverified_setups+=evidence.get('unverified',0)
                                assisted_setups+=evidence.get('assisted',0)
                        else:
                            try:
                                outcome,evidence=run()
                                if getattr(outcome,'handed_off',False):
                                    workspace.keep=True;pending_setups+=1
                                else:
                                    unverified_setups+=evidence.get('unverified',0)
                                    assisted_setups+=evidence.get('assisted',0)
                                finished(outcome,evidence,None)
                            except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
                                if getattr(error,'retry_safe',True) is False:workspace.keep=True
                                failures+=1;finished(None,None,error)
                    elif runtime_deferred:
                        def run_runtime(plugins=tuple(plugin_files),deploy=deploy_loose):
                            ensure_prerequisites(plugins,lambda message:self.events.put(('log',message)),automatic=True)
                            deploy()
                            return InstallerExit(0),{}
                        def runtime_finished(code,evidence,error):
                            if error:
                                self.events.put(('log','Runtime prerequisite/install failed: '+str(error)))
                                self.events.put(('setup_async_error',str(error)))
                        runtime_key='runtime:'+hashlib.sha256((str(archive_path)+str(target)).encode()).hexdigest()
                        dispatcher.submit(runtime_key,archive_path.stem+' (runtime prerequisite)',run_runtime,workspace,runtime_finished)
                        pending_setups+=1
                    else:
                        deploy_loose()

                    if not plugin_files and not script_files and not script_dirs and not installers and not extension_folders:
                        self.events.put(("log", "No plug-ins, scripts, or installers found."))
                        failures += 1
                    hint=pixel_sorter_hint([file.name for file in plugin_files])
                    if hint:self.events.put(('log',hint))
            except Exception as error:
                failures += 1
                self.events.put(("log", f"ERROR: {archive_path.name}: {error}"))
            self.events.put(("progress", index))
        result = {"failures":failures}
        if unverified_setups:
            result['unverified_setups'] = unverified_setups
        if assisted_setups:result['assisted_setups']=assisted_setups
        if pending_setups:result['pending_setups']=pending_setups
        self.events.put(("done", result))

    def _poll_events(self) -> None:

        if self._events_job is not None:
            try:
                self.root.after_cancel(self._events_job)
            except tk.TclError:
                pass
            self._events_job = None
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind.startswith('update_'):
                    self.updater.handle(kind, payload)
                    if getattr(self,'_update_exiting',False):
                        return
                elif kind == 'included_files':
                    key=str(Path(payload).absolute()).casefold()
                    if key in self.opened_installer_folders:continue
                    try:
                        os.startfile(payload)
                        self.opened_installer_folders.add(key)
                        self._log('Opened folder: '+payload)
                    except OSError as error:self._log('Could not open included files: '+str(error))
                elif kind == 'setup_help':
                    key=payload.get('id',payload['name'])
                    existing=self.setup_help_windows.get(key)
                    if existing is not None and existing.root.winfo_exists():existing.update(payload)
                    else:self.setup_help_windows[key]=SetupHelpWindow(self,payload)
                elif kind in {'setup_handed_off','setup_manual_closed'}:
                    existing=self.setup_help_windows.get(payload)
                    if existing is not None and existing.root.winfo_exists():
                        if kind=='setup_handed_off':existing.handed_off()
                        else:existing.manual_closed()
                elif kind=='setup_queue_count':
                    manual,queued=self.setup_dispatcher.status
                    self.setup_status.configure(text=(f'{manual} MANUAL | {queued} QUEUED' if manual or queued else
                        ('SETUP RUNNING' if self.setup_dispatcher.pending_count else '')))
                    if not self.busy and not self.setup_dispatcher.pending_count and self.archive_label['text'].startswith('BATCH PROCESSED'):
                        self.archive_label.configure(text=('SETUP NEEDS ATTENTION — COPY LOG' if self._setup_queue_failed else 'BATCH FINISHED — RESTART AFTER EFFECTS'))
                elif kind=='setup_queue_paused':
                    self.setup_status.configure(text='SETUP TRACKING LOST — COMPLETE ITS WINDOW')
                elif kind=='setup_async_error':
                    self._setup_queue_failed=True
                    self.archive_label.configure(text='SETUP NEEDS ATTENTION — COPY LOG')
                elif kind == 'setup_finished':
                    name,ok,reason=payload
                    if not ok:
                        self._setup_queue_failed=True
                        self.archive_label.configure(text='SETUP NEEDS ATTENTION — COPY LOG')
                    existing=self.setup_help_windows.get(name)
                    if existing is not None and existing.root.winfo_exists():existing.finished(ok,reason)
                elif kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    self.progress.configure(value=payload)
                elif kind == "text_popup":
                    archive_name, file_name, content = payload
                    self._show_text_popup(archive_name, file_name, content)
                elif kind == "activation":
                    self.activation_records[payload['id']] = payload
                    try:
                        self.activation_store.save(payload)
                    except OSError:
                        self._log('Activation details are available this session; Windows could not save the encrypted receipt.')
                    if payload.get('auto_open'):
                        self.open_activation(payload['id'])
                    elif self.activation_window is not None and self.activation_window.root.winfo_exists():
                        self.activation_window.update_records()
                elif kind == "credentials_help":
                    self._show_credentials_help(*payload)
                elif kind == 'support_runtime_done':
                    self.busy = False
                    repaired, error = payload
                    if error:
                        self._log('FILMCONVERT SUPPORT CHECK FAILED: ' + error)
                        self.archive_label.configure(text='FILMCONVERT SUPPORT FILES NEED ATTENTION - COPY LOG')
                    elif repaired:
                        self._log('FilmConvert resources repaired. Restart After Effects before testing Nitrate.')
                        self.archive_label.configure(text='FILMCONVERT RESOURCES REPAIRED - RESTART AFTER EFFECTS')
                elif kind == "preset_done":
                    self.busy = self.preset_scan_running = False
                    self.preset_button.configure(text="COLLECT USER PRESETS")
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                elif kind == "done":
                    self.busy = False
                    self.downloading = False
                    self.library_button.configure(text="ONLINE LIBRARY")
                    failures = payload.get('failures',0) if isinstance(payload,dict) else 0
                    unverified = payload.get('unverified_setups',0) if isinstance(payload,dict) else 0
                    assisted = payload.get('assisted_setups',0) if isinstance(payload,dict) else 0
                    pending = payload.get('pending_setups',0) if isinstance(payload,dict) else 0
                    still_pending=(self.setup_dispatcher.pending_count if hasattr(self,'setup_dispatcher') else pending)
                    self.archive_label.configure(text=(f'INSTALLATION FAILED: {failures} ERROR(S) - SEE LOG'
                        if failures else ('SETUP NEEDS ATTENTION — COPY LOG' if getattr(self,'_setup_queue_failed',False) else
                        ('BATCH PROCESSED — MANUAL / QUEUED SETUPS REMAIN' if pending and still_pending else
                        ('BATCH FINISHED — RESTART AFTER EFFECTS' if pending else
                        ('SETUP FINISHED - HOST CHECK REQUIRED' if unverified else
                        ('FILES VERIFIED (ASSISTED) - RESTART AFTER EFFECTS' if assisted else 'FILES VERIFIED - RESTART AFTER EFFECTS')))))))
                    self._log(f"Finished with {failures} error(s). See the log above." if failures else
                        ('Other items processed. Complete the open manual setup; queued setups start afterwards.' if pending and still_pending else
                        'File deployment complete. Restart After Effects to load new effects.'))
                elif kind == "library_install":
                    self.downloading = False
                    self.library_button.configure(text="ONLINE LIBRARY")
                    self.archive_label.configure(text=f"INSTALLING {payload} ITEM(S)")
                    self.progress.configure(maximum=payload, value=0)
        except queue.Empty:
            pass
        if hasattr(self, 'updater'):
            self.updater.notify_if_idle()
        self._events_job = self.root.after(100, self._poll_events)

    def open_activation(self, selected_id=None):
        if self.activation_window is None or not self.activation_window.root.winfo_exists():
            self.activation_window = ActivationWindow(self)
        self.activation_window.update_records(selected_id)
        self.activation_window.root.lift()

    def _show_credentials_help(self, installer_name, credentials, documents) -> None:
        popup = tk.Toplevel(self.root)
        popup.title("HAZZAA INSTALLER INPUT")
        popup.configure(bg=BLACK)
        popup.geometry("730x520")
        popup.transient(self.root)
        tk.Label(popup, text=installer_name, bg=BLACK, fg=WHITE, font=FONT).pack(padx=12, pady=10)
        tk.Label(popup, text="Copy these details into the matching installer or plug-in registration fields.",
                 bg=BLACK, fg=WHITE, font=FONT).pack(padx=12, pady=(0, 10))
        for kind, value in credentials.values.items():
            row = tk.Frame(popup, bg=BLACK)
            row.pack(fill="x", padx=12, pady=4)
            self._label(row, kind.upper()).pack(side="left", padx=(0, 10))
            entry = tk.Entry(row, bg=PANEL_DARK, fg=WHITE, readonlybackground=PANEL_DARK,
                             relief="flat", font=FONT)
            entry.insert(0, value)
            entry.configure(state="readonly")
            entry.pack(side="left", fill="x", expand=True)
            def copy_value(text=value):
                popup.clipboard_clear()
                popup.clipboard_append(text)
            self._button(row, "COPY", copy_value).pack(side="right", padx=(8, 0))
        body = tk.Text(popup, bg=PANEL_DARK, fg=WHITE, font=FONT, wrap="word", relief="flat")
        body.pack(fill="both", expand=True, padx=12, pady=10)
        body.insert("1.0", "\n\n".join(f"{path.name}\n{content}" for path, content in documents))
        body.configure(state="disabled")
        self._button(popup, "CLOSE", popup.destroy).pack(anchor="e", padx=12, pady=(0, 12))
        self._apply_cursor(popup)
        popup.update_idletasks()
        self._set_dark_titlebar(popup)

    def _show_text_popup(self, archive_name: str, file_name: str, content: str) -> None:
        popup = tk.Toplevel(self.root)
        popup.title(file_name.upper())
        popup.configure(bg=BLACK)
        popup.geometry("620x420")
        popup.transient(self.root)
        tk.Label(popup, text=f"{archive_name}  /  {file_name}", bg=BLACK, fg=WHITE, font=FONT).pack(anchor="w", padx=12, pady=10)
        body = tk.Text(popup, bg=PANEL_DARK, fg=WHITE, insertbackground=WHITE, relief="flat", wrap="word", font=FONT)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        body.insert("1.0", content)
        body.configure(state="disabled")
        close_button = self._button(popup, "CLOSE", popup.destroy)
        close_button.pack(anchor="e", padx=12, pady=(0, 12))
        self._apply_cursor(popup)
        popup.update_idletasks()
        self._set_dark_titlebar(popup)

    def _log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        if getattr(self, 'log_path', None):
            try:
                with self.log_path.open('a', encoding='utf-8') as stream:
                    stream.write(time.strftime('%H:%M:%S ')+message+'\n')
            except OSError:
                self.log_path = None

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log.get('1.0','end-1c'))

    def _animate_snow(self) -> None:
        if not self.root.winfo_exists():
            return
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        now = time.perf_counter()
        elapsed = min(max(now - getattr(self, "_snow_last_time", now), 0.0), 0.1)
        self._snow_last_time = now
        for particle in self.snow:
            particle["y"] += particle["speed"] * elapsed
            particle["x"] += (particle["drift"] + math.sin(now * 1.7 + particle["phase"]) * 5) * elapsed
            if particle["y"] > height:
                particle["y"] = -particle["size"]
                particle["x"] = random.uniform(0, width)
            particle["x"] %= width
            x, y, size = particle["x"], particle["y"], particle["size"]
            self.canvas.coords(particle["item"], x, y, x + size, y + size)
        self._snow_job = self.root.after(PARTICLE_FRAME_MS, self._animate_snow)

    def close(self) -> None:
        if self.busy or (hasattr(self,'setup_dispatcher') and self.setup_dispatcher.automatic_active):
            messagebox.showinfo("Processing", "Wait until archive processing and any installer finish before closing.")
            return
        if hasattr(self,'setup_dispatcher'):
            for path in self.setup_dispatcher.retained_paths:self._log('Pending setup files retained: '+path)
        self.root.destroy()


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == '--apply-update':
        raise SystemExit(helper_main(sys.argv[2]))
    root = tk.Tk()
    app = ArchiveDeployer(root)
    if len(sys.argv) == 3 and sys.argv[1] == '--update-health':
        try:
            acknowledge_update(sys.argv[2])
            app._log('App update installed successfully.')
        except Exception as error:
            app._log('Update startup verification failed: '+str(error))
            root.destroy()
            raise SystemExit(1)
    root.mainloop()


if __name__ == "__main__":
    main()
