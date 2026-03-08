from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from datetime import datetime
from datetime import timezone
from typing import Any

from gws_tui.client import GwsClient
from gws_tui.models import Record
from gws_tui.modules.base import WorkspaceModule


def parse_task_timestamp(value: str) -> str:
    if not value:
        return "No date"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%b %d")
    except ValueError:
        return value


def parse_due_date(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    parsed = date.fromisoformat(cleaned)
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def task_sort_key(task: dict[str, Any]) -> tuple[int, str, str]:
    status = task.get("status", "needsAction")
    due = task.get("due", "")
    updated = task.get("updated", "")
    return (0 if status != "completed" else 1, due or "9999", updated or "")


class TasksModule(WorkspaceModule):
    id = "tasks"
    title = "Tasks"
    description = "Google Tasks across your task lists."
    columns = ("Task", "List", "Due")
    empty_message = "No Google Tasks found."

    def __init__(self) -> None:
        self.selected_tasklist_id = ""
        self.selected_tasklist_name = "All Tasks"
        self.tasklists: list[dict[str, str]] = []

    def badge(self) -> str:
        return "Tasks"

    def loading_message(self) -> str:
        return "Loading task lists and tasks..."

    def empty_hint(self) -> str:
        return "Add tasks in Google Tasks, then refresh to sync them here."

    def list_label(self) -> str:
        return self.selected_tasklist_name

    def subtitle(self) -> str:
        return f"Task list: {self.selected_tasklist_name.lower()}"

    def tasklist_options(self, client: GwsClient) -> list[dict[str, str]]:
        response = client.run(
            "tasks",
            "tasklists",
            "list",
            params={"maxResults": 100},
            page_all=True,
        )
        tasklists = self._collect_items(response, "items")
        options = [{"id": "", "name": "All Tasks"}]
        for tasklist in tasklists:
            options.append(
                {
                    "id": tasklist.get("id", ""),
                    "name": tasklist.get("title", "Untitled list"),
                }
            )
        self.tasklists = [option for option in options if option["name"]]
        if not any(option["id"] == self.selected_tasklist_id for option in self.tasklists):
            self.selected_tasklist_id = ""
            self.selected_tasklist_name = "All Tasks"
        else:
            current = next(option for option in self.tasklists if option["id"] == self.selected_tasklist_id)
            self.selected_tasklist_name = current["name"]
        return self.tasklists

    def set_tasklist(self, tasklist_id: str, tasklist_name: str) -> None:
        self.selected_tasklist_id = tasklist_id
        self.selected_tasklist_name = tasklist_name

    def default_create_tasklist_id(self) -> str:
        if self.selected_tasklist_id:
            return self.selected_tasklist_id
        if len(self.tasklists) > 1:
            return self.tasklists[1]["id"]
        return ""

    def default_create_tasklist_name(self) -> str:
        if self.selected_tasklist_id:
            return self.selected_tasklist_name
        if len(self.tasklists) > 1:
            return self.tasklists[1]["name"]
        return "No task list"

    def fetch_records(self, client: GwsClient) -> list[Record]:
        options = self.tasklist_options(client)
        selected_ids = {self.selected_tasklist_id} if self.selected_tasklist_id else None
        tasklists = [
            {"id": option["id"], "title": option["name"]}
            for option in options
            if option["id"] and (selected_ids is None or option["id"] in selected_ids)
        ]
        if not tasklists:
            return []

        with ThreadPoolExecutor(max_workers=4) as executor:
            task_pages = list(
                executor.map(lambda tasklist: self._fetch_tasklist_tasks(client, tasklist), tasklists)
            )

        records: list[Record] = []
        for tasklist, tasks in zip(tasklists, task_pages, strict=False):
            list_id = tasklist.get("id", "")
            list_title = tasklist.get("title", "Untitled list")
            for task in sorted(tasks, key=task_sort_key):
                if task.get("deleted") or task.get("hidden"):
                    continue
                title = task.get("title", "Untitled task")
                status = task.get("status", "needsAction")
                due = parse_task_timestamp(task.get("due", ""))
                updated = parse_task_timestamp(task.get("updated", ""))
                notes = (task.get("notes") or "").strip()
                preview_lines = [
                    f"Task: {title}",
                    f"List: {list_title}",
                    f"Status: {'Completed' if status == 'completed' else 'Open'}",
                    f"Due: {due}",
                    f"Updated: {updated}",
                ]
                if notes:
                    preview_lines.extend(["", notes])
                records.append(
                    Record(
                        key=f"{list_id}:{task.get('id', '')}",
                        columns=(title, list_title, due),
                        title=title,
                        subtitle=list_title,
                        preview="\n".join(preview_lines).strip(),
                        raw={
                            "task": task,
                            "task_id": task.get("id", ""),
                            "tasklist_id": list_id,
                            "tasklist_title": list_title,
                            "completed": status == "completed",
                        },
                    )
                )
        records.sort(key=lambda record: task_sort_key(record.raw["task"]))
        return records

    def fetch_detail(self, client: GwsClient, record: Record) -> str:
        task = client.run(
            "tasks",
            "tasks",
            "get",
            params={
                "tasklist": record.raw["tasklist_id"],
                "task": record.raw["task_id"],
            },
        )
        status = "Completed" if task.get("status") == "completed" else "Open"
        lines = [
            "Task Overview",
            "",
            f"Task: {task.get('title', record.title)}",
            f"List: {record.raw.get('tasklist_title', record.subtitle or 'Unknown list')}",
            f"Status: {status}",
            f"Due: {parse_task_timestamp(task.get('due', ''))}",
            f"Updated: {parse_task_timestamp(task.get('updated', ''))}",
        ]
        completed = task.get("completed")
        if completed:
            lines.append(f"Completed: {parse_task_timestamp(completed)}")
        parent = task.get("parent")
        if parent:
            lines.append(f"Parent task: {parent}")
        link = task.get("webViewLink")
        if link:
            lines.append(f"Link: {link}")
        notes = (task.get("notes") or "").strip()
        lines.extend(["", "Notes", "", notes or "(No notes)"])
        record.raw["task"] = task
        record.raw["completed"] = task.get("status") == "completed"
        return "\n".join(lines)

    def create_task(self, client: GwsClient, tasklist_id: str, title: str, notes: str = "", due_text: str = "") -> dict:
        body: dict[str, Any] = {"title": title}
        if notes.strip():
            body["notes"] = notes.strip()
        if due_text.strip():
            body["due"] = parse_due_date(due_text)
        return client.run(
            "tasks",
            "tasks",
            "insert",
            params={"tasklist": tasklist_id},
            body=body,
        )

    def update_task_status(self, client: GwsClient, record: Record, completed: bool) -> dict:
        body: dict[str, Any] = {
            "status": "completed" if completed else "needsAction",
        }
        if completed:
            body["completed"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        response = client.run(
            "tasks",
            "tasks",
            "patch",
            params={
                "tasklist": record.raw["tasklist_id"],
                "task": record.raw["task_id"],
            },
            body=body,
        )
        record.raw["completed"] = completed
        task = dict(record.raw.get("task", {}))
        task["status"] = body["status"]
        if completed:
            task["completed"] = body["completed"]
        else:
            task.pop("completed", None)
        record.raw["task"] = task
        return response

    def reset_state(self) -> None:
        self.selected_tasklist_id = ""
        self.selected_tasklist_name = "All Tasks"
        self.tasklists = []

    def _fetch_tasklist_tasks(self, client: GwsClient, tasklist: dict[str, Any]) -> list[dict[str, Any]]:
        response = client.run(
            "tasks",
            "tasks",
            "list",
            params={
                "tasklist": tasklist.get("id", ""),
                "maxResults": 100,
                "showCompleted": True,
                "showHidden": False,
                "showDeleted": False,
                "showAssigned": True,
            },
            page_all=True,
        )
        return self._collect_items(response, "items")

    def _collect_items(self, response: dict[str, Any] | list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        if isinstance(response, list):
            items: list[dict[str, Any]] = []
            for page in response:
                items.extend(page.get(key, []))
            return items
        return response.get(key, [])
