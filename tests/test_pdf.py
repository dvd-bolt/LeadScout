from __future__ import annotations

import pytest
from pypdf import PdfReader
from reportlab.pdfgen.canvas import Canvas

import parsers.hh_resume as hh_resume
from ai_handler import StructuredResume
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
