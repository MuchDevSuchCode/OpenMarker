"""Where OpenMarker keeps its files.

Everything lives under `~/openmarker`. Without accounts that folder is the
one person's data. With accounts, the first account keeps that folder and
each further account has its own under `~/openmarker/accounts/<id>`, with
its own tablet key, library copy, settings and AI key.

The code that reads and writes data asks `data()` for the folder. A web
request, or a background task, that works for one account says so with
`use()` first; nothing else needs to know which account it is serving.
"""
from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path

_current: ContextVar[Path | None] = ContextVar("openmarker_data", default=None)


def root() -> Path:
    """The folder that holds everything, and the data of the first (or only) person."""
    return Path.home() / "openmarker"


def data() -> Path:
    """The data folder of the account being served."""
    return _current.get() or root()


def use(folder: Path | None) -> None:
    """Serve this account's folder for the rest of the current thread (None: the first person's)."""
    _current.set(folder)
