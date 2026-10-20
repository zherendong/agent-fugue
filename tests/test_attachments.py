"""Attached files: kept with the discussion, text taken out of PDF and Word files, and passed to every member."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from fugue import attachments


def tiny_pdf(text: str) -> bytes:
    """A one-page PDF with a line of text, written by hand."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
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
    block = attachments.prompt_block(saved)
    assert "- resume.pdf (a PDF, 1 pages" in block and f"Its text, taken out of the file: {by_name['resume.pdf']['text']}" in block
    assert "Open it with your image or file reading tool." in block
    assert "It is also attached to this message." in attachments.prompt_block(saved, images_attached=True)


def test_a_pdf_without_text_says_so(tmp_path):
    record = attachments.save(tmp_path, "scan.pdf", tiny_pdf(""))
    assert record["text"] is None and "no text layer" in record["note"]
    assert "No text version: it has no text layer" in attachments.prompt_block([record])
