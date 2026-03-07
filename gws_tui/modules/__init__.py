from .base import WorkspaceModule
from .calendar import CalendarModule
from .drive import DriveModule
from .docs import DocsModule
from .gmail import GmailModule


def built_in_modules() -> list[WorkspaceModule]:
    return [
        GmailModule(),
        CalendarModule(),
        DriveModule(),
        DocsModule(),
    ]

__all__ = ["WorkspaceModule", "CalendarModule", "DriveModule", "DocsModule", "GmailModule", "built_in_modules"]
