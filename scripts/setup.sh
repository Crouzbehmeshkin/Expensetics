#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(dirname -- "$script_dir")
venv_path="$project_root/.venv"
venv_python="$venv_path/bin/python"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3.11 or newer is required. Install Python, then run this script again." >&2
    exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "Python 3.11 or newer is required. Upgrade Python, then run this script again." >&2
    exit 1
fi

if [ ! -x "$venv_python" ]; then
    if [ -e "$venv_path" ]; then
        echo "The existing .venv is incomplete. Remove that project-local folder, then run setup again." >&2
        exit 1
    fi
    python3 -m venv "$venv_path"
fi

if ! "$venv_python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "The existing .venv uses Python older than 3.11. Recreate it with a supported Python." >&2
    exit 1
fi

"$venv_python" -m pip install --require-hashes -r "$project_root/requirements/runtime.lock"

printf '\nSetup complete.\nRun: ./.venv/bin/python app.py\n'
