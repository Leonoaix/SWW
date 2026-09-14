"""PDF fixtures stay in memory; these tests do not create user artifacts."""

from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from sww.resume import MAX_PDF_BYTES, extract_resume, extract_skills


def pdf_bytes(text="Python, SQL, React. Software developer.", pages=1, encrypted=False):
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    for index in range(pages):
        page = writer.add_blank_page(width=612, height=792)
        if text and index == 0:
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
                NameObject("/F1"): writer._add_object(font)})})
            stream = DecodedStreamObject()
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream.set_data(("BT /F1 12 Tf 72 720 Td (%s) Tj ET" % escaped).encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("password")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_extracts_actual_pdf_text_and_canonical_skills():
    result = extract_resume(pdf_bytes("Software developer: Python, PostgreSQL, React.js and TypeScript."))
    assert "Software developer" in result["text"]
    assert result["skills"] == ["PostgreSQL", "Python", "React", "TypeScript"]
    assert "Very little" in result["warnings"][0]


@pytest.mark.parametrize("data", [b"", b"not a pdf", b"%PDF-1.7\nnot a valid document", "not bytes"])
def test_rejects_invalid_pdf(data):
    with pytest.raises(ValueError):
        extract_resume(data)


def test_size_limit_is_checked_before_parser():
    with pytest.raises(ValueError, match="10 MiB"):
        extract_resume(b"%PDF-1.7" + b"x" * MAX_PDF_BYTES)


def test_rejects_encrypted_pdf():
    with pytest.raises(ValueError, match="Encrypted"):
        extract_resume(pdf_bytes(encrypted=True))


def test_rejects_scans_or_blank_pdfs():
    with pytest.raises(ValueError, match="OCR"):
        extract_resume(pdf_bytes(text=""))


def test_rejects_too_many_pages():
    with pytest.raises(ValueError, match="20 pages"):
        extract_resume(pdf_bytes(pages=21))


def test_accepts_twenty_pages_and_warns_on_missing_text():
    result = extract_resume(pdf_bytes(pages=20))
    assert any("19 page(s)" in warning for warning in result["warnings"])


def test_rejects_excessive_extracted_text():
    with pytest.raises(ValueError, match="too much text"):
        extract_resume(pdf_bytes("Python " * 30_000))


def test_skill_boundaries_do_not_confuse_languages_or_common_words():
    found = extract_skills("JavaScript TypeScript MySQL. We go to a corporate event. An express service.")
    assert found == ["JavaScript", "MySQL", "TypeScript"]
    assert "Java" not in found
    assert "SQL" not in found
    assert "Go" not in found


def test_skill_aliases_and_symbol_languages():
    found = extract_skills("Skills: C, C++, C#, Go, R; PostgreSQL / AWS / React.js / CI/CD / .NET")
    assert set(found) == {"C", "C++", "C#", "Go", "R", "PostgreSQL", "AWS", "React", "CI/CD", ".NET"}


def test_ligatures_are_normalized():
    assert "Flask" in extract_skills("ﬂask")
