# OpenMarker

An open, local-first companion app for e-ink paper tablets. It talks to the
tablet directly over USB or Wi-Fi, keeps a copy of your documents on your own
computer, and adds things the official app doesn't do.

OpenMarker is an independent project. It is not affiliated with, endorsed by
or supported by the tablet's manufacturer.

> **Status:** early and experimental. It is developed against one device, a
> Paper Pro on firmware 3.29, on Windows. Other devices, firmware versions
> and operating systems are untested. It needs developer mode, and it writes
> to the tablet's storage. Keep cloud sync or another backup of anything you
> care about.

## What it does

- **Local sync.** Copies the tablet's documents to a mirror folder on your
  computer over SSH. No cloud account is involved.
- **Finds the tablet by itself.** Notices when the tablet comes online over
  USB or Wi-Fi, follows it when its Wi-Fi address changes, and can sync as
  soon as it appears.
- **Library in your browser.** Folders, thumbnails, a page viewer, and search
  across document names.
- **Handwriting search and convert to text.** Uses the handwriting recogniser
  built into Windows, fed with your actual pen strokes.
- **Version history.** Every sync keeps a snapshot; any earlier version of a
  document can be restored as a copy.
- **Export.** PDF with your ink over the original pages, plain text, or
  Markdown notes written into a folder of your choice after each sync.
- **Send to tablet.** PDFs, EPUBs, web pages, a daily agenda page from a
  calendar, and pictures, without restarting the tablet's app.
- **Picture import.** Remove a plain backdrop, flatten a photo of paper, or
  cut out the subject with a local AI model.
- **Trace a drawing into real pen strokes.** Turns a line drawing into a
  native notebook page made of fineliner strokes that you can erase, select
  and draw over on the tablet.
- **Organise.** New folders, rename, move and trash from the browser.
- **Hooks.** Run your own command for every document that changes.

## Requirements

- Python 3.12 or newer.
- An `ssh` client on the PATH (included with Windows 10 and later, macOS and
  Linux).
- **Developer mode** switched on in the tablet's settings. Enabling it resets
  the tablet, so let cloud sync finish first; your documents come back when
  you sign in again. The tablet then shows its SSH password under
  *Settings › Help › About › Copyrights and licenses*.
- Windows for handwriting search and convert to text. Everything else is
  plain Python.

## Getting started

1. Get the code and start it:

   ```
   git clone https://github.com/MuchDevSuchCode/OpenMarker.git
   cd OpenMarker
   launch            (Windows; on macOS or Linux: ./launch.sh)
   ```

   The first run creates a private Python environment in `.venv` and installs
   OpenMarker into it, which takes a few minutes. After that it starts
   straight away and opens <http://127.0.0.1:8737/> in your browser.

2. Authorise this computer on the tablet, once. Plug the tablet in by USB,
   wake it, and run:

   ```
   launch setup
   ```

   It asks for the tablet's SSH password one time and installs a key, so the
   password is never needed again or stored anywhere.

3. Press **Sync now** in the browser.

The AI subject cut-out for photos is included. The first time you use it, it
downloads its model (about 180 MB).

### Wi-Fi

SSH over Wi-Fi is off on the tablet by default. To allow it, run this once
while connected by USB:

```
ssh -i ~/openmarker/id_ed25519 root@10.11.99.1 rm-ssh-over-wlan on
```

After the first connection OpenMarker finds the tablet on your network by the
name it announces (mDNS). If your network blocks that, type the tablet's
address into the Wi-Fi field in the sidebar.

### Sending without a restart

New documents are handed to the tablet's built-in upload service, the *USB web
interface* option in the tablet's storage settings. With it switched on,
documents appear while the tablet stays as it is. With it off, OpenMarker
writes the files directly and restarts the tablet's app, which blanks and
locks the screen for a few seconds. Rename, move, trash and new folder always
use the second route.

## Command line

Everything in the browser is also a command. With the launcher, put the
command after `launch`:

| Command | What it does |
| --- | --- |
| `ui` | Open the library in your browser (the default). |
| `setup` | Authorise this computer on the tablet. |
| `pull` | Copy the tablet's documents to the mirror, then index and snapshot. |
| `ls --mirror` | List documents and folders. |
| `search <words>` | Search handwriting and typed text. |
| `export <document>` | Export as PDF; `--text` for text, `--markdown DIR` for notes. |
| `send <file or web address>` | Send a PDF, EPUB, picture or web page. `--as-ink` traces a drawing into pen strokes. |
| `agenda` | Send today's agenda page. |

`launch <command> -h` lists the options of a command.

## Where your data lives

Everything stays in the `openmarker` folder in your home directory:

| Path | Contents |
| --- | --- |
| `mirror/` | The copy of the tablet's documents. |
| `history/` | Snapshots for version history. |
| `index.db` | Recognised handwriting, for search. |
| `config.json` | Settings (below). |
| `id_ed25519` | The SSH key that unlocks your tablet. Treat it like a password. |
| `known_hosts` | Your tablet's identity, so nothing else is mistaken for it. |

Settings in `config.json`:

| Key | Meaning |
| --- | --- |
| `wifi_host` | The tablet's last known Wi-Fi address. Kept up to date automatically. |
| `auto_sync` | Sync in the background while OpenMarker is running. |
| `calendar` | An `.ics` file or address for the agenda page. |
| `markdown_dir` | After each sync, export changed notebooks into this folder. |
| `markdown_tag` | Only export documents carrying this tag. |
| `hooks` | Commands to run for every changed document, e.g. `[{"run": "python my_script.py"}]`. Each receives `OPENMARKER_DOC_ID`, `OPENMARKER_NAME`, `OPENMARKER_PATH`, `OPENMARKER_TAGS` and `OPENMARKER_MIRROR`. |

## How it works

The tablet stores each document as a handful of files named after a random id:
`.metadata` (name, folder), `.content` (page list), the original `.pdf` or
`.epub`, and one `.rm` file of pen strokes per page. OpenMarker reads that
store over SSH, compares file hashes with its mirror, and copies what changed.
Pages are drawn from the stroke files, and the same strokes are what the
handwriting recogniser reads.

The web page is served only to this computer (`127.0.0.1`) and nothing is
sent to any outside service. The exceptions are things you ask for: fetching a
web page or calendar you point it at, and a one-time model download for the AI
cut-out.

## Project layout

```
src/openmarker/
    __main__.py   command line
    server.py     local web server and API
    static/       the browser interface
    tablet.py     SSH: mirroring, uploads, changes on the tablet
    workflow.py   finding the tablet, settings, what happens after a sync
    library.py    reads the document store
    scene.py      parses pen-stroke files
    render.py     draws pages as SVG
    ocr.py        handwriting recognition and the search index
    history.py    snapshots
    export.py     PDF, text and Markdown export
    generate.py   agenda pages and web pages as PDF
    images.py     picture clean-up and cut-out
    ink.py        traces drawings into pen strokes
tests/
launch.cmd, launch.sh
```

## Development

```
pip install -e .[dev]
pytest
```

## Known limits

- Typed text on notebook pages is laid out approximately.
- Traced drawings suit line art, not photographs.
- Not built yet: custom templates, a cloud-free sync server, live screen view.
- No licence has been chosen yet, so the usual open-source permissions are
  not granted until one is added.
