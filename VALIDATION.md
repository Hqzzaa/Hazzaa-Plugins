# Validation

Version: **2026.10.28.5**

Checked on Windows with Python 3.14.0, Pillow 12.2.0, rarfile 4.5, pefile 2024.8.26 and PyInstaller 6.20.0.

- 179 test methods executed: 178 passed, 1 optional test skipped.
- The Windows one-file EXE built successfully using the included portable build specification.
- Application modules were checked for unchanged executable syntax after removing explanatory comments and docstrings.
- Update tests generate an isolated, temporary signing key. The publisher's private key is not required or included.
- Source distribution excludes local logs, server code, publisher credentials, commercial plug-in archives and registration records.

The test suite covers safe archive paths, file deployment, target discovery, installer handoff and queueing, registration-field handling, library organization, uninstall receipts and signed update validation. Native installer checks use controlled fixtures. These results do not certify every commercial installer, license or After Effects version.

`SHA256SUMS.txt` identifies the existing production EXE distributed with this release. Rebuilding with a different environment or build configuration can produce a different binary hash.
