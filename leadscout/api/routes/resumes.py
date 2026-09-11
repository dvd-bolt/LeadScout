from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from leadscout.runtime import AppContext
from leadscout.services import ServiceError

from ..dependencies import current_user, get_context, require_csrf, service_http_error
from ..schemas import ConfirmBody

router = APIRouter(prefix="/accounts/{account_id}/resumes", tags=["resumes"])
PDF_TYPES = {"application/pdf", "application/x-pdf"}


async def _read_pdf(file: UploadFile, maximum: int) -> bytes:
    if file.content_type not in PDF_TYPES:
        await file.close()
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Нужен PDF-файл резюме")
    contents = await file.read(maximum + 1)
    await file.close()
    if len(contents) > maximum:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "PDF слишком большой")
    return contents


@router.get("")
async def list_resumes(
    account_id: int,
    session: dict = Depends(current_user),
    context: AppContext = Depends(get_context),
) -> list[dict]:
    user_id = int(session["user_id"])
    if not await context.db.get_account_for_user(user_id, account_id):
        raise service_http_error(ServiceError("NOT_FOUND", "Аккаунт не найден"))
    return await context.db.list_resume_snapshots(user_id, account_id)


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_resumes(
    account_id: int,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    user_id = int(session["user_id"])
    if not await context.db.get_account_for_user(user_id, account_id):
        raise service_http_error(ServiceError("NOT_FOUND", "Аккаунт не найден"))
    return await context.operations.schedule(
        user_id,
        "resume-sync",
        lambda: context.services.resumes.sync(user_id, account_id),
        str(account_id),
        account_id=account_id,
    )


@router.post("/{snapshot_id}/activate")
async def activate_resume(
    account_id: int,
    snapshot_id: int,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    resume = await context.db.set_active_resume_snapshot(int(session["user_id"]), account_id, snapshot_id)
    if not resume:
        raise service_http_error(ServiceError("NOT_FOUND", "Резюме не найдено"))
    return resume


@router.post("/import", status_code=status.HTTP_202_ACCEPTED)
async def import_resume(
    account_id: int,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
    file: UploadFile = File(...),
    structured_json: str = Form(default=""),
) -> dict:
    user_id = int(session["user_id"])
    if not await context.db.get_account_for_user(user_id, account_id):
        raise service_http_error(ServiceError("NOT_FOUND", "Аккаунт не найден"))
    contents = await _read_pdf(file, context.settings.pdf_max_bytes)
    try:
        structured = json.loads(structured_json) if structured_json else None
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Структура резюме содержит некорректный JSON",
        ) from exc
    if structured is not None and not isinstance(structured, dict):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Структура резюме должна быть JSON-объектом",
        )

    async def job() -> dict:
        with tempfile.TemporaryDirectory(prefix="leadscout-import-") as directory:
            path = Path(directory) / "resume.pdf"
            path.write_bytes(contents)
            return await context.services.resumes.import_pdf(user_id, account_id, str(path), structured)

    return await context.operations.schedule(user_id, "resume-import", job, account_id=account_id)


@router.delete("/{snapshot_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_resume(
    account_id: int,
    snapshot_id: int,
    payload: ConfirmBody,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    if not payload.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Подтвердите удаление резюме")
    user_id = int(session["user_id"])

    async def job() -> dict:
        result = await context.services.resumes.delete(user_id, account_id, snapshot_id)
        if result.get("status") != "SUCCESS":
            return {
                **result,
                "status": result.get("status") or "ERROR",
                "message": result.get("message") or "hh.ru не подтвердил удаление резюме",
            }
        return result

    # Validate ownership synchronously so the endpoint can still return 404.
    snapshot = await context.db.get_resume_snapshot_for_user(user_id, snapshot_id)
    if not snapshot or int(snapshot.get("account_id", -1)) != account_id:
        raise service_http_error(ServiceError("NOT_FOUND", "Резюме не найдено"))
    return await context.operations.schedule(user_id, "resume-delete", job, str(snapshot_id), account_id=account_id)
