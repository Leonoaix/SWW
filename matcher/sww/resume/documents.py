"""Turn an uploaded PDF, DOCX or Markdown file into plain text, and nothing more.

Extraction is separate from interpretation on purpose: this module decides what
the bytes say, `segment.py` decides how the resume is structured, and
`profile.py` decides what that means. Text never leaves the machine here, and
encrypted PDFs are refused even with an empty password.
"""

from __future__ import annotations

import io
import unicodedata
from dataclasses import dataclass, field

from pypdf import PdfReader

from ..config import MAX_PDF_PAGES, MAX_RESUME_BYTES, MAX_RESUME_CHARACTERS
from ..text import clean_unicode
from .docx import extract_docx


@dataclass
class Document:
    """Extracted resume text plus anything the reader could not vouch for."""
    text: str
    filename: str = ""
    warnings: list[str] = field(default_factory=list)


def extract_resume(data: bytes, filename: str = "") -> Document:
    """Extract PDF/DOCX by content, or UTF-8 Markdown by filename.

    Scans must be OCR'd before upload. This function does not send text to any
    service and deliberately rejects encrypted PDFs, even with empty passwords.
    """
    if not isinstance(data, bytes) or not data:
        raise ValueError("Upload a non-empty PDF, DOCX or MD resume.")
    if len(data) > MAX_RESUME_BYTES:
        raise ValueError("PDF, DOCX or MD resume must be 10 MiB or smaller.")
    if filename.lower().endswith((".md", ".markdown")):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Markdown resume must use UTF-8 encoding.") from exc
        if any(unicodedata.category(char) == "Cc" and char not in "\t\n\r" for char in text):
            raise ValueError("Markdown resume must contain text, not binary data.")
        text = clean_unicode(text).strip()
        if len(text) > MAX_RESUME_CHARACTERS:
            raise ValueError("Markdown contains too much text for a resume (maximum 200,000 characters).")
        if not any(char.isalnum() for char in text):
            raise ValueError("No readable text found in this Markdown resume.")
        # Keep source text for evidence matching; never render HTML or fetch links.
        return _document(text, filename, [])
    if data.startswith(b"PK"):
        text, warnings = extract_docx(data, MAX_RESUME_CHARACTERS)
        return _document(clean_unicode(text), filename, warnings)
    if not data.lstrip().startswith(b"%PDF-"):
        raise ValueError("Upload a PDF, DOCX or MD file. Legacy .doc and encrypted Word files are unsupported.")

    warnings = []
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are unsupported. Export an unencrypted PDF first.")
        if not 1 <= len(reader.pages) <= MAX_PDF_PAGES:
            raise ValueError("PDF resume must contain between 1 and 20 pages.")
        pages = []
        character_count = 0
        blank_pages = 0
        for page in reader.pages:
            page_text = clean_unicode(page.extract_text() or "").strip()
            if not page_text:
                blank_pages += 1
            character_count += len(page_text)
            if character_count > MAX_RESUME_CHARACTERS:
                raise ValueError("PDF contains too much text for a resume (maximum 200,000 characters).")
            pages.append(page_text)
        text = "\n\n".join(pages).strip()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Could not read this PDF. Export a fresh PDF with selectable text.") from exc

    if not any(character.isalnum() for character in text):
        raise ValueError("No readable text found. Image-only/scanned PDFs need OCR before upload.")
    if blank_pages:
        warnings.append("%d page(s) contained no extractable text; check for scanned content." % blank_pages)
    return _document(text, filename, warnings)


def _document(text: str, filename: str, warnings: list[str]) -> Document:
    if len(text) < 150:
        warnings.append("Very little resume text was extracted; check the preview before ranking.")
    if "\ufffd" in text:
        warnings.append("Some resume characters could not be decoded; check the extracted text preview.")
    return Document(text=text, filename=filename, warnings=warnings)
