"""Bounded, local DOCX text extraction; never unpack files or follow external links."""

import io
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

MAX_DOCX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_DOCX_XML_BYTES = 8 * 1024 * 1024
MAX_DOCX_ENTRIES = 1000
WORD_NAMESPACES = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
)
REL_NAMESPACES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
)
CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
PACKAGE_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
MAIN_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"


def extract_docx(data: bytes, max_characters: int) -> tuple[str, list[str]]:
    warnings, chunks = [], []
    character_count = 0

    def append(text):
        nonlocal character_count
        character_count += len(text)
        if character_count > max_characters:
            raise ValueError("DOCX contains too much text for a resume (maximum 200,000 characters).")
        chunks.append(text)

    def walk(element, namespace, depth=0):
        if depth > 100:
            raise ValueError("DOCX XML is too deeply nested. Export a simpler DOCX or PDF.")
        tag = element.tag.removeprefix("{" + namespace + "}")
        if tag in {"del", "moveFrom", "instrText", "pPr", "rPr", "sectPr"}:
            return
        if tag == "txbxContent":
            append("\n")
        if tag == "r":
            props = element.find("{" + namespace + "}rPr")
            if props is not None and any(
                prop.tag == "{" + namespace + "}" + name
                and prop.get("{" + namespace + "}val", "true") not in {"0", "false", "off"}
                for prop in props for name in ("vanish", "webHidden")
            ):
                return
        if tag == "t":
            append(element.text or "")
        elif tag in {"tab", "br", "cr"}:
            append("\t" if tag == "tab" else "\n")
        # Word often stores two representations of a text box. Read only one.
        elif element.tag == "{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent":
            choices = list(element)
            if choices:
                walk(choices[0], namespace, depth + 1)
            return
        else:
            for child in element:
                walk(child, namespace, depth + 1)
        if tag in {"p", "tr", "txbxContent"}:
            append("\n")
        elif tag == "tc":
            append("\t")

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            if len(entries) > MAX_DOCX_ENTRIES or sum(entry.file_size for entry in entries) > MAX_DOCX_UNCOMPRESSED_BYTES:
                raise ValueError("DOCX archive is too large when decompressed. Export a smaller DOCX or PDF.")
            if len(names) != len(entries) or any(entry.flag_bits & 1 for entry in entries):
                raise ValueError("Encrypted or ambiguous DOCX archives are unsupported.")
            if any(name.lower().endswith("vbaproject.bin") for name in names):
                raise ValueError("Macro-enabled Word files are unsupported. Save as .docx or PDF.")

            def read_xml(name):
                with archive.open(name) as stream:
                    raw = stream.read(MAX_DOCX_XML_BYTES + 1)
                if len(raw) > MAX_DOCX_XML_BYTES:
                    raise ValueError("DOCX XML is too large. Export a smaller DOCX or PDF.")
                # Strip NUL bytes for detection in UTF-16/32 as well as UTF-8.
                if b"<!DOCTYPE" in raw.replace(b"\x00", b"").upper():
                    raise ValueError("DOCX XML with DTD declarations is unsupported.")
                return ET.fromstring(raw)

            types = read_xml("[Content_Types].xml")
            if not any(part.get("PartName") == "/word/document.xml" and part.get("ContentType") == MAIN_TYPE
                       for part in types.findall("{" + CONTENT_TYPES + "}Override")):
                raise ValueError("The uploaded archive is not a supported DOCX document. Save as .docx or PDF.")
            document = read_xml("word/document.xml")
            namespace = next((ns for ns in WORD_NAMESPACES if document.tag == "{" + ns + "}document"), None)
            if namespace is None or document.find("{" + namespace + "}body") is None:
                raise ValueError("DOCX document body is missing or invalid.")

            # Include only headers/footers referenced by this document, once each.
            references = [(kind, node.get("{" + rel_ns + "}id"))
                          for kind in ("header", "footer")
                          for node in document.iter("{" + namespace + "}" + kind + "Reference")
                          for rel_ns in REL_NAMESPACES if node.get("{" + rel_ns + "}id")]
            parts = {"header": [], "footer": []}
            if references:
                relationships = read_xml("word/_rels/document.xml.rels")
                links = {node.get("Id"): node for node in relationships.findall("{" + PACKAGE_RELS + "}Relationship")}
                seen = set()
                for kind, identifier in references:
                    link = links.get(identifier)
                    if link is None or link.get("TargetMode") == "External":
                        raise ValueError("DOCX header/footer is missing or external. Export a self-contained DOCX or PDF.")
                    target = link.get("Target", "")
                    name = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "word/" + target)
                    if not name.startswith("word/") or "\\" in name or ":" in name:
                        raise ValueError("DOCX contains an invalid header/footer path.")
                    if name not in seen:
                        part = read_xml(name)
                        if part.tag != "{" + namespace + "}" + ("hdr" if kind == "header" else "ftr"):
                            raise ValueError("DOCX header/footer is invalid.")
                        parts[kind].append(part)
                        seen.add(name)
            for part in parts["header"] + [document] + parts["footer"]:
                walk(part, namespace)
            if any(name.startswith("word/media/") for name in names):
                warnings.append("DOCX images are not OCR'd; any text inside images was not extracted.")
            if any(node.tag == "{" + namespace + "}altChunk" for node in document.iter()):
                raise ValueError("DOCX contains embedded document content. Export a regular DOCX or PDF first.")
    except ValueError:
        raise
    except (zipfile.BadZipFile, KeyError, ET.ParseError, RuntimeError, NotImplementedError, OSError) as exc:
        raise ValueError("Could not read this DOCX. Export a fresh, unencrypted .docx or PDF.") from exc
    text = re.sub(r"\n[\t ]*\n+", "\n\n", "".join(chunks)).strip()
    if not any(character.isalnum() for character in text):
        raise ValueError("No readable text found in DOCX. Image-only resumes need OCR before upload.")
    return text, warnings
