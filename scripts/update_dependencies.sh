#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(dirname -- "$script_dir")
project_python="$project_root/.venv/bin/python"

if [ -x "$project_python" ]; then
    python_command=$project_python
elif command -v python3 >/dev/null 2>&1; then
    python_command=$(command -v python3)
else
    echo "Python 3.11 or newer is required." >&2
    exit 1
fi

cd "$project_root"
"$python_command" "$script_dir/update_dependencies.py" "$@"
