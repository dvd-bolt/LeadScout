"""Local in-process task coordinator for hh.ru automation."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections import defaultdict
from urllib.parse import quote_plus

from aiogram import Bot

from ai_handler import extract_search_keywords_from_resume
from config import DEFAULT_MAX_DELAY_SEC, DEFAULT_MIN_DELAY_SEC, MAX_CONCURRENT_BROWSERS
from database import (
    claim_pending_questionnaire,
    finish_pending_questionnaire,
    get_account_for_user,
    get_active_resume_snapshot,
    get_user_accounts,
    is_account_already_applied,
    record_application_event,
    record_successful_application,
    save_pending_questionnaire_account,
    update_account_session,
    update_account_settings_for_user,
)
from keyboards import get_mini_app_keyboard, get_questionnaire_confirmation_keyboard
from parsers.hh_applicant import apply_to_hh_vacancy, submit_approved_questionnaire
from parsers.hh_browser import SharedBrowserPool
from utils.security import SessionDecryptionError, SessionSecurityManager
from utils.validation import escape_html, split_text, strip_telegram_html

logger = logging.getLogger(__name__)


class TaskCoordinator:
    def __init__(self, max_browsers: int = MAX_CONCURRENT_BROWSERS):
        self._account_tasks: dict[int, asyncio.Task] = {}
        self._account_task_users: dict[int, int] = {}
        self._questionnaire_tasks: dict[int, asyncio.Task] = {}
        self._account_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._registry_lock = asyncio.Lock()
        self._launch_lock = asyncio.Lock()
        self._browser_semaphore = asyncio.Semaphore(max_browsers)
        self._active_browsers = 0
        self._bot: Bot | None = None
        self._shutting_down = False

    def configure_bot(self, bot: Bot) -> None:
        self._bot = bot

    def is_running(self, user_id: int, account_id: int) -> bool:
        task = self._account_tasks.get(account_id)
        return bool(
            task
            and not task.done()
            and self._account_task_users.get(account_id) == user_id
        )

    async def start_account(self, user_id: int, account_id: int) -> str:
        if self._shutting_down:
            return "SHUTTING_DOWN"
        account = await get_account_for_user(user_id, account_id)
        if not account:
            return "NOT_FOUND"
        async with self._registry_lock:
            existing = self._account_tasks.get(account_id)
            if existing and not existing.done():
                return "ALREADY_RUNNING"
            task = asyncio.create_task(
                self._run_account_guarded(user_id, account_id),
                name=f"hh-account-{account_id}",
            )
            self._account_tasks[account_id] = task
            self._account_task_users[account_id] = user_id
            task.add_done_callback(lambda completed, key=account_id: self._task_done("account", key, completed))
        return "STARTED"

    async def stop_account(self, user_id: int, account_id: int) -> bool:
        if not await update_account_settings_for_user(
            user_id, account_id, auto_apply_enabled=0
        ):
            return False
        async with self._registry_lock:
            task = (
                self._account_tasks.get(account_id)
                if self._account_task_users.get(account_id) == user_id
                else None
            )
            if task and not task.done():
                task.cancel()
        if task and not task.done():
            await asyncio.gather(task, return_exceptions=True)
        return True

    async def start_questionnaire(self, user_id: int, apply_id: int) -> str:
        if self._shutting_down:
            return "SHUTTING_DOWN"
        async with self._registry_lock:
            existing = self._questionnaire_tasks.get(apply_id)
            if existing and not existing.done():
                return "ALREADY_RUNNING"
            item = await claim_pending_questionnaire(user_id, apply_id)
            if not item:
                return "NOT_AVAILABLE"
            task = asyncio.create_task(
                self._submit_questionnaire_guarded(user_id, item), name=f"hh-questionnaire-{apply_id}"
            )
            self._questionnaire_tasks[apply_id] = task
            task.add_done_callback(
                lambda completed, key=apply_id: self._task_done("questionnaire", key, completed)
            )
        return "STARTED"

    def _task_done(self, kind: str, key: int, task: asyncio.Task) -> None:
        registry = self._account_tasks if kind == "account" else self._questionnaire_tasks
        if registry.get(key) is task:
            registry.pop(key, None)
            if kind == "account":
                self._account_task_users.pop(key, None)
        if not task.cancelled():
            error = task.exception()
            if error:
                logger.error(
                    "Background %s task %d failed",
                    kind,
                    key,
                    exc_info=(type(error), error, error.__traceback__),
                )

    async def _browser_slot(self):
        return _BrowserSlot(self)

    async def _run_account_guarded(self, user_id: int, account_id: int) -> dict:
        async with self._account_locks[account_id]:
            async with await self._browser_slot():
                return await self._run_account(user_id, account_id)

    async def _run_account(self, user_id: int, account_id: int) -> dict:
        account = await get_account_for_user(user_id, account_id)
        if not account:
            return {"status": "NOT_FOUND"}
        name = account.get("account_name") or account.get("phone_or_email") or f"ID {account_id}"
        if account.get("session_status") != "ACTIVE":
            return {"status": "SKIPPED_NOT_AUTHORIZED"}
        if not account.get("auto_apply_enabled"):
            return {"status": "SKIPPED_STOPPED"}
        if account.get("applied_today", 0) >= account.get("daily_limit", 50):
            return {"status": "SKIPPED_LIMIT"}
        if (
            not account.get("resume_text", "").strip()
            or not account.get("active_resume_hh_id", "").strip()
        ):
            await update_account_settings_for_user(
                user_id, account_id, auto_apply_enabled=0
            )
            await self._notify(
                user_id,
                "<b>Автоотклик остановлен.</b> Выберите активное резюме с доступным текстом.",
            )
            return {"status": "SKIPPED_NO_RESUME"}

        encrypted_state = account.get("encrypted_storage_state")
        if not encrypted_state:
            return {"status": "SKIPPED_NO_SESSION"}
        security = SessionSecurityManager()
        try:
            storage_state = security.decrypt_storage_state(encrypted_state)
        except SessionDecryptionError:
            await update_account_session(user_id, account_id, b"", "EXPIRED")
            await self._notify(user_id, f"Сессия аккаунта <code>{escape_html(name)}</code> требует повторного входа.")
            return {"status": "EXPIRED_SESSION"}

        engine = await SharedBrowserPool.get_engine(account.get("proxy_url") or None)
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
                vacancies = await self._collect_vacancies(
                    search_page, user_id, account_id, keyword, seen
                )
                for vacancy_url, vacancy_title in vacancies:
                    current = await get_account_for_user(user_id, account_id)
                    if not current or not await self._account_may_continue(user_id, account_id):
                        break
                    if stop_words and any(word in vacancy_title.lower() for word in stop_words):
                        continue
                    if await is_account_already_applied(user_id, account_id, vacancy_url):
                        continue
                    page = await context.new_page()
                    try:
                        status, cover_letter, extra = await apply_to_hh_vacancy(
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
                        logger.warning("Vacancy %s failed for account %d: %s", vacancy_url, account_id, type(exc).__name__)
                        status, cover_letter, extra = "ERROR_BROWSER", None, None
                    finally:
                        await page.close()

                    details = extra if isinstance(extra, dict) else {}
                    title = details.get("title") or details.get("vacancy", {}).get("title") or vacancy_title
                    company = details.get("company") or details.get("vacancy", {}).get("company") or ""
                    if status.startswith("APPLIED"):
                        created, count = await record_successful_application(
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
                            await self._notify_success(user_id, name, vacancy_url, title, company, count, current["daily_limit"])
                            await asyncio.sleep(random.uniform(DEFAULT_MIN_DELAY_SEC, DEFAULT_MAX_DELAY_SEC))
                    elif status == "QUESTIONNAIRE_REQUIRED" and details:
                        apply_id = await save_pending_questionnaire_account(
                            user_id,
                            account_id,
                            vacancy_url,
                            title,
                            cover_letter or "",
                            details.get("questions", []),
                            details.get("ai_payload", {}),
                            await get_active_resume_snapshot(user_id, account_id),
                        )
                        await self._notify_questionnaire(user_id, apply_id, name, vacancy_url, title, details)
                    elif status != "ALREADY_APPLIED":
                        await record_application_event(
                            user_id, account_id, vacancy_url, status, title, company
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
                    await update_account_session(
                        user_id,
                        account_id,
                        security.encrypt_storage_state(new_state),
                        None,
                    )
                except Exception as exc:
                    logger.warning("Could not persist session for account %d: %s", account_id, type(exc).__name__)
                await context.close()

    async def _resolve_keywords(self, account: dict) -> list[str]:
        configured = [item.strip() for item in account.get("keywords", "").split(",") if item.strip()]
        if configured:
            return configured[:10]
        generated = await extract_search_keywords_from_resume(
            account.get("resume_text", ""), account.get("active_resume_title", "")
        )
        if generated:
            await update_account_settings_for_user(
                account["user_id"], account["id"], keywords=", ".join(generated)
            )
            return generated
        return [account["active_resume_title"]]

    async def _account_may_continue(self, user_id: int, account_id: int) -> bool:
        account = await get_account_for_user(user_id, account_id)
        return bool(
            account
            and account.get("auto_apply_enabled")
            and account.get("session_status") == "ACTIVE"
            and account.get("applied_today", 0) < account.get("daily_limit", 50)
        )

    async def _collect_vacancies(
        self, page, user_id: int, account_id: int, keyword: str, seen: set[str]
    ) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for page_number in range(3):
            account = await get_account_for_user(user_id, account_id)
            if not account or not await self._account_may_continue(user_id, account_id):
                break
            url = (
                "https://hh.ru/search/vacancy?text="
                f"{quote_plus(keyword)}&order_by=publication_time&search_period=3&page={page_number}"
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
                await update_account_session(user_id, account_id, b"", "EXPIRED")
                break
            links = page.locator(
                '[data-qa="serp-item__title"], [data-qa="vacancy-serp__vacancy-title"], '
                'a[data-qa*="vacancy-title"]'
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

    async def _submit_questionnaire_guarded(self, user_id: int, item: dict) -> dict:
        try:
            account_id = item["account_id"]
            async with self._account_locks[account_id]:
                async with await self._browser_slot():
                    return await self._submit_questionnaire(user_id, item)
        except asyncio.CancelledError:
            await finish_pending_questionnaire(user_id, item["id"], "FAILED", "Отправка отменена")
            raise
        except Exception as exc:
            logger.error("Questionnaire %d could not start: %s", item["id"], type(exc).__name__)
            await finish_pending_questionnaire(user_id, item["id"], "FAILED", "Не удалось запустить браузер")
            return {"status": "ERROR"}

    async def _submit_questionnaire(self, user_id: int, item: dict) -> dict:
        apply_id = item["id"]
        account = await get_account_for_user(user_id, item["account_id"])
        if not account or not account.get("encrypted_storage_state"):
            await finish_pending_questionnaire(user_id, apply_id, "FAILED", "Сессия аккаунта не найдена")
            return {"status": "NO_SESSION"}
        if account.get("applied_today", 0) >= account.get("daily_limit", 50):
            await finish_pending_questionnaire(user_id, apply_id, "FAILED", "Дневной лимит откликов исчерпан")
            return {"status": "DAILY_LIMIT"}
        if not item.get("resume_hh_id"):
            await finish_pending_questionnaire(
                user_id, apply_id, "NEEDS_REVIEW", "Не зафиксировано резюме для этой анкеты"
            )
            return {"status": "MISSING_RESUME"}
        security = SessionSecurityManager()
        try:
            state = security.decrypt_storage_state(account["encrypted_storage_state"])
        except SessionDecryptionError:
            await update_account_session(user_id, account["id"], b"", "EXPIRED")
            await finish_pending_questionnaire(user_id, apply_id, "FAILED", "Сессия требует повторного входа")
            return {"status": "EXPIRED_SESSION"}
        try:
            payload = json.loads(item.get("ai_payload_json") or "{}")
            answers = payload.get("answers", [])
        except (json.JSONDecodeError, AttributeError):
            answers = []

        engine = await SharedBrowserPool.get_engine(account.get("proxy_url") or None)
        context = None
        try:
            context = await engine.create_context(storage_state=state)
            page = await context.new_page()
            success, message = await submit_approved_questionnaire(
                page,
                item["vacancy_url"],
                item.get("cover_letter", ""),
                answers,
                item.get("resume_hh_id"),
            )
            await page.close()
            if not success:
                await finish_pending_questionnaire(user_id, apply_id, "FAILED", message)
                await self._notify(user_id, f"Не удалось отправить анкету: {escape_html(message)}")
                return {"status": "ERROR"}
            recorded, _ = await record_successful_application(
                user_id,
                account["id"],
                item["vacancy_url"],
                item.get("cover_letter", ""),
                "APPLIED_WITH_QUESTIONNAIRE",
                item.get("vacancy_title", ""),
            )
            if not recorded and not await is_account_already_applied(user_id, account["id"], item["vacancy_url"]):
                await finish_pending_questionnaire(
                    user_id,
                    apply_id,
                    "NEEDS_REVIEW",
                    "hh.ru подтвердил отклик, но запись локально не подтверждена",
                )
                return {"status": "NEEDS_REVIEW"}
            await finish_pending_questionnaire(user_id, apply_id, "SUBMITTED")
            await self._notify(
                user_id,
                f'<b>Отклик с анкетой подтвержден на hh.ru.</b>\n<a href="{escape_html(item["vacancy_url"])}">'
                f'{escape_html(item.get("vacancy_title") or "Вакансия")}</a>',
            )
            return {"status": "SUCCESS"}
        except asyncio.CancelledError:
            await finish_pending_questionnaire(user_id, apply_id, "FAILED", "Отправка отменена")
            raise
        except Exception as exc:
            logger.error("Questionnaire %d failed: %s", apply_id, type(exc).__name__)
            await finish_pending_questionnaire(user_id, apply_id, "FAILED", "Ошибка браузера")
            return {"status": "ERROR"}
        finally:
            if context:
                try:
                    new_state = await context.storage_state()
                    await update_account_session(
                        user_id,
                        account["id"],
                        security.encrypt_storage_state(new_state),
                        None,
                    )
                except Exception:
                    logger.warning("Could not persist questionnaire session for account %d", account["id"])
                await context.close()

    async def _notify(self, user_id: int, text: str, **kwargs) -> None:
        if not self._bot:
            return
        parse_mode = "HTML"
        if len(text) > 4000:
            text = strip_telegram_html(text)
            parse_mode = None
        chunks = split_text(text, 4000)
        reply_markup = kwargs.pop("reply_markup", None)
        for index, chunk in enumerate(chunks):
            try:
                await self._bot.send_message(
                    user_id,
                    chunk,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup if index == len(chunks) - 1 else None,
                    **kwargs,
                )
            except Exception as exc:
                logger.warning(
                    "Telegram notification for user %d failed: %s",
                    user_id,
                    type(exc).__name__,
                )
                return

    async def _notify_success(
        self,
        user_id: int,
        account_name: str,
        vacancy_url: str,
        title: str,
        company: str,
        count: int,
        limit: int,
    ) -> None:
        await self._notify(
            user_id,
            f"<b>Отклик отправлен.</b>\n"
            f"Аккаунт: <code>{escape_html(account_name)}</code>\n"
            f"Компания: {escape_html(company or 'Не указана')}\n"
            f'<a href="{escape_html(vacancy_url)}">{escape_html(title or "Вакансия")}</a>\n'
            f"Сегодня: <code>{count}/{limit}</code>",
            disable_web_page_preview=True,
            reply_markup=get_mini_app_keyboard("applications"),
        )

    async def _notify_questionnaire(
        self, user_id: int, apply_id: int, account_name: str, url: str, title: str, details: dict
    ) -> None:
        questions = details.get("questions", [])
        payload = details.get("ai_payload", {})
        answer_map = {
            str(answer.get("field_id")): str(answer.get("value") or "")
            for answer in payload.get("answers", [])
            if isinstance(answer, dict)
        }
        lines = []
        for question in questions[:5]:
            if isinstance(question, dict):
                label = question.get("label", "")
                answer = answer_map.get(str(question.get("field_id")), "Не заполнено")
            else:
                label = str(question)
                answer = "Не заполнено"
            lines.append(
                f"• {escape_html(label)}\n  <b>Ответ:</b> {escape_html(answer)}"
            )
        confidence = payload.get("confidence_score")
        confidence_line = (
            f"\nУверенность Gemini: <code>{float(confidence):.0%}</code>\n"
            if isinstance(confidence, (int, float))
            else "\n"
        )
        text = (
            f"<b>Нужно подтвердить ответы работодателю.</b>\n"
            f"Аккаунт: <code>{escape_html(account_name)}</code>\n"
            f'<a href="{escape_html(url)}">{escape_html(title)}</a>\n'
            f"{confidence_line}\n"
            + "\n".join(lines)
        )
        await self._notify(
            user_id,
            text,
            reply_markup=get_questionnaire_confirmation_keyboard(apply_id),
            disable_web_page_preview=True,
        )

    async def shutdown(self) -> None:
        self._shutting_down = True
        async with self._registry_lock:
            tasks = [*self._account_tasks.values(), *self._questionnaire_tasks.values()]
            for task in tasks:
                if not task.done():
                    task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._account_tasks.clear()
        self._account_task_users.clear()
        self._questionnaire_tasks.clear()
        await SharedBrowserPool.shutdown()


class _BrowserSlot:
    def __init__(self, coordinator: TaskCoordinator):
        self.coordinator = coordinator

    async def __aenter__(self):
        await self.coordinator._browser_semaphore.acquire()
        try:
            async with self.coordinator._launch_lock:
                should_stagger = self.coordinator._active_browsers > 0
                self.coordinator._active_browsers += 1
            if should_stagger:
                await asyncio.sleep(random.uniform(5.0, 15.0))
            return self
        except BaseException:
            async with self.coordinator._launch_lock:
                self.coordinator._active_browsers = max(0, self.coordinator._active_browsers - 1)
            self.coordinator._browser_semaphore.release()
            raise

    async def __aexit__(self, exc_type, exc, tb):
        async with self.coordinator._launch_lock:
            self.coordinator._active_browsers = max(0, self.coordinator._active_browsers - 1)
        self.coordinator._browser_semaphore.release()


task_coordinator = TaskCoordinator()


async def process_account_hh_applications(user_id: int, account_id: int) -> dict:
    """Compatibility entrypoint: schedule one local account task."""
    return {"status": await task_coordinator.start_account(user_id, account_id)}


async def process_user_hh_applications(user_id: int) -> dict:
    accounts = await get_user_accounts(user_id)
    launched = 0
    for account in accounts:
        if account.get("session_status") == "ACTIVE" and account.get("auto_apply_enabled"):
            if await task_coordinator.start_account(user_id, account["id"]) == "STARTED":
                launched += 1
    return {"status": "SUCCESS", "launched_accounts": launched}


async def submit_approved_hh_questionnaire(user_id: int, apply_id: int) -> dict:
    return {"status": await task_coordinator.start_questionnaire(user_id, apply_id)}
