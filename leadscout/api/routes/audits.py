from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from leadscout.documents.audit_report import generate_resume_audit_pdf
from leadscout.documents.pdf_reader import PDFValidationError, extract_text_from_pdf
from leadscout.runtime import AppContext
from leadscout.services import ServiceError

from ..dependencies import current_user, get_context, require_csrf, service_http_error
from ..schemas import AuditRequest, MatchRequest
from .resumes import _read_pdf

router = APIRouter(prefix="/audits", tags=["audits"])


def temporary_pdf(prefix: str) -> Path:
    descriptor, filename = tempfile.mkstemp(prefix=prefix, suffix=".pdf")
    os.close(descriptor)
    return Path(filename)


@router.get("")
async def list_audits(
    account_id: int | None = None,
    session: dict = Depends(current_user),
    context: AppContext = Depends(get_context),
) -> list[dict]:
    return await context.db.list_resume_audits(int(session["user_id"]), account_id=account_id)


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_audit(
    payload: AuditRequest,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    user_id = int(session["user_id"])
    try:
        source = await context.services.audits.prepare(
            user_id,
            payload.account_id,
            payload.resume_snapshot_id,
            payload.resume_text,
        )
    except ServiceError as exc:
        raise service_http_error(exc) from exc
    return await context.operations.schedule(user_id, "resume-audit", lambda: context.services.audits.run(source))


@router.post("/pdf", status_code=status.HTTP_202_ACCEPTED)
async def create_pdf_audit(
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
    file: UploadFile = File(...),
    account_id: int | None = Form(default=None),
) -> dict:
    user_id = int(session["user_id"])
    if account_id is not None and not await context.db.get_account_for_user(user_id, account_id):
        raise service_http_error(ServiceError("NOT_FOUND", "Аккаунт не найден"))
    contents = await _read_pdf(file, context.settings.pdf_max_bytes)

    async def job() -> dict:
        with tempfile.TemporaryDirectory(prefix="leadscout-audit-source-") as directory:
            path = Path(directory) / "resume.pdf"
            path.write_bytes(contents)
            try:
                resume_text = await asyncio.to_thread(extract_text_from_pdf, path)
            except PDFValidationError as exc:
                raise ServiceError("INVALID_INPUT", str(exc)) from exc
        source = await context.services.audits.prepare(user_id, account_id=account_id, resume_text=resume_text)
        return await context.services.audits.run(source)

    return await context.operations.schedule(user_id, "pdf-resume-audit", job)


@router.post("/{audit_id}/match", status_code=status.HTTP_202_ACCEPTED)
async def match_audit(
    audit_id: int,
    payload: MatchRequest,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    user_id = int(session["user_id"])
    # Synchronous ownership check keeps missing audits as an immediate 404.
    audit = await context.db.get_resume_audit_for_user(user_id, audit_id)
    if not audit or not str(audit.get("source_resume_text") or "").strip():
        raise service_http_error(ServiceError("NOT_FOUND", "Исходный документ аудита не найден"))
    return await context.operations.schedule(
        user_id,
        "vacancy-match",
        lambda: context.services.audits.match(
            user_id,
            audit_id,
            vacancy_text=payload.vacancy_text,
            vacancy_url=payload.vacancy_url,
        ),
    )


@router.get("/{audit_id}/report")
async def audit_report(
    audit_id: int,
    session: dict = Depends(current_user),
    context: AppContext = Depends(get_context),
) -> FileResponse:
    audit = await context.db.get_resume_audit_for_user(int(session["user_id"]), audit_id)
    if not audit:
        raise service_http_error(ServiceError("NOT_FOUND", "Аудит не найден"))
    temporary = temporary_pdf("leadscout-audit-")
    try:
        await asyncio.to_thread(generate_resume_audit_pdf, audit, str(temporary))
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return FileResponse(
        temporary,
        media_type="application/pdf",
        filename=f"LeadScout_Audit_{audit_id}.pdf",
        background=BackgroundTask(temporary.unlink, missing_ok=True),
    )
