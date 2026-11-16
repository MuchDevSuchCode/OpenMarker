"""Talk to the tablet over SSH: mirror its documents, and change them.

Needs developer mode on the tablet and an `ssh` client on this machine.
`setup` installs a dedicated SSH key on the tablet (asking for the tablet's
password once); after that every command authenticates with the key.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import tarfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

REMOTE_STORE = "/home/root/.local/share/remarkable/xochitl"
USB_HOST = "10.11.99.1"
# Device-local caches that are rebuilt on demand and not worth mirroring.
SKIP = ("rm-search-index.db",)


def default_mirror() -> Path:
    return Path.home() / "openmarker" / "mirror"


@dataclass
class PullResult:
    fetched: list[str]
    unchanged: int
    stale: list[str]  # in the mirror but no longer on the tablet
    pruned: bool


def key_path() -> Path:
    return Path.home() / "openmarker" / "id_ed25519"


def _ssh(host: str, command: str, stdin: bytes | None = None, use_key: bool = True) -> bytes:
    args = ["ssh", "-o", "ConnectTimeout=10"]
    if use_key:
        # BatchMode makes a rejected key fail fast instead of hanging on a prompt.
        args += ["-i", str(key_path()), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"]
    else:
        args += ["-o", "PubkeyAuthentication=no", "-o", "StrictHostKeyChecking=accept-new"]
    proc = subprocess.run(
        args + [f"root@{host}", command], input=stdin, stdout=subprocess.PIPE, check=False
    )
    if proc.returncode != 0:
        hint = " (if the key was rejected, run `openmarker setup` again)" if use_key else ""
        raise RuntimeError(f"ssh to {host} failed with exit code {proc.returncode}{hint}")
    return proc.stdout


def is_set_up() -> bool:
    return key_path().exists()


def setup(host: str = USB_HOST) -> None:
    """Create this machine's key if needed and authorise it on the tablet.

    ssh prompts for the tablet's password once. Safe to run again: the key
    is only appended to the tablet's authorized_keys if it is not there yet.
    """
    key = key_path()
    if not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "openmarker", "-f", str(key)],
            check=True,
        )
    public = key.with_suffix(".pub").read_text(encoding="utf-8").strip()
    install = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && k=$(cat) && "
        "{ grep -qxF \"$k\" ~/.ssh/authorized_keys 2>/dev/null || echo \"$k\" >> ~/.ssh/authorized_keys; } && "
        "chmod 600 ~/.ssh/authorized_keys"
    )
    _ssh(host, install, stdin=public.encode("utf-8"), use_key=False)
    if _ssh(host, "echo ok").strip() != b"ok":
        raise RuntimeError("key was installed but key login did not work")


def remote_hashes(host: str) -> dict[str, str]:
    """Map of relative path -> sha256 for every file in the tablet's store."""
    out = _ssh(host, f"cd {REMOTE_STORE} && find . -type f -exec sha256sum {{}} +")
    hashes = {}
    for line in out.decode("utf-8").splitlines():
        digest, _, path = line.partition("  ")
        path = path.removeprefix("./")
        if path and path not in SKIP:
            hashes[path] = digest
    return hashes


def local_hashes(mirror: Path) -> dict[str, str]:
    hashes = {}
    if not mirror.exists():
        return hashes
    for path in mirror.rglob("*"):
        if path.is_file():
            hashes[path.relative_to(mirror).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return hashes


def pull(host: str = USB_HOST, mirror: Path | None = None, prune: bool = False) -> PullResult:
    mirror = Path(mirror) if mirror else default_mirror()
    remote = remote_hashes(host)
    local = local_hashes(mirror)

    wanted = sorted(p for p, digest in remote.items() if local.get(p) != digest)
    stale = sorted(p for p in local if p not in remote)

    if wanted:
        mirror.mkdir(parents=True, exist_ok=True)
        listing = "".join(f"./{p}\n" for p in wanted).encode("utf-8")
        archive = _ssh(host, f"cd {REMOTE_STORE} && tar -cf - -T -", stdin=listing)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(mirror, filter="data")
        for p in wanted:
            got = hashlib.sha256((mirror / p).read_bytes()).hexdigest()
            if got != remote[p]:
                raise RuntimeError(f"hash mismatch after transfer: {p}")

    if prune:
        for p in stale:
            (mirror / p).unlink()
        for folder in sorted((d for d in mirror.rglob("*") if d.is_dir()), reverse=True):
            if not any(folder.iterdir()):
                folder.rmdir()

    return PullResult(wanted, len(remote) - len(wanted), stale, prune)


# ---- changing things on the tablet ----------------------------------------
#
# The tablet's own app only reads its document store at startup, so every
# change here is followed by restarting that app (a few seconds of blank
# screen). Its sync service then carries the change to the cloud as usual.

_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
UPLOAD_TYPES = {".pdf": "pdf", ".epub": "epub"}


def _check_id(value: str, allow: tuple[str, ...] = ()) -> str:
    if value not in allow and not _ID.fullmatch(value):
        raise ValueError(f"not a document id: {value!r}")
    return value


def _now_ms() -> str:
    return str(int(time.time() * 1000))


def _write(host: str, files: dict[str, bytes]) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mtime = int(time.time())
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    _ssh(host, f"tar -xf - -C {REMOTE_STORE}", stdin=buffer.getvalue())
    listing = "".join(f"{p}" + chr(10) for p in files).encode("utf-8")
    out = _ssh(host, f"cd {REMOTE_STORE} && xargs sha256sum", stdin=listing).decode("utf-8")
    remote = {line.partition("  ")[2]: line.partition("  ")[0] for line in out.splitlines()}
    for path, data in files.items():
        if remote.get(path) != hashlib.sha256(data).hexdigest():
            raise RuntimeError(f"file did not arrive intact on the tablet: {path}")


def restart_app(host: str) -> None:
    _ssh(host, "systemctl restart xochitl")


def _metadata(name: str, parent: str, folder: bool) -> bytes:
    now = _now_ms()
    meta = {
        "createdTime": now,
        "lastModified": now,
        "new": False,
        "parent": parent,
        "pinned": False,
        "source": "",
        "type": "CollectionType" if folder else "DocumentType",
        "visibleName": name,
    }
    if not folder:
        meta.update(lastOpened="0", lastOpenedPage=0)
    return json.dumps(meta, indent=4).encode("utf-8")


def upload(host: str, name: str, data: bytes, kind: str, parent: str = "", restart: bool = True) -> str:
    """Add a PDF or EPUB to the tablet. Returns the new document's id."""
    if kind not in UPLOAD_TYPES.values():
        raise ValueError("only PDF and EPUB files can be sent")
    _check_id(parent, allow=("",))
    doc = str(uuid.uuid4())
    content = {
        "extraMetadata": {},
        "fileType": kind,
        "fontName": "",
        "lineHeight": -1,
        "orientation": "portrait",
        "pageCount": 0,
        "pageTags": [],
        "tags": [],
        "textAlignment": "justify",
        "textScale": 1,
        "zoomMode": "bestFit",
    }
    _write(
        host,
        {
            f"{doc}.{kind}": data,
            f"{doc}.content": json.dumps(content, indent=4).encode("utf-8"),
            f"{doc}.metadata": _metadata(name, parent, folder=False),
        },
    )
    if restart:
        restart_app(host)
    return doc


def new_folder(host: str, name: str, parent: str = "", restart: bool = True) -> str:
    _check_id(parent, allow=("",))
    folder = str(uuid.uuid4())
    _write(host, {f"{folder}.metadata": _metadata(name, parent, folder=True)})
    if restart:
        restart_app(host)
    return folder


def find_folder(host: str, name: str, parent: str = "") -> str | None:
    """Id of an existing folder with this name and parent, read from the tablet."""
    script = f'cd {REMOTE_STORE} && for f in *.metadata; do echo "@@$f"; cat "$f"; echo; done'
    for block in _ssh(host, script).decode("utf-8").split("@@")[1:]:
        file_name, _, body = block.partition(chr(10))
        try:
            meta = json.loads(body)
        except ValueError:
            continue
        if (
            meta.get("type") == "CollectionType"
            and meta.get("visibleName") == name
            and meta.get("parent") == parent
            and not meta.get("deleted")
        ):
            return file_name.strip().removesuffix(".metadata")
    return None


def edit_metadata(host: str, doc: str, restart: bool = True, **changes) -> None:
    """Change fields of a document's or folder's metadata (name, parent)."""
    _check_id(doc)
    if "parent" in changes:
        _check_id(changes["parent"], allow=("", "trash"))
    meta = json.loads(_ssh(host, f"cat {REMOTE_STORE}/{doc}.metadata"))
    meta.update(changes, lastModified=_now_ms())
    _write(host, {f"{doc}.metadata": json.dumps(meta, indent=4).encode("utf-8")})
    if restart:
        restart_app(host)


def rename(host: str, doc: str, name: str) -> None:
    edit_metadata(host, doc, visibleName=name)


def move(host: str, doc: str, parent: str) -> None:
    edit_metadata(host, doc, parent=parent)


def trash(host: str, doc: str) -> None:
    edit_metadata(host, doc, parent="trash")


def add_copy(host: str, doc: str, files: dict[str, bytes], name: str, restart: bool = True) -> str:
    """Add a document from a full set of its files, under a new id and name.

    `files` is keyed by store-relative path using the original id `doc`.
    """
    _check_id(doc)
    new = str(uuid.uuid4())
    out = {}
    for path, data in files.items():
        if path == f"{doc}.metadata":
            meta = json.loads(data)
            meta.update(visibleName=name, lastModified=_now_ms())
            if meta.get("parent") == "trash":
                meta["parent"] = ""
            data = json.dumps(meta, indent=4).encode("utf-8")
        out[new + path[len(doc):]] = data
    if f"{new}.metadata" not in out:
        raise ValueError("no metadata among the files to copy")
    _write(host, out)
    if restart:
        restart_app(host)
    return new
