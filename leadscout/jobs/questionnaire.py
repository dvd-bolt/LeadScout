"""Executor for a claimed questionnaire submission."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from leadscout.diagnostics import ApplicationAttemptTracer, safe_reason_for_status
from leadscout.notifications import Notifier
from leadscout.notifications.formatters import (
    questionnaire_failed,
    questionnaire_submitted,
)
from utils.security import SessionDecryptionError

from .common import deliver_safely

logger = logging.getLogger(__name__)


class QuestionnaireSubmissionJob:
    """Submit a questionnaire whose database state is already ``SUBMITTING``."""

    def __init__(self, dependencies: Any, notifier: Notifier) -> None:
        self.dependencies = dependencies
        self.notifier = notifier

    async def run(self, user_id: int, item: dict) -> dict:
        apply_id = item["id"]
        account = await self.dependencies.get_account_for_user(user_id, item["account_id"])
        tracer = await ApplicationAttemptTracer.start(
            self.dependencies,
            user_id,
            item["account_id"],
            item.get("vacancy_url", ""),
            item.get("vacancy_title", ""),
        )
        if not account or not account.get("encrypted_storage_state"):
            reason = await self._finish_attempt(tracer, "SKIPPED_NO_SESSION")
            await self.dependencies.finish_pending_questionnaire(
                user_id, apply_id, "FAILED", reason
            )
            await self._record_terminal(user_id, item, "SKIPPED_NO_SESSION", tracer=tracer)
            return {"status": "NO_SESSION"}
        if account.get("applied_today", 0) >= account.get("daily_limit", 50):
            reason = await self._finish_attempt(tracer, "SKIPPED_LIMIT")
            await self.dependencies.finish_pending_questionnaire(
                user_id, apply_id, "FAILED", reason
            )
            await self._record_terminal(user_id, item, "SKIPPED_LIMIT", tracer=tracer)
            return {"status": "DAILY_LIMIT"}
        if not item.get("resume_hh_id"):
            reason = await self._finish_attempt(tracer, "ERROR_NO_RESUME")
            await self.dependencies.finish_pending_questionnaire(
                user_id,
                apply_id,
                "NEEDS_REVIEW",
                reason,
            )
            await self._record_terminal(user_id, item, "ERROR_NO_RESUME", tracer=tracer)
            return {"status": "MISSING_RESUME"}

        security = self.dependencies.session_security()
        try:
            state = security.decrypt_storage_state(account["encrypted_storage_state"])
        except SessionDecryptionError:
            await self.dependencies.update_account_session(user_id, account["id"], b"", "EXPIRED")
            reason = await self._finish_attempt(tracer, "ERROR_SESSION_EXPIRED")
            await self.dependencies.finish_pending_questionnaire(
                user_id,
                apply_id,
                "FAILED",
                reason,
            )
            await self._record_terminal(user_id, item, "ERROR_SESSION_EXPIRED", tracer=tracer)
            return {"status": "EXPIRED_SESSION"}

        try:
            payload = json.loads(item.get("ai_payload_json") or "{}")
            answers = payload.get("answers", [])
        except (json.JSONDecodeError, AttributeError):
            answers = []

        engine = await self.dependencies.get_browser_engine(account.get("proxy_url") or None)
        context = None
        page = None
        submission_started = False
        trace_supported = False
        try:
            context = await engine.create_context(storage_state=state)
            page = await context.new_page()
            submit = self.dependencies.submit_approved_questionnaire
            trace_supported = bool(tracer and self._supports_trace("submit_approved_questionnaire"))
            kwargs = {"trace": tracer.stage} if trace_supported else {}
            submission_started = True
            success, message = await submit(
                page, item["vacancy_url"], item.get("cover_letter", ""), answers, item.get("resume_hh_id"), **kwargs
            )
            if not success:
                status = _status_for_message(message)
                reason = await self._finish_attempt(tracer, status)
                final_state = (
                    "NEEDS_REVIEW"
                    if _requires_manual_review(status, tracer.current_stage if tracer else None, trace_supported)
                    else "FAILED"
                )
                await self.dependencies.finish_pending_questionnaire(user_id, apply_id, final_state, reason)
                await self._record_terminal(user_id, item, status, tracer=tracer)
                await deliver_safely(
                    self.notifier,
                    user_id,
                    questionnaire_failed(reason),
                    logger=logger,
                )
                return {"status": "ERROR"}
            if tracer:
                await tracer.stage("CONFIRMING")
            try:
                recorded, _ = await self.dependencies.record_successful_application(
                    user_id,
                    account["id"],
                    item["vacancy_url"],
                    item.get("cover_letter", ""),
                    "APPLIED_WITH_QUESTIONNAIRE",
                    item.get("vacancy_title", ""),
                    details=await self._finish_attempt(tracer, "APPLIED_WITH_QUESTIONNAIRE"),
                    attempt_id=tracer.attempt_id if tracer else "",
                )
            except Exception:
                await self._finish_attempt(tracer, "ERROR_LOCAL_PERSISTENCE")
                await self.dependencies.finish_pending_questionnaire(
                    user_id,
                    apply_id,
                    "NEEDS_REVIEW",
                    safe_reason_for_status("ERROR_LOCAL_PERSISTENCE"),
                )
                await self._record_terminal(user_id, item, "ERROR_LOCAL_PERSISTENCE", tracer=tracer)
                logger.error("Confirmed questionnaire could not be persisted for account %d", account["id"])
                return {"status": "NEEDS_REVIEW"}
            if not recorded and not await self.dependencies.is_account_already_applied(
                user_id, account["id"], item["vacancy_url"]
            ):
                await self.dependencies.finish_pending_questionnaire(
                    user_id,
                    apply_id,
                    "NEEDS_REVIEW",
                    "hh.ru подтвердил отклик, но локальная запись требует проверки.",
                )
                await self._finish_attempt(tracer, "ERROR_LOCAL_PERSISTENCE")
                await self._record_terminal(user_id, item, "ERROR_LOCAL_PERSISTENCE", tracer=tracer)
                return {"status": "NEEDS_REVIEW"}

            # The durable outcome is committed before best-effort notification.
            await self.dependencies.finish_pending_questionnaire(user_id, apply_id, "SUBMITTED")
            await deliver_safely(
                self.notifier,
                user_id,
                questionnaire_submitted(item["vacancy_url"], item.get("vacancy_title", "")),
                logger=logger,
            )
            return {"status": "SUCCESS"}
        except asyncio.CancelledError:
            reason = await self._finish_attempt(tracer, "SKIPPED_STOPPED")
            final_state = (
                "NEEDS_REVIEW"
                if submission_started
                and _requires_manual_review("SKIPPED_STOPPED", tracer.current_stage if tracer else None, trace_supported)
                else "FAILED"
            )
            await self.dependencies.finish_pending_questionnaire(user_id, apply_id, final_state, reason)
            await self._record_terminal(user_id, item, "SKIPPED_STOPPED", tracer=tracer)
            raise
        except Exception as exc:
            logger.error("Questionnaire %d failed: %s", apply_id, type(exc).__name__)
            reason = await self._finish_attempt(tracer, "ERROR_BROWSER")
            final_state = (
                "NEEDS_REVIEW"
                if submission_started
                and _requires_manual_review("ERROR_BROWSER", tracer.current_stage if tracer else None, trace_supported)
                else "FAILED"
            )
            await self.dependencies.finish_pending_questionnaire(user_id, apply_id, final_state, reason)
            await self._record_terminal(user_id, item, "ERROR_BROWSER", tracer=tracer)
            return {"status": "ERROR"}
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    logger.debug("Questionnaire page %d was already closed", apply_id)
            if context:
                try:
                    new_state = await context.storage_state()
                    await self.dependencies.update_account_session(
                        user_id,
                        account["id"],
                        security.encrypt_storage_state(new_state),
                        None,
                    )
                except Exception:
                    logger.warning(
                        "Could not persist questionnaire session for account %d",
                        account["id"],
                    )
                try:
                    await context.close()
                except Exception:
                    logger.warning(
                        "Could not close questionnaire context for account %d",
                        account["id"],
                    )

    async def _finish_attempt(self, tracer: ApplicationAttemptTracer | None, status: str) -> str:
        return await tracer.finish(status) if tracer else safe_reason_for_status(status)

    def _supports_trace(self, operation: str) -> bool:
        checker = getattr(self.dependencies, "supports_application_trace", None)
        return bool(checker and checker(operation))

    async def _record_terminal(
        self, user_id: int, item: dict, status: str, *, tracer: ApplicationAttemptTracer | None
    ) -> None:
        reason = safe_reason_for_status(status)
        await self.dependencies.record_application_event(
            user_id,
            item["account_id"],
            item.get("vacancy_url", ""),
            status,
            item.get("vacancy_title", ""),
            details=reason,
            attempt_id=tracer.attempt_id if tracer else "",
            stage=tracer.current_stage if tracer else "SEARCH",
        )


def _status_for_message(message: str) -> str:
    observed = {
        "Сессия требует повторного входа.": "ERROR_SESSION_EXPIRED",
        "hh.ru запросил CAPTCHA; требуется действие пользователя.": "ERROR_CAPTCHA",
        "Выбранное резюме не найдено в форме отклика.": "ERROR_NO_RESUME",
        "Вакансия недоступна или закрыта.": "ERROR_UNAVAILABLE",
        "Операция превысила время ожидания; отправка не подтверждена.": "ERROR_TIMEOUT",
        "hh.ru не подтвердил отправку отклика.": "ERROR_SUBMIT_UNCONFIRMED",
        "Вакансия перенаправляет на внешний сайт.": "SKIPPED_EXTERNAL",
        "Кнопка отклика не найдена.": "ERROR_NO_BUTTON",
        "Не удалось заполнить все поля анкеты.": "ERROR_FORM",
        "Поле сопроводительного письма не появилось или не заполнилось.": "ERROR_LETTER_FIELD",
        "Кнопка отправки анкеты не найдена.": "ERROR_SUBMIT_BUTTON",
    }
    return observed.get(message, "ERROR_BROWSER")


def _requires_manual_review(status: str, stage: str | None, trace_supported: bool) -> bool:
    """A retry is unsafe when the browser may have reached the submit action."""
    if status in {"ERROR_SUBMIT_UNCONFIRMED", "ERROR_LOCAL_PERSISTENCE"}:
        return True
    if status not in {"ERROR_TIMEOUT", "ERROR_BROWSER", "SKIPPED_STOPPED"}:
        return False
    return not trace_supported or stage in {"SUBMITTING", "CONFIRMING"}
