# Hazzaa Plugins 2026.10.30.3

## Continuum 2026.5

The online library replaces the legacy BCC/Continuum package with Continuum 2026.5 Adobe 19.5.6. Sapphire remains unchanged. The download is a ZIP containing only the supplied installer, without the outer archive password or website shortcuts.

The application uses Inno Setup's unattended switches: /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-. Native administrator-mode installation completed in 188.26 seconds with exit code 0, no assistance callback, no manual handoff and no wizard input. Windows administrator consent is separate from the installer's unattended mode.

Verified 496 installed Windows x64 AEX files in Adobe's shared MediaCore/BorisFX/Continuum directory. The installer also populated its version 19 vendor runtime folder and resource/model folders. The new postcondition checks the current host directory; old Continuum host files alone cannot satisfy it. An explicitly reviewed system net.exe helper exit code does not override an otherwise successful parent installer; unrelated executables and errors remain failures.

## Validation

- 204 regression test methods: 203 passed, 1 optional WinRAR test skipped.
- The sanitized public source passed the same 204 tests.
- 4 server tests, including successful multipart completion at 429 and 1280 parts, invalid lists and bounded payload rejection.
- The packaged executable matches all 26 reviewed application modules and its assets.
- The isolated updater replaced a locked prior QA executable and obtained a real Tk startup receipt for version 2026.10.30.3.
- Existing Nitrate automatic installation/repair behavior is retained.

The installer and host files were checked on this Windows machine. GPU compatibility and every individual Continuum effect were not exhaustively tested. The optional separate After Effects render check was not completed because native app inspection approval timed out; it is not counted as a passed test. Existing user projects were not closed or changed.
