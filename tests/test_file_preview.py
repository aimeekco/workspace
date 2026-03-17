from __future__ import annotations

import unittest

from gws_tui.file_preview import (
    image_dimensions,
    is_text_previewable,
    render_binary_preview,
    render_text_preview,
    render_unavailable_preview,
)


class FilePreviewTest(unittest.TestCase):
    def test_is_text_previewable_by_mime_and_extension(self) -> None:
        self.assertTrue(is_text_previewable("text/plain", "notes.bin"))
        self.assertTrue(is_text_previewable("application/octet-stream", "notes.md"))
        self.assertFalse(is_text_previewable("application/octet-stream", "archive.bin"))

    def test_render_text_preview_truncates_long_content(self) -> None:
        payload = ("a" * 60_000).encode("utf-8")

        preview = render_text_preview(payload)

        self.assertIn("truncated", preview)
        self.assertLess(len(preview), 60_000)

    def test_image_dimensions_detects_png_size(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 8) + (16).to_bytes(4, "big") + (9).to_bytes(4, "big")

        dimensions = image_dimensions(png, "image/png")

        self.assertEqual(dimensions, (16, 9))

    def test_render_binary_preview_shows_hex_for_unknown_mime(self) -> None:
        preview = render_binary_preview(
            name="archive.bin",
            mime_type="application/octet-stream",
            data=b"\x00\x01\x02\x03",
            metadata={"size": "4"},
        )

        self.assertIn("Binary preview unavailable", preview)
        self.assertIn("00 01 02 03", preview)

    def test_render_unavailable_preview_includes_reason(self) -> None:
        preview = render_unavailable_preview(
            name="big.pdf",
            mime_type="application/pdf",
            reason="File is too large",
            metadata={"size": "9000000"},
        )

        self.assertIn("File is too large", preview)
        self.assertIn("big.pdf", preview)


if __name__ == "__main__":
    unittest.main()
