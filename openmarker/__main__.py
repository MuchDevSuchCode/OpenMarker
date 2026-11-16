"""Command line: `python -m openmarker ui|setup|pull|ls`."""
from __future__ import annotations

import argparse
from pathlib import Path

from . import library, tablet


def cmd_ls(args: argparse.Namespace) -> None:
    store = args.store
    if args.mirror:
        store = tablet.default_mirror()
    items = library.load(store)
    rows = sorted(items.values(), key=lambda i: library.path_of(i, items).lower())
    for item in rows:
        kind = "folder" if item.is_folder else item.file_type or "?"
        when = item.modified.strftime("%Y-%m-%d %H:%M") if item.modified else "-"
        pages = "" if item.is_folder else f"{len(item.page_ids)}p, {len(item.annotated_pages())} with ink"
        print(f"{kind:9} {when:17} {pages:20} {library.path_of(item, items)}")


def cmd_setup(args: argparse.Namespace) -> None:
    print(f"Authorising this computer on the tablet at {args.host}.")
    print("Enter the tablet's SSH password if asked (Settings > Help > About > Copyrights and licenses).")
    tablet.setup(args.host)
    print(f"Done. Key: {tablet.key_path()}")


def cmd_pull(args: argparse.Namespace) -> None:
    if not tablet.is_set_up():
        print("First run: this computer is not authorised on the tablet yet.")
        cmd_setup(args)
    result = tablet.pull(args.host, args.to, args.prune)
    print(f"fetched {len(result.fetched)}, unchanged {result.unchanged}")
    for path in result.fetched:
        print(f"  + {path}")
    if result.stale:
        verb = "removed" if result.pruned else "no longer on tablet (kept; use --prune to remove)"
        print(f"{len(result.stale)} {verb}")
        for path in result.stale:
            print(f"  - {path}")


def cmd_ui(args: argparse.Namespace) -> None:
    from . import server  # imported here so `ls` and `pull` work without rmscene

    server.serve(args.port, open_browser=not args.no_browser)


def main() -> None:
    parser = argparse.ArgumentParser(prog="openmarker")
    sub = parser.add_subparsers(dest="command", required=True)

    ls = sub.add_parser("ls", help="list documents and folders")
    where = ls.add_mutually_exclusive_group()
    where.add_argument("--store", type=Path, help="document store (default: desktop app cache)")
    where.add_argument("--mirror", action="store_true", help="list the local tablet mirror")
    ls.set_defaults(func=cmd_ls)

    pull = sub.add_parser("pull", help="copy the tablet's documents to a local mirror over SSH")
    pull.add_argument("--host", default=tablet.USB_HOST, help="tablet address (default: USB)")
    pull.add_argument("--to", type=Path, help=f"mirror folder (default: {tablet.default_mirror()})")
    pull.add_argument("--prune", action="store_true", help="delete mirror files that are gone from the tablet")
    pull.set_defaults(func=cmd_pull)

    setup = sub.add_parser("setup", help="authorise this computer on the tablet (one-time)")
    setup.add_argument("--host", default=tablet.USB_HOST, help="tablet address (default: USB)")
    setup.set_defaults(func=cmd_setup)

    ui = sub.add_parser("ui", help="open the library in your browser")
    ui.add_argument("--port", type=int, default=8737)
    ui.add_argument("--no-browser", action="store_true", help="start the server without opening a browser")
    ui.set_defaults(func=cmd_ui)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
