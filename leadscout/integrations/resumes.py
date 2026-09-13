"""Safe PDF parsing and hh.ru resume synchronization."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date
from typing import Any

from patchright.async_api import Locator, Page

from leadscout.core.concurrency import serialize_account
from leadscout.core.config import PDF_MAX_BYTES, PDF_MAX_PAGES, PDF_MAX_TEXT_CHARS
from leadscout.documents.pdf_reader import (
    PDFValidationError,
    missing_resume_fields,
)
from leadscout.documents.pdf_reader import extract_text_from_pdf as _extract_text_from_pdf
from leadscout.models.resumes import StructuredResume
from utils.humanization import DEFAULT_TRANSITION_TIMEOUT_MS, HumanizationError, human_click, human_type

from .account_client import HHAccountClient

logger = logging.getLogger(__name__)
RESUME_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_DETAIL_BLOCKED_MESSAGE = (
    "hh.ru запросил вход или проверку при открытии резюме. "
    "Локальный текст сохранён; войдите заново и повторите синхронизацию."
)


def extract_text_from_pdf(pdf_path: str | os.PathLike[str]) -> str:
    """Compatibility wrapper preserving legacy monkeypatchable PDF limits."""
    return _extract_text_from_pdf(
        pdf_path,
        max_bytes=PDF_MAX_BYTES,
        max_pages=PDF_MAX_PAGES,
        max_text_chars=PDF_MAX_TEXT_CHARS,
    )


async def _visible(locator: Locator) -> bool:
    return await locator.count() > 0 and await locator.is_visible()


async def _wait_visible(locator: Locator, timeout: int = DEFAULT_TRANSITION_TIMEOUT_MS) -> bool:
    """Wait for a next-step control instead of relying on a cosmetic pause."""
    try:
        await locator.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


async def _extract_visible_resume_text(page: Page) -> tuple[str, bool]:
    """Read only visible resume blocks, excluding navigation and executable DOM content."""
    extracted = await page.evaluate(
        r"""() => {
            const pageText = (document.body?.innerText || '').trim();
            const blocked = Boolean(document.querySelector(
                'input[type="password"], [data-qa*="captcha" i], [data-qa*="login" i], [role="dialog"]'
            )) || /(?:captcha|пройдите\s+(?:проверку|капчу)|подтвердите,?\s*что\s+вы\s+не\s+робот|войдите\s+(?:в|или)|вход\s+на\s+hh)/i.test(pageText);
            const pageContent = document.querySelector('[data-qa="resume-page-content"], [data-qa="resume-view"]');
            const blocks = [...document.querySelectorAll('[data-qa="resume-block-container"]')]
                .filter((node) => !node.parentElement?.closest('[data-qa="resume-block-container"]'));
            const sources = pageContent ? [pageContent] : (blocks.length ? blocks : [...document.querySelectorAll('main')]);
            const ignored = 'script, style, noscript, svg, nav, header, footer, form, button, input, select, textarea, [hidden], [aria-hidden="true"], [role="dialog"], [aria-modal="true"], [data-qa*="header" i], [data-qa*="footer" i], [data-qa*="menu" i], [data-qa*="sidebar" i]';
            const text = sources.map((source) => {
                const clone = source.cloneNode(true);
                clone.querySelectorAll(ignored).forEach((node) => node.remove());
                // innerText only honours CSS visibility and rendered line breaks
                // while the clone is attached. Keep it off-screen and inert for
                // the duration of this synchronous read.
                Object.assign(clone.style, {
                    position: 'fixed',
                    left: '-100000px',
                    top: '0',
                    width: Math.max(320, source.getBoundingClientRect().width) + 'px',
                    opacity: '0',
                    pointerEvents: 'none',
                    zIndex: '-1',
                });
                document.body.appendChild(clone);
                const value = (clone.innerText || '').replace(/\r\n?/g, '\n').trim();
                clone.remove();
                return value;
            }).filter(Boolean).join('\n\n');
            return { blocked, text };
        }"""
    )
    return str(extracted.get("text") or "").strip(), bool(extracted.get("blocked"))


class HHResumeManager(HHAccountClient):
    def __init__(self, *, ai, vacancies, **kwargs):
        super().__init__(**kwargs)
        self.ai = ai
        self.vacancies = vacancies

    async def fetch_vacancy_text(self, user_id, vacancy_url, account_id):
        return await self.vacancies.fetch_vacancy_text(user_id, vacancy_url, account_id)

    @serialize_account
    async def fetch_user_resumes(self, user_id: int, account_id: int | None = None) -> dict[str, Any]:
        if account_id is None:
            return {"status": "ERROR", "message": "Выберите аккаунт hh.ru."}
        account, storage_state = await self._account_and_state(user_id, account_id)
        if not account:
            return {"status": "ERROR", "message": "Сессия аккаунта недоступна. Войдите в hh.ru заново."}
        engine = await self.browser_pool.get_engine(account.get("proxy_url") or None)
        context = None
        try:
            context = await engine.create_context(storage_state=storage_state)
            page = await context.new_page()
            await page.goto("https://hh.ru/applicant/resumes", wait_until="domcontentloaded", timeout=30_000)
            if "account/login" in page.url:
                await self.db.update_account_session(user_id, account_id, b"", "EXPIRED")
                return {"status": "ERROR", "message": "Сессия hh.ru истекла. Войдите заново."}
            raw_resumes = await page.evaluate(
                r"""() => {
                    const result = [];
                    const seen = new Set();
                    const ignored = ['создать резюме', 'загрузить готовое', 'резюме и профиль'];
                    for (const element of document.querySelectorAll(
                        '[data-qa="resume-title"], [data-qa="resume-title-link"], a[href*="/resume/"]'
                    )) {
                        const link = element.tagName === 'A' ? element : element.querySelector('a') || element.closest('a');
                        if (!link) continue;
                        const href = link.href || '';
                        const match = href.match(/\/resume\/([a-zA-Z0-9_-]{6,64})/);
                        const title = (element.innerText || link.innerText || '').trim();
                        if (!match || seen.has(match[1]) || ignored.some(item => title.toLowerCase().includes(item))) continue;
                        seen.add(match[1]);
                        result.push({id: match[1], title: title || 'Резюме', href});
                    }
                    return result;
                }"""
            )
            if not raw_resumes:
                empty_state = page.get_by_text(
                    re.compile(
                        r"(?:у вас (?:пока )?нет резюме|вы (?:ещ[её] )?не создали (?:ни одного )?резюме)",
                        re.IGNORECASE,
                    )
                ).first
                if not await _visible(empty_state):
                    return {
                        "status": "ERROR",
                        "message": "hh.ru не подтвердил список резюме. Локальные данные сохранены; повторите синхронизацию.",
                    }
            resumes: list[dict] = []
            for raw in raw_resumes[:20]:
                title = re.split(r"поднять|обновить|просмотр|сохранить", raw["title"], flags=re.IGNORECASE)[0].strip()
                title = re.sub(r"^(постоянная|временная)\s+работа\s*", "", title, flags=re.IGNORECASE)
                resume_text = ""
                detail_page = await context.new_page()
                try:
                    await detail_page.goto(raw["href"], wait_until="domcontentloaded", timeout=20_000)
                    resume_text, blocked = await _extract_visible_resume_text(detail_page)
                    if "/account/login" in detail_page.url or blocked:
                        return {"status": "ERROR", "message": _DETAIL_BLOCKED_MESSAGE}
                    resume_text = resume_text[:PDF_MAX_TEXT_CHARS]
                except Exception:
                    logger.debug("Resume text was not available for %s", raw["id"])
                finally:
                    await detail_page.close()
                resumes.append(
                    {
                        "id": raw["id"],
                        "title": (title or "Резюме")[:200],
                        "href": raw["href"].split("?", 1)[0],
                        "status": "Опубликовано",
                        "extracted_text": resume_text,
                    }
                )
            snapshots = await self.db.sync_resume_snapshots(user_id, account_id, resumes)
            return {"status": "SUCCESS", "resumes": snapshots}
        except Exception as exc:
            logger.error("Resume synchronization failed: %s", type(exc).__name__)
            return {"status": "ERROR", "message": "Не удалось получить список резюме с hh.ru."}
        finally:
            if context:
                await self._persist_context(user_id, account_id, context)
                await context.close()

    @serialize_account
    async def upload_pdf_resume_to_hh(
        self,
        user_id: int,
        pdf_path: str,
        account_id: int | None = None,
        structured_override: StructuredResume | dict | None = None,
    ) -> dict[str, Any]:
        if account_id is None:
            return {"status": "ERROR", "message": "Выберите аккаунт hh.ru."}
        account, storage_state = await self._account_and_state(user_id, account_id)
        if not account:
            return {"status": "ERROR", "message": "Сессия аккаунта недоступна. Войдите заново."}
        resume_text = await asyncio.to_thread(extract_text_from_pdf, pdf_path)
        before_result = await self.fetch_user_resumes(user_id, account_id)
        if before_result.get("status") != "SUCCESS":
            return before_result
        before_ids = {item["hh_resume_id"] for item in await self.db.list_resume_snapshots(user_id, account_id)}
        account, storage_state = await self._account_and_state(user_id, account_id)
        if not account:
            return {"status": "ERROR", "message": "Сессия аккаунта недоступна. Войдите заново."}

        engine = await self.browser_pool.get_engine(account.get("proxy_url") or None)
        context = None
        used_wizard = False
        try:
            context = await engine.create_context(storage_state=storage_state)
            page = await context.new_page()
            file_input = await self._find_upload_input(page)
            if file_input:
                await file_input.set_input_files(pdf_path)
                await page.wait_for_timeout(2500)
                publish = page.locator(
                    '[data-qa="resume-publish"], [data-qa="resume-submit"], '
                    'button:has-text("Опубликовать"), button:has-text("Сохранить"), button[type="submit"]'
                ).first
                if await _visible(publish):
                    await human_click(page, publish)
                    await page.wait_for_timeout(2500)
            else:
                used_wizard = True
                structured = (
                    structured_override
                    if isinstance(structured_override, StructuredResume)
                    else StructuredResume.model_validate(structured_override)
                    if isinstance(structured_override, dict)
                    else await self.ai.extract_full_structured_resume(resume_text)
                )
                missing = missing_resume_fields(structured)
                if missing:
                    return {
                        "status": "NEEDS_FIELDS",
                        "message": "Для мастера hh.ru не хватает обязательных данных.",
                        "missing_fields": missing,
                        "structured": structured.model_dump(),
                    }
                result = await self._fill_step_by_step_resume(page, structured)
                if result.get("status") != "SUBMITTED":
                    return result
        except PDFValidationError:
            raise
        except Exception as exc:
            logger.error("Resume upload failed: %s", type(exc).__name__)
            return {"status": "ERROR", "message": "Не удалось завершить загрузку резюме на hh.ru."}
        finally:
            if context:
                await self._persist_context(user_id, account_id, context)
                await context.close()

        after_result = await self.fetch_user_resumes(user_id, account_id)
        if after_result.get("status") != "SUCCESS":
            return after_result
        snapshots = await self.db.list_resume_snapshots(user_id, account_id)
        new_items = [item for item in snapshots if item["hh_resume_id"] not in before_ids]
        if len(new_items) != 1:
            method = "пошагового мастера" if used_wizard else "импорта PDF"
            return {
                "status": "ERROR",
                "message": f"hh.ru не подтвердил создание нового резюме после {method}.",
            }
        new_resume = new_items[0]
        await self.db.attach_resume_text(user_id, account_id, new_resume["hh_resume_id"], resume_text)
        refreshed = await self.db.get_resume_snapshot_by_hh_id(user_id, account_id, new_resume["hh_resume_id"])
        if refreshed:
            await self.db.set_active_resume_snapshot(user_id, account_id, refreshed["id"])
        return {
            "status": "SUCCESS",
            "message": "Новое резюме создано и подтверждено в списке hh.ru.",
            "snapshot_id": refreshed["id"] if refreshed else None,
        }

    async def _find_upload_input(self, page: Page) -> Locator | None:
        for url in (
            "https://hh.ru/applicant/resumes",
            "https://hh.ru/profile/resume/professional_role",
            "https://hh.ru/resume/create",
        ):
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            if "account/login" in page.url:
                return None
            direct = page.locator('input[type="file"]').first
            if await direct.count() > 0:
                return direct
            triggers = page.get_by_text(re.compile(r"Загрузить (готовое|резюме|файл)", re.IGNORECASE)).first
            if await _visible(triggers):
                await human_click(page, triggers)
                await page.wait_for_timeout(800)
                direct = page.locator('input[type="file"]').first
                if await direct.count() > 0:
                    return direct
        return None

    async def _fill_step_by_step_resume(self, page: Page, resume: StructuredResume) -> dict[str, Any]:
        try:
            await page.goto(
                "https://hh.ru/profile/resume/professional_role", wait_until="domcontentloaded", timeout=30_000
            )
            manual = page.get_by_text("Укажу профессию", exact=True).first
            if await _visible(manual):
                await human_click(page, manual)
            title_input = page.locator(
                '[data-qa="professional-role-search-input"], [data-qa="resume-title-input"], '
                'input[placeholder*="профессию"], input[placeholder*="Должность"]'
            ).first
            if not await _wait_visible(title_input):
                return {"status": "ERROR", "message": "hh.ru не показал поле профессии."}
            await human_type(page, title_input, resume.title)
            option = page.locator('[role="option"], [data-qa="professional-role-item"]').first
            if not await _wait_visible(option):
                return {"status": "ERROR", "message": "hh.ru не показал вариант профессии."}
            await human_click(page, option)

            first_name = page.locator('[data-qa="resume-person-first-name"], input[name*="firstName"]').first
            if not await self._click_continue(page, expected=first_name):
                return {"status": "ERROR", "message": "Не удалось сохранить профессию в мастере hh.ru."}

            if not (await first_name.input_value()).strip():
                await human_type(page, first_name, resume.first_name)
            city = page.locator('[data-qa="resume-person-area"], input[placeholder*="Город"]').first
            if await _wait_visible(city) and not (await city.input_value()).strip():
                await human_type(page, city, resume.city)
                option = page.locator('[role="option"]').first
                if not await _wait_visible(option):
                    return {"status": "ERROR", "message": "hh.ru не показал вариант города."}
                await human_click(page, option)
            try:
                birth = date.fromisoformat(resume.birth_date)
            except ValueError:
                return {"status": "ERROR", "message": "Дата рождения должна быть в формате ГГГГ-ММ-ДД."}
            for locator, value in (
                (page.locator('[data-qa="resume-person-birth-day"], input[name*="birthDay"]').first, str(birth.day)),
                (page.locator('[data-qa="resume-person-birth-year"], input[name*="birthYear"]').first, str(birth.year)),
            ):
                if await _wait_visible(locator) and not (await locator.input_value()).strip():
                    await human_type(page, locator, value)
            month = page.locator('[data-qa="resume-person-birth-month"], select[name*="birthMonth"]').first
            if not await _wait_visible(month):
                return {"status": "ERROR", "message": "hh.ru не показал поле месяца рождения."}
            try:
                await month.select_option(index=birth.month)
            except Exception:
                return {"status": "ERROR", "message": "Не удалось выбрать месяц рождения."}

            experience_input = page.locator('input[placeholder*="Компания"], input[name*="company"]').first
            institution = page.locator('input[placeholder*="заведение"], input[name*="institution"]').first
            skill_input = page.locator(
                'input[placeholder*="навык"], input[placeholder*="Поиск"], input[name*="skill"]'
            ).first

            if resume.experiences:
                if not await self._click_continue(page, expected=experience_input):
                    return {"status": "ERROR", "message": "hh.ru не открыл шаг опыта работы."}
                experience = resume.experiences[0]
                fields = (
                    ('input[placeholder*="Компания"], input[name*="company"]', experience.company),
                    ('input[placeholder*="Должность"], input[name*="position"]', experience.position),
                    ('textarea[placeholder*="занимались"], textarea[name*="description"]', experience.description),
                )
                for selector, value in fields:
                    if not value:
                        continue
                    locator = page.locator(selector).first
                    if not await _wait_visible(locator):
                        return {"status": "ERROR", "message": "hh.ru не показал обязательное поле опыта."}
                    await human_type(page, locator, value[:3000])
                next_step = institution if resume.education else skill_input
                if not await self._click_continue(page, expected=next_step):
                    return {"status": "ERROR", "message": "hh.ru не открыл следующий шаг резюме."}
            elif resume.education:
                if not await self._click_continue(page, expected=institution):
                    return {"status": "ERROR", "message": "hh.ru не открыл шаг образования."}
            elif not await self._click_continue(page, expected=skill_input):
                return {"status": "ERROR", "message": "hh.ru не открыл шаг навыков."}

            if resume.education:
                education = resume.education[0]
                if education.institution:
                    if not await _wait_visible(institution):
                        return {"status": "ERROR", "message": "hh.ru не показал поле образования."}
                    await human_type(page, institution, education.institution)
                if not await self._click_continue(page, expected=skill_input):
                    return {"status": "ERROR", "message": "hh.ru не открыл шаг навыков."}
            if resume.skills:
                for skill in resume.skills[:10]:
                    await human_type(page, skill_input, skill)
                    exact = page.get_by_text(skill, exact=True).first
                    if not await _wait_visible(exact):
                        return {"status": "ERROR", "message": "hh.ru не подтвердил навык из подсказки."}
                    await human_click(page, exact)

            publish = page.locator(
                '[data-qa="resume-publish"], [data-qa="resume-save"], [data-qa="resume-submit"], '
                'button:has-text("Опубликовать"), button:has-text("Сохранить и опубликовать")'
            ).first
            if not await self._click_continue(page, expected=publish):
                return {"status": "ERROR", "message": "Кнопка публикации резюме не найдена."}
            await human_click(page, publish)
            return {"status": "SUBMITTED"}
        except HumanizationError:
            return {"status": "ERROR", "message": "Мастер hh.ru не подтвердил действие в форме."}
        except Exception as exc:
            logger.error("Step-by-step resume wizard failed: %s", type(exc).__name__)
            return {"status": "ERROR", "message": "Мастер hh.ru остановился на обязательном поле."}

    @staticmethod
    async def _click_continue(page: Page, expected: Locator | None = None) -> bool:
        button = page.locator(
            '[data-qa="resume-submit"], [data-qa="professional-role-submit"], '
            'button:has-text("Сохранить и продолжить"), button:has-text("Продолжить"), '
            'button:has-text("Далее"), button[type="submit"]'
        ).first
        if not await _wait_visible(button):
            return False
        try:
            await human_click(page, button)
        except HumanizationError:
            return False
        return expected is None or await _wait_visible(expected)

    @serialize_account
    async def delete_resume_on_hh(self, user_id: int, resume_id: str, account_id: int | None = None) -> dict[str, Any]:
        if account_id is None or not RESUME_ID_PATTERN.fullmatch(resume_id):
            return {"status": "ERROR", "message": "Некорректный идентификатор резюме."}
        account, storage_state = await self._account_and_state(user_id, account_id)
        if not account:
            return {"status": "ERROR", "message": "Сессия аккаунта недоступна."}
        before = await self.fetch_user_resumes(user_id, account_id)
        if before.get("status") != "SUCCESS":
            return before
        if resume_id not in {item["hh_resume_id"] for item in await self.db.list_resume_snapshots(user_id, account_id)}:
            return {"status": "SUCCESS", "message": "Резюме уже отсутствует на hh.ru."}

        engine = await self.browser_pool.get_engine(account.get("proxy_url") or None)
        context = None
        try:
            context = await engine.create_context(storage_state=storage_state)
            page = await context.new_page()
            await page.goto(f"https://hh.ru/resume/{resume_id}", wait_until="domcontentloaded", timeout=30_000)
            delete_button = page.locator(
                '[data-qa="resume-delete"], [data-qa="resume-delete-button"], button:has-text("Удалить резюме")'
            ).first
            if not await _visible(delete_button):
                more = page.locator(
                    '[data-qa="resume-actions"], [data-qa="resume-actions-more"], '
                    'button[aria-label*="Ещё"], button[aria-label*="Еще"]'
                ).first
                if await _visible(more):
                    await human_click(page, more)
                    delete_button = page.get_by_text("Удалить", exact=True).first
            if not await _wait_visible(delete_button):
                return {"status": "ERROR", "message": "Кнопка удаления резюме не найдена."}
            await human_click(page, delete_button)
            confirm = page.locator(
                '[data-qa="resume-delete-confirm"], [data-qa="confirm-delete"], '
                '[role="dialog"] button:has-text("Удалить")'
            ).first
            if not await _wait_visible(confirm):
                return {"status": "ERROR", "message": "hh.ru не показал подтверждение удаления."}
            await human_click(page, confirm)
        except Exception as exc:
            logger.error("Resume deletion failed: %s", type(exc).__name__)
            return {"status": "ERROR", "message": "Не удалось удалить резюме на hh.ru."}
        finally:
            if context:
                await self._persist_context(user_id, account_id, context)
                await context.close()

        after = await self.fetch_user_resumes(user_id, account_id)
        if after.get("status") != "SUCCESS":
            return after
        remaining = {item["hh_resume_id"] for item in await self.db.list_resume_snapshots(user_id, account_id)}
        if resume_id in remaining:
            return {"status": "ERROR", "message": "hh.ru не подтвердил удаление резюме."}
        return {"status": "SUCCESS", "message": "Резюме удалено и его отсутствие подтверждено."}


__all__ = [
    "HHResumeManager",
    "PDFValidationError",
    "RESUME_ID_PATTERN",
    "extract_text_from_pdf",
    "missing_resume_fields",
]
