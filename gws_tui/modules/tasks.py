from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from datetime import datetime
from datetime import timezone
from typing import Any

from gws_tui.client import GwsClient
from gws_tui.models import Record
from gws_tui.modules.base import WorkspaceModule
from gws_tui.profiles import GwsProfile


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
        self.active_profile_name = "default"
        self.available_profiles: list[GwsProfile] = []
        self.synced_profile_names: tuple[str, ...] = ()

    def badge(self) -> str:
        return "Tasks"

    def loading_message(self) -> str:
        return "Loading task lists and tasks..."

    def empty_hint(self) -> str:
        return "Add tasks in Google Tasks, then refresh to sync them here."

    def configure_profiles(
        self,
        active_profile_name: str | None,
        available_profiles: list[GwsProfile],
        synced_profile_names: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self.active_profile_name = (active_profile_name or "default").strip() or "default"
        self.available_profiles = list(available_profiles)
        self.synced_profile_names = tuple(synced_profile_names or ())

    def list_label(self) -> str:
        return self.selected_tasklist_name

    def subtitle(self) -> str:
        return f"Task list: {self.selected_tasklist_name.lower()}"

    def tasklist_options(self, client: GwsClient) -> list[dict[str, str]]:
        target_profiles = self._target_profiles()
        if len(target_profiles) > 1:
            options = [{"id": "", "name": "All Tasks"}]
            for profile in target_profiles:
                profile_client = self._client_for_profile(client, profile.name)
                response = profile_client.run(
                    "tasks",
                    "tasklists",
                    "list",
                    params={"maxResults": 100},
                    page_all=True,
                )
                tasklists = self._collect_items(response, "items")
                for tasklist in tasklists:
                    tasklist_id = tasklist.get("id", "")
                    tasklist_name = tasklist.get("title", "Untitled list")
                    if not tasklist_id or not tasklist_name:
                        continue
                    options.append(
                        {
                            "id": self._tasklist_key(profile.name, tasklist_id),
                            "name": self._display_tasklist_name(tasklist_name, profile.name, True),
                            "profile_name": profile.name,
                            "tasklist_id": tasklist_id,
                            "tasklist_name": tasklist_name,
                        }
                    )
            self.tasklists = options
            self._sync_selected_tasklist()
            return self.tasklists
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
        self._sync_selected_tasklist()
        return self.tasklists

    def set_tasklist(self, tasklist_id: str, tasklist_name: str) -> None:
        self.selected_tasklist_id = tasklist_id
        self.selected_tasklist_name = tasklist_name

    def default_create_tasklist_id(self) -> str:
        if self.selected_tasklist_id:
            return self.selected_tasklist_id
        default_option = self._default_tasklist_option()
        if default_option is not None:
            return default_option["id"]
        return ""

    def default_create_tasklist_name(self) -> str:
        if self.selected_tasklist_id:
            return self.selected_tasklist_name
        default_option = self._default_tasklist_option()
        if default_option is not None:
            return default_option["name"]
        return "No task list"

    def fetch_records(self, client: GwsClient) -> list[Record]:
        options = self.tasklist_options(client)
        target_profiles = self._target_profiles()
        annotate_profile = len(target_profiles) > 1
        selected_ids = {self.selected_tasklist_id} if self.selected_tasklist_id else None
        tasklists = []
        for option in options:
            if not option["id"] or (selected_ids is not None and option["id"] not in selected_ids):
                continue
            tasklists.append(
                {
                    "key": option["id"],
                    "id": option.get("tasklist_id", option["id"]),
                    "title": option.get("tasklist_name", option["name"]),
                    "display_title": option["name"],
                    "profile_name": option.get("profile_name", ""),
                }
            )
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
            display_title = tasklist.get("display_title", list_title)
            profile_name = tasklist.get("profile_name", "")
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
                    f"List: {display_title if annotate_profile else list_title}",
                    f"Status: {'Completed' if status == 'completed' else 'Open'}",
                    f"Due: {due}",
                    f"Updated: {updated}",
                ]
                if notes:
                    preview_lines.extend(["", notes])
                records.append(
                    Record(
                        key=self._record_key(profile_name, list_id, str(task.get("id", "")), annotate_profile),
                        columns=(title, display_title if annotate_profile else list_title, due),
                        title=title,
                        subtitle=display_title if annotate_profile else list_title,
                        preview="\n".join(preview_lines).strip(),
                        raw={
                            "task": task,
                            "task_id": task.get("id", ""),
                            "tasklist_id": list_id,
                            "tasklist_title": list_title,
                            "profile_name": profile_name,
                            "completed": status == "completed",
                        },
                    )
                )
        records.sort(key=lambda record: task_sort_key(record.raw["task"]))
        return records

    def fetch_detail(self, client: GwsClient, record: Record) -> str:
        detail_client = self._client_for_profile(client, record.raw.get("profile_name"))
        task = detail_client.run(
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

    def create_task(
        self,
        client: GwsClient,
        tasklist_id: str,
        title: str,
        notes: str = "",
        due_text: str = "",
        profile_name: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {"title": title}
        if notes.strip():
            body["notes"] = notes.strip()
        if due_text.strip():
            body["due"] = parse_due_date(due_text)
        target_profile_name, resolved_tasklist_id = self._resolve_task_target(tasklist_id, profile_name)
        target_client = self._client_for_profile(client, target_profile_name)
        return target_client.run(
            "tasks",
            "tasks",
            "insert",
            params={"tasklist": resolved_tasklist_id},
            body=body,
        )

    def update_task_status(self, client: GwsClient, record: Record, completed: bool) -> dict:
        body: dict[str, Any] = {
            "status": "completed" if completed else "needsAction",
        }
        if completed:
            body["completed"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        target_client = self._client_for_profile(client, record.raw.get("profile_name"))
        response = target_client.run(
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

    def update_task(
        self,
        client: GwsClient,
        record: Record,
        title: str,
        notes: str = "",
        due_text: str = "",
    ) -> dict:
        cleaned_title = title.strip()
        if not cleaned_title:
            raise ValueError("Task title is required")
        body: dict[str, Any] = {
            "title": cleaned_title,
            "notes": notes.strip(),
        }
        if due_text.strip():
            body["due"] = parse_due_date(due_text)
        target_client = self._client_for_profile(client, record.raw.get("profile_name"))
        response = target_client.run(
            "tasks",
            "tasks",
            "patch",
            params={
                "tasklist": record.raw["tasklist_id"],
                "task": record.raw["task_id"],
            },
            body=body,
        )
        task = dict(record.raw.get("task", {}))
        task["title"] = body["title"]
        task["notes"] = body["notes"]
        if "due" in body:
            task["due"] = body["due"]
        record.title = cleaned_title
        record.raw["task"] = task
        return response

    def reset_state(self) -> None:
        self.selected_tasklist_id = ""
        self.selected_tasklist_name = "All Tasks"
        self.tasklists = []

    def _fetch_tasklist_tasks(self, client: GwsClient, tasklist: dict[str, Any]) -> list[dict[str, Any]]:
        profile_name = str(tasklist.get("profile_name", "") or "")
        target_client = self._client_for_profile(client, profile_name)
        response = target_client.run(
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

    def _target_profiles(self) -> list[GwsProfile]:
        if not self.available_profiles:
            return []
        ordered: list[GwsProfile] = []
        seen_names: set[str] = set()
        if self.synced_profile_names:
            for name in self.synced_profile_names:
                profile = next((item for item in self.available_profiles if item.name == name), None)
                if profile is None or profile.name in seen_names:
                    continue
                seen_names.add(profile.name)
                ordered.append(profile)
            if ordered:
                return ordered
        active = next((item for item in self.available_profiles if item.name == self.active_profile_name), None)
        return [active] if active is not None else []

    def _client_for_profile(self, client: GwsClient, profile_name: str | None) -> GwsClient:
        if not profile_name:
            return client
        if hasattr(client, "with_config_dir"):
            profile = next((item for item in self.available_profiles if item.name == profile_name), None)
            if profile is not None:
                return client.with_config_dir(profile.config_dir)
        return client

    def _sync_selected_tasklist(self) -> None:
        if not any(option["id"] == self.selected_tasklist_id for option in self.tasklists):
            self.selected_tasklist_id = ""
            self.selected_tasklist_name = "All Tasks"
            return
        current = next(option for option in self.tasklists if option["id"] == self.selected_tasklist_id)
        self.selected_tasklist_name = current["name"]

    def _tasklist_key(self, profile_name: str, tasklist_id: str) -> str:
        return f"{profile_name}::{tasklist_id}"

    def _split_tasklist_key(self, value: str) -> tuple[str, str]:
        profile_name, separator, tasklist_id = value.partition("::")
        if not separator:
            return "", value
        return profile_name, tasklist_id

    def _resolve_task_target(self, tasklist_id: str, profile_name: str | None) -> tuple[str, str]:
        keyed_profile_name, actual_tasklist_id = self._split_tasklist_key(tasklist_id)
        if keyed_profile_name:
            return keyed_profile_name, actual_tasklist_id
        return (profile_name or self.active_profile_name or "").strip(), tasklist_id

    def _display_tasklist_name(self, tasklist_name: str, profile_name: str, annotate_profile: bool) -> str:
        if not annotate_profile or not profile_name:
            return tasklist_name
        return f"{tasklist_name} ({profile_name})"

    def _record_key(self, profile_name: str, tasklist_id: str, task_id: str, annotate_profile: bool) -> str:
        if not annotate_profile or not profile_name:
            return f"{tasklist_id}:{task_id}"
        return f"{profile_name}::{tasklist_id}:{task_id}"

    def _default_tasklist_option(self) -> dict[str, str] | None:
        candidates = [option for option in self.tasklists if option["id"]]
        if not candidates:
            return None
        current_profile_option = next(
            (option for option in candidates if option.get("profile_name", "") == self.active_profile_name),
            None,
        )
        return current_profile_option or candidates[0]
