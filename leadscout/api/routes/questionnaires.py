from __future__ import annotations

from fastapi import APIRouter, Depends, status

from leadscout.runtime import AppContext
from leadscout.services import ServiceError

from ..dependencies import current_user, get_context, require_csrf, service_http_error
from ..presenters import public_questionnaire
from ..schemas import QuestionnaireConfirm, QuestionnaireUpdate

router = APIRouter(prefix="/questionnaires", tags=["questionnaires"])


@router.get("")
async def list_questionnaires(
    account_id: int | None = None,
    before_id: int | None = None,
    limit: int = 100,
    session: dict = Depends(current_user),
    context: AppContext = Depends(get_context),
) -> list[dict]:
    items = await context.db.list_pending_questionnaires(
        int(session["user_id"]), account_id=account_id, before_id=before_id, limit=limit
    )
    return [public_questionnaire(item) for item in items]


@router.get("/{apply_id}")
async def get_questionnaire(
    apply_id: int,
    session: dict = Depends(current_user),
    context: AppContext = Depends(get_context),
) -> dict:
    item = await context.db.get_pending_questionnaire_for_user(int(session["user_id"]), apply_id)
    if not item:
        raise service_http_error(ServiceError("NOT_FOUND", "Анкета не найдена"))
    return public_questionnaire(item)


@router.patch("/{apply_id}")
async def update_questionnaire(
    apply_id: int,
    payload: QuestionnaireUpdate,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    try:
        item = await context.services.questionnaires.edit(
            int(session["user_id"]),
            apply_id,
            payload.cover_letter,
            payload.answers,
            payload.expected_revision,
        )
    except ServiceError as exc:
        raise service_http_error(exc) from exc
    return public_questionnaire(item)


@router.post("/{apply_id}/confirm", status_code=status.HTTP_202_ACCEPTED)
async def confirm_questionnaire(
    apply_id: int,
    payload: QuestionnaireConfirm | None = None,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    try:
        return await context.services.questionnaires.confirm(
            int(session["user_id"]),
            apply_id,
            expected_revision=payload.expected_revision if payload else None,
        )
    except ServiceError as exc:
        raise service_http_error(exc) from exc


@router.post("/{apply_id}/skip")
async def skip_questionnaire(
    apply_id: int,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    try:
        item = await context.services.questionnaires.skip(int(session["user_id"]), apply_id)
    except ServiceError as exc:
        raise service_http_error(exc) from exc
    return public_questionnaire(item)
