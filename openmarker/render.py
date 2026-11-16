"""Render a page's `.rm` stroke file (lines format v6) to SVG.

Handwriting is drawn at its stored coordinates. Typed text, and strokes
anchored to typed text, are laid out approximately: the tablet's exact text
metrics are not known, so those can sit a little off.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from html import escape
from pathlib import Path

from rmscene import read_tree
from rmscene import scene_items as si
from rmscene.text import TextDocument

# rmscene logs every block it does not understand; newer firmware writes a few.
logging.getLogger("rmscene").setLevel(logging.ERROR)

PAGE_LEFT, PAGE_TOP, PAGE_WIDTH, PAGE_HEIGHT = -702.0, 0.0, 1404.0, 1872.0

COLORS = {
    "BLACK": "#1b1b1b",
    "GRAY": "#8f8f8f",
    "WHITE": "#ffffff",
    "YELLOW": "#f5dd4b",
    "GREEN": "#4fa85a",
    "PINK": "#f08fb0",
    "BLUE": "#3567c4",
    "RED": "#cf3a32",
    "GRAY_OVERLAP": "#7d7d7d",
    "HIGHLIGHT": "#f5dd4b",
    "GREEN_2": "#4b9c68",
    "CYAN": "#2f9fc4",
    "MAGENTA": "#bf3d94",
    "YELLOW_2": "#e3bb34",
}

# (width multiplier, opacity) per tool family; widths are stored in quarter units.
TOOL_STYLE = {
    "HIGHLIGHTER": (0.25, 0.35),
    "SHADER": (0.25, 0.25),
    "PENCIL": (0.25, 0.75),
    "MECHANICAL_PENCIL": (0.25, 0.8),
    "MARKER": (0.25, 1.0),
    "PAINTBRUSH": (0.25, 0.9),
}
DEFAULT_STYLE = (0.25, 1.0)

# Approximate text metrics: style value -> (font size, line height).
TEXT_STYLE_DEFAULT = (30.0, 52.0)
TEXT_STYLES = {"HEADING": (56.0, 96.0), "BOLD": (30.0, 52.0)}
TEXT_TOP_OFFSET = -40.0
# Anchor ids the tablet uses for "top of text" and "after the last line".
ANCHOR_TOP, ANCHOR_END = 281474976710654, 281474976710655


@dataclass
class _Bounds:
    left: float = PAGE_LEFT
    top: float = PAGE_TOP
    right: float = PAGE_LEFT + PAGE_WIDTH
    bottom: float = PAGE_TOP + PAGE_HEIGHT

    def include(self, x: float, y: float, pad: float = 24.0) -> None:
        self.left = min(self.left, x - pad)
        self.right = max(self.right, x + pad)
        self.top = min(self.top, y - pad)
        self.bottom = max(self.bottom, y + pad)


def _tool_family(tool: si.Pen) -> str:
    return tool.name.rsplit("_", 1)[0] if tool.name[-1].isdigit() else tool.name


def _color(line: si.Line) -> str:
    rgba = getattr(line, "color_rgba", None)
    if rgba is not None:
        if isinstance(rgba, int):  # 0xAARRGGBB
            return f"#{rgba & 0xFFFFFF:06x}"
        r, g, b = rgba[:3]
        return f"#{r:02x}{g:02x}{b:02x}"
    return COLORS.get(line.color.name, COLORS["BLACK"])


def _stroke(line: si.Line, dx: float, dy: float, bounds: _Bounds) -> str:
    family = _tool_family(line.tool)
    if family.startswith("ERASER") or not line.points:
        return ""
    scale, opacity = TOOL_STYLE.get(family, DEFAULT_STYLE)
    width = max(0.8, scale * sum(p.width for p in line.points) / len(line.points))
    coords = []
    for p in line.points:
        x, y = p.x + dx, p.y + dy
        bounds.include(x, y)
        coords.append(f"{x:.1f},{y:.1f}")
    if len(coords) == 1:
        coords.append(coords[0])
    cap = "butt" if family in ("HIGHLIGHTER", "SHADER") else "round"
    return (
        f'<polyline points="{" ".join(coords)}" stroke="{_color(line)}" '
        f'stroke-width="{width:.2f}" stroke-opacity="{opacity}" stroke-linecap="{cap}"/>'
    )


def _layout_text(text: si.Text | None, bounds: _Bounds) -> tuple[list[str], dict]:
    """Returns SVG text elements and a map of anchor id -> y position."""
    out: list[str] = []
    anchors: dict = {}
    if text is None:
        return out, anchors
    y = text.pos_y + TEXT_TOP_OFFSET
    top = y
    for para in TextDocument.from_scene_item(text).contents:
        size, line_height = TEXT_STYLES.get(para.style.value.name, TEXT_STYLE_DEFAULT)
        weight = "600" if para.style.value.name in ("HEADING", "BOLD") else "400"
        anchors[para.start_id] = y
        for piece in para.contents:
            for char_id in piece.i:
                anchors[char_id] = y
        per_line = max(8, int(text.width / (size * 0.5)))
        for raw in str(para).replace(" ", "\n").split("\n"):
            words, current = raw.split(" "), ""
            lines = []
            for word in words:
                if current and len(current) + 1 + len(word) > per_line:
                    lines.append(current)
                    current = word
                else:
                    current = f"{current} {word}" if current else word
            lines.append(current)
            for content in lines:
                y += line_height
                if content.strip():
                    bounds.include(text.pos_x, y)
                    out.append(
                        f'<text x="{text.pos_x:.1f}" y="{y:.1f}" font-size="{size}" '
                        f'font-weight="{weight}" xml:space="preserve">{escape(content)}</text>'
                    )
    anchors["top"], anchors["end"] = top, y
    return out, anchors


def _walk(group: si.Group, dx: float, dy: float, anchors: dict, bounds: _Bounds, out: list[str]) -> None:
    for child_id in group.children:
        child = group.children[child_id]
        if isinstance(child, si.Line):
            svg = _stroke(child, dx, dy, bounds)
            if svg:
                out.append(svg)
        elif isinstance(child, si.Group):
            cx, cy = dx, dy
            anchor = child.anchor_id.value if child.anchor_id else None
            if anchor is not None:
                if anchor.part1 == 0 and anchor.part2 == ANCHOR_TOP:
                    base = anchors.get("top", 0.0)
                elif anchor.part1 == 0 and anchor.part2 == ANCHOR_END:
                    base = anchors.get("end", 0.0)
                else:
                    base = anchors.get(anchor, anchors.get("top", 0.0))
                cy = dy + base
                if child.anchor_origin_x and child.anchor_origin_x.value is not None:
                    cx = dx + child.anchor_origin_x.value
            _walk(child, cx, cy, anchors, bounds, out)


def page_svg(rm_path: Path) -> str:
    with open(rm_path, "rb") as f:
        tree = read_tree(f)
    bounds = _Bounds()
    text_svg, anchors = _layout_text(tree.root_text, bounds)
    strokes: list[str] = []
    _walk(tree.root, 0.0, 0.0, anchors, bounds, strokes)
    w, h = bounds.right - bounds.left, bounds.bottom - bounds.top
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{bounds.left:.1f} {bounds.top:.1f} {w:.1f} {h:.1f}" '
        f'width="{w:.0f}" height="{h:.0f}">'
        f'<rect x="{bounds.left:.1f}" y="{bounds.top:.1f}" width="{w:.1f}" height="{h:.1f}" fill="#fdfcf9"/>'
        f'<g fill="#1b1b1b" font-family="Georgia, \'Times New Roman\', serif">{"".join(text_svg)}</g>'
        f'<g fill="none" stroke-linejoin="round">{"".join(strokes)}</g>'
        "</svg>"
    )


def blank_svg() -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {PAGE_WIDTH:.0f} {PAGE_HEIGHT:.0f}">'
        f'<rect width="{PAGE_WIDTH:.0f}" height="{PAGE_HEIGHT:.0f}" fill="#fdfcf9"/></svg>'
    )
