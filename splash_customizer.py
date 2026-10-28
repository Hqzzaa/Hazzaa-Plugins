from __future__ import annotations

import ctypes
import hashlib
import io
import os
import queue
import shutil
import stat
import subprocess
import tempfile
import threading
import tkinter as tk
from ctypes import wintypes
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, ImageTk


BLACK = "#000000"
PANEL = "#181818"
WHITE = "#FFFFFF"
FONT = ("Courier New", 10, "bold")


class SplashResourceError(RuntimeError):
    pass


def _resource_pointer(name: str | int) -> tuple[ctypes.c_void_p, object | None]:

    if isinstance(name, int):
        return ctypes.c_void_p(name), None
    keepalive = ctypes.c_wchar_p(name)
    return ctypes.cast(keepalive, ctypes.c_void_p), keepalive


def _decode_resource_name(pointer: int | None) -> str | int:
    if not pointer:
        raise SplashResourceError("Windows returned an empty resource identifier.")
    if pointer <= 0xFFFF:
        return int(pointer)
    return ctypes.wstring_at(pointer)


def _resource_api():
    if os.name != "nt":
        raise SplashResourceError("Splash customization is available on Windows only.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LoadLibraryExW.argtypes = (wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD)
    kernel.LoadLibraryExW.restype = wintypes.HMODULE
    kernel.FreeLibrary.argtypes = (wintypes.HMODULE,)
    kernel.FreeLibrary.restype = wintypes.BOOL
    kernel.EnumResourceNamesW.argtypes = (wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t)
    kernel.EnumResourceNamesW.restype = wintypes.BOOL
    kernel.EnumResourceLanguagesW.argtypes = (
        wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t
    )
    kernel.EnumResourceLanguagesW.restype = wintypes.BOOL
    kernel.FindResourceExW.argtypes = (wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p, wintypes.WORD)
    kernel.FindResourceExW.restype = wintypes.HRSRC
    kernel.LoadResource.argtypes = (wintypes.HMODULE, wintypes.HRSRC)
    kernel.LoadResource.restype = wintypes.HGLOBAL
    kernel.LockResource.argtypes = (wintypes.HGLOBAL,)
    kernel.LockResource.restype = ctypes.c_void_p
    kernel.SizeofResource.argtypes = (wintypes.HMODULE, wintypes.HRSRC)
    kernel.SizeofResource.restype = wintypes.DWORD
    kernel.BeginUpdateResourceW.argtypes = (wintypes.LPCWSTR, wintypes.BOOL)
    kernel.BeginUpdateResourceW.restype = wintypes.HANDLE
    kernel.UpdateResourceW.argtypes = (
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wintypes.WORD,
        ctypes.c_void_p, wintypes.DWORD,
    )
    kernel.UpdateResourceW.restype = wintypes.BOOL
    kernel.EndUpdateResourceW.argtypes = (wintypes.HANDLE, wintypes.BOOL)
    kernel.EndUpdateResourceW.restype = wintypes.BOOL
    return kernel


def _win_error(action: str) -> SplashResourceError:
    error = ctypes.get_last_error()
    return SplashResourceError(f"{action}: {ctypes.FormatError(error).strip()} (Windows error {error})")


def read_splash_resources(dll_path: Path) -> list[dict]:

    kernel = _resource_api()
    module = kernel.LoadLibraryExW(str(dll_path), None, 0x00000002 | 0x00000020)
    if not module:
        raise _win_error(f"Cannot inspect {dll_path.name}")

    png_type, png_keepalive = _resource_pointer("PNG")
    found: list[dict] = []
    enum_name_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t
    )
    enum_lang_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.WORD, ctypes.c_ssize_t,
    )

    try:
        def on_name(_module, _type, name_pointer, _param):
            name = _decode_resource_name(name_pointer)
            if not isinstance(name, str) or not name.casefold().startswith(("ae_splash", "ae_beta_splash")):
                return True
            name_pointer_value, name_keepalive = _resource_pointer(name)
            languages: list[int] = []

            def on_language(_lang_module, _type, _name, language, _lang_param):
                languages.append(int(language))
                return True

            lang_callback = enum_lang_type(on_language)
            ok = kernel.EnumResourceLanguagesW(module, png_type, name_pointer_value, lang_callback, 0)
            if not ok and not languages:
                return True

            for language in languages:
                resource = kernel.FindResourceExW(module, png_type, name_pointer_value, language)
                if not resource:
                    continue
                size = kernel.SizeofResource(module, resource)
                loaded = kernel.LoadResource(module, resource)
                locked = kernel.LockResource(loaded) if loaded else None
                if not locked or not size:
                    continue
                data = ctypes.string_at(locked, size)
                try:
                    with Image.open(io.BytesIO(data)) as image:
                        image.load()
                        dimensions = image.size
                except Exception:
                    continue
                found.append({"name": name, "language": language, "size": dimensions, "data": data})
            return True

        callback = enum_name_type(on_name)
        if not kernel.EnumResourceNamesW(module, png_type, callback, 0) and not found:
            raise _win_error(f"Cannot enumerate PNG resources in {dll_path.name}")
    finally:
        kernel.FreeLibrary(module)


    _ = png_keepalive
    if not found:
        raise SplashResourceError(f"No compatible AE_SPLASH PNG resources were found in {dll_path.name}.")
    return found


@dataclass
class SplashOverlay:
    keep_header: bool = True
    header_x: float = 0.035
    header_y: float = 0.035
    header_scale: float = 1.0
    text: str = ""
    text_x: float = 0.055
    text_y: float = 0.27
    font_size: int = 28
    font_path: str = ""
    text_color: str = WHITE
    outline_color: str = BLACK


def header_artwork(original: Image.Image) -> Image.Image:

    width, height = original.size
    crop = original.convert("RGBA").crop((round(width * .035), round(height * .035),
                                          round(width * .42), round(height * .215)))
    data = bytearray()
    pixels = crop.get_flattened_data() if hasattr(crop, "get_flattened_data") else crop.getdata()
    for red, green, blue, alpha in pixels:
        maximum = max(red, green, blue)
        if maximum:
            data.extend((round(red * 255 / maximum), round(green * 255 / maximum),
                         round(blue * 255 / maximum), round(alpha * maximum / 255)))
        else:
            data.extend((0, 0, 0, 0))
    return Image.frombytes("RGBA", crop.size, bytes(data))


def compose_splash_image(source: Image.Image, size: tuple[int, int], zoom: float = 1.0,
                         offset_x: float = 0, offset_y: float = 0) -> Image.Image:

    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError("Splash dimensions must be positive.")
    image = source.convert("RGBA")
    zoom = min(max(float(zoom), 1.0), 4.0)
    scale = max(width / image.width, height / image.height) * zoom
    scaled_size = (max(width, round(image.width * scale)), max(height, round(image.height * scale)))
    image = image.resize(scaled_size, Image.Resampling.LANCZOS)
    max_x = scaled_size[0] - width
    max_y = scaled_size[1] - height
    left = round(max_x * (min(max(float(offset_x), -100), 100) + 100) / 200)
    top = round(max_y * (min(max(float(offset_y), -100), 100) + 100) / 200)
    image = image.crop((left, top, left + width, top + height))
    black = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    black.alpha_composite(image)


    def horizontal(alpha: int, extent: int, reverse: bool = False) -> Image.Image:
        extent = max(1, min(extent, width))
        values = [round(alpha * max(0.0, 1.0 - (x if not reverse else width - 1 - x) / extent))
                  for x in range(width)]
        return Image.frombytes("L", (width, 1), bytes(values)).resize((width, height))

    def vertical(alpha: int, extent: int, reverse: bool = False) -> Image.Image:
        extent = max(1, min(extent, height))
        values = [round(alpha * max(0.0, 1.0 - (y if not reverse else height - 1 - y) / extent))
                  for y in range(height)]
        return Image.frombytes("L", (1, height), bytes(values)).resize((width, height))

    mask = horizontal(255, round(width * 0.38))
    for edge_mask in (
        horizontal(135, round(width * 0.20), reverse=True),
        vertical(95, round(height * 0.16)),
        vertical(95, round(height * 0.16), reverse=True),
    ):
        mask = ImageChops.lighter(mask, edge_mask)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay.putalpha(mask)
    black.alpha_composite(overlay)
    return black.convert("RGB")


def compose_with_overlay(source: Image.Image, size: tuple[int, int], zoom=1.0,
                         offset_x=0, offset_y=0, style: SplashOverlay | None = None,
                         original: Image.Image | None = None) -> Image.Image:
    black = compose_splash_image(source, size, zoom, offset_x, offset_y).convert("RGBA")
    if style is None:
        return black.convert("RGB")
    width, height = size
    if style.keep_header and original is not None:
        header = header_artwork(original)
        scale = width / original.width * max(.3, min(3.0, style.header_scale))
        header = header.resize((max(1, round(header.width * scale)),
                                max(1, round(header.height * scale))), Image.Resampling.LANCZOS)
        black.alpha_composite(header, (round(width * style.header_x), round(height * style.header_y)))
    if style.text.strip():
        size_px = max(8, round(style.font_size * width / 766))
        try:
            font = ImageFont.truetype(style.font_path or "arial.ttf", size_px)
        except OSError:
            font = ImageFont.load_default(size=size_px)
        ImageDraw.Draw(black).multiline_text((round(width * style.text_x), round(height * style.text_y)),
            style.text, font=font, fill=style.text_color, spacing=max(2, size_px // 5),
            stroke_width=max(1, size_px // 18), stroke_fill=style.outline_color)
    return black.convert("RGB")


def load_still_image(image_path: Path) -> Image.Image:

    with Image.open(image_path) as source:
        source.seek(0)
        source.load()
        return ImageOps.exif_transpose(source).convert("RGBA").copy()


def pan_from_drag(start_x: float, start_y: float, delta_x: float, delta_y: float,
                  area_width: float, area_height: float) -> tuple[float, float]:

    if area_width <= 0 or area_height <= 0:
        return start_x, start_y
    x = min(100.0, max(-100.0, start_x + delta_x * 200.0 / area_width))
    y = min(100.0, max(-100.0, start_y + delta_y * 200.0 / area_height))
    return x, y


def zoom_from_scroll(current: float, wheel_delta: float) -> float:

    return round(min(4.0, max(1.0, current + wheel_delta / 120.0 * 0.1)), 2)


def _afterfx_is_running() -> bool:

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise _win_error("Cannot check whether After Effects is open")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        has_process = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while has_process:
            if entry.szExeFile.casefold() == "afterfx.exe":
                return True
            has_process = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return False


def _write_resources(dll_path: Path, resources: list[dict], image: Image.Image,
                     zoom: float, offset_x: float, offset_y: float,
                     overlay: SplashOverlay | None = None, originals: list[dict] | None = None) -> int:
    kernel = _resource_api()
    update = kernel.BeginUpdateResourceW(str(dll_path), False)
    if not update:
        raise _win_error(f"Cannot edit {dll_path.name}")
    type_pointer, type_keepalive = _resource_pointer("PNG")
    updated = 0
    finished = False
    try:
        for resource in resources:
            reference = next((item for item in (originals or resources)
                              if (item["name"], item["language"]) == (resource["name"], resource["language"])), resource)
            original = Image.open(io.BytesIO(reference["data"])).convert("RGBA")
            output = compose_with_overlay(image, resource["size"], zoom, offset_x, offset_y, overlay, original)
            encoded = io.BytesIO()
            output.save(encoded, format="PNG", optimize=True)
            payload = encoded.getvalue()
            buffer = ctypes.create_string_buffer(payload)
            name_pointer, name_keepalive = _resource_pointer(resource["name"])
            if not kernel.UpdateResourceW(update, type_pointer, name_pointer,
                                          resource["language"], buffer, len(payload)):
                raise _win_error(f"Cannot replace splash resource {resource['name']}")
            _ = name_keepalive
            updated += 1
        if not kernel.EndUpdateResourceW(update, False):
            raise _win_error(f"Cannot save splash resources in {dll_path.name}")
        finished = True
    finally:
        _ = type_keepalive
        if not finished:
            kernel.EndUpdateResourceW(update, True)
    return updated


def backup_path_for(dll_path: Path) -> Path:

    local_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "Documents"))
    install_key = hashlib.sha256(str(Path(dll_path).resolve()).casefold().encode("utf-8")).hexdigest()[:16]
    return local_data / "Hazzaa Plugins" / "Backups" / install_key / (Path(dll_path).name + ".HAZZAA_BACKUP")


def apply_splash_image(dll_path: Path, image: Image.Image, zoom: float = 1.0,
                       offset_x: float = 0, offset_y: float = 0,
                       overlay: SplashOverlay | None = None) -> tuple[Path, int]:

    if _afterfx_is_running():
        raise SplashResourceError("Close After Effects before changing its startup image.")
    dll_path = Path(dll_path)
    if not dll_path.is_file():
        raise SplashResourceError(f"After Effects resource DLL was not found: {dll_path}")
    resources = read_splash_resources(dll_path)
    backup = backup_path_for(dll_path)
    if not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dll_path, backup)

    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(prefix="hazzaa_splash_", suffix=".dll",
                                         dir=dll_path.parent, delete=False) as temp:
            temp_name = Path(temp.name)
        shutil.copyfile(dll_path, temp_name)
        temp_name.chmod(0o644)
        originals = read_splash_resources(backup)
        count = _write_resources(temp_name, resources, image, zoom, offset_x, offset_y, overlay, originals)
        verified = read_splash_resources(temp_name)
        expected = {(item["name"], item["language"]) for item in resources}
        actual = {(item["name"], item["language"]) for item in verified}
        if expected != actual:
            raise SplashResourceError("The updated DLL did not retain every splash variant.")
        if not dll_path.stat().st_mode & stat.S_IWRITE:
            dll_path.chmod(0o644)
        os.replace(temp_name, dll_path)
        temp_name = None
        return backup, count
    except Exception:
        if temp_name is not None:
            try:
                temp_name.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def restore_splash_backup(dll_path: Path) -> Path:

    if _afterfx_is_running():
        raise SplashResourceError("Close After Effects before restoring its startup image.")
    dll_path = Path(dll_path)
    backup = backup_path_for(dll_path)
    if not backup.is_file():
        raise SplashResourceError(f"No HAZZAA backup exists for {dll_path.name}.")
    read_splash_resources(backup)
    with tempfile.NamedTemporaryFile(prefix="hazzaa_restore_", suffix=".dll",
                                     dir=dll_path.parent, delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        shutil.copyfile(backup, temp_path)
        temp_path.chmod(0o644)
        if dll_path.exists() and not dll_path.stat().st_mode & stat.S_IWRITE:
            dll_path.chmod(0o644)
        os.replace(temp_path, dll_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return dll_path


class SplashCustomizer(tk.Toplevel):


    def __init__(self, parent: tk.Tk, dll_path: Path, cursor_callback, dark_titlebar_callback):
        super().__init__(parent)
        self.parent = parent
        self.dll_path = Path(dll_path)
        self.cursor_callback = cursor_callback
        self.dark_titlebar_callback = dark_titlebar_callback
        self.title("HAZZAA SPLASH IMAGE | IMAGE EDITION")
        self.geometry("1100x860")
        self.minsize(1000, 790)
        self.configure(bg=BLACK)
        self.protocol("WM_DELETE_WINDOW", self._close)

        self.resources = read_splash_resources(self.dll_path)
        self.preview_resource = next(
            (item for item in self.resources if item["name"].casefold() == "ae_splash"),
            self.resources[0],
        )
        self.original_image = Image.open(io.BytesIO(self.preview_resource["data"])).convert("RGBA")
        backup = backup_path_for(self.dll_path)
        references = read_splash_resources(backup) if backup.is_file() else self.resources
        reference = next((item for item in references if item["name"] == self.preview_resource["name"]), references[0])
        self.header_original = Image.open(io.BytesIO(reference["data"])).convert("RGBA")
        self.overlay_style = SplashOverlay()
        self.keep_header_var = tk.BooleanVar(value=True)
        self.header_scale_var = tk.DoubleVar(value=1.0)
        self.custom_text_var = tk.StringVar(value="")
        self.text_mode_label = tk.StringVar(value="TEXT BELOW ADOBE")
        self.font_size_var = tk.IntVar(value=28)
        self.move_mode_var = tk.StringVar(value="IMAGE")
        self.font_name_var = tk.StringVar(value="DEFAULT FONT")
        self.apply_queue = queue.Queue()
        self.applying = False
        self.source_path: Path | None = None
        self.source_image: Image.Image | None = None
        self.preview_photo = None
        self.zoom_var = tk.DoubleVar(value=1.25)
        self.x_var = tk.DoubleVar(value=0)
        self.y_var = tk.DoubleVar(value=0)
        self.zoom_text_var = tk.StringVar(value="ZOOM 1.25x")
        self.status_var = tk.StringVar(value=f"{len(self.resources)} SPLASH VARIANTS FOUND")
        self._build()
        self.cursor_callback(self)
        self.update_idletasks()
        self.dark_titlebar_callback(self)
        self._render_preview()

    def _build(self) -> None:
        header = tk.Frame(self, bg=BLACK)
        header.pack(fill="x", padx=18, pady=(14, 8))
        tk.Label(header, text="CUSTOM AFTER EFFECTS STARTUP IMAGE", bg=BLACK, fg=WHITE,
                 font=("Courier New", 13, "bold")).pack(side="left")
        tk.Button(header, text="SELECT IMAGE", command=self._choose_image,
                  bg=PANEL, fg=WHITE, activebackground="#333333", activeforeground=WHITE,
                  relief="flat", font=FONT, padx=10, pady=7).pack(side="right")

        body = tk.Frame(self, bg=BLACK)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        self.preview = tk.Canvas(body, bg=BLACK, highlightthickness=0, height=410)
        self.preview.pack(side="left", fill="both", expand=True)
        self.preview.bind("<Configure>", lambda _event: self._render_preview())

        sidebar = tk.Frame(body, bg=BLACK, width=255)
        sidebar.pack(side="right", fill="y", padx=(14, 0))
        sidebar.pack_propagate(False)
        settings = tk.Canvas(sidebar, bg=BLACK, highlightthickness=0, width=235)
        scrollbar = tk.Scrollbar(sidebar, orient="vertical", command=settings.yview,
                                 bg=PANEL, troughcolor=BLACK, activebackground="#444444")
        scrollbar.pack(side="right", fill="y")
        settings.pack(side="left", fill="both", expand=True)
        settings.configure(yscrollcommand=scrollbar.set)
        lens_panel = tk.Frame(settings, bg=BLACK, width=230)
        settings.create_window(0, 0, anchor="nw", window=lens_panel, width=230)
        lens_panel.bind("<Configure>", lambda _: settings.configure(scrollregion=settings.bbox("all")))
        tk.Label(lens_panel, text="LENS", bg=BLACK, fg=WHITE, font=FONT).pack(pady=(2, 8))


        self.lens = tk.Canvas(lens_panel, width=190, height=190, bg=BLACK, highlightthickness=0)
        self.lens.pack(pady=(0, 8))
        for widget in (self.preview, self.lens):
            widget.bind("<ButtonPress-1>", self._begin_pan)
            widget.bind("<B1-Motion>", self._continue_pan)
            widget.bind("<ButtonRelease-1>", self._end_pan)
            widget.bind("<MouseWheel>", self._on_mouse_wheel)
            widget.bind("<Button-4>", lambda event: self._on_mouse_wheel(event, 120))
            widget.bind("<Button-5>", lambda event: self._on_mouse_wheel(event, -120))
        tk.Label(lens_panel, textvariable=self.zoom_text_var, bg=BLACK, fg=WHITE, font=FONT).pack(pady=(0, 8))
        tk.Label(lens_panel, text="DRAG: MOVE X/Y\nSCROLL: ZOOM", bg=BLACK, fg=WHITE,
                 font=FONT, justify="center").pack(pady=(0, 10))
        tk.Button(lens_panel, text="RESET VIEW", command=self._reset_view,
                  bg=PANEL, fg=WHITE, activebackground="#333333", activeforeground=WHITE,
                  relief="flat", font=FONT, padx=8, pady=6).pack()

        tk.Checkbutton(lens_panel, text="SHOW ADOBE TITLE", variable=self.keep_header_var,
                       command=self._toggle_header, bg=BLACK, fg=WHITE, selectcolor=PANEL,
                       activebackground=BLACK, activeforeground=WHITE, font=FONT).pack(pady=(16, 4))
        tk.Label(lens_panel, text="HEADER SIZE", bg=BLACK, fg=WHITE, font=FONT).pack()
        tk.Scale(lens_panel, from_=0.3, to=2.5, resolution=.05, orient="horizontal",
                 variable=self.header_scale_var, command=lambda _: self._render_preview(),
                 bg=BLACK, fg=WHITE, troughcolor=PANEL, highlightthickness=0,
                 font=FONT, length=215).pack()
        tk.Label(lens_panel, textvariable=self.text_mode_label, bg=BLACK, fg=WHITE, font=FONT).pack(pady=(10, 4))
        self.custom_text_editor = tk.Text(lens_panel, height=3, width=20, wrap="none",
            bg=PANEL, fg=WHITE, insertbackground=WHITE, relief="flat", font=FONT, undo=True)
        self.custom_text_editor.pack(fill="x", ipady=5)
        self.custom_text_editor.bind("<<Modified>>", self._text_edited)
        self.custom_text_var.trace_add("write", self._text_variable_changed)
        tk.Label(lens_panel, text="ENTER: NEW LINE", bg=BLACK, fg=WHITE, font=FONT).pack(pady=4)
        tk.Button(lens_panel, text="CHOOSE FONT .TTF / .OTF", command=self._choose_font,
                  bg=PANEL, fg=WHITE, relief="flat", font=FONT, pady=6).pack(fill="x", pady=5)
        tk.Label(lens_panel, textvariable=self.font_name_var, bg=BLACK, fg=WHITE, font=FONT,
                 wraplength=230).pack()
        colors = tk.Frame(lens_panel, bg=BLACK)
        colors.pack(fill="x", pady=6)
        for label, attribute in (("TEXT COLOR", "text_color"), ("OUTLINE", "outline_color")):
            tk.Button(colors, text=label, command=lambda name=attribute: self._choose_color(name),
                      bg=PANEL, fg=WHITE, relief="flat", font=FONT, pady=5).pack(
                          side="left", fill="x", expand=True, padx=2)
        tk.Scale(lens_panel, label="TEXT SIZE", from_=10, to=140, orient="horizontal",
                 variable=self.font_size_var, command=lambda _: self._render_preview(),
                 bg=BLACK, fg=WHITE, troughcolor=PANEL, highlightthickness=0,
                 font=FONT, length=215).pack(pady=5)
        tk.Label(lens_panel, text="DRAG PREVIEW TO MOVE", bg=BLACK, fg=WHITE, font=FONT).pack(pady=(8, 4))
        modes = tk.OptionMenu(lens_panel, self.move_mode_var, "IMAGE", "ADOBE HEADER", "CUSTOM TEXT")
        modes.configure(bg=PANEL, fg=WHITE, activebackground=PANEL, activeforeground=WHITE,
                        relief="flat", highlightthickness=0, font=FONT)
        modes["menu"].configure(bg=PANEL, fg=WHITE, font=FONT)
        modes.pack(fill="x")
        for child in lens_panel.winfo_children():
            if isinstance(child, tk.Label):
                child.bind("<MouseWheel>", lambda event: settings.yview_scroll(-int(event.delta / 120), "units"))

        footer = tk.Frame(self, bg=BLACK)
        footer.pack(fill="x", padx=18, pady=(6, 15))
        tk.Label(footer, textvariable=self.status_var, bg=BLACK, fg=WHITE, font=FONT,
                 anchor="w", wraplength=1030).pack(fill="x", pady=(0, 8))
        actions = tk.Frame(footer, bg=BLACK)
        actions.pack(fill="x")
        tk.Button(actions, text="RESTORE BACKUP", command=self._restore,
                  bg=PANEL, fg=WHITE, activebackground="#333333", activeforeground=WHITE,
                  relief="flat", font=FONT, padx=10, pady=7).pack(side="right", padx=(8, 0))
        tk.Button(actions, text="APPLY TO AFTER EFFECTS", command=self._apply,
                  bg=PANEL, fg=WHITE, activebackground="#333333", activeforeground=WHITE,
                  relief="flat", font=FONT, padx=10, pady=7).pack(side="right")
        tk.Button(actions, text="START AE", command=self._launch_ae,
                  bg=PANEL, fg=WHITE, activebackground="#333333", activeforeground=WHITE,
                  relief="flat", font=FONT, padx=10, pady=7).pack(side="left", padx=(8, 0))

    def _text_edited(self, event=None):
        if self.custom_text_editor.edit_modified():
            self.custom_text_editor.edit_modified(False)
            self.custom_text_var.set(self.custom_text_editor.get("1.0", "end-1c"))

    def _text_variable_changed(self, *_):
        text=self.custom_text_var.get().replace("\\n", "\n")
        if self.custom_text_editor.get("1.0", "end-1c")!=text:
            self.custom_text_editor.delete("1.0", "end")
            self.custom_text_editor.insert("1.0", text)
            self.custom_text_editor.edit_modified(False)
        self._render_preview()

    def _begin_pan(self, event) -> None:
        widget = event.widget
        mode = "IMAGE" if widget is self.lens else self.move_mode_var.get()
        self._drag_mode = mode
        if mode != "IMAGE":
            style = self.overlay_style
            x, y = ((style.header_x, style.header_y) if mode == "ADOBE HEADER"
                    else (style.text_x, style.text_y))
            self._overlay_drag = (event.x, event.y, x, y)
            return
        self._pan_start = (event.x, event.y, self.x_var.get(), self.y_var.get(),
                           max(widget.winfo_width(), 1), max(widget.winfo_height(), 1))

    def _continue_pan(self, event) -> None:
        if getattr(self, "_drag_mode", "IMAGE") != "IMAGE" and hasattr(self, "_overlay_drag"):
            start_x, start_y, x, y = self._overlay_drag
            width, height = getattr(self, "_preview_dimensions", (1, 1))
            x = min(.95, max(0, x + (event.x - start_x) / max(width, 1)))
            y = min(.95, max(0, y + (event.y - start_y) / max(height, 1)))
            if self._drag_mode == "ADOBE HEADER":
                self.overlay_style.header_x, self.overlay_style.header_y = x, y
            else:
                self.overlay_style.text_x, self.overlay_style.text_y = x, y
            self._render_preview()
            return
        if not hasattr(self, "_pan_start"):
            return
        start_x, start_y, image_x, image_y, area_width, area_height = self._pan_start
        image_x, image_y = pan_from_drag(image_x, image_y, event.x - start_x,
                                         event.y - start_y, area_width, area_height)
        self.x_var.set(image_x)
        self.y_var.set(image_y)
        self._render_preview()

    def _end_pan(self, _event) -> None:
        if hasattr(self, "_pan_start"):
            del self._pan_start
        if hasattr(self, "_overlay_drag"):
            del self._overlay_drag

    def _on_mouse_wheel(self, event, wheel_delta: float | None = None):
        delta = wheel_delta if wheel_delta is not None else event.delta
        self.zoom_var.set(zoom_from_scroll(self.zoom_var.get(), delta))
        self.zoom_text_var.set(f"ZOOM {self.zoom_var.get():.2f}x")
        self._render_preview()
        return "break"

    def _reset_view(self) -> None:
        self.zoom_var.set(1.25)
        self.x_var.set(0)
        self.y_var.set(0)
        self.zoom_text_var.set("ZOOM 1.25x")
        self.overlay_style.header_x, self.overlay_style.header_y = .035, .035
        self.overlay_style.text_x, self.overlay_style.text_y = .055, .27
        self._render_preview()

    def _choose_font(self) -> None:
        selected = filedialog.askopenfilename(parent=self, title="Choose text font",
            initialdir=str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"),
            filetypes=[("Font files", "*.ttf *.otf *.ttc")])
        if not selected:
            return
        try:
            ImageFont.truetype(selected, 28)
        except OSError as error:
            messagebox.showerror("Font", str(error), parent=self)
            return
        self.overlay_style.font_path = selected
        self.font_name_var.set(Path(selected).name)
        self._render_preview()

    def _toggle_header(self) -> None:

        style = self.overlay_style
        if self.keep_header_var.get():
            style.text_x = style.header_x
            style.text_y = min(.9, style.header_y + .18 * self.header_scale_var.get() + .03)
            self.text_mode_label.set("TEXT BELOW ADOBE")
        else:
            style.text_x, style.text_y = style.header_x, style.header_y
            self.text_mode_label.set("REPLACE ADOBE TITLE")
            if not self.custom_text_var.get():
                self.custom_text_var.set("Adobe\\nAfter Effects")
        self.move_mode_var.set("CUSTOM TEXT")
        self._render_preview()

    def _choose_color(self, attribute: str) -> None:
        selected = colorchooser.askcolor(parent=self, title="Choose text color",
                                         initialcolor=getattr(self.overlay_style, attribute))[1]
        if selected:
            setattr(self.overlay_style, attribute, selected)
            self._render_preview()

    def _current_overlay(self) -> SplashOverlay:
        self.overlay_style.keep_header = self.keep_header_var.get()
        self.overlay_style.header_scale = self.header_scale_var.get()
        self.overlay_style.text = self.custom_text_var.get().replace("\\n", "\n")
        self.overlay_style.font_size = self.font_size_var.get()
        return self.overlay_style

    def _choose_image(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self,
            title="Choose startup artwork",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.ico *.avif"),
                       ("All files", "*.*")],
        )
        if not selected:
            return
        try:
            image = load_still_image(Path(selected))
        except Exception as error:
            messagebox.showerror("Image error", f"Could not open that image:\n{error}", parent=self)
            return
        self.source_path = Path(selected)
        self.source_image = image
        self.status_var.set(f"{self.source_path.name}  |  {len(self.resources)} SPLASH SIZES")
        self._render_preview()

    def _render_preview(self) -> None:
        if not self.winfo_exists():
            return
        source = self.source_image if self.source_image is not None else self.original_image
        try:
            rendered = compose_with_overlay(source, self.preview_resource["size"],
                self.zoom_var.get(), self.x_var.get(), self.y_var.get(),
                self._current_overlay(), self.header_original)
            max_w = max(1, self.preview.winfo_width() - 8)
            max_h = max(1, self.preview.winfo_height() - 8)
            rendered.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
            self._preview_dimensions = rendered.size
            self.preview_photo = ImageTk.PhotoImage(rendered, master=self)
            self.preview.delete("all")
            self.preview.create_image(self.preview.winfo_width() // 2, self.preview.winfo_height() // 2,
                                      image=self.preview_photo, anchor="center")
            self._render_lens(rendered)
        except (tk.TclError, ValueError):
            pass

    def _render_lens(self, rendered: Image.Image) -> None:
        lens_size = min(max(self.lens.winfo_width(), 1), max(self.lens.winfo_height(), 1)) - 22
        lens_size = max(lens_size, 1)
        center_x = min(max((self.x_var.get() + 100) / 200, 0), 1)
        center_y = min(max((self.y_var.get() + 100) / 200, 0), 1)
        crop = ImageOps.fit(rendered, (lens_size, lens_size), method=Image.Resampling.LANCZOS,
                            centering=(center_x, center_y)).convert("RGBA")
        mask = Image.new("L", (lens_size, lens_size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, lens_size - 1, lens_size - 1), fill=255)
        crop.putalpha(mask)
        self.lens_photo = ImageTk.PhotoImage(crop, master=self)
        self.lens.delete("all")
        center = self.lens.winfo_width() // 2
        self.lens.create_image(center, self.lens.winfo_height() // 2,
                               image=self.lens_photo, anchor="center")
        radius = lens_size // 2
        self.lens.create_oval(center - radius, center - radius, center + radius, center + radius,
                              outline=WHITE, width=2)
        self.lens.create_oval(center - radius + 5, center - radius + 5,
                              center + radius - 5, center + radius - 5,
                              outline="#666666", width=1)

    def _apply(self) -> None:
        if self.applying:
            return
        if self.source_image is None:
            messagebox.showinfo("Choose artwork", "Select an image first.", parent=self)
            return
        image = self.source_image.copy()
        style = replace(self._current_overlay())
        zoom, x, y = self.zoom_var.get(), self.x_var.get(), self.y_var.get()
        self.applying = True
        self.status_var.set("SAVING STARTUP IMAGE...")
        def work():
            try:
                backup, count = apply_splash_image(self.dll_path, image, zoom, x, y, style)
                self.apply_queue.put(("done", (backup, count)))
            except Exception as error:
                self.apply_queue.put(("error", str(error)))
        threading.Thread(target=work, daemon=False).start()
        self.after(100, self._poll_apply)

    def _poll_apply(self):
        try:
            kind, result = self.apply_queue.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_apply)
            return
        self.applying = False
        if kind == "error":
            self.status_var.set("STARTUP UPDATE FAILED")
            messagebox.showerror("Could not apply splash", result, parent=self)
            return
        backup, count = result
        self.status_var.set("STATIC STARTUP IMAGE SAVED")
        text = f"Updated {count} splash image size(s)."
        messagebox.showinfo("Splash updated", text + f"\nBackup: {backup}", parent=self)

    def _launch_ae(self):
        if self.applying:
            return
        executable = self.dll_path.parent / "AfterFX.exe"
        try:
            if _afterfx_is_running():
                messagebox.showinfo("After Effects", "Close After Effects first to test its startup screen.", parent=self)
                return
            subprocess.Popen([str(executable)], cwd=str(executable.parent), shell=False)
        except OSError as error:
            messagebox.showerror("After Effects", str(error), parent=self)

    def _close(self):
        if self.applying:
            messagebox.showinfo("Updating splash", "Wait until the startup update finishes.", parent=self)
            return
        self.destroy()

    def _restore(self) -> None:
        if self.applying:
            return
        try:
            restore_splash_backup(self.dll_path)
        except Exception as error:
            messagebox.showerror("Could not restore", str(error), parent=self)
            return
        messagebox.showinfo("Restored", "The original After Effects splash resource was restored.", parent=self)
