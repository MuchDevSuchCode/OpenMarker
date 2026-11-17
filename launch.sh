#!/bin/sh
# Start OpenMarker. The first run creates a private Python environment in
# .venv and installs OpenMarker into it; later runs start straight away.
#
#   ./launch.sh                 open the library in your browser
#   ./launch.sh --with-ai       also install the AI picture cut-out, then open
#   ./launch.sh <command> ...   run any OpenMarker command, e.g. ./launch.sh setup
set -e
root=$(cd "$(dirname "$0")" && pwd)
py="$root/.venv/bin/python"

target="."
install=""
if [ "$1" = "--with-ai" ]; then
    target=".[ai]"
    install=1
    shift
    set -- ui "$@"
fi
if [ ! -x "$py" ]; then
    echo "Setting up OpenMarker. This happens once and takes a few minutes."
    python3 -m venv "$root/.venv"
    install=1
fi
if [ -n "$install" ]; then
    (cd "$root" && "$py" -m pip install --quiet --disable-pip-version-check -e "$target")
fi
[ $# -eq 0 ] && set -- ui
exec "$py" -m openmarker "$@"
