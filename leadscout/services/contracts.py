"""Public service contracts and immutable audit inputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .accounts import AccountService
    from .audits import AuditService
    from .automation import AutomationService
    from .questionnaires import QuestionnaireService
    from .resume_drafts import ResumeDraftService
    from .resumes import ResumeService


@dataclass(frozen=True, slots=True)
class AuditSource:
    """Immutable resume document selected for a later audit operation."""

    user_id: int
    account_id: int | None
    resume_snapshot_id: int | None
    resume_text: str


@dataclass(frozen=True, slots=True)
class Services:
    accounts: "AccountService"
    resumes: "ResumeService"
    resume_drafts: "ResumeDraftService"
    automation: "AutomationService"
    questionnaires: "QuestionnaireService"
    audits: "AuditService"
