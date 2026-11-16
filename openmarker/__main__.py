"""Command line: `python -m openmarker <command>`. Run with `-h` for the list."""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import library, tablet, workflow


def _mirror_items() -> dict[str, library.Item]:
    return library.load(tablet.default_mirror())


def _find(items: dict[str, library.Item], name: str) -> library.Item:
    """A document by id, exact path, or unique name."""
    if name in items:
        return items[name]
    matches = [i for i in items.values() if not i.is_folder and name in (i.name, library.path_of(i, items))]
    if len(matches) != 1:
        sys.exit(f"{'No' if not matches else 'More than one'} document named {name!r}; use its path or id from `ls --mirror`.")
    return matches[0]


def _host(args: argparse.Namespace) -> str:
    """Where to reach the tablet: the given address, else USB if it answers, else saved Wi-Fi."""
    if args.host:
        return args.host
    return workflow.find_tablet()[0] or tablet.USB_HOST


def cmd_ls(args: argparse.Namespace) -> None:
    store = tablet.default_mirror() if args.mirror else args.store
    items = library.load(store)
    for item in sorted(items.values(), key=lambda i: library.path_of(i, items).lower()):
        kind = "folder" if item.is_folder else item.file_type or "?"
        when = item.modified.strftime("%Y-%m-%d %H:%M") if item.modified else "-"
        pages = "" if item.is_folder else f"{len(item.page_ids)}p, {len(item.annotated_pages())} with ink"
        print(f"{kind:9} {when:17} {pages:20} {library.path_of(item, items)}")


def cmd_setup(args: argparse.Namespace) -> None:
    host = args.host or tablet.USB_HOST
    print(f"Authorising this computer on the tablet at {host}.")
    print("Enter the tablet's SSH password if asked (Settings > Help > About > Copyrights and licenses).")
    tablet.setup(host)
    print(f"Done. Key: {tablet.key_path()}")


def _ensure_setup(args: argparse.Namespace) -> None:
    if not tablet.is_set_up():
        print("First run: this computer is not authorised on the tablet yet.")
        cmd_setup(args)


def cmd_pull(args: argparse.Namespace) -> None:
    _ensure_setup(args)
    result, report = workflow.sync(_host(args), args.to, args.prune)
    print(f"fetched {len(result.fetched)}, unchanged {result.unchanged}")
    for path in result.fetched:
        print(f"  + {path}")
    if result.stale:
        verb = "removed" if result.pruned else "no longer on tablet (kept; use --prune to remove)"
        print(f"{len(result.stale)} {verb}")
        for path in result.stale:
            print(f"  - {path}")
    if report.get("indexed"):
        print(f"indexed {report['indexed']} page(s) for search")
    if report.get("exported"):
        print(f"exported {report['exported']} document(s) to Markdown")


def cmd_search(args: argparse.Namespace) -> None:
    from . import ocr

    items = _mirror_items()
    for hit in ocr.search(" ".join(args.words)):
        item = items.get(hit["doc"])
        if item and hit["page"] in item.page_ids:
            number = item.page_ids.index(hit["page"]) + 1
            print(f"{library.path_of(item, items)}  p{number}:  {hit['snippet']}")


def cmd_export(args: argparse.Namespace) -> None:
    from . import export

    items = _mirror_items()
    if args.markdown:
        chosen = [_find(items, args.document)] if args.document else [
            i for i in items.values() if not i.is_folder and not i.trashed and i.file_type == "notebook"
        ]
        for item in chosen:
            print(export.markdown(item, items, args.markdown))
        return
    if not args.document:
        sys.exit("Name a document to export, or use --markdown DIR to export notebooks.")
    item = _find(items, args.document)
    if args.text:
        if args.out:
            args.out.write_text(export.text(item), encoding="utf-8")
            print(args.out)
        else:
            sys.stdout.reconfigure(encoding="utf-8")
            print(export.text(item), end="")
        return
    target = args.out or Path(f"{export.safe_name(item.name)}.pdf")
    target.write_bytes(export.pdf(item))
    print(target)


def cmd_send(args: argparse.Namespace) -> None:
    from . import generate, images

    _ensure_setup(args)
    host = _host(args)
    if args.source.lower().startswith(("http://", "https://")):
        name, data = generate.web_page_pdf(args.source)
        kind = "pdf"
    else:
        path = Path(args.source)
        kind = tablet.UPLOAD_TYPES.get(path.suffix.lower())
        name, data = path.stem, path.read_bytes()
        if path.suffix.lower() in images.SUFFIXES:
            picture, applied = images.clean(data, args.background, args.tolerance)
            print(f"background: {applied}")
            kind, data = "pdf", images.page_pdf(picture, args.name or name)
        elif kind is None:
            sys.exit("Only PDF and EPUB files, pictures, or a web address can be sent.")
    parent = ""
    if args.folder:
        parent = tablet.find_folder(host, args.folder) or tablet.new_folder(host, args.folder, restart=False)
    tablet.upload(host, args.name or name, data, kind, parent)
    print(f"sent {args.name or name!r} to the tablet")


def cmd_agenda(args: argparse.Namespace) -> None:
    from . import generate

    _ensure_setup(args)
    host = _host(args)
    today = date.today()
    source = args.calendar or workflow.load_config().get("calendar")
    events = generate.calendar_events(source, today) if source else None
    folder = tablet.find_folder(host, "Agenda") or tablet.new_folder(host, "Agenda", restart=False)
    tablet.upload(host, f"Agenda {today.isoformat()}", generate.agenda_pdf(today, events), "pdf", folder)
    print(f"sent agenda for {today.isoformat()}" + (f" with {len(events)} event(s)" if events is not None else ""))


def cmd_ui(args: argparse.Namespace) -> None:
    from . import server

    server.serve(args.port, open_browser=not args.no_browser)


def main() -> None:
    parser = argparse.ArgumentParser(prog="openmarker")
    sub = parser.add_subparsers(dest="command", required=True)
    host_help = "tablet address (default: USB if connected, otherwise the saved Wi-Fi address)"

    ui = sub.add_parser("ui", help="open the library in your browser")
    ui.add_argument("--port", type=int, default=8737)
    ui.add_argument("--no-browser", action="store_true", help="start the server without opening a browser")
    ui.set_defaults(func=cmd_ui)

    setup = sub.add_parser("setup", help="authorise this computer on the tablet (one-time)")
    setup.add_argument("--host", help="tablet address (default: USB)")
    setup.set_defaults(func=cmd_setup)

    pull = sub.add_parser("pull", help="copy the tablet's documents to the local mirror, then index and snapshot")
    pull.add_argument("--host", help=host_help)
    pull.add_argument("--to", type=Path, help=f"mirror folder (default: {tablet.default_mirror()})")
    pull.add_argument("--prune", action="store_true", help="delete mirror files that are gone from the tablet")
    pull.set_defaults(func=cmd_pull)

    ls = sub.add_parser("ls", help="list documents and folders")
    where = ls.add_mutually_exclusive_group()
    where.add_argument("--store", type=Path, help="document store (default: desktop app cache)")
    where.add_argument("--mirror", action="store_true", help="list the local tablet mirror")
    ls.set_defaults(func=cmd_ls)

    search = sub.add_parser("search", help="search handwriting and typed text in the mirror")
    search.add_argument("words", nargs="+")
    search.set_defaults(func=cmd_search)

    export = sub.add_parser("export", help="export a document as PDF or text, or notebooks as Markdown")
    export.add_argument("document", nargs="?", help="name, path or id (see `ls --mirror`)")
    export.add_argument("--out", type=Path, help="file to write")
    export.add_argument("--text", action="store_true", help="convert handwriting to text instead of exporting a PDF")
    export.add_argument("--markdown", type=Path, metavar="DIR", help="write Markdown notes into this folder instead")
    export.set_defaults(func=cmd_export)

    send = sub.add_parser("send", help="send a PDF, an EPUB, a picture or a web page to the tablet")
    send.add_argument("--background", choices=("auto", "subject", "cutout", "paper", "keep"), default="auto",
                      help="for pictures: cut out the main subject with a local model (subject), remove a plain "
                           "backdrop (cutout), flatten a photo of paper (paper), or leave as is")
    send.add_argument("--tolerance", type=float, default=40, help="for cutout: how far from the backdrop colour still counts (1-200)")
    send.add_argument("source", help="file path or web address")
    send.add_argument("--name", help="name to show on the tablet")
    send.add_argument("--folder", help="top-level folder to put it in (created if missing)")
    send.add_argument("--host", help=host_help)
    send.set_defaults(func=cmd_send)

    agenda = sub.add_parser("agenda", help="send today's agenda page to the tablet")
    agenda.add_argument("--calendar", help=".ics file or address (default: `calendar` in config.json)")
    agenda.add_argument("--host", help=host_help)
    agenda.set_defaults(func=cmd_agenda)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
