#!/bin/sh
# Build and smoke only disposable files; never invoke an SDK query.
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
scratch=$(mktemp -d "${TMPDIR:-/tmp}/mahler-sdk-verify.XXXXXXXX")
trap 'rm -rf -- "$scratch"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$scratch/home" "$scratch/tmp" "$scratch/unrelated"
# Remove inherited credentials, Python paths and user pip configuration.
env -i PATH="$PATH" HOME="$scratch/home" TMPDIR="$scratch/tmp" \
    PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 \
    /bin/sh -eu -s -- "$repo" "$scratch" <<'SHELL'
repo=$1
scratch=$2
python3 -I - "$repo/sdk_runner" "$scratch/source" <<'PY'
from pathlib import Path
import shutil
import sys
source = Path(sys.argv[1])
if not (source / 'README.md').is_file():
    raise SystemExit('sdk_runner/README.md is required')
shutil.copytree(source, sys.argv[2],
                ignore=shutil.ignore_patterns('build', 'dist', '*.egg-info', '__pycache__'))
PY
python3 -I -m venv "$scratch/venv"
python="$scratch/venv/bin/python"
"$python" -I -m pip wheel --no-deps "$scratch/source" --wheel-dir "$scratch/wheels"
set -- "$scratch"/wheels/mahler_sdk_runner-*.whl
[ "$#" -eq 1 ] && [ -f "$1" ]
"$python" -I -m pip install --no-deps "$1"
cd "$scratch/unrelated"
"$python" -I - "$repo/sdk_runner/pyproject.toml" <<'PY'
from importlib.metadata import distribution
from pathlib import Path
import sys
import tomllib

project = tomllib.loads(Path(sys.argv[1]).read_text())['project']
dist = distribution('mahler-sdk-runner')
assert dist.metadata['Name'] == project['name'], 'distribution name'
assert dist.version == project['version'], 'distribution version'
assert dist.metadata['Requires-Python'] == project['requires-python'], 'Python requirement'
assert dist.metadata.get_payload().strip(), 'packaged README'
entries = [e for e in dist.entry_points if e.group == 'console_scripts'
           and e.name == 'mahler-sdk-run']
assert len(entries) == 1 and entries[0].value == 'mahler_sdk_run:main', 'invalid entry point'
assert callable(entries[0].load()), 'entry point is not callable'
import mahler_sdk_run
assert Path(mahler_sdk_run.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), \
    'mahler_sdk_run must come from the installed wheel'
PY
"$scratch/venv/bin/mahler-sdk-run" --help > "$scratch/help.txt"
"$python" -I - "$scratch/help.txt" <<'PY'
from pathlib import Path
import sys
help_text = Path(sys.argv[1]).read_text()
for expected in ('usage: mahler-sdk-run', '--prompt-file', '--cwd', '--model', '--max-turns'):
    assert expected in help_text, f'missing CLI help: {expected}'
print('SDK runner packaging verified')
PY
SHELL
