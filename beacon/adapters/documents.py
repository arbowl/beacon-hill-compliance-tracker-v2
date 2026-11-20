"""Reads creation dates and text from PDF and DOCX bytes."""

import html
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime

from pypdf import PdfReader
from pypdf.errors import PyPdfError

log = logging.getLogger(__name__)
_PDF_MAGIC = b"%PDF"
_ZIP_MAGIC = b"PK\x03\x04"
_CREATED = re.compile(r"<dcterms:created[^>]*>([^<]+)</dcterms:created>")
_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class DocumentContent:
    """What a document says about itself; empty when unreadable."""

    created_at: datetime | None
    text: str


_EMPTY = DocumentContent(None, "")


def _aware(value: datetime | None) -> datetime | None:
    return value if value is not None and value.tzinfo is not None else None


def read_pdf(body: bytes) -> DocumentContent:
    """Creation date (when timezone-aware) and concatenated page text."""
    try:
        reader = PdfReader(io.BytesIO(body))
        created = reader.metadata.creation_date if reader.metadata else None
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except (PyPdfError, ValueError, KeyError, TypeError) as error:
        log.warning("Unreadable PDF: %r", error)
        return _EMPTY
    return DocumentContent(_aware(created), text)


def read_docx(body: bytes) -> DocumentContent:
    """Creation date from core properties and paragraph text from the body."""
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", "replace")
            names = archive.namelist()
            core = (
                archive.read("docProps/core.xml").decode("utf-8", "replace")
                if "docProps/core.xml" in names
                else ""
            )
    except (zipfile.BadZipFile, KeyError) as error:
        log.warning("Unreadable DOCX: %r", error)
        return _EMPTY
    text = html.unescape(_TAG.sub("", xml.replace("</w:p>", "\n")))
    match = _CREATED.search(core)
    created = None
    if match:
        try:
            created = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
        except ValueError:
            created = None
    return DocumentContent(_aware(created), text)


def read_document(body: bytes) -> DocumentContent:
    """Dispatch on the file signature; anything else reads as empty."""
    if body.startswith(_PDF_MAGIC):
        return read_pdf(body)
    if body.startswith(_ZIP_MAGIC):
        return read_docx(body)
    log.warning("Unrecognized document format: %r", body[:8])
    return _EMPTY
