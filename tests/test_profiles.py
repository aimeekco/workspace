from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gws_tui.profiles import GwsProfile, discover_profiles, inspect_profile, inspect_profile_local, load_workspace_settings


class ProfilesTest(unittest.TestCase):
    def test_discover_profiles_reads_json_file_and_auto_detects_gws_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_root = root / ".config"
            config_root.mkdir()
            (config_root / "gws").mkdir()
            (config_root / "gws-personal").mkdir()
            profiles_file = root / "workspace" / ".gws_tui_profiles.json"
            profiles_file.parent.mkdir()
            profiles_file.write_text(
                json.dumps(
                    {
                        "default": "work",
                        "profiles": {
                            "work": str(config_root / "gws-work"),
                        },
                    }
                )
            )

            with patch("gws_tui.profiles.Path.home", return_value=root):
                profiles, default_name = discover_profiles(profiles_file.parent)

        names = [profile.name for profile in profiles]
        self.assertIn("work", names)
        self.assertIn("personal", names)
        self.assertEqual(default_name, "work")

    def test_inspect_profile_reports_ready_when_refresh_token_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            (config_dir / "client_secret.json").write_text("{}")
            profile = GwsProfile(name="work", config_dir=str(config_dir))
            status_completed = subprocess.CompletedProcess(
                args=["gws", "auth", "status"],
                returncode=0,
                stdout=json.dumps(
                    {
                        "client_config_exists": True,
                        "encrypted_credentials_exists": True,
                        "has_refresh_token": True,
                        "project_id": "workspace-123",
                    }
                ),
                stderr="",
            )
            probe_completed = subprocess.CompletedProcess(
                args=["gws", "calendar", "calendarList", "list"],
                returncode=0,
                stdout='{"items":[]}',
                stderr="",
            )

            with patch("subprocess.run", side_effect=[status_completed, probe_completed]):
                diagnostic = inspect_profile(profile)

        self.assertEqual(diagnostic.status, "Ready")
        self.assertTrue(diagnostic.client_config_exists)
        self.assertTrue(diagnostic.encrypted_credentials_exists)
        self.assertTrue(diagnostic.has_refresh_token)
        self.assertTrue(diagnostic.probe_ok)
        self.assertEqual(diagnostic.project_id, "workspace-123")

    def test_inspect_profile_reports_missing_client_secret_without_auth(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = GwsProfile(name="school", config_dir=temp_dir)
            completed = subprocess.CompletedProcess(
                args=["gws", "auth", "status"],
                returncode=1,
                stdout="",
                stderr="No OAuth client configured.",
            )

            with patch("subprocess.run", return_value=completed):
                diagnostic = inspect_profile(profile)

        self.assertEqual(diagnostic.status, "Needs OAuth client")
        self.assertFalse(diagnostic.client_config_exists)
        self.assertIn("No OAuth client configured.", diagnostic.detail)

    def test_inspect_profile_local_marks_saved_credentials_as_checking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            (config_dir / "client_secret.json").write_text("{}")
            (config_dir / "credentials.enc").write_text("encrypted")
            profile = GwsProfile(name="personal", config_dir=str(config_dir))

            diagnostic = inspect_profile_local(profile)

        self.assertEqual(diagnostic.status, "Checking...")
        self.assertTrue(diagnostic.client_config_exists)
        self.assertTrue(diagnostic.encrypted_credentials_exists)
        self.assertIn("running live auth check", diagnostic.detail.lower())

    def test_inspect_profile_local_marks_missing_saved_credentials_as_login_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            (config_dir / "client_secret.json").write_text("{}")
            profile = GwsProfile(name="personal", config_dir=str(config_dir))

            diagnostic = inspect_profile_local(profile)

        self.assertEqual(diagnostic.status, "Login required")
        self.assertTrue(diagnostic.client_config_exists)
        self.assertFalse(diagnostic.encrypted_credentials_exists)

    def test_inspect_profile_reports_request_failure_when_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            (config_dir / "client_secret.json").write_text("{}")
            profile = GwsProfile(name="school", config_dir=str(config_dir))
            status_completed = subprocess.CompletedProcess(
                args=["gws", "auth", "status"],
                returncode=0,
                stdout=json.dumps(
                    {
                        "client_config_exists": True,
                        "encrypted_credentials_exists": True,
                        "has_refresh_token": True,
                    }
                ),
                stderr="",
            )
            probe_completed = subprocess.CompletedProcess(
                args=["gws", "calendar", "calendarList", "list"],
                returncode=1,
                stdout="",
                stderr=json.dumps(
                    {
                        "error": {
                            "message": "Authentication failed: no native root CA certificates found",
                        }
                    }
                ),
            )

            with patch("subprocess.run", side_effect=[status_completed, probe_completed]):
                diagnostic = inspect_profile(profile)

        self.assertEqual(diagnostic.status, "Request failed")
        self.assertFalse(diagnostic.probe_ok)
        self.assertIn("no native root CA certificates found", diagnostic.detail)

    def test_discover_profiles_keeps_default_bound_to_main_config_when_env_profile_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_root = root / ".config"
            default_dir = config_root / "gws"
            school_dir = config_root / "gws-school"
            default_dir.mkdir(parents=True)
            school_dir.mkdir(parents=True)

            with (
                patch("gws_tui.profiles.Path.home", return_value=root),
                patch.dict(os.environ, {"GOOGLE_WORKSPACE_CLI_CONFIG_DIR": str(school_dir)}, clear=False),
            ):
                profiles, default_name = discover_profiles(root)

        profiles_by_name = {profile.name: profile.config_dir for profile in profiles}
        self.assertEqual(profiles_by_name["default"], str(default_dir))
        self.assertEqual(profiles_by_name["school"], str(school_dir))
        self.assertEqual(default_name, "school")

    def test_load_workspace_settings_reads_module_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profiles_file = root / ".gws_tui_profiles.json"
            profiles_file.write_text(
                json.dumps(
                    {
                        "profiles": {
                            "personal": "~/.config/gws",
                            "school": "~/.config/gws-school",
                        },
                        "module_sync": {
                            "today": ["personal", "school", "school"],
                            "calendar": ["school"],
                            "tasks": ["personal"],
                        },
                    }
                )
            )

            settings = load_workspace_settings(root)

        self.assertEqual(settings.module_sync["today"], ("personal", "school"))
        self.assertEqual(settings.module_sync["calendar"], ("school",))
        self.assertEqual(settings.module_sync["tasks"], ("personal",))


if __name__ == "__main__":
    unittest.main()
