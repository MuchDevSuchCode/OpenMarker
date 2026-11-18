"""Documents waiting to be sent to the tablet.

Something sent while the tablet is asleep or away is kept here, already in
its final form, and delivered the next time the tablet is reached. Each
entry is two files in ~/openmarker/outbox: <id>.json describing it and
<id>.bin holding the document.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import paths

KINDS = ("pdf", "epub", "notebook")  # a notebook is its files zipped up
MAX_TRIES = 5  # failed deliveries before an entry waits to be retried by hand
_lock = threading.Lock()


def folder() -> Path:
    return paths.data() / "outbox"


def _paths(entry_id: str) -> tuple[Path, Path]:
    if not entry_id or not all(c in "0123456789abcdef-" for c in entry_id):
        raise ValueError("not an outbox entry")
    return folder() / f"{entry_id}.json", folder() / f"{entry_id}.bin"


def _save(entry: dict) -> None:
    described, _ = _paths(entry["id"])
    draft = described.with_suffix(".tmp")
    draft.write_text(json.dumps(entry, indent=2), encoding="utf-8")
    draft.replace(described)


def add(name: str, kind: str, data: bytes, parent: str = "", setting: str = "") -> dict:
    """Keep a document for later. `parent` is the folder it should go in;
    `setting`, if given, names the setting whose folder takes precedence."""
    if kind not in KINDS:
        raise ValueError("unknown kind of document")
    entry = {
        "id": str(uuid.uuid4()),
        "name": name,
        "kind": kind,
        "parent": parent,
        "setting": setting,
        "size": len(data),
        "added": datetime.now(timezone.utc).isoformat(),
        "tries": 0,
        "error": "",
    }
    with _lock:
        folder().mkdir(parents=True, exist_ok=True)
        _paths(entry["id"])[1].write_bytes(data)  # the document first: an entry is never listed without it
        _save(entry)
    return entry


def entries() -> list[dict]:
    """Everything waiting, oldest first."""
    out = []
    with _lock:
        for path in folder().glob("*.json"):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(entry, dict) and entry.get("id") == path.stem and entry.get("kind") in KINDS:
                out.append(entry)
    return sorted(out, key=lambda e: str(e.get("added", "")))


def waiting() -> int:
    """How many entries would be sent by themselves when the tablet is next reached."""
    return sum(1 for entry in entries() if entry.get("tries", 0) < MAX_TRIES)


def data(entry_id: str) -> bytes:
    return _paths(entry_id)[1].read_bytes()


def failed(entry_id: str, error: str) -> None:
    with _lock:
        described, _ = _paths(entry_id)
        try:
            entry = json.loads(described.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        entry["tries"] = int(entry.get("tries", 0)) + 1
        entry["error"] = error[:300]
        _save(entry)


def remove(entry_id: str) -> None:
    with _lock:
        for path in _paths(entry_id):
            path.unlink(missing_ok=True)
