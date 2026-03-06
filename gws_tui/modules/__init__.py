from .base import WorkspaceModule
from .calendar import CalendarModule
from .docs import DocsModule
from .gmail import GmailModule


def built_in_modules() -> list[WorkspaceModule]:
    return [
        GmailModule(),
        CalendarModule(),
        DocsModule(),
    ]


__all__ = ["WorkspaceModule", "CalendarModule", "DocsModule", "GmailModule", "built_in_modules"]
