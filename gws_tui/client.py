from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Callable


class GwsError(RuntimeError):
    """Raised when a gws command fails."""

    def __init__(self, command: list[str], message: str) -> None:
        self.command = command
        self.message = message
        super().__init__(message)


@dataclass(slots=True)
class GwsCommandEvent:
    """Lifecycle event for a gws command."""

    command: list[str]
    status: str
    detail: str = ""


@dataclass(slots=True)
class GwsClient:
    """Thin wrapper around the gws CLI."""

    binary: str = "gws"
    observer: Callable[[GwsCommandEvent], None] | None = None
    config_dir: str | None = None

    def with_config_dir(self, config_dir: str | None) -> "GwsClient":
        return GwsClient(binary=self.binary, observer=self.observer, config_dir=config_dir)

    def run(
        self,
        service: str,
        *segments: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        page_all: bool = False,
        page_limit: int = 5,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        command = [self.binary, service, *segments]
        if params is not None:
            command.extend(["--params", json.dumps(params)])
        if body is not None:
            command.extend(["--json", json.dumps(body)])
        if page_all:
            command.extend(["--page-all", "--page-limit", str(page_limit)])

        self._emit(command, "start")

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            env=self._command_env(),
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() or "gws command failed"
            self._emit(command, "error", stderr)
            raise GwsError(command, stderr)

        stdout = result.stdout.strip()
        if not stdout:
            self._emit(command, "ok", "empty")
            return {}

        try:
            if page_all:
                parsed = [json.loads(line) for line in stdout.splitlines() if line.strip()]
                self._emit(command, "ok", f"{len(parsed)} pages")
                return parsed
            parsed = json.loads(stdout)
            self._emit(command, "ok")
            return parsed
        except json.JSONDecodeError as exc:
            self._emit(command, "error", f"Invalid JSON response: {exc}")
            raise GwsError(command, f"Invalid JSON response: {exc}") from exc

    def _emit(self, command: list[str], status: str, detail: str = "") -> None:
        if self.observer is None:
            return
        self.observer(GwsCommandEvent(command=list(command), status=status, detail=detail))

    def _command_env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.config_dir:
            env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = self.config_dir
        return env
