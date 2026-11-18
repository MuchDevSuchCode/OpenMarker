# PyInstaller recipe for the installed app. Run through packaging/build.py.
#
# Windows: dist/OpenMarker/OpenMarker.exe (no console) and openmarker-cli.exe (the same
#          program with a console, for the command line and for hooks).
# macOS:   dist/OpenMarker.app, a menu-bar app with no Dock icon.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT / "src"))
from openmarker import __version__  # noqa: E402

WINDOWS, MAC = sys.platform == "win32", sys.platform == "darwin"

datas = [(str(ROOT / "src" / "openmarker" / "static"), "openmarker/static")]
binaries, hidden = [], collect_submodules("openmarker")

# Packages that load parts of themselves by name or carry data files of their own.
whole = ["rembg", "pymatting", "pypdfium2", "pypdfium2_raw", "rmscene", "icalendar", "recurring_ical_events",
         "x_wr_timezone", "anthropic", "pystray"]
if WINDOWS:
    whole.append("winrt")
for package in whole:
    try:
        found = collect_all(package)
    except Exception:  # not installed on this system
        continue
    datas += found[0]
    binaries += found[1]
    hidden += found[2]
for package in ("rembg", "pymatting", "icalendar", "recurring_ical_events", "anthropic", "pooch", "onnxruntime"):
    try:
        datas += copy_metadata(package)
    except Exception:
        pass

analysis = Analysis(
    [str(ROOT / "packaging" / "run_app.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PyQt5", "PyQt6", "PySide2", "PySide6", "torch", "pandas"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
icon = str(ROOT / "packaging" / ("icon.icns" if MAC else "icon.ico"))


def program(name, console):
    return EXE(pyz, analysis.scripts, [], exclude_binaries=True, name=name, console=console,
               icon=icon, upx=False, disable_windowed_traceback=False)


programs = [program("OpenMarker", console=False)]
if WINDOWS:
    programs.append(program("openmarker-cli", console=True))
folder = COLLECT(*programs, analysis.binaries, analysis.datas, upx=False, name="OpenMarker")

if MAC:
    app = BUNDLE(
        folder,
        name="OpenMarker.app",
        icon=icon,
        bundle_identifier="io.github.muchdevsuchcode.openmarker",
        version=__version__,
        info_plist={
            "CFBundleDisplayName": "OpenMarker",
            "CFBundleShortVersionString": __version__,
            "LSUIElement": True,  # lives in the menu bar, not the Dock
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "OpenMarker is open source.",
        },
    )
