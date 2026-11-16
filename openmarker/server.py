"""Local web UI. Serves only on 127.0.0.1."""
from __future__ import annotations

import json
import re
import socket
import threading
import webbrowser
from datetime import datetime, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import library, render, tablet

PORT = 8737
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
PAGE_ROUTE = re.compile(rf"^/api/page/({UUID})/({UUID})\.svg$")
THUMB_ROUTE = re.compile(rf"^/api/thumb/({UUID})\.png$")
PDF_ROUTE = re.compile(rf"^/api/pdf/({UUID})\.pdf$")
UI_FILE = Path(__file__).with_name("ui.html")
_sync_lock = threading.Lock()


def config_path() -> Path:
    return Path.home() / "openmarker" / "config.json"


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(config: dict) -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps(config, indent=2), encoding="utf-8")


def store_dir() -> tuple[Path, str]:
    """The tablet mirror if one exists, otherwise the desktop app's cache."""
    mirror = tablet.default_mirror()
    if mirror.exists() and any(mirror.glob("*.metadata")):
        return mirror, "mirror"
    return library.default_store(), "desktop"


def reachable(host: str) -> bool:
    try:
        with socket.create_connection((host, 22), timeout=0.7):
            return True
    except OSError:
        return False


def find_tablet() -> tuple[str | None, str | None]:
    """First reachable address and how it is connected."""
    if reachable(tablet.USB_HOST):
        return tablet.USB_HOST, "USB"
    wifi = load_config().get("wifi_host")
    if wifi and reachable(wifi):
        return wifi, "Wi-Fi"
    return None, None


def status() -> dict:
    host, via = find_tablet()
    config = load_config()
    return {
        "set_up": tablet.is_set_up(),
        "connected": host is not None,
        "via": via,
        "host": host,
        "wifi_host": config.get("wifi_host", ""),
        "last_sync": config.get("last_sync"),
        "source": store_dir()[1],
    }


def library_json() -> dict:
    store, source = store_dir()
    items = library.load(store)
    out = []
    for item in items.values():
        inked = item.annotated_pages()
        thumb = None
        if not item.is_folder:
            if item.file_type == "notebook" and item.page_ids:
                thumb = f"/api/page/{item.id}/{item.page_ids[0]}.svg"
            elif any((store / f"{item.id}.thumbnails").glob("*.png")):
                thumb = f"/api/thumb/{item.id}.png"
        out.append(
            {
                "id": item.id,
                "name": item.name,
                "parent": item.parent,
                "folder": item.is_folder,
                "type": item.file_type,
                "pages": item.page_ids,
                "inked": inked,
                "modified": item.modified.isoformat() if item.modified else None,
                "thumb": thumb,
                "has_pdf": (store / f"{item.id}.pdf").exists(),
            }
        )
    return {"source": source, "items": out}


@lru_cache(maxsize=256)
def _rendered(path: str, mtime: float) -> bytes:
    return render.page_svg(Path(path)).encode("utf-8")


def sync() -> dict:
    if not tablet.is_set_up():
        return {"ok": False, "error": "needs_setup"}
    host, via = find_tablet()
    if host is None:
        return {"ok": False, "error": "not_connected"}
    if not _sync_lock.acquire(blocking=False):
        return {"ok": False, "error": "busy"}
    try:
        result = tablet.pull(host)
    except RuntimeError as exc:
        return {"ok": False, "error": "failed", "detail": str(exc)}
    finally:
        _sync_lock.release()
    config = load_config()
    config["last_sync"] = datetime.now(timezone.utc).isoformat()
    save_config(config)
    return {"ok": True, "via": via, "fetched": len(result.fetched), "unchanged": result.unchanged}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep the terminal quiet
        pass

    def _send(self, body: bytes, content_type: str, code: int = 200, cache: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=30" if cache else "no-store")
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
        path = urlparse(self.path).path
        store, _ = store_dir()
        if path == "/":
            self._send(UI_FILE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/library":
            self._json(library_json())
        elif path == "/api/status":
            self._json(status())
        elif m := PAGE_ROUTE.match(path):
            rm = store / m[1] / f"{m[2]}.rm"
            if rm.exists():
                try:
                    body = _rendered(str(rm), rm.stat().st_mtime)
                except Exception:  # a page we cannot parse should not break the grid
                    body = render.blank_svg().encode("utf-8")
            else:
                body = render.blank_svg().encode("utf-8")
            self._send(body, "image/svg+xml", cache=True)
        elif m := THUMB_ROUTE.match(path):
            thumbs = sorted((store / f"{m[1]}.thumbnails").glob("*.png"))
            if thumbs:
                self._send(thumbs[0].read_bytes(), "image/png", cache=True)
            else:
                self._json({"error": "not found"}, 404)
        elif m := PDF_ROUTE.match(path):
            pdf = store / f"{m[1]}.pdf"
            if pdf.exists():
                self._send(pdf.read_bytes(), "application/pdf", cache=True)
            else:
                self._json({"error": "not found"}, 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self._local_only():
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        path = urlparse(self.path).path
        if path == "/api/sync":
            self._json(sync())
        elif path == "/api/config":
            host = str(body.get("wifi_host", "")).strip()
            if host and not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", host):
                self._json({"ok": False, "error": "invalid address"}, 400)
                return
            config = load_config()
            config["wifi_host"] = host
            save_config(config)
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


def serve(port: int = PORT, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"openmarker is running at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
