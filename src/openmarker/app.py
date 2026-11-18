"""The installed app: OpenMarker with no terminal.

Starting it runs the local service in the background and opens the library
in the browser. Starting it again while it is running just opens the library.
It shows an icon with Open and Quit in the Windows notification area or the
macOS menu bar, and can be stopped from Settings too. Anything it would have printed goes to
`~/openmarker/openmarker.log`.

    OpenMarker                 start (or show) the app
    OpenMarker --background    start without opening the browser (for starting at login)
    OpenMarker --quit          stop the running app (the installer does this before an update)
    OpenMarker <command> ...   run a command-line command, as `python -m openmarker` does
"""
from __future__ import annotations

import sys
import threading
import webbrowser
from pathlib import Path

LOG_LIMIT = 1_000_000
MUTEX = "OpenMarkerRunning"  # the Windows installer waits for this to go before replacing files


def _log_to_file() -> None:
    """A windowed program has nowhere to print; keep what is said in a small log file."""
    path = Path.home() / "openmarker" / "openmarker.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists() or path.stat().st_size > LOG_LIMIT
        out = open(path, "w" if fresh else "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    sys.stdout = sys.stderr = out


def _hold_mutex() -> object | None:
    if sys.platform != "win32":
        return None
    import ctypes

    return ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX)


def _tray(url: str, stop) -> object | None:
    """An icon in the Windows notification area or the macOS menu bar, or None where there is none."""
    if sys.platform not in ("win32", "darwin"):
        return None
    try:
        import pystray
        from PIL import Image

        picture = Image.open(Path(__file__).parent / "static" / "icon.png")
        return pystray.Icon("OpenMarker", picture, "OpenMarker", pystray.Menu(
            pystray.MenuItem("Open OpenMarker", lambda: webbrowser.open(url), default=True),
            pystray.MenuItem("Quit", lambda: stop()),
        ))
    except Exception as exc:  # the app is still usable without the icon
        print(f"no icon: {exc!r}")
        return None


def _quit_running() -> None:
    import urllib.request

    from . import server

    request = urllib.request.Request(f"{server.address()}api/quit", data=b"{}", headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(request, timeout=5).close()
    except OSError:
        pass  # nothing was running


def main() -> None:
    from . import tablet

    if tablet.answer_askpass():
        return
    args = [a for a in sys.argv[1:] if not a.startswith("-psn_")]  # older macOS adds one when opening an app
    if args == ["--quit"]:
        _quit_running()
        return
    if args and args != ["--background"]:
        from .__main__ import main as command_line

        command_line()
        return
    if sys.stdout is None or sys.stderr is None or getattr(sys, "frozen", False):
        _log_to_file()

    from . import server

    url = server.address()
    if server.running():
        webbrowser.open(url)
        return
    try:
        httpd = server.start()
    except OSError as exc:
        print(f"could not start: {exc!r}")
        webbrowser.open(url)  # whatever holds the port is shown, which explains more than silence
        return
    held = _hold_mutex()  # noqa: F841 (kept until the process ends)
    print(f"OpenMarker started at {url}")
    if not args:
        webbrowser.open(url)

    icon = _tray(url, httpd.shutdown)
    if icon is None:
        httpd.serve_forever()
        return

    def serve() -> None:
        try:
            httpd.serve_forever()
        finally:
            icon.stop()

    serving = threading.Thread(target=serve, daemon=True)
    serving.start()
    try:
        icon.run()
    except Exception as exc:  # carry on without the icon
        print(f"the icon stopped: {exc!r}")
        serving.join()
    else:
        httpd.shutdown()  # the icon went away some other way: stop serving too


if __name__ == "__main__":
    main()
