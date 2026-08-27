# Dependency maintenance

This document is the dependency-maintenance process for Expensetics. The update script is an optional convenience for the mechanical lock-and-audit commands; it does not choose packages, assess compatibility, replace code review, or prove that the application still works.

Normal use, tests, packaging, and dependency maintenance each have a separate project-local environment. Nothing needs to be installed into the system Python.

The optional Windows installer wrapper uses Inno Setup 6 from its official distribution. It is an external build tool rather than an application dependency: end users receive only its compiled installer, and the application runtime remains defined entirely by the hashed Python locks. Review and update Inno Setup separately before Windows releases.

## Files and environments

| Purpose | Direct dependencies | Generated artifact lock | Local environment |
| --- | --- | --- | --- |
| Application | `requirements/runtime.in` | `requirements/runtime.lock` | `.venv` |
| Tests | `requirements/dev.in` | `requirements/dev.lock` | `.venv` |
| Desktop packaging | `requirements/build.in` | `requirements/build.lock` | `.build-venv` |
| Lock generation and auditing | `requirements/tools.in` | `requirements/tools.lock` | `.dependency-venv` |

The short `.in` files are the only places where direct versions are chosen. Generated `.lock` files flatten the complete dependency graphs and allow only recorded SHA-256 distribution hashes. Keeping every profile in one directory makes the input/lock pairs visible without cluttering the repository root. Commit both files whenever a dependency changes.

## Maintenance process

Use this process before a release, after a relevant security advisory, and once a month even when no direct dependency version changes.

### 1. Research the change

Before editing a version:

- read the upstream release notes and migration guidance between the installed and proposed versions;
- confirm Python 3.11/3.12 and supported Windows/macOS compatibility;
- confirm that required wheels still exist, especially for SQLCipher and packaging dependencies;
- review published advisories, project ownership, recent maintenance activity, and license compatibility;
- identify whether the package is runtime, test-only, build-only, or maintenance-only;
- prefer a focused update over adding another package or refreshing the entire graph.

For a new runtime dependency, document why the standard library and existing packages are insufficient in [`../SECURITY.md`](../SECURITY.md) before accepting it.

### 2. Change dependency intent

Change the exact direct pin in the relevant short `.in` file. Do not edit generated lock versions or hashes by hand.

### 3. Regenerate and scan

The optional helper performs the deterministic mechanical work:

   ```powershell
   .\scripts\update_dependencies.ps1
   ```

   On macOS or Linux:

   ```sh
   sh scripts/update_dependencies.sh
   ```

It regenerates all four locks with `pip-tools`, installs no packages globally, and runs `pip-audit --strict` against each result. The underlying controls are the generated SHA-256 artifact allowlists and the four explicit `pip-audit` checks; the wrapper is not a security boundary.

The default update preserves unrelated locked versions where resolution permits. To refresh one transitive package deliberately, use `--package PACKAGE`; the option can be repeated. Use `--all` only for a deliberate full graph refresh:

```powershell
.\scripts\update_dependencies.ps1 --package aiohttp
.\scripts\update_dependencies.ps1 --all
```

The updater creates `.dependency-venv` from `requirements/tools.lock`, refreshes that toolchain first, and restores all previous locks if compilation or an audit fails. Editing `requirements/tools.in` and following this same process updates `pip-tools` or `pip-audit`.

If the helper ever becomes harder to understand than the commands it wraps, remove it and run the pinned `pip-compile` and `pip-audit` commands directly from `.dependency-venv`. The file separation and review process remain the durable design.

### 4. Review the result

Inspect every requirement and lock diff before installing it:

- the requested direct version is exact and expected;
- unrelated locked versions did not move without explanation;
- new transitive packages have an identifiable `# via` dependency path;
- no test, build, or maintenance package leaked into `requirements/runtime.lock`;
- every locked distribution has SHA-256 hashes;
- removed packages are genuinely no longer needed;
- major-version, platform-specific, and native-wheel changes receive extra scrutiny.

Do not merge or distribute an unexplained lock diff.

### 5. Install and run automated verification

For runtime or test changes on Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements/dev.lock
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -m "not e2e" --cov=finance_app
.\.venv\Scripts\python.exe -m pytest -m e2e
```

Install Chromium once with `.\.venv\Scripts\python.exe -m playwright install chromium` if the browser executable is not already present. On macOS, use `./.venv/bin/python` in the same commands.

For packaging changes, build on each target operating system using `scripts/build_windows.ps1` or `scripts/build_macos.sh`. PyInstaller output is platform-specific, so a successful Windows build does not validate macOS.

### 6. Check real functionality

Automated tests are necessary but not the final release check for runtime, database, browser, or packaging updates. Never use the personal vault for this check. Start the app with `EXPENSETICS_DATA_DIR` pointing to a disposable folder under `.test-tmp`, then verify the affected paths and this core smoke flow:

```powershell
$env:EXPENSETICS_DATA_DIR = Join-Path $PWD '.test-tmp\dependency-smoke'
.\.venv\Scripts\python.exe app.py
```

On macOS:

```sh
EXPENSETICS_DATA_DIR="$PWD/.test-tmp/dependency-smoke" ./.venv/bin/python app.py
```

1. create and unlock a disposable encrypted vault;
2. add an expense with the keyboard-first flow and confirm overview totals update;
3. edit and delete the expense, including the delete confirmation;
4. import a supported bank fixture through the review grid and confirm duplicate handling;
5. navigate Overview, Expenses, Budget, Accounts, Liabilities, Insights, and Settings;
6. create and restore an encrypted backup, then lock and unlock the vault;
7. if packaging changed, repeat vault unlock and expense creation from the packaged application.

Record the operating system, package versions changed, audit result, automated-test result, and smoke paths checked in the change description. A tool-only update needs its tool workflow checked, not an unrelated full UI exercise; use judgment based on what the dependency can affect.

### 7. Respond to vulnerabilities

When an audit reports a vulnerability:

1. trace the package through the lock file's `# via` lines and determine whether it is runtime, test, build, or maintenance exposure;
2. read the original advisory and upstream fix rather than relying only on its severity label;
3. update to the smallest supported fixed version and rerun this entire process;
4. do not suppress the advisory merely to make CI pass;
5. if it is demonstrably unreachable, document the technical rationale, owner, and review date before accepting a temporary exception.

Treat an exploitable runtime or encryption-related issue as release-blocking. Tooling vulnerabilities still need resolution, but their non-runtime scope should be recorded accurately.

## Review policy

- Prefer established, actively maintained packages with a clear upstream owner and release history.
- Keep runtime dependencies to those that materially improve the application; test/build tools must never leak into the runtime lock.
- Do not accept an unexpected major update or a large transitive diff without reading upstream compatibility notes.
- A clean `pip-audit` result means no matching advisory was known to its advisory sources at that moment; it is not a guarantee of safety.
- Artifact hashes make installation reproducible and reject unreviewed files. They do not establish that the package's code is trustworthy.
- Follow the process after dependency changes and before a release. CI installs by hash, runs the full test suite, and audits every maintained lock.

The current direct-package rationale and security architecture are documented in [`../SECURITY.md`](../SECURITY.md).
