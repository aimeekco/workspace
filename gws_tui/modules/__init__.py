from .base import WorkspaceModule
from .calendar import CalendarModule
from .drive import DriveModule
from .docs import DocsModule
from .gmail import GmailModule
from .sheets import SheetsModule
from .tasks import TasksModule
from .today import TodayModule


def built_in_modules() -> list[WorkspaceModule]:
    return [
        TodayModule(),
        GmailModule(),
        CalendarModule(),
        TasksModule(),
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
    "TasksModule",
    "TodayModule",
    "built_in_modules",
]
