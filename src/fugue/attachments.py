"""Files attached to a question: kept in the discussion's own folder, where every member can read them.

The members run read-only, so none of them can install a PDF reader for
itself, and their tools read different things: Claude Code's file reader
opens images and PDFs, Codex reads text and is handed images with the
message, and the DeepSeek Harness reads text and (on image-capable models)
images. So fugue does the converting when the file arrives: it keeps each
file as it came, adds a text version of PDF and Word files, renders a PDF's
pages as images, and tells each member what there is and which to trust:
the original, and the page images for anything about layout.
"""

from __future__ import annotations

import re
import struct
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree

from .store import FugueError

IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_TOTAL_BYTES = 50 * 1024 * 1024
TEXT_SAMPLE_BYTES = 8192
# A PDF's first pages are rendered as images, wide enough to read body text,
# small enough to send to every member with its message.
MAX_PAGE_IMAGES = 10
PAGE_IMAGE_WIDTH = 1400


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


def tidy(text: str) -> str:
    """A page's text with runs of spaces closed up and at most one blank line in a row."""
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def page_text(page) -> str:
    """One page's text, read by where the letters sit on the page.

    pypdf's default reading guesses spaces from the gaps between pieces of
    text, and splits words that a PDF (LaTeX's, typically) draws a few
    letters at a time: "REVOL VE", "T ools". Reading by layout places each
    letter by its width, so words stay whole; the extra spacing it adds to
    keep columns is closed up afterwards.
    """
    try:
        text = tidy(page.extract_text(extraction_mode="layout") or "")
    except Exception:  # an unusual page; the default reading may still manage
        text = ""
    return text or (page.extract_text() or "")


def pdf_text(path: Path) -> tuple[str | None, int | None, str | None]:
    """The text of a PDF, its page count, and why there is no text if there isn't."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, None, "pypdf isn't installed, so there is no text version"
    try:
        reader = PdfReader(str(path))
        pages = [page_text(page) for page in reader.pages]
    except Exception as exc:  # pypdf raises many kinds of error on damaged files
        return None, None, f"its text couldn't be read ({exc.__class__.__name__})"
    text = "\n\n".join(f"--- page {i} ---\n{page.strip()}" for i, page in enumerate(pages, 1))
    if not any(page.strip() for page in pages):
        return None, len(pages), "it has no text layer (probably scanned images)"
    return text, len(pages), None


def png(width: int, height: int, rows: bytes, stride: int) -> bytes:
    """An RGB image as a PNG file, written with the standard library."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\x00" + rows[y * stride:y * stride + width * 3] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def page_images(path: Path, folder: Path) -> tuple[list[str], str | None]:
    """The first pages of a PDF as PNG files next to it, and a note if pages were left out or none could be made."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return [], "pypdfium2 isn't installed, so there are no page images"
    made = []
    try:
        pdf = pdfium.PdfDocument(str(path))
        count = len(pdf)
        for number in range(min(count, MAX_PAGE_IMAGES)):
            page = pdf[number]
            scale = min(3.0, PAGE_IMAGE_WIDTH / page.get_width()) if page.get_width() else 2.0
            bitmap = page.render(scale=scale, rev_byteorder=True)
            if bitmap.n_channels != 3:
                raise ValueError(f"unexpected {bitmap.mode} bitmap")
            image = _unused(folder, f"{path.name}.page-{number + 1}.png")
            image.write_bytes(png(bitmap.width, bitmap.height, bytes(bitmap.buffer), bitmap.stride))
            made.append(str(image))
        pdf.close()
    except Exception as exc:  # pdfium raises its own errors on damaged files
        return made, f"its pages couldn't all be rendered ({exc.__class__.__name__})"
    if count > MAX_PAGE_IMAGES:
        return made, f"only its first {MAX_PAGE_IMAGES} of {count} pages are shown as images"
    return made, None


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
        record["page_images"], images_note = page_images(path, folder)
        if images_note:
            record["images_note"] = images_note
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

INTRO = ("The user attached these files to the question; they are part of it. Read the ones you need. The original "
         "files are what counts: fugue added a text version of PDF and Word files for reading their content, but "
         "taking text out of a file can get spacing, order or symbols wrong, so judge layout and formatting, and "
         "anything in the text that looks garbled, from the original or its page images.")


def prompt_block(records: list[dict], *, tool: str | None = None) -> str:
    """What a member is told about the attached files, after the question.

    `tool` is the member's tool: Claude Code is told to open PDFs itself, Codex
    that their pages come with the message, and the DeepSeek Harness where the
    page images are.
    """
    if not records:
        return ""
    lines = [INTRO, ""]
    for record in records:
        what = KIND_WORDS.get(record["kind"], "a file")
        if record.get("pages"):
            what += f", {record['pages']} page{'' if record['pages'] == 1 else 's'}"
        lines.append(f"- {record['name']} ({what}, {_size(record['size'])}): {record['path']}")
        if record.get("text"):
            lines.append(f"  Its text version: {record['text']}")
        elif record.get("note"):
            lines.append(f"  No text version: {record['note']}.")
        pages = record.get("page_images") or []
        if record["kind"] == "pdf" and tool == "claude":
            lines.append("  Open the PDF itself with your file reader: it shows the pages as they look.")
        elif pages and tool == "codex":
            lines.append(f"  Its pages are attached to this message as images ({_count(pages)}).")
        elif pages:
            lines.append(f"  Its pages as images, to open with your image reading tool: {', '.join(pages)}")
        if record.get("images_note") and (pages or tool != "claude"):
            lines.append(f"  Note: {record['images_note']}.")
        if record["kind"] == "image":
            lines.append("  It is also attached to this message." if tool == "codex"
                         else "  Open it with your image or file reading tool.")
    return "\n".join(lines)


def _count(pages: list[str]) -> str:
    return "1 page" if len(pages) == 1 else f"{len(pages)} pages"


def with_attachments(question: str, records: list[dict] | None, *, tool: str | None = None) -> str:
    block = prompt_block(records or [], tool=tool)
    return f"{question.rstrip()}\n\n{block}" if block else question


def images(records: list[dict] | None) -> list[str]:
    """The images to hand Codex with its message: attached images, and the pages of attached PDFs."""
    found = []
    for record in records or []:
        if record["kind"] == "image":
            found.append(record["path"])
        found.extend(record.get("page_images") or [])
    return found
