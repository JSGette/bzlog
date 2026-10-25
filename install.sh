#!/bin/sh
# Install from a source checkout or extracted release; never downloads code.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "${PYTHON:-python3}" "$script_dir/install.py" "$@"
