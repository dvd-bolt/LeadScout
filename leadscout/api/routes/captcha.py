"""API endpoints for resolving hh.ru captchas requested during automation."""

from __future__ import annotations

import base64
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from leadscout.integrations.captcha import (
    CaptchaSession,
    extract_captcha_data_uri,
    get_captcha_manager,
)
from leadscout.notifications.formatters import captcha_resolved
from leadscout.runtime import AppContext

from ..dependencies import current_user, get_context, require_csrf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/captcha", tags=["captcha"])


class CaptchaSubmitBody(BaseModel):
    account_id: int
    code: str = Field(min_length=1, max_length=64)


class CaptchaReloadBody(BaseModel):
    account_id: int


async def _ensure_active_captcha_session(
    context: AppContext,
    user_id: int,
    account: dict,
) -> CaptchaSession:
    manager = get_captcha_manager()
    session = manager.get_session(account["id"])
    if session and not session.is_closed and not session.page.is_closed():
        return session

    encrypted_state = account.get("encrypted_storage_state")
    if not encrypted_state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="У аккаунта нет активной сессии")

    security = context.security_factory()
    storage_state = security.decrypt_storage_state(encrypted_state)

    engine = await context.browser_pool.get_engine(account.get("proxy_url") or None)
    browser_context = await engine.create_context(storage_state=storage_state)
    page = await browser_context.new_page()

    captcha_url = account.get("pending_captcha_page_url") or "https://hh.ru/account/captcha"
    try:
        await page.goto(captcha_url, wait_until="domcontentloaded", timeout=20_000)
    except Exception as exc:
        logger.warning("Could not navigate to captcha page %s: %s", captcha_url, exc)
        if "/account/captcha" not in (page.url or ""):
            await page.goto("https://hh.ru/account/captcha", wait_until="domcontentloaded", timeout=20_000)

    uri = await extract_captcha_data_uri(page)
    if not uri:
        try:
            raw = await page.screenshot()
            uri = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        except Exception:
            uri = ""

    await context.db.set_account_pending_captcha(
        user_id, account["id"], uri, page.url or captcha_url
    )
    return await manager.register_session(
        user_id=user_id,
        account_id=account["id"],
        browser_context=browser_context,
        page=page,
        data_uri=uri,
        page_url=page.url or captcha_url,
    )


@router.get("")
async def get_captcha(
    account_id: int | None = None,
    session: dict = Depends(current_user),
    context: AppContext = Depends(get_context),
) -> dict:
    user_id = int(session["user_id"])
    target_id = account_id
    if target_id is None:
        active = await context.db.get_active_account(user_id)
        if not active:
            return {"status": "NONE"}
        target_id = active["id"]

    account = await context.db.get_account_for_user(user_id, target_id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Аккаунт не найден")

    captcha_uri = account.get("pending_captcha_data_uri")
    if not captcha_uri:
        return {"status": "NONE", "account_id": target_id}

    manager = get_captcha_manager()
    active_session = manager.get_session(target_id)
    if not active_session:
        try:
            active_session = await _ensure_active_captcha_session(context, user_id, account)
            captcha_uri = active_session.data_uri
        except Exception as exc:
            logger.warning("Could not pre-open captcha session: %s", exc)

    return {
        "status": "WAITING_FOR_CAPTCHA",
        "account_id": target_id,
        "captcha_data_uri": (active_session.data_uri if active_session else captcha_uri),
        "created_at": account.get("pending_captcha_created_at", ""),
    }


@router.post("/submit")
async def submit_captcha(
    payload: CaptchaSubmitBody,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    user_id = int(session["user_id"])
    account = await context.db.get_account_for_user(user_id, payload.account_id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Аккаунт не найден")

    manager = get_captcha_manager()
    active_session = manager.get_session(payload.account_id)
    if not active_session:
        try:
            active_session = await _ensure_active_captcha_session(context, user_id, account)
            return {
                "status": "INVALID_CAPTCHA",
                "account_id": payload.account_id,
                "captcha_data_uri": active_session.data_uri,
                "message": "Сессия проверки обновлена. Пожалуйста, введите код с новой картинки.",
            }
        except Exception as exc:
            logger.error("Failed to open captcha session: %s", exc)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Ошибка при открытии капчи")

    try:
        success, new_uri = await active_session.enter_code(payload.code)
        if success:
            captcha_page_url = str(account.get("pending_captcha_page_url") or "")
            security = context.security_factory()
            new_state = await active_session.browser_context.storage_state()
            await context.db.update_account_session(
                user_id,
                payload.account_id,
                security.encrypt_storage_state(new_state),
                "ACTIVE",
            )
            await context.db.clear_account_pending_captcha(user_id, payload.account_id)
            await manager.close_session(payload.account_id)

            name = account.get("account_name") or account.get("phone_or_email") or f"ID {payload.account_id}"
            notifier = getattr(context.coordinator, "_notifier", None)
            if not notifier and context.bot:
                from leadscout.notifications import TelegramNotifier

                notifier = TelegramNotifier(context.bot)
            if notifier:
                from leadscout.jobs.common import deliver_safely

                await deliver_safely(
                    notifier,
                    user_id,
                    captcha_resolved(name),
                    logger=logger,
                )

            resume_attempt = await context.db.get_account_pending_resume_attempt(
                user_id, payload.account_id
            )
            if resume_attempt:
                draft_id = int(resume_attempt["draft_id"])
                operation = await context.operations.schedule(
                    user_id,
                    "resume-draft-resume",
                    lambda: context.services.resume_drafts.resume(
                        user_id,
                        payload.account_id,
                        draft_id,
                        int(resume_attempt["draft_revision"]),
                        str(resume_attempt.get("confirmed_fingerprint") or ""),
                    ),
                    resource=str(draft_id),
                    account_id=payload.account_id,
                )
                return {
                    "status": "SUCCESS",
                    "account_id": payload.account_id,
                    "resume_draft_id": draft_id,
                    **operation,
                }

            if "/resume" in captcha_page_url or "/profile/" in captcha_page_url:
                return {
                    "status": "SUCCESS",
                    "account_id": payload.account_id,
                    "required_action": "RETRY_RESUME_CHECK",
                    "message": "Проверка пройдена. Повторите проверку черновика резюме.",
                }

            # Captchas raised by vacancy automation keep their historical
            # behavior; a resume captcha never starts a vacancy search.
            try:
                await context.services.automation.start(user_id, payload.account_id)
            except Exception as exc:
                logger.warning("Could not auto-resume automation after captcha: %s", exc)

            return {"status": "SUCCESS", "account_id": payload.account_id}

        # Failed: hh.ru refreshed image on the same page
        if new_uri:
            await context.db.set_account_pending_captcha(
                user_id, payload.account_id, new_uri, active_session.page_url
            )
        return {
            "status": "INVALID_CAPTCHA",
            "account_id": payload.account_id,
            "captcha_data_uri": new_uri or active_session.data_uri,
            "message": "Неверный код с картинки. Попробуйте ещё раз.",
        }
    except Exception as exc:
        logger.error("Failed to process captcha submission for account %d: %s", payload.account_id, exc)
        await manager.close_session(payload.account_id)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Ошибка при отправке капчи")


@router.post("/reload")
async def reload_captcha(
    payload: CaptchaReloadBody,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    user_id = int(session["user_id"])
    account = await context.db.get_account_for_user(user_id, payload.account_id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Аккаунт не найден")

    manager = get_captcha_manager()
    active_session = manager.get_session(payload.account_id)
    if not active_session:
        active_session = await _ensure_active_captcha_session(context, user_id, account)
    else:
        new_uri = await active_session.reload()
        if new_uri:
            await context.db.set_account_pending_captcha(
                user_id, payload.account_id, new_uri, active_session.page_url
            )

    return {
        "status": "WAITING_FOR_CAPTCHA",
        "account_id": payload.account_id,
        "captcha_data_uri": active_session.data_uri,
    }
