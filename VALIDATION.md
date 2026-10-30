# Hazzaa Plugins 2026.10.30.2

## FilmConvert Nitrate fix

The reviewed Nitrate 3.56 Qt installer extracted its AEX into Adobe MediaCore but left its runtime resources staged under FilmConvert/PremierePro. Its log reported completion without the relocation performed by its final GUI step. ProgramData/RubberMonkey was absent on the affected local installation.

The app now completes this reviewed installation step in Python. It reads the vendor's Qt resource manifests, validates paths, installs the declared profiles/resources and vendor utilities in the machine's actual ProgramData folder, and verifies copied contents. Source files are preserved; interrupted file deployment rolls back. Existing license files are left unchanged. The vendor batch script and its broad permission changes are not executed.

The fix runs after unattended setup, after a successfully completed manual Nitrate wizard, and on application startup when the reviewed installed resources are incomplete. Manual wizards are not reopened. AEX presence alone no longer satisfies the Nitrate installation check.

The 2026.10.30.2 fix also detects an existing reviewed 3.56 installation before launching setup. Its valid x64 host file is checked, support files are repaired and verified, and AUTO INSTALL completes without another installer process or assistance window. This avoids Qt IFW's TargetDirectoryInUse error when installing the same version again. First-time setup keeps the original unattended command. Unknown versions and missing/invalid host binaries are not treated as completed installations.

## Validation

- 201 regression test methods: 200 passed, 1 optional WinRAR archive-creation test skipped.
- 22 FilmConvert tests cover manifests, custom installation locations, redirected ProgramData, missing/damaged files, idempotence, copy rollback, cancellation, manual completion, startup repair and repeat installation without launching setup.
- The actual installed Nitrate package completed the application's repeat-install flow with zero setup processes, zero manual assistance windows and all 105 support files verified.
- Repaired and verified 105 real installed support files.
- In an isolated After Effects 2026 instance (26.5x89), FC NITRATE initialized successfully and rendered a one-frame 320 x 180 test composition. Output: 180,040-byte AVI.
- The existing After Effects instance and its project were not closed or modified.

These checks establish this installation fault and its repair. They do not guarantee compatibility with every GPU, driver, Windows build or legacy effect. Nitrate's graphics-runtime requirements still apply. Native success on one machine is separate from the portable-path and failure-case regression tests.
