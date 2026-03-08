from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, parsedate_to_datetime
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


def normalize_forward_subject(subject: str) -> str:
    if subject.lower().startswith("fwd:"):
        return subject
    return f"Fwd: {subject}"


def quote_text(value: str) -> str:
    lines = value.splitlines() or [""]
    return "\n".join("> " if line == "" else f"> {line}" for line in lines)


def attachment_names(payload: dict) -> list[str]:
    names: list[str] = []
    for part in payload.get("parts") or []:
        filename = part.get("filename", "").strip()
        if filename:
            names.append(filename)
        names.extend(attachment_names(part))
    return names


def canonical_addresses(values: list[str], exclude: set[str] | None = None) -> list[str]:
    excluded = {value.lower() for value in (exclude or set())}
    seen: set[str] = set()
    results: list[str] = []
    for name, address in getaddresses(values):
        lowered = address.strip().lower()
        if not lowered or lowered in excluded or lowered in seen:
            continue
        seen.add(lowered)
        results.append(formataddr((name, address.strip())) if name else address.strip())
    return results


class GmailModule(WorkspaceModule):
    id = "gmail"
    title = "Gmail"
    description = "Recent inbox messages."
    columns = ("Subject", "From", "Time")
    empty_message = "No inbox messages found."

    def __init__(self) -> None:
        self.search_query = ""
        self.unread_only = False
        self.selected_mailbox_id = "INBOX"
        self.selected_mailbox_name = "Inbox"
        self.mailboxes: list[dict[str, str]] = []

    def subtitle(self) -> str:
        if not self.search_query and not self.unread_only:
            return self.description
        return f"Mailbox: {self.scope_summary()}"

    def badge(self) -> str:
        return "Mail"

    def loading_message(self) -> str:
        return f"Syncing {self.scope_summary()}..."

    def empty_hint(self) -> str:
        return "Use / to search, u for unread only, or r to refresh."

    def list_label(self) -> str:
        return self.selected_mailbox_name

    def mailbox_options(self, client: GwsClient) -> list[dict[str, str]]:
        response = client.run(
            "gmail",
            "users",
            "labels",
            "list",
            params={"userId": "me"},
        )
        labels = response.get("labels", [])
        label_map = {label.get("id", ""): label for label in labels}
        preferred = [
            ("INBOX", "Inbox"),
            ("SENT", "Sent"),
            ("DRAFT", "Drafts"),
            ("STARRED", "Starred"),
            ("IMPORTANT", "Important"),
            ("TRASH", "Trash"),
        ]
        options: list[dict[str, str]] = []
        for label_id, fallback_name in preferred:
            label = label_map.get(label_id)
            if label is None:
                continue
            options.append(
                {
                    "id": label_id,
                    "name": label.get("name", fallback_name).title(),
                    "type": "SYSTEM",
                }
            )
        user_labels = sorted(
            [label for label in labels if label.get("type") == "USER"],
            key=lambda label: label.get("name", "").lower(),
        )
        for label in user_labels:
            options.append(
                {
                    "id": label.get("id", ""),
                    "name": label.get("name", "Unnamed label"),
                    "type": "USER",
                }
            )
        self.mailboxes = [option for option in options if option["id"]]
        if not any(option["id"] == self.selected_mailbox_id for option in self.mailboxes):
            self.selected_mailbox_id = "INBOX"
            self.selected_mailbox_name = "Inbox"
        else:
            current = next(option for option in self.mailboxes if option["id"] == self.selected_mailbox_id)
            self.selected_mailbox_name = current["name"]
        return self.mailboxes

    def set_mailbox(self, mailbox_id: str, mailbox_name: str) -> None:
        self.selected_mailbox_id = mailbox_id
        self.selected_mailbox_name = mailbox_name

    def scope_query(self) -> str:
        terms: list[str] = []
        if self.search_query:
            terms.append(self.search_query)
        if self.unread_only:
            terms.append("is:unread")
        return " ".join(term for term in terms if term)

    def scope_summary(self) -> str:
        parts = [self.selected_mailbox_name.lower()]
        if self.search_query:
            parts.append(f'query="{self.search_query}"')
        if self.unread_only:
            parts.append("unread only")
        return ", ".join(parts)

    def set_search_query(self, query: str) -> None:
        self.search_query = query.strip()

    def toggle_unread_only(self) -> bool:
        self.unread_only = not self.unread_only
        return self.unread_only

    def reset_state(self) -> None:
        self.search_query = ""
        self.unread_only = False
        self.selected_mailbox_id = "INBOX"
        self.selected_mailbox_name = "Inbox"
        self.mailboxes = []

    def is_unread(self, label_ids: list[str] | None) -> bool:
        return "UNREAD" in (label_ids or [])

    def format_subject_cell(self, subject: str, label_ids: list[str] | None) -> str:
        if self.is_unread(label_ids):
            return f"● {subject}"
        return subject

    def build_raw_message(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        attachment_paths: list[str] | None = None,
    ) -> str:
        message = EmailMessage()
        message["To"] = to
        if cc:
            message["Cc"] = cc
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
        cc: str,
        in_reply_to: str,
        references: str,
        attachment_paths: list[str] | None = None,
    ) -> str:
        message = EmailMessage()
        message["To"] = to
        if cc:
            message["Cc"] = cc
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
        self.mailbox_options(client)
        query = self.scope_query()
        self.empty_message = f"No Gmail messages found for {self.scope_summary()}."
        params: dict[str, Any] = {
            "userId": "me",
            "maxResults": 20,
        }
        if query:
            params["q"] = query
        if self.selected_mailbox_id:
            params["labelIds"] = self.selected_mailbox_id
        response = client.run(
            "gmail",
            "users",
            "messages",
            "list",
            params=params,
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
            label_ids = metadata.get("labelIds", [])
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
                    columns=(self.format_subject_cell(subject, label_ids), sender, date),
                    title=subject,
                    subtitle=sender,
                    preview=preview,
                    raw={
                        "headers": headers,
                        "label_ids": label_ids,
                        "unread": self.is_unread(label_ids),
                        "thread_id": metadata.get("threadId"),
                        "mailbox_id": self.selected_mailbox_id,
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
        cc: str = "",
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
                    cc=cc,
                    attachment_paths=attachment_paths,
                )
            },
        )

    def fetch_profile_email(self, client: GwsClient) -> str:
        response = client.run(
            "gmail",
            "users",
            "getProfile",
            params={"userId": "me"},
        )
        return str(response.get("emailAddress", "")).strip().lower()

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
            "cc": "",
            "subject": subject,
            "body": reply_body,
            "thread_id": response.get("threadId", ""),
            "in_reply_to": message_id_header,
            "references": combined_references or message_id_header,
        }

    def fetch_reply_all_context(self, client: GwsClient, message_id: str) -> dict[str, str]:
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
        current_user = self.fetch_profile_email(client)
        sender_addresses = {
            address.strip().lower()
            for _, address in getaddresses([sender])
            if address.strip()
        }
        cc_recipients = canonical_addresses(
            [header_value(headers, "To", ""), header_value(headers, "Cc", "")],
            exclude=sender_addresses | {current_user},
        )
        reply_context = self.fetch_reply_context(client, message_id)
        reply_context["cc"] = ", ".join(cc_recipients)
        return reply_context

    def fetch_forward_context(self, client: GwsClient, message_id: str) -> dict[str, str]:
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
        subject = normalize_forward_subject(header_value(headers, "Subject", "No subject"))
        body = extract_body(payload).strip() or response.get("snippet") or ""
        lines = [
            "",
            "",
            "---------- Forwarded message ---------",
            f"From: {header_value(headers, 'From', 'Unknown sender')}",
            f"Date: {header_value(headers, 'Date', 'Unknown date')}",
            f"Subject: {header_value(headers, 'Subject', 'No subject')}",
            f"To: {header_value(headers, 'To', 'Unknown recipient')}",
        ]
        cc = header_value(headers, "Cc", "").strip()
        if cc:
            lines.append(f"Cc: {cc}")
        lines.extend(["", body])
        return {
            "to": "",
            "cc": "",
            "subject": subject,
            "body": "\n".join(lines).rstrip(),
        }

    def reply_to_message(
        self,
        client: GwsClient,
        to: str,
        subject: str,
        body: str,
        cc: str,
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
                    cc=cc,
                    in_reply_to=in_reply_to,
                    references=references,
                    attachment_paths=attachment_paths,
                ),
            },
        )

    def create_draft(
        self,
        client: GwsClient,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        attachment_paths: list[str] | None = None,
        thread_id: str = "",
        in_reply_to: str = "",
        references: str = "",
    ) -> dict:
        raw = (
            self.build_raw_reply_message(
                to=to,
                subject=subject,
                body=body,
                cc=cc,
                in_reply_to=in_reply_to,
                references=references,
                attachment_paths=attachment_paths,
            )
            if thread_id
            else self.build_raw_message(
                to=to,
                subject=subject,
                body=body,
                cc=cc,
                attachment_paths=attachment_paths,
            )
        )
        message: dict[str, Any] = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        return client.run(
            "gmail",
            "users",
            "drafts",
            "create",
            params={"userId": "me"},
            body={"message": message},
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

    def mark_message_read(self, client: GwsClient, message_id: str) -> dict:
        return client.run(
            "gmail",
            "users",
            "messages",
            "modify",
            params={"userId": "me", "id": message_id},
            body={
                "addLabelIds": [],
                "removeLabelIds": ["UNREAD"],
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
        if record.raw.get("unread"):
            self.mark_message_read(client, record.key)
            label_ids = [label_id for label_id in record.raw.get("label_ids", []) if label_id != "UNREAD"]
            record.raw["label_ids"] = label_ids
            record.raw["unread"] = False
        thread_id = str(record.raw.get("thread_id") or "").strip()
        if thread_id:
            return self.fetch_thread_detail(client, record, thread_id)

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
        attachments = attachment_names(payload)
        lines = [
            "Message Overview",
            "",
            f"Subject: {subject}",
            f"From: {sender}",
            f"To: {recipient}",
            f"Date: {date}",
        ]
        if label_ids:
            lines.append(f"Labels: {', '.join(label_ids)}")
        if attachments:
            lines.append(f"Attachments: {', '.join(attachments)}")
        lines.extend(["", "Body", "", body])
        return "\n".join(lines)

    def fetch_thread_detail(self, client: GwsClient, record: Record, thread_id: str) -> str:
        response = client.run(
            "gmail",
            "users",
            "threads",
            "get",
            params={
                "userId": "me",
                "id": thread_id,
                "format": "full",
            },
        )
        messages = response.get("messages", [])
        if not messages:
            return "(Empty thread)"

        selected_message = next((message for message in messages if message.get("id") == record.key), messages[-1])
        record.raw["label_ids"] = selected_message.get("labelIds", [])

        thread_subject = header_value(
            selected_message.get("payload", {}).get("headers", []),
            "Subject",
            record.title or "No subject",
        )
        lines = [
            "Thread Overview",
            "",
            f"Thread: {thread_subject}",
            f"Messages: {len(messages)}",
            "",
        ]

        for index, message in enumerate(messages, start=1):
            payload = message.get("payload", {})
            headers = payload.get("headers", [])
            sender = header_value(headers, "From", "Unknown sender")
            recipient = header_value(headers, "To", "Unknown recipient")
            date = header_value(headers, "Date", "Unknown date")
            subject = header_value(headers, "Subject", thread_subject)
            body = extract_body(payload).strip() or message.get("snippet") or "(No message body)"
            attachments = attachment_names(payload)
            labels = message.get("labelIds", [])
            marker = " [selected]" if message.get("id") == record.key else ""
            lines.extend(
                [
                    f"--- Message {index}{marker} ---",
                    f"Subject: {subject}",
                    f"From: {sender}",
                    f"To: {recipient}",
                    f"Date: {date}",
                ]
            )
            if labels:
                lines.append(f"Labels: {', '.join(labels)}")
            if attachments:
                lines.append(f"Attachments: {', '.join(attachments)}")
            lines.extend(["", "Body", "", body, ""])
        return "\n".join(lines)
