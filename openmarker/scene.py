"""Parse a page's `.rm` stroke file (lines format v6) into plain drawing data.

Every output (SVG, PDF, handwriting recognition) works from this model.
Handwriting keeps its stored coordinates. Typed text, and strokes anchored
to typed text, are laid out approximately: the tablet's exact text metrics
are not known, so those can sit a little off.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from rmscene import read_tree
from rmscene import scene_items as si
from rmscene.text import TextDocument

# rmscene logs every block it does not understand; newer firmware writes a few.
logging.getLogger("rmscene").setLevel(logging.ERROR)

PAGE_LEFT, PAGE_TOP, PAGE_WIDTH, PAGE_HEIGHT = -702.0, 0.0, 1404.0, 1872.0
PAPER = "#fdfcf9"

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

# Per tool family: opacity, whether width follows pen pressure, square ends.
# Stored point widths are in quarter units.
TOOLS = {
    "BALLPOINT": (1.0, True, False),
    "FINELINER": (1.0, False, False),
    "MARKER": (1.0, True, False),
    "PAINTBRUSH": (0.92, True, False),
    "CALIGRAPHY": (1.0, True, False),
    "PENCIL": (0.72, True, False),
    "MECHANICAL_PENCIL": (0.8, False, False),
    "HIGHLIGHTER": (0.35, False, True),
    "SHADER": (0.25, False, True),
}
WIDTH_SCALE = 0.25

# Approximate text metrics: style name -> (font size, line height).
TEXT_STYLE_DEFAULT = (30.0, 52.0)
TEXT_STYLES = {"HEADING": (56.0, 96.0), "BOLD": (30.0, 52.0)}
TEXT_TOP_OFFSET = -40.0
# Anchor ids the tablet uses for "top of text" and "after the last line".
ANCHOR_TOP, ANCHOR_END = 281474976710654, 281474976710655


@dataclass
class Stroke:
    points: list[tuple[float, float, float]]  # x, y, width
    color: str
    opacity: float
    pressure: bool  # width varies along the stroke
    square: bool  # square ends (highlighters)
    tool: str

    @property
    def mean_width(self) -> float:
        return sum(p[2] for p in self.points) / len(self.points)

    @property
    def is_writing(self) -> bool:
        """Pen strokes that could be handwriting, as opposed to highlighting."""
        return not self.square


@dataclass
class TextLine:
    x: float
    y: float
    size: float
    bold: bool
    content: str


@dataclass
class Page:
    strokes: list[Stroke] = field(default_factory=list)
    text: list[TextLine] = field(default_factory=list)
    left: float = PAGE_LEFT
    top: float = PAGE_TOP
    right: float = PAGE_LEFT + PAGE_WIDTH
    bottom: float = PAGE_TOP + PAGE_HEIGHT

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    def include(self, x: float, y: float, pad: float = 24.0) -> None:
        self.left = min(self.left, x - pad)
        self.right = max(self.right, x + pad)
        self.top = min(self.top, y - pad)
        self.bottom = max(self.bottom, y + pad)

    @property
    def typed_text(self) -> str:
        return "\n".join(line.content for line in self.text)


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


def _stroke(line: si.Line, dx: float, dy: float, page: Page) -> Stroke | None:
    family = _tool_family(line.tool)
    if family.startswith("ERASER") or not line.points:
        return None
    opacity, pressure, square = TOOLS.get(family, (1.0, False, False))
    points = []
    for p in line.points:
        x, y = p.x + dx, p.y + dy
        page.include(x, y)
        points.append((x, y, max(0.8, p.width * WIDTH_SCALE)))
    return Stroke(points, _color(line), opacity, pressure, square, family)


def _layout_text(text: si.Text | None, page: Page) -> dict:
    """Places typed text on the page; returns a map of anchor id -> y position."""
    anchors: dict = {}
    if text is None:
        return anchors
    y = text.pos_y + TEXT_TOP_OFFSET
    top = y
    for para in TextDocument.from_scene_item(text).contents:
        style = para.style.value.name
        size, line_height = TEXT_STYLES.get(style, TEXT_STYLE_DEFAULT)
        anchors[para.start_id] = y
        for piece in para.contents:
            for char_id in piece.i:
                anchors[char_id] = y
        per_line = max(8, int(text.width / (size * 0.5)))
        for raw in str(para).replace(" ", "\n").split("\n"):
            lines, current = [], ""
            for word in raw.split(" "):
                if current and len(current) + 1 + len(word) > per_line:
                    lines.append(current)
                    current = word
                else:
                    current = f"{current} {word}" if current else word
            lines.append(current)
            for content in lines:
                y += line_height
                if content.strip():
                    page.include(text.pos_x, y)
                    page.text.append(TextLine(text.pos_x, y, size, style in ("HEADING", "BOLD"), content))
    anchors["top"], anchors["end"] = top, y
    return anchors


def _walk(group: si.Group, dx: float, dy: float, anchors: dict, page: Page) -> None:
    for child_id in group.children:
        child = group.children[child_id]
        if isinstance(child, si.Line):
            stroke = _stroke(child, dx, dy, page)
            if stroke:
                page.strokes.append(stroke)
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
            _walk(child, cx, cy, anchors, page)


def read(stream: BinaryIO) -> Page:
    tree = read_tree(stream)
    page = Page()
    anchors = _layout_text(tree.root_text, page)
    _walk(tree.root, 0.0, 0.0, anchors, page)
    return page


def load(rm_path: Path) -> Page:
    with open(rm_path, "rb") as f:
        return read(f)
