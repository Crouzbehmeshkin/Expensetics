"""Regenerate and audit Expensetics dependency locks.

The dependency toolchain lives in its own project-local virtual environment so
normal app, test, and packaging environments remain isolated.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
import venv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_VENV = PROJECT_ROOT / ".dependency-venv"
TOOLS_MARKER = TOOLS_VENV / ".requirements.sha256"


@dataclass(frozen=True)
class LockSpec:
    source: str
    lock: str
    allow_unsafe: bool = False

    @property
    def source_path(self) -> Path:
        return PROJECT_ROOT / self.source

    @property
    def lock_path(self) -> Path:
        return PROJECT_ROOT / self.lock


TOOLS_LOCK = LockSpec(
    "requirements/tools.in",
    "requirements/tools.lock",
    allow_unsafe=True,
)
APP_LOCKS = (
    LockSpec("requirements/runtime.in", "requirements/runtime.lock"),
    LockSpec("requirements/dev.in", "requirements/dev.lock", allow_unsafe=True),
    LockSpec("requirements/build.in", "requirements/build.lock", allow_unsafe=True),
)
ALL_LOCKS = (TOOLS_LOCK, *APP_LOCKS)


def _venv_executable(windows_name: str, posix_name: str) -> Path:
    folder = "Scripts" if os.name == "nt" else "bin"
    name = windows_name if os.name == "nt" else posix_name
    return TOOLS_VENV / folder / name


def _tool_python() -> Path:
    return _venv_executable("python.exe", "python")


def _pip_compile() -> Path:
    return _venv_executable("pip-compile.exe", "pip-compile")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(command: list[str], *, environment: dict[str, str] | None = None) -> None:
    display = subprocess.list2cmdline(command)
    print(f"\n> {display}", flush=True)
    subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
    )


def _install_tools() -> None:
    if not _tool_python().is_file():
        print(f"Creating isolated dependency tool environment at {TOOLS_VENV.name}")
        venv.EnvBuilder(with_pip=True).create(TOOLS_VENV)

    required_hash = _sha256(TOOLS_LOCK.lock_path)
    installed_hash = TOOLS_MARKER.read_text(encoding="ascii").strip() if TOOLS_MARKER.exists() else ""
    tools_are_usable = _pip_compile().is_file()
    if required_hash == installed_hash and tools_are_usable:
        return

    _run(
        [
            str(_tool_python()),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--require-hashes",
            "-r",
            str(TOOLS_LOCK.lock_path),
        ]
    )
    TOOLS_MARKER.write_text(required_hash + "\n", encoding="ascii")


def _compile_lock(
    spec: LockSpec,
    *,
    upgrade_all: bool,
    upgrade_packages: tuple[str, ...],
) -> None:
    command = [
        str(_pip_compile()),
        "--quiet",
        "--generate-hashes",
        "--strip-extras",
        "--output-file",
        spec.lock,
    ]
    if spec.allow_unsafe:
        command.append("--allow-unsafe")
    if upgrade_all:
        command.append("--upgrade")
    for package in upgrade_packages:
        command.extend(("--upgrade-package", package))
    command.append(spec.source)

    environment = os.environ.copy()
    public_command = ["python", "scripts/update_dependencies.py"]
    if upgrade_all:
        public_command.append("--all")
    for package in upgrade_packages:
        public_command.extend(("--package", package))
    environment["CUSTOM_COMPILE_COMMAND"] = subprocess.list2cmdline(public_command)
    _run(command, environment=environment)


def _audit(spec: LockSpec) -> None:
    _run(
        [
            str(_tool_python()),
            "-m",
            "pip_audit",
            "--strict",
            "--disable-pip",
            "--require-hashes",
            "-r",
            spec.lock,
        ]
    )


def _snapshot_locks() -> dict[Path, bytes | None]:
    return {
        spec.lock_path: spec.lock_path.read_bytes() if spec.lock_path.exists() else None
        for spec in ALL_LOCKS
    }


def _restore_locks(snapshots: dict[Path, bytes | None]) -> None:
    for path, content in snapshots.items():
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(content)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Regenerate all hash locks and reject known-vulnerable results.",
    )
    upgrade = parser.add_mutually_exclusive_group()
    upgrade.add_argument(
        "--all",
        action="store_true",
        help="upgrade every compatible transitive dependency",
    )
    upgrade.add_argument(
        "--package",
        action="append",
        default=[],
        metavar="NAME",
        help="upgrade only this package (repeatable)",
    )
    return parser.parse_args()


def main() -> int:
    if sys.version_info < (3, 11):
        print("Python 3.11 or newer is required.", file=sys.stderr)
        return 2

    args = _parse_args()
    packages = tuple(args.package)
    snapshots = _snapshot_locks()
    try:
        _install_tools()

        # Refresh the tool lock first, then use that exact toolchain for every
        # application lock and audit in this same run.
        _compile_lock(TOOLS_LOCK, upgrade_all=args.all, upgrade_packages=packages)
        _install_tools()
        for spec in APP_LOCKS:
            _compile_lock(spec, upgrade_all=args.all, upgrade_packages=packages)
        for spec in ALL_LOCKS:
            _audit(spec)
    except (OSError, subprocess.CalledProcessError) as error:
        _restore_locks(snapshots)
        print(
            f"\nDependency update failed; previous lock files were restored.\n{error}",
            file=sys.stderr,
        )
        return 1

    print("\nAll dependency locks were regenerated and audited successfully.")
    print("Review the lock-file diff, then run the tests documented in docs/DEPENDENCIES.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
