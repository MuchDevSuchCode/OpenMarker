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
    # For PDF-backed documents: page id -> page number in the original file.
    # Pages inserted on the tablet are absent.
    source_pages: dict[str, int] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    page_tags: dict[str, list[str]] = field(default_factory=dict)  # page id -> tags put on that page
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
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _pages(content: dict) -> tuple[list[str], dict[str, int]]:
    """Ordered page ids, and which original-file page each one shows."""
    # formatVersion 2 keeps pages under cPages with CRDT ordering keys;
    # formatVersion 1 is a plain ordered list with a parallel redirection map.
    if "cPages" in content:
        pages = [p for p in content["cPages"]["pages"] if "deleted" not in p]
        pages.sort(key=lambda p: p["idx"]["value"])
        source = {p["id"]: p["redir"]["value"] for p in pages if "redir" in p and p["redir"]["value"] >= 0}
        return [p["id"] for p in pages], source
    ids = list(content.get("pages") or [])
    redirect = content.get("redirectionPageMap") or []
    return ids, {page: n for page, n in zip(ids, redirect) if n >= 0}


def _tags(content: dict) -> list[str]:
    names = set()
    for key in ("tags", "pageTags"):
        entries = content.get(key)
        if isinstance(entries, list):
            for tag in entries:
                name = tag.get("name") if isinstance(tag, dict) else None
                if isinstance(name, str) and name.strip():
                    names.add(name)
    return sorted(names)


def _page_tags(content: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    entries = content.get("pageTags")
    if isinstance(entries, list):
        for tag in entries:
            if isinstance(tag, dict) and isinstance(tag.get("name"), str) and isinstance(tag.get("pageId"), str):
                if tag["name"].strip():
                    out.setdefault(tag["pageId"], []).append(tag["name"].strip())
    return out


def read_content(path: Path) -> tuple[dict, list[str], dict[str, int]]:
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(content, dict):
            return {}, [], {}
        page_ids, source_pages = _pages(content)
    except (ValueError, OSError, TypeError, KeyError, AttributeError):
        return {}, [], {}
    page_ids = list(dict.fromkeys(p for p in page_ids if isinstance(p, str)))
    source_pages = {p: n for p, n in source_pages.items() if p in page_ids and type(n) is int and n >= 0}
    return content, page_ids, source_pages


def load(store: Path | None = None) -> dict[str, Item]:
    store = Path(store) if store else default_store()
    items: dict[str, Item] = {}
    for meta_path in sorted(store.glob("*.metadata")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(meta, dict):
            continue
        if meta.get("deleted"):
            continue
        content, page_ids, source_pages = read_content(meta_path.with_suffix(".content"))
        items[meta_path.stem] = Item(
            id=meta_path.stem,
            name=meta.get("visibleName") if isinstance(meta.get("visibleName"), str) and meta["visibleName"].strip() else "Untitled",
            parent=meta.get("parent") if isinstance(meta.get("parent"), str) else "",
            is_folder=meta.get("type") == "CollectionType",
            modified=_timestamp(meta.get("lastModified")),
            file_type=content.get("fileType") if content.get("fileType") in ("pdf", "epub", "notebook") else "notebook",
            page_ids=page_ids,
            source_pages=source_pages,
            tags=_tags(content),
            page_tags=_page_tags(content),
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
