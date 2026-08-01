"""
LeadScout AI — Асинхронный воркер Taskiq (Redis Broker / InMemory Broker Fallback).
Выполняет фоновые задачи по браузерной автотизации hh.ru, первичной OTP-авторизации, автооткликам
и повторной отправке подтвержденных анкет из Telegram.
"""

import asyncio
import logging
import json
import socket
from taskiq import InMemoryBroker
from aiogram import Bot

from config import REDIS_URL, BOT_TOKEN
from database import (
    get_or_create_user,
    get_user_session,
    update_user_session,
    save_hh_apply,
    increment_applied_today,
    is_already_applied,
    save_pending_questionnaire,
    get_pending_questionnaire,
    update_pending_questionnaire_status,
)
from keyboards import get_questionnaire_confirmation_keyboard
from utils.security import SessionSecurityManager
from parsers.hh_browser import HHBrowserEngine
from parsers.hh_applicant import apply_to_hh_vacancy, submit_approved_questionnaire

logger = logging.getLogger(__name__)


def _is_redis_available(host: str = "127.0.0.1", port: int = 6379) -> bool:
    """Проверяет доступность порта Redis."""
    try:
        s = socket.socket()
        s.settimeout(1)
        res = s.connect_ex((host, port))
        s.close()
        return res == 0
    except Exception:
        return False


# Динамический выбор брокера: Redis если доступен, иначе InMemoryBroker
if _is_redis_available():
    try:
        from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend
        result_backend = RedisAsyncResultBackend(redis_url=REDIS_URL)
        broker = ListQueueBroker(url=REDIS_URL).with_result_backend(result_backend)
        logger.info("Taskiq успешно подключен к Redis (%s)", REDIS_URL)
    except Exception as e:
        broker = InMemoryBroker()
        logger.warning("Не удалось подключиться к Redis (%s). Используется InMemoryBroker.", e)
else:
    broker = InMemoryBroker()
    logger.info("ℹ️ Redis не обнаружен на порту 6379. Автоматически активирован локальный InMemoryBroker.")

security_mgr = SessionSecurityManager()


import urllib.parse


@broker.task
async def process_user_hh_applications(user_id: int) -> dict:
    """
    Фоновая задача: запуск цикла поиска вакансий и откликов для конкретного пользователя hh.ru.
    """
    user = await get_or_create_user(user_id)
    if user["session_status"] != "ACTIVE":
        logger.info("Пользователь %d не авторизован в hh.ru (статус: %s). Пропуск.", user_id, user["session_status"])
        return {"status": "SKIPPED_NOT_AUTHORIZED"}

    if not user.get("auto_apply_enabled"):
        logger.info("Автоотклик остановлен пользователем %d. Пропуск выполнения.", user_id)
        return {"status": "SKIPPED_STOPPED_BY_USER"}

    if user["applied_today"] >= user["daily_limit"]:
        logger.info("Пользователь %d достиг суточного лимита откликов (%d/%d).", user_id, user["applied_today"], user["daily_limit"])
        return {"status": "SKIPPED_LIMIT_REACHED"}

    encrypted_state, status = await get_user_session(user_id)
    if not encrypted_state:
        return {"status": "SKIPPED_NO_SESSION"}

    try:
        storage_state = security_mgr.decrypt_storage_state(encrypted_state)
    except Exception as e:
        logger.error("Не удалось расшифровать сессию для user_id %d: %s", user_id, e)
        await update_user_session(user_id, b"", "EXPIRED")
        return {"status": "EXPIRED_SESSION"}

    engine = HHBrowserEngine(proxy_url=user.get("proxy_url"))
    context = None
    try:
        await engine.start()
        context = await engine.create_context(storage_state=storage_state)
        search_tab = await context.new_page()

        keywords_str = user.get("keywords", "Python")
        kw_list = [k.strip() for k in keywords_str.split(",") if k.strip()]
        if not kw_list:
            kw_list = ["Python"]

        stop_words = [w.strip().lower() for w in user.get("stop_words", "").split(",") if w.strip()]

        processed_count = 0
        seen_urls_in_run = set()

        for kw in kw_list:
            if user["applied_today"] >= user["daily_limit"]:
                logger.info("Пользователь %d достиг суточного лимита (%d). Завершение.", user_id, user["daily_limit"])
                break

            encoded_kw = urllib.parse.quote_plus(kw)

            # Пагинация по страницам поисковой выдачи для каждого ключевого слова
            for page_num in range(3):
                if user["applied_today"] >= user["daily_limit"]:
                    break

                search_url = f"https://hh.ru/search/vacancy?text={encoded_kw}&order_by=publication_time&search_period=3&page={page_num}"
                if user.get("min_salary"):
                    search_url += f"&salary={user['min_salary']}&currency_code=RUR"
                if user.get("only_remote"):
                    search_url += "&schedule=remote"

                logger.info("Поиск вакансий по ключу '%s' (Стр. %d): %s", kw, page_num + 1, search_url)
                await search_tab.goto(search_url, wait_until="domcontentloaded")
                await search_tab.wait_for_timeout(1500)

                # Перехват возможной инвалидации сессии
                if "account/login" in search_tab.url:
                    logger.warning("Сессия пользователя %d истекла.", user_id)
                    await update_user_session(user_id, b"", "EXPIRED")
                    
                    if BOT_TOKEN:
                        bot = Bot(token=BOT_TOKEN)
                        await bot.send_message(
                            chat_id=user_id,
                            text="⚠️ **Сессия hh.ru истекла.** Пожалуйста, пройдите повторную авторизацию."
                        )
                        await bot.session.close()
                        return {"status": "SESSION_EXPIRED"}

                # Извлечение карточек вакансий через актуальные DOM-селекторы hh.ru
                vacancy_cards = await search_tab.locator('[data-qa="serp-item__title"], [data-qa="vacancy-serp__vacancy-title"], a[data-qa*="vacancy-title"]').all()
                if not vacancy_cards:
                    logger.info("На странице %d для ключевого слова '%s' больше нет вакансий.", page_num + 1, kw)
                    break

                urls_with_titles = []
                for link in vacancy_cards:
                    href = await link.get_attribute("href")
                    title = await link.text_content() or ""
                    if href and "/vacancy/" in href and "/response" not in href:
                        clean_url = href.split("?")[0] if href.startswith("http") else f"https://hh.ru{href.split('?')[0]}"
                        if clean_url not in seen_urls_in_run:
                            seen_urls_in_run.add(clean_url)
                            urls_with_titles.append((clean_url, title.strip().lower()))

            for vacancy_url, vac_title_lower in urls_with_titles:
                if user["applied_today"] >= user["daily_limit"]:
                    break

                # 1. Ранняя фильтрация по стоп-словам
                if stop_words and any(sw in vac_title_lower for sw in stop_words):
                    logger.info("Вакансия '%s' пропущена (стоп-слово: %s).", vacancy_url, vac_title_lower)
                    continue

                already = await is_already_applied(user_id, vacancy_url)
                if already:
                    logger.debug("Пользователь %d уже откликался на %s. Пропуск.", user_id, vacancy_url)
                    continue

                # Отдельная изоляция вкладки браузера для предотвращения утечки RAM
                vac_page = await context.new_page()
                try:
                    status_code, cover_letter, extra = await apply_to_hh_vacancy(
                        page=vac_page,
                        resume_context=user.get("resume_text", "Python разработчик с опытом."),
                        vacancy_url=vacancy_url,
                        target_resume_title=user.get("active_resume_title")
                    )
                finally:
                    await vac_page.close()

                # Вторичная проверка стоп-слов в полном описании
                if extra and isinstance(extra, dict) and stop_words:
                    full_desc = (extra.get("title", "") + " " + extra.get("description", "")).lower()
                    if any(sw in full_desc for sw in stop_words):
                        logger.info("Вакансия %s пропущена из-за наличия стоп-слова в описании.", vacancy_url)
                        continue

                if status_code == "SKIPPED_IRRELEVANT":
                    reason = extra.get("reason", "Не соответствует профилю") if isinstance(extra, dict) else "Не соответствует профилю"
                    logger.info("Пользователь %d: вакансия %s пропущена как нерелевантная резюме (Причина: %s).", user_id, vacancy_url, reason)
                    continue

                if status_code in ["APPLIED_DIRECT", "APPLIED_WITH_LETTER"]:
                    await save_hh_apply(user_id, vacancy_url, cover_letter or "", status_code)
                    current_applied = await increment_applied_today(user_id)
                    user["applied_today"] = current_applied
                    processed_count += 1
                    
                    if BOT_TOKEN:
                        company_name = extra.get("company", "Работодатель") if isinstance(extra, dict) else "Работодатель"
                        vac_title = extra.get("title", "Вакансия") if isinstance(extra, dict) else "Вакансия"
                        
                        bot = Bot(token=BOT_TOKEN)
                        msg_text = (
                            f"🎯 **Отклик отправлен в компанию {company_name}!**\n\n"
                            f"📌 **Вакансия:** [{vac_title}]({vacancy_url})\n"
                            f"Статус: `{status_code}`\n"
                        )
                        if cover_letter:
                            msg_text += f"\n📝 **Сопроводительное письмо:**\n`{cover_letter[:300]}...`"
                        
                        await bot.send_message(chat_id=user_id, text=msg_text, parse_mode="Markdown")
                        await bot.session.close()

                    await asyncio.sleep(user.get("min_delay_sec", 30))

                elif status_code == "QUESTIONNAIRE_REQUIRED" and extra:
                    v_title = extra.get("vacancy", {}).get("title", "Вакансия с анкетой")
                    questions = extra.get("questions", [])
                    ai_payload = extra.get("ai_payload", {})

                    apply_id = await save_pending_questionnaire(
                        user_id=user_id,
                        vacancy_url=vacancy_url,
                        vacancy_title=v_title,
                        cover_letter=cover_letter or "",
                        questions=questions,
                        ai_payload=ai_payload
                    )

                    if BOT_TOKEN:
                        bot = Bot(token=BOT_TOKEN)
                        q_text = "\n".join([f"• {q}" for q in questions[:3]])
                        msg_text = (
                            f"❓ **Требуется ваше подтверждение отклика!**\n\n"
                            f"📌 **Вакансия:** [{v_title}]({vacancy_url})\n"
                            f"❓ **Вопросы работодателя:**\n{q_text}\n\n"
                            f"📝 **Предложенное письмо:**\n`{(cover_letter or '')[:250]}...`"
                        )
                        await bot.send_message(
                            chat_id=user_id,
                            text=msg_text,
                            reply_markup=get_questionnaire_confirmation_keyboard(apply_id),
                            parse_mode="Markdown"
                        )
                        await bot.session.close()

        # Обновляем сохраненные куки в шифрованном виде после работы
        new_state = await context.storage_state()
        encrypted_new_state = security_mgr.encrypt_storage_state(new_state)
        await update_user_session(user_id, encrypted_new_state, "ACTIVE")

        return {"status": "SUCCESS", "processed": processed_count}

    except Exception as e:
        logger.error("Ошибка при выполнении задачи для %d: %s", user_id, e)
        return {"status": f"ERROR: {e}"}

    finally:
        if context:
            await context.close()
        await engine.close()


@broker.task
async def submit_approved_hh_questionnaire(user_id: int, apply_id: int) -> dict:
    """
    Фоновая задача: физическая отправка подтвержденного отклика с анкетой на hh.ru.
    """
    item = await get_pending_questionnaire(apply_id)
    if not item:
        return {"status": "NOT_FOUND"}

    encrypted_state, status = await get_user_session(user_id)
    if not encrypted_state:
        return {"status": "NO_SESSION"}

    try:
        storage_state = security_mgr.decrypt_storage_state(encrypted_state)
    except Exception as e:
        return {"status": "EXPIRED_SESSION"}

    answers = []
    if item.get("ai_payload_json"):
        try:
            payload = json.loads(item["ai_payload_json"])
            answers = payload.get("answers", [])
        except Exception:
            answers = []

    user = await get_or_create_user(user_id)
    engine = HHBrowserEngine(proxy_url=user.get("proxy_url"))
    context = None
    try:
        await engine.start()
        context = await engine.create_context(storage_state=storage_state)
        page = await context.new_page()

        success, msg = await submit_approved_questionnaire(
            page=page,
            vacancy_url=item["vacancy_url"],
            cover_letter=item.get("cover_letter", ""),
            answers=answers
        )

        if success:
            await save_hh_apply(user_id, item["vacancy_url"], item.get("cover_letter", ""), "APPLIED_WITH_QUESTIONNAIRE")
            await increment_applied_today(user_id)
            await update_pending_questionnaire_status(apply_id, "SUBMITTED")

            if BOT_TOKEN:
                bot = Bot(token=BOT_TOKEN)
                v_title = item.get("vacancy_title", "Вакансия")
                await bot.send_message(
                    chat_id=user_id,
                    text=f"🎯 **Отклик с анкетой успешно отправлен на hh.ru!**\n📌 [{v_title}]({item['vacancy_url']})",
                    parse_mode="Markdown"
                )
                await bot.session.close()

        return {"status": "SUCCESS" if success else "ERROR", "message": msg}

    except Exception as e:
        logger.error("Ошибка при отправке одобренной анкеты %d: %s", apply_id, e)
        return {"status": f"ERROR: {e}"}
    finally:
        if context:
            await context.close()
        await engine.close()
