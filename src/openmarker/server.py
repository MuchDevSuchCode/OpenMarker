"""Local web UI. Serves only on 127.0.0.1."""
from __future__ import annotations

import io
import json
import re
import threading
import time
import webbrowser
import zipfile
from datetime import date
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import ai, export, generate, history, images, ink, library, ocr, outbox, render, scene, tablet, workflow

PORT = 8737
AUTO_SYNC_SECONDS = 30
MAX_UPLOAD = 500 * 1024 * 1024
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
ROUTES = {
    "original": re.compile(rf"^/api/original/({UUID})\.(pdf|epub)$"),
    "page": re.compile(rf"^/api/page/({UUID})/({UUID})\.svg$"),
    "thumb": re.compile(rf"^/api/thumb/({UUID})\.png$"),
    "pdf": re.compile(rf"^/api/pdf/({UUID})\.pdf$"),
    "source": re.compile(rf"^/api/source/({UUID})/(\d+)\.png$"),
    "export": re.compile(rf"^/api/export/({UUID})\.pdf$"),
    "history": re.compile(rf"^/api/history/({UUID})$"),
    "text": re.compile(rf"^/api/text/({UUID})\.(json|txt|md)$"),
}
SNAPSHOT = re.compile(r"\d{8}T\d{12}Z")
UI_FILE = Path(__file__).parent / "static" / "ui.html"
_sync_lock = threading.Lock()


class UserError(Exception):
    """A problem to show to the user as-is."""


def store_dir() -> tuple[Path, str]:
    """The tablet mirror if one exists, otherwise the desktop app's cache."""
    mirror = tablet.default_mirror()
    if mirror.exists() and any(mirror.glob("*.metadata")):
        return mirror, "mirror"
    return library.default_store(), "desktop"


def require_tablet() -> str:
    if not tablet.is_set_up():
        raise UserError("Run setup first: python -m openmarker setup")
    host = _seen[0] or workflow.find_tablet()[0]
    if host is None:
        raise UserError("The tablet isn't reachable. Wake it and connect USB or Wi-Fi.")
    return host


def _pack(files: dict[str, bytes]) -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for path, data in files.items():
            z.writestr(path, data)
    return archive.getvalue()


def _deliver(host: str, name: str, kind: str, data: bytes, parent: str = "", setting: str = "") -> None:
    """Put one document on the tablet. `setting` names a folder setting that, if filled in, decides where."""
    if setting:
        parent = destination(host, setting, parent)
    if kind == "notebook":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            files = {path: z.read(path) for path in z.namelist()}
        for path in files:
            if path.endswith(".metadata"):  # the folder may have been decided since the notebook was made
                meta = json.loads(files[path])
                meta["parent"] = parent
                files[path] = json.dumps(meta, indent=4).encode("utf-8")
        tablet.add_files(host, files)
    else:
        tablet.upload(host, name, data, kind, parent)


def send(name: str, kind: str, data: bytes, parent: str = "", setting: str = "") -> bool:
    """Send a document to the tablet, or keep it for later if the tablet can't be reached.

    Returns whether it was delivered now. What is kept goes out with the next sync.
    """
    if not tablet.is_set_up():
        raise UserError("Run setup first: python -m openmarker setup")
    host = _seen[0] or workflow.find_tablet()[0]
    if host:
        try:
            _deliver(host, name, kind, data, parent, setting)
            return True
        except (RuntimeError, OSError):
            if workflow.find_tablet()[0]:  # still there, so it is a real failure and not the tablet going away
                raise
    outbox.add(name, kind, data, parent, setting)
    return False


def _send_waiting(host: str, retry: bool) -> dict:
    """Deliver what is in the outbox. Returns {"sent": how many, "send_error": what went wrong, if anything}."""
    waiting = [e for e in outbox.entries() if retry or e.get("tries", 0) < outbox.MAX_TRIES]
    if not waiting:
        return {}
    items = library.load(tablet.default_mirror())
    sent, error = 0, ""
    for entry in waiting:
        parent = entry.get("parent", "")
        place = items.get(parent)
        if place is None or not place.is_folder or ai._trashed(place, items):
            parent = ""  # the folder is gone, or was trashed, since this was queued
        try:
            _deliver(host, entry["name"], entry["kind"], outbox.data(entry["id"]), parent, entry.get("setting", ""))
        except (RuntimeError, OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            if not workflow.find_tablet()[0]:
                break  # the tablet went away again; nothing is wrong with the document
            outbox.failed(entry["id"], str(exc))
            error = f"“{entry['name']}” could not be sent: {exc}"
            continue
        outbox.remove(entry["id"])
        sent += 1
    return {"sent": sent, **({"send_error": error} if error else {})}


def outbox_json() -> dict:
    return {"items": [
        {"id": e["id"], "name": e["name"], "kind": e["kind"], "size": e.get("size", 0), "added": e.get("added", ""),
         "error": e.get("error", ""), "stuck": e.get("tries", 0) >= outbox.MAX_TRIES}
        for e in outbox.entries()
    ]}


_seen: tuple[str | None, str | None] = (None, None)  # the tablet's address and link, kept current by _watch_tablet
WATCH_SECONDS = 5
DISCOVER_SECONDS = 15  # how often to look the tablet up by name while it is missing


def _watch_tablet() -> None:
    """Notice the tablet coming and going, find it again by name when its
    Wi-Fi address has changed, and sync as soon as it appears (if auto-sync is
    on, or something is waiting to be sent)."""
    global _seen
    searched = 0.0
    while True:
        try:
            host, via = workflow.find_tablet()
            if host and host != _seen[0] and tablet.is_set_up() and not tablet.is_ours(host):
                host, via = None, None  # something else has that address
            if not host and tablet.is_set_up() and time.time() - searched > DISCOVER_SECONDS:
                searched = time.time()
                host = workflow.discover()
                via = "Wi-Fi" if host else None
            arrived = host is not None and _seen[0] is None
            if arrived and tablet.is_set_up():
                workflow.remember_name(host)
            _seen = (host, via)
            if arrived and tablet.is_set_up() and (workflow.load_config().get("auto_sync") or outbox.waiting()):
                sync(wait=False)
        except Exception:  # the watcher must never take the server down
            pass
        time.sleep(WATCH_SECONDS)


def status() -> dict:
    host, via = _seen
    config = workflow.load_config()
    return {
        "set_up": tablet.is_set_up(),
        "connected": host is not None,
        "via": via,
        "wifi_host": config.get("wifi_host", ""),
        "auto_sync": bool(config.get("auto_sync")),
        "last_sync": config.get("last_sync"),
        "source": store_dir()[1],
        "handwriting_search": ocr.available(),
        "subject_cutout": images.subject_available(),
        "ai": ai.enabled(),
        "outbox": len(outbox.entries()),
    }


def settings_json() -> dict:
    """Everything the Settings sheet shows."""
    config = workflow.load_config()
    hooks = config.get("hooks")
    return {
        "wifi_host": config.get("wifi_host") or "",
        "tablet_name": config.get("tablet_name") or "",
        "auto_sync": bool(config.get("auto_sync")),
        "calendar": config.get("calendar") or "",
        "markdown_dir": config.get("markdown_dir") or "",
        "markdown_tag": config.get("markdown_tag") or "",
        "hooks": len(hooks) if isinstance(hooks, list) else 0,
        "ai_enabled": bool(config.get("ai_enabled")),
        "ai_key": ai.key_source(),  # where the key comes from; the key itself is never sent to the browser
        "ai_model": ai.model(),
        "ai_models": list(ai.MODELS),
        "ai_answer_tag": config.get("ai_answer_tag") or "",
        "ai_effort": ai.effort(),
        "ai_efforts": list(ai.EFFORTS),
        "ai_fallback": ai.fallback(),
        **{name: ai.number(name) for name in ai.NUMBERS},
        "ai_limits": {name: list(limits) for name, limits in ai.NUMBERS.items()},
        "agenda_folder": config.get("agenda_folder", DEFAULT_AGENDA_FOLDER),
        "ai_summary_folder": config.get("ai_summary_folder") or "",
        "ai_answer_folder": config.get("ai_answer_folder") or "",
        "folder": str(workflow.config_path().parent),
        "handwriting_search": ocr.available(),
        "subject_cutout": images.subject_available(),
    }


def _setting(body: dict, key: str, label: str, limit: int) -> str:
    value = body[key]
    if not isinstance(value, str):
        raise UserError(f"{label} must be text.")
    value = value.strip()
    if len(value) > limit or any(ord(c) < 32 for c in value):
        raise UserError(f"{label} is too long or has characters that can't be used.")
    return value


def settings_changes(body: dict) -> dict:
    """Check the settings in a request. Returns what to store; None means "remove"."""
    changes: dict = {}
    if "wifi_host" in body:
        host = _setting(body, "wifi_host", "Wi-Fi address", 253)
        if host and not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", host):
            raise UserError("That address doesn't look right.")
        changes["wifi_host"] = host
    if "tablet_name" in body:
        name = _setting(body, "tablet_name", "Tablet name", 70).removesuffix(".local")
        if name and not re.fullmatch(r"[A-Za-z0-9-]{1,63}", name):
            raise UserError("A tablet name has only letters, digits and hyphens.")
        changes["tablet_name"] = name or None
    if "auto_sync" in body:
        if not isinstance(body["auto_sync"], bool):
            raise UserError("Automatic sync must be true or false.")
        changes["auto_sync"] = body["auto_sync"]
    if "calendar" in body:
        changes["calendar"] = _setting(body, "calendar", "Calendar", 2048) or None
    if "markdown_dir" in body:
        folder = _setting(body, "markdown_dir", "Markdown folder", 1024)
        if folder and not Path(folder).expanduser().is_absolute():
            raise UserError("Give the Markdown folder as a full path, such as D:/Notes.")
        changes["markdown_dir"] = folder or None
    if "markdown_tag" in body:
        changes["markdown_tag"] = _setting(body, "markdown_tag", "Tag", 100) or None
    if "ai_enabled" in body:
        if not isinstance(body["ai_enabled"], bool):
            raise UserError("AI features must be true or false.")
        changes["ai_enabled"] = body["ai_enabled"]
    if "ai_model" in body:
        if body["ai_model"] not in ai.MODELS:
            raise UserError("Choose one of the listed models.")
        changes["ai_model"] = body["ai_model"]
    if "ai_answer_tag" in body:
        changes["ai_answer_tag"] = _setting(body, "ai_answer_tag", "Tag", 100) or None
    if "ai_effort" in body:
        if body["ai_effort"] not in ("",) + ai.EFFORTS:
            raise UserError("Choose one of the listed effort levels.")
        changes["ai_effort"] = body["ai_effort"] or None
    if "ai_fallback" in body:
        if not isinstance(body["ai_fallback"], bool):
            raise UserError("The fallback setting must be true or false.")
        changes["ai_fallback"] = body["ai_fallback"]
    for name, (_, low, high) in ai.NUMBERS.items():
        if name in body:
            if type(body[name]) is not int or not low <= body[name] <= high:
                raise UserError(f"{NUMBER_LABELS[name]} must be a whole number from {low:,} to {high:,}.")
            changes[name] = body[name]
    for name, label in FOLDER_LABELS.items():
        if name in body:
            path = "/".join(part.strip() for part in _setting(body, name, label, 300).replace(chr(92), "/").split("/") if part.strip())
            # The agenda has a folder unless it is emptied on purpose; the others default to "beside the document".
            changes[name] = path if name == "agenda_folder" else path or None
    return changes


DEFAULT_AGENDA_FOLDER = "Agenda"
NUMBER_LABELS = {"ai_max_tokens": "Longest reply", "ai_pages": "Pages at a time", "ai_picture_width": "Page picture width"}
FOLDER_LABELS = {"agenda_folder": "Agenda folder", "ai_summary_folder": "Summaries folder", "ai_answer_folder": "Answers folder"}


def destination(host: str, setting: str, otherwise: str = "") -> str:
    """The folder on the tablet that a setting names, as an id; `otherwise` if the setting is empty.

    A folder that does not exist yet is made, and the tablet's app restarted
    once so that it knows the folder before anything is sent into it.
    """
    default = DEFAULT_AGENDA_FOLDER if setting == "agenda_folder" else ""
    path = workflow.load_config().get(setting, default)
    if not isinstance(path, str) or not path.strip():
        return otherwise
    folder, created = tablet.ensure_folder(host, path)
    if created:
        tablet.restart_app(host)
        time.sleep(6)
    return folder


def api_key_change(body: dict) -> str | None | bool:
    """The API key a settings request asks to store: a key, None to remove it, False for no change."""
    if body.get("ai_key_clear") is True:
        return None
    if "ai_key" not in body:
        return False
    key = _setting(body, "ai_key", "API key", 400)
    if not key:
        return False
    if not re.fullmatch(r"[\x21-\x7e]{20,400}", key):
        raise UserError("That doesn't look like an API key.")
    return key


def library_json() -> dict:
    store, source = store_dir()
    out = []
    for item in library.load(store).values():
        has_pdf = (store / f"{item.id}.pdf").exists()
        thumb = None
        if not item.is_folder and item.page_ids:
            first = item.page_ids[0]
            if has_pdf and first in item.source_pages:
                thumb = f"/api/source/{item.id}/{item.source_pages[first]}.png"
            elif item.file_type != "epub":
                thumb = f"/api/page/{item.id}/{first}.svg"
        out.append(
            {
                "id": item.id,
                "name": item.name,
                "parent": item.parent,
                "folder": item.is_folder,
                "type": item.file_type,
                "pages": item.page_ids,
                "source_pages": item.source_pages,
                "modified": item.modified.isoformat() if item.modified else None,
                "thumb": thumb,
                "has_pdf": has_pdf,
                "has_original": item.file_type in {"pdf", "epub"} and (store / f"{item.id}.{item.file_type}").exists(),
                "tags": item.tags,
            }
        )
    return {"source": source, "items": out}


_source_png, _source_size = render.pdf_page_png, render.pdf_page_size


@lru_cache(maxsize=256)
def _page(path: str, mtime: float) -> scene.Page:
    return scene.load(Path(path))


def _preview_pdf(store: Path, doc: str, snapshot: str) -> Path | None:
    if not snapshot:
        return store / f"{doc}.pdf"
    if SNAPSHOT.fullmatch(snapshot):
        return history.files_at(doc, snapshot).get(f"{doc}.pdf")
    return None


def page_svg(doc: str, page_id: str, query: dict) -> bytes:
    """One page as SVG. Query: at=<snapshot>, q=<search words>, over=<source page number>."""
    store, _ = store_dir()
    snapshot = query.get("at", [""])[0]
    rm = store / doc / f"{page_id}.rm"
    pdf = store / f"{doc}.pdf"
    if snapshot and SNAPSHOT.fullmatch(snapshot):
        files = history.files_at(doc, snapshot)
        rm = files.get(f"{doc}/{page_id}.rm")
        pdf = files.get(f"{doc}.pdf")
    try:
        page = _page(str(rm), rm.stat().st_mtime) if rm and rm.exists() else scene.Page()
    except Exception:  # a page we cannot parse should not break the view
        page = scene.Page()
    words = query.get("q", [""])[0].lower().split()
    boxes = []
    if words and not snapshot:
        boxes = [h["boxes"] for h in ocr.search(" ".join(words)) if h["doc"] == doc and h["page"] == page_id]
        boxes = [tuple(b) for group in boxes for b in group]
    over = query.get("over", [""])[0]
    if over.isdigit() and pdf and pdf.exists():
        # Drawn on top of a rendered original page: no paper, framed to that page.
        width, height = _source_size(str(pdf), pdf.stat().st_mtime, int(over))
        scale, x0 = export.source_fit(width, height, page.paper[1])
        framed = scene.Page(page.strokes, page.text, -x0 / scale, 0.0, (width - x0) / scale, height / scale)
        return render.svg(framed, background=False, highlights=boxes).encode("utf-8")
    return render.svg(page, highlights=boxes).encode("utf-8")


def search_json(query: str) -> dict:
    store, _ = store_dir()
    items = library.load(store)
    hits = []
    for hit in ocr.search(query):
        item = items.get(hit["doc"])
        if item and hit["page"] in item.page_ids:
            hits.append(
                {
                    "doc": item.id,
                    "name": item.name,
                    "page": hit["page"],
                    "number": item.page_ids.index(hit["page"]) + 1,
                    "snippet": hit["snippet"],
                    "trashed": item.trashed,
                }
            )
    return {"hits": hits}


def sync(wait: bool = True) -> dict:
    """Fetch what changed on the tablet, and deliver what is waiting to be sent.

    A sync that was asked for (`wait`) also retries documents that had stopped being retried.
    """
    host = require_tablet()
    if not _sync_lock.acquire(blocking=wait):
        raise UserError("A sync is already running.")
    try:
        mirror = tablet.default_mirror()
        if mirror.exists():
            # Record what the mirror holds before files removed on the tablet are dropped from it.
            history.record(mirror, tablet.local_hashes(mirror))
        result, report = workflow.sync(host, prune=True)
        answers = _answer_tagged(host)
        delivered = _send_waiting(host, retry=wait)
        if answers.get("answered") or delivered.get("sent"):
            time.sleep(2)  # let the tablet take the new documents in, then fetch them
            workflow.sync(host, prune=True)
    except RuntimeError as exc:
        raise UserError(f"Sync failed: {exc}") from exc
    finally:
        _sync_lock.release()
    return {"ok": True, "fetched": len(result.fetched), "removed": len(result.stale), **report, **answers, **delivered}


def _answer_tagged(host: str) -> dict:
    """Reply to pages the user tagged for an answer on the tablet (see ai.answer_tag).

    Each reply is sent as a document next to the one that asked. A page is
    answered once for what is written on it; change the page and it is
    answered again. Returns {"answered": how many, "ai_error": what stopped it}.
    """
    tag = ai.answer_tag()
    if not tag:
        return {}
    items = library.load(tablet.default_mirror())
    sent, error, restart, folder = 0, "", False, None
    for item, page_id, digest in ai.pending_answers(items, tag):
        number = item.page_ids.index(page_id) + 1
        try:
            reply = ai.answer_page(item, page_id)
            pdf = generate.text_pdf("Answer", reply, kicker=f"{item.name} · page {number}")
            if folder is None:  # "" when answers go beside the page that asked
                folder = destination(host, "ai_answer_folder")
            parent = folder or (item.parent if item.parent in items else "")
            restart = bool(tablet.upload(host, f"{item.name} – answer p{number}", pdf, "pdf", parent, restart=False)) or restart
        except ai.AIError as exc:
            error = str(exc)
            break
        except (RuntimeError, OSError, ValueError) as exc:
            error = f"An answer could not be sent: {exc}"
            break
        ocr.mark_answered(item.id, page_id, digest)
        sent += 1
    if restart:
        tablet.restart_app(host)
    return {"answered": sent, **({"ai_error": error} if error else {})}


def ai_action(action: str, body: dict) -> dict:
    """The AI features (see ai.py). Only the ones that end in a change touch the tablet."""
    store, _ = store_dir()
    items = library.load(store)
    delivered = True

    def document() -> library.Item:
        item = items.get(_id(body, "id"))
        if item is None or item.is_folder:
            raise UserError("Unknown document.")
        return item

    def words(key: str, limit: int) -> str:
        value = body.get(key, "")
        if not isinstance(value, str) or len(value) > limit:
            raise UserError("That text is too long.")
        return value.strip()

    try:
        if action == "ai/transcribe":
            return {"ok": True, **ai.transcribe(document())}
        if action == "ai/summary":
            return {"ok": True, **ai.summarise(document())}
        if action == "ai/ask":
            return {"ok": True, **ai.ask_notes(words("question", 2000), items, within=_id(body, "within", ("",)) if body.get("within") else "")}
        if action == "ai/tidy":
            return {"ok": True, **ai.tidy(items, _id(body, "folder", ("",)))}
        if action == "ai/summary-send":
            item = document()
            actions = body.get("actions", [])
            if not isinstance(actions, list) or len(actions) > 100 or not all(isinstance(a, str) and len(a) <= 1000 for a in actions):
                raise UserError("Those actions can't be sent.")
            pdf = generate.text_pdf(words("title", 200) or "Summary", words("summary", 20000),
                                    kicker=f"Summary · {item.name}", checklist=[a for a in actions if a.strip()])
            beside = item.parent if item.parent in items else ""
            delivered = send(f"{item.name} – summary", "pdf", pdf, beside, "ai_summary_folder")
        elif action == "ai/chat-send":
            answer = words("answer", 60000)
            if not answer:
                raise UserError("There is nothing to send.")
            question = " ".join(words("question", ai.CHAT_MESSAGE).split())
            place = items.get(_id(body, "within", ("",))) if body.get("within") else None
            if place is None:
                beside = ""
            elif place.is_folder:
                beside = place.id
            else:
                beside = place.parent if place.parent in items else ""
            title = (question[:57].rstrip() + "…") if len(question) > 58 else (question or "From your notes")
            pdf = generate.text_pdf(title, answer, kicker=f"Chat · {place.name}" if place else "Chat · your notes")
            name = "".join(c for c in title if c not in '\\/:*?"<>|').strip() or "Chat answer"
            delivered = send(f"{name} – answer", "pdf", pdf, beside, "ai_answer_folder")
        elif action == "ai/tidy-apply":
            host = require_tablet()
            changes = body.get("changes")
            if not isinstance(changes, list) or not changes or len(changes) > ai.TIDY_LIMIT:
                raise UserError("Nothing to change.")
            edits = []
            for change_ in changes:
                if not isinstance(change_, dict):
                    raise UserError("Unknown document.")
                item = items.get(_id(change_, "id"))
                if item is None or item.is_folder:
                    raise UserError("Unknown document.")
                fields = {}
                name = _text(change_, "name")
                if name != item.name:
                    fields["visibleName"] = name
                parent = _id(change_, "parent", ("",))
                if parent != item.parent:
                    if parent and not (parent in items and items[parent].is_folder):
                        raise UserError("Unknown folder.")
                    fields["parent"] = parent
                if fields:
                    edits.append((item.id, fields))
            for doc, fields in edits:  # all checked before anything is changed
                tablet.edit_metadata(host, doc, restart=False, **fields)
            if edits:
                tablet.restart_app(host)
        else:
            raise UserError("Unknown action.")
    except ai.AIError as exc:
        raise UserError(str(exc)) from exc
    except (ValueError, RuntimeError, KeyError, OSError) as exc:
        raise UserError(str(exc)) from exc
    if not delivered:
        return {"ok": True, "queued": True}
    time.sleep(2)  # give the tablet's app a moment before reading its files
    return sync()


def chat_events(body: dict):
    """The reply to a chat message as events for the browser; a failure is the last event, never an exception."""
    try:
        store, _ = store_dir()
        within = _id(body, "within", ("",)) if body.get("within") else ""
        yield from ai.chat(body.get("messages"), library.load(store), within)
    except (ai.AIError, UserError) as exc:
        yield {"type": "error", "error": str(exc)}
    except Exception as exc:  # the stream has begun, so this is the only way left to say so
        yield {"type": "error", "error": f"Something went wrong: {exc}"}


def _auto_sync_loop() -> None:
    while True:
        time.sleep(AUTO_SYNC_SECONDS)
        try:
            if (workflow.load_config().get("auto_sync") or outbox.waiting()) and tablet.is_set_up() and workflow.find_tablet()[0]:
                sync(wait=False)
        except Exception:  # background sync must never take the server down
            pass


def _follow_tablet() -> None:
    """With automatic sync on, sync within seconds of anything changing on the tablet.

    The timed sync in _auto_sync_loop stays as the safety net.
    """
    while True:
        try:
            host = _seen[0]
            if host and tablet.is_set_up() and workflow.load_config().get("auto_sync"):
                for _ in tablet.changes(host):
                    if not workflow.load_config().get("auto_sync"):
                        break
                    try:
                        sync()  # waits its turn, so a change during another sync is not missed
                    except UserError:
                        pass
        except Exception:  # the watcher must never take the server down
            pass
        time.sleep(WATCH_SECONDS)


def _index_existing() -> None:
    store, source = store_dir()
    if source == "mirror":
        try:
            ocr.update_index(store, tablet.local_hashes(store))
        except Exception:
            pass


def _text(body: dict, key: str, limit: int = 200) -> str:
    value = body.get(key, "")
    if not isinstance(value, str):
        raise UserError(f"{key} must be text.")
    value = value.strip()
    if not value or len(value) > limit:
        raise UserError("That name is empty or too long.")
    return value


def _id(body: dict, key: str, allow: tuple[str, ...] = ()) -> str:
    value = body.get(key, "")
    if not isinstance(value, str):
        raise UserError("Unknown document.")
    if value not in allow and not re.fullmatch(UUID, value):
        raise UserError("Unknown document.")
    return value


def cleaned_image(raw: bytes, query: dict):
    """The uploaded picture with its background made white, per the request's mode and tolerance."""
    if not raw:
        raise UserError("That picture is empty.")
    mode = query.get("mode", ["auto"])[0]
    try:
        tolerance = min(200.0, max(1.0, float(query.get("tolerance", [images.DEFAULT_TOLERANCE])[0])))
        return images.clean(raw, mode, tolerance)
    except RuntimeError as exc:
        raise UserError(str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise UserError("That file couldn't be read as a picture.") from exc


_traced: tuple[int, list] | None = None  # the last picture traced, so sending reuses the preview's work


def traced_strokes(raw: bytes) -> list:
    """Pen strokes for a drawing (see ink.trace). Slow, so the latest result is kept."""
    global _traced
    key = hash(raw)
    if _traced is None or _traced[0] != key:
        if not raw:
            raise UserError("That picture is empty.")
        try:
            picture, _ = images.clean(raw, "keep")
        except (ValueError, OSError) as exc:
            raise UserError("That file couldn't be read as a picture.") from exc
        _traced = (key, ink.trace(picture))
    return _traced[1]


def change(action: str, body: dict, raw: bytes = b"", query: dict | None = None) -> dict:
    """Operations that write to the tablet. Each ends with a sync so the UI is current.

    Sending a document works without the tablet: it is kept and goes out when
    the tablet is next reached. Everything else needs the tablet now.
    """
    query = query or {}
    delivered = True
    if action == "outbox-remove":
        try:
            outbox.remove(str(body.get("id", "")))
        except ValueError as exc:
            raise UserError("Unknown document.") from exc
        return {"ok": True, **outbox_json()}
    try:
        if action == "upload":
            name = query.get("name", [""])[0].strip()
            suffix = Path(name).suffix.lower()
            if suffix not in tablet.UPLOAD_TYPES:
                raise UserError("Only PDF and EPUB files can be sent to the tablet.")
            if not raw:
                raise UserError("That file is empty.")
            parent = query.get("parent", [""])[0]
            delivered = send(Path(name).stem[:200], tablet.UPLOAD_TYPES[suffix], raw, _id({"p": parent}, "p", ("",)))
        elif action == "upload-image" and query.get("mode", [""])[0] == "ink":
            name = query.get("name", ["Drawing"])[0].strip()[:200] or "Drawing"
            parent = query.get("parent", [""])[0]
            parent = _id({"p": parent}, "p", ("",))
            _, files = ink.notebook_files(name, traced_strokes(raw), parent)
            delivered = send(name, "notebook", _pack(files), parent)
        elif action == "upload-image":
            name = Path(query.get("name", ["Picture"])[0].strip()).stem[:200] or "Picture"
            picture, _ = cleaned_image(raw, query)
            parent = query.get("parent", [""])[0]
            delivered = send(name, "pdf", images.page_pdf(picture, name), _id({"p": parent}, "p", ("",)))
        elif action == "send-url":
            title, data = generate.web_page_pdf(str(body.get("url", "")).strip())
            delivered = send(title, "pdf", data, _id(body, "parent", ("",)))
        elif action == "agenda":
            today = date.today()
            source = workflow.load_config().get("calendar")
            events = generate.calendar_events(source, today) if source else None
            delivered = send(f"Agenda {today.isoformat()}", "pdf", generate.agenda_pdf(today, events), "", "agenda_folder")
        elif action == "folder":
            tablet.new_folder(require_tablet(), _text(body, "name"), _id(body, "parent", ("",)))
        elif action == "rename":
            tablet.rename(require_tablet(), _id(body, "id"), _text(body, "name"))
        elif action == "move":
            target, doc = _id(body, "parent", ("",)), _id(body, "id")
            items = library.load(store_dir()[0])
            walker = target
            while walker in items:  # a folder cannot be moved into itself or its own contents
                if walker == doc:
                    raise UserError("A folder can't be moved inside itself.")
                walker = items[walker].parent
            tablet.move(require_tablet(), doc, target)
        elif action == "trash":
            tablet.trash(require_tablet(), _id(body, "id"))
        elif action == "restore":
            doc, snapshot = _id(body, "id"), str(body.get("snapshot", ""))
            if not SNAPSHOT.fullmatch(snapshot):
                raise UserError("Unknown version.")
            files = {path: blob.read_bytes() for path, blob in history.files_at(doc, snapshot).items()}
            meta = json.loads(files[f"{doc}.metadata"])
            stamp = f"{snapshot[:4]}-{snapshot[4:6]}-{snapshot[6:8]} {snapshot[9:11]}:{snapshot[11:13]}"
            tablet.add_copy(require_tablet(), doc, files, f"{meta.get('visibleName', 'Document')} (restored {stamp})")
        else:
            raise UserError("Unknown action.")
    except (ValueError, RuntimeError, KeyError, OSError) as exc:
        raise UserError(str(exc)) from exc
    if not delivered:
        return {"ok": True, "queued": True}
    time.sleep(2)  # give the tablet's app a moment to come back before reading its files
    return sync()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep the terminal quiet
        pass

    def _events(self, events) -> None:
        """Send events one JSON line at a time, as they happen. Stops if the browser stops listening."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for event in events:
                self.wfile.write(json.dumps(event).encode("utf-8") + b"\n")
                self.wfile.flush()
        except OSError:  # the browser went away (Stop was pressed, or the page closed)
            pass
        finally:
            events.close()  # ends the request to the AI service too

    def _send(self, body: bytes, content_type: str, code: int = 200, cache: bool = False, download: str = "") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=20" if cache else "no-store")
        if download:
            safe = export.safe_name(download).encode("ascii", "replace").decode("ascii")
            self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict, code: int = 200) -> None:
        self._send(json.dumps(data).encode("utf-8"), "application/json", code)

    def _local_only(self) -> bool:
        """Reject requests that another website's page is making to this server."""
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if host not in allowed or (origin and urlparse(origin).netloc not in allowed):
            self._json({"error": "forbidden"}, 403)
            return False
        return True

    def do_GET(self) -> None:
        if not self._local_only():
            return
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        store, _ = store_dir()
        match = {name: route.match(path) for name, route in ROUTES.items()}
        if path == "/":
            self._send(UI_FILE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/library":
            self._json(library_json())
        elif path == "/api/status":
            self._json(status())
        elif path == "/api/outbox":
            self._json(outbox_json())
        elif path == "/api/config":
            self._json(settings_json())
        elif path == "/api/search":
            self._json(search_json(query.get("q", [""])[0]))
        elif m := match["page"]:
            self._send(page_svg(m[1], m[2], query), "image/svg+xml", cache=True)
        elif m := match["history"]:
            self._json({"versions": history.versions(m[1])})
        elif m := match["text"]:
            item = library.load(store).get(m[1])
            if item is None or item.is_folder:
                self._json({"error": "not found"}, 404)
            elif m[2] == "json":
                self._json({"pages": ocr.document_text(item), "available": ocr.available()})
            else:
                body = export.text(item, markdown_headings=m[2] == "md").encode("utf-8")
                self._send(body, "text/plain; charset=utf-8", download=f"{item.name}.{m[2]}")
        elif m := match["original"]:
            original = store / f"{m[1]}.{m[2]}"
            item = library.load(store).get(m[1])
            if not item or not original.exists():
                self._json({"error": "not found"}, 404)
            else:
                mime = "application/epub+zip" if m[2] == "epub" else "application/pdf"
                self._send(original.read_bytes(), mime, download=f"{item.name}.{m[2]}")
        elif m := match["thumb"]:
            thumbs = sorted((store / f"{m[1]}.thumbnails").glob("*.png"))
            if thumbs:
                self._send(thumbs[0].read_bytes(), "image/png", cache=True)
            else:
                self._json({"error": "not found"}, 404)
        elif (m := match["pdf"]) and (store / f"{m[1]}.pdf").exists():
            self._send((store / f"{m[1]}.pdf").read_bytes(), "application/pdf", cache=True)
        elif m := match["source"]:
            try:
                pdf = _preview_pdf(store, m[1], query.get("at", [""])[0])
                if not pdf or not pdf.is_file():
                    self._json({"error": "not found"}, 404)
                    return
                self._send(_source_png(str(pdf), pdf.stat().st_mtime, int(m[2])), "image/png", cache=True)
            except Exception:
                self._json({"error": "not found"}, 404)
        elif m := match["export"]:
            item = library.load(store).get(m[1])
            if item is None or item.is_folder or item.file_type == "epub":
                self._json({"error": "not found"}, 404)
            else:
                self._send(export.pdf(item), "application/pdf", download=f"{item.name}.pdf")
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self._local_only():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                raise ValueError
        except ValueError:
            self._json({"ok": False, "error": "Invalid request size."}, 400)
            return
        if length > MAX_UPLOAD:
            self._json({"ok": False, "error": "That file is too large."}, 413)
            return
        raw = self.rfile.read(length)
        url = urlparse(self.path)
        action = url.path.removeprefix("/api/")
        body: dict = {}
        if action not in ("upload", "upload-image", "image-preview"):
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                self._json({"ok": False, "error": "Invalid JSON request."}, 400)
                return
            if not isinstance(body, dict):
                self._json({"ok": False, "error": "Request must be a JSON object."}, 400)
                return
        try:
            if action == "image-preview":
                if parse_qs(url.query).get("mode", [""])[0] == "ink":
                    strokes = traced_strokes(raw)
                    picture, applied = ink.preview(strokes), f"ink:{len(strokes)}"
                else:
                    picture, applied = cleaned_image(raw, parse_qs(url.query))
                self.send_response(200)
                body_png = images.preview_png(picture)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body_png)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Applied-Mode", applied)
                self.end_headers()
                self.wfile.write(body_png)
            elif action == "sync":
                self._json(sync())
            elif action == "config":
                changes, key = settings_changes(body), api_key_change(body)
                workflow.update_config(**changes)
                if key is not False:
                    ai.save_key(key)
                self._json({"ok": True})
            elif action == "ai/chat":
                self._events(chat_events(body))
            elif action.startswith("ai/"):
                self._json(ai_action(action, body))
            else:
                self._json(change(action, body, raw, parse_qs(url.query)))
        except UserError as exc:
            self._json({"ok": False, "error": str(exc)})


def serve(port: int = PORT, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=_index_existing, daemon=True).start()
    threading.Thread(target=_auto_sync_loop, daemon=True).start()
    threading.Thread(target=_watch_tablet, daemon=True).start()
    threading.Thread(target=_follow_tablet, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"OpenMarker is running at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
