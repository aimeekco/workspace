from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class GwsProfile:
    name: str
    config_dir: str


@dataclass(slots=True)
class GwsProfileDiagnostic:
    name: str
    config_dir: str
    client_config_exists: bool
    encrypted_credentials_exists: bool
    has_refresh_token: bool
    project_id: str = ""
    status: str = ""
    detail: str = ""
    probe_ok: bool = False
    probe_message: str = ""


@dataclass(slots=True)
class WorkspaceSettings:
    module_sync: dict[str, tuple[str, ...]]


def discover_profiles(cwd: Path | None = None) -> tuple[list[GwsProfile], str | None]:
    cwd = cwd or Path.cwd()
    explicit_profile = os.environ.get("GWS_TUI_PROFILE", "").strip() or None
    explicit_config_dir = os.environ.get("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", "").strip() or None

    ordered: list[GwsProfile] = []
    by_name: dict[str, GwsProfile] = {}
    by_dir: dict[str, GwsProfile] = {}

    def add_profile(name: str, config_dir: str) -> None:
        normalized_name = name.strip()
        if not normalized_name:
            return
        resolved_dir = str(Path(config_dir).expanduser())
        existing = by_dir.get(resolved_dir)
        if existing is not None:
            by_name[normalized_name] = existing
            return
        profile = GwsProfile(normalized_name, resolved_dir)
        ordered.append(profile)
        by_name[normalized_name] = profile
        by_dir[resolved_dir] = profile

    profiles_file = resolve_profiles_file(cwd)
    file_default: str | None = None
    if profiles_file is not None:
        file_profiles, file_default = load_profiles_file(profiles_file)
        for profile in file_profiles:
            add_profile(profile.name, profile.config_dir)

    default_dir = str((Path.home() / ".config" / "gws").expanduser())
    add_profile("default", default_dir)

    config_root = Path.home() / ".config"
    for candidate in sorted(config_root.glob("gws-*")):
        if not candidate.is_dir():
            continue
        suffix = candidate.name.removeprefix("gws-").strip()
        if suffix:
            add_profile(suffix, str(candidate))

    if explicit_config_dir:
        resolved_dir = str(Path(explicit_config_dir).expanduser())
        matched = by_dir.get(resolved_dir)
        if matched is not None:
            if explicit_profile and explicit_profile not in by_name:
                by_name[explicit_profile] = matched
            return ordered, explicit_profile or matched.name
        add_profile(_derive_profile_name(resolved_dir, by_name), resolved_dir)
        matched = by_dir.get(resolved_dir)
        if matched is not None:
            if explicit_profile and explicit_profile not in by_name:
                by_name[explicit_profile] = matched
            return ordered, explicit_profile or matched.name

    if explicit_profile and explicit_profile in by_name:
        return ordered, explicit_profile

    if file_default and file_default in by_name:
        return ordered, file_default

    return ordered, ordered[0].name if ordered else None


def resolve_profiles_file(cwd: Path) -> Path | None:
    explicit = os.environ.get("GWS_TUI_PROFILES_FILE", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.exists() else None
    candidate = cwd / ".gws_tui_profiles.json"
    return candidate if candidate.exists() else None


def load_profiles_file(path: Path) -> tuple[list[GwsProfile], str | None]:
    payload = json.loads(path.read_text())
    default_name = payload.get("default")
    raw_profiles = payload.get("profiles", {})
    profiles: list[GwsProfile] = []
    if isinstance(raw_profiles, dict):
        for name, config_dir in raw_profiles.items():
            if not isinstance(name, str) or not isinstance(config_dir, str):
                continue
            profiles.append(GwsProfile(name=name.strip(), config_dir=config_dir.strip()))
    return profiles, default_name.strip() if isinstance(default_name, str) and default_name.strip() else None


def load_workspace_settings(cwd: Path | None = None) -> WorkspaceSettings:
    cwd = cwd or Path.cwd()
    profiles_file = resolve_profiles_file(cwd)
    if profiles_file is None:
        return WorkspaceSettings(module_sync={})
    try:
        payload = json.loads(profiles_file.read_text())
    except (OSError, json.JSONDecodeError):
        return WorkspaceSettings(module_sync={})
    module_sync: dict[str, tuple[str, ...]] = {}
    raw_module_sync = payload.get("module_sync", {})
    if isinstance(raw_module_sync, dict):
        for module_id, profile_names in raw_module_sync.items():
            if not isinstance(module_id, str) or not isinstance(profile_names, list):
                continue
            ordered_names: list[str] = []
            seen_names: set[str] = set()
            for profile_name in profile_names:
                if not isinstance(profile_name, str):
                    continue
                normalized = profile_name.strip()
                if not normalized or normalized in seen_names:
                    continue
                seen_names.add(normalized)
                ordered_names.append(normalized)
            if ordered_names:
                module_sync[module_id.strip()] = tuple(ordered_names)
    return WorkspaceSettings(module_sync=module_sync)


def _derive_profile_name(config_dir: str, by_name: dict[str, GwsProfile]) -> str:
    path = Path(config_dir).expanduser()
    stem = path.name.strip() or "profile"
    if stem == "gws":
        candidate = "default"
    elif stem.startswith("gws-") and stem[4:].strip():
        candidate = stem[4:].strip()
    else:
        candidate = stem
    if candidate not in by_name:
        return candidate
    index = 2
    while f"{candidate}-{index}" in by_name:
        index += 1
    return f"{candidate}-{index}"


def inspect_profile_local(profile: GwsProfile) -> GwsProfileDiagnostic:
    config_dir = Path(profile.config_dir).expanduser()
    client_config = config_dir / "client_secret.json"
    encrypted_credentials = config_dir / "credentials.enc"
    client_config_exists = client_config.exists()
    encrypted_credentials_exists = encrypted_credentials.exists()

    if not client_config_exists:
        status = "Needs OAuth client"
        detail = "Missing client_secret.json"
    elif encrypted_credentials_exists:
        status = "Checking..."
        detail = "Saved credentials detected; running live auth check"
    else:
        status = "Login required"
        detail = "OAuth client present, but no saved credentials"

    return GwsProfileDiagnostic(
        name=profile.name,
        config_dir=str(config_dir),
        client_config_exists=client_config_exists,
        encrypted_credentials_exists=encrypted_credentials_exists,
        has_refresh_token=False,
        status=status,
        detail=detail,
    )


def inspect_profile(profile: GwsProfile, binary: str = "gws") -> GwsProfileDiagnostic:
    diagnostic = inspect_profile_local(profile)
    config_dir = Path(profile.config_dir).expanduser()

    env = dict(os.environ)
    env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = str(config_dir)
    result = subprocess.run(
        [binary, "auth", "status"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or "Unable to read auth status"
        diagnostic.detail = stderr
        return diagnostic

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        diagnostic.detail = "Invalid auth status response"
        return diagnostic

    apply_status_payload(diagnostic, payload)
    if diagnostic.client_config_exists and diagnostic.has_refresh_token:
        probe_result = subprocess.run(
            [binary, "calendar", "calendarList", "list", "--params", '{"maxResults":1}'],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if probe_result.returncode == 0:
            diagnostic.probe_ok = True
            diagnostic.probe_message = "calendar probe ok"
            diagnostic.status = "Ready"
            diagnostic.detail = "Authenticated and request probe succeeded"
        else:
            diagnostic.probe_ok = False
            diagnostic.probe_message = extract_error_message(probe_result.stderr, "Calendar probe failed")
            diagnostic.status = "Request failed"
            diagnostic.detail = diagnostic.probe_message
    return diagnostic


def apply_status_payload(diagnostic: GwsProfileDiagnostic, payload: dict[str, Any]) -> None:
    diagnostic.client_config_exists = bool(payload.get("client_config_exists", diagnostic.client_config_exists))
    diagnostic.encrypted_credentials_exists = bool(
        payload.get("encrypted_credentials_exists", diagnostic.encrypted_credentials_exists)
    )
    diagnostic.has_refresh_token = bool(payload.get("has_refresh_token", False))
    diagnostic.project_id = str(payload.get("project_id", "") or "")

    if diagnostic.client_config_exists and diagnostic.has_refresh_token:
        diagnostic.status = "Ready"
        diagnostic.detail = "Authenticated"
        return
    if diagnostic.client_config_exists:
        diagnostic.status = "Login required"
        diagnostic.detail = "OAuth client present, but no refresh token"
        return
    diagnostic.status = "Needs OAuth client"
    diagnostic.detail = "Missing client_secret.json"


def extract_error_message(raw: str, fallback: str) -> str:
    text = raw.strip()
    if not text:
        return fallback
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    error = payload.get("error", {})
    message = error.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return text
