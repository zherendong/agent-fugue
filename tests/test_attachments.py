"""Attached files: kept with the discussion, text taken out of PDF and Word files, and passed to every member."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from fugue import attachments


def tiny_pdf(text: str, *, drawing: bytes | None = None) -> bytes:
    """A one-page PDF with a line of text, written by hand; `drawing` replaces how the text is drawn."""
    stream = b"BT /F1 12 Tf 72 720 Td " + (drawing or f"({text}) Tj".encode()) + b" ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def tiny_docx(paragraphs: list[str], table: list[list[str]]) -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    paras = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    rows = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row) + "</w:tr>"
                   for row in table)
    xml = f'<?xml version="1.0"?><w:document {w}><w:body>{paras}<w:tbl>{rows}</w:tbl></w:body></w:document>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return out.getvalue()


PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def test_files_are_kept_with_a_text_version_of_pdf_and_word(tmp_path):
    saved = attachments.save_all(tmp_path, [
        ("../../resume.pdf", tiny_pdf("Ten years of Rust")),
        ("design.docx", tiny_docx(["Goals", "Keep the cache warm"], [["Option", "Cost"], ["Disk", "low"]])),
        ("shot.png", PNG),
        ("notes.md", "# Notes\nplain text".encode()),
        ("blob.bin", bytes(range(256))),
        ("notes.md", b"a second file with the same name"),
    ])
    by_name = {record["name"]: record for record in saved}
    assert sorted(by_name) == ["blob.bin", "design.docx", "notes-2.md", "notes.md", "resume.pdf", "shot.png"]
    assert Path(by_name["resume.pdf"]["path"]).parent == tmp_path  # no way out of the folder
    assert by_name["resume.pdf"]["kind"] == "pdf" and by_name["resume.pdf"]["pages"] == 1
    assert "Ten years of Rust" in Path(by_name["resume.pdf"]["text"]).read_text()
    assert Path(by_name["design.docx"]["text"]).read_text() == "Goals\nKeep the cache warm\nOption | Cost\nDisk | low\n"
    assert [by_name[n]["kind"] for n in ("shot.png", "notes.md", "blob.bin")] == ["image", "text", "other"]
    pdf = by_name["resume.pdf"]
    assert [Path(p).name for p in pdf["page_images"]] == ["resume.pdf.page-1.png"]
    assert Path(pdf["page_images"][0]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    block = attachments.prompt_block(saved)
    assert "- resume.pdf (a PDF, 1 page, " in block and f"Its text version: {pdf['text']}" in block
    assert "The original files are what counts" in block
    assert f"Its pages as images, to open with your image reading tool: {pdf['page_images'][0]}" in block
    assert "Open it with your image or file reading tool." in block
    # Each tool is told what suits it: Claude Code opens the PDF itself, Codex gets the pages with its message.
    claude, codex = attachments.prompt_block(saved, tool="claude"), attachments.prompt_block(saved, tool="codex")
    assert "Open the PDF itself with your file reader" in claude and pdf["page_images"][0] not in claude
    assert "Its pages are attached to this message as images (1 page)." in codex
    assert "It is also attached to this message." in codex
    assert attachments.images(saved) == [pdf["page_images"][0], by_name["shot.png"]["path"]]


def test_words_drawn_a_few_letters_at_a_time_stay_whole(tmp_path):
    # LaTeX often draws a word in pieces; reading by gaps alone made "REVOLVE" into "REVOL VE".
    record = attachments.save(tmp_path, "cv.pdf", tiny_pdf("", drawing=b"(T) Tj 9 0 Td (ools) Tj"))
    assert Path(record["text"]).read_text().strip().endswith("Tools")
    assert attachments.tidy("  a    b  \n\n\n\n c ") == "a b\n\nc"


def test_long_pdfs_show_only_their_first_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "MAX_PAGE_IMAGES", 0)
    record = attachments.save(tmp_path, "long.pdf", tiny_pdf("One page"))
    assert record["page_images"] == [] and record["images_note"] == "only its first 0 of 1 pages are shown as images"
    assert "Note: only its first 0 of 1 pages are shown as images." in attachments.prompt_block([record], tool="dsh")


def test_a_pdf_without_text_says_so(tmp_path):
    record = attachments.save(tmp_path, "scan.pdf", tiny_pdf(""))
    assert record["text"] is None and "no text layer" in record["note"]
    assert "No text version: it has no text layer" in attachments.prompt_block([record])
