from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from gws_tui.client import GwsClient, GwsCommandEvent, GwsError


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

    def test_run_emits_observer_events(self) -> None:
        events: list[GwsCommandEvent] = []
        client = GwsClient(observer=events.append)
        completed = subprocess.CompletedProcess(
            args=["gws"],
            returncode=0,
            stdout='{"items": [{"id": "abc"}]}',
            stderr="",
        )
        with patch("subprocess.run", return_value=completed):
            client.run("calendar", "calendarList", "list")

        self.assertEqual([event.status for event in events], ["start", "ok"])
        self.assertEqual(events[0].command[:3], ["gws", "calendar", "calendarList"])

    def test_run_uses_profile_config_dir_in_environment(self) -> None:
        client = GwsClient(config_dir="/tmp/gws-work")
        completed = subprocess.CompletedProcess(
            args=["gws"],
            returncode=0,
            stdout="{}",
            stderr="",
        )
        with patch("subprocess.run", return_value=completed) as run_mock:
            client.run("drive", "files", "list")

        self.assertEqual(run_mock.call_args.kwargs["env"]["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"], "/tmp/gws-work")


if __name__ == "__main__":
    unittest.main()
