"""Document parsing and generation entry points."""

from leadscout.documents.audit_report import generate_resume_audit_pdf
from leadscout.documents.pdf_reader import PDFValidationError, extract_text_from_pdf, missing_resume_fields

__all__ = [
    "PDFValidationError",
    "extract_text_from_pdf",
    "generate_resume_audit_pdf",
    "missing_resume_fields",
]
