"""Assemble services from explicitly supplied dependencies."""

from __future__ import annotations

from typing import Any

from .accounts import AccountService
from .audits import AuditService
from .automation import AutomationService
from .contracts import Services
from .questionnaires import QuestionnaireService
from .resume_drafts import ResumeDraftService
from .resumes import ResumeService


def build_services(
    *,
    db: Any,
    coordinator: Any,
    login_manager: Any,
    resume_manager: Any,
    ai: Any,
) -> Services:
    """Build one coherent service graph from explicit production or test dependencies."""
    return Services(
        accounts=AccountService(db=db, coordinator=coordinator, login_manager=login_manager),
        resumes=ResumeService(db=db, coordinator=coordinator, resume_manager=resume_manager),
        resume_drafts=ResumeDraftService(
            db=db,
            coordinator=coordinator,
            resume_manager=resume_manager,
            ai=ai,
        ),
        automation=AutomationService(db=db, coordinator=coordinator),
        questionnaires=QuestionnaireService(db=db, coordinator=coordinator),
        audits=AuditService(
            db=db,
            coordinator=coordinator,
            resume_manager=resume_manager,
            ai=ai,
        ),
    )
