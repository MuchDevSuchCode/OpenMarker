"""Turn a drawing into real pen strokes on a native notebook page.

The picture is traced, not embedded: on the tablet the result is ordinary
fineliner ink that can be erased, selected and drawn over.

How the tracing works:

  * Solid dark areas are filled with parallel hatch strokes, like colouring in
    with a pen.
  * Lines are thinned to their centre and each becomes one stroke of matching
    thickness. Lines that are light in the picture are drawn with the grey pen.
  * Smoothly shaded areas with no lines of their own get fine grey hatching,
    closer together where the shading is darker.
"""
from __future__ import annotations

import io
import json
import math
import time
import uuid
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageOps
from rmscene import CrdtId, LwwValue, write_blocks
from rmscene import scene_items as si
from rmscene import scene_stream as ss
from rmscene.crdt_sequence import CrdtSequenceItem
from scipy import ndimage
from skimage.morphology import remove_small_objects, skeletonize

PAPER = (1620, 2160)  # page size in page units; x runs from -width/2
MARGIN = 70
WORK = 2.0  # working pixels per page unit while tracing
FILL_WIDTHS = (5.0, 2.4)  # hatch strokes used to fill solid areas, widest first (page units)
FILL_ANGLE = -52.0  # direction of the fill hatching, degrees (negative leans like "/")
LINE_WIDTHS = (0.9, 1.3, 1.8, 2.4, 3.0)  # thicknesses available for traced lines
LINE_PASSES = 2
MIN_LENGTH = 4.0  # traced lines shorter than this (page units) are dropped
SOLID_BELOW = 105  # grey level (0-255) under which an area counts as solid black
LINE_CONTRAST = 14  # how much darker than its surroundings a pixel must be to count as a line
GREY_ABOVE = 150  # lines lighter than this are drawn with the grey pen
SHADE_LEVELS = ((150, 5.0), (190, 9.0))  # (shading no lighter than this, hatch spacing in page units)
SIMPLIFY = 0.45  # how far (page units) a simplified stroke may stray from the traced line
# A fineliner stroke of thickness scale s stores point width 8*s and is drawn
# about 2*s page units wide (measured from strokes made on the tablet).
UNITS_PER_SCALE = 2.0


@dataclass
class InkStroke:
    points: np.ndarray  # (n, 2) page coordinates
    width: float  # page units
    grey: bool = False


# ---- tracing ---------------------------------------------------------------

_NEIGHBOURS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def _trace(skeleton: np.ndarray) -> list[np.ndarray]:
    """Split a one-pixel-wide skeleton into paths of (row, col) pixels.

    A path does not stop at a crossing: it carries on along the branch that
    bends least, the way a pen line runs through other lines.
    """
    todo = np.pad(skeleton, 1)
    degree = ndimage.convolve(todo.astype(np.uint8), np.ones((3, 3), np.uint8), mode="constant") - 1
    degree[~todo] = 0
    paths = []

    def walk(start: tuple[int, int]) -> list[tuple[int, int]]:
        path = [start]
        todo[start] = False
        r, c = start
        while True:
            options = [(r + dr, c + dc) for dr, dc in _NEIGHBOURS if todo[r + dr, c + dc]]
            if not options:
                return path
            if len(options) > 1 and len(path) > 1:
                back = path[max(0, len(path) - 6)]
                hr, hc = r - back[0], c - back[1]  # recent heading
                options.sort(key=lambda o: -((o[0] - r) * hr + (o[1] - c) * hc) / math.hypot(o[0] - r, o[1] - c))
            r, c = options[0]
            path.append((r, c))
            todo[r, c] = False

    # Start at line ends so strokes run end to end, then take what is left (loops).
    for wanted in (lambda d: d == 1, lambda d: d > 0):
        for start in zip(*np.nonzero(todo & wanted(degree))):
            if todo[start]:
                path = walk(start)
                if len(path) > 1:
                    paths.append(np.array(path) - 1)
    return paths


def _simplify(points: np.ndarray, tolerance: float) -> np.ndarray:
    """Ramer-Douglas-Peucker: drop points that lie within `tolerance` of the line."""
    if len(points) < 3:
        return points
    keep = np.zeros(len(points), bool)
    keep[[0, -1]] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        start, end = points[a], points[b]
        seg = end - start
        length = math.hypot(*seg)
        rel = points[a + 1 : b] - start
        if length == 0:
            dist = np.hypot(rel[:, 0], rel[:, 1])
        else:
            dist = np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / length
        worst = int(dist.argmax())
        if dist[worst] > tolerance:
            mid = a + 1 + worst
            keep[mid] = True
            stack += [(a, mid), (mid, b)]
    return points[keep]


def _hatch(allowed: np.ndarray, pitch: float, shortest: float) -> list[np.ndarray]:
    """Straight segments along parallel hatch lines, wherever `allowed` is true.

    `pitch` and `shortest` are in pixels. Each segment is a (2, 2) array of
    (row, col) end points.
    """
    if not allowed.any():
        return []
    rows, cols = allowed.shape
    angle = math.radians(FILL_ANGLE)
    along = (math.sin(angle), math.cos(angle))  # (row, col) step along a hatch line
    across = (along[1], -along[0])
    reach = int(math.hypot(rows, cols) / 2) + 2
    t = np.arange(-reach, reach + 1)
    k = np.arange(-reach, reach + 1, pitch)
    r = np.rint(rows / 2 + t[None, :] * along[0] + k[:, None] * across[0]).astype(int)
    c = np.rint(cols / 2 + t[None, :] * along[1] + k[:, None] * across[1]).astype(int)
    inside = (r >= 0) & (r < rows) & (c >= 0) & (c < cols)
    hit = np.zeros(r.shape, bool)
    hit[inside] = allowed[r[inside], c[inside]]
    edges = np.diff(np.pad(hit, ((0, 0), (1, 1))).astype(np.int8), axis=1)
    out = []
    for line, a in zip(*np.nonzero(edges == 1)):
        b = a + int(np.argmax(edges[line, a + 1 :] == -1))  # last sample of this run
        if b - a >= shortest:
            out.append(np.array([[r[line, a], c[line, a]], [r[line, b], c[line, b]]]))
    return out


def _fill(mask: np.ndarray, width: float) -> list[tuple[np.ndarray, float]]:
    """Hatch strokes of one width (pixels) that fill the solid parts of a mask.

    A stroke is laid wherever its whole width fits inside the mask, on
    parallel lines close enough together to leave no gaps.
    """
    fits = ndimage.distance_transform_edt(mask) >= width / 2
    return [(segment, width) for segment in _hatch(fits, width * 0.8, width * 1.2)]


def _lines(mask: np.ndarray, detail: float) -> list[tuple[np.ndarray, float]]:
    """Centre-line strokes for the thin parts of a mask, as (pixel path, width in pixels)."""
    thickness = ndimage.distance_transform_edt(mask) * 2 - 1  # full local width, pixels
    out = []
    for path in _trace(skeletonize(mask)):
        if len(path) < MIN_LENGTH * WORK / detail:
            continue
        # One stroke has one thickness. Take the thinner end of what the line
        # measures so strokes never spill outside the drawing; wider stretches
        # are picked up by the next pass.
        measured = np.percentile(thickness[path[:, 0], path[:, 1]], 35) / WORK
        width = min(LINE_WIDTHS, key=lambda step: abs(step - measured))
        out.append((path, width * WORK))
    return out


def trace(image: Image.Image, detail: float = 1.0, shading: bool = True) -> list[InkStroke]:
    """Pen strokes reproducing `image`, fitted to the page.

    `detail` below 1 gives fewer, coarser strokes. `shading` adds fine grey
    hatching where the picture is smoothly shaded.
    """
    grey_image = ImageOps.grayscale(image)
    box_w, box_h = PAPER[0] - 2 * MARGIN, PAPER[1] - 2 * MARGIN
    scale = min(box_w / grey_image.width, box_h / grey_image.height)  # image pixels -> page units
    size = (max(2, round(grey_image.width * scale * WORK)), max(2, round(grey_image.height * scale * WORK)))
    tone = np.asarray(grey_image.resize(size, Image.LANCZOS), dtype=np.float32)
    left = -size[0] / WORK / 2
    top = (PAPER[1] - size[1] / WORK) / 2

    # Ink is anything solidly dark, plus anything clearly darker than its
    # surroundings: that second test is what catches thin, light pencil lines.
    surroundings = ndimage.gaussian_filter(tone, 5 * WORK)
    ink = (tone < SOLID_BELOW) | ((tone < surroundings - LINE_CONTRAST) & (tone < 225))
    ink = remove_small_objects(ink, max_size=6)
    mask = ink.copy()
    strokes: list[InkStroke] = []

    def lay(found: list[tuple[np.ndarray, float]], simplify: bool) -> None:
        """Keep these strokes and take what they cover out of the mask."""
        nonlocal mask
        covered = Image.new("L", size, 0)
        draw = ImageDraw.Draw(covered)
        for path, width in found:
            xy = [(float(c), float(r)) for r, c in path]
            draw.line(xy, fill=255, width=max(1, round(width)), joint="curve")
            radius = width / 2
            for x, y in (xy[0], xy[-1]):
                draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)
            points = np.array([(c / WORK + left, r / WORK + top) for r, c in path])
            if simplify:
                points = _simplify(points, SIMPLIFY / detail)
            light = float(np.median(tone[path[:, 0], path[:, 1]])) > GREY_ABOVE
            strokes.append(InkStroke(points, width / WORK, grey=light))
        mask = mask & (np.asarray(covered) == 0)

    for width in FILL_WIDTHS:
        lay(_fill(mask, width * WORK), simplify=False)
    for _ in range(LINE_PASSES):
        mask = remove_small_objects(mask, max_size=round(12 / detail))
        found = _lines(mask, detail)
        if not found:
            break
        lay(found, simplify=True)

    if shading:
        # Smooth shading: grey areas that the line work has left mostly empty.
        smooth = ndimage.gaussian_filter(tone, 3 * WORK)
        busy = ndimage.gaussian_filter(ink.astype(np.float32), 4 * WORK) > 0.3
        darker = 0.0
        for level, spacing in SHADE_LEVELS:
            region = (smooth < level) & (smooth >= darker) & ~busy
            for segment in _hatch(region, spacing * WORK / detail, 6 * WORK):
                points = np.array([(c / WORK + left, r / WORK + top) for r, c in segment])
                strokes.append(InkStroke(points, LINE_WIDTHS[0], grey=True))
            darker = level
    return strokes


def preview(strokes: list[InkStroke], scale: float = 0.5) -> Image.Image:
    """The strokes drawn on a white page, for checking before sending."""
    page = Image.new("L", (round(PAPER[0] * scale), round(PAPER[1] * scale)), 255)
    big = Image.new("L", (page.width * 2, page.height * 2), 255)  # draw large, shrink for smooth edges
    draw = ImageDraw.Draw(big)
    k = scale * 2
    for grey in (True, False):  # black on top
        for stroke in strokes:
            if stroke.grey != grey:
                continue
            xy = [((x + PAPER[0] / 2) * k, y * k) for x, y in stroke.points]
            draw.line(xy, fill=140 if grey else 20, width=max(1, round(stroke.width * k)), joint="curve")
    return big.resize(page.size, Image.LANCZOS)


# ---- writing a notebook page ------------------------------------------------


def page_file(strokes: list[InkStroke]) -> bytes:
    """A `.rm` page (lines format v6) with the strokes on one layer."""
    layer = CrdtId(0, 11)

    def blocks():
        yield ss.AuthorIdsBlock(author_uuids={1: uuid.uuid4()})
        yield ss.MigrationInfoBlock(migration_id=CrdtId(1, 1), is_device=True)
        yield ss.PageInfoBlock(loads_count=1, merges_count=0, text_chars_count=0, text_lines_count=0)
        yield ss.SceneInfo(
            current_layer=LwwValue(CrdtId(0, 0), CrdtId(0, 0)),
            background_visible=LwwValue(CrdtId(0, 0), True),
            root_document_visible=LwwValue(CrdtId(0, 0), True),
            paper_size=PAPER,
        )
        yield ss.SceneTreeBlock(tree_id=layer, node_id=CrdtId(0, 0), is_update=True, parent_id=CrdtId(0, 1))
        yield ss.TreeNodeBlock(si.Group(node_id=CrdtId(0, 1)))
        yield ss.TreeNodeBlock(si.Group(node_id=layer, label=LwwValue(CrdtId(0, 12), "Layer 1")))
        yield ss.SceneGroupItemBlock(
            parent_id=CrdtId(0, 1),
            item=CrdtSequenceItem(CrdtId(0, 13), CrdtId(0, 0), CrdtId(0, 0), 0, layer),
        )
        previous = CrdtId(0, 0)
        for number, stroke in enumerate(strokes, 16):
            thickness = stroke.width / UNITS_PER_SCALE
            stored = max(1, round(thickness * 8))
            points = []
            for index, (x, y) in enumerate(stroke.points):
                nx, ny = stroke.points[min(index + 1, len(stroke.points) - 1)]
                px, py = stroke.points[max(index - 1, 0)]
                heading = math.atan2(ny - py, nx - px) % (2 * math.pi)
                points.append(
                    si.Point(
                        x=float(x), y=float(y), speed=4, direction=round(heading / (2 * math.pi) * 255),
                        width=stored, pressure=170,
                    )
                )
            line = si.Line(
                color=si.PenColor.GRAY if stroke.grey else si.PenColor.BLACK,
                tool=si.Pen.FINELINER_2,
                points=points,
                thickness_scale=thickness,
                starting_length=0.0,
            )
            item_id = CrdtId(1, number)
            yield ss.SceneLineItemBlock(
                parent_id=layer, item=CrdtSequenceItem(item_id, previous, CrdtId(0, 0), 0, line)
            )
            previous = item_id

    buffer = io.BytesIO()
    write_blocks(buffer, blocks())
    return buffer.getvalue()


def notebook_files(name: str, strokes: list[InkStroke], parent: str = "") -> tuple[str, dict[str, bytes]]:
    """All the files of a new one-page notebook holding the strokes.

    Returns (document id, files keyed by store-relative path).
    """
    doc, page = str(uuid.uuid4()), str(uuid.uuid4())
    now = str(int(time.time() * 1000))
    content = {
        "cPages": {
            "lastOpened": {"timestamp": "0:0", "value": ""},
            "original": {"timestamp": "0:0", "value": -1},
            "pages": [
                {
                    "id": page,
                    "idx": {"timestamp": "1:2", "value": "ba"},
                    "modifed": now,
                    "template": {"timestamp": "1:1", "value": "Blank"},
                }
            ],
            "uuids": [{"first": str(uuid.uuid4()), "second": 1}],
        },
        "coverPageNumber": -1,
        "documentMetadata": {},
        "extraMetadata": {},
        "fileType": "notebook",
        "fontName": "",
        "formatVersion": 2,
        "lineHeight": -1,
        "orientation": "portrait",
        "pageCount": 1,
        "pageTags": [],
        "sizeInBytes": "0",
        "tags": [],
        "textAlignment": "justify",
        "textScale": 1,
        "zoomMode": "bestFit",
    }
    metadata = {
        "createdTime": now,
        "lastModified": now,
        "lastOpened": "0",
        "lastOpenedPage": 0,
        "new": False,
        "parent": parent,
        "pinned": False,
        "source": "",
        "type": "DocumentType",
        "visibleName": name,
    }
    return doc, {
        f"{doc}.content": json.dumps(content, indent=4).encode("utf-8"),
        f"{doc}.metadata": json.dumps(metadata, indent=4).encode("utf-8"),
        f"{doc}/{page}.rm": page_file(strokes),
    }
