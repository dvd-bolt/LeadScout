"""Authenticated FastAPI surface for the personal LeadScout Telegram Mini App."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from ai_handler import analyze_resume_quality, match_resume_to_vacancy
from config import (
    BOT_TOKEN,
    OWNER_TELEGRAM_ID,
    PDF_MAX_BYTES,
    WEB_APP_ORIGINS,
    WEB_SECURE_COOKIES,
    WEB_SESSION_TTL_SEC,
)
from database import (
    AccountLimitError,
    DuplicateAccountError,
    complete_operation,
    create_hh_account,
    create_operation,
    delete_hh_account_for_user,
    get_account_by_login,
    get_account_for_user,
    get_active_account,
    get_active_resume_snapshot,
    get_application_stats,
    get_operation_for_user,
    get_or_create_user,
    get_pending_questionnaire_for_user,
    get_resume_audit_for_user,
    get_resume_snapshot_for_user,
    get_user_accounts,
    list_application_events,
    list_pending_questionnaires,
    list_resume_audits,
    list_resume_snapshots,
    save_resume_audit,
    set_active_account,
    set_active_resume_snapshot,
    start_operation,
    update_account_settings_for_user,
    update_pending_questionnaire_answers,
    update_pending_questionnaire_letter,
)
from parsers.hh_login import HHLoginManager
from parsers.hh_resume import HHResumeManager, PDFValidationError, extract_text_from_pdf
from utils.pdf_generator import generate_resume_audit_pdf
from utils.validation import normalize_proxy_url
from worker import task_coordinator

logger = logging.getLogger(__name__)
WEB_DIR = Path(__file__).with_name("web")
WEB_DIST_DIR = WEB_DIR / "dist"
SESSION_COOKIE = "leadscout_session"


class TelegramLogin(BaseModel):
    init_data: str = Field(min_length=20, max_length=10_000)


class AccountPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_name: str | None = Field(default=None, max_length=120)
    keywords: str | None = Field(default=None, max_length=1_000)
    stop_words: str | None = Field(default=None, max_length=1_000)
    min_salary: int | None = Field(default=None, ge=0, le=100_000_000)
    daily_limit: int | None = Field(default=None, ge=1, le=200)
    only_remote: bool | None = None
    send_cover_letter: bool | None = None
    proxy_url: str | None = Field(default=None, max_length=2_000)


class LoginStart(BaseModel):
    phone_or_email: str = Field(min_length=5, max_length=254)
    account_name: str = Field(default="", max_length=120)


class OtpSubmission(BaseModel):
    code: str = Field(pattern=r"^\d{4,8}$")


class CaptchaSubmission(BaseModel):
    code: str = Field(min_length=1, max_length=32)


class ConfirmBody(BaseModel):
    confirm: bool


class QuestionnaireUpdate(BaseModel):
    cover_letter: str | None = Field(default=None, min_length=1, max_length=10_000)
    answers: list[dict] | None = Field(default=None, max_length=50)


class AuditRequest(BaseModel):
    account_id: int | None = None
    resume_snapshot_id: int | None = None
    resume_text: str | None = Field(default=None, min_length=50, max_length=50_000)


class MatchRequest(BaseModel):
    vacancy_text: str = Field(min_length=15, max_length=50_000)


def _http_error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail)


def _sign_session(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(BOT_TOKEN.encode("utf-8"), raw, hashlib.sha256).digest()
    return f"{base64.urlsafe_b64encode(raw).decode().rstrip('=')}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def _decode_session(token: str) -> dict:
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        raw = base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4))
        signature = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        expected = hmac.new(BOT_TOKEN.encode("utf-8"), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        payload = json.loads(raw)
        if int(payload["expires_at"]) < int(time.time()):
            raise ValueError("expired")
        return payload
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _http_error(status.HTTP_401_UNAUTHORIZED, "Сессия Mini App истекла") from exc


def _validate_telegram_init_data(init_data: str) -> dict:
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError as exc:
        raise _http_error(status.HTTP_401_UNAUTHORIZED, "Данные Telegram некорректны") from exc
    supplied_hash = pairs.pop("hash", "")
    if not supplied_hash:
        raise _http_error(status.HTTP_401_UNAUTHORIZED, "Telegram не передал подпись приложения")
    check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied_hash, expected_hash):
        raise _http_error(status.HTTP_401_UNAUTHORIZED, "Подпись Telegram не прошла проверку")
    try:
        auth_date = int(pairs["auth_date"])
        user = json.loads(pairs["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _http_error(status.HTTP_401_UNAUTHORIZED, "Данные Telegram неполные") from exc
    if abs(int(time.time()) - auth_date) > 300:
        raise _http_error(status.HTTP_401_UNAUTHORIZED, "Данные Telegram устарели; откройте приложение заново")
    if OWNER_TELEGRAM_ID is None or user_id != OWNER_TELEGRAM_ID:
        raise _http_error(status.HTTP_403_FORBIDDEN, "Этот личный кабинет недоступен для данного аккаунта")
    return {"id": user_id, "name": user.get("first_name") or "", "username": user.get("username") or ""}


async def current_user(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise _http_error(status.HTTP_401_UNAUTHORIZED, "Откройте приложение через Telegram")
    session = _decode_session(token)
    user_id = int(session["user_id"])
    if OWNER_TELEGRAM_ID is None or user_id != OWNER_TELEGRAM_ID:
        raise _http_error(status.HTTP_403_FORBIDDEN, "Нет доступа к личному кабинету")
    return session


async def require_csrf(request: Request, session: dict = Depends(current_user)) -> dict:
    origin = request.headers.get("origin", "").rstrip("/")
    csrf = request.headers.get("x-csrf-token", "")
    if origin not in WEB_APP_ORIGINS or not hmac.compare_digest(csrf, str(session.get("csrf", ""))):
        raise _http_error(status.HTTP_403_FORBIDDEN, "Проверка запроса не пройдена")
    return session


def _automation_state(account: dict) -> str:
    session_status = str(account.get("session_status") or "")
    if session_status in {"ERROR", "FAILED"}:
        return "ERROR"
    if session_status != "ACTIVE":
        return "NEEDS_LOGIN"
    if task_coordinator.is_running(int(account.get("user_id", 0)), int(account.get("id", 0))):
        return "SEARCHING"
    if account.get("auto_apply_enabled"):
        return "QUEUED"
    return "WAITING"


def _public_account(account: dict) -> dict:
    return {
        "id": account["id"],
        "account_name": account.get("account_name") or account.get("phone_or_email"),
        "session_status": account.get("session_status"),
        "active_resume_hh_id": account.get("active_resume_hh_id", ""),
        "active_resume_title": account.get("active_resume_title", ""),
        "daily_limit": account.get("daily_limit", 50),
        "applied_today": account.get("applied_today", 0),
        "applied_date": account.get("applied_date", ""),
        "auto_apply_enabled": bool(account.get("auto_apply_enabled")),
        "automation_state": _automation_state(account),
        "only_remote": bool(account.get("only_remote")),
        "send_cover_letter": bool(account.get("send_cover_letter")),
        "min_salary": account.get("min_salary", 0),
        "keywords": account.get("keywords", ""),
        "stop_words": account.get("stop_words", ""),
        "has_proxy": bool(account.get("proxy_url")),
        "last_synced_at": account.get("last_synced_at", ""),
        "next_scheduled_search_at": account.get("next_scheduled_search_at", ""),
        "resume_ready": bool(account.get("resume_text", "").strip() and account.get("active_resume_hh_id")),
    }


def _decode_questionnaire(item: dict) -> dict:
    result = dict(item)
    for field in ("questions_json", "ai_payload_json"):
        try:
            result[field.removesuffix("_json")] = json.loads(result.pop(field) or "[]")
        except json.JSONDecodeError:
            result[field.removesuffix("_json")] = [] if field == "questions_json" else {}
    result.pop("resume_text", None)
    return result


async def _run_operation(
    operation_id: str, user_id: int, job: Callable[[], Awaitable[dict]]
) -> None:
    await start_operation(operation_id, user_id)
    try:
        result = await job()
    except asyncio.CancelledError:
        await complete_operation(operation_id, user_id, error="Операция отменена")
        raise
    except Exception as exc:
        logger.exception("Mini App operation %s failed", operation_id)
        await complete_operation(operation_id, user_id, error=f"Ошибка операции: {type(exc).__name__}")
    else:
        await complete_operation(operation_id, user_id, result=result)


async def schedule_operation(user_id: int, kind: str, job: Callable[[], Awaitable[dict]]) -> dict:
    operation_id = str(uuid.uuid4())
    await create_operation(operation_id, user_id, kind)
    asyncio.create_task(_run_operation(operation_id, user_id, job), name=f"mini-app-{kind}-{operation_id}")
    return {"operation_id": operation_id, "status": "PENDING"}


def _captcha_response(result: dict) -> dict:
    response = {key: value for key, value in result.items() if key != "captcha_bytes"}
    captcha = result.get("captcha_bytes")
    if captcha:
        response["captcha_data_uri"] = "data:image/png;base64," + base64.b64encode(captcha).decode("ascii")
    return response


def create_app() -> FastAPI:
    app = FastAPI(title="LeadScout Mini App API", version="1.0.0", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthcheck() -> dict:
        return {"status": "ok"}

    @app.post("/api/v1/auth/telegram")
    async def authenticate(payload: TelegramLogin, response: Response) -> dict:
        telegram_user = _validate_telegram_init_data(payload.init_data)
        await get_or_create_user(telegram_user["id"])
        csrf = secrets.token_urlsafe(32)
        token = _sign_session(
            {
                "user_id": telegram_user["id"],
                "csrf": csrf,
                "expires_at": int(time.time()) + WEB_SESSION_TTL_SEC,
            }
        )
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=WEB_SESSION_TTL_SEC,
            httponly=True,
            secure=WEB_SECURE_COOKIES,
            samesite="strict",
            path="/",
        )
        return {"user": telegram_user, "csrf_token": csrf, "expires_in": WEB_SESSION_TTL_SEC}

    @app.post("/api/v1/auth/logout")
    async def logout(response: Response, _: dict = Depends(require_csrf)) -> Response:
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/v1/me")
    async def get_me(session: dict = Depends(current_user)) -> dict:
        user_id = int(session["user_id"])
        accounts = await get_user_accounts(user_id)
        active = await get_active_account(user_id)
        pending = await list_pending_questionnaires(user_id, limit=100)
        stats = await get_application_stats(user_id, active["id"] if active else None)
        events = await list_application_events(user_id, limit=5, account_id=active["id"] if active else None)
        return {
            "user_id": user_id,
            "csrf_token": session["csrf"],
            "accounts": [_public_account(item) for item in accounts],
            "active_account_id": active["id"] if active else None,
            "stats": stats,
            "pending_review_count": sum(item["status"] != "SUBMITTED" for item in pending),
            "recent_events": events,
            "next_scheduled_search_at": active.get("next_scheduled_search_at", "") if active else "",
        }

    @app.get("/api/v1/accounts")
    async def list_accounts(session: dict = Depends(current_user)) -> list[dict]:
        return [_public_account(item) for item in await get_user_accounts(int(session["user_id"]))]

    @app.patch("/api/v1/accounts/{account_id}")
    async def update_account(account_id: int, payload: AccountPatch, session: dict = Depends(require_csrf)) -> dict:
        values = payload.model_dump(exclude_none=True)
        if "proxy_url" in values:
            try:
                values["proxy_url"] = normalize_proxy_url(values["proxy_url"]) if values["proxy_url"] else ""
            except ValueError as exc:
                raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Некорректный адрес прокси") from exc
        for key in ("only_remote", "send_cover_letter"):
            if key in values:
                values[key] = int(values[key])
        if not await update_account_settings_for_user(int(session["user_id"]), account_id, **values):
            raise _http_error(status.HTTP_404_NOT_FOUND, "Аккаунт не найден")
        account = await get_account_for_user(int(session["user_id"]), account_id)
        return _public_account(account or {})

    @app.post("/api/v1/accounts/{account_id}/activate")
    async def activate_account(account_id: int, session: dict = Depends(require_csrf)) -> dict:
        if not await set_active_account(int(session["user_id"]), account_id):
            raise _http_error(status.HTTP_404_NOT_FOUND, "Аккаунт не найден")
        return {"active_account_id": account_id}

    @app.delete("/api/v1/accounts/{account_id}")
    async def delete_account(account_id: int, payload: ConfirmBody, session: dict = Depends(require_csrf)) -> Response:
        if not payload.confirm:
            raise _http_error(status.HTTP_400_BAD_REQUEST, "Подтвердите удаление аккаунта")
        await task_coordinator.stop_account(int(session["user_id"]), account_id)
        if not await delete_hh_account_for_user(int(session["user_id"]), account_id):
            raise _http_error(status.HTTP_404_NOT_FOUND, "Аккаунт не найден")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/v1/login-flows/start", status_code=status.HTTP_202_ACCEPTED)
    async def start_login(payload: LoginStart, session: dict = Depends(require_csrf)) -> dict:
        user_id = int(session["user_id"])
        account = await get_account_by_login(user_id, payload.phone_or_email)
        if not account:
            try:
                account = await create_hh_account(user_id, payload.phone_or_email, payload.account_name)
            except (AccountLimitError, DuplicateAccountError) as exc:
                raise _http_error(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        result = await HHLoginManager.start_login(user_id, payload.phone_or_email, account_id=account["id"])
        return {"account_id": account["id"], **_captcha_response(result)}

    @app.post("/api/v1/login-flows/otp", status_code=status.HTTP_202_ACCEPTED)
    async def submit_otp(payload: OtpSubmission, session: dict = Depends(require_csrf)) -> dict:
        return _captcha_response(await HHLoginManager.submit_otp(int(session["user_id"]), payload.code))

    @app.post("/api/v1/login-flows/captcha", status_code=status.HTTP_202_ACCEPTED)
    async def submit_captcha(payload: CaptchaSubmission, session: dict = Depends(require_csrf)) -> dict:
        return _captcha_response(await HHLoginManager.submit_captcha(int(session["user_id"]), payload.code))

    @app.post("/api/v1/login-flows/captcha/reload", status_code=status.HTTP_202_ACCEPTED)
    async def reload_captcha(session: dict = Depends(require_csrf)) -> dict:
        return _captcha_response(await HHLoginManager.reload_captcha(int(session["user_id"])))

    @app.post("/api/v1/login-flows/cancel", status_code=status.HTTP_204_NO_CONTENT)
    async def cancel_login(session: dict = Depends(require_csrf)) -> Response:
        await HHLoginManager.cancel(int(session["user_id"]))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/v1/accounts/{account_id}/resumes")
    async def list_resumes(account_id: int, session: dict = Depends(current_user)) -> list[dict]:
        user_id = int(session["user_id"])
        if not await get_account_for_user(user_id, account_id):
            raise _http_error(status.HTTP_404_NOT_FOUND, "Аккаунт не найден")
        return await list_resume_snapshots(user_id, account_id)

    @app.post("/api/v1/accounts/{account_id}/resumes/sync", status_code=status.HTTP_202_ACCEPTED)
    async def sync_resumes(account_id: int, session: dict = Depends(require_csrf)) -> dict:
        user_id = int(session["user_id"])
        if not await get_account_for_user(user_id, account_id):
            raise _http_error(status.HTTP_404_NOT_FOUND, "Аккаунт не найден")
        return await schedule_operation(
            user_id, "resume-sync", lambda: HHResumeManager.fetch_user_resumes(user_id, account_id)
        )

    @app.post("/api/v1/accounts/{account_id}/resumes/{snapshot_id}/activate")
    async def activate_resume(account_id: int, snapshot_id: int, session: dict = Depends(require_csrf)) -> dict:
        resume = await set_active_resume_snapshot(int(session["user_id"]), account_id, snapshot_id)
        if not resume:
            raise _http_error(status.HTTP_404_NOT_FOUND, "Резюме не найдено")
        return resume

    @app.post("/api/v1/accounts/{account_id}/resumes/import", status_code=status.HTTP_202_ACCEPTED)
    async def import_resume(
        account_id: int,
        session: dict = Depends(require_csrf),
        file: UploadFile = File(...),
        structured_json: str = Form(default=""),
    ) -> dict:
        user_id = int(session["user_id"])
        if not await get_account_for_user(user_id, account_id):
            raise _http_error(status.HTTP_404_NOT_FOUND, "Аккаунт не найден")
        if file.content_type not in {"application/pdf", "application/x-pdf"}:
            raise _http_error(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Нужен PDF-файл резюме")
        contents = await file.read(PDF_MAX_BYTES + 1)
        await file.close()
        if len(contents) > PDF_MAX_BYTES:
            raise _http_error(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "PDF слишком большой")
        try:
            structured = json.loads(structured_json) if structured_json else None
        except json.JSONDecodeError as exc:
            raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Структура резюме содержит некорректный JSON") from exc
        directory = Path(tempfile.mkdtemp(prefix="leadscout-upload-"))
        path = directory / "resume.pdf"
        path.write_bytes(contents)

        async def job() -> dict:
            try:
                return await HHResumeManager.upload_pdf_resume_to_hh(
                    user_id, str(path), account_id=account_id, structured_override=structured
                )
            finally:
                path.unlink(missing_ok=True)
                directory.rmdir()

        return await schedule_operation(user_id, "resume-import", job)

    @app.delete("/api/v1/accounts/{account_id}/resumes/{snapshot_id}", status_code=status.HTTP_202_ACCEPTED)
    async def delete_resume(
        account_id: int, snapshot_id: int, payload: ConfirmBody, session: dict = Depends(require_csrf)
    ) -> dict:
        """Delete only after an explicit client confirmation and hh.ru verification."""
        if not payload.confirm:
            raise _http_error(status.HTTP_400_BAD_REQUEST, "Подтвердите удаление резюме")
        user_id = int(session["user_id"])
        snapshot = await get_resume_snapshot_for_user(user_id, snapshot_id)
        if not snapshot or snapshot.get("account_id") != account_id:
            raise _http_error(status.HTTP_404_NOT_FOUND, "Резюме не найдено")

        async def job() -> dict:
            result = await HHResumeManager.delete_resume_on_hh(user_id, snapshot["hh_resume_id"], account_id)
            if result.get("status") != "SUCCESS":
                raise RuntimeError(result.get("message") or "hh.ru не подтвердил удаление резюме")
            return result

        return await schedule_operation(user_id, "resume-delete", job)

    @app.get("/api/v1/questionnaires")
    async def questionnaires(account_id: int | None = None, session: dict = Depends(current_user)) -> list[dict]:
        items = await list_pending_questionnaires(int(session["user_id"]), account_id=account_id)
        return [_decode_questionnaire(item) for item in items]

    @app.patch("/api/v1/questionnaires/{apply_id}")
    async def update_questionnaire(
        apply_id: int, payload: QuestionnaireUpdate, session: dict = Depends(require_csrf)
    ) -> dict:
        user_id = int(session["user_id"])
        item = await get_pending_questionnaire_for_user(user_id, apply_id)
        if not item:
            raise _http_error(status.HTTP_404_NOT_FOUND, "Анкета не найдена")
        if payload.cover_letter is not None and not await update_pending_questionnaire_letter(
            user_id, apply_id, payload.cover_letter
        ):
            raise _http_error(status.HTTP_409_CONFLICT, "Анкета уже обрабатывается")
        if payload.answers is not None:
            ai_payload = json.loads(item.get("ai_payload_json") or "{}")
            ai_payload["answers"] = payload.answers
            if not await update_pending_questionnaire_answers(user_id, apply_id, ai_payload):
                raise _http_error(status.HTTP_409_CONFLICT, "Анкета уже обрабатывается")
        updated = await get_pending_questionnaire_for_user(user_id, apply_id)
        return _decode_questionnaire(updated or item)

    @app.post("/api/v1/questionnaires/{apply_id}/confirm", status_code=status.HTTP_202_ACCEPTED)
    async def confirm_questionnaire(apply_id: int, session: dict = Depends(require_csrf)) -> dict:
        state = await task_coordinator.start_questionnaire(int(session["user_id"]), apply_id)
        if state not in {"STARTED", "ALREADY_RUNNING"}:
            raise _http_error(status.HTTP_409_CONFLICT, "Анкета недоступна для отправки")
        return {"status": state, "apply_id": apply_id}

    @app.post("/api/v1/automation/{account_id}/start", status_code=status.HTTP_202_ACCEPTED)
    async def start_automation(account_id: int, session: dict = Depends(require_csrf)) -> dict:
        user_id = int(session["user_id"])
        account = await get_account_for_user(user_id, account_id)
        if not account:
            raise _http_error(status.HTTP_404_NOT_FOUND, "Аккаунт не найден")
        if account.get("session_status") != "ACTIVE" or not account.get("active_resume_hh_id"):
            raise _http_error(status.HTTP_409_CONFLICT, "Нужны активная сессия и выбранное резюме")
        await update_account_settings_for_user(user_id, account_id, auto_apply_enabled=1)
        return {"status": await task_coordinator.start_account(user_id, account_id), "account_id": account_id}

    @app.post("/api/v1/automation/{account_id}/stop")
    async def stop_automation(account_id: int, session: dict = Depends(require_csrf)) -> dict:
        stopped = await task_coordinator.stop_account(int(session["user_id"]), account_id)
        if not stopped:
            raise _http_error(status.HTTP_404_NOT_FOUND, "Аккаунт не найден")
        return {"status": "STOPPED", "account_id": account_id}

    @app.post("/api/v1/automation/stop-all")
    async def stop_all_automation(session: dict = Depends(require_csrf)) -> dict:
        user_id = int(session["user_id"])
        accounts = await get_user_accounts(user_id)
        stopped = [account["id"] for account in accounts if await task_coordinator.stop_account(user_id, account["id"])]
        return {"status": "STOPPED", "account_ids": stopped}

    @app.get("/api/v1/applications")
    async def applications(account_id: int | None = None, session: dict = Depends(current_user)) -> dict:
        user_id = int(session["user_id"])
        return {
            "history": await list_application_events(user_id, account_id=account_id),
            "stats": await get_application_stats(user_id, account_id=account_id),
        }

    @app.get("/api/v1/audits")
    async def audits(account_id: int | None = None, session: dict = Depends(current_user)) -> list[dict]:
        return await list_resume_audits(int(session["user_id"]), account_id=account_id)

    @app.post("/api/v1/audits", status_code=status.HTTP_202_ACCEPTED)
    async def create_audit(payload: AuditRequest, session: dict = Depends(require_csrf)) -> dict:
        user_id = int(session["user_id"])
        account = await get_account_for_user(user_id, payload.account_id) if payload.account_id else await get_active_account(user_id)
        if payload.account_id and not account:
            raise _http_error(status.HTTP_404_NOT_FOUND, "Аккаунт не найден")
        snapshot = None
        if payload.resume_snapshot_id:
            snapshot = await get_resume_snapshot_for_user(user_id, payload.resume_snapshot_id)
            if not snapshot or (account and snapshot["account_id"] != account["id"]):
                raise _http_error(status.HTTP_404_NOT_FOUND, "Резюме не найдено")
        if not snapshot and account:
            snapshot = await get_active_resume_snapshot(user_id, account["id"])
        resume_text = payload.resume_text or (snapshot or {}).get("extracted_text") or (account or {}).get("resume_text", "")
        if not resume_text.strip():
            raise _http_error(status.HTTP_409_CONFLICT, "Для аудита нужен текст резюме")

        async def job() -> dict:
            result = await analyze_resume_quality(resume_text)
            audit = result.model_dump()
            audit_id = await save_resume_audit(
                user_id,
                account["id"] if account else None,
                result.profession_name,
                result.overall_score,
                audit.get("category_scores", {}),
                result.penalties,
                result.top_recommendations,
                [insight.model_dump() for insight in result.insights],
                result.summary_text,
                source_resume_text=resume_text,
                source_resume_snapshot_id=snapshot.get("id") if snapshot else None,
            )
            return {"audit_id": audit_id, "is_it_profession": result.is_it_profession}

        return await schedule_operation(user_id, "resume-audit", job)

    @app.post("/api/v1/audits/pdf", status_code=status.HTTP_202_ACCEPTED)
    async def create_pdf_audit(
        session: dict = Depends(require_csrf),
        file: UploadFile = File(...),
        account_id: int | None = Form(default=None),
    ) -> dict:
        """Audit a separate PDF without importing it to hh.ru or retaining the file."""
        user_id = int(session["user_id"])
        account = await get_account_for_user(user_id, account_id) if account_id else await get_active_account(user_id)
        if account_id and not account:
            raise _http_error(status.HTTP_404_NOT_FOUND, "Аккаунт не найден")
        if file.content_type not in {"application/pdf", "application/x-pdf"}:
            raise _http_error(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Нужен PDF-файл резюме")
        contents = await file.read(PDF_MAX_BYTES + 1)
        await file.close()
        if len(contents) > PDF_MAX_BYTES:
            raise _http_error(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "PDF слишком большой")
        temporary = Path(tempfile.mkstemp(prefix="leadscout-audit-source-", suffix=".pdf")[1])
        try:
            temporary.write_bytes(contents)
            resume_text = await asyncio.to_thread(extract_text_from_pdf, temporary)
        except PDFValidationError as exc:
            raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        finally:
            temporary.unlink(missing_ok=True)

        async def job() -> dict:
            result = await analyze_resume_quality(resume_text)
            audit = result.model_dump()
            audit_id = await save_resume_audit(
                user_id,
                account["id"] if account else None,
                result.profession_name,
                result.overall_score,
                audit.get("category_scores", {}),
                result.penalties,
                result.top_recommendations,
                [insight.model_dump() for insight in result.insights],
                result.summary_text,
                source_resume_text=resume_text,
            )
            return {"audit_id": audit_id, "is_it_profession": result.is_it_profession}

        return await schedule_operation(user_id, "pdf-resume-audit", job)

    @app.post("/api/v1/audits/{audit_id}/match", status_code=status.HTTP_202_ACCEPTED)
    async def match_audit(audit_id: int, payload: MatchRequest, session: dict = Depends(require_csrf)) -> dict:
        user_id = int(session["user_id"])
        audit = await get_resume_audit_for_user(user_id, audit_id)
        if not audit or not audit.get("source_resume_text"):
            raise _http_error(status.HTTP_404_NOT_FOUND, "Исходный текст аудита не найден")

        async def job() -> dict:
            return (await match_resume_to_vacancy(audit["source_resume_text"], payload.vacancy_text)).model_dump()

        return await schedule_operation(user_id, "vacancy-match", job)

    @app.get("/api/v1/audits/{audit_id}/report")
    async def audit_report(audit_id: int, session: dict = Depends(current_user)) -> FileResponse:
        audit = await get_resume_audit_for_user(int(session["user_id"]), audit_id)
        if not audit:
            raise _http_error(status.HTTP_404_NOT_FOUND, "Аудит не найден")
        temporary = Path(tempfile.mkstemp(prefix="leadscout-audit-", suffix=".pdf")[1])
        generate_resume_audit_pdf(audit, str(temporary))
        return FileResponse(
            temporary,
            media_type="application/pdf",
            filename=f"LeadScout_Audit_{audit_id}.pdf",
            background=BackgroundTask(temporary.unlink, missing_ok=True),
        )

    @app.get("/api/v1/operations/{operation_id}")
    async def operation(operation_id: str, session: dict = Depends(current_user)) -> dict:
        result = await get_operation_for_user(int(session["user_id"]), operation_id)
        if not result:
            raise _http_error(status.HTTP_404_NOT_FOUND, "Операция не найдена")
        return result

    if WEB_DIST_DIR.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIST_DIR, html=True), name="mini-app")
    return app


app = create_app()
