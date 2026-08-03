"""
LeadScout AI — Модуль интерактивной OTP-авторизации hh.ru (Patchright Stealth).
Управляет процессами входа через СМС/email для пользователей Telegram.
"""

import asyncio
import time
import logging
from typing import Dict, Any
from patchright.async_api import Page, BrowserContext

from parsers.hh_browser import HHBrowserEngine
from utils.humanization import human_type, human_click, human_type_digits
from utils.security import SessionSecurityManager
from database import update_user_session

logger = logging.getLogger(__name__)


class HHLoginSession:
    """Сессия авторизации hh.ru для конкретного пользователя и аккаунта."""

    def __init__(self, user_id: int, phone_or_email: str, account_id: int | None = None):
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
        self.engine = HHBrowserEngine()
        await self.engine.start()
        self.context = await self.engine.create_context()
        self.page = await self.context.new_page()

        try:
            logger.info("Пользователь %d: переход на страницу логина hh.ru...", self.user_id)
            await self.page.goto("https://hh.ru/account/login", wait_until="domcontentloaded")
            await asyncio.sleep(2.0)

            # 1. Выбор типа аккаунта (Соискатель), если на первом шаге показана плашка выбора
            applicant_card = self.page.locator('[data-qa="account-type-card-APPLICANT"]').first
            if await applicant_card.count() > 0:
                await human_click(self.page, applicant_card)
                await asyncio.sleep(0.5)

            # Клик по кнопке продолжить на шаге выбора роли
            step1_submit = self.page.locator('[data-qa="submit-button"], button[type="submit"], [data-qa="account-signup-submit"]').first
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
                digits = re.sub(r'\D', '', login_str)
                if len(digits) == 11 and digits.startswith(('7', '8')):
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
                await self.cleanup()
                return {"status": "ERROR", "message": "Не найдено поле ввода логина. Проверьте адрес входа hh.ru."}

            await human_type(self.page, login_input, text_to_type)
            await asyncio.sleep(0.5)

            # 4. Нажатие кнопки продолжить / запросить код
            submit_btn = self.page.locator('[data-qa="submit-button"], [data-qa="account-signup-submit"], button[type="submit"]').first
            if await submit_btn.count() > 0 and await submit_btn.is_visible():
                await human_click(self.page, submit_btn)
                await asyncio.sleep(2.5)

            # 5. Проверка появления капчи (картинки с кодом)
            await asyncio.sleep(1.0)
            captcha_img = self.page.locator('[data-qa="account-captcha-picture"], img[src*="/captcha/picture"], img[data-qa="captcha-image"]').first
            captcha_input = self.page.locator('[data-qa="account-captcha-input"], input[name="captchaText"], input[name="captcha"]').first

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
            logger.error("Пользователь %d: ошибка 1 фазы логина: %s", self.user_id, e)
            await self.cleanup()
            return {"status": "ERROR", "message": str(e)}

    async def _get_fresh_captcha_bytes(self, old_src: str | None = None) -> bytes:
        """Ожидает загрузки нового URL картинки капчи и снимает точный скриншот."""
        captcha_img = self.page.locator('[data-qa="account-captcha-picture"], img[src*="/captcha/picture"], img[data-qa="captcha-image"]').first

        if old_src and await captcha_img.count() > 0:
            try:
                await self.page.wait_for_function(
                    "old => { const img = document.querySelector('[data-qa=\"account-captcha-picture\"], img[src*=\"/captcha/picture\"], img[data-qa=\"captcha-image\"]'); return img && img.getAttribute('src') !== old; }",
                    arg=old_src,
                    timeout=4000
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
            logger.info("Пользователь %d: ввод капчи '%s'...", self.user_id, captcha_text)
            captcha_img = self.page.locator('[data-qa="account-captcha-picture"], img[src*="/captcha/picture"], img[data-qa="captcha-image"]').first
            old_src = await captcha_img.get_attribute("src") if await captcha_img.count() > 0 else None

            captcha_input = self.page.locator('[data-qa="account-captcha-input"], input[name="captchaText"], input[name="captcha"]').first
            if await captcha_input.count() > 0:
                await captcha_input.fill(captcha_text)
                await asyncio.sleep(0.3)
                await captcha_input.press("Enter")
                await asyncio.sleep(1.5)

            # Проверка: осталась ли капча или обновилась
            captcha_input_after = self.page.locator('[data-qa="account-captcha-input"], input[name="captchaText"], input[name="captcha"]').first
            captcha_img_after = self.page.locator('[data-qa="account-captcha-picture"], img[src*="/captcha/picture"], img[data-qa="captcha-image"]').first

            if await captcha_img_after.is_visible() or await captcha_input_after.is_visible():
                logger.warning("Пользователь %d: неверный код капчи или новая картинка.", self.user_id)
                captcha_bytes = await self._get_fresh_captcha_bytes(old_src=old_src)
                return {"status": "INVALID_CAPTCHA", "captcha_bytes": captcha_bytes, "message": "Неверный код с картинки."}

            logger.info("Пользователь %d: капча успешно пройдена! Ожидание СМС-кода...", self.user_id)
            return {"status": "WAITING_FOR_OTP"}

        except Exception as e:
            logger.error("Пользователь %d: ошибка при вводе капчи: %s", self.user_id, e)
            await self.cleanup()
            return {"status": "ERROR", "message": str(e)}

    async def reload_captcha_flow(self) -> dict[str, Any]:
        """Клик по кнопке Перегенерировать капчу и получение нового скриншота."""
        if not self.page:
            return {"status": "ERROR", "message": "Сессия логина не найдена или истекла."}

        try:
            logger.info("Пользователь %d: клик по кнопке обновления капчи...", self.user_id)
            captcha_img = self.page.locator('[data-qa="account-captcha-picture"], img[src*="/captcha/picture"], img[data-qa="captcha-image"]').first
            old_src = await captcha_img.get_attribute("src") if await captcha_img.count() > 0 else None

            reload_btn = self.page.locator('[data-qa="account-captcha-reload"], [data-qa="captcha-renew-text"], button:has([data-qa*="reload"])').first
            if await reload_btn.count() > 0 and await reload_btn.is_visible():
                await human_click(self.page, reload_btn)

            captcha_bytes = await self._get_fresh_captcha_bytes(old_src=old_src)
            return {"status": "WAITING_FOR_CAPTCHA", "captcha_bytes": captcha_bytes}

        except Exception as e:
            logger.error("Пользователь %d: ошибка при обновлении капчи: %s", self.user_id, e)
            return {"status": "ERROR", "message": str(e)}

    async def toggle_captcha_lang_flow(self) -> dict[str, Any]:
        """Клик по кнопке Переключения языка капчи (English / Русский) и получение нового скриншота."""
        if not self.page:
            return {"status": "ERROR", "message": "Сессия логина не найдена или истекла."}

        try:
            logger.info("Пользователь %d: клик по кнопке смены языка капчи...", self.user_id)
            captcha_img = self.page.locator('[data-qa="account-captcha-picture"], img[src*="/captcha/picture"], img[data-qa="captcha-image"]').first
            old_src = await captcha_img.get_attribute("src") if await captcha_img.count() > 0 else None

            lang_btn = self.page.locator('[data-qa="account-captcha-lang-switch"], [data-qa="captcha-language"]').first
            if await lang_btn.count() > 0 and await lang_btn.is_visible():
                await human_click(self.page, lang_btn)

            captcha_bytes = await self._get_fresh_captcha_bytes(old_src=old_src)
            return {"status": "WAITING_FOR_CAPTCHA", "captcha_bytes": captcha_bytes}

        except Exception as e:
            logger.error("Пользователь %d: ошибка при смене языка капчи: %s", self.user_id, e)
            return {"status": "ERROR", "message": str(e)}

    async def complete_login_flow(self, code: str) -> dict[str, Any]:
        """Вторая фаза: человеческий ввод полученного СМС-кода и сохранение сессии."""
        if not self.page:
            return {"status": "ERROR", "message": "Сессия логина не найдена или истекла."}

        try:
            self.otp_code = code.strip()
            logger.info("Пользователь %d: посимвольный ввод СМС-кода '%s'...", self.user_id, self.otp_code)

            # Поиск поля ввода OTP кода
            otp_input = self.page.locator('[data-qa="otp-code-input"], input[name="code"], input[autocomplete="one-time-code"]').first
            if await otp_input.count() > 0:
                await human_type_digits(self.page, otp_input, self.otp_code)
                await asyncio.sleep(0.5)
            else:
                # Посимвольный ввод в ячейки формы авторизации если несколько полей
                inputs = await self.page.locator('form input[type="text"], form input[type="number"], [data-qa*="otp"] input').all()
                if len(inputs) >= len(self.otp_code):
                    for idx, digit in enumerate(self.otp_code):
                        await human_type_digits(self.page, inputs[idx], digit)
                else:
                    await human_type_digits(self.page, self.page.keyboard, self.otp_code)

            # Плавный клик подтверждения если есть кнопка
            confirm_btn = self.page.locator('[data-qa="otp-code-submit"], button[type="submit"]').first
            if await confirm_btn.is_visible():
                await human_click(self.page, confirm_btn)

            await asyncio.sleep(3.0)

            # Проверка успеха авторизации
            if "account/login" not in self.page.url:
                logger.info("Пользователь %d: успешная авторизация на hh.ru!", self.user_id)
                storage_state = await self.context.storage_state()
                
                sec_mgr = SessionSecurityManager()
                encrypted_state = sec_mgr.encrypt_storage_state(storage_state)
                
                from database import update_account_session
                if self.account_id:
                    await update_account_session(self.account_id, encrypted_state, status="ACTIVE")
                else:
                    await update_user_session(self.user_id, encrypted_state, status="ACTIVE")
                await self.cleanup()
                return {"status": "SUCCESS"}
            else:
                logger.warning("Пользователь %d: неверный СМС-код или ошибка подтверждения.", self.user_id)
                await self.cleanup()
                return {"status": "INVALID_CODE", "message": "Неверный СМС-код. Попробуйте еще раз через меню авторизации."}

        except Exception as e:
            logger.error("Пользователь %d: ошибка при вводе СМС-кода: %s", self.user_id, e)
            await self.cleanup()
            return {"status": "ERROR", "message": str(e)}

    async def cleanup(self):
        """Очистка ресурсов браузера."""
        self.is_done = True
        try:
            if self.context:
                await self.context.close()
            if self.engine:
                await self.engine.close()
        except Exception as e:
            logger.debug("Ошибка при закрытии логин-сессии: %s", e)


class HHLoginManager:
    """Глобальный менеджер активных сессий входа."""

    _sessions: dict[int, HHLoginSession] = {}

    @classmethod
    async def _auto_cleanup_session(cls, user_id: int, timeout: float = 600.0) -> None:
        """Автоматическое закрытие брошенной сессии авторизации по таймауту (10 мин)."""
        await asyncio.sleep(timeout)
        session = cls._sessions.get(user_id)
        if session and not session.is_done:
            logger.info("Сессия входа пользователя %d не активна %d сек. Автоматическое освобождение ресурсов Chrome...", user_id, int(timeout))
            await session.cleanup()
            cls._sessions.pop(user_id, None)

    @classmethod
    async def start_login(cls, user_id: int, phone_or_email: str, account_id: int | None = None) -> dict[str, Any]:
        if user_id in cls._sessions:
            await cls._sessions[user_id].cleanup()
        
        session = HHLoginSession(user_id, phone_or_email, account_id=account_id)
        cls._sessions[user_id] = session

        # Запуск таски автоочистки через 10 минут
        asyncio.create_task(cls._auto_cleanup_session(user_id, timeout=600.0))

        return await session.start_login_flow()

    @classmethod
    async def reload_captcha(cls, user_id: int) -> dict[str, Any]:
        session = cls._sessions.get(user_id)
        if not session or session.is_done:
            return {"status": "ERROR", "message": "Сессия входа не найдена. Начните процесс авторизации заново."}
        return await session.reload_captcha_flow()

    @classmethod
    async def toggle_captcha_lang(cls, user_id: int) -> dict[str, Any]:
        session = cls._sessions.get(user_id)
        if not session or session.is_done:
            return {"status": "ERROR", "message": "Сессия входа не найдена. Начните процесс авторизации заново."}
        return await session.toggle_captcha_lang_flow()

    @classmethod
    async def submit_captcha(cls, user_id: int, code: str) -> dict[str, Any]:
        session = cls._sessions.get(user_id)
        if not session or session.is_done:
            return {"status": "ERROR", "message": "Сессия входа не найдена. Начните процесс авторизации заново."}
        
        return await session.complete_captcha_flow(code)

    @classmethod
    async def submit_otp(cls, user_id: int, code: str) -> dict[str, Any]:
        session = cls._sessions.get(user_id)
        if not session or session.is_done:
            return {"status": "ERROR", "message": "Сессия входа не найдена. Начните процесс авторизации заново."}
        
        res = await session.complete_login_flow(code)
        cls._sessions.pop(user_id, None)
        return res
