"""What happens around a sync: snapshot, index, exports and user hooks.

Settings live in `~/openmarker/config.json`:

    "wifi_host": "192.168.1.50"  the tablet's Wi-Fi address, used when USB is unplugged
    "auto_sync": true            sync in the background while the UI is running
    "calendar": "https://…/basic.ics"
                                 calendar (.ics file or address) for the agenda page
    "markdown_dir": "D:/Vault"   after each sync, export changed notebooks there
    "markdown_tag": "export"     ...but only documents carrying this tag
    "hooks": [{"run": "python my_script.py"}]
                                 run a command for every document that changed;
                                 it receives OPENMARKER_DOC_ID, OPENMARKER_NAME,
                                 OPENMARKER_PATH, OPENMARKER_TAGS, OPENMARKER_MIRROR
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import history, library, tablet


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


def update_config(**changes) -> dict:
    config = load_config()
    config.update(changes)
    save_config(config)
    return config


def reachable(host: str, timeout: float = 0.7) -> bool:
    try:
        with socket.create_connection((host, 22), timeout=timeout):
            return True
    except OSError:
        return False


def find_tablet() -> tuple[str | None, str | None]:
    """First reachable address and how it is connected: USB, else the saved Wi-Fi address."""
    if reachable(tablet.USB_HOST):
        return tablet.USB_HOST, "USB"
    wifi = load_config().get("wifi_host")
    if wifi and reachable(wifi):
        return wifi, "Wi-Fi"
    return None, None


def discover() -> str | None:
    """Look for the tablet on this computer's local networks.

    The tablet's Wi-Fi address changes from time to time. This tries every
    address on the networks this computer is attached to (assumed to be
    home-sized: 254 addresses each) and returns the one that proves to be the
    tablet (see tablet.is_ours), saving it as the Wi-Fi address.
    """
    try:
        own = {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        return None
    usb_net = tablet.USB_HOST.rsplit(".", 1)[0]
    candidates = []
    for address in sorted(own):
        net, _, last = address.rpartition(".")
        private = address.startswith(("10.", "192.168.")) or (
            address.startswith("172.") and 16 <= int(address.split(".")[1]) <= 31)
        if private and net != usb_net:
            candidates += [f"{net}.{n}" for n in range(1, 255) if str(n) != last]
    if not candidates:
        return None
    with ThreadPoolExecutor(max_workers=128) as pool:
        listening = [h for h, up in zip(candidates, pool.map(lambda h: reachable(h, 0.5), candidates)) if up]
    for host in listening:
        if tablet.is_ours(host):
            update_config(wifi_host=host)
            return host
    return None


def changed_documents(paths: list[str]) -> list[str]:
    """Document ids touched by a list of store-relative paths."""
    ids = {p.split("/")[0].split(".")[0] for p in paths if p != ".tree" and ".thumbnails" not in p}
    return sorted(i for i in ids if len(i) == 36)


def after_sync(mirror: Path, hashes: dict[str, str], changed: list[str]) -> dict:
    """Snapshot, index, then run exports and hooks for the changed documents."""
    report = {"snapshot": history.record(mirror, hashes) is not None, "indexed": 0, "exported": 0, "hooks": 0}
    try:
        from . import ocr

        report["indexed"] = ocr.update_index(mirror, hashes)
    except ImportError:
        pass
    config = load_config()
    items = library.load(mirror)
    docs = [items[i] for i in changed if i in items and not items[i].is_folder and not items[i].trashed]

    target = config.get("markdown_dir")
    if target and docs:
        from . import export

        tag = config.get("markdown_tag")
        for item in docs:
            if tag and tag not in item.tags:
                continue
            export.markdown(item, items, Path(target))
            report["exported"] += 1

    for hook in config.get("hooks") or []:
        command = hook.get("run") if isinstance(hook, dict) else None
        if not command:
            continue
        for item in docs:
            env = dict(
                os.environ,
                OPENMARKER_DOC_ID=item.id,
                OPENMARKER_NAME=item.name,
                OPENMARKER_PATH=library.path_of(item, items),
                OPENMARKER_TAGS=",".join(item.tags),
                OPENMARKER_MIRROR=str(mirror),
            )
            subprocess.run(command, shell=True, env=env, check=False, timeout=300)
            report["hooks"] += 1
    return report


def sync(host: str, mirror: Path | None = None, prune: bool = False) -> tuple[tablet.PullResult, dict]:
    """Pull from the tablet, then do everything that follows a pull."""
    mirror = Path(mirror) if mirror else tablet.default_mirror()
    result = tablet.pull(host, mirror, prune)
    hashes = tablet.local_hashes(mirror)
    is_default = mirror == tablet.default_mirror()
    report = after_sync(mirror, hashes, changed_documents(result.fetched)) if is_default else {}
    if is_default:
        update_config(last_sync=datetime.now(timezone.utc).isoformat())
    return result, report
