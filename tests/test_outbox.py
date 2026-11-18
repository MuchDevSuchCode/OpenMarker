import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openmarker import outbox, server, tablet, workflow


class OutboxTests(unittest.TestCase):
    """Sending while the tablet is away: kept, then delivered at the next sync."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.mirror = self.home / "openmarker" / "mirror"
        self.mirror.mkdir(parents=True)
        self.reachable = None  # the tablet's address while it can be reached
        self.uploads = []
        self.fail = None
        for target, name, replacement in (
            (Path, "home", lambda: self.home),
            (tablet, "is_set_up", lambda: True),
            (workflow, "find_tablet", lambda: (self.reachable, "USB" if self.reachable else None)),
            (workflow, "sync", lambda host, prune=True: (type("R", (), {"fetched": [], "stale": []})(), {})),
            (tablet, "upload", self.upload),
            (tablet, "add_files", lambda host, files: self.uploads.append(("notebook", files))),
            (tablet, "local_hashes", lambda mirror: {}),
            (server.history, "record", lambda *a: None),
            (server.time, "sleep", lambda seconds: None),
            (server, "_works", {}),
        ):
            patcher = patch.object(target, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def upload(self, host, name, data, kind, parent="", restart=True):
        if self.fail:
            raise self.fail
        self.uploads.append((name, kind, parent, data))
        return ""

    def folder(self, name, parent=""):
        folder_id = f"00000000-0000-4000-8000-{len(list(self.mirror.glob('*.metadata'))):012d}"
        (self.mirror / f"{folder_id}.metadata").write_text(
            json.dumps({"visibleName": name, "parent": parent, "type": "CollectionType"}), encoding="utf-8")
        return folder_id

    def test_a_file_sent_while_the_tablet_is_away_is_kept_and_goes_out_with_the_next_sync(self):
        shelf = self.folder("Shelf")
        result = server.change("upload", {}, b"%PDF-1.4 book", {"name": ["Book.pdf"], "parent": [shelf]})
        self.assertEqual(result, {"ok": True, "queued": True})
        self.assertEqual(self.uploads, [])
        waiting = server.outbox_json()["items"]
        self.assertEqual([(e["name"], e["kind"], e["size"]) for e in waiting], [("Book", "pdf", 13)])
        self.assertEqual(server.status()["outbox"], 1)

        self.reachable = "10.11.99.1"
        result = server.sync()
        self.assertEqual(result["sent"], 1)
        self.assertEqual(self.uploads, [("Book", "pdf", shelf, b"%PDF-1.4 book")])
        self.assertEqual(server.outbox_json()["items"], [])
        self.assertEqual(list(outbox.folder().iterdir()), [])

    def test_a_file_is_sent_straight_away_when_the_tablet_is_there(self):
        self.reachable = "10.11.99.1"
        result = server.change("upload", {}, b"%PDF-1.4", {"name": ["Book.pdf"], "parent": [""]})
        self.assertNotIn("queued", result)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(outbox.entries(), [])

    def test_a_failure_with_the_tablet_still_there_is_reported_and_not_queued(self):
        self.reachable = "10.11.99.1"
        self.fail = RuntimeError("no space left")
        with self.assertRaises(server.UserError):
            server.change("upload", {}, b"%PDF-1.4", {"name": ["Book.pdf"], "parent": [""]})
        self.assertEqual(outbox.entries(), [])

    def test_the_tablet_dropping_away_part_way_keeps_the_file(self):
        self.reachable = "10.11.99.1"

        def drop(*args, **kwargs):
            self.reachable = None
            raise OSError("connection lost")

        with patch.object(tablet, "upload", drop):
            result = server.change("upload", {}, b"%PDF-1.4", {"name": ["Book.pdf"], "parent": [""]})
        self.assertTrue(result["queued"])
        self.assertEqual(len(outbox.entries()), 1)

    def test_a_folder_that_was_trashed_meanwhile_is_replaced_by_the_top_level(self):
        shelf = self.folder("Shelf", parent="trash")
        gone = "11111111-1111-4111-8111-111111111111"
        outbox.add("In the bin", "pdf", b"a", shelf)
        outbox.add("Nowhere", "pdf", b"b", gone)
        self.reachable = "10.11.99.1"
        server.sync()
        self.assertEqual([(name, parent) for name, _, parent, _ in self.uploads], [("In the bin", ""), ("Nowhere", "")])

    def test_a_notebook_is_kept_whole_and_put_where_it_was_meant_to_go(self):
        shelf = self.folder("Shelf")
        doc = "22222222-2222-4222-8222-222222222222"
        files = {f"{doc}.metadata": json.dumps({"visibleName": "Sketch", "parent": shelf}).encode(), f"{doc}/page.rm": b"ink"}
        self.assertFalse(server.send("Sketch", "notebook", server._pack(files), shelf))
        self.reachable = "10.11.99.1"
        server.sync()
        kind, delivered = self.uploads[0]
        self.assertEqual(kind, "notebook")
        self.assertEqual(delivered[f"{doc}/page.rm"], b"ink")
        self.assertEqual(json.loads(delivered[f"{doc}.metadata"])["parent"], shelf)

    def test_a_document_that_keeps_failing_stops_being_retried_until_a_sync_is_asked_for(self):
        outbox.add("Stubborn", "pdf", b"a")
        self.reachable = "10.11.99.1"
        self.fail = RuntimeError("rejected")
        for _ in range(outbox.MAX_TRIES + 2):
            result = server.sync(wait=False)
        entry = server.outbox_json()["items"][0]
        self.assertTrue(entry["stuck"])
        self.assertIn("rejected", entry["error"])
        self.assertEqual(outbox.entries()[0]["tries"], outbox.MAX_TRIES)  # the background stopped trying
        self.assertNotIn("sent", result)
        self.assertEqual(outbox.waiting(), 0)
        self.fail = None
        self.assertEqual(server.sync()["sent"], 1)  # a sync that was asked for tries again

    def test_the_tablet_going_away_during_delivery_is_not_counted_against_the_document(self):
        outbox.add("Patient", "pdf", b"a")
        self.reachable = "10.11.99.1"

        def drop(*args, **kwargs):
            self.reachable = None
            raise OSError("connection lost")

        with patch.object(tablet, "upload", drop):
            server.sync()
        self.assertEqual((outbox.entries()[0]["tries"], outbox.entries()[0]["error"]), (0, ""))

    def test_something_waiting_can_be_removed(self):
        entry = outbox.add("Never mind", "pdf", b"a")
        result = server.change("outbox-remove", {"id": entry["id"]})
        self.assertEqual(result, {"ok": True, "items": []})
        with self.assertRaises(server.UserError):
            server.change("outbox-remove", {"id": "../config"})

    def test_changes_other_than_sending_still_need_the_tablet(self):
        with self.assertRaises(server.UserError):
            server.change("folder", {"name": "New", "parent": ""})
        self.assertEqual(outbox.entries(), [])


if __name__ == "__main__":
    unittest.main()
