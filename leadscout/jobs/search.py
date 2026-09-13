"""Executor for one account's vacancy-search and application cycle."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any
from urllib.parse import quote_plus

from leadscout.core.config import DEFAULT_MAX_DELAY_SEC, DEFAULT_MIN_DELAY_SEC
from leadscout.diagnostics import ApplicationAttemptTracer, safe_reason_for_status
from leadscout.integrations.vacancies import extract_search_vacancies, vacancy_id_from_url
from leadscout.notifications import Notifier
from leadscout.notifications.formatters import (
    automation_stopped_no_resume,
    expired_session,
    questionnaire_required,
    successful_application,
)
from utils.security import SessionDecryptionError

from .common import deliver_safely

logger = logging.getLogger(__name__)


class AccountSearchJob:
    """Perform external browser work for a previously registered account task."""

    def __init__(
        self,
        dependencies: Any,
        notifier: Notifier,
        *,
        min_delay: float = DEFAULT_MIN_DELAY_SEC,
        max_delay: float = DEFAULT_MAX_DELAY_SEC,
    ) -> None:
        self.dependencies = dependencies
        self.notifier = notifier
        self.min_delay = min_delay
        self.max_delay = max_delay

    async def run(self, user_id: int, account_id: int) -> dict:
        account = await self.dependencies.get_account_for_user(user_id, account_id)
        if not account:
            return {"status": "NOT_FOUND"}
        name = account.get("account_name") or account.get("phone_or_email") or f"ID {account_id}"
        if account.get("session_status") != "ACTIVE":
            await self._record_terminal(user_id, account_id, "", "SKIPPED_NOT_AUTHORIZED")
            return {"status": "SKIPPED_NOT_AUTHORIZED"}
        if not account.get("auto_apply_enabled"):
            await self._record_terminal(user_id, account_id, "", "SKIPPED_STOPPED")
            return {"status": "SKIPPED_STOPPED"}
        if account.get("applied_today", 0) >= account.get("daily_limit", 50):
            await self._record_terminal(user_id, account_id, "", "SKIPPED_LIMIT")
            return {"status": "SKIPPED_LIMIT"}
        if not account.get("resume_text", "").strip() or not account.get("active_resume_hh_id", "").strip():
            await self.dependencies.update_account_settings_for_user(user_id, account_id, auto_apply_enabled=0)
            await deliver_safely(
                self.notifier,
                user_id,
                automation_stopped_no_resume(),
                logger=logger,
            )
            await self._record_terminal(user_id, account_id, "", "SKIPPED_NO_RESUME")
            return {"status": "SKIPPED_NO_RESUME"}

        encrypted_state = account.get("encrypted_storage_state")
        if not encrypted_state:
            await self._record_terminal(user_id, account_id, "", "SKIPPED_NO_SESSION")
            return {"status": "SKIPPED_NO_SESSION"}
        security = self.dependencies.session_security()
        try:
            storage_state = security.decrypt_storage_state(encrypted_state)
        except SessionDecryptionError:
            await self.dependencies.update_account_session(user_id, account_id, b"", "EXPIRED")
            await deliver_safely(self.notifier, user_id, expired_session(name), logger=logger)
            await self._record_terminal(user_id, account_id, "", "ERROR_SESSION_EXPIRED")
            return {"status": "EXPIRED_SESSION"}

        engine = await self.dependencies.get_browser_engine(account.get("proxy_url") or None)
        context = None
        processed = 0
        active_attempt: ApplicationAttemptTracer | None = None
        try:
            context = await engine.create_context(storage_state=storage_state)
            search_page = await context.new_page()
            keywords = await self._resolve_keywords(account)
            stop_words = [word.strip().lower() for word in account.get("stop_words", "").split(",") if word.strip()]
            seen: set[str] = set()

            for keyword in keywords:
                if not await self._account_may_continue(user_id, account_id):
                    break
                vacancies = await self._collect_vacancies(search_page, user_id, account_id, keyword, seen)
                for vacancy_url, vacancy_title in vacancies:
                    current = await self.dependencies.get_account_for_user(user_id, account_id)
                    if not current or not await self._account_may_continue(user_id, account_id):
                        break
                    active_attempt = await ApplicationAttemptTracer.start(
                        self.dependencies, user_id, account_id, vacancy_url, vacancy_title
                    )
                    if stop_words and any(word in vacancy_title.lower() for word in stop_words):
                        await self._record_terminal(
                            user_id, account_id, vacancy_url, "SKIPPED_STOP_WORD", vacancy_title, tracer=active_attempt
                        )
                        active_attempt = None
                        continue
                    if await self.dependencies.is_account_already_applied(user_id, account_id, vacancy_url):
                        await self._record_terminal(
                            user_id,
                            account_id,
                            vacancy_url,
                            "SKIPPED_ALREADY_APPLIED",
                            vacancy_title,
                            tracer=active_attempt,
                        )
                        active_attempt = None
                        continue
                    if await self.dependencies.has_unresolved_application_attempt(user_id, account_id, vacancy_url):
                        await self._record_terminal(
                            user_id,
                            account_id,
                            vacancy_url,
                            "SKIPPED_NEEDS_REVIEW",
                            vacancy_title,
                            tracer=active_attempt,
                        )
                        active_attempt = None
                        continue
                    if await self.dependencies.has_open_questionnaire_for_vacancy(user_id, account_id, vacancy_url):
                        await self._record_terminal(
                            user_id,
                            account_id,
                            vacancy_url,
                            "SKIPPED_NEEDS_REVIEW",
                            vacancy_title,
                            tracer=active_attempt,
                        )
                        active_attempt = None
                        continue
                    page = await context.new_page()
                    snapshot = await self.dependencies.get_active_resume_snapshot(user_id, account_id)
                    source_resume = {
                        "id": (
                            snapshot["id"]
                            if snapshot and snapshot["hh_resume_id"] == current["active_resume_hh_id"]
                            else None
                        ),
                        "hh_resume_id": current["active_resume_hh_id"],
                        "title": current["active_resume_title"],
                        "extracted_text": current["resume_text"],
                    }
                    try:
                        apply = self.dependencies.apply_to_hh_vacancy
                        kwargs = (
                            {"trace": active_attempt.stage}
                            if active_attempt and self._supports_trace("apply_to_hh_vacancy")
                            else {}
                        )
                        status, cover_letter, extra = await apply(
                            page=page,
                            resume_context=current["resume_text"],
                            vacancy_url=vacancy_url,
                            target_resume_id=current["active_resume_hh_id"],
                            send_cover_letter=bool(current.get("send_cover_letter", 1)),
                            stop_words=stop_words,
                            **kwargs,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "Vacancy %s failed for account %d: %s",
                            vacancy_url,
                            account_id,
                            type(exc).__name__,
                        )
                        status, cover_letter, extra = "ERROR_BROWSER", None, None
                    finally:
                        await page.close()

                    details = extra if isinstance(extra, dict) else {}
                    title = details.get("title") or details.get("vacancy", {}).get("title") or vacancy_title
                    company = details.get("company") or details.get("vacancy", {}).get("company") or ""
                    if status.startswith("APPLIED"):
                        if active_attempt:
                            await active_attempt.stage("CONFIRMING")
                        reason = await self._finish_attempt(active_attempt, status)
                        try:
                            created, count = await self.dependencies.record_successful_application(
                                user_id,
                                account_id,
                                vacancy_url,
                                cover_letter or "",
                                status,
                                title,
                                company,
                                details=reason,
                                attempt_id=active_attempt.attempt_id if active_attempt else "",
                            )
                        except Exception:
                            # hh.ru has confirmed the response, but a local write
                            # failure must not fall through to another browser try.
                            await self._record_terminal(
                                user_id,
                                account_id,
                                vacancy_url,
                                "ERROR_LOCAL_PERSISTENCE",
                                title,
                                company,
                                tracer=active_attempt,
                            )
                            logger.error("Confirmed response could not be persisted for account %d", account_id)
                            active_attempt = None
                            continue
                        if created:
                            processed += 1
                            await deliver_safely(
                                self.notifier,
                                user_id,
                                successful_application(
                                    name,
                                    vacancy_url,
                                    title,
                                    company,
                                    count,
                                    current["daily_limit"],
                                    app_url=self.dependencies.app_url,
                                ),
                                logger=logger,
                            )
                            await asyncio.sleep(random.uniform(self.min_delay, self.max_delay))
                        elif await self.dependencies.is_account_already_applied(user_id, account_id, vacancy_url):
                            await self._record_terminal(
                                user_id,
                                account_id,
                                vacancy_url,
                                "ALREADY_APPLIED",
                                title,
                                company,
                                tracer=active_attempt,
                            )
                        else:
                            await self._record_terminal(
                                user_id,
                                account_id,
                                vacancy_url,
                                "ERROR_LOCAL_PERSISTENCE",
                                title,
                                company,
                                tracer=active_attempt,
                            )
                            logger.error("Confirmed response could not be recorded for account %d", account_id)
                    elif status == "QUESTIONNAIRE_REQUIRED" and details:
                        apply_id = await self.dependencies.save_pending_questionnaire_account(
                            user_id,
                            account_id,
                            vacancy_url,
                            title,
                            cover_letter or "",
                            details.get("questions", []),
                            details.get("ai_payload", {}),
                            source_resume,
                        )
                        await self._record_terminal(
                            user_id, account_id, vacancy_url, status, title, company, tracer=active_attempt
                        )
                        await deliver_safely(
                            self.notifier,
                            user_id,
                            questionnaire_required(
                                apply_id,
                                name,
                                vacancy_url,
                                title,
                                details,
                                app_url=self.dependencies.app_url,
                            ),
                            logger=logger,
                        )
                    elif status != "ALREADY_APPLIED":
                        if status == "ERROR_SESSION_EXPIRED":
                            await self.dependencies.update_account_session(user_id, account_id, b"", "EXPIRED")
                        await self._record_terminal(
                            user_id, account_id, vacancy_url, status, title, company, tracer=active_attempt
                        )
                    else:
                        await self._record_terminal(
                            user_id, account_id, vacancy_url, status, title, company, tracer=active_attempt
                        )
                    active_attempt = None

            return {"status": "SUCCESS", "processed": processed}
        except asyncio.CancelledError:
            logger.info("Account task %d cancelled", account_id)
            if active_attempt:
                await self._record_terminal(
                    user_id,
                    account_id,
                    active_attempt.vacancy_hh_id,
                    "SKIPPED_STOPPED",
                    tracer=active_attempt,
                )
            raise
        except Exception as exc:
            logger.error("Account task %d failed: %s", account_id, type(exc).__name__)
            return {"status": "ERROR"}
        finally:
            if context:
                try:
                    new_state = await context.storage_state()
                    await self.dependencies.update_account_session(
                        user_id,
                        account_id,
                        security.encrypt_storage_state(new_state),
                        None,
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not persist session for account %d: %s",
                        account_id,
                        type(exc).__name__,
                    )
                try:
                    await context.close()
                except Exception as exc:
                    logger.warning(
                        "Could not close browser context for account %d: %s",
                        account_id,
                        type(exc).__name__,
                    )

    async def _finish_attempt(self, tracer: ApplicationAttemptTracer | None, status: str) -> str:
        return await tracer.finish(status) if tracer else safe_reason_for_status(status)

    def _supports_trace(self, operation: str) -> bool:
        checker = getattr(self.dependencies, "supports_application_trace", None)
        return bool(checker and checker(operation))

    async def _record_terminal(
        self,
        user_id: int,
        account_id: int,
        vacancy_url: str,
        status: str,
        vacancy_title: str = "",
        company: str = "",
        *,
        tracer: ApplicationAttemptTracer | None = None,
    ) -> None:
        """Persist exactly one user-visible terminal event for an attempt."""
        if tracer is None:
            tracer = await ApplicationAttemptTracer.start(
                self.dependencies, user_id, account_id, vacancy_url, vacancy_title
            )
        reason = await self._finish_attempt(tracer, status)
        await self.dependencies.record_application_event(
            user_id,
            account_id,
            vacancy_url,
            status,
            vacancy_title,
            company,
            reason,
            attempt_id=tracer.attempt_id if tracer else "",
            stage=tracer.current_stage if tracer else "SEARCH",
        )

    async def _resolve_keywords(self, account: dict) -> list[str]:
        configured = [item.strip() for item in account.get("keywords", "").split(",") if item.strip()]
        if configured:
            return configured[:10]
        generated = await self.dependencies.extract_search_keywords_from_resume(
            account.get("resume_text", ""), account.get("active_resume_title", "")
        )
        if generated:
            await self.dependencies.update_account_settings_for_user(
                account["user_id"], account["id"], keywords=", ".join(generated)
            )
            return generated
        return [account["active_resume_title"]]

    async def _account_may_continue(self, user_id: int, account_id: int) -> bool:
        account = await self.dependencies.get_account_for_user(user_id, account_id)
        return bool(
            account
            and account.get("auto_apply_enabled")
            and account.get("session_status") == "ACTIVE"
            and account.get("applied_today", 0) < account.get("daily_limit", 50)
        )

    async def _collect_vacancies(
        self,
        page: Any,
        user_id: int,
        account_id: int,
        keyword: str,
        seen: set[str],
    ) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for page_number in range(3):
            account = await self.dependencies.get_account_for_user(user_id, account_id)
            if not account or not await self._account_may_continue(user_id, account_id):
                break
            url = (
                "https://hh.ru/search/vacancy?text="
                f"{quote_plus(keyword)}&order_by=publication_time&search_period=3"
                f"&page={page_number}"
            )
            if account.get("min_salary"):
                url += f"&salary={account['min_salary']}&currency_code=RUR"
            if account.get("only_remote"):
                url += "&schedule=remote"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            except Exception:
                logger.warning("Search navigation failed for account %d", account_id)
                continue
            if "account/login" in page.url:
                await self.dependencies.update_account_session(user_id, account_id, b"", "EXPIRED")
                break
            read_status, cards = await extract_search_vacancies(page)
            if read_status == "ERROR_SESSION_EXPIRED":
                await self.dependencies.update_account_session(user_id, account_id, b"", "EXPIRED")
                break
            if read_status != "SUCCESS":
                # There is no vacancy attempt to record yet.  Keep the concrete
                # code in the worker log rather than creating a false application
                # event with a search URL or query parameters.
                logger.warning("Search results unavailable for account %d: %s", account_id, read_status)
                if read_status in {"ERROR_CAPTCHA", "ERROR_EXTERNAL"}:
                    break
                continue
            for clean, title in cards:
                vacancy_id = vacancy_id_from_url(clean)
                if not vacancy_id or vacancy_id in seen:
                    continue
                seen.add(vacancy_id)
                found.append((clean, title))
        return found
