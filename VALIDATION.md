# Hazzaa Plugins 2026.10.31.1

## Continuum background installation

The full application installation workflow was tested with the real Continuum 2026.5 19.5.6 installer. It launched one silent installer, returned parent/overall exit code 0, verified 496 host AEX files, and created zero assistance windows. It completed in 457.27 seconds on the test machine.

The reported failure was reproduced: successful setup was incorrectly replaced by a helper process exit code 2, causing a second normal wizard. The reviewed package now verifies successful parent completion against installed files without automatically rerunning a normal installer. Genuine parent errors, other child failures and missing files remain errors. Transient status windows receive a settling period before manual attention is considered. Legacy copy-folder instructions and LIB buttons apply only to the old 2019 installer.

## Owner availability control

The existing Prismo owner dashboard contains the Hazzaa Plugins availability switch, behind its existing password/session authentication. Other accounts cannot read or change owner settings. The publisher can pause availability, edit the displayed message, and restore availability. Changes are audited.

Updated applications verify a separately signed policy at startup and poll while open. Pausing blocks new operations and queued installers; an already running installation finishes safely. Offline clients retain their last verified state. Older app versions cannot be remotely stopped locally, but their online library is gated on the server. The status policy controls availability and a text message. The update-signing key stays separate from the control key; only a public control key is bundled with the app.

## Verification

- 219 Python regression methods: 218 passed, one optional WinRAR test skipped.
- The source release passes the same regression suite.
- Five focused server tests passed, including owner/ordinary/unauthenticated roles, pause/resume, signed policy, stale writes, large multipart completion, library access and updater behavior.
- Three pre-existing Prismo integration assertions fail identically against the unmodified deployed Prismo API and the candidate API; no additional Prismo failures were introduced. The baseline comparison is retained separately.
- The packaged application is checked against 29 runtime modules and its assets.
- The isolated updater tests locked-EXE handoff, replacement and real Tk startup.
- The Continuum archive is unchanged from the fully SHA-256-verified 3,598,193,176-byte production download. It contains one installer and requires no archive password. Sapphire remains unchanged.

Windows administrator consent is separate from unattended setup. GPU compatibility and every individual effect are outside this installation check. Node.js is used for development-only signature fixtures and Worker tests; the packaged app does not require it.
