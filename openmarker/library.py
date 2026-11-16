"""Read-only view of a document store in the tablet's on-disk layout.

The same layout is used by the desktop app's cache and by the tablet itself:
one `<uuid>.metadata` and `<uuid>.content` JSON pair per item, plus a
`<uuid>/` folder of per-page `.rm` stroke files.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TRASH = "trash"


def default_store() -> Path:
    """Location of the desktop app's cache on this machine."""
    return Path(os.environ["APPDATA"]) / "remarkable" / "desktop"


@dataclass
class Item:
    id: str
    name: str
    parent: str  # "" = top level, "trash" = trashed, else a folder id
    is_folder: bool
    modified: datetime | None
    file_type: str = ""  # notebook | pdf | epub
    page_ids: list[str] = field(default_factory=list)
    store: Path | None = None

    @property
    def trashed(self) -> bool:
        return self.parent == TRASH

    def page_file(self, page_id: str) -> Path:
        return self.store / self.id / f"{page_id}.rm"

    def annotated_pages(self) -> list[str]:
        """Page ids that have a stroke file on disk."""
        return [p for p in self.page_ids if self.page_file(p).exists()]


def _timestamp(ms: str | None) -> datetime | None:
    if not ms or ms == "0":
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def _page_ids(content: dict) -> list[str]:
    # formatVersion 2 keeps pages under cPages with CRDT ordering keys;
    # formatVersion 1 is a plain ordered list.
    if "cPages" in content:
        pages = [p for p in content["cPages"]["pages"] if "deleted" not in p]
        pages.sort(key=lambda p: p["idx"]["value"])
        return [p["id"] for p in pages]
    return list(content.get("pages") or [])


def load(store: Path | None = None) -> dict[str, Item]:
    store = Path(store) if store else default_store()
    items: dict[str, Item] = {}
    for meta_path in sorted(store.glob("*.metadata")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("deleted"):
            continue
        content_path = meta_path.with_suffix(".content")
        content = (
            json.loads(content_path.read_text(encoding="utf-8"))
            if content_path.exists()
            else {}
        )
        items[meta_path.stem] = Item(
            id=meta_path.stem,
            name=meta.get("visibleName", ""),
            parent=meta.get("parent", ""),
            is_folder=meta.get("type") == "CollectionType",
            modified=_timestamp(meta.get("lastModified")),
            file_type=content.get("fileType", ""),
            page_ids=_page_ids(content),
            store=store,
        )
    return items


def path_of(item: Item, items: dict[str, Item]) -> str:
    parts = [item.name]
    parent = item.parent
    seen = set()
    while parent and parent != TRASH and parent in items and parent not in seen:
        seen.add(parent)
        parts.append(items[parent].name)
        parent = items[parent].parent
    if parent == TRASH:
        parts.append("(trash)")
    return "/".join(reversed(parts))
