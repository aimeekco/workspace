from .base import WorkspaceModule
from .calendar import CalendarModule
from .drive import DriveModule
from .docs import DocsModule
from .gmail import GmailModule
from .sheets import SheetsModule


def built_in_modules() -> list[WorkspaceModule]:
    return [
        GmailModule(),
        CalendarModule(),
        DriveModule(),
        SheetsModule(),
        DocsModule(),
    ]

__all__ = [
    "WorkspaceModule",
    "CalendarModule",
    "DriveModule",
    "DocsModule",
    "GmailModule",
    "SheetsModule",
    "built_in_modules",
]
