"""Draw a parsed page as SVG."""
from __future__ import annotations

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


def page_svg(rm_path: Path, **options) -> str:
    return svg(scene.load(rm_path), **options)


def blank_svg(background: bool = True) -> str:
    return svg(Page(), background=background)
