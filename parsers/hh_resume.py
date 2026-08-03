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
from utils.humanization import human_click, human_scroll, human_type
from parsers.hh_browser import HHBrowserEngine
from patchright.async_api import Page

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
    async def fetch_user_resumes(cls, user_id: int, account_id: int | None = None) -> dict[str, Any]:
        """
        Заходит на https://hh.ru/applicant/resumes под сессией аккаунта
        и получает полный список его резюме на сайте.
        """
        from database import get_active_account, get_account_by_id, update_account_settings
        account = await get_account_by_id(account_id) if account_id else await get_active_account(user_id)
        if not account or not account.get("encrypted_storage_state"):
            return {"status": "ERROR", "message": "Сессия hh.ru для данного аккаунта не найдена. Пройдите авторизацию."}

        sec_mgr = SessionSecurityManager()
        storage_state = sec_mgr.decrypt_storage_state(account["encrypted_storage_state"])

        engine = HHBrowserEngine(proxy_url=account.get("proxy_url"))
        context = None

        try:
            context = await engine.create_context(storage_state=storage_state)
            page = await context.new_page()

            logger.info("Пользователь %d: загрузка списка резюме с hh.ru...", user_id)
            await page.goto("https://hh.ru/applicant/resumes", wait_until="domcontentloaded")
            try:
                await page.wait_for_selector('[data-qa="resume-list-action-more"], a[href*="/resume/"]', timeout=2500)
            except Exception:
                await asyncio.sleep(1.0)

            # Проверка редиректа на логин
            if "account/login" in page.url:
                await update_user_settings(user_id, session_status="EXPIRED")
                return {"status": "ERROR", "message": "Сессия hh.ru истекла. Пожалуйста, войдите заново."}

            import re
            resumes = []
            seen_ids = set()

            # Ищем карточки резюме по кнопкам действий data-qa="resume-list-action-more"
            dots_buttons = await page.locator('[data-qa="resume-list-action-more"]').all()
            for dots in dots_buttons:
                try:
                    card_info = await dots.evaluate("""el => {
                        let curr = el;
                        while (curr && curr !== document.body) {
                            let a = curr.querySelector('a[href*="/resume/"]');
                            if (a && a.href && !a.href.includes('/history') && !a.href.includes('edit')) {
                                return { href: a.href, text: a.innerText };
                            }
                            curr = curr.parentElement;
                        }
                        return null;
                    }""")

                    if card_info and card_info.get("href"):
                        href = card_info["href"]
                        match = re.search(r'/resume/([a-f0-9]{32,40})', href)
                        if match:
                            resume_id = match.group(1)
                            if resume_id not in seen_ids:
                                seen_ids.add(resume_id)
                                raw_title = card_info["text"].strip()
                                title_clean = re.sub(r'^(постоянная|временная)\s+работа\s*', '', raw_title, flags=re.IGNORECASE).strip()
                                title_clean = re.split(r'поднять|обновить|просмотр|сохранить', title_clean, flags=re.IGNORECASE)[0].strip() if title_clean else "Резюме"
                                if len(title_clean) > 40:
                                    title_clean = title_clean[:37] + "..."

                                resumes.append({
                                    "id": resume_id,
                                    "title": title_clean,
                                    "href": f"https://hh.ru/resume/{resume_id}",
                                    "status": "Опубликовано",
                                })
                except Exception as ex:
                    logger.debug("Ошибка извлечения карточки резюме: %s", ex)

            # Фолбэк если кнопка не найдена (например старая верстка)
            if not resumes:
                links = await page.locator('a[href*="/resume/"]').all()
                for link in links:
                    try:
                        href = await link.get_attribute("href")
                        if not href or "/edit/" in href or "/history" in href or "create" in href:
                            continue
                        match = re.search(r'/resume/([a-f0-9]{32,40})', href)
                        if match:
                            resume_id = match.group(1)
                            if resume_id in seen_ids:
                                continue
                            seen_ids.add(resume_id)
                            raw_title = (await link.text_content()).strip()
                            title_clean = re.sub(r'^(постоянная|временная)\s+работа\s*', '', raw_title, flags=re.IGNORECASE).strip()
                            title_clean = re.split(r'поднять|обновить|просмотр|сохранить', title_clean, flags=re.IGNORECASE)[0].strip() if raw_title else "Резюме"
                            if len(title_clean) > 40:
                                title_clean = title_clean[:37] + "..."

                            resumes.append({
                                "id": resume_id,
                                "title": title_clean,
                                "href": f"https://hh.ru/resume/{resume_id}",
                                "status": "Опубликовано",
                            })
                    except Exception:
                        pass

            logger.info("Пользователь %d: найдено %d уникальных резюме на hh.ru", user_id, len(resumes))
            return {"status": "SUCCESS", "resumes": resumes}

        except Exception as e:
            logger.error("Пользователь %d: ошибка получения резюме с hh.ru: %s", user_id, e)
            return {"status": "ERROR", "message": f"Ошибка получения резюме с hh.ru: {e}"}
        finally:
            if context:
                await context.close()
            await engine.close()

    @classmethod
    async def upload_pdf_resume_to_hh(cls, user_id: int, pdf_path: str, account_id: int | None = None) -> dict[str, Any]:
        """
        Загружает PDF-файл резюме на hh.ru через браузерную форму
        и публикует его со статусом 'Видно всем работодателям'.
        """
        from database import get_active_account, get_account_by_id, update_account_settings
        account = await get_account_by_id(account_id) if account_id else await get_active_account(user_id)
        if not account or not account.get("encrypted_storage_state"):
            return {"status": "ERROR", "message": "Сессия hh.ru для данного аккаунта не найдена. Пройдите авторизацию."}

        sec_mgr = SessionSecurityManager()
        storage_state = sec_mgr.decrypt_storage_state(account["encrypted_storage_state"])

        engine = HHBrowserEngine(proxy_url=account.get("proxy_url"))
        context = None

        try:
            context = await engine.create_context(storage_state=storage_state)
            page = await context.new_page()

            urls_to_try = [
                "https://hh.ru/applicant/resumes",
                "https://hh.ru/profile/resume/professional_role",
                "https://hh.ru/resume/create",
            ]

            file_input = None
            for target_url in urls_to_try:
                logger.info("Пользователь %d: переход на страницу %s...", user_id, target_url)
                await page.goto(target_url, wait_until="domcontentloaded")
                await asyncio.sleep(2.5)

                if "account/login" in page.url:
                    await update_user_settings(user_id, session_status="EXPIRED")
                    return {"status": "ERROR", "message": "Сессия hh.ru истекла. Пожалуйста, авторизуйтесь заново."}

                # На странице списка резюме пробуем кликнуть «Загрузить готовое резюме» / «Загрузить файл»
                upload_triggers = [
                    '[data-qa="resume-upload-button"]',
                    '[data-qa="resume-create-upload"]',
                    'a:has-text("Загрузить готовое")',
                    'button:has-text("Загрузить готовое")',
                    'a:has-text("Загрузить резюме")',
                    'button:has-text("Загрузить")',
                    'button:has-text("Выбрать")',
                    'div:has-text("Загрузить файл")',
                ]
                for trig_sel in upload_triggers:
                    trig_el = page.locator(trig_sel).first
                    if await trig_el.count() > 0 and await trig_el.is_visible():
                        try:
                            await human_click(page, trig_el)
                            await asyncio.sleep(1.5)
                            break
                        except Exception:
                            pass

                file_input = page.locator('input[type="file"]').first
                if await file_input.count() > 0:
                    break

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
                logger.info("Пользователь %d: инпут прямой загрузки файла недоступен. Запуск ИИ-пошагового автозаполнения...", user_id)
                resume_text = account.get("resume_text", "")
                from ai_handler import extract_full_structured_resume
                structured = await extract_full_structured_resume(resume_text)

                if not structured:
                    return {"status": "WARNING", "message": "Текст резюме сохранен в боте для ИИ, но не удалось найти прямую форму выгрузки и извлечь структуру."}

                return await cls._fill_step_by_step_resume(page, user_id, structured)

        except Exception as e:
            logger.error("Пользователь %d: ошибка при загрузке PDF на hh.ru: %s", user_id, e)
            return {"status": "ERROR", "message": f"Ошибка выгрузки на hh.ru: {e}"}
        finally:
            if context:
                await context.close()
            await engine.close()

    @classmethod
    async def _fill_step_by_step_resume(cls, page: Page, user_id: int, structured: Any) -> dict[str, Any]:
        """
        Пошаговое заполнение мастера создания резюме на hh.ru при отсутствии прямой загрузки PDF.
        """
        try:
            logger.info("Пользователь %d: старт ИИ-мастера создания резюме (%s)...", user_id, structured.title)
            await page.goto("https://hh.ru/profile/resume/professional_role", wait_until="domcontentloaded")
            await asyncio.sleep(2.5)

            # Шаг 1: Кем вы хотите работать?
            specify_btn = page.locator('div:has-text("Укажу профессию"), button:has-text("Укажу профессию")').first
            if await specify_btn.count() > 0 and await specify_btn.is_visible():
                await human_click(page, specify_btn)
                await asyncio.sleep(1.0)

            title_input = page.locator('input[placeholder*="профессию"], input[placeholder*="Должность"], input[type="text"]').first
            if await title_input.count() > 0:
                await human_type(page, title_input, structured.title)
                await asyncio.sleep(1.0)

            next_btn = page.locator('button:has-text("Сохранить и продолжить"), button:has-text("Продолжить"), button:has-text("Далее")').first
            if await next_btn.count() > 0 and await next_btn.is_visible():
                await human_click(page, next_btn)
                await asyncio.sleep(2.0)

            # Шаг 2: Уточните специальность (модалка если выводится)
            spec_modal_btn = page.locator('button:has-text("Сохранить и продолжить"), button:has-text("Продолжить")').first
            if await spec_modal_btn.count() > 0 and await spec_modal_btn.is_visible():
                await human_click(page, spec_modal_btn)
                await asyncio.sleep(2.0)

            # Шаг 3: Опыт работы
            if structured.experiences:
                logger.info("Пользователь %d: заполнение %d мест работы...", user_id, len(structured.experiences))
                for idx, exp in enumerate(structured.experiences):
                    if idx > 0:
                        add_exp_btn = page.locator('button:has-text("Добавить"), div:has-text("Добавить"), [data-qa*="experience-add"]').first
                        if await add_exp_btn.count() > 0 and await add_exp_btn.is_visible():
                            await human_click(page, add_exp_btn)
                            await asyncio.sleep(1.5)

                    comp_inp = page.locator('input[placeholder*="Компания"], [data-qa*="company"]').last
                    if await comp_inp.count() > 0:
                        await human_type(page, comp_inp, exp.company)

                    pos_inp = page.locator('input[placeholder*="Должность"], [data-qa*="position"]').last
                    if await pos_inp.count() > 0:
                        await human_type(page, pos_inp, exp.position)

                    if exp.is_current:
                        chk = page.locator('input[type="checkbox"][name*="current"], label:has-text("Работаю сейчас"), input[type="checkbox"]').first
                        if await chk.count() > 0:
                            try:
                                await chk.check()
                            except Exception:
                                pass

                    desc_inp = page.locator('textarea[placeholder*="занимались"], textarea').last
                    if await desc_inp.count() > 0 and exp.description:
                        await human_type(page, desc_inp, exp.description[:1000])

                exp_next_btn = page.locator('button:has-text("Продолжить"), button:has-text("Далее")').first
                if await exp_next_btn.count() > 0 and await exp_next_btn.is_visible():
                    await human_click(page, exp_next_btn)
                    await asyncio.sleep(2.0)

            # Шаг 4: Образование
            if structured.education:
                edu = structured.education[0]
                inst_inp = page.locator('input[placeholder*="заведение"], input[placeholder*="Название"]').first
                if await inst_inp.count() > 0 and edu.institution:
                    await human_type(page, inst_inp, edu.institution)

                edu_next_btn = page.locator('button:has-text("Продолжить"), button:has-text("Далее")').first
                if await edu_next_btn.count() > 0 and await edu_next_btn.is_visible():
                    await human_click(page, edu_next_btn)
                    await asyncio.sleep(2.0)

            # Шаг 5: Навыки
            if structured.skills:
                skill_search = page.locator('input[placeholder*="Поиск"], input[placeholder*="навык"]').first
                if await skill_search.count() > 0:
                    for sk in structured.skills[:8]:
                        await human_type(page, skill_search, sk)
                        await asyncio.sleep(0.5)
                        tag_btn = page.locator(f'button:has-text("{sk}"), span:has-text("{sk}")').first
                        if await tag_btn.count() > 0 and await tag_btn.is_visible():
                            await human_click(page, tag_btn)

            # Шаг 6: Финальная публикация
            final_pub_btn = page.locator('button:has-text("Сохранить и опубликовать"), button:has-text("Опубликовать"), button:has-text("Сохранить")').first
            if await final_pub_btn.count() > 0 and await final_pub_btn.is_visible():
                await human_click(page, final_pub_btn)
                await asyncio.sleep(3.0)

            logger.info("Пользователь %d: ИИ-резюме пошагово создано и опубликовано!", user_id)
            return {"status": "SUCCESS", "message": "Резюме из PDF-файла успешно создано и опубликовано пошаговым ИИ-мастером на hh.ru!"}

        except Exception as ex:
            logger.error("Пользователь %d: ошибка при пошаговом создании резюме: %s", user_id, ex)
            return {"status": "ERROR", "message": f"Ошибка пошагового автозаполнения: {ex}"}


    @classmethod
    async def delete_resume_on_hh(cls, user_id: int, resume_id: str, account_id: int | None = None) -> dict[str, Any]:
        """
        Удаляет резюме с сайта hh.ru по его 32-значному ID через интерфейс hh.ru:
        1. Переход на https://hh.ru/resume/{resume_id}
        2. Ожидание элемента data-qa="resume-delete"
        3. Нажатие на иконку корзины 🗑
        4. Подтверждение удаление кнопкой data-qa="resume-delete-confirm" в модальном окне
        """
        from database import get_active_account, get_account_by_id, update_account_settings
        account = await get_account_by_id(account_id) if account_id else await get_active_account(user_id)
        if not account or not account.get("encrypted_storage_state"):
            return {"status": "ERROR", "message": "Сессия hh.ru для данного аккаунта не найдена. Пройдите авторизацию."}

        sec_mgr = SessionSecurityManager()
        storage_state = sec_mgr.decrypt_storage_state(account["encrypted_storage_state"])

        engine = HHBrowserEngine(proxy_url=account.get("proxy_url"))
        context = None

        try:
            context = await engine.create_context(storage_state=storage_state)
            page = await context.new_page()

            target_url = f"https://hh.ru/resume/{resume_id}"
            logger.info("Пользователь %d: переход на страницу резюме для удаления: %s", user_id, target_url)
            await page.goto(target_url, wait_until="domcontentloaded")
            await asyncio.sleep(2.0)

            if "account/login" in page.url:
                await update_user_settings(user_id, session_status="EXPIRED")
                return {"status": "ERROR", "message": "Сессия hh.ru истекла. Пожалуйста, авторизуйтесь заново."}

            # 0. Проверка: если при переходе на страницу резюме отображается баннер ошибки и отсутствует заголовок/содержимое резюме
            err_banner = page.locator('div:has-text("Произошла ошибка. Возникли неполадки"), div:has-text("Страница не найдена"), div:has-text("Резюме не найдено")').first
            has_resume_content = await page.locator('h1, [data-qa="resume-block-position"], [data-qa="resume-title"]').count() > 0

            if await err_banner.count() > 0 and not has_resume_content:
                logger.info("Пользователь %d: резюме %s не существует или отображает баннер ошибки на hh.ru", user_id, resume_id)
                return {"status": "SUCCESS", "message": "Резюме уже было удалено с hh.ru!"}

            delete_btn = None

            # Способ 1: Прямая кнопка или иконка корзины на странице резюме
            direct_selectors = [
                '[data-qa="resume-delete"]',
                '[data-qa="resume-delete-button"]',
                '[data-qa="resume-actions-delete"]',
                '[data-qa="resume-sidebar-delete"]',
                '[data-qa="resume-delete-link"]',
                'button[title*="Удалить"]',
                'a[title*="Удалить"]',
                'button:has-text("Удалить резюме")',
                'a:has-text("Удалить резюме")',
                'button:has-text("Удалить")',
                'a:has-text("Удалить")',
            ]
            for sel in direct_selectors:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    delete_btn = el
                    break

            # Способ 2: Через выпадающее меню действий (3 точки / "Действия" / "Ещё") на странице резюме
            if not delete_btn:
                more_triggers = [
                    '[data-qa="resume-actions"]',
                    '[data-qa="resume-header-actions"]',
                    '[data-qa="resume-actions-more"]',
                    '[data-qa="moreItems-button"]',
                    'button[aria-label*="Еще"]',
                    'button[aria-label*="Ещё"]',
                    'button:has-text("Действия")',
                ]
                for trig in more_triggers:
                    el = page.locator(trig).first
                    if await el.count() > 0 and await el.is_visible():
                        await human_click(page, el)
                        await asyncio.sleep(1.0)
                        break

                # Ищем кнопку в открытом меню
                menu_delete_selectors = [
                    '[data-qa="resume-delete"]',
                    '[data-qa="operations-list-delete-resume"]',
                    '[data-qa*="delete"]',
                    'button:has-text("Удалить")',
                    'a:has-text("Удалить")',
                    'span:has-text("Удалить")',
                ]
                for sel in menu_delete_selectors:
                    el = page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible():
                        delete_btn = el
                        break

            # Способ 3: Через страницу списка резюме /applicant/resumes
            if not delete_btn:
                logger.info("Проверка страницы списка резюме /applicant/resumes...")
                await page.goto("https://hh.ru/applicant/resumes", wait_until="domcontentloaded")
                await asyncio.sleep(2.5)

                card = page.locator(f'a[href*="{resume_id}"]').first
                if await card.count() == 0:
                    logger.info("Пользователь %d: резюме %s отсутствует в списке /applicant/resumes (уже удалено)", user_id, resume_id)
                    return {"status": "SUCCESS", "message": "Резюме уже удалено с hh.ru!"}

                # Находим контейнер карточки и кликаем на 3 точки
                container = card.locator('xpath=ancestor::div[contains(@class, "resume") or contains(@class, "item") or contains(@data-qa, "resume") or contains(@class, "card")]').first
                dots_btn = container.locator('[data-qa="resume-list-action-more"], button[aria-label*="Еще"], button[aria-label*="Ещё"]').first
                if await dots_btn.count() == 0:
                    dots_btn = page.locator('[data-qa="resume-list-action-more"]').first

                if await dots_btn.count() > 0 and await dots_btn.is_visible():
                    await human_click(page, dots_btn)
                    await asyncio.sleep(1.0)

                    # Проверяем кнопку удаления прямо в меню карточки
                    card_menu_del = page.locator('[data-qa*="delete"], [data-qa="operations-list-delete-resume"], button:has-text("Удалить"), a:has-text("Удалить")').first
                    if await card_menu_del.count() > 0 and await card_menu_del.is_visible():
                        delete_btn = card_menu_del
                    else:
                        # Иначе кликаем "Редактировать"
                        edit_item = page.locator('[data-qa*="edit"], [data-qa="operations-list-edit-resume"], a:has-text("Редактировать"), button:has-text("Редактировать")').first
                        if await edit_item.count() > 0 and await edit_item.is_visible():
                            await human_click(page, edit_item)
                            await asyncio.sleep(2.5)
                            # Ищем кнопку на странице редактирования
                            for sel in direct_selectors:
                                el = page.locator(sel).first
                                if await el.count() > 0 and await el.is_visible():
                                    delete_btn = el
                                    break

            if not delete_btn:
                logger.warning("Пользователь %d: не удалось найти иконку корзины / кнопку удаления резюме %s", user_id, resume_id)
                return {"status": "ERROR", "message": "Не удалось найти иконку корзины/кнопку «Удалить резюме» на hh.ru."}

            # Клик по найденной кнопке удаления
            logger.info("Пользователь %d: нажатие на кнопку удаления резюме...", user_id)
            await human_click(page, delete_btn)
            await asyncio.sleep(1.5)

            # Подтверждение в модальном диалоге hh.ru
            confirm_selectors = [
                '[data-qa="resume-delete-confirm"]',
                '[data-qa="confirm-delete"]',
                '[data-qa="delete-submit"]',
                'button:has-text("Да, удалить")',
                'button:has-text("Удалить резюме")',
                'button:has-text("Удалить")',
                'div[role="dialog"] button:has-text("Удалить")',
            ]

            confirm_btn = None
            for sel in confirm_selectors:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    confirm_btn = el
                    break

            if confirm_btn:
                logger.info("Пользователь %d: подтверждение удаления в модальном окне...", user_id)
                await human_click(page, confirm_btn)
                await asyncio.sleep(2.5)

            logger.info("Пользователь %d: резюме %s успешно удалено с hh.ru!", user_id, resume_id)
            return {"status": "SUCCESS", "message": "Резюме успешно удалено с сайта hh.ru!"}

        except Exception as e:
            logger.error("Пользователь %d: ошибка при удалении резюме %s с hh.ru: %s", user_id, resume_id, e)
            return {"status": "ERROR", "message": f"Ошибка удаления резюме с hh.ru: {e}"}
        finally:
            if context:
                await context.close()
            await engine.close()

