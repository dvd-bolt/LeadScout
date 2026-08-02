"""
LeadScout AI — Асинхронный воркер Taskiq (Redis Broker / InMemory Broker Fallback).
Выполняет фоновые задачи по браузерной автотизации hh.ru, автооткликам для мульти-аккаунтов
и повторной отправке подтвержденных анкет из Telegram.
"""

import asyncio
import logging
import json
import random
import socket
import urllib.parse
from taskiq import InMemoryBroker
from aiogram import Bot

from config import REDIS_URL, BOT_TOKEN, MAX_CONCURRENT_BROWSERS
from database import (
    get_or_create_user,
    get_user_accounts,
    get_account_by_id,
    update_account_session,
    update_account_settings,
    save_account_hh_apply,
    increment_account_applied_today,
    is_account_already_applied,
    save_pending_questionnaire_account,
    get_pending_questionnaire,
    update_pending_questionnaire_status,
)
from keyboards import get_questionnaire_confirmation_keyboard
from utils.security import SessionSecurityManager
from parsers.hh_browser import HHBrowserEngine
from parsers.hh_applicant import apply_to_hh_vacancy, submit_approved_questionnaire
from ai_handler import extract_search_keywords_from_resume

logger = logging.getLogger(__name__)

# Семафор контроля параллельных браузеров (по умолчанию 2 на 1 IP)
browser_semaphore = asyncio.Semaphore(MAX_CONCURRENT_BROWSERS)


def _escape_md(text: str) -> str:
    """Экранирует спецсимволы Markdown V1 для безопасной отправки в Telegram."""
    for ch in ('_', '*', '`', '['):
        text = text.replace(ch, f'\\{ch}')
    return text


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


active_browsers_count = 0


@broker.task
async def process_account_hh_applications(account_id: int) -> dict:
    """
    Фоновая задача: запуск цикла поиска вакансий и откликов для конкретного аккаунта hh.ru.
    Работает под семафором (до 2 браузеров одновременно).
    При 1 аккаунте запуск мгновенный (0 сек), при 2+ аккаунтах — рассинхронизация 5-15 сек.
    """
    global active_browsers_count
    account = await get_account_by_id(account_id)
    if not account:
        logger.error("Аккаунт id=%d не найден в БД.", account_id)
        return {"status": "NOT_FOUND"}

    user_id = account["user_id"]
    account_name = account.get("account_name") or account.get("phone_or_email") or f"ID {account_id}"

    if account.get("session_status") != "ACTIVE":
        logger.info("Аккаунт %s (user %d) не авторизован в hh.ru (статус: %s). Пропуск.", account_name, user_id, account.get("session_status"))
        return {"status": "SKIPPED_NOT_AUTHORIZED"}

    if not account.get("auto_apply_enabled"):
        logger.info("Автоотклик остановлен для аккаунта %s (user %d). Пропуск выполнения.", account_name, user_id)
        return {"status": "SKIPPED_STOPPED_BY_USER"}

    if account.get("applied_today", 0) >= account.get("daily_limit", 50):
        logger.info("Аккаунт %s (user %d) достиг суточного лимита откликов (%d/%d).", account_name, user_id, account["applied_today"], account["daily_limit"])
        return {"status": "SKIPPED_LIMIT_REACHED"}

    encrypted_state = account.get("encrypted_storage_state")
    if not encrypted_state:
        return {"status": "SKIPPED_NO_SESSION"}

    try:
        storage_state = security_mgr.decrypt_storage_state(encrypted_state)
    except Exception as e:
        logger.error("Не удалось расшифровать сессию для аккаунта %s: %s", account_name, e)
        await update_account_session(account_id, b"", "EXPIRED")
        return {"status": "EXPIRED_SESSION"}

    # Захват семафора контроля количества параллельных браузеров (до 2)
    async with browser_semaphore:
        if active_browsers_count > 0:
            stagger_delay = random.uniform(5.0, 15.0)
            logger.info("Параллельный браузер #%d для '%s' (стартовая рассинхронизация: %.1f сек)...", active_browsers_count + 1, account_name, stagger_delay)
            await asyncio.sleep(stagger_delay)
        else:
            logger.info("Единственный браузер для '%s' запущен мгновенно (без задержки)...", account_name)

        active_browsers_count += 1
        engine = HHBrowserEngine(proxy_url=account.get("proxy_url"))
        context = None
        try:
            await engine.start()
            context = await engine.create_context(storage_state=storage_state)
            search_tab = await context.new_page()

            # Извлечение/формирование ключевых слов
            keywords_str = account.get("keywords")
            if not keywords_str or keywords_str.strip() in ["", "Python", "Python, Backend, FastAPI, Django"]:
                resume_text = account.get("resume_text", "")
                resume_title = account.get("active_resume_title", "")
                ai_keywords = extract_search_keywords_from_resume(resume_text, resume_title)
                if ai_keywords:
                    keywords_str = ", ".join(ai_keywords)
                    await update_account_settings(account_id, keywords=keywords_str)
                else:
                    keywords_str = resume_title or "Python"

            kw_list = [k.strip() for k in keywords_str.split(",") if k.strip()]
            if not kw_list:
                kw_list = [account.get("active_resume_title") or "Python"]

            stop_words = [w.strip().lower() for w in account.get("stop_words", "").split(",") if w.strip()]

            processed_count = 0
            seen_urls_in_run = set()

            for kw in kw_list:
                urls_with_titles = []

                # Проверка флага автооткликов перед каждым ключевым словом
                curr_acc = await get_account_by_id(account_id)
                if not curr_acc or not curr_acc.get("auto_apply_enabled"):
                    logger.info("Автоотклик остановлен для %s во время выполнения. Прерывание.", account_name)
                    break

                if curr_acc["applied_today"] >= curr_acc["daily_limit"]:
                    logger.info("Аккаунт %s достиг лимита (%d). Завершение.", account_name, curr_acc["daily_limit"])
                    break

                encoded_kw = urllib.parse.quote_plus(kw)

                for page_num in range(3):
                    curr_acc = await get_account_by_id(account_id)
                    if not curr_acc or not curr_acc.get("auto_apply_enabled") or curr_acc["applied_today"] >= curr_acc["daily_limit"]:
                        break

                    search_url = f"https://hh.ru/search/vacancy?text={encoded_kw}&order_by=publication_time&search_period=3&page={page_num}"
                    if curr_acc.get("min_salary"):
                        search_url += f"&salary={curr_acc['min_salary']}&currency_code=RUR"
                    if curr_acc.get("only_remote"):
                        search_url += "&schedule=remote"

                    logger.info("Поиск вакансий [%s] по ключу '%s' (Стр. %d)...", account_name, kw, page_num + 1)
                    try:
                        await search_tab.goto(search_url, wait_until="domcontentloaded", timeout=20000)
                    except Exception as nav_err:
                        logger.warning("Таймаут перехода [%s] на %s: %s", account_name, search_url, nav_err)
                        try:
                            await search_tab.goto(search_url, wait_until="commit", timeout=15000)
                        except Exception:
                            continue
                    await search_tab.wait_for_timeout(1500)

                    # Проверка инвалидации сессии
                    if "account/login" in search_tab.url:
                        logger.warning("Сессия аккаунта %s (%d) истекла.", account_name, user_id)
                        await update_account_session(account_id, b"", "EXPIRED")
                        if BOT_TOKEN:
                            bot = Bot(token=BOT_TOKEN)
                            safe_name = _escape_md(account_name)
                            await bot.send_message(
                                chat_id=user_id,
                                text=f"⚠️ **Сессия аккаунта `{safe_name}` истекла.** Пожалуйста, войдите повторно через `👤 Мои аккаунты`."
                            )
                            await bot.session.close()
                        return {"status": "SESSION_EXPIRED"}

                    vacancy_cards = await search_tab.locator('[data-qa="serp-item__title"], [data-qa="vacancy-serp__vacancy-title"], a[data-qa*="vacancy-title"]').all()
                    if not vacancy_cards:
                        break

                    for link in vacancy_cards:
                        href = await link.get_attribute("href")
                        title = await link.text_content() or ""
                        if href and "/vacancy/" in href and "/response" not in href:
                            clean_url = href.split("?")[0] if href.startswith("http") else f"https://hh.ru{href.split('?')[0]}"
                            if clean_url not in seen_urls_in_run:
                                seen_urls_in_run.add(clean_url)
                                urls_with_titles.append((clean_url, title.strip().lower()))

                for vacancy_url, vac_title_lower in urls_with_titles:
                    curr_acc = await get_account_by_id(account_id)
                    if not curr_acc or not curr_acc.get("auto_apply_enabled") or curr_acc["applied_today"] >= curr_acc["daily_limit"]:
                        break

                    # 1. Фильтрация по стоп-словам
                    if stop_words and any(sw in vac_title_lower for sw in stop_words):
                        continue

                    already = await is_account_already_applied(account_id, vacancy_url)
                    if already:
                        continue

                    vac_page = await context.new_page()
                    try:
                        status_code, cover_letter, extra = await apply_to_hh_vacancy(
                            page=vac_page,
                            resume_context=account.get("resume_text", "Разработчик с опытом."),
                            vacancy_url=vacancy_url,
                            target_resume_title=account.get("active_resume_title"),
                            send_cover_letter=bool(account.get("send_cover_letter", 1)),
                            stop_words=stop_words
                        )
                    finally:
                        await vac_page.close()

                    if status_code == "SKIPPED_STOP_WORD":
                        logger.info("Аккаунт %s: вакансия %s пропущена из-за стоп-слова в описании.", account_name, vacancy_url)
                        continue

                    if status_code == "SKIPPED_IRRELEVANT":
                        reason = extra.get("reason", "Не соответствует профилю") if isinstance(extra, dict) else "Не соответствует профилю"
                        logger.info("Аккаунт %s: вакансия %s пропущена как нерелевантная (%s)", account_name, vacancy_url, reason)
                        continue

                    if status_code in ["APPLIED_DIRECT", "APPLIED_WITH_LETTER"]:
                        await save_account_hh_apply(user_id, account_id, vacancy_url, cover_letter or "", status_code)
                        current_applied = await increment_account_applied_today(account_id)
                        processed_count += 1

                        if BOT_TOKEN:
                            company_name = extra.get("company", "Работодатель") if isinstance(extra, dict) else "Работодатель"
                            vac_title = extra.get("title", "Вакансия") if isinstance(extra, dict) else "Вакансия"
                            safe_acc_name = _escape_md(account_name)
                            safe_company = _escape_md(company_name)
                            safe_title = _escape_md(vac_title)
                            safe_letter = _escape_md((cover_letter or "")[:300])

                            bot = Bot(token=BOT_TOKEN)
                            msg_text = (
                                f"🎯 *Отклик отправлен с аккаунта `{safe_acc_name}`!*\n\n"
                                f"🏢 *Компания:* {safe_company}\n"
                                f"📌 *Вакансия:* [{safe_title}]({vacancy_url})\n"
                                f"📊 *Всего сегодня:* `{current_applied}/{account.get('daily_limit', 50)}`\n"
                            )
                            if cover_letter:
                                msg_text += f"\n📝 *Сопроводительное письмо:*\n{safe_letter}"

                            try:
                                await bot.send_message(chat_id=user_id, text=msg_text, parse_mode="Markdown")
                            except Exception:
                                plain = f"🎯 Отклик отправлен (Аккаунт: {account_name})!\n🏢 Компания: {company_name}\n📌 Вакансия: {vac_title}\n{vacancy_url}"
                                await bot.send_message(chat_id=user_id, text=plain)
                            await bot.session.close()

                        await asyncio.sleep(account.get("min_delay_sec", 30))

                    elif status_code == "QUESTIONNAIRE_REQUIRED" and extra:
                        v_title = extra.get("vacancy", {}).get("title", "Вакансия с анкетой")
                        questions = extra.get("questions", [])
                        ai_payload = extra.get("ai_payload", {})

                        apply_id = await save_pending_questionnaire_account(
                            user_id=user_id,
                            account_id=account_id,
                            vacancy_url=vacancy_url,
                            vacancy_title=v_title,
                            cover_letter=cover_letter or "",
                            questions=questions,
                            ai_payload=ai_payload
                        )

                        if BOT_TOKEN:
                            bot = Bot(token=BOT_TOKEN)
                            q_text = "\n".join([f"• {_escape_md(q)}" for q in questions[:3]])
                            safe_v_title = _escape_md(v_title)
                            safe_acc_name = _escape_md(account_name)
                            safe_cl = _escape_md((cover_letter or '')[:250])
                            msg_text = (
                                f"❓ *Требуется подтверждение отклика (`{safe_acc_name}`)*!\n\n"
                                f"📌 *Вакансия:* [{safe_v_title}]({vacancy_url})\n"
                                f"❓ *Вопросы работодателя:*\n{q_text}\n\n"
                                f"📝 *Предложенное письмо:*\n{safe_cl}"
                            )
                            try:
                                await bot.send_message(
                                    chat_id=user_id,
                                    text=msg_text,
                                    reply_markup=get_questionnaire_confirmation_keyboard(apply_id),
                                    parse_mode="Markdown"
                                )
                            except Exception:
                                plain = f"❓ Требуется подтверждение отклика ({account_name})!\n📌 Вакансия: {v_title}\n{vacancy_url}"
                                await bot.send_message(
                                    chat_id=user_id,
                                    text=plain,
                                    reply_markup=get_questionnaire_confirmation_keyboard(apply_id)
                                )
                            await bot.session.close()

            # Обновление сохраненных кук после работы
            new_state = await context.storage_state()
            encrypted_new_state = security_mgr.encrypt_storage_state(new_state)
            await update_account_session(account_id, encrypted_new_state, "ACTIVE")

            return {"status": "SUCCESS", "processed": processed_count}

        except Exception as e:
            logger.error("Ошибка при выполнении задачи для аккаунта %s (id=%d): %s", account_name, account_id, e)
            return {"status": f"ERROR: {e}"}

        finally:
            active_browsers_count = max(0, active_browsers_count - 1)
            if context:
                await context.close()
            await engine.close()


@broker.task
async def process_user_hh_applications(user_id: int) -> dict:
    """
    Фоновая задача для пользователя: запускает поисковые задачи для всех его активных аккаунтов.
    """
    accounts = await get_user_accounts(user_id)
    active_accs = [acc for acc in accounts if acc.get("session_status") == "ACTIVE" and acc.get("auto_apply_enabled")]

    if not active_accs:
        logger.info("Пользователь %d не имеет активных аккаунтов для автоотклика.", user_id)
        return {"status": "NO_ACTIVE_ACCOUNTS"}

    for acc in active_accs:
        try:
            await process_account_hh_applications.kiq(acc["id"])
        except Exception:
            asyncio.create_task(process_account_hh_applications(acc["id"]))

    return {"status": "SUCCESS", "launched_accounts": len(active_accs)}


@broker.task
async def submit_approved_hh_questionnaire(user_id: int, apply_id: int) -> dict:
    """
    Фоновая задача: физическая отправка подтвержденного отклика с анкетой на hh.ru.
    """
    item = await get_pending_questionnaire(apply_id)
    if not item:
        return {"status": "NOT_FOUND"}

    account_id = item.get("account_id")
    account = await get_account_by_id(account_id) if account_id else None

    if account:
        encrypted_state = account.get("encrypted_storage_state")
        proxy_url = account.get("proxy_url")
    else:
        user = await get_or_create_user(user_id)
        encrypted_state = user.get("encrypted_storage_state")
        proxy_url = user.get("proxy_url")

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

    engine = HHBrowserEngine(proxy_url=proxy_url)
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
            await save_account_hh_apply(user_id, account_id or 0, item["vacancy_url"], item.get("cover_letter", ""), "APPLIED_WITH_QUESTIONNAIRE")
            if account_id:
                await increment_account_applied_today(account_id)
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
