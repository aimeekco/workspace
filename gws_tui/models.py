from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Record:
    """Normalized record rendered by module views."""

    key: str
    columns: tuple[str, ...]
    title: str
    subtitle: str = ""
    preview: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
