"""Tracing a drawing into pen strokes, and writing them as a notebook page."""
import io
import json
import unittest

from PIL import Image, ImageDraw

from openmarker import ink, scene


def drawing() -> Image.Image:
    image = Image.new("L", (400, 500), 255)
    draw = ImageDraw.Draw(image)
    draw.line((40, 60, 360, 60), fill=0, width=3)
    draw.ellipse((120, 160, 280, 320), outline=0, width=4)
    draw.rectangle((150, 380, 250, 460), fill=0)  # a solid area, to be hatched
    return image


class TraceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.strokes = ink.trace(drawing())

    def test_strokes_stay_on_the_page(self):
        self.assertGreater(len(self.strokes), 3)
        for stroke in self.strokes:
            self.assertGreaterEqual(len(stroke.points), 2)
            self.assertTrue((abs(stroke.points[:, 0]) <= ink.PAPER[0] / 2).all())
            self.assertTrue(((stroke.points[:, 1] >= 0) & (stroke.points[:, 1] <= ink.PAPER[1])).all())

    def test_blank_picture_gives_no_strokes(self):
        self.assertEqual(ink.trace(Image.new("L", (200, 200), 255)), [])

    def test_page_file_reads_back(self):
        page = scene.read(io.BytesIO(ink.page_file(self.strokes)))
        self.assertEqual(len(page.strokes), len(self.strokes))
        self.assertEqual(page.paper, (float(ink.PAPER[0]), float(ink.PAPER[1])))
        self.assertTrue(all(stroke.tool == "FINELINER" for stroke in page.strokes))

    def test_notebook_has_the_files_the_tablet_expects(self):
        doc, files = ink.notebook_files("Test drawing", self.strokes, parent="")
        metadata = json.loads(files[f"{doc}.metadata"])
        content = json.loads(files[f"{doc}.content"])
        self.assertEqual(metadata["visibleName"], "Test drawing")
        self.assertEqual(metadata["type"], "DocumentType")
        page_id = content["cPages"]["pages"][0]["id"]
        self.assertIn(f"{doc}/{page_id}.rm", files)


if __name__ == "__main__":
    unittest.main()
