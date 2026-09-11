"""Compatibility facade for resume-audit PDF generation."""

from leadscout.documents.audit_report import (
    FONT_BOLD,
    FONT_REGULAR,
    _get_progress_bar,
    _safe,
    generate_resume_audit_pdf,
)

__all__ = ["FONT_BOLD", "FONT_REGULAR", "_get_progress_bar", "_safe", "generate_resume_audit_pdf"]
