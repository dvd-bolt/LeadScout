"""Executor for one account's vacancy-search and application cycle."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any
from urllib.parse import quote_plus

from leadscout.core.config import DEFAULT_MAX_DELAY_SEC, DEFAULT_MIN_DELAY_SEC
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
            return {"status": "SKIPPED_NOT_AUTHORIZED"}
        if not account.get("auto_apply_enabled"):
            return {"status": "SKIPPED_STOPPED"}
        if account.get("applied_today", 0) >= account.get("daily_limit", 50):
            return {"status": "SKIPPED_LIMIT"}
        if not account.get("resume_text", "").strip() or not account.get("active_resume_hh_id", "").strip():
            await self.dependencies.update_account_settings_for_user(user_id, account_id, auto_apply_enabled=0)
            await deliver_safely(
                self.notifier,
                user_id,
                automation_stopped_no_resume(),
                logger=logger,
            )
            return {"status": "SKIPPED_NO_RESUME"}

        encrypted_state = account.get("encrypted_storage_state")
        if not encrypted_state:
            return {"status": "SKIPPED_NO_SESSION"}
        security = self.dependencies.session_security()
        try:
            storage_state = security.decrypt_storage_state(encrypted_state)
        except SessionDecryptionError:
            await self.dependencies.update_account_session(user_id, account_id, b"", "EXPIRED")
            await deliver_safely(self.notifier, user_id, expired_session(name), logger=logger)
            return {"status": "EXPIRED_SESSION"}

        engine = await self.dependencies.get_browser_engine(account.get("proxy_url") or None)
        context = None
        processed = 0
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
                    if stop_words and any(word in vacancy_title.lower() for word in stop_words):
                        continue
                    if await self.dependencies.is_account_already_applied(user_id, account_id, vacancy_url):
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
                        status, cover_letter, extra = await self.dependencies.apply_to_hh_vacancy(
                            page=page,
                            resume_context=current["resume_text"],
                            vacancy_url=vacancy_url,
                            target_resume_id=current["active_resume_hh_id"],
                            send_cover_letter=bool(current.get("send_cover_letter", 1)),
                            stop_words=stop_words,
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
                        created, count = await self.dependencies.record_successful_application(
                            user_id,
                            account_id,
                            vacancy_url,
                            cover_letter or "",
                            status,
                            title,
                            company,
                        )
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
                        await self.dependencies.record_application_event(
                            user_id,
                            account_id,
                            vacancy_url,
                            status,
                            title,
                            company,
                        )

            return {"status": "SUCCESS", "processed": processed}
        except asyncio.CancelledError:
            logger.info("Account task %d cancelled", account_id)
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
            links = page.locator(
                '[data-qa="serp-item__title"], [data-qa="vacancy-serp__vacancy-title"], a[data-qa*="vacancy-title"]'
            )
            for index in range(await links.count()):
                link = links.nth(index)
                href = await link.get_attribute("href")
                if not href or "/vacancy/" not in href or "/response" in href:
                    continue
                clean = href.split("?", 1)[0]
                if not clean.startswith("https://"):
                    clean = "https://hh.ru" + clean
                if clean in seen:
                    continue
                seen.add(clean)
                found.append((clean, ((await link.text_content()) or "Вакансия").strip()))
        return found
