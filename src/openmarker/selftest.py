"""Check that every part of the app loads and works in this installation.

Run as `openmarker selftest`. It touches nothing outside a temporary folder
and never contacts the tablet or the network; packaged builds run it to prove
nothing was left out.
"""
from __future__ import annotations

import io
import sys
import tempfile
from datetime import date
from pathlib import Path


def _checks() -> list[tuple[str, object]]:
    def pages() -> str:
        from . import generate, render

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "agenda.pdf"
            path.write_bytes(generate.agenda_pdf(date(2026, 1, 5), [("09:00", "10:00", "Planning")]))
            path.write_bytes(generate.text_pdf("Title", "Some words.", "Kicker", ["One", "Two"]))
            picture = render.pdf_page_png(str(path), path.stat().st_mtime, 0)
        return f"made a PDF and drew it ({len(picture)} bytes)"

    def drawing() -> str:
        from PIL import Image, ImageDraw

        from . import images, ink, render, scene

        picture = Image.new("RGB", (400, 300), "white")
        pen = ImageDraw.Draw(picture)
        pen.ellipse((60, 60, 240, 240), outline="black", width=6)
        pen.line((40, 270, 360, 30), fill="black", width=5)
        data = io.BytesIO()
        picture.save(data, "PNG")
        cleaned, _ = images.clean(data.getvalue(), "cutout")
        images.page_pdf(cleaned, "Drawing")
        strokes = ink.trace(picture)
        page = scene.read(io.BytesIO(ink.page_file(strokes)))
        if not page.strokes:
            raise RuntimeError("the traced page came back empty")
        render.png(page)
        return f"traced {len(strokes)} pen strokes and read them back"

    def handwriting() -> str:
        from . import ocr

        if sys.platform != "win32":
            return "not on this system (it uses the recogniser built into Windows)"
        if not ocr.available():
            raise RuntimeError("the Windows recogniser could not be loaded")
        from PIL import Image, ImageDraw

        from . import ink, scene

        picture = Image.new("RGB", (500, 200), "white")
        ImageDraw.Draw(picture).line((60, 100, 440, 100), fill="black", width=5)
        ocr.recognise(scene.read(io.BytesIO(ink.page_file(ink.trace(picture, shading=False)))))
        return "the Windows recogniser ran"

    def cutout() -> str:
        import onnxruntime

        from . import images

        if not images.subject_available():
            raise RuntimeError("rembg could not be loaded")
        return f"ready (onnxruntime {onnxruntime.__version__}; its model downloads on first use)"

    def calendar() -> str:
        import icalendar
        import recurring_ical_events  # noqa: F401

        icalendar.Calendar.from_ical("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nEND:VCALENDAR\r\n")
        return "ready"

    def assistant() -> str:
        import anthropic

        anthropic.Anthropic(api_key="not-a-key")
        return f"ready (anthropic {anthropic.__version__}; off until switched on)"

    def interface() -> str:
        from . import __version__, server

        if "<title>" not in server.UI_FILE.read_text(encoding="utf-8"):
            raise RuntimeError("the page is missing")
        if not (server.UI_FILE.parent / "icon.png").exists():
            raise RuntimeError("the icon is missing")
        return f"version {__version__}"

    def remote() -> str:
        import shutil

        missing = [name for name in ("ssh", "ssh-keygen") if not shutil.which(name)]
        if missing:
            raise RuntimeError(f"{' and '.join(missing)} not found; the tablet cannot be reached without it")
        return shutil.which("ssh")

    return [("Interface", interface), ("Documents and pages", pages), ("Pictures and pen strokes", drawing),
            ("Handwriting recognition", handwriting), ("Picture cut-out", cutout), ("Calendar", calendar),
            ("AI", assistant), ("Connection to the tablet", remote)]


def run() -> int:
    failed = 0
    for name, check in _checks():
        try:
            print(f"ok      {name}: {check()}", flush=True)
        except Exception as exc:  # report every part, not only the first that fails
            failed += 1
            print(f"FAILED  {name}: {exc!r}", flush=True)
    print("Everything works." if not failed else f"{failed} part(s) failed.", flush=True)
    return 1 if failed else 0
