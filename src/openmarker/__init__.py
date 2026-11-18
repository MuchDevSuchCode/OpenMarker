"""OpenMarker: an open, local-first companion for e-ink paper tablets."""
import os
import sys
from pathlib import Path

__version__ = "0.1.0"

if getattr(sys, "frozen", False):
    # A packaged app has no source files to keep compiled code beside, and its own
    # folder may not be writable: give the libraries that cache a place in the data folder.
    os.environ.setdefault("NUMBA_CACHE_DIR", str(Path.home() / "openmarker" / "cache" / "numba"))
