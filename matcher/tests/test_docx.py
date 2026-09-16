"""Synthetic OOXML fixtures are built in memory, never from personal documents."""
from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from sww.resume import extract_resume, extract_skills

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def package(parts):
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    return output.getvalue()


def docx_bytes(text="Python, SQL, React. Software developer.", *, body=None, extra=None):
    content = body if body is not None else f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>"
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>',
        "_rels/.rels": f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{R}/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>{content}</w:body></w:document>',
    }
    parts.update(extra or {})
    return package(parts)


def test_docx_paragraph_runs_tables_and_unicode_preserve_reading_order():
    result = extract_resume(docx_bytes(body='''
        <w:p><w:r><w:t>软件工程 Python</w:t></w:r><w:r><w:t>, SQL</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>PostgreSQL</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Docker</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        <w:p><w:hyperlink r:id="external"><w:r><w:t>GitHub Actions</w:t></w:r></w:hyperlink></w:p>'''))
    assert result.text.startswith("软件工程 Python, SQL\n")
    assert result.text.index("PostgreSQL") < result.text.index("Docker") < result.text.index("GitHub")
    assert extract_skills(result.text) == ["Docker", "GitHub Actions", "PostgreSQL", "Python", "SQL"]


def test_docx_reads_referenced_headers_footers_only_once():
    body = '<w:p><w:r><w:t>Python developer</w:t></w:r></w:p><w:sectPr>' \
        '<w:headerReference r:id="head"/><w:headerReference r:id="head"/><w:footerReference r:id="foot"/></w:sectPr>'
    result = extract_resume(docx_bytes(body=body, extra={
        "word/_rels/document.xml.rels": f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="head" Type="{R}/header" Target="header1.xml"/>'
            f'<Relationship Id="foot" Type="{R}/footer" Target="/word/footer1.xml"/></Relationships>',
        "word/header1.xml": f'<w:hdr xmlns:w="{W}"><w:p><w:r><w:t>Example Student</w:t></w:r></w:p></w:hdr>',
        "word/footer1.xml": f'<w:ftr xmlns:w="{W}"><w:p><w:r><w:t>Available four months</w:t></w:r></w:p></w:ftr>',
        "word/header-unused.xml": f'<w:hdr xmlns:w="{W}"><w:p><w:r><w:t>Rust</w:t></w:r></w:p></w:hdr>',
    }))
    assert result.text.count("Example Student") == 1
    assert result.text.startswith("Example Student\nPython")
    assert result.text.endswith("Available four months")
    assert "Rust" not in extract_skills(result.text)


def test_docx_skips_deleted_hidden_and_field_instructions_but_reads_textboxes():
    result = extract_resume(docx_bytes(body='''
        <w:p><w:del><w:r><w:t>Rust</w:t></w:r></w:del>
        <w:r><w:rPr><w:vanish/></w:rPr><w:t>Java</w:t></w:r>
        <w:r><w:instrText>INCLUDETEXT https://example.invalid/SQL</w:instrText></w:r>
        <w:ins><w:r><w:t>Python</w:t><w:tab/><w:t>Docker</w:t></w:r></w:ins>
        <w:r><w:pict><w:txbxContent><w:p><w:r><w:t>React</w:t></w:r></w:p></w:txbxContent></w:pict></w:r></w:p>'''))
    assert extract_skills(result.text) == ["Docker", "Python", "React"]
    assert result.text.count("React") == 1


def test_docx_alternate_textbox_representation_is_not_duplicated():
    content = '<w:p><w:r><w:t>Python services</w:t></w:r></w:p>'
    result = extract_resume(docx_bytes(body=f'<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
        f'<mc:Choice Requires="wps">{content}</mc:Choice><mc:Fallback>{content}</mc:Fallback></mc:AlternateContent>'))
    assert result.text.count("Python services") == 1


@pytest.mark.parametrize("data", [b"PKbroken", package({"random.txt": "Python"}), b"\xd0\xcf\x11\xe0old Word document"])
def test_rejects_corrupt_zip_non_docx_and_legacy_word(data):
    with pytest.raises(ValueError, match="DOCX|Word"):
        extract_resume(data)


def test_docx_blank_and_images_require_ocr():
    with pytest.raises(ValueError, match="OCR"):
        extract_resume(docx_bytes("", extra={"word/media/image1.png": b"image bytes"}))
    result = extract_resume(docx_bytes(extra={"word/media/image1.png": b"image bytes"}))
    assert any("not OCR" in warning for warning in result.warnings)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_rejects_xml_dtd_in_all_common_encodings(encoding):
    xml = f'<?xml version="1.0" encoding="{encoding}"?><!DOCTYPE document [<!ENTITY x "Python">]>' \
        f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>'
    with pytest.raises(ValueError, match="DTD"):
        extract_resume(docx_bytes(extra={"word/document.xml": xml.encode(encoding)}))


def test_docx_rejects_excessive_text_and_decompressed_content(monkeypatch):
    with pytest.raises(ValueError, match="too much text"):
        extract_resume(docx_bytes("Python " * 30_000))
    monkeypatch.setattr("sww.resume.docx.MAX_DOCX_UNCOMPRESSED_BYTES", 1024)
    with pytest.raises(ValueError, match="decompressed"):
        extract_resume(docx_bytes("Python " * 1000))


def test_docx_rejects_large_xml_before_parsing(monkeypatch):
    monkeypatch.setattr("sww.resume.docx.MAX_DOCX_XML_BYTES", 1024)
    with pytest.raises(ValueError, match="XML is too large"):
        extract_resume(docx_bytes("Python " * 1000))


@pytest.mark.parametrize("target,mode", [("https://example.invalid/header.xml", 'TargetMode="External"'), ("../../private.xml", "")])
def test_docx_rejects_external_and_traversing_header_references(target, mode):
    with pytest.raises(ValueError, match="external|invalid"):
        extract_resume(docx_bytes(body='<w:sectPr><w:headerReference r:id="head"/></w:sectPr>', extra={
            "word/_rels/document.xml.rels": f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="head" Type="{R}/header" Target="{target}" {mode}/></Relationships>',
        }))


def test_docx_rejects_macros():
    with pytest.raises(ValueError, match="Macro"):
        extract_resume(docx_bytes(extra={"word/vbaProject.bin": b"macro"}))
