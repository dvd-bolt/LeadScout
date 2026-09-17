"""Versioned resume-draft API."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from leadscout.runtime import AppContext
from leadscout.services import ServiceError

from ..dependencies import current_user, get_context, require_csrf, service_http_error
from .resumes import _read_pdf

router = APIRouter(prefix="/accounts/{account_id}/resume-drafts", tags=["resume-drafts"])


class CreateDraftBody(BaseModel):
    source: Literal["MANUAL", "PDF"] = "MANUAL"
    data: dict[str, Any] | None = None


class PatchDraftBody(BaseModel):
    expected_revision: int = Field(gt=0)
    current_step: str = Field(default="profession", min_length=1, max_length=100)
    data: dict[str, Any]


class PublishDraftBody(BaseModel):
    expected_revision: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=200)
    confirmation_fingerprint: str = Field(default="", max_length=100)


class ResumeDraftBody(BaseModel):
    expected_revision: int = Field(gt=0)
    confirmation_fingerprint: str = Field(default="", max_length=100)


async def _service_call(function, *args):
    try:
        return await function(*args)
    except ServiceError as exc:
        raise service_http_error(exc) from exc


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_draft(
    account_id: int,
    payload: CreateDraftBody,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    return await _service_call(
        context.services.resume_drafts.create,
        int(session["user_id"]),
        account_id,
        payload.source,
        payload.data,
    )


@router.get("")
async def list_drafts(
    account_id: int,
    session: dict = Depends(current_user),
    context: AppContext = Depends(get_context),
) -> list[dict]:
    return await _service_call(context.services.resume_drafts.list, int(session["user_id"]), account_id)


@router.get("/{draft_id}")
async def get_draft(
    account_id: int,
    draft_id: int,
    session: dict = Depends(current_user),
    context: AppContext = Depends(get_context),
) -> dict:
    return await _service_call(
        context.services.resume_drafts.get, int(session["user_id"]), account_id, draft_id
    )


@router.patch("/{draft_id}")
async def patch_draft(
    account_id: int,
    draft_id: int,
    payload: PatchDraftBody,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    return await _service_call(
        context.services.resume_drafts.update,
        int(session["user_id"]),
        account_id,
        draft_id,
        payload.expected_revision,
        payload.data,
        payload.current_step,
    )


@router.delete("/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_draft(
    account_id: int,
    draft_id: int,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> None:
    await _service_call(
        context.services.resume_drafts.delete, int(session["user_id"]), account_id, draft_id
    )


@router.post("/{draft_id}/extract-pdf", status_code=status.HTTP_202_ACCEPTED)
async def extract_pdf(
    account_id: int,
    draft_id: int,
    file: UploadFile = File(...),
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    user_id = int(session["user_id"])
    await _service_call(context.services.resume_drafts.get, user_id, account_id, draft_id)
    contents = await _read_pdf(file, context.settings.pdf_max_bytes)

    async def job() -> dict:
        with tempfile.TemporaryDirectory(prefix="leadscout-resume-pdf-") as directory:
            path = Path(directory) / "resume.pdf"
            path.write_bytes(contents)
            return await context.services.resume_drafts.extract_pdf(
                user_id, account_id, draft_id, str(path)
            )

    return await context.operations.schedule(
        user_id,
        "resume-draft-extract",
        job,
        resource=str(draft_id),
        account_id=account_id,
    )


@router.post("/{draft_id}/validate")
async def validate_draft(
    account_id: int,
    draft_id: int,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    return await _service_call(
        context.services.resume_drafts.validate, int(session["user_id"]), account_id, draft_id
    )


@router.post("/{draft_id}/preflight", status_code=status.HTTP_202_ACCEPTED)
async def preflight_draft(
    account_id: int,
    draft_id: int,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    user_id = int(session["user_id"])
    await _service_call(context.services.resume_drafts.get, user_id, account_id, draft_id)
    return await context.operations.schedule(
        user_id,
        "resume-draft-preflight",
        lambda: context.services.resume_drafts.preflight(user_id, account_id, draft_id),
        resource=str(draft_id),
        account_id=account_id,
    )


@router.post("/{draft_id}/publish", status_code=status.HTTP_202_ACCEPTED)
async def publish_draft(
    account_id: int,
    draft_id: int,
    payload: PublishDraftBody,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    user_id = int(session["user_id"])
    attempt, reused = await _service_call(
        context.services.resume_drafts.register_publish,
        user_id,
        account_id,
        draft_id,
        payload.expected_revision,
        payload.idempotency_key,
        payload.confirmation_fingerprint,
    )
    if reused:
        if attempt.get("operation_id"):
            return {
                "operation_id": attempt["operation_id"],
                "attempt_id": attempt["id"],
                "status": attempt["status"],
                "reused": True,
            }
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "code": "PUBLISH_ALREADY_STARTED",
                "message": "Публикация уже зарегистрирована. Выполните сверку результата.",
                "attempt_id": attempt["id"],
            },
        )
    operation = await context.operations.schedule(
        user_id,
        "resume-draft-publish",
        lambda: context.services.resume_drafts.run_publish(user_id, attempt["id"]),
        resource=str(draft_id),
        account_id=account_id,
    )
    await context.db.set_resume_attempt_operation_id(user_id, attempt["id"], operation["operation_id"])
    return {**operation, "attempt_id": attempt["id"], "reused": False}


@router.post("/{draft_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume_publish(
    account_id: int,
    draft_id: int,
    payload: ResumeDraftBody,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    user_id = int(session["user_id"])
    await _service_call(context.services.resume_drafts.get, user_id, account_id, draft_id)
    return await context.operations.schedule(
        user_id,
        "resume-draft-resume",
        lambda: context.services.resume_drafts.resume(
            user_id, account_id, draft_id, payload.expected_revision, payload.confirmation_fingerprint
        ),
        resource=str(draft_id),
        account_id=account_id,
    )


@router.post("/{draft_id}/reconcile", status_code=status.HTTP_202_ACCEPTED)
async def reconcile_publish(
    account_id: int,
    draft_id: int,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    user_id = int(session["user_id"])
    await _service_call(context.services.resume_drafts.get, user_id, account_id, draft_id)
    return await context.operations.schedule(
        user_id,
        "resume-draft-reconcile",
        lambda: context.services.resume_drafts.reconcile(user_id, account_id, draft_id),
        resource=str(draft_id),
        account_id=account_id,
    )
