"""The AI features, with the model replaced by canned replies. Nothing here goes online."""
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from openmarker import ai, generate, ink, library, ocr, server, workflow


class FakeClient:
    """Stands in for the Anthropic client: records each request and answers with `reply`."""

    def __init__(self, reply="ok", stop_reason="end_turn"):
        self.requests = []
        self.reply, self.stop_reason = reply, stop_reason
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    @contextmanager
    def stream(self, **request):
        self.requests.append(request)
        text = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        message = SimpleNamespace(stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=text)])
        yield SimpleNamespace(get_final_message=lambda: message)


class AITests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        home = patch.object(Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        environment = patch.dict("os.environ", {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            __import__("os").environ.pop(name, None)
        self.mirror = self.home / "openmarker" / "mirror"
        self.mirror.mkdir(parents=True)

    def switch_on(self, **settings):
        ai.save_key("sk-ant-test-key-0123456789")
        workflow.update_config(ai_enabled=True, **settings)

    def notebook(self, name, parent="", page_tag=None):
        stroke = ink.InkStroke(np.array([(0.0, 100.0), (200.0, 300.0)]), 3.0)
        doc, files = ink.notebook_files(name, [stroke], parent)
        for path, data in files.items():
            target = self.mirror / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        page = next(p for p in files if p.endswith(".rm")).split("/")[1][:-3]
        if page_tag:
            content = json.loads((self.mirror / f"{doc}.content").read_text())
            content["pageTags"] = [{"name": page_tag, "pageId": page, "timestamp": 1}]
            (self.mirror / f"{doc}.content").write_text(json.dumps(content))
        return doc, page

    def folder(self, name, parent=""):
        folder = "00000000-0000-4000-8000-%012d" % len(list(self.mirror.glob("*.metadata")))
        (self.mirror / f"{folder}.metadata").write_text(json.dumps({"type": "CollectionType", "visibleName": name, "parent": parent}))
        return folder

    def items(self):
        return library.load(self.mirror)

    # ---- off by default, and the key stays on this computer ----------------------

    def test_nothing_is_sent_unless_switched_on_with_a_key(self):
        client = FakeClient()
        with patch.object(ai, "_client", return_value=client):
            with self.assertRaises(ai.AIError):
                ai.ask("system", "hello")
            workflow.update_config(ai_enabled=True)  # on, but no key
            self.assertFalse(ai.enabled())
            with self.assertRaises(ai.AIError):
                ai.ask("system", "hello")
        self.assertEqual(client.requests, [])

    def test_the_key_is_saved_apart_and_never_reported(self):
        self.switch_on()
        self.assertEqual(ai.key_source(), "saved")
        self.assertNotIn("sk-ant", json.dumps(server.settings_json()))
        self.assertNotIn("sk-ant", (self.home / "openmarker" / "config.json").read_text())
        ai.save_key(None)
        self.assertEqual(ai.key_source(), "")
        self.assertFalse(ai.enabled())

    def test_settings_are_checked(self):
        self.assertEqual(server.settings_changes({"ai_enabled": True, "ai_model": "claude-sonnet-5-5", "ai_answer_tag": " ask "}),
                         {"ai_enabled": True, "ai_model": "claude-sonnet-5-5", "ai_answer_tag": "ask"})
        self.assertEqual(server.settings_changes({"ai_answer_tag": ""}), {"ai_answer_tag": None})
        for body in ({"ai_enabled": "yes"}, {"ai_model": "some-other-model"}):
            with self.assertRaises(server.UserError):
                server.settings_changes(body)
        self.assertIs(server.api_key_change({}), False)
        self.assertIs(server.api_key_change({"ai_key": ""}), False)
        self.assertIsNone(server.api_key_change({"ai_key_clear": True}))
        self.assertEqual(server.api_key_change({"ai_key": " sk-ant-0123456789abcdefgh "}), "sk-ant-0123456789abcdefgh")
        with self.assertRaises(server.UserError):
            server.api_key_change({"ai_key": "too short"})

    # ---- the request ---------------------------------------------------------------

    def test_request_uses_the_chosen_model_and_asks_for_json_when_given_a_schema(self):
        self.switch_on()
        client = FakeClient({"answer": "x"})
        with patch.object(ai, "_client", return_value=client):
            self.assertEqual(ai.ask("system", "hello", {"type": "object"}), {"answer": "x"})
        request = client.requests[0]
        self.assertEqual(request["model"], "claude-opus-5-5")
        self.assertEqual(request["output_config"], {"format": {"type": "json_schema", "schema": {"type": "object"}}})
        self.assertEqual((request["fallbacks"], request["betas"]), ("default", [ai.FALLBACK_BETA]))
        workflow.update_config(ai_model="claude-haiku-5-5")
        with patch.object(ai, "_client", return_value=client):
            ai.ask("system", "hello")
        self.assertEqual(client.requests[1]["model"], "claude-haiku-5-5")
        self.assertNotIn("fallbacks", client.requests[1])
        self.assertNotIn("output_config", client.requests[1])

    def test_a_declined_or_cut_off_reply_is_reported_not_used(self):
        self.switch_on()
        for reason in ("refusal", "max_tokens"):
            with patch.object(ai, "_client", return_value=FakeClient("partial", reason)):
                with self.assertRaises(ai.AIError):
                    ai.ask("system", "hello")

    # ---- features --------------------------------------------------------------------

    def test_transcription_is_kept_for_search_and_not_repeated(self):
        self.switch_on()
        doc, page = self.notebook("Notes")
        client = FakeClient("order cedar boards")
        with patch.object(ai, "_client", return_value=client):
            first = ai.transcribe(self.items()[doc])
            second = ai.transcribe(self.items()[doc])
        self.assertEqual((first["pages"], first["done"], first["left"]), (["order cedar boards"], 1, 0))
        self.assertEqual(second["done"], 0)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.requests[0]["messages"][0]["content"][0]["type"], "image")
        self.assertEqual([hit["doc"] for hit in ocr.search("cedar")], [doc])

    def test_a_question_is_answered_from_indexed_pages_with_their_sources(self):
        self.switch_on()
        doc, page = self.notebook("Shed")
        binned, binned_page = self.notebook("Old", parent="trash")
        ocr.store_text(doc, page, "h", "order cedar boards")
        ocr.store_text(binned, binned_page, "h", "secret")
        client = FakeClient({"answer": "Cedar.", "sources": [1, 1, 9]})
        with patch.object(ai, "_client", return_value=client):
            reply = ai.ask_notes("which wood?", self.items())
        self.assertEqual(reply["answer"], "Cedar.")
        self.assertEqual(reply["sources"], [{"doc": doc, "page": page, "name": "Shed", "number": 1}])
        sent = client.requests[0]["messages"][0]["content"]
        self.assertIn("order cedar boards", sent)
        self.assertNotIn("secret", sent)  # trashed documents are left out

    def test_a_large_library_sends_the_closest_pages_and_says_so(self):
        self.switch_on()
        near, near_page = self.notebook("Shed")
        far, far_page = self.notebook("Cake")
        ocr.store_text(near, near_page, "h", "cedar boards " * 20)
        ocr.store_text(far, far_page, "h", "lemons and sugar " * 20)
        client = FakeClient({"answer": "Cedar.", "sources": []})
        with patch.object(ai, "_client", return_value=client):
            reply = ai.ask_notes("how many cedar boards?", self.items(), budget=300)
        self.assertIn("1 pages closest", reply["note"])
        self.assertNotIn("lemons", client.requests[0]["messages"][0]["content"])

    def test_tidy_keeps_only_real_changes_to_known_documents_and_folders(self):
        self.switch_on()
        recipes = self.folder("Recipes")
        cake, cake_page = self.notebook("Untitled")
        shed, shed_page = self.notebook("Shed rebuild")
        ocr.store_text(cake, cake_page, "h", "lemon cake two lemons three eggs")
        ocr.store_text(shed, shed_page, "h", "measure the old frame")
        docs = sorted([("Shed rebuild", shed), ("Untitled", cake)])  # the order they are listed in
        number = {doc: n for n, (_, doc) in enumerate(docs, 1)}
        reply = {"suggestions": [
            {"document": number[cake], "name": "Lemon cake", "folder": 1, "reason": "a recipe"},
            {"document": number[shed], "name": "Shed rebuild", "folder": 0, "reason": "fine"},  # no change
            {"document": 99, "name": "Ghost", "folder": 0, "reason": ""},  # not a document
        ]}
        with patch.object(ai, "_client", return_value=FakeClient(reply)):
            result = ai.tidy(self.items())
        self.assertEqual(result["suggestions"], [{"id": cake, "current": "Untitled", "name": "Lemon cake",
                                                  "parent": recipes, "parent_name": "Recipes", "reason": "a recipe"}])

    def test_a_tagged_page_is_answered_once_until_it_changes(self):
        doc, page = self.notebook("Questions", page_tag="Ask")
        self.notebook("Untagged")
        self.assertEqual(ai.answer_tag(), "")  # switched off: nothing is pending
        self.switch_on(ai_answer_tag="ask")
        pending = ai.pending_answers(self.items(), ai.answer_tag())
        self.assertEqual([(item.id, page_id) for item, page_id, _ in pending], [(doc, page)])
        ocr.mark_answered(doc, page, pending[0][2])
        self.assertEqual(ai.pending_answers(self.items(), "ask"), [])
        ocr.mark_answered(doc, page, "an older version of the page")
        self.assertEqual(len(ai.pending_answers(self.items(), "ask")), 1)

    def test_text_page_is_a_pdf_that_grows_with_its_text(self):
        short = generate.text_pdf("Answer", "A short reply.", kicker="Notes · page 1")
        long = generate.text_pdf("Answer", "- item\n" * 80, checklist=["one", "two"])
        self.assertTrue(short.startswith(b"%PDF"))
        self.assertGreater(long.count(b"/Type /Page\n"), short.count(b"/Type /Page\n"))


if __name__ == "__main__":
    unittest.main()
