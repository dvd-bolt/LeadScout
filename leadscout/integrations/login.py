"""
LeadScout AI — Модуль интерактивной OTP-авторизации hh.ru (Patchright Stealth).
Управляет процессами входа через СМС/email для пользователей Telegram.
"""

import asyncio
import logging
import time
from typing import Any

from patchright.async_api import BrowserContext, Page

from leadscout.core.concurrency import serialize_login
from leadscout.integrations.browser import HHBrowserEngine
from utils.humanization import HumanizationError, human_click, human_type, human_type_digits

logger = logging.getLogger(__name__)


class HHLoginSession:
    """Сессия авторизации hh.ru для конкретного пользователя и аккаунта."""

    def __init__(
        self, user_id: int, phone_or_email: str, account_id: int | None = None, *, db, engine_factory, security_factory
    ):
        self.db = db
        self.engine_factory = engine_factory
        self.security_factory = security_factory
        self.user_id = user_id
        self.phone_or_email = phone_or_email
        self.account_id = account_id
        self.engine: HHBrowserEngine | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.otp_event = asyncio.Event()
        self.otp_code: str | None = None
        self.result: dict[str, Any] = {}
        self.is_done = False
        self.created_at = time.time()

    async def start_login_flow(self) -> dict[str, Any]:
        """Первая фаза: запуск браузера и ввод номера телефона / email."""
        try:
            account = await self.db.get_account_for_user(self.user_id, self.account_id) if self.account_id else None
            self.engine = self.engine_factory(proxy_url=(account or {}).get("proxy_url") or None)
            await self.engine.start()
            self.context = await self.engine.create_context()
            self.page = await self.context.new_page()
            logger.info("Пользователь %d: переход на страницу логина hh.ru...", self.user_id)
            await self.page.goto("https://hh.ru/account/login", wait_until="domcontentloaded")
            await asyncio.sleep(2.0)

            # 1. Выбор типа аккаунта (Соискатель), если на первом шаге показана плашка выбора
            applicant_card = self.page.locator('[data-qa="account-type-card-APPLICANT"]').first
            if await applicant_card.count() > 0:
                await human_click(self.page, applicant_card)
                await asyncio.sleep(0.5)

            # Клик по кнопке продолжить на шаге выбора роли
            step1_submit = self.page.locator(
                '[data-qa="submit-button"], button[type="submit"], [data-qa="account-signup-submit"]'
            ).first
            if await step1_submit.count() > 0 and await step1_submit.is_visible():
                await human_click(self.page, step1_submit)
                await asyncio.sleep(2.0)

            # 2. Определение формата (телефон или email)
            login_str = self.phone_or_email.strip()
            is_email = "@" in login_str

            if is_email:
                # Переключение на email если есть таб
                email_tab = self.page.locator('[data-qa="credential-type-email"]').first
                if await email_tab.count() > 0:
                    await human_click(self.page, email_tab)
                    await asyncio.sleep(0.5)
                text_to_type = login_str
            else:
                # Очистка и форматирование телефона для национального инпута Magritte (10 цифр без +7/8)
                import re

                digits = re.sub(r"\D", "", login_str)
                if len(digits) == 11 and digits.startswith(("7", "8")):
                    text_to_type = digits[1:]
                else:
                    text_to_type = digits

                phone_tab = self.page.locator('[data-qa="credential-type-phone"]').first
                if await phone_tab.count() > 0:
                    await human_click(self.page, phone_tab)
                    await asyncio.sleep(0.5)

            # 3. Поиск и ввод логина
            login_input = self.page.locator(
                '[data-qa="magritte-phone-input-national-number-input"], '
                '[data-qa="account-signup-email"], '
                'input[name="login"], '
                'input[type="text"], '
                'input[type="tel"], '
                'input[type="email"]'
            ).first

            if await login_input.count() == 0:
                logger.error("Пользователь %d: не найдено поле ввода логина на hh.ru", self.user_id)
                await self.abort()
                return {"status": "ERROR", "message": "Не найдено поле ввода логина. Проверьте адрес входа hh.ru."}

            await human_type(
                self.page,
                login_input,
                text_to_type,
                value_mode="exact" if is_email else "digits",
            )
            await asyncio.sleep(0.5)

            # 4. Нажатие кнопки продолжить / запросить код
            submit_btn = self.page.locator(
                '[data-qa="submit-button"], [data-qa="account-signup-submit"], button[type="submit"]'
            ).first
            if await submit_btn.count() > 0 and await submit_btn.is_visible():
                await human_click(self.page, submit_btn)
                await asyncio.sleep(2.5)

            # 5. Проверка появления капчи (картинки с кодом)
            await asyncio.sleep(1.0)
            captcha_img = self.page.locator(
                '[data-qa="account-captcha-picture"], img[src*="/captcha/picture"], img[data-qa="captcha-image"]'
            ).first
            captcha_input = self.page.locator(
                '[data-qa="account-captcha-input"], input[name="captchaText"], input[name="captcha"]'
            ).first

            if await captcha_img.is_visible() or await captcha_input.is_visible():
                logger.info("Пользователь %d: обнаружена капча hh.ru! Запрос решения через Telegram...", self.user_id)
                await asyncio.sleep(1.0)

                if await captcha_img.is_visible():
                    await captcha_img.scroll_into_view_if_needed()
                    captcha_bytes = await captcha_img.screenshot()
                else:
                    form_elem = self.page.locator('form, [data-qa="account-login-form"]').first
                    if await form_elem.is_visible():
                        captcha_bytes = await form_elem.screenshot()
                    else:
                        captcha_bytes = await self.page.screenshot()
                return {"status": "WAITING_FOR_CAPTCHA", "captcha_bytes": captcha_bytes}

            logger.info("Пользователь %d: СМС-код запрошен на hh.ru. Ожидание кода из Telegram...", self.user_id)
            return {"status": "WAITING_FOR_OTP"}

        except Exception as e:
            logger.error("Пользователь %d: ошибка 1 фазы логина: %s", self.user_id, type(e).__name__)
            await self.abort()
            return {"status": "ERROR", "message": "Не удалось открыть форму входа hh.ru."}

    async def _get_fresh_captcha_bytes(self, old_src: str | None = None) -> bytes:
        """Ожидает загрузки нового URL картинки капчи и снимает точный скриншот."""
        captcha_img = self.page.locator(
            '[data-qa="account-captcha-picture"], img[src*="/captcha/picture"], img[data-qa="captcha-image"]'
        ).first

        if old_src and await captcha_img.count() > 0:
            try:
                await self.page.wait_for_function(
                    'old => { const img = document.querySelector(\'[data-qa="account-captcha-picture"], img[src*="/captcha/picture"], img[data-qa="captcha-image"]\'); return img && img.getAttribute(\'src\') !== old; }',
                    arg=old_src,
                    timeout=4000,
                )
            except Exception:
                pass

        await asyncio.sleep(0.8)

        if await captcha_img.is_visible():
            await captcha_img.scroll_into_view_if_needed()
            return await captcha_img.screenshot()
        else:
            form_elem = self.page.locator('form, [data-qa="account-login-form"]').first
            if await form_elem.is_visible():
                return await form_elem.screenshot()
            else:
                return await self.page.screenshot()

    async def complete_captcha_flow(self, captcha_text: str) -> dict[str, Any]:
        """Ввод текста капчи и отправка формы."""
        if not self.page:
            return {"status": "ERROR", "message": "Сессия логина не найдена или истекла."}

        try:
            logger.info("Пользователь %d: отправка ответа капчи...", self.user_id)
            captcha_img = self.page.locator(
                '[data-qa="account-captcha-picture"], img[src*="/captcha/picture"], img[data-qa="captcha-image"]'
            ).first
            old_src = await captcha_img.get_attribute("src") if await captcha_img.count() > 0 else None

            captcha_input = self.page.locator(
                '[data-qa="account-captcha-input"], input[name="captchaText"], input[name="captcha"]'
            ).first
            if await captcha_input.count() > 0:
                await captcha_input.fill(captcha_text)
                await asyncio.sleep(0.3)
                await captcha_input.press("Enter")
                await asyncio.sleep(1.5)

            # Проверка: осталась ли капча или обновилась
            captcha_input_after = self.page.locator(
                '[data-qa="account-captcha-input"], input[name="captchaText"], input[name="captcha"]'
            ).first
            captcha_img_after = self.page.locator(
                '[data-qa="account-captcha-picture"], img[src*="/captcha/picture"], img[data-qa="captcha-image"]'
            ).first

            if await captcha_img_after.is_visible() or await captcha_input_after.is_visible():
                logger.warning("Пользователь %d: неверный код капчи или новая картинка.", self.user_id)
                captcha_bytes = await self._get_fresh_captcha_bytes(old_src=old_src)
                return {
                    "status": "INVALID_CAPTCHA",
                    "captcha_bytes": captcha_bytes,
                    "message": "Неверный код с картинки.",
                }

            logger.info("Пользователь %d: капча успешно пройдена! Ожидание СМС-кода...", self.user_id)
            return {"status": "WAITING_FOR_OTP"}

        except Exception as e:
            logger.error("Пользователь %d: ошибка при вводе капчи: %s", self.user_id, type(e).__name__)
            await self.abort()
            return {"status": "ERROR", "message": "Не удалось проверить капчу."}

    async def reload_captcha_flow(self) -> dict[str, Any]:
        """Клик по кнопке Перегенерировать капчу и получение нового скриншота."""
        if not self.page:
            return {"status": "ERROR", "message": "Сессия логина не найдена или истекла."}

        try:
            logger.info("Пользователь %d: клик по кнопке обновления капчи...", self.user_id)
            captcha_img = self.page.locator(
                '[data-qa="account-captcha-picture"], img[src*="/captcha/picture"], img[data-qa="captcha-image"]'
            ).first
            old_src = await captcha_img.get_attribute("src") if await captcha_img.count() > 0 else None

            reload_btn = self.page.locator(
                '[data-qa="account-captcha-reload"], [data-qa="captcha-renew-text"], button:has([data-qa*="reload"])'
            ).first
            if await reload_btn.count() > 0 and await reload_btn.is_visible():
                await human_click(self.page, reload_btn)

            captcha_bytes = await self._get_fresh_captcha_bytes(old_src=old_src)
            return {"status": "WAITING_FOR_CAPTCHA", "captcha_bytes": captcha_bytes}

        except Exception as e:
            logger.error("Пользователь %d: ошибка при обновлении капчи: %s", self.user_id, type(e).__name__)
            return {"status": "ERROR", "message": "Не удалось обновить картинку капчи."}

    async def toggle_captcha_lang_flow(self) -> dict[str, Any]:
        """Клик по кнопке Переключения языка капчи (English / Русский) и получение нового скриншота."""
        if not self.page:
            return {"status": "ERROR", "message": "Сессия логина не найдена или истекла."}

        try:
            logger.info("Пользователь %d: клик по кнопке смены языка капчи...", self.user_id)
            captcha_img = self.page.locator(
                '[data-qa="account-captcha-picture"], img[src*="/captcha/picture"], img[data-qa="captcha-image"]'
            ).first
            old_src = await captcha_img.get_attribute("src") if await captcha_img.count() > 0 else None

            lang_btn = self.page.locator('[data-qa="account-captcha-lang-switch"], [data-qa="captcha-language"]').first
            if await lang_btn.count() > 0 and await lang_btn.is_visible():
                await human_click(self.page, lang_btn)

            captcha_bytes = await self._get_fresh_captcha_bytes(old_src=old_src)
            return {"status": "WAITING_FOR_CAPTCHA", "captcha_bytes": captcha_bytes}

        except Exception as e:
            logger.error("Пользователь %d: ошибка при смене языка капчи: %s", self.user_id, type(e).__name__)
            return {"status": "ERROR", "message": "Не удалось переключить язык капчи."}

    async def complete_login_flow(self, code: str) -> dict[str, Any]:
        """Вторая фаза: человеческий ввод полученного СМС-кода и сохранение сессии."""
        if not self.page:
            return {"status": "ERROR", "message": "Сессия логина не найдена или истекла."}

        try:
            self.otp_code = code.strip()
            logger.info("Пользователь %d: ввод полученного СМС-кода...", self.user_id)

            otp_target = self.page.locator(
                '[data-qa="otp-code-input"], input[name="code"], input[autocomplete="one-time-code"]'
            )
            otp_form_input = self.page.locator(
                '[data-qa*="otp"] input, input[autocomplete="one-time-code"], '
                'form input[name*="code" i], form input[type="number"]'
            ).first
            try:
                await otp_form_input.wait_for(state="visible", timeout=10_000)
            except Exception as exc:
                raise HumanizationError("otp", "target_not_ready") from exc
            cells = await self._otp_cells(self.otp_code)
            try:
                if cells:
                    await human_type_digits(self.page, cells, self.otp_code)
                elif await otp_target.count() == 1:
                    await human_type_digits(self.page, otp_target.first, self.otp_code)
                else:
                    raise HumanizationError("otp", "ambiguous_fields")
            except HumanizationError:
                # The last character can trigger a navigation that detaches the
                # input. It is successful only with a positive auth marker.
                if not await self._is_authenticated(timeout=2_000):
                    raise

            if not await self._is_authenticated(timeout=1_500):
                confirm_btn = self.page.locator('[data-qa="otp-code-submit"], button[type="submit"]').first
                if await confirm_btn.count() == 0:
                    raise HumanizationError("otp", "confirm_not_ready")
                await human_click(self.page, confirm_btn)

            if await self._is_authenticated(timeout=5_000):
                return await self._finish_authenticated_login()

            logger.warning("Пользователь %d: неверный СМС-код или ошибка подтверждения.", self.user_id)
            await self.abort()
            return {"status": "INVALID_CODE", "message": "Неверный СМС-код. Попробуйте еще раз через меню авторизации."}

        except Exception as e:
            logger.error("Пользователь %d: ошибка при вводе СМС-кода: %s", self.user_id, type(e).__name__)
            await self.abort()
            return {"status": "ERROR", "message": "Не удалось подтвердить код hh.ru."}

    async def _otp_cells(self, code: str) -> list:
        """Return a complete, visible set of one-character OTP inputs only."""
        if not self.page:
            return []
        candidates = self.page.locator(
            '[data-qa*="otp"] input, input[autocomplete="one-time-code"], '
            'form input[name*="code" i], form input[type="number"]'
        )
        cells = []
        for index in range(await candidates.count()):
            candidate = candidates.nth(index)
            if not await candidate.is_visible():
                continue
            if await candidate.get_attribute("maxlength") == "1":
                cells.append(candidate)
        return cells if len(cells) == len(code) else []

    async def _is_authenticated(self, *, timeout: int) -> bool:
        if not self.page:
            return False
        authenticated = self.page.locator(
            '[data-qa="mainmenu_myResumes"], [data-qa="mainmenu_vacancyResponses"], a[href*="/applicant/resumes"]'
        ).first
        try:
            await authenticated.wait_for(state="visible", timeout=timeout)
            return "account/login" not in self.page.url
        except Exception:
            return False

    async def _finish_authenticated_login(self) -> dict[str, Any]:
        """Persist a positively verified hh.ru session exactly once."""
        if not self.context:
            return {"status": "ERROR", "message": "Контекст браузера уже закрыт."}
        logger.info("Пользователь %d: успешная авторизация на hh.ru!", self.user_id)
        storage_state = await self.context.storage_state()
        encrypted_state = self.security_factory().encrypt_storage_state(storage_state)
        if self.account_id:
            updated = await self.db.update_account_session(
                self.user_id,
                self.account_id,
                encrypted_state,
                status="ACTIVE",
            )
            if not updated:
                await self.cleanup()
                return {"status": "ERROR", "message": "Аккаунт для сохранения сессии не найден."}
        else:
            await self.db.update_user_session(self.user_id, encrypted_state, status="ACTIVE")
        await self.cleanup()
        return {"status": "SUCCESS"}

    async def abort(self) -> None:
        """Close browser resources and remove an unfinished account row."""
        await self.cleanup()
        if self.account_id:
            account = await self.db.get_account_for_user(self.user_id, self.account_id)
            if account and account.get("session_status") == "AUTH_PENDING":
                await self.db.delete_hh_account_for_user(self.user_id, self.account_id)

    async def cleanup(self):
        """Close each owned resource, even if closing another one fails."""
        self.is_done = True
        context, engine = self.context, self.engine
        self.context = self.engine = self.page = None
        errors = []
        for resource in (context, engine):
            if resource is not None:
                try:
                    await resource.close()
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise ExceptionGroup("Login session cleanup failures", errors)


class HHLoginManager:
    """Менеджер сессий входа, принадлежащий одному AppContext."""

    def __init__(self, *, db, engine_factory, security_factory, locks, stop_account):
        self.db = db
        self.engine_factory = engine_factory
        self.security_factory = security_factory
        self.locks = locks
        self.stop_account = stop_account
        self.session_factory = HHLoginSession
        self._sessions: dict[int, HHLoginSession] = {}
        self._cleanup_tasks: dict[int, asyncio.Task] = {}

    async def _auto_cleanup_session(self, user_id: int, session: HHLoginSession, timeout: float = 600.0) -> None:
        """Автоматическое закрытие брошенной сессии авторизации по таймауту (10 мин)."""
        await asyncio.sleep(timeout)
        async with self.locks.login_locks[user_id]:
            current = self._sessions.get(user_id)
            if current is session and not session.is_done:
                logger.info("Closing inactive login session for user %d", user_id)
                await session.abort()
                self._sessions.pop(user_id, None)
            if self._cleanup_tasks.get(user_id) is asyncio.current_task():
                self._cleanup_tasks.pop(user_id, None)

    @serialize_login
    async def start_login(self, user_id: int, phone_or_email: str, account_id: int | None = None) -> dict[str, Any]:
        if account_id is not None:
            # A fresh login must not race a worker persisting its older cookies.
            await self.stop_account(user_id, account_id)
        old_timer = self._cleanup_tasks.pop(user_id, None)
        if old_timer:
            old_timer.cancel()
            await asyncio.gather(old_timer, return_exceptions=True)
        if user_id in self._sessions:
            previous = self._sessions[user_id]
            if previous.account_id == account_id:
                await previous.cleanup()
            else:
                await previous.abort()

        session = self.session_factory(
            user_id,
            phone_or_email,
            account_id=account_id,
            db=self.db,
            engine_factory=self.engine_factory,
            security_factory=self.security_factory,
        )
        self._sessions[user_id] = session

        # Запуск таски автоочистки через 10 минут
        self._cleanup_tasks[user_id] = asyncio.create_task(self._auto_cleanup_session(user_id, session, timeout=600.0))

        result = await session.start_login_flow()
        if result.get("status") == "ERROR" and self._sessions.get(user_id) is session:
            self._sessions.pop(user_id, None)
            timer = self._cleanup_tasks.pop(user_id, None)
            if timer:
                timer.cancel()
                await asyncio.gather(timer, return_exceptions=True)
            if not session.is_done:
                await session.abort()
        return result

    @serialize_login
    async def reload_captcha(self, user_id: int) -> dict[str, Any]:
        session = self._sessions.get(user_id)
        if not session or session.is_done:
            return {"status": "ERROR", "message": "Сессия входа не найдена. Начните процесс авторизации заново."}
        return await session.reload_captcha_flow()

    @serialize_login
    async def toggle_captcha_lang(self, user_id: int) -> dict[str, Any]:
        session = self._sessions.get(user_id)
        if not session or session.is_done:
            return {"status": "ERROR", "message": "Сессия входа не найдена. Начните процесс авторизации заново."}
        return await session.toggle_captcha_lang_flow()

    @serialize_login
    async def submit_captcha(self, user_id: int, code: str) -> dict[str, Any]:
        session = self._sessions.get(user_id)
        if not session or session.is_done:
            return {"status": "ERROR", "message": "Сессия входа не найдена. Начните процесс авторизации заново."}

        return await session.complete_captcha_flow(code)

    @serialize_login
    async def submit_otp(self, user_id: int, code: str) -> dict[str, Any]:
        session = self._sessions.get(user_id)
        if not session or session.is_done:
            return {"status": "ERROR", "message": "Сессия входа не найдена. Начните процесс авторизации заново."}

        res = await session.complete_login_flow(code)
        self._sessions.pop(user_id, None)
        timer = self._cleanup_tasks.pop(user_id, None)
        if timer:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        return res

    @serialize_login
    async def cancel(self, user_id: int) -> None:
        timer = self._cleanup_tasks.pop(user_id, None)
        if timer:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        session = self._sessions.pop(user_id, None)
        if session:
            await session.abort()

    async def shutdown(self) -> None:
        timers = list(self._cleanup_tasks.values())
        self._cleanup_tasks.clear()
        for timer in timers:
            timer.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)
        sessions = list(self._sessions.values())
        self._sessions.clear()
        results = await asyncio.gather(*(session.abort() for session in sessions), return_exceptions=True)
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("Login manager cleanup failures", errors)


__all__ = ["HHLoginManager", "HHLoginSession"]
