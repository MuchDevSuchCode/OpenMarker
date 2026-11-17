"""Regression coverage for library resilience and API request data types."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openmarker import library, server


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


class RequestTests(unittest.TestCase):
    def post(self, raw):
        handler = object.__new__(server.Handler)
        handler.headers = {"Content-Length": str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        handler.path = "/api/config"
        handler._local_only = lambda: True
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


if __name__ == "__main__":
    unittest.main()
