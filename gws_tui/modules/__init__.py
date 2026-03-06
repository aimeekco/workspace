from .base import WorkspaceModule
from .calendar import CalendarModule
from .gmail import GmailModule


def built_in_modules() -> list[WorkspaceModule]:
    return [
        CalendarModule(),
        GmailModule(),
    ]


__all__ = ["WorkspaceModule", "CalendarModule", "GmailModule", "built_in_modules"]
