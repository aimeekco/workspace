from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gws_tui.env import load_env_file


class EnvLoaderTest(unittest.TestCase):
    def test_load_env_file_populates_unset_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text("GEMINI_API_KEY=test-key\nGEMINI_MODEL=gemini-2.0-flash\n")

            with patch.dict(os.environ, {}, clear=True):
                load_env_file(env_path)

                self.assertEqual(os.environ["GEMINI_API_KEY"], "test-key")
                self.assertEqual(os.environ["GEMINI_MODEL"], "gemini-2.0-flash")

    def test_load_env_file_does_not_override_existing_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text("GEMINI_API_KEY=file-key\n")

            with patch.dict(os.environ, {"GEMINI_API_KEY": "existing-key"}, clear=True):
                load_env_file(env_path)

                self.assertEqual(os.environ["GEMINI_API_KEY"], "existing-key")


if __name__ == "__main__":
    unittest.main()
