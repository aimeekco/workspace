from __future__ import annotations

from io import BytesIO
import os
import shutil
import subprocess
import tempfile
from typing import Any


GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDE_MIME = "application/vnd.google-apps.presentation"
GOOGLE_DRAWING_MIME = "application/vnd.google-apps.drawing"

TEXT_MIME_TYPES = {
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-sh",
    "application/x-yaml",
    "application/yaml",
}

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".csv",
    ".tsv",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".css",
    ".js",
    ".ts",
    ".py",
    ".toml",
    ".ini",
    ".cfg",
    ".log",
}

MAX_PREVIEW_CHARS = 50_000


def is_google_doc_mime(mime_type: str) -> bool:
    return (mime_type or "").strip() == GOOGLE_DOC_MIME


def is_google_sheet_mime(mime_type: str) -> bool:
    return (mime_type or "").strip() == GOOGLE_SHEET_MIME


def is_google_workspace_mime(mime_type: str) -> bool:
    return (mime_type or "").strip().startswith("application/vnd.google-apps.")


def is_text_previewable(mime_type: str, name: str = "") -> bool:
    normalized = (mime_type or "").strip().lower()
    if normalized.startswith("text/") or normalized in TEXT_MIME_TYPES:
        return True
    lowered_name = name.lower()
    return any(lowered_name.endswith(ext) for ext in TEXT_EXTENSIONS)


def human_size(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    units = ("KB", "MB", "GB", "TB")
    scaled = float(value)
    for unit in units:
        scaled /= 1024.0
        if scaled < 1024 or unit == units[-1]:
            return f"{scaled:.1f} {unit}"
    return f"{value} B"


def build_metadata_header(name: str, mime_type: str, metadata: dict[str, Any] | None = None) -> list[str]:
    info = metadata or {}
    lines = [
        f"Name: {name or 'Untitled file'}",
        f"MIME: {mime_type or 'Unknown'}",
    ]
    size = str(info.get("size", "") or "").strip()
    if size.isdigit():
        lines.append(f"Size: {human_size(int(size))}")
    modified = str(info.get("modifiedTime", "") or "").strip()
    if modified:
        lines.append(f"Modified: {modified}")
    owner = "Unknown"
    owners = info.get("owners", [])
    if isinstance(owners, list) and owners:
        owner = str(owners[0].get("displayName", owner) or owner)
    lines.append(f"Owner: {owner}")
    return lines


def truncate_text(value: str, limit: int = MAX_PREVIEW_CHARS) -> str:
    if len(value) <= limit:
        return value
    remainder = len(value) - limit
    return f"{value[:limit].rstrip()}\n\n… (truncated {remainder} characters)"


def decode_text_bytes(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def render_text_preview(data: bytes) -> str:
    return truncate_text(decode_text_bytes(data).strip() or "(File is empty)")


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    index = 2
    while index + 1 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        index += 2
        if marker in {0xD8, 0xD9}:
            continue
        if index + 1 >= len(data):
            return None
        segment_length = int.from_bytes(data[index:index + 2], "big")
        if segment_length < 2 or index + segment_length > len(data):
            return None
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if index + 7 > len(data):
                return None
            height = int.from_bytes(data[index + 3:index + 5], "big")
            width = int.from_bytes(data[index + 5:index + 7], "big")
            if width > 0 and height > 0:
                return width, height
            return None
        index += segment_length
    return None


def image_dimensions(data: bytes, mime_type: str) -> tuple[int, int] | None:
    if mime_type == "image/png":
        return _png_dimensions(data)
    if mime_type in {"image/jpeg", "image/jpg"}:
        return _jpeg_dimensions(data)
    dimensions = _png_dimensions(data)
    if dimensions is not None:
        return dimensions
    return _jpeg_dimensions(data)


def _ascii_image_preview(data: bytes, max_width: int = 80, max_height: int = 34) -> str | None:
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return None
    try:
        with Image.open(BytesIO(data)) as image:
            image.thumbnail((max_width, max_height))
            grayscale = image.convert("L")
            width, height = grayscale.size
            if width <= 0 or height <= 0:
                return None
            pixels = list(grayscale.getdata())
    except Exception:  # noqa: BLE001
        return None
    palette = " .:-=+*#%@"
    lines: list[str] = []
    for row in range(height):
        start = row * width
        row_values = pixels[start:start + width]
        mapped = "".join(palette[(value * (len(palette) - 1)) // 255] for value in row_values)
        lines.append(mapped.rstrip())
    return "\n".join(lines).strip() or None


def render_image_preview(data: bytes, mime_type: str) -> str:
    lines = ["Image Preview", ""]
    dimensions = image_dimensions(data, mime_type)
    if dimensions is not None:
        lines.append(f"Dimensions: {dimensions[0]} x {dimensions[1]}")
    lines.append("")
    ascii_preview = _ascii_image_preview(data)
    if ascii_preview:
        lines.append(ascii_preview)
        return "\n".join(lines).strip()
    lines.append("Inline image rendering needs Pillow (`pip install pillow`) in this environment.")
    return "\n".join(lines).strip()


def render_pdf_preview(data: bytes) -> str:
    if not shutil.which("pdftotext"):
        return "PDF Preview\n\nInstall `pdftotext` to enable inline PDF text extraction."
    with tempfile.NamedTemporaryFile(prefix="gws_tui_pdf_", suffix=".pdf", delete=False) as temp_file:
        temp_file.write(data)
        temp_path = temp_file.name
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-f", "1", "-l", "12", temp_path, "-"],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
    if result.returncode != 0:
        detail = result.stderr.strip() or "pdftotext failed"
        return f"PDF Preview\n\nUnable to extract text.\n\n{detail}"
    extracted = result.stdout.strip()
    if not extracted:
        return "PDF Preview\n\nNo extractable text found in the first 12 pages."
    return "PDF Preview\n\n" + truncate_text(extracted)


def render_binary_preview(
    name: str,
    mime_type: str,
    data: bytes,
    metadata: dict[str, Any] | None = None,
) -> str:
    lines = ["File Preview", ""]
    lines.extend(build_metadata_header(name, mime_type, metadata))
    lines.extend(["", ""])
    if is_text_previewable(mime_type, name):
        lines.append(render_text_preview(data))
        return "\n".join(lines).strip()
    if mime_type == "application/pdf":
        lines.append(render_pdf_preview(data))
        return "\n".join(lines).strip()
    if mime_type.startswith("image/"):
        lines.append(render_image_preview(data, mime_type))
        return "\n".join(lines).strip()
    sample = data[:96].hex(" ")
    lines.append("Binary preview unavailable for this MIME type.")
    if sample:
        lines.extend(["", "First bytes (hex)", sample])
    return "\n".join(lines).strip()


def render_unavailable_preview(name: str, mime_type: str, reason: str, metadata: dict[str, Any] | None = None) -> str:
    lines = ["File Preview", ""]
    lines.extend(build_metadata_header(name, mime_type, metadata))
    lines.extend(["", "", reason.strip() or "Preview unavailable."])
    return "\n".join(lines).strip()
