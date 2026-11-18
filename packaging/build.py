"""Build the installer for the system this runs on.

    pip install -e ".[build]"
    python packaging/build.py            # everything
    python packaging/build.py --app-only # stop after the app folder (no installer)

Windows: dist/OpenMarker-<version>-windows-setup.exe   (needs Inno Setup 6: https://jrsoftware.org/isdl.php)
macOS:   dist/OpenMarker-<version>-macos-<arch>.dmg

The packaged app is asked to check itself (`selftest`) before it is wrapped, so
a build that left something out fails here and not on someone's computer.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
sys.path.insert(0, str(ROOT / "src"))
from openmarker import __version__  # noqa: E402


def run(*command: str | Path) -> None:
    print(">", *map(str, command), flush=True)
    subprocess.run([str(part) for part in command], check=True, cwd=ROOT)


def build_app() -> Path:
    """Freeze the app, and return the program to run for the command line."""
    for old in (DIST / "OpenMarker", DIST / "OpenMarker.app"):
        shutil.rmtree(old, ignore_errors=True)
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", DIST, "--workpath", ROOT / "build", ROOT / "packaging" / "openmarker.spec")
    if sys.platform == "darwin":
        shutil.rmtree(DIST / "OpenMarker", ignore_errors=True)  # the same files, outside the .app
        return DIST / "OpenMarker.app" / "Contents" / "MacOS" / "OpenMarker"
    return DIST / "OpenMarker" / ("openmarker-cli.exe" if sys.platform == "win32" else "OpenMarker")


def inno_setup() -> str:
    places = [shutil.which("ISCC"), shutil.which("iscc")]
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs")):
        if base:
            places.append(str(Path(base) / "Inno Setup 6" / "ISCC.exe"))
    for place in places:
        if place and Path(place).exists():
            return place
    sys.exit("Inno Setup 6 was not found. Install it from https://jrsoftware.org/isdl.php and run this again.")


def windows_installer() -> Path:
    run(inno_setup(), f"/DAppVersion={__version__}", "/Qp", ROOT / "packaging" / "windows.iss")
    return DIST / f"OpenMarker-{__version__}-windows-setup.exe"


def mac_disk_image() -> Path:
    arch = {"arm64": "apple-silicon", "x86_64": "intel"}.get(platform.machine(), platform.machine())
    target = DIST / f"OpenMarker-{__version__}-macos-{arch}.dmg"
    stage = ROOT / "build" / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    run("ditto", DIST / "OpenMarker.app", stage / "OpenMarker.app")
    (stage / "Applications").symlink_to("/Applications")  # drag the app onto this to install
    target.unlink(missing_ok=True)
    run("hdiutil", "create", "-volname", "OpenMarker", "-srcfolder", stage, "-ov", "-format", "UDZO", target)
    return target


def main() -> None:
    program = build_app()
    run(program, "selftest")
    if "--app-only" in sys.argv:
        print(f"Built {program.parent}")
        return
    if sys.platform == "win32":
        made = windows_installer()
    elif sys.platform == "darwin":
        made = mac_disk_image()
    else:
        sys.exit("Installers are built for Windows and macOS. On other systems use launch.sh.")
    print(f"Built {made} ({made.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
