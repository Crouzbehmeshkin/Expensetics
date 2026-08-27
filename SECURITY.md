# Security policy

Expensetics stores personal financial records and treats confidentiality, deterministic behavior, and recoverability as product requirements.

## Supported versions

Before the first stable release, only the current `main` branch is supported. Security fixes are applied there; older commits and encrypted backups produced by an earlier pre-v1 schema do not receive compatibility guarantees.

## Reporting a vulnerability

Use GitHub's **Report a vulnerability** form when it is available for this repository. If private vulnerability reporting is not available, open an issue titled `Security contact request` containing only a high-level description and ask the maintainer to arrange a private channel.

Do not include exploit details, passwords, vaults, device-unlock envelopes, original bank exports, account identifiers, or transaction data in a public issue. A useful private report includes the affected commit, operating system, impact, reproducible steps using synthetic data, and any proposed mitigation.

## Dependency security

### Direct dependencies

- **NiceGUI 3.16.0** provides the local browser UI. It is the framework requested by the project specification and builds on established FastAPI, Starlette, Uvicorn, Pydantic, and aiohttp components. Version 3.16.0 includes the upstream fix for GHSA-46f2-xhpw-8vh3; earlier versions accepted unbounded Socket.IO request bodies.
- **sqlcipher3 0.6.2** supplies maintained Windows, macOS, and Linux wheels for SQLCipher 4.12.0. It provides full-database AES encryption and is used for every production database and portable backup connection.
- **pytest 9.0.3** is development-only and runs the correctness tests. The application does not import it at runtime.
- **pytest-cov 7.1.0** and **Playwright 1.62.0** are development-only coverage and browser-regression tools.
- **pip-audit 2.10.1** is the PyPA-maintained development-only advisory scanner.
- **pip-tools 7.6.1** is the Jazzband-maintained dependency resolver used only to generate deterministic hash locks. It is isolated in `.dependency-venv` and is never imported or shipped by the application.
- **PyInstaller 6.21.0** is optional build-only tooling used by NiceGUI's official `nicegui-pack` workflow. It is installed into `.build-venv` and is not shipped as an importable application dependency.

`requirements/runtime.in`, `requirements/dev.in`, `requirements/build.in`, and `requirements/tools.in` list direct runtime, testing, packaging, and dependency-maintenance packages separately. Their corresponding `.lock` files record every transitive version and accepted SHA-256 artifact hash. Setup, build, and maintenance scripts use pip's `--require-hashes`, so an unlisted distribution artifact is rejected. The canonical review and verification process, including the optional rollback-capable helper, is documented in [`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md).

## Device unlock design

Device unlock adds no package dependency. It uses browser and operating-system primitives already present on supported Windows and macOS installations:

- a user-verifying WebAuthn **platform** credential (`userVerification: required`);
- the WebAuthn Level 3 PRF extension to produce a credential-specific 256-bit secret;
- Web Crypto AES-GCM with a fresh 96-bit IV and fixed application context as authenticated additional data;
- a small local envelope containing only version, credential ID, non-secret authenticator transport hints, PRF salt, IV, ciphertext, and relying-party host.

The PRF output and readable database password are never persisted. The envelope is excluded from encrypted backups and is removed when device unlock is disabled or all local data is deleted. Changing the app password rewraps the envelope after another device verification; if that update cannot complete, device unlock is removed and manual unlock remains valid.

This is intentionally a local vault-unlock mechanism, not a remote account login. AES-GCM authentication and SQLCipher's key verification fail closed if the platform credential, PRF output, envelope, or database password does not match. Browser/platform support is feature-detected and the normal app-password path is always retained. A compromised operating-system user session remains outside the threat model: software running as that user can inspect the live process or attempt to imitate a local service.

## Runtime authorization

Unlocking issues a random, server-tracked capability to that browser session. Every production database connection requires the capability at the shared database boundary; UI pages and editors receive a session-bound repository instead of using a global repository. A stale callback, old tab, or newly opened browser session therefore cannot read or mutate the vault merely because another browser has unlocked the process. Each capability expires after 10 minutes without browser activity; expiry is enforced server-side.

Locking invalidates every issued capability, waits for in-flight authorized database or backup operations to finish, clears the in-memory database password, and redirects connected clients to the unlock page. Restore and password-rekey operations use an exclusive maintenance lease: existing work drains first and new reads or writes wait until the verified vault-wide mutation finishes. Temporary databases used by automated tests are unprotected by default, but can opt into the same boundary. The production database cannot opt out.

The HTTP server accepts only loopback bind addresses. Its default browser origin is `http://localhost`, the WebAuthn-defined local HTTP exception needed for a valid relying-party domain; IP-literal loopback URLs cannot enroll device unlock. This prevents an environment-variable mistake from exposing the password-bearing local UI over an unencrypted LAN connection. Expensetics has no remote-access mode.

Encrypted backup creation uses a unique app-created temporary file, is atomic, and refuses to replace an existing destination. Restore verifies a separately encrypted, current-schema replacement before atomically swapping it into place. Delete-all invalidates sessions first and removes only the exact active vault plus its known WAL, journal, interrupted-migration, and interrupted-restore artifacts; it never deletes by extension or scans arbitrary backups. Cleanup continues if one named artifact is temporarily locked.

## Current verification

The runtime, development, packaging, and dependency-maintenance locks were checked again on 2026-08-27 with the PyPA-maintained `pip-audit` 2.10.1 against the Python Packaging Advisory Database. All four final hash-locked graphs reported **no known vulnerabilities**. `pip check` also reported no broken or incompatible requirements.

This is a point-in-time check, not a guarantee that future advisories will not be published. Re-run an audit before releases and after dependency changes.

The optional PyInstaller build environment is intentionally separate and hash-locked. Its dependency graph is audited, but each generated Windows or macOS folder should still be scanned before distribution. For macOS distribution beyond local testing, use Developer ID signing and Apple notarization; keep all signing credentials outside the repository.
