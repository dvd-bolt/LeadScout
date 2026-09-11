"""Legacy resume entrypoints using the default context."""

from leadscout.compat import ContextProxy
from leadscout.integrations.resumes import PDFValidationError, extract_text_from_pdf, missing_resume_fields
from leadscout.models.resumes import StructuredResume

HHResumeManager = ContextProxy("resume_manager")
__all__ = [
    "HHResumeManager",
    "PDFValidationError",
    "extract_text_from_pdf",
    "StructuredResume",
    "missing_resume_fields",
]
