"""Safe PDF parsing and hh.ru resume synchronization."""

from __future__ import annotations

import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

from patchright.async_api import Locator, Page
from pypdf import PdfReader

from ai_handler import StructuredResume, extract_full_structured_resume
from config import PDF_MAX_BYTES, PDF_MAX_PAGES, PDF_MAX_TEXT_CHARS
from database import (
    attach_resume_text,
    get_account_for_user,
    get_resume_snapshot_by_hh_id,
    list_resume_snapshots,
    set_active_resume_snapshot,
    sync_resume_snapshots,
    update_account_session,
)
from parsers.hh_applicant import extract_vacancy_details
from parsers.hh_browser import SharedBrowserPool
from utils.humanization import human_click, human_type
from utils.security import SessionDecryptionError, SessionSecurityManager
from utils.validation import normalize_hh_vacancy_url

logger = logging.getLogger(__name__)
RESUME_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


class PDFValidationError(ValueError):
    pass


def extract_text_from_pdf(pdf_path: str | os.PathLike[str]) -> str:
    """Extract bounded text from a text-based PDF without logging its path."""
    path = Path(pdf_path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PDFValidationError("PDF-файл недоступен.") from exc
    if size <= 0 or size > PDF_MAX_BYTES:
        raise PDFValidationError(f"Размер PDF должен быть не больше {PDF_MAX_BYTES // (1024 * 1024)} МБ.")
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise PDFValidationError("Защищенный паролем PDF не поддерживается.")
        if len(reader.pages) > PDF_MAX_PAGES:
            raise PDFValidationError(f"В PDF должно быть не больше {PDF_MAX_PAGES} страниц.")
        parts: list[str] = []
        total = 0
        for page in reader.pages:
            text = (page.extract_text() or "").strip()
            if not text:
                continue
            remaining = PDF_MAX_TEXT_CHARS - total
            if remaining <= 0:
                break
            parts.append(text[:remaining])
            total += len(parts[-1])
        result = "\n".join(parts).strip()
    except PDFValidationError:
        raise
    except Exception as exc:
        raise PDFValidationError("Не удалось прочитать PDF. Проверьте, что файл не поврежден.") from exc
    if len(result) < 50:
        raise PDFValidationError("В PDF не найден читаемый текст. Скан без текстового слоя не поддерживается.")
    logger.info("Extracted %d characters from a PDF", len(result))
    return result


def missing_resume_fields(resume: StructuredResume) -> list[str]:
    required = {
        "first_name": resume.first_name,
        "birth_date": resume.birth_date,
        "city": resume.city,
        "title": resume.title,
    }
    return [name for name, value in required.items() if not str(value or "").strip()]


async def _visible(locator: Locator) -> bool:
    return await locator.count() > 0 and await locator.is_visible()


class HHResumeManager:
    @classmethod
    async def _account_and_state(cls, user_id: int, account_id: int) -> tuple[dict, dict] | tuple[None, None]:
        account = await get_account_for_user(user_id, account_id)
        if not account or not account.get("encrypted_storage_state"):
            return None, None
        try:
            state = SessionSecurityManager().decrypt_storage_state(account["encrypted_storage_state"])
        except SessionDecryptionError:
            await update_account_session(user_id, account_id, b"", "EXPIRED")
            return None, None
        return account, state

    @classmethod
    async def _persist_context(cls, user_id: int, account_id: int, context) -> None:
        try:
            state = await context.storage_state()
            encrypted = SessionSecurityManager().encrypt_storage_state(state)
            await update_account_session(user_id, account_id, encrypted, "ACTIVE")
        except Exception as exc:
            logger.warning("Could not persist resume browser session: %s", type(exc).__name__)

    @classmethod
    async def fetch_user_resumes(cls, user_id: int, account_id: int | None = None) -> dict[str, Any]:
        if account_id is None:
            return {"status": "ERROR", "message": "Выберите аккаунт hh.ru."}
        account, storage_state = await cls._account_and_state(user_id, account_id)
        if not account:
            return {"status": "ERROR", "message": "Сессия аккаунта недоступна. Войдите в hh.ru заново."}
        engine = await SharedBrowserPool.get_engine(account.get("proxy_url") or None)
        context = None
        try:
            context = await engine.create_context(storage_state=storage_state)
            page = await context.new_page()
            await page.goto("https://hh.ru/applicant/resumes", wait_until="domcontentloaded", timeout=30_000)
            if "account/login" in page.url:
                await update_account_session(user_id, account_id, b"", "EXPIRED")
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
            resumes: list[dict] = []
            for raw in raw_resumes[:20]:
                title = re.split(r"поднять|обновить|просмотр|сохранить", raw["title"], flags=re.IGNORECASE)[0].strip()
                title = re.sub(r"^(постоянная|временная)\s+работа\s*", "", title, flags=re.IGNORECASE)
                resume_text = ""
                detail_page = await context.new_page()
                try:
                    await detail_page.goto(raw["href"], wait_until="domcontentloaded", timeout=20_000)
                    body = detail_page.locator(
                        '[data-qa="resume-block-container"], [data-qa="resume-page-content"], main'
                    ).first
                    if await body.count() > 0:
                        resume_text = ((await body.text_content()) or "").strip()[:PDF_MAX_TEXT_CHARS]
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
            snapshots = await sync_resume_snapshots(user_id, account_id, resumes)
            return {"status": "SUCCESS", "resumes": snapshots}
        except Exception as exc:
            logger.error("Resume synchronization failed: %s", type(exc).__name__)
            return {"status": "ERROR", "message": "Не удалось получить список резюме с hh.ru."}
        finally:
            if context:
                await cls._persist_context(user_id, account_id, context)
                await context.close()

    @classmethod
    async def fetch_vacancy_text(cls, user_id: int, vacancy_url: str, account_id: int) -> dict[str, Any]:
        normalized = normalize_hh_vacancy_url(vacancy_url)
        if not normalized:
            return {"status": "ERROR", "message": "Допустима только HTTPS-ссылка на вакансию hh.ru."}
        account, storage_state = await cls._account_and_state(user_id, account_id)
        if not account:
            return {"status": "ERROR", "message": "Сессия hh.ru недоступна."}
        engine = await SharedBrowserPool.get_engine(account.get("proxy_url") or None)
        context = None
        try:
            context = await engine.create_context(storage_state=storage_state)
            page = await context.new_page()
            await page.goto(normalized, wait_until="domcontentloaded", timeout=30_000)
            if "account/login" in page.url:
                await update_account_session(user_id, account_id, b"", "EXPIRED")
                return {"status": "ERROR", "message": "Сессия hh.ru истекла."}
            details = await extract_vacancy_details(page)
            if len(details.get("description", "")) < 15:
                return {"status": "ERROR", "message": "Описание вакансии на странице не найдено."}
            return {"status": "SUCCESS", "url": normalized, **details}
        except Exception as exc:
            logger.warning("Vacancy extraction failed: %s", type(exc).__name__)
            return {"status": "ERROR", "message": "Не удалось загрузить вакансию с hh.ru."}
        finally:
            if context:
                await cls._persist_context(user_id, account_id, context)
                await context.close()

    @classmethod
    async def upload_pdf_resume_to_hh(
        cls,
        user_id: int,
        pdf_path: str,
        account_id: int | None = None,
        structured_override: StructuredResume | dict | None = None,
    ) -> dict[str, Any]:
        if account_id is None:
            return {"status": "ERROR", "message": "Выберите аккаунт hh.ru."}
        account, storage_state = await cls._account_and_state(user_id, account_id)
        if not account:
            return {"status": "ERROR", "message": "Сессия аккаунта недоступна. Войдите заново."}
        resume_text = extract_text_from_pdf(pdf_path)
        before_result = await cls.fetch_user_resumes(user_id, account_id)
        if before_result.get("status") != "SUCCESS":
            return before_result
        before_ids = {item["hh_resume_id"] for item in await list_resume_snapshots(user_id, account_id)}
        account, storage_state = await cls._account_and_state(user_id, account_id)
        if not account:
            return {"status": "ERROR", "message": "Сессия аккаунта недоступна. Войдите заново."}

        engine = await SharedBrowserPool.get_engine(account.get("proxy_url") or None)
        context = None
        used_wizard = False
        try:
            context = await engine.create_context(storage_state=storage_state)
            page = await context.new_page()
            file_input = await cls._find_upload_input(page)
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
                    else await extract_full_structured_resume(resume_text)
                )
                missing = missing_resume_fields(structured)
                if missing:
                    return {
                        "status": "NEEDS_FIELDS",
                        "message": "Для мастера hh.ru не хватает обязательных данных.",
                        "missing_fields": missing,
                        "structured": structured.model_dump(),
                    }
                result = await cls._fill_step_by_step_resume(page, structured)
                if result.get("status") != "SUBMITTED":
                    return result
        except PDFValidationError:
            raise
        except Exception as exc:
            logger.error("Resume upload failed: %s", type(exc).__name__)
            return {"status": "ERROR", "message": "Не удалось завершить загрузку резюме на hh.ru."}
        finally:
            if context:
                await cls._persist_context(user_id, account_id, context)
                await context.close()

        after_result = await cls.fetch_user_resumes(user_id, account_id)
        if after_result.get("status") != "SUCCESS":
            return after_result
        snapshots = await list_resume_snapshots(user_id, account_id)
        new_items = [item for item in snapshots if item["hh_resume_id"] not in before_ids]
        if len(new_items) != 1:
            method = "пошагового мастера" if used_wizard else "импорта PDF"
            return {
                "status": "ERROR",
                "message": f"hh.ru не подтвердил создание нового резюме после {method}.",
            }
        new_resume = new_items[0]
        await attach_resume_text(user_id, account_id, new_resume["hh_resume_id"], resume_text)
        refreshed = await get_resume_snapshot_by_hh_id(user_id, account_id, new_resume["hh_resume_id"])
        if refreshed:
            await set_active_resume_snapshot(user_id, account_id, refreshed["id"])
        return {
            "status": "SUCCESS",
            "message": "Новое резюме создано и подтверждено в списке hh.ru.",
            "snapshot_id": refreshed["id"] if refreshed else None,
        }

    @classmethod
    async def _find_upload_input(cls, page: Page) -> Locator | None:
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

    @classmethod
    async def _fill_step_by_step_resume(cls, page: Page, resume: StructuredResume) -> dict[str, Any]:
        try:
            await page.goto("https://hh.ru/profile/resume/professional_role", wait_until="domcontentloaded", timeout=30_000)
            manual = page.get_by_text("Укажу профессию", exact=True).first
            if await _visible(manual):
                await human_click(page, manual)
            title_input = page.locator(
                '[data-qa="professional-role-search-input"], [data-qa="resume-title-input"], '
                'input[placeholder*="профессию"], input[placeholder*="Должность"]'
            ).first
            if not await _visible(title_input):
                return {"status": "ERROR", "message": "hh.ru не показал поле профессии."}
            await human_type(page, title_input, resume.title)
            option = page.locator('[role="option"], [data-qa="professional-role-item"]').first
            if await _visible(option):
                await human_click(page, option)
            if not await cls._click_continue(page):
                return {"status": "ERROR", "message": "Не удалось сохранить профессию в мастере hh.ru."}

            first_name = page.locator('[data-qa="resume-person-first-name"], input[name*="firstName"]').first
            if await _visible(first_name) and not (await first_name.input_value()).strip():
                await human_type(page, first_name, resume.first_name)
            city = page.locator('[data-qa="resume-person-area"], input[placeholder*="Город"]').first
            if await _visible(city) and not (await city.input_value()).strip():
                await human_type(page, city, resume.city)
                option = page.locator('[role="option"]').first
                if await _visible(option):
                    await human_click(page, option)
            try:
                birth = date.fromisoformat(resume.birth_date)
            except ValueError:
                return {"status": "ERROR", "message": "Дата рождения должна быть в формате ГГГГ-ММ-ДД."}
            for locator, value in (
                (page.locator('[data-qa="resume-person-birth-day"], input[name*="birthDay"]').first, str(birth.day)),
                (page.locator('[data-qa="resume-person-birth-year"], input[name*="birthYear"]').first, str(birth.year)),
            ):
                if await _visible(locator) and not (await locator.input_value()).strip():
                    await human_type(page, locator, value)
            month = page.locator('[data-qa="resume-person-birth-month"], select[name*="birthMonth"]').first
            if await _visible(month):
                try:
                    await month.select_option(index=birth.month)
                except Exception:
                    return {"status": "ERROR", "message": "Не удалось выбрать месяц рождения."}
            await cls._click_continue(page)

            if resume.experiences:
                experience = resume.experiences[0]
                fields = (
                    ('input[placeholder*="Компания"], input[name*="company"]', experience.company),
                    ('input[placeholder*="Должность"], input[name*="position"]', experience.position),
                    ('textarea[placeholder*="занимались"], textarea[name*="description"]', experience.description),
                )
                for selector, value in fields:
                    locator = page.locator(selector).first
                    if value and await _visible(locator):
                        await human_type(page, locator, value[:3000])
                await cls._click_continue(page)

            if resume.education:
                education = resume.education[0]
                institution = page.locator(
                    'input[placeholder*="заведение"], input[name*="institution"]'
                ).first
                if education.institution and await _visible(institution):
                    await human_type(page, institution, education.institution)
                await cls._click_continue(page)

            skill_input = page.locator(
                'input[placeholder*="навык"], input[placeholder*="Поиск"], input[name*="skill"]'
            ).first
            if resume.skills and await _visible(skill_input):
                for skill in resume.skills[:10]:
                    await human_type(page, skill_input, skill)
                    exact = page.get_by_text(skill, exact=True).first
                    if await _visible(exact):
                        await human_click(page, exact)
                    else:
                        await skill_input.press("Enter")
            await cls._click_continue(page)

            publish = page.locator(
                '[data-qa="resume-publish"], [data-qa="resume-save"], [data-qa="resume-submit"], '
                'button:has-text("Опубликовать"), button:has-text("Сохранить и опубликовать")'
            ).first
            if not await _visible(publish):
                return {"status": "ERROR", "message": "Кнопка публикации резюме не найдена."}
            await human_click(page, publish)
            await page.wait_for_timeout(2500)
            return {"status": "SUBMITTED"}
        except Exception as exc:
            logger.error("Step-by-step resume wizard failed: %s", type(exc).__name__)
            return {"status": "ERROR", "message": "Мастер hh.ru остановился на обязательном поле."}

    @staticmethod
    async def _click_continue(page: Page) -> bool:
        button = page.locator(
            '[data-qa="resume-submit"], [data-qa="professional-role-submit"], '
            'button:has-text("Сохранить и продолжить"), button:has-text("Продолжить"), '
            'button:has-text("Далее"), button[type="submit"]'
        ).first
        if not await _visible(button):
            return False
        await human_click(page, button)
        await page.wait_for_timeout(1500)
        return True

    @classmethod
    async def delete_resume_on_hh(
        cls, user_id: int, resume_id: str, account_id: int | None = None
    ) -> dict[str, Any]:
        if account_id is None or not RESUME_ID_PATTERN.fullmatch(resume_id):
            return {"status": "ERROR", "message": "Некорректный идентификатор резюме."}
        account, storage_state = await cls._account_and_state(user_id, account_id)
        if not account:
            return {"status": "ERROR", "message": "Сессия аккаунта недоступна."}
        before = await cls.fetch_user_resumes(user_id, account_id)
        if before.get("status") != "SUCCESS":
            return before
        if resume_id not in {item["hh_resume_id"] for item in await list_resume_snapshots(user_id, account_id)}:
            return {"status": "SUCCESS", "message": "Резюме уже отсутствует на hh.ru."}

        engine = await SharedBrowserPool.get_engine(account.get("proxy_url") or None)
        context = None
        try:
            context = await engine.create_context(storage_state=storage_state)
            page = await context.new_page()
            await page.goto(f"https://hh.ru/resume/{resume_id}", wait_until="domcontentloaded", timeout=30_000)
            delete_button = page.locator(
                '[data-qa="resume-delete"], [data-qa="resume-delete-button"], '
                'button:has-text("Удалить резюме")'
            ).first
            if not await _visible(delete_button):
                more = page.locator(
                    '[data-qa="resume-actions"], [data-qa="resume-actions-more"], '
                    'button[aria-label*="Ещё"], button[aria-label*="Еще"]'
                ).first
                if await _visible(more):
                    await human_click(page, more)
                    delete_button = page.get_by_text("Удалить", exact=True).first
            if not await _visible(delete_button):
                return {"status": "ERROR", "message": "Кнопка удаления резюме не найдена."}
            await human_click(page, delete_button)
            confirm = page.locator(
                '[data-qa="resume-delete-confirm"], [data-qa="confirm-delete"], '
                '[role="dialog"] button:has-text("Удалить")'
            ).first
            if not await _visible(confirm):
                return {"status": "ERROR", "message": "hh.ru не показал подтверждение удаления."}
            await human_click(page, confirm)
            await page.wait_for_timeout(1800)
        except Exception as exc:
            logger.error("Resume deletion failed: %s", type(exc).__name__)
            return {"status": "ERROR", "message": "Не удалось удалить резюме на hh.ru."}
        finally:
            if context:
                await cls._persist_context(user_id, account_id, context)
                await context.close()

        after = await cls.fetch_user_resumes(user_id, account_id)
        if after.get("status") != "SUCCESS":
            return after
        remaining = {item["hh_resume_id"] for item in await list_resume_snapshots(user_id, account_id)}
        if resume_id in remaining:
            return {"status": "ERROR", "message": "hh.ru не подтвердил удаление резюме."}
        return {"status": "SUCCESS", "message": "Резюме удалено и его отсутствие подтверждено."}
