# Hazzaa Plugins

<img src="assets/cat_logo.png" width="72" alt="Hazzaa Plugins">

Windows desktop tool for installing and organizing After Effects plug-ins, scripts and presets.

[Download](https://github.com/Hqzzaa/Hazzaa-Plugins/releases/latest) · [Report a problem](https://github.com/Hqzzaa/Hazzaa-Plugins/issues) · [Website](https://hazzaa.space)

## Features

- Detect After Effects 2019–2026 installations or select a custom target folder.
- Install from local ZIP/RAR archives or the online library.
- Keep plug-ins and scripts in separate, alphabetized library sections.
- Route AEX files, scripts, ScriptUI panels and CEP extensions to their host locations.
- Attempt unattended setup; hand interactive installers to the user without restarting them.
- Continue independent file deployment while installer packages wait their turn.
- Collect user presets, back up supported registry changes and remove recorded installations.
- Customize the startup image with pan, zoom, multiline text, fonts and colors.
- Download signed application updates with file verification and rollback.
- Repair incomplete FilmConvert Nitrate 3.56 support resources after setup and on application startup.
- Install Continuum 2026.5 and verify its current Adobe MediaCore location.

## Use

1. Close After Effects and open **Hazzaa Plugins.exe**.
2. Select the After Effects version or browse to its Plug-ins folder.
3. Choose **SELECT FROM PC** or **ONLINE LIBRARY**, then select the items to install.
4. Leave **AUTO INSTALL** enabled and select **PROCESS**.
5. If a setup wizard needs input, finish that same wizard. Keep The App open so queued installers can continue.
6. Restart After Effects when installation finishes.

**ACTIVATION** shows saved registration details for products that require entry inside After Effects. Installing files does not activate a commercial license. The source repository contains no commercial plug-in packages or license credentials.

## Run from source

Requires 64-bit Windows and Python 3.14 with Tcl/Tk.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe ae_archive_deployer.py
```

Run from an elevated terminal when writing to protected installation folders. The packaged EXE requests administrator access at startup.

## Build

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm "Hazzaa Plugins.spec"
```

Output: `dist/Hazzaa Plugins.exe`. The build uses Tcl/Tk from the selected Python installation and bundles the RAR extractor. No publisher private key is required to run or build the application. Forks should configure their own update endpoint and public key before distributing their own releases.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -p "test_*.py"
```

Tests use temporary files and synthetic plug-in/installer fixtures. Some checks require Windows GUI support; optional integration checks skip when their prerequisites are absent. Tests do not establish compatibility with every vendor installer or every After Effects release.

## Verification

Compare the downloaded EXE against `SHA256SUMS.txt` from the same release:

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath '.\Hazzaa Plugins.exe'
```

The application verifies signed update manifests and checks downloaded file sizes and SHA-256 hashes before installation. Windows may still display SmartScreen for an executable without established publisher reputation. Source availability and a matching hash do not replace a security review.

## License

Application code: [MIT](LICENSE), © 2026 HAZZAA. Third-party dependencies retain their respective licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Availability

The publisher can pause and restore application availability. The app checks a signed status at launch and while open. Offline clients retain their last verified state. Running installations finish safely before new work is blocked. Continuum 2026.5 installs in the background; additional file copying is not required.
