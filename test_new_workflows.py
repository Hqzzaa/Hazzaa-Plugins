import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image, ImageDraw
from ae_organization import (allowed_registry_key, apply_registry_changes, find_preset_folders,
    merge_presets, parse_adobe_registry, preset_directory, restore_registry_backup)
from installer_automation import Credentials, InstallerAssistant, credentials_for_installer, read_document
from splash_customizer import SplashOverlay, compose_with_overlay


class Workflows(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hazzaa_workflow_")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_readme_fields_preserve_password_and_choose_nearest_product(self):
        documents = [
            (self.root / "readme.txt", "Username: Root User\nPassword: old\nSerial: AAA-BBB-CCC"),
            (self.root / "product" / "readme.txt", "Name: Local User\nPassword: A=b/C!\nSerial:\nDDD-EEE-FFF"),
            (self.root / "other" / "readme.txt", "Username: Wrong Product\nPassword: wrong"),
        ]
        result = credentials_for_installer(documents, self.root / "product" / "setup.exe")
        self.assertEqual(result.values, {"username": "Local User", "password": "A=b/C!", "serial": "DDD-EEE-FFF"})
        conflict = credentials_for_installer([
            (self.root / "readme.txt", "Username: One\nPassword: Same"),
            (self.root / "info.txt", "Username: Two\nPassword: Same")], self.root / "setup.exe")
        self.assertNotIn("username", conflict.values)
        self.assertIn("username", conflict.conflicts)
        self.assertNotIn("Same", repr(conflict))

    def test_unicode_document_and_plain_serial(self):
        path = self.root / "readme.txt"
        path.write_bytes("الاسم: مستخدم\nكلمة المرور: abc!=".encode("utf-16"))
        result = credentials_for_installer([(path, read_document(path))], self.root / "setup.exe")
        self.assertEqual(result.values["username"], "مستخدم")
        self.assertEqual(result.values["password"], "abc!=")
        result = credentials_for_installer([(self.root / "serial.txt", "ABCD-1234-EFGH")], self.root / "setup.exe")
        self.assertEqual(result.values["serial"], "ABCD-1234-EFGH")

    def test_preset_collection_merges_without_overwrite_or_repeated_duplicates(self):
        one, two = self.root / "Backup1" / "User Presets", self.root / "Backup2" / "User Presets"
        destination = preset_directory("2026", self.root / "Documents")
        for folder, content in ((one, b"one"), (two, b"two")):
            (folder / "Subfolder").mkdir(parents=True)
            (folder / "Subfolder" / "Effect.ffx").write_bytes(content)
        destination.mkdir(parents=True)
        found = list(find_preset_folders([self.root], destination))
        self.assertEqual(set(found), {one, two})
        for folder in found:
            merge_presets(folder, destination)
        self.assertEqual({p.read_bytes() for p in destination.rglob("*.ffx")}, {b"one", b"two"})
        for folder in found:
            self.assertEqual(merge_presets(folder, destination), 0)
        self.assertEqual(len(list(destination.rglob("*.ffx"))), 2)
        self.assertTrue((one / "Subfolder" / "Effect.ffx").exists())

    def test_registry_scope_parse_and_backup_restore_with_fake_registry(self):
        text = '[HKEY_CURRENT_USER\\Software\\Adobe\\CSXS.11]\n"PlayerDebugMode"="1"\n' \
               '[HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run]\n"Bad"="bad.exe"'
        changes, rejected = parse_adobe_registry(text)
        self.assertEqual(len(changes), 1)
        self.assertGreater(rejected, 0)
        self.assertFalse(allowed_registry_key(r"HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet"))

        class Handle:
            def __init__(self, key): self.key = key
            def __enter__(self): return self.key
            def __exit__(self, *args): pass
        class Registry:
            HKEY_CURRENT_USER = "HKCU"
            HKEY_LOCAL_MACHINE = "HKLM"
            KEY_SET_VALUE = 2
            def __init__(self): self.values = {}
            def OpenKey(self, root, key, *args): return Handle((root, key))
            CreateKeyEx = OpenKey
            def QueryValueEx(self, key, name):
                try: return self.values[(key, name)]
                except KeyError: raise FileNotFoundError()
            def SetValueEx(self, key, name, _, kind, value): self.values[(key, name)] = (value, kind)
            def DeleteValue(self, key, name):
                try: del self.values[(key, name)]
                except KeyError: raise FileNotFoundError()
        registry = Registry()
        backup = apply_registry_changes(changes, self.root / "backups", registry)
        self.assertTrue(registry.values)
        restore_registry_backup(backup, registry)
        self.assertFalse(registry.values)
        self.assertTrue(backup.is_file())

    def test_splash_preserves_adobe_header_and_renders_custom_text(self):
        original = Image.new("RGB", (766, 516), "black")
        ImageDraw.Draw(original).rectangle((40, 38, 100, 60), fill=(230, 0, 0))
        artwork = Image.new("RGB", (766, 516), (30, 70, 150))
        style = SplashOverlay(text="HAZZAA", text_x=.6, text_y=.6, font_size=35,
                              text_color="#ff0000", outline_color="#0000ff")
        output = compose_with_overlay(artwork, original.size, style=style, original=original)
        self.assertGreater(output.getpixel((50, 45))[0], 180)
        no_header = compose_with_overlay(artwork, original.size,
            style=SplashOverlay(keep_header=False), original=original)
        self.assertNotEqual(output.crop((430, 300, 750, 400)).tobytes(),
                            no_header.crop((430, 300, 750, 400)).tobytes())
        self.assertNotEqual(output.getpixel((50, 45)), no_header.getpixel((50, 45)))
        text_pixels = output.crop((430, 300, 750, 400)).get_flattened_data()
        self.assertTrue(any(red > 200 and green < 40 and blue < 40 for red, green, blue in text_pixels))

    @unittest.skipUnless(os.name == "nt", "Windows native integration")
    def test_actual_native_installer_receives_fields_and_advances_pages(self):
        output = self.root / "result.json"
        fixture = Path(__file__).with_name("native_installer_fixture.py")
        process = subprocess.Popen([sys.executable, str(fixture), str(output)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            assistant = InstallerAssistant(process.pid, Credentials({
                "username": "Hazzaa Test", "password": "Local-Test!=", "serial": "AAAA-BBBB-CCCC"}))
            deadline = time.monotonic() + 15
            while process.poll() is None and time.monotonic() < deadline:
                assistant.poll()
                time.sleep(.1)
            if process.poll() is None:
                self.fail("Native fixture did not complete its form and install pages.")
            self.assertEqual(process.returncode, 0, process.stderr.read().decode(errors="replace"))
            result = json.loads(output.read_text())
            self.assertEqual(result["username"], "Hazzaa Test")
            self.assertEqual(result["password"], "Local-Test!=")
            self.assertEqual([result[f"serial{i}"] for i in range(3)], ["AAAA", "BBBB", "CCCC"])
            self.assertEqual(result["destination"], "C:\\Example")
            self.assertTrue(result["installed"])
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            process.stderr.close()

    @unittest.skipUnless(os.name == "nt", "Windows native integration")
    def test_unknown_field_stops_advancing_and_requests_manual_copy_help(self):
        output = self.root / "unknown.json"
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("native_installer_fixture.py")),
                                    str(output), "--unknown"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        helped = []
        try:
            assistant = InstallerAssistant(process.pid, Credentials({
                "username": "Hazzaa", "password": "Test!", "serial": "AAAA-BBBB-CCCC"}),
                help_callback=lambda: helped.append(True))
            deadline = time.monotonic() + 5
            while not assistant.reported_fields and time.monotonic() < deadline:
                assistant.poll()
                time.sleep(.1)
            assistant.stalled_since = time.monotonic() - 21
            assistant.poll()
            self.assertEqual(helped, [True])
            self.assertFalse(output.exists(), "Unknown required fields must stop automatic submission.")
            self.assertIsNone(process.poll())
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
