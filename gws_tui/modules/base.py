from __future__ import annotations

from abc import ABC, abstractmethod

from gws_tui.client import GwsClient
from gws_tui.models import Record


class WorkspaceModule(ABC):
    """Interface implemented by each workspace module."""

    id: str
    title: str
    description: str
    columns: tuple[str, ...]
    empty_message: str = "No records found."

    @abstractmethod
    def fetch_records(self, client: GwsClient) -> list[Record]:
        raise NotImplementedError

    @abstractmethod
    def fetch_detail(self, client: GwsClient, record: Record) -> str:
        raise NotImplementedError
