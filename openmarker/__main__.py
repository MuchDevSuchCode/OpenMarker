"""`python -m openmarker ls [--store PATH]` lists the library."""
from __future__ import annotations

import argparse
from pathlib import Path

from . import library


def main() -> None:
    parser = argparse.ArgumentParser(prog="openmarker")
    sub = parser.add_subparsers(dest="command", required=True)
    ls = sub.add_parser("ls", help="list documents and folders")
    ls.add_argument("--store", type=Path, help="document store (default: desktop app cache)")
    args = parser.parse_args()

    items = library.load(args.store)
    rows = sorted(items.values(), key=lambda i: library.path_of(i, items).lower())
    for item in rows:
        kind = "folder" if item.is_folder else item.file_type or "?"
        when = item.modified.strftime("%Y-%m-%d %H:%M") if item.modified else "-"
        pages = "" if item.is_folder else f"{len(item.page_ids)}p, {len(item.annotated_pages())} with ink"
        print(f"{kind:9} {when:17} {pages:20} {library.path_of(item, items)}")


if __name__ == "__main__":
    main()
