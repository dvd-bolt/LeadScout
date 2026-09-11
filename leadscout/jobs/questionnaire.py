"""Executor for a claimed questionnaire submission."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

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
        if not account or not account.get("encrypted_storage_state"):
            await self.dependencies.finish_pending_questionnaire(
                user_id, apply_id, "FAILED", "Сессия аккаунта не найдена"
            )
            return {"status": "NO_SESSION"}
        if account.get("applied_today", 0) >= account.get("daily_limit", 50):
            await self.dependencies.finish_pending_questionnaire(
                user_id, apply_id, "FAILED", "Дневной лимит откликов исчерпан"
            )
            return {"status": "DAILY_LIMIT"}
        if not item.get("resume_hh_id"):
            await self.dependencies.finish_pending_questionnaire(
                user_id,
                apply_id,
                "NEEDS_REVIEW",
                "Не зафиксировано резюме для этой анкеты",
            )
            return {"status": "MISSING_RESUME"}

        security = self.dependencies.session_security()
        try:
            state = security.decrypt_storage_state(account["encrypted_storage_state"])
        except SessionDecryptionError:
            await self.dependencies.update_account_session(user_id, account["id"], b"", "EXPIRED")
            await self.dependencies.finish_pending_questionnaire(
                user_id,
                apply_id,
                "FAILED",
                "Сессия требует повторного входа",
            )
            return {"status": "EXPIRED_SESSION"}

        try:
            payload = json.loads(item.get("ai_payload_json") or "{}")
            answers = payload.get("answers", [])
        except (json.JSONDecodeError, AttributeError):
            answers = []

        engine = await self.dependencies.get_browser_engine(account.get("proxy_url") or None)
        context = None
        page = None
        try:
            context = await engine.create_context(storage_state=state)
            page = await context.new_page()
            success, message = await self.dependencies.submit_approved_questionnaire(
                page,
                item["vacancy_url"],
                item.get("cover_letter", ""),
                answers,
                item.get("resume_hh_id"),
            )
            if not success:
                await self.dependencies.finish_pending_questionnaire(user_id, apply_id, "FAILED", message)
                await deliver_safely(
                    self.notifier,
                    user_id,
                    questionnaire_failed(message),
                    logger=logger,
                )
                return {"status": "ERROR"}
            recorded, _ = await self.dependencies.record_successful_application(
                user_id,
                account["id"],
                item["vacancy_url"],
                item.get("cover_letter", ""),
                "APPLIED_WITH_QUESTIONNAIRE",
                item.get("vacancy_title", ""),
            )
            if not recorded and not await self.dependencies.is_account_already_applied(
                user_id, account["id"], item["vacancy_url"]
            ):
                await self.dependencies.finish_pending_questionnaire(
                    user_id,
                    apply_id,
                    "NEEDS_REVIEW",
                    "hh.ru подтвердил отклик, но запись локально не подтверждена",
                )
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
            raise
        except Exception as exc:
            logger.error("Questionnaire %d failed: %s", apply_id, type(exc).__name__)
            await self.dependencies.finish_pending_questionnaire(user_id, apply_id, "FAILED", "Ошибка браузера")
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
