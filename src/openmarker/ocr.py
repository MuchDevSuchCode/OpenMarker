"""Handwriting recognition and the search index.

Recognition uses the handwriting recogniser built into Windows, fed with the
pen strokes themselves (far more accurate than OCR on a picture). It runs
entirely on this machine. On other systems recognition is unavailable and
only typed text is indexed.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from . import scene

RECOGNISER_SCALE = 0.5  # page units -> recogniser units
_lock = threading.Lock()


@dataclass
class Word:
    text: str
    x: float
    y: float
    w: float
    h: float


def available() -> bool:
    try:
        import winrt.windows.ui.input.inking  # noqa: F401
    except ImportError:
        return False
    return True


def _recognise(page: scene.Page) -> list[Word]:
    from winrt.windows.foundation import Point
    from winrt.windows.foundation.numerics import Matrix3x2
    from winrt.windows.ui.input.inking import (
        InkPoint,
        InkRecognitionTarget,
        InkRecognizerContainer,
        InkStrokeBuilder,
        InkStrokeContainer,
    )

    builder, container = InkStrokeBuilder(), InkStrokeContainer()
    identity = Matrix3x2(1, 0, 0, 1, 0, 0)
    count = 0
    for stroke in page.strokes:
        if not stroke.is_writing:
            continue
        points = [
            InkPoint(Point((x - page.left) * RECOGNISER_SCALE, (y - page.top) * RECOGNISER_SCALE), 0.5)
            for x, y, _ in stroke.points
        ]
        container.add_stroke(builder.create_stroke_from_ink_points(points, identity))
        count += 1
    if not count:
        return []
    # The recogniser object must stay referenced until the result arrives:
    # letting it be collected mid-operation hangs or crashes the call.
    recogniser = InkRecognizerContainer()
    results = recogniser.recognize_async(container, InkRecognitionTarget.ALL).get()
    words = []
    for result in results:
        candidates = result.get_text_candidates()
        if not candidates:
            continue
        box = result.bounding_rect
        words.append(
            Word(
                candidates[0],
                box.x / RECOGNISER_SCALE + page.left,
                box.y / RECOGNISER_SCALE + page.top,
                box.width / RECOGNISER_SCALE,
                box.height / RECOGNISER_SCALE,
            )
        )
    return words


def _reading_order(words: list[Word]) -> list[list[Word]]:
    """Group words into lines, top to bottom then left to right."""
    lines: list[list[Word]] = []
    for word in sorted(words, key=lambda w: w.y + w.h / 2):
        centre = word.y + word.h / 2
        last = lines[-1] if lines else None
        if last and abs(centre - sum(w.y + w.h / 2 for w in last) / len(last)) < max(word.h, 40) * 0.6:
            last.append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda w: w.x) for line in lines]


def recognise(page: scene.Page) -> list[list[Word]]:
    """Handwritten words on the page, as lines in reading order."""
    if not available():
        return []
    with _lock:  # the Windows recogniser is not safe to call concurrently
        words = _recognise(page)
    return _reading_order(words)


def page_text(page: scene.Page, lines: list[list[Word]]) -> str:
    handwriting = "\n".join(" ".join(w.text for w in line) for line in lines)
    return "\n".join(part for part in (page.typed_text, handwriting) if part)


# ---- index ---------------------------------------------------------------


def index_path() -> Path:
    return Path.home() / "openmarker" / "index.db"


def _connect() -> sqlite3.Connection:
    index_path().parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(index_path())
    db.execute(
        "CREATE TABLE IF NOT EXISTS pages ("
        "doc TEXT, page TEXT, hash TEXT, text TEXT, words TEXT, PRIMARY KEY (doc, page))"
    )
    # `source` is "ai" for text read by the AI transcription, "" for the built-in recogniser.
    if "source" not in {row[1] for row in db.execute("PRAGMA table_info(pages)")}:
        db.execute("ALTER TABLE pages ADD COLUMN source TEXT NOT NULL DEFAULT ''")
    db.execute("CREATE TABLE IF NOT EXISTS answers (doc TEXT, page TEXT, hash TEXT, PRIMARY KEY (doc, page))")
    return db


def update_index(store: Path, hashes: dict[str, str]) -> int:
    """Recognise pages whose stroke file changed. Returns how many were indexed.

    `hashes` maps store-relative paths to content hashes.
    """
    db = _connect()
    known = {(doc, page): digest for doc, page, digest in db.execute("SELECT doc, page, hash FROM pages")}
    wanted = {}
    for path, digest in hashes.items():
        if path.endswith(".rm") and path.count("/") == 1:
            doc, name = path.split("/")
            wanted[(doc, name[:-3])] = digest
    done = 0
    for key, digest in wanted.items():
        if known.get(key) == digest:
            continue
        try:
            page = scene.load(store / key[0] / f"{key[1]}.rm")
            lines = recognise(page)
            text = page_text(page, lines)
            words = [[w.text, w.x, w.y, w.w, w.h] for line in lines for w in line]
        except Exception:  # an unreadable page must not stop the rest
            text, words = "", []
        db.execute(
            "INSERT OR REPLACE INTO pages (doc, page, hash, text, words, source) VALUES (?, ?, ?, ?, ?, '')",
            (key[0], key[1], digest, text, json.dumps(words)),
        )
        db.commit()
        done += 1
    stale = [key for key in known if key not in wanted]
    db.executemany("DELETE FROM pages WHERE doc = ? AND page = ?", stale)
    db.commit()
    db.close()
    return done


def stored_text(doc: str, page: str) -> str:
    db = _connect()
    row = db.execute("SELECT text FROM pages WHERE doc = ? AND page = ?", (doc, page)).fetchone()
    db.close()
    return row[0] if row else ""


def text_source(doc: str, page: str, digest: str) -> str | None:
    """Where the stored text of a page came from ("ai" or ""), or None if the page has changed since."""
    db = _connect()
    row = db.execute("SELECT hash, source FROM pages WHERE doc = ? AND page = ?", (doc, page)).fetchone()
    db.close()
    return row[1] if row and row[0] == digest else None


def store_text(doc: str, page: str, digest: str, text: str) -> None:
    """Replace a page's text with a better reading. Word positions from the recogniser are kept."""
    db = _connect()
    row = db.execute("SELECT hash, words FROM pages WHERE doc = ? AND page = ?", (doc, page)).fetchone()
    words = row[1] if row and row[0] == digest else "[]"
    db.execute(
        "INSERT OR REPLACE INTO pages (doc, page, hash, text, words, source) VALUES (?, ?, ?, ?, ?, 'ai')",
        (doc, page, digest, text, words),
    )
    db.commit()
    db.close()


def all_text() -> list[tuple[str, str, str]]:
    """(document, page, text) for every indexed page that has text."""
    db = _connect()
    rows = db.execute("SELECT doc, page, text FROM pages WHERE text != '' ORDER BY doc, page").fetchall()
    db.close()
    return rows


def was_answered(doc: str, page: str, digest: str) -> bool:
    db = _connect()
    row = db.execute("SELECT hash FROM answers WHERE doc = ? AND page = ?", (doc, page)).fetchone()
    db.close()
    return bool(row) and row[0] == digest


def mark_answered(doc: str, page: str, digest: str) -> None:
    db = _connect()
    db.execute("INSERT OR REPLACE INTO answers VALUES (?, ?, ?)", (doc, page, digest))
    db.commit()
    db.close()


def document_text(item) -> list[str]:
    """Recognised text for each page of a document, in page order.

    Uses the index where it has the page, and recognises on the spot otherwise.
    """
    out = []
    for page_id in item.page_ids:
        text = stored_text(item.id, page_id)
        rm = item.page_file(page_id)
        if not text and rm.exists():
            try:
                page = scene.load(rm)
                text = page_text(page, recognise(page))
            except Exception:  # an unreadable page becomes an empty one
                text = ""
        out.append(text)
    return out


def search(query: str) -> list[dict]:
    """Pages containing every word of the query (case-insensitive, partial words match)."""
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return []
    db = _connect()
    rows = db.execute("SELECT doc, page, text, words FROM pages WHERE text != ''").fetchall()
    db.close()
    hits = []
    for doc, page, text, words in rows:
        lowered = text.lower()
        if not all(term in lowered for term in terms):
            continue
        line = next((l for l in text.splitlines() if any(t in l.lower() for t in terms)), "")
        boxes = [w[1:] for w in json.loads(words) if any(t in w[0].lower() for t in terms)]
        hits.append({"doc": doc, "page": page, "snippet": line.strip()[:160], "boxes": boxes})
    return hits
