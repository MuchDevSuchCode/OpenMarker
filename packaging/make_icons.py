"""Draw the app icon (the mark from the project page) in every format the builds need.

    python packaging/make_icons.py

Writes packaging/icon.ico (Windows), packaging/icon.icns (macOS) and
src/openmarker/static/icon.png (the notification-area icon). The results are
committed, so this only needs running when the mark changes.
"""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
ACCENT, WHITE = (180, 71, 46, 255), (255, 255, 255, 255)


def mark(size: int = 1024) -> Image.Image:
    """The mark on a 64-unit grid, drawn large and scaled down for clean edges."""
    unit = size * 4 / 64
    big = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
    pen = ImageDraw.Draw(big)
    pen.rounded_rectangle((0, 0, size * 4 - 1, size * 4 - 1), radius=14 * unit, fill=ACCENT)
    pen.polygon([(x * unit, y * unit) for x, y in ((18, 46), (22, 33), (41, 14), (50, 23), (31, 42))], fill=WHITE)
    pen.line((16 * unit, 50 * unit, 48 * unit, 50 * unit), fill=WHITE, width=round(4 * unit))
    for x in (16, 48):  # round ends
        pen.ellipse(((x - 2) * unit, 48 * unit, (x + 2) * unit, 52 * unit), fill=WHITE)
    return big.resize((size, size), Image.LANCZOS)


def main() -> None:
    full = mark()
    full.save(HERE / "icon.ico", sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    # macOS icons sit inside a margin, as the system's own do.
    padded = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    padded.alpha_composite(full.resize((824, 824), Image.LANCZOS), (100, 100))
    padded.save(HERE / "icon.icns")
    full.resize((256, 256), Image.LANCZOS).save(HERE.parent / "src" / "openmarker" / "static" / "icon.png")
    print("icons written")


if __name__ == "__main__":
    main()
