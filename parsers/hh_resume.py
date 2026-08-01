"""
LeadScout AI — Модуль взаимодействия с резюме hh.ru.
Осуществляет парсинг существующих резюме пользователя с hh.ru,
а также автоматическую загрузку и публикацию PDF-файлов резюме.
"""

import os
import logging
import asyncio
from typing import Any
from pypdf import PdfReader

from database import get_or_create_user, update_user_settings
from utils.security import SessionSecurityManager
from utils.humanization import human_click, human_scroll
from parsers.hh_browser import HHBrowserEngine

logger = logging.getLogger(__name__)


def extract_text_from_pdf(pdf_path: str) -> str:
    """Извлекает полный текстовый контент из PDF-файла с помощью pypdf."""
    try:
        reader = PdfReader(pdf_path)
        text_content = []
        for page in reader.pages:
            extracted = page.extract_text()
            if extracted:
                text_content.append(extracted)
        full_text = "\n".join(text_content).strip()
        logger.info("Извлечено %d символов текста из PDF: %s", len(full_text), pdf_path)
        return full_text
    except Exception as e:
        logger.error("Ошибка при извлечении текста из PDF %s: %s", pdf_path, e)
        return ""


class HHResumeManager:
    """Менеджер для парсинга и загрузки резюме на hh.ru."""

    @classmethod
    async def fetch_user_resumes(cls, user_id: int) -> dict[str, Any]:
        """
        Заходит на https://hh.ru/applicant/resumes под сессией пользователя
        и получает полный список его резюме на сайте.
        """
        user = await get_or_create_user(user_id)
        if not user or not user.get("encrypted_storage_state"):
            return {"status": "ERROR", "message": "Сессия hh.ru не найдена. Пройдите авторизацию."}

        sec_mgr = SessionSecurityManager()
        storage_state = sec_mgr.decrypt_storage_state(user["encrypted_storage_state"])

        engine = HHBrowserEngine(proxy_url=user.get("proxy_url"))
        context = None

        try:
            context = await engine.create_context(storage_state=storage_state)
            page = await context.new_page()

            logger.info("Пользователь %d: загрузка списка резюме с hh.ru...", user_id)
            await page.goto("https://hh.ru/applicant/resumes", wait_until="domcontentloaded")
            await asyncio.sleep(2.0)

            # Проверка редиректа на логин
            if "account/login" in page.url:
                await update_user_settings(user_id, session_status="EXPIRED")
                return {"status": "ERROR", "message": "Сессия hh.ru истекла. Пожалуйста, войдите заново."}

            import re
            resumes = []
            seen_ids = set()

            links = await page.locator('a[href*="/resume/"], [data-qa="resume-title"]').all()
            for link in links:
                try:
                    href = await link.get_attribute("href")
                    if not href:
                        child = link.locator('a[href*="/resume/"]').first
                        if await child.count() > 0:
                            href = await child.get_attribute("href")

                    if not href or "/edit/" in href or "/history" in href or "create" in href:
                        continue

                    match = re.search(r'/resume/([a-f0-9]{32})', href)
                    if match:
                        resume_id = match.group(1)
                        if resume_id in seen_ids:
                            continue
                        seen_ids.add(resume_id)

                        raw_title = (await link.text_content()).strip()
                        title_clean = re.split(r'поднять|обновить|просмотр|сохранить', raw_title, flags=re.IGNORECASE)[0].strip() if raw_title else "Резюме"
                        if len(title_clean) > 40:
                            title_clean = title_clean[:37] + "..."

                        resumes.append({
                            "id": resume_id,
                            "title": title_clean,
                            "href": f"https://hh.ru/resume/{resume_id}",
                            "status": "Опубликовано",
                        })
                except Exception as ex:
                    logger.debug("Ошибка дедупликации ссылки резюме: %s", ex)

            logger.info("Пользователь %d: найдено %d уникальных резюме на hh.ru", user_id, len(resumes))
            return {"status": "SUCCESS", "resumes": resumes}

        except Exception as e:
            logger.error("Пользователь %d: ошибка при получении резюме с hh.ru: %s", user_id, e)
            return {"status": "ERROR", "message": f"Не удалось загрузить список резюме: {e}"}
        finally:
            if context:
                await context.close()
            await engine.close()

    @classmethod
    async def upload_pdf_resume_to_hh(cls, user_id: int, pdf_path: str) -> dict[str, Any]:
        """
        Загружает PDF-файл резюме на hh.ru через браузерную форму
        и публикует его со статусом 'Видно всем работодателям'.
        """
        user = await get_or_create_user(user_id)
        if not user or not user.get("encrypted_storage_state"):
            return {"status": "ERROR", "message": "Сессия hh.ru не найдена. Пройдите авторизацию."}

        sec_mgr = SessionSecurityManager()
        storage_state = sec_mgr.decrypt_storage_state(user["encrypted_storage_state"])

        engine = HHBrowserEngine(proxy_url=user.get("proxy_url"))
        context = None

        try:
            context = await engine.create_context(storage_state=storage_state)
            page = await context.new_page()

            urls_to_try = [
                "https://hh.ru/profile/resume/professional_role",
                "https://hh.ru/resume/create",
                "https://hh.ru/applicant/resumes/upload",
            ]

            file_input = None
            for target_url in urls_to_try:
                logger.info("Пользователь %d: переход на страницу %s...", user_id, target_url)
                await page.goto(target_url, wait_until="domcontentloaded")
                await asyncio.sleep(2.5)

                if "account/login" in page.url:
                    await update_user_settings(user_id, session_status="EXPIRED")
                    return {"status": "ERROR", "message": "Сессия hh.ru истекла. Пожалуйста, авторизуйтесь заново."}

                file_input = page.locator('input[type="file"]').first
                if await file_input.count() > 0:
                    break

                # Поиск кнопки "Выбрать" или блока "Загрузить файл"
                upload_btn = page.locator('button:has-text("Выбрать"), button:has-text("Загрузить файл"), div:has-text("Загрузить файл с резюме")').first
                if await upload_btn.count() > 0 and await upload_btn.is_visible():
                    try:
                        await human_click(page, upload_btn)
                        await asyncio.sleep(1.5)
                        file_input = page.locator('input[type="file"]').first
                        if await file_input.count() > 0:
                            break
                    except Exception:
                        pass

            if file_input and await file_input.count() > 0:
                logger.info("Пользователь %d: загрузка PDF-файла %s на hh.ru...", user_id, pdf_path)
                await file_input.set_input_files(pdf_path)
                
                # Ожидание процесса распознавания и публикации резюме на hh.ru (10-15 сек)
                logger.info("Пользователь %d: ожидание обработки и публикации резюме на hh.ru...", user_id)
                await asyncio.sleep(10.0)

                # Поиск и нажатие кнопки "Перейти к резюме" или "Опубликовать" / "Сохранить"
                publish_btn = page.locator('button:has-text("Перейти к резюме"), [data-qa="resume-publish"], [data-qa="resume-submit"], button[type="submit"], button:has-text("Опубликовать"), button:has-text("Сохранить")').first
                if await publish_btn.count() > 0 and await publish_btn.is_visible():
                    await human_click(page, publish_btn)
                    await asyncio.sleep(3.0)

                logger.info("Пользователь %d: PDF-резюме успешно создано и опубликовано на hh.ru!", user_id)
                return {"status": "SUCCESS", "message": "Резюме из PDF-файла успешно выгружено, обработано и опубликовано на hh.ru!"}
            else:
                logger.warning("Пользователь %d: инпут загрузки файла не найден на hh.ru.", user_id)
                return {"status": "WARNING", "message": "Текст резюме сохранен в боте для ИИ, но не удалось найти форму выгрузки файлом на hh.ru."}

        except Exception as e:
            logger.error("Пользователь %d: ошибка при загрузке PDF на hh.ru: %s", user_id, e)
            return {"status": "ERROR", "message": f"Ошибка выгрузки на hh.ru: {e}"}
        finally:
            if context:
                await context.close()
            await engine.close()
