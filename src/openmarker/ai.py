"""Optional AI features, using Anthropic's Claude API.

Everything here is off until it is switched on in Settings, and then runs only
when asked: on the document you press a button for, the question you type, or
(if a tag is set for it) the pages you tagged on the tablet. What a feature
reads is sent to the Claude API: page pictures for reading handwriting, text
that was already recognised for questions across the library.

    transcribe   read the handwriting on a document's pages, more accurately
                 than the built-in recogniser; the result feeds search
    summarise    a summary and a list of actions for one document
    ask_notes    answer a question from the text of the whole library
    chat         a conversation about the notes, the reply arriving as it is written
    tidy         suggest names and folders for documents
    answer_page  read a page as a question and write the reply
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path

from . import export, library, ocr, render, scene, workflow

MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2")
DEFAULT_MODEL = "claude-opus-5-5"
# Models that can hand a declined request to another model within the same call.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Inference settings: name in config.json -> (default, lowest, highest).
NUMBERS = {
    "ai_max_tokens": (16000, 1000, 64000),  # longest reply the model may write
    "ai_pages": (20, 1, 100),  # page pictures sent in one request, and pages transcribed in one go
    "ai_picture_width": (1240, 600, 2400),  # width in pixels of the page pictures sent
}
TEXT_BUDGET = 300_000  # characters of notes sent with one question
CHAT_TURNS = 40  # messages of a conversation sent along with a new one
CHAT_MESSAGE = 8000  # longest single chat message, in characters
TIDY_LIMIT = 60  # documents looked at in one tidy-up
EXCERPT = 700  # characters of each document shown to the model when tidying


class AIError(Exception):
    """A problem to show to the user as-is."""


# ---- settings ---------------------------------------------------------------


def key_path() -> Path:
    return Path.home() / "openmarker" / "anthropic_key"


def stored_key() -> str:
    try:
        return key_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_key(key: str | None) -> None:
    """Keep the API key in its own file, or remove it. It is never sent back to the browser."""
    path = key_path()
    if not key:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key.strip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def key_source() -> str:
    """Where the API key comes from: "saved", "environment" or "" if there is none."""
    if stored_key():
        return "saved"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "environment"
    return ""


def model() -> str:
    chosen = workflow.load_config().get("ai_model")
    return chosen if chosen in MODELS else DEFAULT_MODEL


def number(name: str) -> int:
    """One of the NUMBERS settings, kept within its range."""
    default, low, high = NUMBERS[name]
    value = workflow.load_config().get(name)
    return min(high, max(low, value)) if type(value) is int else default


def effort() -> str:
    """How hard the model should think, or "" to leave that to the model's own default."""
    chosen = workflow.load_config().get("ai_effort")
    return chosen if chosen in EFFORTS else ""


def fallback() -> bool:
    """Whether a request the model declines may be passed to another model."""
    return workflow.load_config().get("ai_fallback", True) is not False


def enabled() -> bool:
    return bool(workflow.load_config().get("ai_enabled")) and bool(key_source())


def answer_tag() -> str:
    """The page tag that asks for an answer, or "" if that is switched off."""
    tag = workflow.load_config().get("ai_answer_tag")
    return tag.strip() if isinstance(tag, str) and enabled() else ""


# ---- talking to the model ----------------------------------------------------


def _client():
    try:
        import anthropic
    except ImportError as exc:
        raise AIError("AI features need the anthropic package: pip install anthropic") from exc
    key = stored_key()
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


def _request(system: str | list, messages: list, schema: dict | None = None) -> tuple[dict, list]:
    """The request for the model, built from the saved settings, and the beta features it uses."""
    if not workflow.load_config().get("ai_enabled"):
        raise AIError("AI features are switched off. Turn them on in Settings.")
    if not key_source():
        raise AIError("Add an Anthropic API key in Settings to use AI features.")
    chosen = model()
    request: dict = {
        "model": chosen,
        "max_tokens": number("ai_max_tokens"),
        "system": system,
        "messages": messages,
    }
    output = {}
    if effort():
        output["effort"] = effort()
    if schema is not None:
        output["format"] = {"type": "json_schema", "schema": schema}
    if output:
        request["output_config"] = output
    betas = []
    if chosen in FALLBACK_MODELS and fallback():
        # If the model declines a request, let the service pass it to another model.
        betas.append(FALLBACK_BETA)
        request["fallbacks"] = "default"
    return request, betas


def _failure(exc: Exception) -> AIError:
    """What to tell the user when a request to the AI service fails."""
    import anthropic

    if isinstance(exc, anthropic.AuthenticationError):
        return AIError("The API key was not accepted. Check it in Settings.")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return AIError("That API key isn't allowed to use this model.")
    if isinstance(exc, anthropic.RateLimitError):
        return AIError("The AI service is busy or your usage limit was reached. Try again shortly.")
    if isinstance(exc, anthropic.APIStatusError):
        return AIError(f"The AI service refused the request ({exc.status_code}): {exc.message}")
    if isinstance(exc, anthropic.APIConnectionError):
        return AIError("Couldn't reach the AI service. Check the internet connection.")
    return AIError(f"The AI request failed: {exc}")


def ask(system: str, content: list | str, schema: dict | None = None) -> str | dict:
    """One request to the model. Returns its text, or the parsed reply when a JSON schema is given."""
    request, betas = _request(system, [{"role": "user", "content": content}], schema)
    import anthropic

    try:
        with _client().beta.messages.stream(betas=betas, **request) as stream:
            message = stream.get_final_message()
    except anthropic.AnthropicError as exc:
        raise _failure(exc) from exc
    if message.stop_reason == "refusal":
        raise AIError("The model declined to answer that.")
    if message.stop_reason == "max_tokens":
        raise AIError("The answer was too long to finish. Try a smaller document or a narrower question.")
    text = "".join(block.text for block in message.content if block.type == "text").strip()
    if schema is None:
        return text
    try:
        return json.loads(text)
    except ValueError as exc:
        raise AIError("The model's reply could not be read.") from exc


# ---- pages as pictures --------------------------------------------------------


def page_png(item: library.Item, page_id: str) -> bytes:
    """A page as the model sees it: the ink, over the original page for PDFs."""
    rm = item.page_file(page_id)
    page = scene.load(rm) if rm.exists() else scene.Page()
    source = item.source_pages.get(page_id)
    pdf = item.store / f"{item.id}.pdf"
    backdrop = None
    if source is not None and pdf.exists():
        mtime = pdf.stat().st_mtime
        width, height = render.pdf_page_size(str(pdf), mtime, source)
        scale, x0 = export.source_fit(width, height, page.paper[1])
        page = scene.Page(page.strokes, page.text, -x0 / scale, 0.0, (width - x0) / scale, height / scale)
        backdrop = render.pdf_page_png(str(pdf), mtime, source)
    return render.png(page, backdrop, number("ai_picture_width"))


def _picture(png: bytes) -> dict:
    data = base64.standard_b64encode(png).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---- features -----------------------------------------------------------------

TRANSCRIBE = (
    "You transcribe handwritten notes from a picture of a page. Reply with the text on the page and "
    "nothing else: no introduction, no commentary, no markdown. Keep the writer's line breaks and "
    "wording, including mistakes. Write lists one item per line. Where a word cannot be read, give "
    "your best reading followed by [?]. If the page has no writing, reply with nothing at all."
)
TRANSCRIBE_OVER_PRINT = (
    " The page is a printed document that someone has written on. Transcribe only what was added "
    "by hand, not the printed text."
)


def transcribe(item: library.Item) -> dict:
    """Read the handwriting on a document's pages and keep it for search and text view.

    Works through as many pages as the "pages at a time" setting allows that have not been read this way yet.
    Returns {"pages": text for every page, "done": pages read now, "left": pages still to do}.
    """
    todo = []
    for page_id in item.annotated_pages():
        digest = _file_hash(item.page_file(page_id))
        if ocr.text_source(item.id, page_id, digest) != "ai":
            todo.append((page_id, digest))
    batch = todo[:number("ai_pages")]
    for page_id, digest in batch:
        system = TRANSCRIBE + (TRANSCRIBE_OVER_PRINT if page_id in item.source_pages else "")
        text = ask(system, [_picture(page_png(item, page_id)), {"type": "text", "text": "Transcribe this page."}])
        ocr.store_text(item.id, page_id, digest, text)
    return {"pages": ocr.document_text(item), "done": len(batch), "left": len(todo) - len(batch)}


SUMMARY = (
    "You are given someone's own notes: handwritten pages as pictures, or their text. Write a short, "
    "faithful summary of what the notes say and list the actions they contain. Use only what is in "
    "the notes; do not add advice or facts of your own. Plain text only, no markdown. An action is "
    "something to be done, written as a short instruction; give an empty list if there are none."
)
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "A few words naming what the notes are about."},
        "summary": {"type": "string", "description": "Two to six sentences."},
        "actions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary", "actions"],
    "additionalProperties": False,
}


def summarise(item: library.Item) -> dict:
    """{"title", "summary", "actions", "note"} for one document."""
    if not item.page_ids:
        raise AIError("This document has no pages to read yet.")
    note = ""
    if len(item.page_ids) <= number("ai_pages"):
        content: list = [_picture(page_png(item, page_id)) for page_id in item.page_ids]
        content.append({"type": "text", "text": f"These are the pages of “{item.name}”, in order."})
    else:
        pages = [text for text in ocr.document_text(item) if text.strip()]
        if not pages:
            raise AIError("This document is too long to send as pictures and has no recognised text yet.")
        joined = "\n\n".join(f"Page {n}:\n{text}" for n, text in enumerate(pages, 1))
        if len(joined) > TEXT_BUDGET:
            raise AIError("This document is too long to summarise in one go.")
        content = [{"type": "text", "text": f"The recognised text of “{item.name}”:\n\n{joined}"}]
        note = f"Long document: worked from its recognised text, not the page pictures ({len(item.page_ids)} pages)."
    reply = ask(SUMMARY, content, SUMMARY_SCHEMA)
    return {"title": str(reply["title"]).strip(), "summary": str(reply["summary"]).strip(),
            "actions": [str(a).strip() for a in reply["actions"] if str(a).strip()], "note": note}


ASK = (
    "You answer a question using only the person's own notes, given below as numbered pages. If the "
    "notes do not contain the answer, say so plainly instead of guessing. Recognised handwriting has "
    "mistakes, so read it charitably. Plain text only, no markdown. In `sources`, give the numbers of "
    "the pages your answer rests on, most relevant first; an empty list if none."
)
ASK_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["answer", "sources"],
    "additionalProperties": False,
}


def _relevant(pages: list[dict], question: str, budget: int) -> list[dict]:
    """The pages that share most words with the question, within a character budget."""
    terms = {t for t in re.findall(r"\w{3,}", question.lower())}
    ranked = sorted(pages, key=lambda p: -sum(p["text"].lower().count(t) for t in terms))
    kept, used = [], 0
    for page in ranked:
        if used + len(page["text"]) > budget and kept:
            break
        kept.append(page)
        used += len(page["text"])
    return kept


def _within(item: library.Item, items: dict[str, library.Item], place: str) -> bool:
    """Whether the item is `place` itself or somewhere inside that folder."""
    seen = set()
    while item is not None and item.id not in seen:
        if item.id == place:
            return True
        seen.add(item.id)
        item = items.get(item.parent)
    return False


def ask_notes(question: str, items: dict[str, library.Item], budget: int = TEXT_BUDGET, within: str = "") -> dict:
    """Answer from the recognised text of every page in the library, or only
    of the document or folder whose id is `within`.

    Returns {"answer", "sources": [{"doc", "page", "name", "number"}], "note"}.
    """
    question = question.strip()
    if not question:
        raise AIError("Type a question first.")
    pages, listing, note = _notes(items, within, question, budget)
    reply = ask(ASK, f"My notes:\n\n{listing}\n\nMy question: {question}", ASK_SCHEMA)
    sources, seen = [], set()
    for number in reply["sources"]:
        if isinstance(number, int) and 1 <= number <= len(pages) and number not in seen:
            seen.add(number)
            sources.append({k: pages[number - 1][k] for k in ("doc", "page", "name", "number")})
    return {"answer": str(reply["answer"]).strip(), "sources": sources, "note": note}


def _notes(items: dict[str, library.Item], within: str, question: str, budget: int) -> tuple[list[dict], str, str]:
    """The recognised pages to answer from, the same pages as numbered text, and a note if some were left out."""
    pages = []
    for doc, page, text in ocr.all_text():
        item = items.get(doc)
        if item and not item.is_folder and not _trashed(item, items) and page in item.page_ids and (not within or _within(item, items, within)):
            pages.append({"doc": doc, "page": page, "name": item.name, "number": item.page_ids.index(page) + 1, "text": text})
    if not pages:
        raise AIError("There is no recognised text here yet. Open it and use “Convert to text”, or “Read the handwriting with AI”."
                      if within else "There is no recognised text to search yet. Sync, or use “Improve with AI” on a document.")
    # Always in the same order, so a follow-up question sends the same text and the service can reuse it.
    pages.sort(key=lambda p: (p["name"].lower(), p["doc"], p["number"]))
    note = ""
    if sum(len(p["text"]) for p in pages) > budget:
        chosen = {(p["doc"], p["page"]) for p in _relevant(pages, question, budget)}
        note = f"{'There is a lot here' if within else 'Your library is large'}, so this read the {len(chosen)} pages closest to the question, of {len(pages)}."
        pages = [p for p in pages if (p["doc"], p["page"]) in chosen]
    listing = "\n\n".join(f"[{n}] {p['name']}, page {p['number']}\n{p['text']}" for n, p in enumerate(pages, 1))
    return pages, listing, note


CHAT = (
    "You are talking with someone about their own notes, given below as numbered pages. Answer from "
    "the notes; when they do not contain the answer, say so plainly instead of guessing, and keep "
    "anything you add from general knowledge clearly apart. Recognised handwriting has mistakes, so "
    "read it charitably. After each statement that rests on a page, cite it by its number in square "
    "brackets, like [3] or [2][5]. Plain text only, no markdown. Be brief unless asked for more."
)


def chat_messages(messages) -> list[dict]:
    """A conversation from the browser, checked: text only, taking turns, ending with a question."""
    if not isinstance(messages, list) or not messages:
        raise AIError("Type a question first.")
    out = []
    for message in messages[-CHAT_TURNS:]:
        role = message.get("role") if isinstance(message, dict) else None
        text = message.get("content") if isinstance(message, dict) else None
        if role not in ("user", "assistant") or not isinstance(text, str) or not text.strip():
            raise AIError("That conversation can't be read.")
        if len(text) > CHAT_MESSAGE * (1 if role == "user" else 8):
            raise AIError("That message is too long.")
        if out and out[-1]["role"] == role:
            raise AIError("That conversation can't be read.")
        out.append({"role": role, "content": text.strip()})
    while out and out[0]["role"] != "user":  # cutting old messages can leave a reply first
        out.pop(0)
    if not out or out[-1]["role"] != "user":
        raise AIError("Type a question first.")
    return out


def chat(messages, items: dict[str, library.Item], within: str = "", budget: int = TEXT_BUDGET):
    """Carry on a conversation about the notes, or about the document or folder `within`.

    Yields {"type": "text", "text"} as the reply is written, then one
    {"type": "done", "sources": [{"doc", "page", "name", "number", "cite"}], "note"}.
    Nothing is kept between calls: the browser sends the conversation each time.
    """
    messages = chat_messages(messages)
    asked = " ".join(m["content"] for m in messages if m["role"] == "user")
    pages, listing, note = _notes(items, within, asked, budget)
    system = [
        {"type": "text", "text": CHAT},
        # The notes are the bulk of every request in a conversation; marked so they are read once and reused.
        {"type": "text", "text": f"The notes:\n\n{listing}", "cache_control": {"type": "ephemeral"}},
    ]
    request, betas = _request(system, messages)
    import anthropic

    written = []
    try:
        with _client().beta.messages.stream(betas=betas, **request) as stream:
            for text in stream.text_stream:
                written.append(text)
                yield {"type": "text", "text": text}
            message = stream.get_final_message()
    except anthropic.AnthropicError as exc:
        raise _failure(exc) from exc
    if message.stop_reason == "refusal":
        raise AIError("The model declined to answer that.")
    if message.stop_reason == "max_tokens":
        note = (note + " " if note else "") + "The reply was cut short: it reached the longest reply allowed in Settings."
    sources, seen = [], set()
    for match in re.finditer(r"\[(\d{1,4})\]", "".join(written)):
        number_ = int(match.group(1))
        if 1 <= number_ <= len(pages) and number_ not in seen:
            seen.add(number_)
            sources.append({**{k: pages[number_ - 1][k] for k in ("doc", "page", "name", "number")}, "cite": number_})
    yield {"type": "done", "sources": sources, "note": note}


def _trashed(item: library.Item, items: dict[str, library.Item]) -> bool:
    seen = set()
    while item is not None and item.id not in seen:
        if item.parent == library.TRASH:
            return True
        seen.add(item.id)
        item = items.get(item.parent)
    return False


TIDY = (
    "You help keep a library of handwritten notebooks tidy. For each numbered document you are given "
    "its current name and the start of its recognised text, which has mistakes. Suggest a change only "
    "where it clearly helps: a short, specific name (two to five words, no dates unless the notes are "
    "about a date) for a document whose name says nothing about its contents, and a folder from the "
    "list when the document plainly belongs in it. Leave well-named, well-filed documents out of your "
    "reply. Never invent a folder. In each suggestion, `name` is the name the document should have "
    "(repeat the current one to keep it) and `folder` is the number of a listed folder, or 0 to leave "
    "the document where it is."
)
TIDY_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "document": {"type": "integer"},
                    "name": {"type": "string"},
                    "folder": {"type": "integer"},
                    "reason": {"type": "string", "description": "A few words."},
                },
                "required": ["document", "name", "folder", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["suggestions"],
    "additionalProperties": False,
}


def tidy(items: dict[str, library.Item], folder: str = "") -> dict:
    """Suggest names and folders for the documents in one folder.

    Returns {"suggestions": [{"id", "current", "name", "parent", "parent_name", "reason"}], "note"}.
    """
    docs, unread = [], 0
    for item in sorted(items.values(), key=lambda i: i.name.lower()):
        if item.is_folder or item.parent != folder:
            continue
        text = " ".join(" ".join(ocr.stored_text(item.id, page) for page in item.page_ids[:3]).split())
        if text:
            docs.append((item, text[:EXCERPT]))
        else:
            unread += 1
    notes = []
    if unread:
        notes.append(f"{unread} document{'s have' if unread != 1 else ' has'} no recognised text and {'were' if unread != 1 else 'was'} left out.")
    if len(docs) > TIDY_LIMIT:
        notes.append(f"Looked at the first {TIDY_LIMIT} of {len(docs)} documents here.")
        docs = docs[:TIDY_LIMIT]
    if not docs:
        raise AIError("Nothing here has recognised text to go on. Sync, or use “Improve with AI” on a document.")
    folders = [i for i in items.values() if i.is_folder and not _trashed(i, items)]
    folders.sort(key=lambda i: library.path_of(i, items).lower())
    listing = "\n\n".join(f"[{n}] Name: {item.name}\nText: {text}" for n, (item, text) in enumerate(docs, 1))
    places = "\n".join(f"[{n}] {library.path_of(f, items)}" for n, f in enumerate(folders, 1)) or "(there are no folders)"
    reply = ask(TIDY, f"Folders:\n{places}\n\nDocuments:\n\n{listing}", TIDY_SCHEMA)
    suggestions, seen = [], set()
    for entry in reply["suggestions"]:
        number, place = entry.get("document"), entry.get("folder")
        if not isinstance(number, int) or not 1 <= number <= len(docs) or number in seen:
            continue
        seen.add(number)
        item = docs[number - 1][0]
        name = " ".join(str(entry.get("name", "")).split())[:200] or item.name
        target = folders[place - 1] if isinstance(place, int) and 1 <= place <= len(folders) else None
        parent = target.id if target else item.parent
        if name == item.name and parent == item.parent:
            continue
        suggestions.append({
            "id": item.id, "current": item.name, "name": name, "parent": parent,
            "parent_name": library.path_of(target, items) if target and parent != item.parent else "",
            "reason": str(entry.get("reason", "")).strip()[:200],
        })
    return {"suggestions": suggestions, "note": " ".join(notes)}


ANSWER = (
    "The picture is a page from someone's paper tablet. They wrote a question or a request on it by "
    "hand and marked the page to get a reply. Read the page and reply to what it asks. Your reply "
    "will be shown as a plain page on the same e-ink tablet, so write plain text with no markdown, "
    "keep it as short as answers the request well, use short paragraphs, and start list items with "
    "“- ”. If the page asks nothing you can make out, say what you could read and ask them to "
    "write the request more clearly."
)


def answer_page(item: library.Item, page_id: str) -> str:
    return ask(ANSWER, [_picture(page_png(item, page_id)), {"type": "text", "text": "Reply to this page."}])


def pending_answers(items: dict[str, library.Item], tag: str) -> list[tuple[library.Item, str, str]]:
    """(document, page, content hash) for pages carrying the tag that have not been answered as they are now."""
    out = []
    for item in items.values():
        if item.is_folder or _trashed(item, items):
            continue
        for page_id in item.page_ids:
            if tag.lower() not in (t.lower() for t in item.page_tags.get(page_id, [])):
                continue
            rm = item.page_file(page_id)
            if not rm.exists():
                continue
            digest = _file_hash(rm)
            if not ocr.was_answered(item.id, page_id, digest):
                out.append((item, page_id, digest))
    return out
