# OpenMarker

An open, local-first companion app for e-ink paper tablets. It talks to the
tablet directly over USB or Wi-Fi, keeps a copy of your documents on your own
computer, and adds things the official app doesn't do.

Project page, with screenshots and a guide to setting up the tablet:
<https://muchdevsuchcode.github.io/OpenMarker/>

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
  soon as it appears. With automatic sync on, a change made on the tablet
  shows in the browser a few seconds later.
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
- **Organise.** New folders, rename, move and trash from the browser:
  right-click a document, a folder or the empty space beside them.
- **AI, if you want it.** Off by default. With your own Anthropic API key:
  summaries with action lists, more accurate handwriting to text, questions
  answered from your notes, name and folder suggestions, and answers to
  questions you write on the tablet. See [AI features](#ai-features-optional).
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
name it announces (mDNS). If your network blocks that, open **Settings** in
the sidebar and type the tablet's address into **Wi-Fi address**.

### AI features (optional)

AI is off until you switch it on under **Settings › AI** and enter an
[Anthropic API key](https://console.anthropic.com/). Use is billed to that
key by Anthropic. What you run a feature on is sent to Anthropic's Claude API;
nothing is sent until you use one.

| Feature | Where | What is sent |
| --- | --- | --- |
| **Summarise** | *Summarise* in a document | Pictures of that document's pages. The summary and its action list can be sent to the tablet as a page. |
| **Improve with AI** | *Convert to text* in a document | Pictures of that document's handwritten pages, 20 at a time. The text replaces the built-in recogniser's and feeds search. Works on macOS and Linux too. |
| **Chat with your notes** | *AI* menu, *Ask AI* in search results, or right-click a document or folder | The recognised text of your notebooks (the closest pages, if the library is very large) and the conversation so far. Replies appear as they are written and name the pages they rest on; any reply can be sent to the tablet as a page. |
| **Suggest names and folders** | *AI* menu | The names and opening text of the documents in the current folder, and your folder names. Nothing changes until you apply the suggestions you ticked. |
| **Answer tagged pages** | Set a tag under Settings › AI | After a sync, a picture of each page carrying that tag. The answer arrives as a document next to the notebook. A page is answered again only if it changes. |

The first four are also in the right-click menu of a document or folder.
There, *Ask about this document* (or *folder*) opens the chat keeping to the
recognised text of that one document or folder, and *Suggest names and
folders* works on the folder you clicked.

The chat reads text, not page pictures, so a notebook has to have been
converted to text (or read with AI) before the chat can see it. A
conversation is kept only in the open page: it is not written to disk, and
is gone when you start a new chat or reload. Each message sends the notes
again; they are marked for the API's prompt cache, so follow-ups in a large
library cost a fraction of the first question.

*Answer tagged pages* is the only feature that runs by itself, and only for
pages you tagged on the tablet. It runs when you sync from the browser (or
automatic sync does), not from `launch pull`. Leave its tag empty to keep it
off.

Settings › AI also holds the inference options: the model (`claude-opus-5-5`
unless you choose another), how much effort it spends, the longest reply it
may write, how many page pictures go in one request and how large they are,
and whether a request the model declines may pass to another Claude model.

Summaries and answers are put beside the document they belong to. To collect
them in a folder instead, name it under **Settings › Where things go on the
tablet**; the agenda page's folder is set there too. A folder that does not
exist yet is made the first time something is sent to it, which restarts the
tablet's app once.

The key
is kept in `anthropic_key` in the data folder and is never sent back to the
browser; if `ANTHROPIC_API_KEY` is set in the environment, that is used when
no key is saved.

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
| `anthropic_key` | Your Anthropic API key, if you saved one for the AI features. Treat it like a password. |
| `known_hosts` | Your tablet's identity, so nothing else is mistaken for it. |

Most settings can be changed in **Settings** in the sidebar; hooks are edited
in the file. Settings in `config.json`:

| Key | Meaning |
| --- | --- |
| `wifi_host` | The tablet's last known Wi-Fi address. Kept up to date automatically. |
| `tablet_name` | The name the tablet announces on the network. Looked up as `<name>.local` when the address changes; learned on first connection. |
| `auto_sync` | Sync in the background while OpenMarker is running. |
| `calendar` | An `.ics` file or address for the agenda page. |
| `markdown_dir` | After each sync, export changed notebooks into this folder. |
| `markdown_tag` | Only export documents carrying this tag. |
| `ai_enabled` | Switch the AI features on. Off by default. |
| `ai_model` | The Claude model to use. |
| `ai_answer_tag` | Answer pages carrying this tag after a sync. Empty: off. |
| `ai_effort` | `low`, `medium`, `high`, `xhigh` or `max`. Unset: the model's own default. |
| `ai_max_tokens` | The longest reply the model may write, in tokens (default 16000). |
| `ai_pages` | Page pictures sent in one request (default 20). |
| `ai_picture_width` | Width in pixels of the page pictures sent (default 1240). |
| `ai_fallback` | Let a request the model declines pass to another Claude model (default on). |
| `agenda_folder` | Folder on the tablet for agenda pages (default `Agenda`; empty for the top level). |
| `ai_summary_folder` | Folder on the tablet for AI summaries. Unset: beside the document. |
| `ai_answer_folder` | Folder on the tablet for AI answers. Unset: beside the page that asked. |
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
web page or calendar you point it at, a one-time model download for the AI
cut-out, and, only if you switch them on, the AI features, which send what you
use them on to Anthropic.

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
    ai.py         the optional AI features
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
