from __future__ import annotations
from test_support import plugin_bytes

import importlib.util
import os
import shutil
import subprocess
import tempfile
import time
import tkinter as tk
import unittest
import zipfile
from pathlib import Path
from queue import Queue
from unittest import mock

from PIL import Image

from splash_customizer import (
    SplashCustomizer,
    compose_splash_image,
    load_still_image,
    pan_from_drag,
    read_splash_resources,
    zoom_from_scroll,
)


SOURCE = Path(__file__).with_name("ae_archive_deployer.py")
SPEC = importlib.util.spec_from_file_location("ae_archive_deployer", SOURCE)
APP = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(APP)


class WorkerHarness:


    def __init__(self, approve_installers: bool = True) -> None:
        self.events = Queue()
        self.approve_installers = approve_installers
        self.ui_requests = []

    def _request_ui(self, kind, payload):
        self.ui_requests.append((kind, payload))
        if kind == "confirm_installer":
            return self.approve_installers
        return True


class FakeProcess:
    def __init__(self, command, cwd, shell, **options):
        self.command = command
        self.cwd = Path(cwd)
        self.shell = shell

    def wait(self):

        if self.command[0] != "msiexec":
            assert Path(self.command[0]).is_file()
        else:
            assert (self.cwd / "Installer.msi").is_file()
        return 0


class DeployerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae_test_")
        self.root = Path(self.temp.name)
        def fixture_runner(command, directory, **_options):
            return APP.subprocess.Popen(command, cwd=str(directory), shell=False).wait()
        runner = mock.patch.object(APP, 'run_installer', side_effect=fixture_runner)
        runner.start()
        self.addCleanup(runner.stop)

    def tearDown(self):
        self.temp.cleanup()

    def test_1100_generated_safe_zip_scenarios(self):

        for index in range(1100):
            relative = Path(f"set_{index % 37:02d}") / f"folder_{index:04d}" / f"payload_{index}.dat"
            archive_path = self.root / f"case_{index:04d}.zip"
            output = self.root / f"out_{index:04d}"
            expected = f"payload:{index}:snow".encode()
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(relative.as_posix(), expected)
            output.mkdir()
            APP.extract_archive(archive_path, output)
            self.assertEqual((output / relative).read_bytes(), expected, f"scenario {index}")

    def test_500_generated_path_traversal_scenarios(self):

        traversal_names = []
        for index in range(250):
            traversal_names.extend((
                f"../escape_{index}.dat",
                f"..\\escape_{index}.dat",
            ))
        for member in traversal_names:
            with self.subTest(member=member):
                with self.assertRaises(ValueError):
                    APP._safe_destination(self.root, member)

    def test_absolute_and_drive_paths_are_rejected(self):
        for member in ("/outside/file", r"\outside\file", r"C:\outside\file", "//server/share/file"):
            with self.subTest(member=member):
                with self.assertRaises(ValueError):
                    APP._safe_destination(self.root, member)

    def test_symlink_escape_is_rejected_when_supported(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "link"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks is not permitted in this environment")
        with self.assertRaises(ValueError):
            APP._safe_destination(self.root, "link/escape.txt")

    def test_install_detection_and_numeric_sort(self):
        adobe = self.root / "Adobe"
        for version in ("2024", "2026", "2025"):
            (adobe / f"Adobe After Effects {version}" / "Support Files").mkdir(parents=True)
            (adobe / f"Adobe After Effects {version}" / "Support Files" / "AfterFX.exe").write_bytes(b"host fixture")
        (adobe / "Adobe Premiere Pro 2026" / "Support Files").mkdir(parents=True)
        (adobe / "Adobe After Effects broken").mkdir(parents=True)
        found = APP.detect_ae_installations(adobe)
        self.assertEqual([version for version, _ in found], ["2026", "2025", "2024"])
        self.assertEqual(found[0][1], adobe / "Adobe After Effects 2026" / "Support Files" / "Plug-ins")

    def test_unreadable_or_missing_install_root_returns_empty(self):
        self.assertEqual(APP.detect_ae_installations(self.root / "missing"), [])

    def test_frozen_resource_path_uses_assets_directory(self):
        bundle = self.root / "_MEI-test"
        with mock.patch.object(APP.sys, "_MEIPASS", str(bundle), create=True):
            self.assertEqual(APP.resource_path("cat_logo.png"), bundle / "assets" / "cat_logo.png")

    def test_splash_composition_fills_target_and_fades_edges(self):
        source = Image.new("RGB", (32, 24), (220, 30, 20))
        rendered = compose_splash_image(source, (64, 40), zoom=1.5, offset_x=45, offset_y=-25)
        self.assertEqual(rendered.size, (64, 40))
        self.assertEqual(rendered.getpixel((0, 20)), (0, 0, 0))
        center = rendered.getpixel((48, 20))
        self.assertGreater(center[0], center[1])
        self.assertGreater(center[0], 80)

    def test_still_image_loader_reads_only_one_image_and_detaches_the_file(self):
        path = self.root / "preview.tiff"
        first = Image.new("RGBA", (12, 8), (255, 0, 0, 255))
        second = Image.new("RGBA", (12, 8), (0, 255, 0, 255))
        first.save(path, save_all=True, append_images=[second])
        loaded = load_still_image(path)
        path.unlink()
        self.assertEqual(loaded.size, (12, 8))
        self.assertEqual(loaded.getpixel((4, 4))[:3], (255, 0, 0))

    def test_lens_drag_and_scroll_controls(self):

        self.assertEqual(pan_from_drag(0, 0, 47.5, -47.5, 190, 190), (50, -50))
        self.assertEqual(pan_from_drag(90, -90, 190, -190, 190, 190), (100, -100))
        self.assertEqual(zoom_from_scroll(1.25, 120), 1.35)
        self.assertEqual(zoom_from_scroll(1.25, -120), 1.15)
        self.assertEqual(zoom_from_scroll(4.0, 120), 4.0)
        self.assertEqual(zoom_from_scroll(1.0, -120), 1.0)

    def test_installed_ae_splash_resources_are_detected_read_only(self):
        dll = Path(r"C:\Program Files\Adobe\Adobe After Effects 2026\Support Files\AfterFXLib.dll")
        if not dll.is_file():
            self.skipTest("AE 2026 splash library is not installed on this machine")
        resources = read_splash_resources(dll)
        names = {entry["name"] for entry in resources}
        self.assertIn("AE_SPLASH", names)
        self.assertIn("AE_SPLASH_AT_2X", names)
        self.assertTrue(all(entry["size"][0] > 0 and entry["size"][1] > 0 for entry in resources))

    def test_splash_customizer_gui_can_be_constructed(self):
        dll = Path(r"C:\Program Files\Adobe\Adobe After Effects 2026\Support Files\AfterFXLib.dll")
        if not dll.is_file():
            self.skipTest("AE 2026 splash library is not installed on this machine")
        try:
            root = tk.Tk()
        except Exception as error:
            self.skipTest(f"Tk is unavailable in this environment: {error}")
        self.addCleanup(root.destroy)
        root.withdraw()
        editor = SplashCustomizer(root, dll, lambda _widget: None, lambda _window: None)
        root.update()
        self.assertEqual(editor.title(), "HAZZAA SPLASH IMAGE | IMAGE EDITION")
        editor.geometry("1000x790")
        root.update_idletasks()
        self.assertFalse(hasattr(editor, "animate_checkbox"))
        self.assertFalse(hasattr(editor, "animation_after"))
        self.assertEqual(len(editor.resources), 6)
        self.assertEqual(editor.preview_resource["name"], "AE_SPLASH")
        self.assertGreater(editor.lens.winfo_width(), 100)
        editor.custom_text_var.set("MY TITLE")
        editor.keep_header_var.set(False)
        editor._toggle_header()
        self.assertEqual(editor.overlay_style.text_y, editor.overlay_style.header_y)
        self.assertFalse(editor._current_overlay().keep_header)
        self.assertEqual(editor._current_overlay().text, "MY TITLE")
        editor.keep_header_var.set(True)
        editor._toggle_header()
        self.assertGreater(editor.overlay_style.text_y, editor.overlay_style.header_y)
        self.assertTrue(editor._current_overlay().keep_header)
        selected = self.root / "chosen.png"
        Image.new("RGB", (80, 60), "blue").save(selected)
        with mock.patch("splash_customizer.filedialog.askopenfilename", return_value=str(selected)):
            editor._choose_image()
        self.assertEqual(editor.source_image.size, (80, 60))
        with mock.patch("splash_customizer.apply_splash_image", return_value=(self.root / "backup.dll", 6)) as apply, \
             mock.patch("splash_customizer.messagebox.showinfo"):
            editor._apply()
            end = time.monotonic() + 3
            while editor.applying and time.monotonic() < end:
                root.update()
                time.sleep(.01)
            self.assertFalse(editor.applying)
            apply.assert_called_once()
            self.assertEqual(editor.status_var.get(), "STATIC STARTUP IMAGE SAVED")
        editor.destroy()

    def test_asset_classification_and_text_files(self):
        extracted = self.root / "assets"
        (extracted / "Plug-ins").mkdir(parents=True)
        (extracted / "Scripts" / "ScriptUI Panels").mkdir(parents=True)
        files = (
            extracted / "Plug-ins" / "Glow.aex",
            extracted / "Scripts" / "ScriptUI Panels" / "Panel.jsx",
            extracted / "serial.txt",
            extracted / "Installer.exe",
            extracted / "Installer.msi",
        )
        for file in files:
            file.write_text("sample", encoding="utf-8")
        plugins, scripts, folders, installers, texts = APP.find_assets(extracted)
        self.assertEqual([p.name for p in plugins], ["Glow.aex"])
        self.assertEqual(scripts, [])
        self.assertEqual([p.name for p in folders], ["Scripts"])
        self.assertEqual({p.suffix for p in installers}, {".exe", ".msi"})
        self.assertEqual([p.name for p in texts], ["serial.txt"])

    def test_generic_script_files_and_text_extensions(self):
        extracted = self.root / "generic"
        extracted.mkdir()
        for name in ("panel.jsxbin", "include.jsxinc", "readme.text", "LICENSE.txt", "ordinary.txt"):
            (extracted / name).write_text("data", encoding="utf-8")
        _, scripts, folders, _, texts = APP.find_assets(extracted)
        self.assertEqual({p.name for p in scripts}, {"panel.jsxbin", "include.jsxinc"})
        self.assertEqual(folders, [])
        self.assertEqual({p.name for p in texts}, {"readme.text", "LICENSE.txt"})

    def test_unique_names_preserve_existing_files_and_folders(self):
        target = self.root / "Existing.aex"
        target.write_text("old", encoding="utf-8")
        self.assertEqual(APP.unique_destination(target).name, "Existing (2).aex")
        folder = self.root / "Scripts"
        folder.mkdir()
        self.assertEqual(APP.unique_destination(folder).name, "Scripts (2)")

    def test_script_target_sibling_for_after_effects_plugin_path(self):
        plugin_target = self.root / "Support Files" / "Plug-ins"
        self.assertEqual(APP.script_destination_directory(plugin_target),
                         self.root / "Support Files" / "Scripts")

    def test_custom_script_target_and_existing_directory_merge(self):
        custom = self.root / "custom" / "effects"
        self.assertEqual(APP.script_destination_directory(custom), custom / "Scripts")
        source = self.root / "archive" / "Scripts"
        (source / "ScriptUI Panels").mkdir(parents=True)
        (source / "helper.jsx").write_text("helper", encoding="utf-8")
        (source / "ScriptUI Panels" / "Panel.jsx").write_text("panel", encoding="utf-8")
        destination = custom / "Scripts"
        (destination / "ScriptUI Panels").mkdir(parents=True)
        (destination / "ScriptUI Panels" / "Existing.jsx").write_text("existing", encoding="utf-8")
        (destination / "ScriptUI Panels" / "Panel.jsx").write_text("keep old", encoding="utf-8")
        APP.merge_directory_contents(source, destination)
        self.assertFalse(source.exists())
        self.assertEqual((destination / "helper.jsx").read_text(encoding="utf-8"), "helper")
        self.assertEqual((destination / "ScriptUI Panels" / "Panel.jsx").read_text(encoding="utf-8"), "keep old")
        self.assertEqual((destination / "ScriptUI Panels" / "Panel (2).jsx").read_text(encoding="utf-8"), "panel")
        self.assertEqual((destination / "ScriptUI Panels" / "Existing.jsx").read_text(encoding="utf-8"), "existing")

    def test_invalid_zip_and_unsupported_archive_fail_cleanly(self):
        broken = self.root / "broken.zip"
        broken.write_bytes(b"not a zip")
        with self.assertRaises(zipfile.BadZipFile):
            APP.extract_archive(broken, self.root / "broken-out")
        with self.assertRaises(ValueError):
            APP.extract_archive(self.root / "backup.7z", self.root / "out")

    def test_path_traversal_inside_zip_skips_bad_entry_and_keeps_safe_files(self):
        archive = self.root / "traversal.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("safe.txt", "safe")
            z.writestr("../escaped.txt", "bad")
        output = self.root / "safe-out"
        output.mkdir()
        warnings = APP.extract_archive(archive, output)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Skipped unsafe entry", warnings[0])
        self.assertEqual((output / "safe.txt").read_text(), "safe")
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_missing_rar_backend_has_clear_error(self):
        archive = self.root / "empty.rar"
        archive.write_bytes(b"not a real rar")
        old_rarfile = APP.rarfile
        APP.rarfile = None
        try:
            with self.assertRaisesRegex(RuntimeError, "bundled RAR reader"):
                APP.extract_archive(archive, self.root / "rar-out")
        finally:
            APP.rarfile = old_rarfile

    def test_zip_worker_moves_files_shows_text_and_runs_exe_in_requested_mode(self):
        archive = self.root / "bundle.zip"
        plugin_target = self.root / "Support Files" / "Plug-ins"
        script_target = self.root / "Support Files" / "Scripts"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("Effect.aex", plugin_bytes("plugin"))
            z.writestr("Scripts/Panel.jsx", "script")
            z.writestr("Scripts/ScriptUI Panels/Panel.jsx", "panel")
            z.writestr("Scripts/helpers/panel-data.txt", "asset")
            z.writestr("readme.txt", "Installation instructions only")
            z.writestr("Installer.exe", "NullsoftInst fixture")
        plugin_target.mkdir(parents=True)
        harness = WorkerHarness()
        commands = []
        class CaptureProcess(FakeProcess):
            def __init__(self, command, cwd, shell, **options):
                commands.append(command)
                super().__init__(command, cwd, shell)
        old_popen = APP.subprocess.Popen
        APP.subprocess.Popen = CaptureProcess
        try:
            with mock.patch.object(APP, "wait_with_autofill", side_effect=lambda process, *_: process.wait()):
                APP.ArchiveDeployer._process_worker(harness, [archive], plugin_target, True)
        finally:
            APP.subprocess.Popen = old_popen
        self.assertEqual((plugin_target / "Effect.aex").read_bytes(), plugin_bytes("plugin"))
        self.assertEqual((script_target / "Panel.jsx").read_text(), "script")
        self.assertEqual((script_target / "ScriptUI Panels" / "Panel.jsx").read_text(), "panel")
        self.assertEqual((script_target / "helpers" / "panel-data.txt").read_text(), "asset")
        events = list(harness.events.queue)
        self.assertFalse(any(event[0] == "activation" for event in events))
        self.assertFalse(harness.ui_requests)
        logs = [item[1] for item in list(harness.events.queue) if item[0] == "log"]
        self.assertIn("/S", commands[0])
        self.assertTrue(any(item[0] == "done" for item in list(harness.events.queue)))

    def test_readme_installer_fallback_and_archive_presets_work_together(self):
        archive = self.root / "registered-plugin.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("readme.txt", "Username: LocalOwner\nPassword: LocalPass!=\nSerial: AAA-BBB-CCC")
            z.writestr("setup.exe", "NullsoftInst test placeholder, never executed")
            z.writestr("User Presets/Folder/Example.ffx", "preset data")
        harness = WorkerHarness()
        observed = []
        def assist(process, credentials, log, help_callback):
            self.assertEqual(credentials.values["username"], "LocalOwner")
            self.assertEqual(credentials.values["password"], "LocalPass!=")
            help_callback()
            return process.wait()
        class CaptureProcess(FakeProcess):
            def __init__(self, command, cwd, shell, **options):
                observed.append(command)
                super().__init__(command, cwd, shell)
        with mock.patch.object(APP.subprocess, "Popen", CaptureProcess), \
             mock.patch.object(APP, "wait_with_autofill", side_effect=assist):
            APP.ArchiveDeployer._process_worker(harness, [archive], self.root / "plugins", True,
                                               self.root / "User Presets", False)
        self.assertIn("/S", observed[0], "README details must not force a visible wizard")
        self.assertEqual((self.root / "User Presets/Folder/Example.ffx").read_text(), "preset data")
        events = list(harness.events.queue)
        self.assertFalse(any(kind == "activation" for kind, _ in events))
        self.assertFalse(any(kind in {"credentials_help", "text_popup"} for kind, _ in events))
        logs = "\n".join(payload for kind, payload in events if kind == "log")
        self.assertNotIn("LocalPass", logs)
        self.assertNotIn("ERROR:", logs)

    def test_msi_worker_uses_msiexec_quiet_flags(self):
        archive = self.root / "installer.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("Installer.msi", "msi")
        harness = WorkerHarness()
        observed = []
        class CaptureProcess(FakeProcess):
            def __init__(self, command, cwd, shell, **options):
                observed.append(command)
                super().__init__(command, cwd, shell)
        old_popen = APP.subprocess.Popen
        APP.subprocess.Popen = CaptureProcess
        try:
            APP.ArchiveDeployer._process_worker(harness, [archive], self.root / "plugins", True)
        finally:
            APP.subprocess.Popen = old_popen
        self.assertEqual(observed[0][0:2], ["msiexec", "/i"])
        self.assertIn("/qn", observed[0])
        self.assertIn("/norestart", observed[0])

    def test_installer_keeps_neighboring_script_assets_until_it_exits(self):
        archive = self.root / "script-with-installer.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("Scripts/Panel.jsx", "panel")
            z.writestr("Scripts/setup.exe", "placeholder")
        checked = []
        class InspectProcess(FakeProcess):
            def wait(process):
                self.assertTrue((process.cwd / "Panel.jsx").is_file())
                checked.append(True)
                return super(InspectProcess, process).wait()
        with mock.patch.object(APP.subprocess, "Popen", InspectProcess):
            APP.ArchiveDeployer._process_worker(WorkerHarness(), [archive], self.root / "Plug-ins", False)
        self.assertEqual(checked, [True])
        self.assertTrue((self.root / "Scripts" / "Panel.jsx").exists())

    def test_normal_exe_launch_has_no_silent_argument(self):
        archive = self.root / "normal.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("Installer.exe", "NullsoftInst fixture")
        observed = []
        class CaptureProcess(FakeProcess):
            def __init__(self, command, cwd, shell, **options):
                observed.append(command)
                super().__init__(command, cwd, shell)
        old_popen = APP.subprocess.Popen
        APP.subprocess.Popen = CaptureProcess
        try:
            APP.ArchiveDeployer._process_worker(WorkerHarness(), [archive], self.root / "plugins", False)
        finally:
            APP.subprocess.Popen = old_popen
        self.assertEqual(len(observed[0]), 1)

    def test_installer_runs_automatically_without_a_confirmation_dialog(self):
        archive = self.root / "declined.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("Installer.exe", "NullsoftInst fixture")
        harness = WorkerHarness(approve_installers=False)
        target = self.root / "plugins"
        target.mkdir()
        observed = []
        class CaptureProcess(FakeProcess):
            def __init__(self, command, cwd, shell, **options):
                observed.append(command)
                super().__init__(command, cwd, shell)
        old_popen = APP.subprocess.Popen
        APP.subprocess.Popen = CaptureProcess
        try:
            APP.ArchiveDeployer._process_worker(harness, [archive], target, False)
        finally:
            APP.subprocess.Popen = old_popen
        self.assertEqual(len(observed), 1)
        self.assertFalse(harness.ui_requests)

    def test_bad_archive_does_not_stop_following_archives(self):
        bad = self.root / "bad.zip"
        bad.write_bytes(b"broken")
        good = self.root / "good.zip"
        with zipfile.ZipFile(good, "w") as z:
            z.writestr("Effect.aex", plugin_bytes("ok"))
        harness = WorkerHarness()
        target = self.root / "plugins"
        target.mkdir()
        APP.ArchiveDeployer._process_worker(harness, [bad, good], target, False)
        self.assertEqual((target / "Effect.aex").read_bytes(), plugin_bytes("ok"))
        logs = [item[1] for item in list(harness.events.queue) if item[0] == "log"]
        self.assertTrue(any("ERROR: bad.zip" in log for log in logs))
        self.assertTrue(any("Processing good.zip" in log for log in logs))

    def test_collision_safe_deployment(self):
        target = self.root / "plugins"
        target.mkdir()
        (target / "Effect.aex").write_text("existing", encoding="utf-8")
        archive = self.root / "collision.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("Effect.aex", plugin_bytes("new"))
        with mock.patch.dict(os.environ, {"LOCALAPPDATA":str(self.root / "local")}):
            APP.ArchiveDeployer._process_worker(WorkerHarness(), [archive], target, False)
        self.assertEqual((target / "Effect.aex").read_bytes(), plugin_bytes("new"))
        self.assertFalse((target / "Effect (2).aex").exists())
        self.assertEqual(next((self.root / "local").rglob("*.backup")).read_text(),"existing")

    def test_real_rar_round_trip_if_winrar_is_installed(self):
        rar = Path(r"C:\Program Files\WinRAR\Rar.exe")
        unrar = Path(r"C:\Program Files\WinRAR\UnRAR.exe")
        if not (rar.is_file() and unrar.is_file() and APP.rarfile is not None):
            self.skipTest("WinRAR/rarfile not installed")
        source = self.root / "Effect.aex"
        source.write_bytes(b"rar integration payload")
        archive = self.root / "actual.rar"
        subprocess.run([str(rar), "a", "-idq", archive.name, source.name], cwd=self.root,
                       check=True, timeout=30, capture_output=True)
        extracted = self.root / "rar-out"
        extracted.mkdir()
        self.assertTrue(APP.configure_rar_backend())
        self.assertEqual(Path(APP.rarfile.UNRAR_TOOL).resolve(), APP.resource_path("UnRAR.exe").resolve())
        APP.extract_archive(archive, extracted)
        self.assertEqual((extracted / "Effect.aex").read_bytes(), source.read_bytes())

    def test_animated_cursor_is_loadable_on_windows(self):
        if os.name != "nt":
            self.skipTest("Windows animated cursor validation is Windows-only")
        import ctypes
        cursor = ctypes.windll.user32.LoadCursorFromFileW(str(APP.resource_path("bat_cursor.ani")))
        self.assertTrue(cursor)
        ctypes.windll.user32.DestroyCursor(cursor)

    def test_tk_gui_can_be_constructed_when_tk_is_available(self):
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception as error:
            self.skipTest(f"Tk is unavailable in this environment: {error}")
        self.addCleanup(root.destroy)
        root.withdraw()
        app = APP.ArchiveDeployer(root)
        root.update_idletasks()
        self.assertEqual(root.title(), APP.TITLE)
        self.assertTrue(hasattr(app, "progress"))
        self.assertEqual(len(app.snow), APP.PARTICLE_COUNT)
        self.assertEqual(app.cursor_spec[0], "@")
        menu = app.version_menu["menu"]
        versions = [menu.entrycget(index, "label") for index in range(menu.index("end") + 1)]
        self.assertEqual(versions, [str(year) for year in range(2026, 2018, -1)])
        absent = self.root / "not-installed" / "Support Files" / "Plug-ins"
        app.version_paths["2019"] = absent
        app.version_var.set("2019")
        app.target_var.set(str(absent))
        app.archives = [self.root / "not-executed.zip"]
        with mock.patch.object(APP.messagebox, "showerror") as error:
            app.process_archives()
            error.assert_called_once()
        self.assertFalse(absent.exists())


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(DeployerTests)
    )
    print("Generated safe ZIP scenarios: 1,100")
    print("Generated traversal scenarios: 500")
    raise SystemExit(not result.wasSuccessful())
