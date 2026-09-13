from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pypdf import PdfReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

import leadscout.documents.pdf_reader as pdf_reader
import leadscout.integrations.resumes as hh_resume
from ai_handler import StructuredResume
from leadscout.services.resumes import ResumeService
from parsers.hh_resume import PDFValidationError, extract_text_from_pdf, missing_resume_fields
from utils.pdf_generator import generate_resume_audit_pdf


def test_pdf_extraction_and_limits(tmp_path, monkeypatch):
    path = tmp_path / "resume.pdf"
    canvas = Canvas(str(path))
    canvas.drawString(50, 800, "Python developer with production backend experience and measurable results.")
    canvas.save()
    assert "Python developer" in extract_text_from_pdf(path)

    monkeypatch.setattr(hh_resume, "PDF_MAX_BYTES", 10)
    with pytest.raises(PDFValidationError):
        extract_text_from_pdf(path)


def test_invalid_pdf_is_rejected(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"not a pdf but long enough to enter the parser" * 4)
    with pytest.raises(PDFValidationError):
        extract_text_from_pdf(path)


def test_pdf_text_layer_preserves_cyrillic_english_entities_and_line_breaks(tmp_path):
    path = tmp_path / "mixed-language.pdf"
    pdfmetrics.registerFont(TTFont("LeadScoutArial", r"C:\Windows\Fonts\arial.ttf"))
    canvas = Canvas(str(path))
    canvas.setFont("LeadScoutArial", 11)
    lines = [
        "Иван Петров — Backend Engineer",
        "• Python, SQL & R&D <platform>",
        "Опыт: production systems and API design.",
    ]
    for index, line in enumerate(lines):
        canvas.drawString(50, 800 - index * 20, line)
    canvas.save()

    text = extract_text_from_pdf(path)

    assert "Иван Петров" in text
    assert "Backend Engineer" in text
    assert "Python, SQL & R&D <platform>" in text
    assert "\n" in text


def test_pdf_with_garbled_text_layer_is_rejected_without_reencoding(tmp_path, monkeypatch):
    path = tmp_path / "garbled.pdf"
    path.write_bytes(b"%PDF-1.4 placeholder")

    class Page:
        def extract_text(self):
            return "\ufffd" * 60

    class Reader:
        is_encrypted = False
        pages = [Page()]

    monkeypatch.setattr(pdf_reader, "PdfReader", lambda _: Reader())
    with pytest.raises(PDFValidationError, match="неподдерживаемый шрифт"):
        pdf_reader.extract_text_from_pdf(path)


@pytest.mark.asyncio
async def test_pdf_validation_error_is_returned_to_the_import_operation():
    manager = SimpleNamespace(
        upload_pdf_resume_to_hh=AsyncMock(
            side_effect=PDFValidationError("В PDF не найден читаемый текст. Скан без текстового слоя не поддерживается.")
        )
    )
    service = ResumeService(
        db=SimpleNamespace(get_account_for_user=AsyncMock(return_value={"id": 7})),
        coordinator=None,
        resume_manager=manager,
    )

    result = await service.import_pdf(42, 7, "anonymized.pdf")

    assert result == {
        "status": "ERROR",
        "message": "В PDF не найден читаемый текст. Скан без текстового слоя не поддерживается.",
    }


def test_empty_and_too_many_pages_are_rejected(tmp_path, monkeypatch):
    empty_path = tmp_path / "empty.pdf"
    canvas = Canvas(str(empty_path))
    canvas.showPage()
    canvas.save()
    with pytest.raises(PDFValidationError):
        extract_text_from_pdf(empty_path)

    pages_path = tmp_path / "pages.pdf"
    canvas = Canvas(str(pages_path))
    for index in range(3):
        canvas.drawString(50, 800, f"Page {index} with enough readable resume text " * 3)
        canvas.showPage()
    canvas.save()
    monkeypatch.setattr(hh_resume, "PDF_MAX_PAGES", 2)
    with pytest.raises(PDFValidationError):
        extract_text_from_pdf(pages_path)


def test_resume_wizard_never_fabricates_required_fields():
    assert missing_resume_fields(StructuredResume()) == [
        "first_name",
        "birth_date",
        "city",
        "title",
    ]


def test_audit_pdf_escapes_model_content(tmp_path):
    path = tmp_path / "audit.pdf"
    generate_resume_audit_pdf(
        {
            "profession_name": "Backend <Engineer> & Lead",
            "overall_score": 80,
            "category_scores": {
                "hard_skills": 80,
                "impact_metrics": 80,
                "parseability": 80,
                "timeline": 80,
                "style": 80,
            },
            "summary_text": "Used <unsafe> & markup",
            "penalties": ["Missing <metrics> & details"],
            "top_recommendations": ["Add <numbers> & outcomes"],
            "insights": [
                {
                    "tier": 1,
                    "title": "Fix <title>",
                    "description": "Use A & B",
                    "score_impact": "+10 <points>",
                }
            ],
        },
        str(path),
    )
    assert path.exists() and path.stat().st_size > 0
    text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    assert "Backend <Engineer> & Lead" in text
