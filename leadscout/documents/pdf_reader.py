"""Bounded extraction of text-based resume PDFs."""

from __future__ import annotations

import logging
import os
import unicodedata
from pathlib import Path

from pypdf import PdfReader

from leadscout.core.config import PDF_MAX_BYTES, PDF_MAX_PAGES, PDF_MAX_TEXT_CHARS
from leadscout.models.resumes import StructuredResume

logger = logging.getLogger(__name__)


class PDFValidationError(ValueError):
    """A user-safe validation failure for an uploaded resume PDF."""


def _validate_extracted_text(text: str) -> None:
    """Reject a broken PDF text layer without attempting to rewrite its encoding."""
    visible = [character for character in text if not character.isspace()]
    if not visible:
        raise PDFValidationError("В PDF не найден читаемый текст. Скан без текстового слоя не поддерживается.")
    unreadable = sum(
        character == "\ufffd"
        or (ord(character) < 32 and character not in "\t\n\r")
        or unicodedata.category(character) == "Co"
        for character in visible
    )
    # A few unusual glyphs can be legitimate, but a text layer made mostly of
    # replacement/control/private-use glyphs cannot be safely shown or sent to AI.
    if unreadable >= max(3, len(visible) // 50):
        raise PDFValidationError(
            "Текстовый слой PDF повреждён или использует неподдерживаемый шрифт. "
            "Загрузите PDF с корректным текстовым слоем; OCR не поддерживается."
        )


def extract_text_from_pdf(
    pdf_path: str | os.PathLike[str],
    *,
    max_bytes: int | None = None,
    max_pages: int | None = None,
    max_text_chars: int | None = None,
) -> str:
    """Extract bounded text without logging the potentially sensitive path."""
    byte_limit = PDF_MAX_BYTES if max_bytes is None else max_bytes
    page_limit = PDF_MAX_PAGES if max_pages is None else max_pages
    text_limit = PDF_MAX_TEXT_CHARS if max_text_chars is None else max_text_chars
    path = Path(pdf_path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PDFValidationError("PDF-файл недоступен.") from exc
    if size <= 0 or size > byte_limit:
        raise PDFValidationError(f"Размер PDF должен быть не больше {byte_limit // (1024 * 1024)} МБ.")
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise PDFValidationError("Защищенный паролем PDF не поддерживается.")
        if len(reader.pages) > page_limit:
            raise PDFValidationError(f"В PDF должно быть не больше {page_limit} страниц.")
        parts: list[str] = []
        total = 0
        for page in reader.pages:
            text = (page.extract_text() or "").strip()
            if not text:
                continue
            remaining = text_limit - total
            if remaining <= 0:
                break
            parts.append(text[:remaining])
            total += len(parts[-1])
        result = "\n".join(parts).strip()
    except PDFValidationError:
        raise
    except Exception as exc:
        raise PDFValidationError("Не удалось прочитать PDF. Проверьте, что файл не поврежден.") from exc
    if len(result) < 50:
        raise PDFValidationError("В PDF не найден читаемый текст. Скан без текстового слоя не поддерживается.")
    _validate_extracted_text(result)
    logger.info("Extracted %d characters from a PDF", len(result))
    return result


def missing_resume_fields(resume: StructuredResume) -> list[str]:
    """List required hh.ru fields that AI extraction did not find."""
    required = {
        "first_name": resume.first_name,
        "birth_date": resume.birth_date,
        "city": resume.city,
        "title": resume.title,
    }
    return [name for name, value in required.items() if not str(value or "").strip()]
