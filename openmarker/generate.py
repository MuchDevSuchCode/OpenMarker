"""Make pages to send to the tablet: a daily agenda, or a web page as PDF."""
from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import urllib.request
from datetime import date, datetime, time
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.pdfgen.canvas import Canvas

# The tablet's own PDFs use this page size, so pages fill the screen exactly.
PAGE = (1620.0, 2160.0)
INK, FAINT, RULE = HexColor("#1b1b1b"), HexColor("#6f6a60"), HexColor("#c9c3b4")


def calendar_events(source: str, day: date) -> list[tuple[str, str, str]]:
    """(time range, title, location) for one day, from an .ics file path or URL."""
    import icalendar
    import recurring_ical_events

    if source.lower().startswith(("http://", "https://", "webcal://")):
        url = source.replace("webcal://", "https://", 1)
        request = urllib.request.Request(url, headers={"User-Agent": "openmarker"})
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    else:
        raw = Path(source).read_bytes()
    calendar = icalendar.Calendar.from_ical(raw)
    events = []
    for event in recurring_ical_events.of(calendar).at(day):
        start, end = event["DTSTART"].dt, event.get("DTEND")
        title = str(event.get("SUMMARY", "(no title)"))
        location = str(event.get("LOCATION", "") or "")
        if isinstance(start, datetime):
            start = start.astimezone() if start.tzinfo else start
            finish = end.dt if end else start
            finish = finish.astimezone() if getattr(finish, "tzinfo", None) else finish
            when, key = f"{start:%H:%M} – {finish:%H:%M}", start.time()
        else:
            when, key = "All day", time.min
        events.append((key, when, title, location))
    return [(when, title, location) for _, when, title, location in sorted(events)]


def agenda_pdf(day: date, events: list[tuple[str, str, str]] | None) -> bytes:
    """One page: the day's events (if a calendar was given), then ruled space for notes."""
    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=PAGE)
    width, height = PAGE
    left, right, y = 130.0, width - 130.0, height - 190.0
    canvas.setTitle(f"Agenda {day.isoformat()}")
    canvas.setFillColor(INK)
    canvas.setFont("Times-Bold", 84)
    canvas.drawString(left, y, day.strftime("%A"))
    canvas.setFont("Times-Roman", 44)
    canvas.setFillColor(FAINT)
    canvas.drawString(left, y - 66, day.strftime("%d %B %Y").lstrip("0"))
    y -= 150
    canvas.setStrokeColor(INK)
    canvas.setLineWidth(3)
    canvas.line(left, y, right, y)
    y -= 78
    if events is not None:
        if not events:
            canvas.setFont("Times-Italic", 38)
            canvas.setFillColor(FAINT)
            canvas.drawString(left, y, "Nothing scheduled.")
            y -= 70
        for when, title, location in events[:14]:
            canvas.setFillColor(FAINT)
            canvas.setFont("Helvetica", 30)
            canvas.drawString(left, y, when)
            canvas.setFillColor(INK)
            canvas.setFont("Helvetica-Bold", 36)
            canvas.drawString(left + 290, y, title[:52])
            if location:
                canvas.setFillColor(FAINT)
                canvas.setFont("Helvetica", 26)
                canvas.drawString(left + 290, y - 36, location[:70])
                y -= 30
            # a box to tick
            canvas.setStrokeColor(FAINT)
            canvas.setLineWidth(2)
            canvas.rect(right - 34, y - 4, 34, 34)
            y -= 74
        if len(events) > 14:
            canvas.setFillColor(FAINT)
            canvas.setFont("Helvetica", 26)
            canvas.drawString(left, y, f"…and {len(events) - 14} more")
            y -= 60
        y -= 20
    canvas.setFillColor(FAINT)
    canvas.setFont("Helvetica-Bold", 26)
    canvas.drawString(left, y, "NOTES")
    y -= 64
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(1.5)
    while y > 120:
        canvas.line(left, y, right, y)
        y -= 64
    canvas.save()
    return buffer.getvalue()


def _browser() -> str | None:
    candidates = [
        shutil.which("msedge"),
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    return next((c for c in candidates if c and Path(c).exists()), None)


def web_page_pdf(url: str) -> tuple[str, bytes]:
    """Print a web page to PDF with an installed Chromium-based browser.

    Returns (page title, PDF bytes). The page is printed as the browser lays
    it out for print; it is not reduced to a reading view.
    """
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("only http and https addresses can be fetched")
    browser = _browser()
    if browser is None:
        raise RuntimeError("no Edge or Chrome installation found to print the page")
    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / "page.pdf"
        subprocess.run(
            [
                browser,
                "--headless",
                "--disable-gpu",
                "--no-pdf-header-footer",
                f"--user-data-dir={Path(folder) / 'profile'}",
                f"--print-to-pdf={target}",
                url,
            ],
            check=False,
            timeout=120,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not target.exists():
            raise RuntimeError("the browser did not produce a PDF for that address")
        data = target.read_bytes()
    from pypdf import PdfReader

    title = (PdfReader(io.BytesIO(data)).metadata or {}).get("/Title") or url.split("//", 1)[1].split("?")[0].strip("/")
    return str(title)[:120], data

