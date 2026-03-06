from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from gws_tui.client import GwsClient, GwsError


class GwsClientTest(unittest.TestCase):
    def test_run_returns_parsed_json(self) -> None:
        client = GwsClient()
        completed = subprocess.CompletedProcess(
            args=["gws"],
            returncode=0,
            stdout='{"items": [{"id": "abc"}]}',
            stderr="",
        )
        with patch("subprocess.run", return_value=completed) as run_mock:
            result = client.run("calendar", "calendarList", "list", params={"maxResults": 1})

        self.assertEqual(result["items"][0]["id"], "abc")
        self.assertIn("--params", run_mock.call_args.args[0])

    def test_run_raises_for_cli_error(self) -> None:
        client = GwsClient()
        completed = subprocess.CompletedProcess(
            args=["gws"],
            returncode=1,
            stdout="",
            stderr="auth failed",
        )
        with patch("subprocess.run", return_value=completed):
            with self.assertRaises(GwsError) as ctx:
                client.run("gmail", "users", "messages", "list")

        self.assertIn("auth failed", str(ctx.exception))

    def test_run_parses_ndjson_when_page_all_is_enabled(self) -> None:
        client = GwsClient()
        completed = subprocess.CompletedProcess(
            args=["gws"],
            returncode=0,
            stdout='{"items":[1]}\n{"items":[2]}',
            stderr="",
        )
        with patch("subprocess.run", return_value=completed):
            result = client.run("calendar", "events", "list", page_all=True)

        self.assertEqual(result, [{"items": [1]}, {"items": [2]}])


if __name__ == "__main__":
    unittest.main()
