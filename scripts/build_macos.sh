#!/bin/sh
set -eu

if [ "$(uname -s)" != "Darwin" ]; then
    echo "This script must be run on macOS; PyInstaller packages are platform-specific." >&2
    exit 1
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(dirname -- "$script_dir")
project_python="$project_root/.venv/bin/python"
build_venv="$project_root/.build-venv"

if [ ! -x "$project_python" ]; then
    echo "Run 'sh scripts/setup.sh' before building the macOS package." >&2
    exit 1
fi

if [ ! -d "$build_venv" ]; then
    "$project_python" -m venv "$build_venv"
fi

"$build_venv/bin/python" -m pip install --require-hashes -r "$project_root/requirements/build.lock"

cd "$project_root"
"$build_venv/bin/nicegui-pack" app.py \
    --name Expensetics \
    --onedir \
    --clean \
    --noconfirm \
    --add-data "$project_root/finance_app/styles:finance_app/styles" \
    --add-data "$project_root/finance_app/assets:finance_app/assets"

printf '\nmacOS package created in dist/Expensetics.\nZip that entire folder before sharing it.\n'
