"""Pull the tablet's document store to a local mirror over SSH.

Needs developer mode on the tablet and an `ssh` client on this machine.
`setup` installs a dedicated SSH key on the tablet (asking for the tablet's
password once); after that every command authenticates with the key.
"""
from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
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
