from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
import mimetypes
from pathlib import Path
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


def normalize_reply_subject(subject: str) -> str:
    if subject.lower().startswith("re:"):
        return subject
    return f"Re: {subject}"


def quote_text(value: str) -> str:
    lines = value.splitlines() or [""]
    return "\n".join("> " if line == "" else f"> {line}" for line in lines)


class GmailModule(WorkspaceModule):
    id = "gmail"
    title = "Gmail"
    description = "Recent inbox messages."
    columns = ("From", "Subject", "Date")
    empty_message = "No inbox messages found."

    def build_raw_message(
        self,
        to: str,
        subject: str,
        body: str,
        attachment_paths: list[str] | None = None,
    ) -> str:
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        self._attach_files(message, attachment_paths or [])
        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        return encoded.rstrip("=")

    def build_raw_reply_message(
        self,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str,
        references: str,
        attachment_paths: list[str] | None = None,
    ) -> str:
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message["In-Reply-To"] = in_reply_to
        message["References"] = references
        message.set_content(body)
        self._attach_files(message, attachment_paths or [])
        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        return encoded.rstrip("=")

    def _attach_files(self, message: EmailMessage, attachment_paths: list[str]) -> None:
        for attachment_path in attachment_paths:
            path = Path(attachment_path).expanduser()
            if not path.exists():
                raise FileNotFoundError(f"Attachment not found: {path}")
            if not path.is_file():
                raise ValueError(f"Attachment is not a file: {path}")
            content_type, _ = mimetypes.guess_type(path.name)
            if content_type:
                maintype, subtype = content_type.split("/", 1)
            else:
                maintype, subtype = "application", "octet-stream"
            message.add_attachment(
                path.read_bytes(),
                maintype=maintype,
                subtype=subtype,
                filename=path.name,
            )

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
                        "thread_id": metadata.get("threadId"),
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

    def send_message(
        self,
        client: GwsClient,
        to: str,
        subject: str,
        body: str,
        attachment_paths: list[str] | None = None,
    ) -> dict:
        return client.run(
            "gmail",
            "users",
            "messages",
            "send",
            params={"userId": "me"},
            body={
                "raw": self.build_raw_message(
                    to=to,
                    subject=subject,
                    body=body,
                    attachment_paths=attachment_paths,
                )
            },
        )

    def fetch_reply_context(self, client: GwsClient, message_id: str) -> dict[str, str]:
        response = client.run(
            "gmail",
            "users",
            "messages",
            "get",
            params={
                "userId": "me",
                "id": message_id,
                "format": "full",
            },
        )
        payload = response.get("payload", {})
        headers = payload.get("headers", [])
        sender = header_value(headers, "Reply-To") or header_value(headers, "From", "")
        subject = normalize_reply_subject(header_value(headers, "Subject", "No subject"))
        sent_at = header_value(headers, "Date", "Unknown date")
        message_id_header = header_value(headers, "Message-ID", "")
        references = header_value(headers, "References", "").strip()
        combined_references = " ".join(part for part in [references, message_id_header] if part).strip()
        original_body = extract_body(payload).strip() or response.get("snippet") or ""
        quoted = quote_text(original_body)
        reply_body = f"\n\nOn {sent_at}, {header_value(headers, 'From', 'Unknown sender')} wrote:\n{quoted}".rstrip()
        return {
            "to": sender,
            "subject": subject,
            "body": reply_body,
            "thread_id": response.get("threadId", ""),
            "in_reply_to": message_id_header,
            "references": combined_references or message_id_header,
        }

    def reply_to_message(
        self,
        client: GwsClient,
        to: str,
        subject: str,
        body: str,
        thread_id: str,
        in_reply_to: str,
        references: str,
        attachment_paths: list[str] | None = None,
    ) -> dict:
        return client.run(
            "gmail",
            "users",
            "messages",
            "send",
            params={"userId": "me"},
            body={
                "threadId": thread_id,
                "raw": self.build_raw_reply_message(
                    to=to,
                    subject=subject,
                    body=body,
                    in_reply_to=in_reply_to,
                    references=references,
                    attachment_paths=attachment_paths,
                ),
            },
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
