"""Accounts: who may open OpenMarker, and whose data they see.

Accounts are optional. Until the first one is made, OpenMarker answers only
to this computer and needs no sign-in. Once there is one, every browser must
sign in, which is what makes it safe to reach OpenMarker from elsewhere
through a tunnel.

`~/openmarker/accounts.json` holds the accounts and the browsers that are
signed in. A password is kept only as a salted scrypt hash, and a signed-in
browser only as the hash of its cookie, so the file opens nothing by itself.
Deleting the file turns accounts off again.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from . import paths

SESSION_DAYS = 30
MIN_PASSWORD, MAX_PASSWORD = 10, 200
MAX_ACCOUNTS = 20
SCRYPT = {"n": 2**15, "r": 8, "p": 1, "maxmem": 2**26, "dklen": 32}
TRIES = 5            # wrong passwords for one name before it has to wait
WAIT, LONGEST_WAIT = 30, 900  # seconds; the wait doubles with each further wrong password
BURST, BURST_WINDOW, BURST_WAIT = 30, 300, 60  # wrong passwords for any name, before everyone waits

_lock = threading.RLock()
_hashing = threading.Semaphore(2)  # hashing is slow on purpose; a flood of sign-ins must not take the computer with it
_wrong: dict[str, tuple[int, float]] = {}  # name -> (wrong passwords in a row, when it may try again)
_recent: list[float] = []                  # when the latest wrong passwords came in, for any name
_cache: tuple[float, dict] | None = None


class AccountError(ValueError):
    """A problem to show to the person as-is."""


class Locked(AccountError):
    """Too many wrong passwords; try again later."""


def _file() -> Path:
    return paths.root() / "accounts.json"


def _load() -> dict:
    global _cache
    path = _file()
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {"accounts": [], "sessions": {}}
    if _cache is None or _cache[0] != stamp:
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            # Never fall back to "no accounts" here: that would open the app to everyone.
            raise AccountError(f"{path} can't be read. Repair it, or delete it to turn accounts off.") from exc
        if not isinstance(saved, dict) or not isinstance(saved.get("accounts"), list):
            raise AccountError(f"{path} can't be read. Repair it, or delete it to turn accounts off.")
        saved.setdefault("sessions", {})
        _cache = (stamp, saved)
    return json.loads(json.dumps(_cache[1]))  # a copy, so that a change is only kept by _save


def _save(saved: dict) -> None:
    global _cache
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    draft = path.with_name(path.name + ".new")
    draft.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    try:
        draft.chmod(0o600)
    except OSError:
        pass
    os.replace(draft, path)
    _cache = None


def _hash(password: str, salt: bytes) -> str:
    with _hashing:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT).hex()


def _check_name(name: object) -> str:
    if not isinstance(name, str):
        raise AccountError("Enter a name.")
    name = " ".join(name.split())
    if not re.fullmatch(r"[^\W_](?:[\w .'-]{0,38}[^\W_])?", name):
        raise AccountError("A name is 1 to 40 letters or digits; spaces, dots, hyphens and apostrophes may come between them.")
    return name


def _check_password(password: object) -> str:
    if not isinstance(password, str) or not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise AccountError(f"A password has at least {MIN_PASSWORD} characters.")
    return password


def enabled() -> bool:
    return bool(_load()["accounts"])


def everyone() -> list[dict]:
    return _load()["accounts"]


def find(account_id: str) -> dict | None:
    return next((a for a in everyone() if a["id"] == account_id), None)


def folder(account: dict) -> Path:
    """Where this account's data is. The first account keeps the folder that was there before accounts."""
    if account.get("first"):
        return paths.root()
    return paths.root() / "accounts" / account["id"]


def public(account: dict) -> dict:
    return {"id": account["id"], "name": account["name"], "admin": bool(account.get("admin")), "first": bool(account.get("first"))}


def create(name: object, password: object, admin: bool = False) -> dict:
    """Add an account. The first one is an administrator and takes over the data that is already here."""
    name, password = _check_name(name), _check_password(password)
    salt = secrets.token_bytes(16)
    digest = _hash(password, salt)
    with _lock:
        saved = _load()
        if any(a["name"].casefold() == name.casefold() for a in saved["accounts"]):
            raise AccountError("There is already an account with that name.")
        if len(saved["accounts"]) >= MAX_ACCOUNTS:
            raise AccountError(f"There can be at most {MAX_ACCOUNTS} accounts.")
        first = not saved["accounts"]
        account = {"id": secrets.token_hex(8), "name": name, "salt": salt.hex(), "hash": digest,
                   "admin": bool(admin) or first, "first": first}
        saved["accounts"].append(account)
        _save(saved)
    folder(account).mkdir(parents=True, exist_ok=True)
    return account


def set_password(account_id: str, password: object) -> None:
    """Change a password. Every browser signed in to that account has to sign in again."""
    password = _check_password(password)
    salt = secrets.token_bytes(16)
    digest = _hash(password, salt)
    with _lock:
        saved = _load()
        account = next((a for a in saved["accounts"] if a["id"] == account_id), None)
        if account is None:
            raise AccountError("Unknown account.")
        account["salt"], account["hash"] = salt.hex(), digest
        saved["sessions"] = {k: s for k, s in saved["sessions"].items() if s.get("account") != account_id}
        _save(saved)
        _wrong.pop(account["name"].casefold(), None)


def remove(account_id: str) -> dict:
    """Take an account away. Its data folder is left where it is.

    The first account goes last: it holds the data that was here before
    accounts, and removing it turns accounts off.
    """
    with _lock:
        saved = _load()
        account = next((a for a in saved["accounts"] if a["id"] == account_id), None)
        if account is None:
            raise AccountError("Unknown account.")
        if account.get("first") and len(saved["accounts"]) > 1:
            raise AccountError("Remove the other accounts first: this one holds the library that was here before accounts.")
        saved["accounts"].remove(account)
        saved["sessions"] = {k: s for k, s in saved["sessions"].items() if s.get("account") != account_id}
        _save(saved)
    return account


def _waiting(name: str, now: float) -> float:
    """Seconds this name has to wait before another try (0: none)."""
    del _recent[: max(0, len(_recent) - 200)]
    burst = [t for t in _recent if now - t < BURST_WINDOW]
    wait = _wrong.get(name, (0, 0.0))[1] - now
    if len(burst) >= BURST:
        wait = max(wait, burst[-1] + BURST_WAIT - now)
    return max(0.0, wait)


def check(name: object, password: object, trusted: bool = False) -> dict | None:
    """The account these belong to, or None. Raises Locked after too many wrong passwords.

    `trusted` is for someone at this computer itself, who is never made to
    wait: otherwise anyone on the internet could lock the owner out.
    """
    if not isinstance(name, str) or not isinstance(password, str) or len(name) > 100 or len(password) > MAX_PASSWORD:
        return None
    key = " ".join(name.split()).casefold()
    with _lock:
        wait = _waiting(key, time.time())
    if wait and not trusted:
        raise Locked(f"Too many wrong passwords. Try again in {_duration(wait)}.")
    account = next((a for a in everyone() if a["name"].casefold() == key), None)
    # An unknown name is hashed all the same, so the reply takes as long and gives nothing away.
    salt = bytes.fromhex(account["salt"]) if account else b"no such account!"
    right = hmac.compare_digest(_hash(password, salt), account["hash"] if account else "")
    with _lock:
        if account and right:
            _wrong.pop(key, None)
            return account
        now = time.time()
        count = _wrong.get(key, (0, 0.0))[0] + 1
        if len(_wrong) > 2000:
            _wrong.clear()
        _wrong[key] = (count, now + min(LONGEST_WAIT, WAIT * 2 ** (count - TRIES)) if count >= TRIES else 0.0)
        _recent.append(now)
    return None


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{max(1, round(seconds))} seconds"
    return f"{round(seconds / 60)} minutes"


def _mark(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "replace")).hexdigest()


def sign_in(account_id: str) -> str:
    """Start a session, and return the secret the browser keeps in its cookie."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        saved = _load()
        saved["sessions"] = {k: s for k, s in saved["sessions"].items() if s.get("expires", 0) > now}
        saved["sessions"][_mark(token)] = {"account": account_id, "expires": int(now + SESSION_DAYS * 86400)}
        _save(saved)
    return token


def signed_in(token: str | None) -> dict | None:
    """The account a browser's cookie belongs to, or None."""
    if not token or len(token) > 200:
        return None
    saved = _load()
    session = saved["sessions"].get(_mark(token))
    if not session or session.get("expires", 0) <= time.time():
        return None
    return next((a for a in saved["accounts"] if a["id"] == session.get("account")), None)


def sign_out(token: str | None) -> None:
    if not token:
        return
    with _lock:
        saved = _load()
        if saved["sessions"].pop(_mark(token), None) is not None:
            _save(saved)
