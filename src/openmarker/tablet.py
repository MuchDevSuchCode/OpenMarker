"""Talk to the tablet over SSH: mirror its documents, and change them.

Needs developer mode on the tablet and an `ssh` client on this machine.
`setup` installs a dedicated SSH key on the tablet (asking for the tablet's
password once); after that every command authenticates with the key.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import paths

REMOTE_STORE = "/home/root/.local/share/remarkable/xochitl"
USB_HOST = "10.11.99.1"
# Device-local caches that are rebuilt on demand and not worth mirroring.
SKIP = ("rm-search-index.db",)


def default_mirror() -> Path:
    return paths.data() / "mirror"


@dataclass
class PullResult:
    fetched: list[str]
    unchanged: int
    stale: list[str]  # in the mirror but no longer on the tablet
    pruned: bool


# Programs started from an app that has no console window must not open one of their own.
QUIET: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


class SetupError(RuntimeError):
    """Authorising this computer on the tablet failed, for a reason worth showing."""


def key_path() -> Path:
    return paths.data() / "id_ed25519"


def _ssh_args(use_key: bool = True) -> list[str]:
    # The tablet is recognised by its own key, whatever address it has today:
    # its Wi-Fi address changes, and USB and Wi-Fi are the same device.
    args = ["ssh", "-o", "ConnectTimeout=10", "-o", "HostKeyAlias=openmarker-tablet",
            "-o", f"UserKnownHostsFile={key_path().with_name('known_hosts')}",
            "-o", "StrictHostKeyChecking=accept-new"]
    if use_key:
        # BatchMode makes a rejected key fail fast instead of hanging on a prompt.
        args += ["-i", str(key_path()), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"]
    else:
        args += ["-o", "PubkeyAuthentication=no"]
    return args


def _ssh(host: str, command: str, stdin: bytes | None = None, use_key: bool = True) -> bytes:
    feed = {"stdin": subprocess.DEVNULL} if stdin is None else {"input": stdin}
    proc = subprocess.run(
        _ssh_args(use_key) + [f"root@{host}", command], stdout=subprocess.PIPE, check=False, **feed, **QUIET
    )
    if proc.returncode != 0:
        hint = " (if the tablet no longer accepts this computer, set it up again under Tablet in the sidebar)" if use_key else ""
        raise RuntimeError(f"ssh to {host} failed with exit code {proc.returncode}{hint}")
    return proc.stdout


def is_set_up() -> bool:
    return key_path().exists()


def hostname(host: str) -> str:
    """The tablet's network name, or "" if it has an unusable one."""
    name = _ssh(host, "hostname").decode("utf-8", "replace").strip()
    return name if re.fullmatch(r"[A-Za-z0-9-]{1,63}", name) else ""


def is_ours(host: str) -> bool:
    """Whether the device at this address is the tablet that was set up.

    True only if it presents the tablet's recorded key and accepts ours, so
    another machine that happens to have the address is never mistaken for it.
    """
    if not is_set_up() or not key_path().with_name("known_hosts").exists():
        return False
    proc = subprocess.run(
        _ssh_args() + ["-o", "ConnectTimeout=4", f"root@{host}", "true"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, **QUIET,
    )
    return proc.returncode == 0


ASKPASS = "OPENMARKER_ASKPASS"  # set in the environment of the ssh that asks us for the password


def answer_askpass() -> bool:
    """If ssh started this program to ask for the password, print it and say so.

    Called first thing by every entry point: with a password given to setup(),
    ssh is told to run this program again instead of prompting on a terminal.
    """
    secret = os.environ.get(ASKPASS)
    if secret is None:
        return False
    data = (secret + "\n").encode("utf-8")
    try:
        os.write(1, data)
    except OSError:
        sys.stdout.buffer.write(data)
        sys.stdout.flush()
    return True


def _askpass_program(folder: Path) -> str:
    """A program ssh can run that answers with the password in its environment."""
    if getattr(sys, "frozen", False):
        return sys.executable  # the installed app answers for itself
    if sys.platform == "win32":
        script = folder / "askpass.cmd"
        script.write_text(f'@"{sys.executable}" -c "from openmarker import tablet; tablet.answer_askpass()"\r\n', encoding="utf-8")
    else:
        script = folder / "askpass.sh"
        script.write_text(f'#!/bin/sh\nexec "{sys.executable}" -c "from openmarker import tablet; tablet.answer_askpass()"\n', encoding="utf-8")
        script.chmod(0o700)
    return str(script)


def _authorise(host: str, public: str, install: str, password: str) -> None:
    """Install our key using the given password, with no terminal involved."""
    with tempfile.TemporaryDirectory() as folder:
        env = dict(os.environ)
        env.update({ASKPASS: password, "SSH_ASKPASS": _askpass_program(Path(folder)), "SSH_ASKPASS_REQUIRE": "force"})
        env.setdefault("DISPLAY", "openmarker:0")  # older ssh only uses the helper when this is set
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        try:
            proc = subprocess.run(
                _ssh_args(use_key=False) + ["-o", "NumberOfPasswordPrompts=1", f"root@{host}", install],
                input=public.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, check=False, timeout=60, **QUIET,
            )
        except subprocess.TimeoutExpired as exc:
            raise SetupError("The tablet did not answer in time. Wake it and try again.") from exc
        except FileNotFoundError as exc:
            raise SetupError("The ssh program was not found on this computer.") from exc
    if proc.returncode == 0:
        return
    said = proc.stderr.decode("utf-8", "replace").lower()
    if "permission denied" in said:
        raise SetupError("The tablet did not accept that password. Check it on the tablet and try again.")
    if "host key" in said or "identification has changed" in said:
        raise SetupError("The device at that address is not the tablet this computer was set up with.")
    raise SetupError("The tablet could not be reached. Wake it, connect it by USB and try again.")


def setup(host: str = USB_HOST, password: str | None = None) -> None:
    """Create this machine's key if needed and authorise it on the tablet.

    With no password, ssh prompts for it on the terminal. Given one (from the
    browser, where there is no terminal), it is handed to ssh directly. Safe
    to run again: the key is only appended to the tablet's authorized_keys if
    it is not there yet. If authorising fails the first time, nothing is left
    behind, so the app does not believe it is set up.
    """
    key = key_path()
    created = not key.exists()
    if created:
        key.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "openmarker", "-f", str(key)],
            check=True, stdin=subprocess.DEVNULL, **QUIET,
        )
    # Typing the password again is a fresh introduction: a tablet that was reset
    # presents a new identity, which the record of the old one would refuse.
    known = key.with_name("known_hosts")
    before = known.read_bytes() if password is not None and known.exists() else None
    if before is not None:
        known.unlink()
    try:
        _setup(host, key, password)
    except Exception:
        if created:
            key.unlink(missing_ok=True)
            key.with_suffix(".pub").unlink(missing_ok=True)
        if before is not None:
            known.write_bytes(before)
        raise


def _setup(host: str, key: Path, password: str | None) -> None:
    public = key.with_suffix(".pub").read_text(encoding="utf-8").strip()
    install = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && k=$(cat) && "
        "{ grep -qxF \"$k\" ~/.ssh/authorized_keys 2>/dev/null || echo \"$k\" >> ~/.ssh/authorized_keys; } && "
        "chmod 600 ~/.ssh/authorized_keys"
    )
    if password is None:
        _ssh(host, install, stdin=public.encode("utf-8"), use_key=False)
    else:
        _authorise(host, public, install, password)
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


# Runs on the tablet: prints a line each time its documents have changed and
# then stayed the same for one interval, so a burst of writes counts once.
# What it prints is a fingerprint of every file's name, size and time.
_WATCH = (
    "cd {store} || exit 1; last=; sent=; "
    "while :; do "
    "now=$(find . -type f ! -name {skip} -exec stat -c '%n %s %Y' {{}} + 2>/dev/null | sha256sum); "
    'if [ "$now" = "$last" ] && [ "$now" != "$sent" ]; then echo "$now"; sent=$now; fi; '
    "last=$now; sleep {interval}; done"
)


def changes(host: str, interval: int = 2):
    """Yield once each time the tablet's documents change, for as long as the tablet stays reachable.

    One connection stays open and the tablet does the watching, so a change
    is known a few seconds after it happens. Ends when the connection drops
    (the tablet slept or left the network).
    """
    command = _WATCH.format(store=REMOTE_STORE, skip=SKIP[0], interval=int(interval))
    process = subprocess.Popen(
        _ssh_args() + ["-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2", f"root@{host}", command],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **QUIET,
    )
    try:
        first = True
        for line in process.stdout:
            if not line.strip():
                continue
            if first:  # the state when watching began, not a change
                first = False
                continue
            yield
    finally:
        process.terminate()
        process.wait()


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
# New documents are handed to the tablet app's own upload service (the "USB
# web interface" setting), which adds them while the app keeps running. When
# that service is off, and for every other change, the files are written
# into the store directly; the app only reads its store at startup, so it is
# then restarted, which blanks and locks the screen for a few seconds.
# Either way the tablet's sync service carries the change to the cloud.

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


WEB_SERVICE = (USB_HOST, 80)  # where the tablet app's upload service listens, as seen from the tablet


@contextmanager
def _web_service(host: str):
    """Base URL of the tablet's upload service, or None if it is not running.

    The service only listens on the tablet's USB address, so over Wi-Fi it is
    reached through an SSH tunnel.
    """
    with socket.socket() as probe:  # pick a free local port
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    tunnel = subprocess.Popen(
        _ssh_args() + ["-N", "-o", "ExitOnForwardFailure=yes",
                       "-L", f"127.0.0.1:{port}:{WEB_SERVICE[0]}:{WEB_SERVICE[1]}", f"root@{host}"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **QUIET,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 20
        while time.time() < deadline and tunnel.poll() is None:
            try:
                urllib.request.urlopen(f"{base}/documents/", timeout=15).read()
                break
            except (OSError, urllib.error.URLError):
                time.sleep(0.3)
        else:
            base = None
        yield base
    finally:
        tunnel.terminate()
        tunnel.wait()


def web_upload(host: str, filename: str, data: bytes, parent: str = "") -> bool:
    """Hand a file to the tablet app's upload service. False if the service is off.

    The document appears on the tablet straight away, named after the file,
    with no restart.
    """
    _check_id(parent, allow=("",))
    with _web_service(host) as base:
        if base is None:
            return False
        # The service puts uploads in the folder that was listed last.
        urllib.request.urlopen(f"{base}/documents/{parent}", timeout=30).read()
        boundary = uuid.uuid4().hex
        safe = filename.replace('"', "'").replace(chr(13), " ").replace(chr(10), " ")
        crlf = chr(13) + chr(10)
        head = (f"--{boundary}{crlf}"
                f'Content-Disposition: form-data; name="file"; filename="{safe}"{crlf}'
                f"Content-Type: application/octet-stream{crlf}{crlf}")
        tail = f"{crlf}--{boundary}--{crlf}"
        request = urllib.request.Request(
            f"{base}/upload", data=head.encode("utf-8") + data + tail.encode("utf-8"),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     "Origin": base, "Referer": f"{base}/"},
        )
        try:
            urllib.request.urlopen(request, timeout=300).read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"the tablet refused the file ({exc.code}): {exc.read()[:200]!r}") from exc
        return True


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
    if web_upload(host, f"{name.replace('/', '-')}.{kind}", data, parent):
        return ""  # the tablet chose the id
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


def ensure_folder(host: str, path: str) -> tuple[str, bool]:
    """Id of the folder at `path` (such as "Notes/Answers"), making whatever part of it is missing.

    Also returns whether anything was made: the tablet's app only learns of a
    new folder when it is restarted, which is left to the caller.
    """
    parent, created = "", False
    for name in (part.strip() for part in path.split("/")):
        if not name:
            continue
        found = None if created else find_folder(host, name, parent)
        if found is None:
            found = new_folder(host, name, parent, restart=False)
            created = True
        parent = found
    return parent, created


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


def add_files(host: str, files: dict[str, bytes], restart: bool = True) -> None:
    """Add a complete new document given as its files (store-relative paths)."""
    for path in files:
        _check_id(path.split("/")[0].split(".")[0])
    meta = next((json.loads(d) for p, d in files.items() if p.endswith(".metadata")), None)
    if meta and meta.get("parent") != "trash":
        # The same files zipped up are the tablet's own document archive format.
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            for path, data in files.items():
                z.writestr(path, data)
        name = str(meta.get("visibleName", "Document")).replace("/", "-")
        if web_upload(host, f"{name}.rmdoc", archive.getvalue(), meta.get("parent", "")):
            return
    _write(host, files)
    if restart:
        restart_app(host)


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
