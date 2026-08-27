# Desktop packaging

Expensetics runs as a loopback-only local web application and opens in the user's normal browser. Packaged builds include Python and the application dependencies; the user does not need to install Python separately. Financial data remains outside the package in the operating system's user-data directory.

Packages must be built separately on their target operating systems. The primary Windows artifact is a per-user installer containing a PyInstaller one-folder payload; the same folder may be zipped as a secondary portable build. The macOS release target is a signed and notarized application bundle in a DMG. Linux currently uses the isolated source installation described in the README.

## Build on Windows

Build on a 64-bit Windows machine:

```powershell
.\scripts\setup.ps1
.\scripts\build_windows.ps1
```

The build script creates an isolated `.build-venv`; it does not install Python build tools globally. Its default output is the application payload:

```text
dist\Expensetics\
```

Zip that complete folder for a portable test build. Do not distribute only the executable inside it: the adjacent runtime files are part of the application.

To compile the same payload and wrap it in a per-user installer, first install Inno Setup 6 from its official distribution, then run:

```powershell
.\scripts\build_windows.ps1 -Installer -Version 0.1.0
```

The result is `dist\Expensetics-Setup-0.1.0.exe`. Inno Setup is a build-machine application, not a runtime Python dependency, and the script fails clearly when it is unavailable. The declarative installer source is `scripts\windows-installer.iss`.

For a single-file diagnostic build instead:

```powershell
.\scripts\build_windows.ps1 -OneFile
```

That output is `dist\Expensetics.exe`. It is not the preferred release payload because it extracts its runtime for every launch, starts more slowly, and is harder to diagnose.

The script invokes PyInstaller directly and treats a missing executable or non-zero build result as a failure. This avoids false “created” messages from wrapper processes which fail to locate PyInstaller.

## Build on macOS

Build on the oldest macOS version you intend to support:

```sh
sh scripts/setup.sh
sh scripts/build_macos.sh
```

The macOS script also creates a separate `.build-venv`; it does not install packages globally. Its output is `dist/Expensetics`. Test the complete folder on both Intel and Apple silicon Macs if both architectures need support. A build is architecture-specific unless deliberately produced as a universal binary.

## Data location

Development data remains in `data`. A packaged executable stores its encrypted database away from the read-only application files:

- Windows: `%LOCALAPPDATA%\Expensetics`
- macOS: `~/Library/Application Support/Expensetics`
- Linux: `$XDG_DATA_HOME/Expensetics` or `~/.local/share/Expensetics`

Set `EXPENSETICS_DATA_DIR` before launch to choose a different writable location, including a folder beside a portable copy.

## Windows installer

The release installer should install the one-folder payload under `%LOCALAPPDATA%\Programs\Expensetics`, create a Start menu shortcut, register an uninstaller, and leave `%LOCALAPPDATA%\Expensetics` untouched during upgrades and ordinary uninstall. Inno Setup is the selected installer builder because its small declarative configuration supports per-user installation, shortcuts, upgrades, uninstallation, and signed output without changing the Python runtime architecture.

The installer itself is a single file for download; the installed application is intentionally not a one-file PyInstaller executable. A portable ZIP remains useful for testing and troubleshooting.

Unsigned installers and executables may trigger Microsoft Defender SmartScreen when downloaded from another computer. For broader distribution, sign the application executable and installer with an Authenticode certificate, timestamp the signatures, and keep signing credentials outside this repository.

## Linux installation

Linux users currently run the isolated source environment documented in the README. This avoids implying that one binary can integrate correctly with every distribution, architecture, desktop environment, and system crypto configuration. An onedir tarball or distribution-native package can be added later when concrete targets are selected; Docker is not part of the desktop application model.

## Security boundary

The packaged build is intended for one person. Manual unlock keeps the database password in process memory only. Optional device unlock uses the browser's user-verifying WebAuthn platform authenticator and PRF extension, so Windows Hello, Touch ID, or a device password/PIN must release the encryption key before the locally stored password envelope can be opened. Browsers or platform authenticators without PRF support fall back to the app password without weakening the vault.

The envelope is scoped to the local host used during setup, excluded from portable backups, and useless without both the platform credential and its PRF output. It is convenience on that installation, not a replacement for remembering the app password and keeping a separately password-protected backup. Do not expose the local server to the public internet. A hosted multi-user edition would require a different authentication and key-management design, per-user isolation, HTTPS, and a separate deployment review.

The application accepts only loopback hosts (`127.0.0.1`, `::1`, or `localhost`), so other devices on the network cannot connect. A non-loopback `EXPENSETICS_HOST` value is rejected at startup.

The default browser origin is `http://localhost`, which is WebAuthn's explicit secure-context exception for local HTTP. Opening the same port through `127.0.0.1` or `::1` remains local but cannot enroll a standards-compliant device-unlock credential; use the `localhost` URL for Windows Hello or Touch ID.

## Sharing on macOS

An unsigned folder is suitable for local testing, but macOS Gatekeeper may warn when a friend opens software downloaded from the internet. For polished public distribution outside the Mac App Store, sign the build with an Apple Developer ID and submit it to Apple's notarization service. Signing credentials should stay outside this repository and should never be committed.
