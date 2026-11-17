"""Export documents: PDF (ink laid over the original pages) and Markdown."""
from __future__ import annotations

import io
import re
from pathlib import Path

from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import RectangleObject
from reportlab.lib.colors import HexColor
from reportlab.pdfgen.canvas import Canvas

from . import library, ocr, render, scene

NOTEBOOK_SCALE = 0.4  # page units -> PDF points for notebook pages (1404 wide -> 562pt)


def _draw(canvas: Canvas, page: scene.Page, scale: float, x0: float, y_top: float) -> None:
    """Draw a page's text and ink. Page point (x, y) lands at (x0 + x*scale, y_top - y*scale)."""
    for line in page.text:
        canvas.setFillColor(HexColor("#1b1b1b"))
        canvas.setFont("Times-Bold" if line.bold else "Times-Roman", line.size * scale)
        canvas.drawString(x0 + line.x * scale, y_top - line.y * scale, line.content)
    canvas.setLineJoin(1)
    for stroke in page.strokes:
        canvas.setStrokeColor(HexColor(stroke.color), alpha=stroke.opacity)
        canvas.setLineCap(2 if stroke.square else 1)
        points = [(x0 + x * scale, y_top - y * scale, w * scale) for x, y, w in stroke.points]
        if len(points) == 1:
            points.append(points[0])
        if stroke.pressure and stroke.opacity == 1:
            for (xa, ya, wa), (xb, yb, wb) in zip(points, points[1:]):
                canvas.setLineWidth((wa + wb) / 2)
                canvas.line(xa, ya, xb, yb)
        else:
            canvas.setLineWidth(stroke.mean_width * scale)
            path = canvas.beginPath()
            path.moveTo(points[0][0], points[0][1])
            for x, y, _ in points[1:]:
                path.lineTo(x, y)
            canvas.drawPath(path, stroke=1, fill=0)


def _ink_layer(page: scene.Page, width: float, height: float, scale: float, x0: float, y_top: float) -> PdfReader:
    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=(width, height))
    _draw(canvas, page, scale, x0, y_top)
    canvas.save()
    buffer.seek(0)
    return PdfReader(buffer)


def source_fit(pdf_width: float, pdf_height: float, paper_height: float = scene.PAGE_HEIGHT) -> tuple[float, float]:
    """How the tablet fits an original page into its page area.

    Returns (scale from page units to PDF points, x position of page-unit 0).
    The original page is fitted to the page area's height and centred.
    """
    scale = pdf_height / paper_height
    return scale, pdf_width / 2


def pdf(item: library.Item) -> bytes:
    store = item.store
    source_path = store / f"{item.id}.pdf"
    source = PdfReader(source_path) if source_path.exists() else None
    writer = PdfWriter()
    for page_id in item.page_ids:
        rm = item.page_file(page_id)
        page = scene.load(rm) if rm.exists() else scene.Page()
        number = item.source_pages.get(page_id)
        if source is not None and number is not None and number < len(source.pages):
            original = source.pages[number]
            width, height = float(original.mediabox.width), float(original.mediabox.height)
            scale, x0 = source_fit(width, height, page.paper[1])
            out = writer.add_page(original)
            if page.strokes or page.text:
                layer = _ink_layer(page, width, height, scale, x0, height).pages[0]
                left, bottom = float(original.mediabox.left), float(original.mediabox.bottom)
                out.merge_transformed_page(layer, Transformation().translate(left, bottom))
        else:
            width, height = page.width * NOTEBOOK_SCALE, page.height * NOTEBOOK_SCALE
            layer = _ink_layer(page, width, height, NOTEBOOK_SCALE, -page.left * NOTEBOOK_SCALE, height + page.top * NOTEBOOK_SCALE)
            out = writer.add_page(layer.pages[0])
            out.mediabox = RectangleObject((0, 0, width, height))
    writer.add_metadata({"/Title": item.name, "/Creator": "OpenMarker"})
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def text(item: library.Item, markdown_headings: bool = False) -> str:
    """The whole document as plain text, one section per page that has any."""
    parts = []
    for number, page in enumerate(ocr.document_text(item), 1):
        if page.strip():
            heading = f"## Page {number}" if markdown_headings else f"--- Page {number} ---"
            parts.append(f"{heading}\n\n{page.strip()}")
    title = f"# {item.name}\n\n" if markdown_headings else ""
    return title + "\n\n".join(parts) + "\n"


def safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "untitled"


def markdown(item: library.Item, items: dict[str, library.Item], target: Path) -> Path:
    """Write `<folders>/<name>.md` plus one SVG per page under `target`."""
    relative = Path(*[safe_name(part) for part in library.path_of(item, items).split("/")])
    note = (target / relative).with_suffix(".md")
    assets = note.parent / f"{note.stem}.assets"
    assets.mkdir(parents=True, exist_ok=True)
    lines = [f"# {item.name}", ""]
    if item.tags:
        lines += [" ".join(f"#{tag.replace(' ', '-')}" for tag in item.tags), ""]
    if item.modified:
        lines += [f"*Last edited {item.modified:%Y-%m-%d %H:%M} UTC*", ""]
    for number, page_id in enumerate(item.page_ids, 1):
        rm = item.page_file(page_id)
        if not rm.exists():
            continue
        (assets / f"page-{number}.svg").write_text(render.page_svg(rm), encoding="utf-8")
        lines += [f"## Page {number}", "", f"![Page {number}]({assets.name}/page-{number}.svg)", ""]
        text = ocr.stored_text(item.id, page_id)
        if text:
            lines += [text.replace("\n", "  \n"), ""]
    note.write_text("\n".join(lines), encoding="utf-8")
    return note
