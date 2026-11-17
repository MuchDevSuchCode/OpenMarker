"""Draw a parsed page as SVG."""
from __future__ import annotations

import io
import threading
from functools import lru_cache
from html import escape
from pathlib import Path

from . import scene
from .scene import Page, Stroke

WIDTH_STEP = 0.5  # pressure strokes are split where the width changes by this much


def _polyline(points: list[tuple[float, float, float]], width: float) -> str:
    coords = [f"{x:.1f},{y:.1f}" for x, y, _ in points]
    if len(coords) == 1:
        coords.append(coords[0])
    return f'<polyline points="{" ".join(coords)}" stroke-width="{width:.2f}"/>'


def _stroke(stroke: Stroke) -> str:
    cap = "butt" if stroke.square else "round"
    if not stroke.pressure:
        body = _polyline(stroke.points, stroke.mean_width)
    else:
        # Runs of similar width become one polyline each; runs share an end
        # point so the round caps hide the joins.
        parts, run = [], [stroke.points[0]]
        level = round(stroke.points[0][2] / WIDTH_STEP)
        for point in stroke.points[1:]:
            run.append(point)
            if round(point[2] / WIDTH_STEP) != level:
                parts.append(_polyline(run, level * WIDTH_STEP or WIDTH_STEP))
                run, level = [point], round(point[2] / WIDTH_STEP)
        if len(run) > 1 or not parts:
            parts.append(_polyline(run, level * WIDTH_STEP or WIDTH_STEP))
        body = "".join(parts)
    opacity = f' opacity="{stroke.opacity}"' if stroke.opacity < 1 else ""
    return f'<g stroke="{stroke.color}" stroke-linecap="{cap}"{opacity}>{body}</g>'


def svg(page: Page, background: bool = True, highlights: list[tuple[float, float, float, float]] = ()) -> str:
    """`highlights` are (x, y, width, height) boxes drawn behind the ink."""
    box = f'x="{page.left:.1f}" y="{page.top:.1f}" width="{page.width:.1f}" height="{page.height:.1f}"'
    text = "".join(
        f'<text x="{t.x:.1f}" y="{t.y:.1f}" font-size="{t.size}" '
        f'font-weight="{600 if t.bold else 400}" xml:space="preserve">{escape(t.content)}</text>'
        for t in page.text
    )
    marks = "".join(
        f'<rect x="{x - 6:.1f}" y="{y - 6:.1f}" width="{w + 12:.1f}" height="{h + 12:.1f}" rx="8" fill="#ffd84a" fill-opacity=".55"/>'
        for x, y, w, h in highlights
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{page.left:.1f} {page.top:.1f} {page.width:.1f} {page.height:.1f}" '
        f'width="{page.width:.0f}" height="{page.height:.0f}">'
        + (f'<rect {box} fill="{scene.PAPER}"/>' if background else "")
        + marks
        + f'<g fill="#1b1b1b" font-family="Georgia, \'Times New Roman\', serif">{text}</g>'
        f'<g fill="none" stroke-linejoin="round">{"".join(_stroke(s) for s in page.strokes)}</g>'
        "</svg>"
    )


def png(page: Page, backdrop: bytes | None = None, width: int = 1240) -> bytes:
    """The page as a PNG picture, `width` pixels across: ink on white, or over `backdrop` (a PNG of the original page)."""
    from PIL import Image, ImageColor, ImageDraw, ImageFont

    k = 2 * width / page.width  # drawn at twice the size, then shrunk for smooth lines
    size = (round(page.width * k), round(page.height * k))
    if backdrop:
        canvas = Image.open(io.BytesIO(backdrop)).convert("RGB").resize(size, Image.LANCZOS)
    else:
        canvas = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(canvas)
    for line in page.text:
        font = ImageFont.load_default(size=max(8, line.size * k))
        draw.text(((line.x - page.left) * k, (line.y - page.top) * k), line.content, font=font, fill="#1b1b1b", anchor="ls")
    for stroke in page.strokes:
        points = [((x - page.left) * k, (y - page.top) * k) for x, y, _ in stroke.points]
        if len(points) == 1:
            points.append(points[0])
        thickness = max(1, round(stroke.mean_width * k))
        if stroke.opacity < 1:  # highlighters: laid over what is beneath
            layer = Image.new("RGBA", size, (0, 0, 0, 0))
            colour = ImageColor.getrgb(stroke.color) + (round(255 * stroke.opacity),)
            ImageDraw.Draw(layer).line(points, fill=colour, width=thickness)
            canvas.paste(layer, mask=layer)
        else:
            draw.line(points, fill=stroke.color, width=thickness, joint="curve")
    canvas = canvas.resize((width, round(width * page.height / page.width)), Image.LANCZOS)
    buffer = io.BytesIO()
    canvas.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


# The PDF renderer is not safe to use from two threads at once: pages asked
# for at the same moment would crash the whole program.
_pdf_lock = threading.Lock()


@lru_cache(maxsize=64)
def pdf_page_png(path: str, mtime: float, number: int) -> bytes:
    """One page of a PDF file as a PNG picture. `mtime` only keeps the cache honest."""
    import pypdfium2

    with _pdf_lock:
        document = pypdfium2.PdfDocument(path)
        try:
            page = document[number]
            width, _ = page.get_size()
            image = page.render(scale=min(2.0, 1400 / width)).to_pil()
        finally:
            document.close()
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


@lru_cache(maxsize=64)
def pdf_page_size(path: str, mtime: float, number: int) -> tuple[float, float]:
    import pypdfium2

    with _pdf_lock:
        document = pypdfium2.PdfDocument(path)
        try:
            return document[number].get_size()
        finally:
            document.close()


def page_svg(rm_path: Path, **options) -> str:
    return svg(scene.load(rm_path), **options)


def blank_svg(background: bool = True) -> str:
    return svg(Page(), background=background)
