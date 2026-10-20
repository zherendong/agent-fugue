"""Files attached to a question: kept in the discussion's own folder, where every member can read them.

The members' tools don't read the same things: Claude Code's file reader
opens images and PDFs, Codex reads text and is handed images with the
message, and the DeepSeek Harness reads text and (on image-capable models)
images. So fugue keeps each file as it came, adds a text version of PDF and
Word files next to it, and tells every member where both are.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .store import FugueError

IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_TOTAL_BYTES = 50 * 1024 * 1024
TEXT_SAMPLE_BYTES = 8192


def safe_name(name: str) -> str:
    """A plain file name: no folders, nothing hidden, nothing the shell or a prompt would trip over."""
    base = Path(str(name).replace("\\", "/")).name.strip()
    base = re.sub(r"[\x00-\x1f<>:\"|?*`$]", "_", base).lstrip(".")
    return base[:120] or "attachment"


def _unused(folder: Path, name: str) -> Path:
    path = folder / name
    stem, suffix, n = path.stem, path.suffix, 2
    while path.exists():
        path = folder / f"{stem}-{n}{suffix}"
        n += 1
    return path


def _looks_like_text(data: bytes) -> bool:
    sample = data[:TEXT_SAMPLE_BYTES]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as exc:
        return exc.start > len(sample) - 4  # a character cut off at the end of the sample
    return True


def pdf_text(path: Path) -> tuple[str | None, int | None, str | None]:
    """The text of a PDF, its page count, and why there is no text if there isn't."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, None, "pypdf isn't installed, so there is no text version"
    try:
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # pypdf raises many kinds of error on damaged files
        return None, None, f"its text couldn't be read ({exc.__class__.__name__})"
    text = "\n\n".join(f"--- page {i} ---\n{page.strip()}" for i, page in enumerate(pages, 1))
    if not any(page.strip() for page in pages):
        return None, len(pages), "it has no text layer (probably scanned images)"
    return text, len(pages), None


WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_text(path: Path) -> tuple[str | None, str | None]:
    """The paragraphs and table rows of a Word file, in order."""
    try:
        with zipfile.ZipFile(path) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, OSError):
        return None, "it couldn't be read as a Word file"
    lines = []
    body = root.find(f"{WORD}body")
    for block in body if body is not None else []:
        if block.tag == f"{WORD}p":
            lines.append("".join(t.text or "" for t in block.iter(f"{WORD}t")))
        elif block.tag == f"{WORD}tbl":
            for row in block.iter(f"{WORD}tr"):
                cells = ["".join(t.text or "" for t in cell.iter(f"{WORD}t")).strip() for cell in row.iter(f"{WORD}tc")]
                lines.append(" | ".join(cells))
    text = "\n".join(lines).strip()
    return (text, None) if text else (None, "it has no text")


def save(folder: Path, name: str, data: bytes) -> dict:
    """Keep one attached file, and a text version of it if it is a PDF or Word file."""
    folder.mkdir(parents=True, exist_ok=True)
    path = _unused(folder, safe_name(name))
    path.write_bytes(data)
    suffix = path.suffix.lower()
    record = {"name": path.name, "path": str(path), "size": len(data), "text": None}
    text = note = None
    if suffix in IMAGE_TYPES:
        record["kind"] = "image"
    elif suffix == ".pdf":
        record["kind"] = "pdf"
        text, pages, note = pdf_text(path)
        record["pages"] = pages
    elif suffix == ".docx":
        record["kind"] = "word"
        text, note = docx_text(path)
    elif _looks_like_text(data):
        record["kind"] = "text"
    else:
        record["kind"] = "other"
    if text:
        text_path = _unused(folder, f"{path.name}.txt")
        text_path.write_text(text + "\n", encoding="utf-8")
        record["text"] = str(text_path)
    if note:
        record["note"] = note
    return record


def save_all(folder: Path, files: list[tuple[str, bytes]]) -> list[dict]:
    if sum(len(data) for _, data in files) > MAX_TOTAL_BYTES:
        raise FugueError(f"The attachments come to more than {MAX_TOTAL_BYTES // (1024 * 1024)} MB.")
    return [save(folder, name, data) for name, data in files]


def _size(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB" if size >= 1024 * 1024 else f"{max(1, round(size / 1024))} KB"


KIND_WORDS = {"image": "an image", "pdf": "a PDF", "word": "a Word file", "text": "a text file", "other": "a file"}


def prompt_block(records: list[dict], *, images_attached: bool = False) -> str:
    """What a member is told about the attached files, after the question."""
    if not records:
        return ""
    lines = ["The user attached these files to the question; they are part of it. Read the ones you need:", ""]
    for record in records:
        what = KIND_WORDS.get(record["kind"], "a file")
        if record.get("pages"):
            what += f", {record['pages']} pages"
        lines.append(f"- {record['name']} ({what}, {_size(record['size'])}): {record['path']}")
        if record.get("text"):
            lines.append(f"  Its text, taken out of the file: {record['text']}")
        elif record.get("note"):
            lines.append(f"  No text version: {record['note']}.")
        if record["kind"] == "image":
            lines.append("  It is also attached to this message." if images_attached
                         else "  Open it with your image or file reading tool.")
    return "\n".join(lines)


def with_attachments(question: str, records: list[dict] | None, *, images_attached: bool = False) -> str:
    block = prompt_block(records or [], images_attached=images_attached)
    return f"{question.rstrip()}\n\n{block}" if block else question


def images(records: list[dict] | None) -> list[str]:
    return [record["path"] for record in records or [] if record["kind"] == "image"]
