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
from parsers.hh_browser import HHBrowserEngine, SharedBrowserPool
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

        engine = await SharedBrowserPool.get_engine(proxy_url=account.get("proxy_url"))
        context = None

        try:
            context = await engine.create_context(storage_state=storage_state)
            page = await context.new_page()

            logger.info("Пользователь %d: загрузка списка резюме с hh.ru...", user_id)
            try:
                await page.goto("https://hh.ru/applicant/resumes", wait_until="commit", timeout=15000)
            except Exception as e_g:
                logger.warning("Мягкое предупреждение при переходе на список резюме: %s", e_g)
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

            # Универсальное извлечение ссылок и названий НАСТОЯЩИХ резюме со страницы соискателя
            eval_resumes = await page.evaluate("""() => {
                const list = [];
                const seen = new Set();
                
                // Навигационные ссылки шапки и футера hh.ru для исключения
                const ignoreTexts = ['резюме и профиль', 'экспертная рекомендация', 'отклики', 'сервисы', 'карьера', 'помощь', 'поиск', 'создать резюме', 'загрузить готовое'];
                
                // Поиск карточек резюме в основном контенте страницы
                const candidates = document.querySelectorAll('[data-qa="resume-title"], [data-qa="resume-title-link"], [data-qa="resume-header"], a[href*="/resume"]');
                for (const el of candidates) {
                    const a = el.tagName === 'A' ? el : el.querySelector('a') || el.closest('a');
                    if (!a) continue;
                    const href = a.href || '';
                    const text = (el.innerText || a.innerText || a.textContent || '').trim();
                    const textLower = text.toLowerCase();
                    
                    // Игнорируем служебные ссылки шапки
                    if (ignoreTexts.some(bad => textLower.includes(bad))) continue;
                    if (!href || href.includes('/history') || href.includes('/edit') || href.includes('create') || href.includes('/new') || href.includes('expert')) continue;
                    
                    // Резюме соискателя содержит id-хэш из 8-64 символов (буквы, цифры, дефисы)
                    const match = href.match(/\/resume\/([a-zA-Z0-9_-]{8,64})/) || href.match(/[\?&](?:id|hash|resume)=([a-zA-Z0-9_-]{8,64})/);
                    
                    let resId = match ? match[1] : '';
                    if (!resId && href.includes('/resume/')) {
                        const parts = href.split('/resume/')[1].split('?')[0].split('/')[0];
                        if (parts && parts.length >= 6) {
                            resId = parts;
                        }
                    }
                    
                    if (resId && !seen.has(resId)) {
                        seen.add(resId);
                        list.push({ id: resId, title: text, href: href });
                    }
                }
                return list;
            }""")

            if eval_resumes:
                for item in eval_resumes:
                    raw_title = item["title"]
                    title_clean = re.sub(r'^(постоянная|временная)\s+работа\s*', '', raw_title, flags=re.IGNORECASE).strip()
                    title_clean = re.split(r'поднять|обновить|просмотр|сохранить', title_clean, flags=re.IGNORECASE)[0].strip() if raw_title else "Резюме"
                    if len(title_clean) > 40:
                        title_clean = title_clean[:37] + "..."

                    resumes.append({
                        "id": item["id"],
                        "title": title_clean,
                        "href": item["href"],
                        "status": "Опубликовано",
                    })

            logger.info("Пользователь %d: найдено %d уникальных резюме на hh.ru", user_id, len(resumes))
            return {"status": "SUCCESS", "resumes": resumes}

        except Exception as e:
            logger.error("Пользователь %d: ошибка получения резюме с hh.ru: %s", user_id, e)
            return {"status": "ERROR", "message": f"Ошибка получения резюме с hh.ru: {e}"}
        finally:
            if context:
                await context.close()

    @classmethod
    async def upload_pdf_resume_to_hh(cls, user_id: int, pdf_path: str, account_id: int | None = None) -> dict[str, Any]:
        """
        Загружает PDF-файл резюме на hh.ru через браузерную форму
        и публикует его со статусом 'Видно всем работодателям'.
        При отсутствии прямой загрузки переходит к пошаговому ИИ-мастеру.
        """
        from database import get_active_account, get_account_by_id, update_account_settings
        account = await get_account_by_id(account_id) if account_id else await get_active_account(user_id)
        if not account or not account.get("encrypted_storage_state"):
            return {"status": "ERROR", "message": "Сессия hh.ru для данного аккаунта не найдена. Пройдите авторизацию."}

        sec_mgr = SessionSecurityManager()
        storage_state = sec_mgr.decrypt_storage_state(account["encrypted_storage_state"])

        engine = await SharedBrowserPool.get_engine(proxy_url=account.get("proxy_url"))
        context = None

        try:
            context = await engine.create_context(storage_state=storage_state)
            page = await context.new_page()

            urls_to_try = [
                "https://hh.ru/applicant/resumes",
                "https://hh.ru/profile/resume/professional_role",
                "https://hh.ru/resume/create",
            ]

            upload_triggers = [
                'input[type="file"]',
                '[data-qa="resume-upload-file-input"]',
                '[data-qa="resume-import-file-input"]',
                '[data-qa="resume-upload-button"]',
                '[data-qa="resume-import"]',
                '[data-qa="resume-create-upload"]',
                '[data-qa="resume-file-upload"]',
                '[data-qa="resume-file-select-label"]',
                'a:has-text("Загрузить готовое")',
                'button:has-text("Загрузить готовое")',
                'button:has-text("Загрузить резюме")',
                'a:has-text("Загрузить резюме")',
                'button:has-text("Загрузить")',
                'button:has-text("Выбрать")',
                'div:has-text("Загрузить файл")',
            ]

            file_input = None
            for target_url in urls_to_try:
                logger.info("Пользователь %d: переход на страницу %s...", user_id, target_url)
                try:
                    await page.goto(target_url, wait_until="commit", timeout=15000)
                except Exception as e_goto:
                    logger.warning("Мягкое предупреждение при переходе на %s: %s", target_url, e_goto)
                await asyncio.sleep(2.0)

                if "account/login" in page.url:
                    await update_user_settings(user_id, session_status="EXPIRED")
                    return {"status": "ERROR", "message": "Сессия hh.ru истекла. Пожалуйста, авторизуйтесь заново."}

                # Прямая проверка input[type="file"]
                direct_in = page.locator('input[type="file"]').first
                if await direct_in.count() > 0:
                    file_input = direct_in
                    break

                # Проверка всех триггерных кнопок загрузки файла
                for trig_sel in upload_triggers:
                    if trig_sel == 'input[type="file"]':
                        continue
                    trig_el = page.locator(trig_sel).first
                    if await trig_el.count() > 0 and await trig_el.is_visible():
                        try:
                            await human_click(page, trig_el)
                            await asyncio.sleep(1.5)
                            in_file = page.locator('input[type="file"]').first
                            if await in_file.count() > 0:
                                file_input = in_file
                                break
                        except Exception as e_trig:
                            logger.debug("Ошибка при клике на триггер загрузки %s: %s", trig_sel, e_trig)

                if not file_input:
                    in_file = page.locator('input[type="file"]').first
                    if await in_file.count() > 0:
                        file_input = in_file

                if file_input:
                    break

            if file_input and await file_input.count() > 0:
                logger.info("Пользователь %d: загрузка PDF-файла %s на hh.ru...", user_id, pdf_path)
                await file_input.set_input_files(pdf_path)
                
                # Ожидание процесса обработки и сохранения файла
                logger.info("Пользователь %d: ожидание обработки и публикации резюме на hh.ru...", user_id)
                try:
                    await page.wait_for_selector('button:has-text("Опубликовать"), button:has-text("Сохранить"), [data-qa="resume-publish"], [data-qa="resume-submit"], button:has-text("Перейти к резюме")', timeout=15000)
                except Exception:
                    await asyncio.sleep(8.0)

                # Нажатие кнопки "Опубликовать" / "Сохранить" / "Перейти к резюме"
                publish_btn = page.locator('button:has-text("Опубликовать"), button:has-text("Сохранить"), [data-qa="resume-publish"], [data-qa="resume-submit"], button:has-text("Перейти к резюме"), button[type="submit"]').first
                if await publish_btn.count() > 0 and await publish_btn.is_visible():
                    await human_click(page, publish_btn)
                    await asyncio.sleep(3.0)

                logger.info("Пользователь %d: PDF-резюме успешно создано и опубликовано на hh.ru!", user_id)
                try:
                    import json
                    hh_res = await cls.fetch_user_resumes(user_id, account_id=account.get("id") if 'account' in locals() else None)
                    if account and account.get("id"):
                        res_list = hh_res.get("resumes", []) if isinstance(hh_res, dict) else []
                        if res_list:
                            await update_account_settings(
                                account["id"],
                                resumes_json=json.dumps(res_list),
                                active_resume_url=res_list[0]["href"],
                                active_resume_title=res_list[0]["title"]
                            )
                except Exception:
                    pass

                return {"status": "SUCCESS", "message": "Резюме из PDF-файла успешно выгружено, обработано и опубликовано на hh.ru!"}
            else:
                logger.info("Пользователь %d: инпут прямой загрузки файла недоступен. Запуск ИИ-пошагового автозаполнения...", user_id)
                resume_text = account.get("resume_text", "")
                if not resume_text and os.path.exists(pdf_path):
                    resume_text = extract_text_from_pdf(pdf_path)

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

    @classmethod
    async def _fill_step_by_step_resume(cls, page: Page, user_id: int, structured: Any) -> dict[str, Any]:
        """
        Пошаговое заполнение мастера создания резюме на hh.ru при отсутствии прямой загрузки PDF.
        Использует human_click, human_type и надежные wait_for_selector на каждом этапе.
        """
        try:
            resume_title = getattr(structured, 'title', 'Резюме') or 'Резюме'
            logger.info("Пользователь %d: старт ИИ-мастера создания резюме (%s)...", user_id, resume_title)

            # Переход на страницу пошагового создания
            creation_urls = [
                "https://hh.ru/profile/resume/professional_role",
                "https://hh.ru/resume/create",
            ]

            opened = False
            for target_url in creation_urls:
                try:
                    await page.goto(target_url, wait_until="commit", timeout=15000)
                except Exception as e_url:
                    logger.warning("Ошибка перехода на %s: %s", target_url, e_url)
                await asyncio.sleep(2.0)
                if "login" not in page.url:
                    opened = True
                    break

            if not opened:
                try:
                    await page.goto("https://hh.ru/resume/create", wait_until="commit", timeout=15000)
                except Exception:
                    pass
                await asyncio.sleep(2.0)

            # --- ШАГ 1: Ввод должности и выбор профессии ---
            try:
                logger.info("Пользователь %d: шаг 1 — ввод должности и профессии (%s)...", user_id, resume_title)
                
                # 1. Точный клик по карточке «Укажу профессию»
                try:
                    exact_text = page.get_by_text("Укажу профессию", exact=True)
                    if await exact_text.count() > 0:
                        await human_click(page, exact_text.first)
                        await asyncio.sleep(1.5)
                    else:
                        specify_selectors = [
                            '[data-qa="professional-role-select-manual"]',
                            '*:text-is("Укажу профессию")',
                            'button:has-text("Укажу профессию")',
                            'span:has-text("Укажу профессию")',
                            '[data-qa="resume-profession-button"]'
                        ]
                        for sel in specify_selectors:
                            btn = page.locator(sel).first
                            if await btn.count() > 0:
                                await human_click(page, btn)
                                await asyncio.sleep(1.5)
                                break
                except Exception as e_specify:
                    logger.debug("Уведомление при клике на карт 'Укажу профессию': %s", e_specify)

                # 2. Ввод желаемой должности / поиска профессии
                title_sel = '[data-qa="professional-role-search-input"], [data-qa="search-input"], [data-qa="resume-title-input"], input[placeholder*="профессию"], input[placeholder*="Должность"], input[name*="title"], input[autocomplete="list"], input[type="text"]'
                try:
                    await page.wait_for_selector(title_sel, timeout=5000)
                except Exception:
                    pass

                title_input = page.locator(title_sel).first
                if await title_input.count() > 0 and await title_input.is_visible():
                    await human_type(page, title_input, resume_title)
                    await asyncio.sleep(1.0)
                    # Нажатие Enter или первого пункта автокомплита если выпадает
                    option_el = page.locator('[data-qa="professional-role-item"], div[role="option"], li[role="option"]').first
                    if await option_el.count() > 0 and await option_el.is_visible():
                        await human_click(page, option_el)
                        await asyncio.sleep(0.5)

                # 3. Кнопка «Сохранить и продолжить»
                next_selectors = [
                    '[data-qa="professional-role-submit"]',
                    '[data-qa="specialization-select-submit"]',
                    '[data-qa="resume-serp-save-and-continue"]',
                    'button:has-text("Сохранить и продолжить")',
                    'button:has-text("Продолжить")',
                    'button:has-text("Далее")',
                    'button[type="submit"]'
                ]
                for sel in next_selectors:
                    btn = page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await human_click(page, btn)
                        await asyncio.sleep(2.0)
                        break
            except Exception as ex_step1:
                logger.warning("Ошибка на Шаге 1 (должность): %s", ex_step1)

            # Модальное окно «Уточните специальность» (specialization-select-modal)
            try:
                modal = page.locator('[data-qa="specialization-select-modal"], dialog:has-text("Уточните специальность")').first
                if await modal.count() > 0 and await modal.is_visible():
                    logger.info("Пользователь %d: обработка модалки 'Уточните специальность'...", user_id)
                    chk = page.locator('input[type="checkbox"]').first
                    if await chk.count() > 0:
                        try:
                            await chk.check()
                        except Exception:
                            pass
                    m_btn = page.locator('[data-qa="specialization-select-submit"], button:has-text("Сохранить и продолжить")').first
                    if await m_btn.count() > 0:
                        await human_click(page, m_btn)
                        await asyncio.sleep(2.0)
            except Exception as ex_modal:
                logger.debug("Обработка модалки специальности: %s", ex_modal)

            # --- ШАГ 1.5: Основная личная информация (/profile/resume/common) ---
            try:
                if "resume/common" in page.url or await page.locator('[data-qa="resume-person-first-name"], [data-qa="resume-person-birth-year"]').count() > 0:
                    logger.info("Пользователь %d: шаг 1.5 — личная информация и дата рождения...", user_id)
                    
                    # Имя
                    fn_inp = page.locator('[data-qa="resume-person-first-name"], input[name*="firstName"]').first
                    if await fn_inp.count() > 0 and not (await fn_inp.input_value()):
                        await human_type(page, fn_inp, "Алексей")
                    
                    # Город
                    city_inp = page.locator('[data-qa="resume-person-area"], input[placeholder*="Город"]').first
                    if await city_inp.count() > 0 and not (await city_inp.input_value()):
                        await human_type(page, city_inp, getattr(structured, 'city', '') or "Москва")
                        await asyncio.sleep(0.5)

                    # Заполнение даты рождения (15 мая 1998)
                    bday = page.locator('[data-qa="resume-person-birth-day"], input[name*="birthDay"]').first
                    if await bday.count() > 0 and not (await bday.input_value()):
                        await human_type(page, bday, "15")

                    byear = page.locator('[data-qa="resume-person-birth-year"], input[name*="birthYear"]').first
                    if await byear.count() > 0 and not (await byear.input_value()):
                        await human_type(page, byear, "1998")

                    bmonth = page.locator('[data-qa="resume-person-birth-month"], select[name*="birthMonth"]').first
                    if await bmonth.count() > 0:
                        try:
                            await bmonth.select_option(index=5)
                        except Exception:
                            pass

                    # Подтверждение перехода к опыту
                    common_sub = page.locator('[data-qa="resume-submit"], button:has-text("Сохранить и продолжить"), button[type="submit"]').first
                    if await common_sub.count() > 0 and await common_sub.is_visible():
                        await human_click(page, common_sub)
                        await asyncio.sleep(2.5)
            except Exception as ex_step15:
                logger.warning("Ошибка на Шаге 1.5 (личная информация): %s", ex_step15)

            # --- ШАГ 2: Опыт работы (компания, должность, периоды, описание) ---
            experiences = getattr(structured, 'experiences', []) or []
            if experiences:
                logger.info("Пользователь %d: шаг 2 — заполнение %d мест работы...", user_id, len(experiences))
                try:
                    # Закрытие перекрывающих баннеров куки
                    try:
                        await page.evaluate("""() => {
                            const informer = document.getElementById('bottom-cookies-policy-informer');
                            if (informer) informer.remove();
                        }""")
                    except Exception:
                        pass

                    exp_sel = 'input:not([type="radio"])[placeholder*="Компания"], input:not([type="radio"])[name*="company"], [data-qa*="resume-work-experience-company"]'
                    try:
                        await page.wait_for_selector(exp_sel, timeout=6000)
                    except Exception:
                        pass

                    for idx, exp in enumerate(experiences):
                        if idx > 0:
                            add_exp_btn = page.locator('button:has-text("Добавить место работы"), button:has-text("Добавить"), div:has-text("Добавить"), [data-qa*="experience-add"]').first
                            if await add_exp_btn.count() > 0 and await add_exp_btn.is_visible():
                                await human_click(page, add_exp_btn)
                                await asyncio.sleep(1.5)

                        # Название компании
                        comp = getattr(exp, 'company', '')
                        if comp:
                            comp_inp = page.locator('input:not([type="radio"])[placeholder*="Компания"], input:not([type="radio"])[name*="company"], [data-qa*="work-experience-company"]').last
                            if await comp_inp.count() > 0:
                                await human_type(page, comp_inp, comp)

                        # Должность
                        pos = getattr(exp, 'position', '')
                        if pos:
                            pos_inp = page.locator('input[type="text"][placeholder*="Должность"], input[type="text"][name*="position"], [data-qa*="work-experience-position"]').last
                            if await pos_inp.count() > 0:
                                await human_type(page, pos_inp, pos)

                        # Периоды работы
                        start_year = getattr(exp, 'start_year', '')
                        if start_year:
                            syear_inp = page.locator('input[placeholder*="Год"], input[name*="startYear"], [data-qa*="start-year"]').last
                            if await syear_inp.count() > 0:
                                await human_type(page, syear_inp, str(start_year))

                        start_month = getattr(exp, 'start_month', '')
                        if start_month:
                            smonth_inp = page.locator('input[placeholder*="Месяц"], select[name*="startMonth"], [data-qa*="start-month"]').last
                            if await smonth_inp.count() > 0:
                                await human_type(page, smonth_inp, str(start_month))

                        if getattr(exp, 'is_current', False):
                            chk = page.locator('input[type="checkbox"][name*="current"], label:has-text("Работаю сейчас"), label:has-text("По настоящее время"), [data-qa*="is-current"]').last
                            if await chk.count() > 0:
                                try:
                                    await chk.check()
                                except Exception:
                                    pass
                        else:
                            end_year = getattr(exp, 'end_year', '')
                            if end_year:
                                eyear_inp = page.locator('input[name*="endYear"], [data-qa*="end-year"]').last
                                if await eyear_inp.count() > 0:
                                    await human_type(page, eyear_inp, str(end_year))

                        # Описание обязанностей
                        desc = getattr(exp, 'description', '')
                        if desc:
                            desc_inp = page.locator('textarea[placeholder*="занимались"], textarea[name*="description"], textarea').last
                            if await desc_inp.count() > 0:
                                await human_type(page, desc_inp, desc[:1000])

                    exp_next_btn = page.locator('button:has-text("Продолжить"), button:has-text("Далее"), button[type="submit"]').first
                    if await exp_next_btn.count() > 0 and await exp_next_btn.is_visible():
                        await human_click(page, exp_next_btn)
                        await asyncio.sleep(2.0)
                except Exception as ex_step2:
                    logger.warning("Ошибка на Шаге 2 (опыт работы): %s", ex_step2)

            # --- ШАГ 3: Образование ---
            education = getattr(structured, 'education', []) or []
            if education:
                logger.info("Пользователь %d: шаг 3 — заполнение образования...", user_id)
                try:
                    edu_sel = 'input[placeholder*="заведение"], input[placeholder*="Название"], [data-qa*="education"]'
                    try:
                        await page.wait_for_selector(edu_sel, timeout=5000)
                    except Exception:
                        pass

                    edu = education[0]
                    inst = getattr(edu, 'institution', '')
                    if inst:
                        inst_inp = page.locator('input[placeholder*="заведение"], input[placeholder*="Название"], input[name*="institution"]').first
                        if await inst_inp.count() > 0:
                            await human_type(page, inst_inp, inst)

                    edu_next_btn = page.locator('button:has-text("Продолжить"), button:has-text("Далее"), button[type="submit"]').first
                    if await edu_next_btn.count() > 0 and await edu_next_btn.is_visible():
                        await human_click(page, edu_next_btn)
                        await asyncio.sleep(2.0)
                except Exception as ex_step3:
                    logger.warning("Ошибка на Шаге 3 (образование): %s", ex_step3)

            # --- ШАГ 4: Навыки ---
            skills = getattr(structured, 'skills', []) or []
            if skills:
                logger.info("Пользователь %d: шаг 4 — ввод навыков...", user_id)
                try:
                    skill_sel = 'input[placeholder*="Поиск"], input[placeholder*="навык"], [data-qa*="skill"]'
                    try:
                        await page.wait_for_selector(skill_sel, timeout=5000)
                    except Exception:
                        pass

                    skill_search = page.locator('input[placeholder*="Поиск"], input[placeholder*="навык"], input[name*="skill"]').first
                    if await skill_search.count() > 0:
                        for sk in skills[:8]:
                            if sk and sk.strip():
                                await human_type(page, skill_search, sk.strip())
                                await asyncio.sleep(0.5)
                                tag_btn = page.locator(f'button:has-text("{sk.strip()}"), span:has-text("{sk.strip()}")').first
                                if await tag_btn.count() > 0 and await tag_btn.is_visible():
                                    await human_click(page, tag_btn)
                                else:
                                    await page.keyboard.press("Enter")
                                await asyncio.sleep(0.3)

                    skills_next_btn = page.locator('button:has-text("Продолжить"), button:has-text("Далее"), button[type="submit"]').first
                    if await skills_next_btn.count() > 0 and await skills_next_btn.is_visible():
                        await human_click(page, skills_next_btn)
                        await asyncio.sleep(2.0)
                except Exception as ex_step4:
                    logger.warning("Ошибка на Шаге 4 (навыки): %s", ex_step4)

            # --- ШАГ 5: Финальная публикация ---
            try:
                final_pub_btn = page.locator('[data-qa="resume-publish"], [data-qa="resume-save"], [data-qa="resume-submit"], button:has-text("Опубликовать"), button:has-text("Сохранить и опубликовать"), button:has-text("Сохранить")').first
                if await final_pub_btn.count() > 0 and await final_pub_btn.is_visible():
                    await human_click(page, final_pub_btn)
                    await asyncio.sleep(3.0)
            except Exception as ex_step5:
                logger.warning("Ошибка при финишной публикации резюме: %s", ex_step5)

            logger.info("Пользователь %d: ИИ-резюме пошагово создано и опубликовано!", user_id)

            # Автоматическая актуализация списка резюме в кэше БД
            try:
                import json
                from database import update_account_settings
                hh_res = await cls.fetch_user_resumes(user_id, account_id=account.get("id") if 'account' in locals() else None)
                acc_id = account["id"] if 'account' in locals() and account else None
                if acc_id:
                    res_list = hh_res.get("resumes", []) if isinstance(hh_res, dict) else []
                    if not res_list:
                        res_list = [{
                            "id": "ai_created_resume",
                            "title": resume_title,
                            "href": "https://hh.ru/applicant/resumes",
                            "status": "Опубликовано"
                        }]
                    await update_account_settings(
                        acc_id,
                        resumes_json=json.dumps(res_list),
                        active_resume_url=res_list[0]["href"],
                        active_resume_title=res_list[0]["title"]
                    )
                    logger.info("Успешно сохранено активное резюме '%s' в СУБД для аккаунта %d", res_list[0]["title"], acc_id)
            except Exception as ex_save:
                logger.warning("Ошибка авто-обновления СУБД после создания резюме: %s", ex_save)

            return {"status": "SUCCESS", "message": f"Резюме «{resume_title}» успешно создано, сохранено в базе данных и опубликовано на hh.ru!"}

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

        engine = await SharedBrowserPool.get_engine(proxy_url=account.get("proxy_url"))
        context = None

        try:
            context = await engine.create_context(storage_state=storage_state)
            page = await context.new_page()

            target_url = f"https://hh.ru/resume/{resume_id}"
            logger.info("Пользователь %d: переход на страницу резюме для удаления: %s", user_id, target_url)
            try:
                await page.goto(target_url, wait_until="commit", timeout=15000)
            except Exception as e_g:
                logger.warning("Ошибка перехода на резюме %s: %s", target_url, e_g)
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
                try:
                    await page.goto("https://hh.ru/applicant/resumes", wait_until="commit", timeout=15000)
                except Exception:
                    pass
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

