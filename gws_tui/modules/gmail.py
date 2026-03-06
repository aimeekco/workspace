from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Any

from gws_tui.client import GwsClient
from gws_tui.models import Record
from gws_tui.modules.base import WorkspaceModule


def header_value(headers: list[dict], name: str, fallback: str = "") -> str:
    lowered = name.lower()
    for header in headers:
        if header.get("name", "").lower() == lowered:
            return header.get("value", fallback)
    return fallback


def format_message_date(value: str) -> str:
    try:
        return parsedate_to_datetime(value).astimezone().strftime("%b %d %I:%M %p")
    except (TypeError, ValueError, IndexError):
        return value or "Unknown date"


def decode_body(data: str) -> str:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(f"{data}{padding}").decode("utf-8", errors="replace")


def extract_body(payload: dict) -> str:
    parts = payload.get("parts") or []
    if parts:
        plain_parts: list[str] = []
        fallback_parts: list[str] = []
        for part in parts:
            extracted = extract_body(part)
            if not extracted:
                continue
            mime_type = part.get("mimeType", "")
            if mime_type == "text/plain":
                plain_parts.append(extracted)
            else:
                fallback_parts.append(extracted)
        combined = plain_parts or fallback_parts
        return "\n".join(text for text in combined if text.strip())

    body = payload.get("body", {})
    data = body.get("data")
    if not data:
        return ""
    try:
        return decode_body(data)
    except (ValueError, TypeError):
        return ""


class GmailModule(WorkspaceModule):
    id = "gmail"
    title = "Gmail"
    description = "Recent inbox messages."
    columns = ("From", "Subject", "Date")
    empty_message = "No inbox messages found."

    def build_raw_message(self, to: str, subject: str, body: str) -> str:
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        return encoded.rstrip("=")

    def fetch_records(self, client: GwsClient) -> list[Record]:
        response = client.run(
            "gmail",
            "users",
            "messages",
            "list",
            params={
                "userId": "me",
                "maxResults": 20,
                "q": "in:inbox",
            },
        )
        message_ids = [message["id"] for message in response.get("messages", [])]
        if not message_ids:
            return []

        with ThreadPoolExecutor(max_workers=6) as executor:
            metadata_responses = list(executor.map(lambda message_id: self._fetch_metadata(client, message_id), message_ids))

        records: list[Record] = []
        for message_id, metadata in zip(message_ids, metadata_responses, strict=False):
            headers = metadata.get("payload", {}).get("headers", [])
            subject = header_value(headers, "Subject", "No subject")
            sender = header_value(headers, "From", "Unknown sender")
            date = format_message_date(header_value(headers, "Date", "Unknown date"))
            preview = "\n".join(
                [
                    f"From: {sender}",
                    f"Subject: {subject}",
                    f"Date: {date}",
                    "",
                    metadata.get("snippet") or "Press Enter for the full message.",
                ]
            )
            records.append(
                Record(
                    key=message_id,
                    columns=(sender, subject, date),
                    title=subject,
                    subtitle=sender,
                    preview=preview,
                    raw={
                        "headers": headers,
                        "label_ids": metadata.get("labelIds", []),
                    },
                )
            )
        return records

    def _fetch_metadata(self, client: GwsClient, message_id: str) -> dict:
        return client.run(
            "gmail",
            "users",
            "messages",
            "get",
            params={
                "userId": "me",
                "id": message_id,
                "format": "metadata",
            },
        )

    def send_message(self, client: GwsClient, to: str, subject: str, body: str) -> dict:
        return client.run(
            "gmail",
            "users",
            "messages",
            "send",
            params={"userId": "me"},
            body={"raw": self.build_raw_message(to=to, subject=subject, body=body)},
        )

    def list_user_labels(self, client: GwsClient) -> list[dict[str, Any]]:
        response = client.run(
            "gmail",
            "users",
            "labels",
            "list",
            params={"userId": "me"},
        )
        labels = response.get("labels", [])
        return sorted(
            [label for label in labels if label.get("type") == "USER"],
            key=lambda label: label.get("name", "").lower(),
        )

    def update_message_labels(
        self,
        client: GwsClient,
        message_id: str,
        existing_label_ids: list[str],
        selected_label_ids: list[str],
    ) -> dict:
        existing = set(existing_label_ids)
        selected = set(selected_label_ids)
        return client.run(
            "gmail",
            "users",
            "messages",
            "modify",
            params={"userId": "me", "id": message_id},
            body={
                "addLabelIds": sorted(selected - existing),
                "removeLabelIds": sorted(existing - selected),
            },
        )

    def trash_message(self, client: GwsClient, message_id: str) -> dict:
        return client.run(
            "gmail",
            "users",
            "messages",
            "trash",
            params={"userId": "me", "id": message_id},
        )

    def fetch_detail(self, client: GwsClient, record: Record) -> str:
        response = client.run(
            "gmail",
            "users",
            "messages",
            "get",
            params={
                "userId": "me",
                "id": record.key,
                "format": "full",
            },
        )
        payload = response.get("payload", {})
        headers = payload.get("headers", [])
        subject = header_value(headers, "Subject", "No subject")
        sender = header_value(headers, "From", "Unknown sender")
        recipient = header_value(headers, "To", "Unknown recipient")
        date = header_value(headers, "Date", "Unknown date")
        body = extract_body(payload).strip() or response.get("snippet") or "(No message body)"
        label_ids = response.get("labelIds", [])
        record.raw["label_ids"] = label_ids
        lines = [
            f"Subject: {subject}",
            f"From: {sender}",
            f"To: {recipient}",
            f"Date: {date}",
            "",
            body,
        ]
        return "\n".join(lines)
