"""Prepare a picture for the tablet: put it on a clean white page as a PDF.

E-ink shows anything that is not pure white as visible grey or dither, and
shows transparency as black. So the background is turned white first:

    cutout  a plain backdrop is removed, keeping the subject (logos, product
            shots, drawings on a flat colour). Works from the picture's edges
            inwards; it does not recognise objects.
    paper   a photo of paper or a whiteboard is flattened: uneven lighting and
            shadows are evened out and the sheet becomes white.
    keep    the picture is used as it is.
    auto    transparency is filled with white; otherwise `cutout` if the edges
            are one flat colour, else `paper`.
"""
from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

MODES = ("auto", "cutout", "paper", "keep")
SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")
PAGE_SHORT, PAGE_LONG = 1620, 2160  # the tablet's page, in pixels and PDF points
MARGIN = 90
DEFAULT_TOLERANCE = 40  # colour distance (0-441) still counted as backdrop


def _open(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image)  # honour the camera's rotation flag
    image.load()
    return image


def _has_transparency(image: Image.Image) -> bool:
    if image.mode not in ("RGBA", "LA") and "transparency" not in image.info:
        return False
    alpha = np.asarray(image.convert("RGBA"))[..., 3]
    return bool((alpha < 250).mean() > 0.005)


def _on_white(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    page = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    return Image.alpha_composite(page, rgba).convert("RGB")


def _edge_pixels(pixels: np.ndarray) -> np.ndarray:
    return np.concatenate([pixels[0], pixels[-1], pixels[:, 0], pixels[:, -1]])


def _flat_edges(image: Image.Image) -> bool:
    edge = _edge_pixels(np.asarray(image, dtype=np.float32))
    return float(np.abs(edge - np.median(edge, axis=0)).mean()) < 9.0


def cutout(image: Image.Image, tolerance: float = DEFAULT_TOLERANCE) -> Image.Image:
    """Whiten the backdrop: everything close to the edge colour that is connected to an edge."""
    pixels = np.asarray(image, dtype=np.float32)
    backdrop = np.median(_edge_pixels(pixels), axis=0)
    distance = np.sqrt(((pixels - backdrop) ** 2).sum(axis=2))
    near = Image.fromarray(np.where(distance < tolerance, 255, 0).astype(np.uint8))
    # A one-pixel frame joins all four edges, so one fill from the corner
    # reaches every backdrop region that touches an edge and nothing enclosed.
    framed = ImageOps.expand(near, border=1, fill=255)
    ImageDraw.floodfill(framed, (0, 0), 128)
    reached = np.asarray(framed)[1:-1, 1:-1] == 128
    # Soften the boundary so edges fade to white instead of leaving a halo.
    mask = Image.fromarray((reached * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(3))
    blend = np.asarray(mask.filter(ImageFilter.GaussianBlur(1.2)), dtype=np.float32)[..., None] / 255
    return Image.fromarray((pixels * (1 - blend) + 255 * blend).astype(np.uint8))


def paper(image: Image.Image) -> Image.Image:
    """Flatten a photo of paper: divide out the lighting, then lift the sheet to white."""
    small_size = (max(1, image.width // 8), max(1, image.height // 8))
    # The sheet itself is the brightest thing nearby: a local maximum, smoothed,
    # is an estimate of how brightly each part of the sheet is lit.
    light = image.resize(small_size, Image.BILINEAR).filter(ImageFilter.MaxFilter(9))
    light = light.filter(ImageFilter.GaussianBlur(6)).resize(image.size, Image.BILINEAR)
    pixels = np.asarray(image, dtype=np.float32)
    lit = np.maximum(np.asarray(light, dtype=np.float32), 1.0)
    flat = np.clip(pixels / lit, 0, 1)
    # Stretch so near-white becomes white and the darkest marks stay dark.
    low, high = 0.35, 0.9
    flat = np.clip((flat - low) / (high - low), 0, 1)
    return Image.fromarray((flat * 255).astype(np.uint8))


def clean(data: bytes, mode: str = "auto", tolerance: float = DEFAULT_TOLERANCE) -> tuple[Image.Image, str]:
    """The picture with a white background, and the mode that was applied."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; use one of {', '.join(MODES)}")
    image = _open(data)
    transparent = _has_transparency(image)
    rgb = _on_white(image)
    if mode == "auto":
        mode = "keep" if transparent else "cutout" if _flat_edges(rgb) else "paper"
    if mode == "cutout":
        rgb = cutout(rgb, tolerance)
    elif mode == "paper":
        rgb = paper(rgb)
    return rgb, mode


def page_pdf(image: Image.Image, title: str = "") -> bytes:
    """One page the tablet's size (turned sideways for wide pictures) with the picture centred."""
    wide = image.width > image.height
    page_w, page_h = (PAGE_LONG, PAGE_SHORT) if wide else (PAGE_SHORT, PAGE_LONG)
    box_w, box_h = page_w - 2 * MARGIN, page_h - 2 * MARGIN
    scale = min(box_w / image.width, box_h / image.height)
    width, height = image.width * scale, image.height * scale
    if scale < 1:  # no point storing more pixels than the screen can show
        image = image.resize((round(width), round(height)), Image.LANCZOS)
    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=(page_w, page_h))
    canvas.setTitle(title)
    canvas.drawImage(ImageReader(image), (page_w - width) / 2, (page_h - height) / 2, width, height)
    canvas.save()
    return buffer.getvalue()


def preview_png(image: Image.Image, longest: int = 900) -> bytes:
    shown = image.copy()
    shown.thumbnail((longest, longest), Image.LANCZOS)
    buffer = io.BytesIO()
    shown.save(buffer, "PNG")
    return buffer.getvalue()
