#!/bin/sh
# Start OpenMarker. The first run creates a private Python environment in
# .venv and installs OpenMarker into it; later runs start straight away.
#
#   ./launch.sh                 open the library in your browser
#   ./launch.sh <command> ...   run any OpenMarker command, e.g. ./launch.sh setup
set -e
root=$(cd "$(dirname "$0")" && pwd)
py="$root/.venv/bin/python"

if [ ! -x "$py" ]; then
    echo "Setting up OpenMarker. This happens once and takes a few minutes."
    python3 -m venv "$root/.venv"
    (cd "$root" && "$py" -m pip install --quiet --disable-pip-version-check -e .)
fi
[ $# -eq 0 ] && set -- ui
exec "$py" -m openmarker "$@"
