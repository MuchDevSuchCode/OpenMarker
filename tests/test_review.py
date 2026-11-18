"""Regression coverage for library resilience and API request data types."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openmarker import history, library, server, tablet


class LibraryTests(unittest.TestCase):
    def test_invalid_dates_and_records_do_not_hide_valid_documents(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            (root / "broken.metadata").write_text("{")
            (root / "array.metadata").write_text("[]")
            (root / "valid.metadata").write_text(json.dumps({"visibleName": None, "lastModified": "bad"}))
            (root / "valid.content").write_text(json.dumps({"tags": [None, {"name": 123}], "pages": None}))
            items = library.load(root)
            self.assertEqual(list(items), ["valid"])
            self.assertEqual(items["valid"].name, "Untitled")
            self.assertIsNone(items["valid"].modified)
            self.assertEqual(items["valid"].tags, [])

    def test_text_fields_reject_objects(self):
        for value in (None, 123, [], {}):
            with self.assertRaises(server.UserError):
                server._text({"name": value}, "name")


class HistoryPreviewTests(unittest.TestCase):
    def test_preview_uses_the_snapshot_page_order_and_pdf(self):
        doc = "00000000-0000-0000-0000-000000000001"
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            mirror = root / "mirror"
            mirror.mkdir()
            content = mirror / f"{doc}.content"
            pdf = mirror / f"{doc}.pdf"
            content.write_text(json.dumps({
                "pages": ["kept", "deleted", "inserted"],
                "redirectionPageMap": [2, 0, -1],
            }))
            pdf.write_bytes(b"historical PDF")
            with patch.object(history, "root", return_value=root / "history"):
                snapshot = history.record(mirror, tablet.local_hashes(mirror))
                content.write_text(json.dumps({"pages": ["kept", "new"], "redirectionPageMap": [0, 1]}))
                pdf.write_bytes(b"current PDF")
                version, = history.versions(doc)
                self.assertEqual(version["pages"], ["kept", "deleted", "inserted"])
                self.assertEqual(version["source_pages"], {"kept": 2, "deleted": 0})
                self.assertTrue(version["has_pdf"])
                historical_pdf = server._preview_pdf(mirror, doc, snapshot.stem)
                self.assertEqual(historical_pdf.read_bytes(), b"historical PDF")
                with patch.object(server, "store_dir", return_value=(mirror, "mirror")), patch.object(
                    server, "_source_size", return_value=(500, 800)
                ) as source_size:
                    server.page_svg(doc, "kept", {"at": [snapshot.stem], "over": ["2"]})
                source_size.assert_called_once_with(str(historical_pdf), historical_pdf.stat().st_mtime, 2)


class RequestTests(unittest.TestCase):
    def post(self, raw):
        handler = object.__new__(server.Handler)
        handler.headers = {"Content-Length": str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        handler.path = "/api/config"
        handler._enter = lambda post: True
        result = []
        handler._json = lambda data, code=200: result.append((code, data))
        handler.do_POST()
        return result[0]

    def test_non_object_or_invalid_json_is_rejected(self):
        for raw in (b"[]", b"null", b"{invalid"):
            self.assertEqual(self.post(raw)[0], 400)

    @patch.object(server.workflow, "update_config")
    def test_string_boolean_does_not_enable_sync(self, update):
        _, response = self.post(b'{"auto_sync":"false"}')
        self.assertFalse(response["ok"])
        update.assert_not_called()

    @patch.object(server.workflow, "update_config")
    def test_real_boolean_is_preserved(self, update):
        _, response = self.post(b'{"auto_sync":false}')
        self.assertTrue(response["ok"])
        update.assert_called_once_with(auto_sync=False)


class SettingsTests(unittest.TestCase):
    def test_values_are_trimmed_and_empty_ones_removed(self):
        changes = server.settings_changes({
            "wifi_host": " 192.168.1.50 ", "tablet_name": "my-tablet.local", "calendar": "",
            "markdown_dir": "", "markdown_tag": " export ",
        })
        self.assertEqual(changes, {
            "wifi_host": "192.168.1.50", "tablet_name": "my-tablet", "calendar": None,
            "markdown_dir": None, "markdown_tag": "export",
        })

    def test_only_settings_that_were_sent_change(self):
        self.assertEqual(server.settings_changes({"auto_sync": True}), {"auto_sync": True})
        self.assertEqual(server.settings_changes({"hooks": [{"run": "x"}], "last_sync": "x"}), {})

    def test_bad_values_are_refused(self):
        for body in (
            {"wifi_host": "not an address"},
            {"tablet_name": "two words"},
            {"tablet_name": 5},
            {"calendar": "line\nbreak"},
            {"markdown_dir": "relative/folder"},
            {"markdown_tag": "x" * 101},
        ):
            with self.assertRaises(server.UserError, msg=body):
                server.settings_changes(body)

    def test_removed_settings_leave_the_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "config.json"
            with patch.object(server.workflow, "config_path", return_value=path):
                server.workflow.update_config(calendar="a.ics", auto_sync=True)
                server.workflow.update_config(calendar=None, markdown_tag=None)
                self.assertEqual(json.loads(path.read_text()), {"auto_sync": True})
                self.assertEqual(server.settings_json()["calendar"], "")


if __name__ == "__main__":
    unittest.main()
