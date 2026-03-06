from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any


class GwsError(RuntimeError):
    """Raised when a gws command fails."""

    def __init__(self, command: list[str], message: str) -> None:
        self.command = command
        self.message = message
        super().__init__(message)


@dataclass(slots=True)
class GwsClient:
    """Thin wrapper around the gws CLI."""

    binary: str = "gws"

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

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() or "gws command failed"
            raise GwsError(command, stderr)

        stdout = result.stdout.strip()
        if not stdout:
            return {}

        try:
            if page_all:
                return [json.loads(line) for line in stdout.splitlines() if line.strip()]
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise GwsError(command, f"Invalid JSON response: {exc}") from exc
