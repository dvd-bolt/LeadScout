"""Safe PDF parsing and hh.ru resume synchronization."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import date
from typing import Any

from patchright.async_api import Locator, Page
from patchright.async_api import TimeoutError as PatchrightTimeoutError

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
from .captcha import extract_captcha_data_uri

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


async def _page_gate(page: Page) -> dict | None:
    """Detect authentication and human-verification UI regardless of URL."""
    gate = await page.evaluate(
        r"""() => {
            const text = (document.body?.innerText || '').replace(/\s+/g, ' ').trim();
            const visible = (selector) => [...document.querySelectorAll(selector)].some((node) => {
                const style = getComputedStyle(node);
                return style.display !== 'none' && style.visibility !== 'hidden' && node.getClientRects().length;
            });
            if (visible('[data-qa*="captcha" i], iframe[src*="captcha" i]') ||
                /(?:captcha|пройдите (?:проверку|капчу)|подтвердите,? что вы не робот)/i.test(text)) {
                return {code: 'CAPTCHA_REQUIRED', message: 'hh.ru запросил проверку, что вы не робот.'};
            }
            if (visible('input[type="password"], [data-qa="login-input-username"], [data-qa*="login-form" i]') ||
                /(?:войти в аккаунт|вход на hh\.ru)/i.test(text)) {
                return {code: 'LOGIN_REQUIRED', message: 'Сессия hh.ru истекла. Войдите заново.'};
            }
            if (visible('[data-qa*="verification-code" i], input[autocomplete="one-time-code"]') ||
                /(?:подтвердите (?:номер|телефон|почту|email)|код подтверждения)/i.test(text)) {
                return {code: 'CONTACT_CONFIRMATION_REQUIRED', message: 'hh.ru требует подтверждение контакта.'};
            }
            return null;
        }"""
    )
    return dict(gate) if gate else None


async def _wait_for_resume_page_signal(page: Page, timeout: int = DEFAULT_TRANSITION_TIMEOUT_MS) -> bool:
    """Wait for either a supported wizard control or an authentication gate.

    hh.ru hydrates the resume wizard after the initial document is committed.  A
    ``domcontentloaded`` navigation wait can therefore hang even though the
    useful UI is already available (or a login/captcha page was rendered).
    """
    try:
        await page.wait_for_function(
            r"""() => {
                const text = (document.body?.innerText || '').replace(/\s+/g, ' ');
                const selectors = [
                    '[data-qa="resume-profile-position-input"]',
                    '[data-qa="professional-role-search-input"]',
                    '[data-qa="resume-title-input"]',
                    'input[placeholder*="профессию"]',
                    '[data-qa="resume-person-first-name"]',
                    'input[name*="firstName"]',
                    'input[type="tel"]',
                    'input[type="email"]',
                    '[data-qa*="salary" i] input',
                    '[data-qa^="resume-profile-experience-specific-company-input"]',
                    'input[name="company"]',
                    '[data-qa*="education-institution" i]',
                    'textarea[name="institution"]',
                    '[data-qa*="language" i] input',
                    '[data-qa*="skill" i] input',
                    'input[placeholder*="навык"]',
                    '[data-qa="resume-about-me-input"]',
                    'textarea[name="about"]',
                    'input[type="url"]',
                    '[data-qa="resume-publish"]',
                    '[data-qa="resume-save"]',
                    '[data-qa*="captcha" i]',
                    '[data-qa*="login-form" i]',
                    'input[type="password"]',
                    'input[autocomplete="one-time-code"]'
                ];
                return selectors.some((selector) => document.querySelector(selector)) ||
                    /(?:Укажу профессию|Добавить (?:место работы|опыт|образование|учебное заведение)|уровень владения|оцените навык|войти в аккаунт|вход на hh\.ru|captcha|капч|не робот|код подтверждения)/i.test(text);
            }""",
            timeout=timeout,
        )
        return True
    except PatchrightTimeoutError:
        return False


def _safe_page_path(page: Page) -> str:
    """Return a diagnostic hh path without query data or user content."""
    match = re.match(r"https?://[^/]+(?P<path>/[^?#]*)", page.url or "")
    return match.group("path") if match else ""


def _resume_id_from_url(url: str, *, allow_edit: bool = False) -> str:
    suffix = r"(?:/edit)?" if allow_edit else ""
    match = re.match(
        rf"^https?://[^/]+/resume/([A-Za-z0-9_-]{{6,64}}){suffix}/?(?:[?#].*)?$",
        url or "",
    )
    return match.group(1) if match else ""


async def _read_resume_profile_fields(page: Page) -> dict[str, str]:
    """Read displayed profile values; keep hh's numeric area ID separate."""
    profile = await page.evaluate(
        r"""() => {
            const value = (...selectors) => {
                for (const selector of selectors) {
                    const node = document.querySelector(selector);
                    if (node && 'value' in node && String(node.value || '').trim()) return String(node.value).trim();
                }
                return '';
            };
            const displayedValue = (...selectors) => {
                const meaningful = (raw) => {
                    const candidate = String(raw || '').replace(/\s+/g, ' ').trim();
                    return candidate && !/^\d+$/.test(candidate) &&
                        !/^(?:город|регион|место проживания)$/i.test(candidate) ? candidate : '';
                };
                for (const selector of selectors) {
                    for (const node of document.querySelectorAll(selector)) {
                        const style = getComputedStyle(node);
                        const visible = style.display !== 'none' && style.visibility !== 'hidden' && node.getClientRects().length;
                        if (!visible) continue;
                        if (node instanceof HTMLSelectElement) {
                            const label = meaningful(node.selectedOptions[0]?.textContent);
                            if (label) return label;
                        }
                        const ariaValue = node.getAttribute('aria-valuetext') || node.getAttribute('aria-label');
                        const ariaLabel = meaningful(ariaValue);
                        if (ariaLabel) return ariaLabel;
                        if ('value' in node) {
                            const fieldValue = meaningful(node.value);
                            if (fieldValue) return fieldValue;
                        }
                        const textValue = meaningful(node.textContent);
                        if (textValue) return textValue;
                    }
                }
                return '';
            };
            return {
                first_name: value('[name="firstName"]', '[data-qa*="first-name" i]'),
                last_name: value('[name="lastName"]', '[data-qa*="last-name" i]'),
                birth_date: value('[name="birthday"]', 'input[type="date"]'),
                city: displayedValue(
                    '[data-qa*="area" i] [data-qa*="selected" i]',
                    '[data-qa*="area" i] [role="option"][aria-selected="true"]',
                    '[data-qa*="area" i] input:not([type="hidden"])',
                    'input[name="area"]:not([type="hidden"])',
                    'select[name="area"]'
                ),
                city_id: value('input[name="area"][type="hidden"]', 'input[name="area"]'),
                phone: value('input[type="tel"]', '[name="phone"]'),
                email: value('input[type="email"]', '[name="email"]'),
            };
        }"""
    )
    return {key: str(value or "").strip() for key, value in dict(profile).items()}


def _profile_fingerprint(profile: dict) -> str:
    value = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _missing_external_sections(data: dict, external_text: str) -> list[str]:
    """Conservatively verify user-selected content against the rendered hh resume."""
    haystack = " ".join(external_text.casefold().split())
    missing: list[str] = []

    def contains(value: str) -> bool:
        needle = " ".join(str(value or "").casefold().split())
        if needle.isdigit():
            return needle in "".join(character for character in haystack if character.isdigit())
        return not needle or needle in haystack

    experiences = [item for item in data.get("experiences") or [] if item.get("selected", True)]
    if experiences and any(
        not contains(item.get("company", ""))
        or not contains(item.get("position", ""))
        or not contains(item.get("description", ""))
        for item in experiences
    ):
        missing.append("Опыт")
    education = [item for item in data.get("education") or [] if item.get("selected", True)]
    if education and any(
        not contains(item.get("institution", ""))
        or not contains(item.get("specialization", ""))
        or not contains(item.get("end_year", ""))
        for item in education
    ):
        missing.append("Образование")
    skills = [item.get("name", "") for item in data.get("skills") or [] if item.get("name")]
    if skills and any(not contains(skill) for skill in skills):
        missing.append("Навыки")
    about = str((data.get("about") or {}).get("text") or "")
    if about and not contains(about):
        missing.append("О себе")
    languages = [item.get("name", "") for item in data.get("languages") or [] if item.get("name")]
    if languages and any(not contains(language) for language in languages):
        missing.append("Языки")
    links = [item.get("url", "") for item in (data.get("about") or {}).get("links") or [] if item.get("url")]
    if links and any(not contains(link) for link in links):
        missing.append("Ссылки")
    additional = data.get("additional") or {}
    extra_names = [
        item.get("name", "")
        for key in ("courses", "exams", "certificates", "recommendations")
        for item in additional.get(key) or []
        if item.get("name")
    ]
    if extra_names and any(not contains(name) for name in extra_names):
        missing.append("Дополнительные разделы")
    conditions = data.get("work_conditions") or {}
    condition_values = [
        str(conditions.get("salary") or ""),
        *(conditions.get("employment_types") or []),
        *(conditions.get("schedules") or []),
        *(conditions.get("work_formats") or []),
        str(conditions.get("relocation") or ""),
        str(conditions.get("business_trips") or ""),
    ]
    if any(value and not contains(value) for value in condition_values):
        missing.append("Условия работы")
    return missing


def _profile_matches_draft(data: dict, profile: dict) -> bool:
    personal = data.get("personal") or {}
    contacts = data.get("contacts") or {}
    checks = (
        (personal.get("first_name"), profile.get("first_name")),
        (personal.get("last_name"), profile.get("last_name")),
        (personal.get("birth_date"), profile.get("birth_date")),
        (personal.get("city"), profile.get("city")),
        (contacts.get("phone"), profile.get("phone")),
        (contacts.get("email"), profile.get("email")),
    )
    return all(
        not expected
        or (
            actual
            and str(expected).strip().casefold() == str(actual).strip().casefold()
        )
        for expected, actual in checks
    )


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
            gate = await _page_gate(page)
            if gate and gate["code"] == "LOGIN_REQUIRED":
                await self.db.update_account_session(user_id, account_id, b"", "EXPIRED")
                return {"status": "ERROR", **gate}
            if gate:
                return {"status": "NEEDS_ACTION", **gate, "required_action": gate["code"]}
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
                        const cardText = (element.closest('[data-qa*="resume" i], article')?.innerText || '').toLowerCase();
                        const status = /модерац/.test(cardText) ? 'На модерации'
                            : /черновик|не опубликован/.test(cardText) ? 'Черновик'
                            : 'Опубликовано';
                        seen.add(match[1]);
                        result.push({id: match[1], title: title || 'Резюме', href, status});
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
                        "status": raw.get("status") or "Опубликовано",
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
        return {
            "status": "CLIENT_UPDATE_REQUIRED",
            "code": "CLIENT_UPDATE_REQUIRED",
            "message": "Прямой импорт PDF отключён. Используйте сохраняемый мастер резюме.",
        }

    @serialize_account
    async def inspect_resume_profile(self, user_id: int, account_id: int) -> dict[str, Any]:
        account, storage_state = await self._account_and_state(user_id, account_id)
        if not account:
            return {"status": "NEEDS_ACTION", "code": "LOGIN_REQUIRED", "message": "Войдите в hh.ru."}
        engine = await self.browser_pool.get_engine(account.get("proxy_url") or None)
        context = None
        try:
            context = await engine.create_context(storage_state=storage_state)
            page = await context.new_page()
            await page.goto("https://hh.ru/applicant/profile/me", wait_until="domcontentloaded", timeout=30_000)
            gate = await _page_gate(page)
            if gate:
                if gate["code"] == "CAPTCHA_REQUIRED":
                    captcha_uri = await extract_captcha_data_uri(page)
                    await self.db.set_account_pending_captcha(
                        user_id, account_id, captcha_uri or "", page.url
                    )
                return {"status": "NEEDS_ACTION", **gate, "required_action": gate["code"]}
            profile = await _read_resume_profile_fields(page)
            return {
                "status": "SUCCESS",
                "profile": profile,
                "profile_fingerprint": _profile_fingerprint(profile),
                "capabilities": {"max_skills": 100, "supports_custom_skills": True},
            }
        except Exception as exc:
            logger.error("Resume profile inspection failed: %s", type(exc).__name__)
            return {
                "status": "ERROR",
                "code": "PROFILE_READ_FAILED",
                "stage": "PREFLIGHT",
                "message": "Не удалось прочитать профиль hh.ru. Черновик сохранён.",
                "retryable": True,
            }
        finally:
            if context:
                await self._persist_context(user_id, account_id, context)
                await context.close()

    @serialize_account
    async def publish_resume_draft(
        self,
        user_id: int,
        account_id: int,
        data: dict,
        *,
        attempt: dict,
        on_external_saved,
    ) -> dict[str, Any]:
        existing_id = str(attempt.get("hh_resume_id") or "")
        before = await self.fetch_user_resumes(user_id, account_id)
        if before.get("status") != "SUCCESS":
            return {**before, "stage": "OPEN"}
        before_ids = {item["hh_resume_id"] for item in await self.db.list_resume_snapshots(user_id, account_id)}
        account, storage_state = await self._account_and_state(user_id, account_id)
        if not account:
            return {"status": "NEEDS_ACTION", "code": "LOGIN_REQUIRED", "stage": "OPEN", "message": "Войдите в hh.ru."}
        structured = StructuredResume.model_validate(
            {
                "first_name": (data.get("personal") or {}).get("first_name", ""),
                "last_name": (data.get("personal") or {}).get("last_name", ""),
                "middle_name": (data.get("personal") or {}).get("middle_name", ""),
                "birth_date": (data.get("personal") or {}).get("birth_date", ""),
                "title": (data.get("profession") or {}).get("title", ""),
                "salary": (data.get("work_conditions") or {}).get("salary"),
                "city": (data.get("personal") or {}).get("city", ""),
                "experiences": [item for item in data.get("experiences") or [] if item.get("selected", True)],
                "education": [item for item in data.get("education") or [] if item.get("selected", True)],
                "skills": [item.get("name", "") for item in data.get("skills") or [] if item.get("name")],
                "about": (data.get("about") or {}).get("text", ""),
            }
        )
        context = None
        recognized_screens: list[str] = []
        try:
            engine = await self.browser_pool.get_engine(account.get("proxy_url") or None)
            context = await engine.create_context(storage_state=storage_state)
            page = await context.new_page()
            start_url = (
                f"https://hh.ru/resume/{existing_id}/edit"
                if existing_id
                else "https://hh.ru/profile/resume/professional_role"
            )
            result = await self._fill_step_by_step_resume(
                page, structured, draft_data=data, start_url=start_url
            )
            recognized_screens = list(result.get("recognized_screens") or [])
            if result.get("code") == "CAPTCHA_REQUIRED":
                captcha_uri = await extract_captcha_data_uri(page)
                await self.db.set_account_pending_captcha(
                    user_id, account_id, captcha_uri or "", page.url
                )
            if result.get("status") != "SUBMITTED":
                return result
            saved_resume_id = _resume_id_from_url(page.url, allow_edit=True)
            if saved_resume_id:
                await on_external_saved(saved_resume_id, page.url.split("?", 1)[0])
        except Exception as exc:
            logger.error("Resume publication failed: %s", type(exc).__name__)
            return {
                "status": "UNCERTAIN",
                "code": "BROWSER_INTERRUPTED",
                "stage": "VERIFY",
                "message": "Связь с hh.ru прервалась. Перед повтором нужна сверка результата.",
                "retryable": True,
            }
        finally:
            if context:
                await self._persist_context(user_id, account_id, context)
                await context.close()

        after = await self.fetch_user_resumes(user_id, account_id)
        if after.get("status") != "SUCCESS":
            return {**after, "status": "UNCERTAIN", "code": "VERIFY_FAILED", "stage": "VERIFY"}
        snapshots = await self.db.list_resume_snapshots(user_id, account_id)
        created = (
            [item for item in snapshots if item["hh_resume_id"] == existing_id]
            if existing_id
            else [item for item in snapshots if item["hh_resume_id"] not in before_ids]
        )
        if len(created) != 1:
            return {
                "status": "UNCERTAIN",
                "code": "EXTERNAL_RESULT_AMBIGUOUS",
                "stage": "VERIFY",
                "message": "Не удалось однозначно подтвердить созданное резюме. Новое создание заблокировано до сверки.",
                "retryable": True,
            }
        item = created[0]
        await on_external_saved(item["hh_resume_id"], item["href"])
        missing_sections = _missing_external_sections(data, str(item.get("extracted_text") or ""))
        profile_check = await self.inspect_resume_profile(user_id, account_id)
        if profile_check.get("status") != "SUCCESS" or not _profile_matches_draft(
            data, dict(profile_check.get("profile") or {})
        ):
            missing_sections.append("Профиль и контакты")
        missing_sections = list(dict.fromkeys(missing_sections))
        if missing_sections:
            return {
                "status": "PARTIAL",
                "code": "SECTIONS_NOT_VERIFIED",
                "stage": "VERIFY",
                "message": f"Резюме создано. Не завершён перенос раздела «{missing_sections[0]}».",
                "missing_sections": missing_sections,
                "hh_resume_id": item["hh_resume_id"],
                "hh_resume_url": item["href"],
                "hh_status": item.get("status") or "",
                "recognized_screens": recognized_screens,
            }
        return {
            "status": "SUCCESS",
            "code": "PUBLISH_VERIFIED",
            "stage": "VERIFY",
            "message": "Резюме создано, а результат подтверждён в hh.ru.",
            "hh_resume_id": item["hh_resume_id"],
            "hh_resume_url": item["href"],
            "hh_status": item.get("status") or "",
            "recognized_screens": recognized_screens,
        }

    @serialize_account
    async def reconcile_resume_publish(
        self, user_id: int, account_id: int, draft: dict, attempt: dict
    ) -> dict[str, Any]:
        result = await self.fetch_user_resumes(user_id, account_id)
        if result.get("status") != "SUCCESS":
            return {**result, "status": "UNCERTAIN", "code": "RECONCILE_FAILED", "stage": "RECONCILE"}
        snapshots = await self.db.list_resume_snapshots(user_id, account_id)
        hh_resume_id = str(attempt.get("hh_resume_id") or draft.get("hh_resume_id") or "")
        matches = [item for item in snapshots if hh_resume_id and item["hh_resume_id"] == hh_resume_id]
        if not matches and not hh_resume_id:
            title = str((draft.get("data", {}).get("profession") or {}).get("title") or "").casefold()
            matches = [item for item in snapshots if str(item.get("title") or "").casefold() == title]
        if len(matches) != 1:
            return {
                "status": "UNCERTAIN",
                "code": "EXTERNAL_RESULT_AMBIGUOUS",
                "stage": "RECONCILE",
                "message": "Результат на hh.ru нельзя определить однозначно. Не запускайте новое создание.",
            }
        item = matches[0]
        missing_sections = _missing_external_sections(
            draft.get("data") or {}, str(item.get("extracted_text") or "")
        )
        profile_check = await self.inspect_resume_profile(user_id, account_id)
        if profile_check.get("status") != "SUCCESS" or not _profile_matches_draft(
            draft.get("data") or {}, dict(profile_check.get("profile") or {})
        ):
            missing_sections.append("Профиль и контакты")
        missing_sections = list(dict.fromkeys(missing_sections))
        if missing_sections:
            return {
                "status": "PARTIAL",
                "code": "SECTIONS_NOT_VERIFIED",
                "stage": "RECONCILE",
                "message": f"Резюме найдено. Не завершён перенос раздела «{missing_sections[0]}».",
                "missing_sections": missing_sections,
                "hh_resume_id": item["hh_resume_id"],
                "hh_resume_url": item["href"],
                "hh_status": item.get("status") or "",
            }
        return {
            "status": "SUCCESS",
            "code": "PUBLISH_RECONCILED",
            "stage": "RECONCILE",
            "message": "Существующее резюме найдено; повторное создание не требуется.",
            "hh_resume_id": item["hh_resume_id"],
            "hh_resume_url": item["href"],
            "hh_status": item.get("status") or "",
        }

    async def _fill_step_by_step_resume(
        self,
        page: Page,
        resume: StructuredResume,
        draft_data: dict | None = None,
        start_url: str = "https://hh.ru/profile/resume/professional_role",
    ) -> dict[str, Any]:
        visited_screens: list[str] = []
        current_stage = "OPEN"
        try:
            await page.goto(start_url, wait_until="commit", timeout=30_000)
            page_ready = await _wait_for_resume_page_signal(page)
            gate = await _page_gate(page)
            if gate:
                return {
                    "status": "NEEDS_ACTION",
                    **gate,
                    "stage": "OPEN",
                    "required_action": gate["code"],
                    "recognized_screens": visited_screens,
                }
            if not page_ready:
                return self._form_changed("initial", visited_screens)
            experience_index = 0
            education_index = 0
            link_index = 0
            additional_indexes = {
                "courses": 0,
                "exams": 0,
                "certificates": 0,
                "recommendations": 0,
            }
            for _ in range(40):
                gate = await _page_gate(page)
                if gate:
                    return {
                        "status": "NEEDS_ACTION",
                        **gate,
                        "stage": "HH_WIZARD",
                        "required_action": gate["code"],
                        "recognized_screens": visited_screens,
                    }
                manual = page.get_by_text("Укажу профессию", exact=True).first
                if await _visible(manual):
                    current_stage = "PROFESSION"
                    await human_click(page, manual)
                    title_wait = page.locator(
                        '[data-qa="resume-profile-position-input"], '
                        '[data-qa="professional-role-search-input"], [data-qa="resume-title-input"]'
                    ).first
                    if not await _wait_visible(title_wait):
                        return self._form_changed("profession", visited_screens)
                    continue

                title_input = page.locator(
                    '[role="dialog"] [data-qa="resume-profile-position-input"], '
                    '[data-qa="resume-profile-position-input"], '
                    '[data-qa="professional-role-search-input"], [data-qa="resume-title-input"], '
                    'input[placeholder*="профессию"], input[placeholder*="Должность"]'
                ).first
                if await _visible(title_input):
                    current_stage = "PROFESSION"
                    visited_screens.append("profession")
                    profession_value = str(
                        ((draft_data or {}).get("profession") or {}).get("hh_profession") or resume.title
                    )
                    await self._replace_value(page, title_input, profession_value)
                    options = page.locator('[role="option"], [data-qa="suggest-item-cell"], [data-qa="professional-role-item"]')
                    if not await _wait_visible(options.first):
                        return {
                            "status": "NEEDS_ACTION",
                            "code": "PROFESSION_NOT_FOUND",
                            "stage": "PROFESSION",
                            "message": "hh.ru не предложил профессию. Выберите её вручную в мастере.",
                        }
                    option_texts = [text.strip() for text in await options.all_inner_texts() if text.strip()]
                    exact_indexes = [
                        i for i, text in enumerate(option_texts)
                        if text.casefold() == profession_value.casefold()
                    ]
                    if exact_indexes:
                        await human_click(page, options.nth(exact_indexes[0]))
                    elif len(option_texts) == 1:
                        await human_click(page, options.first)
                    else:
                        return {
                            "status": "NEEDS_ACTION",
                            "code": "AMBIGUOUS_PROFESSION",
                            "stage": "PROFESSION",
                            "message": "Профессия неоднозначна. Подтвердите вариант перед продолжением.",
                            "options": option_texts[:20],
                        }
                    for specialization in ((draft_data or {}).get("profession") or {}).get(
                        "specializations"
                    ) or []:
                        choice = page.get_by_text(str(specialization), exact=True).first
                        if await _visible(choice):
                            await human_click(page, choice)
                    if not await self._click_continue(page, scope=title_input):
                        return await self._form_failure(page, "PROFESSION", "Не удалось сохранить профессию.")
                    continue

                first_name = page.locator(
                    '[data-qa="resume-person-first-name"], input[name*="firstName"]'
                ).first
                if await _visible(first_name):
                    current_stage = "PERSONAL"
                    visited_screens.append("personal")
                    for locator, value in (
                        (first_name, resume.first_name),
                        (page.locator('[data-qa="resume-person-last-name"], input[name*="lastName"]').first, resume.last_name),
                        (page.locator('[data-qa="resume-person-middle-name"], input[name*="middleName"]').first, resume.middle_name),
                    ):
                        await self._fill_empty(page, locator, value)
                    city = page.locator('[data-qa="resume-person-area"], input[placeholder*="Город"]').first
                    if await _visible(city) and not (await city.input_value()).strip():
                        await human_type(page, city, resume.city)
                        options = page.locator('[role="option"], [data-qa="suggest-item-cell"]')
                        if not await _wait_visible(options.first):
                            return await self._form_failure(page, "PERSONAL", "hh.ru не подтвердил город.")
                        texts = [text.strip() for text in await options.all_inner_texts() if text.strip()]
                        exact = [i for i, text in enumerate(texts) if text.casefold() == resume.city.casefold()]
                        if exact:
                            await human_click(page, options.nth(exact[0]))
                        elif len(texts) == 1:
                            await human_click(page, options.first)
                        else:
                            return {
                                "status": "NEEDS_ACTION",
                                "code": "AMBIGUOUS_CITY",
                                "stage": "PERSONAL",
                                "message": "Город неоднозначен. Подтвердите вариант.",
                                "options": texts[:20],
                            }
                    try:
                        birth = date.fromisoformat(resume.birth_date)
                    except ValueError:
                        return await self._form_failure(page, "PERSONAL", "Укажите точную дату рождения.")
                    birthday = page.locator('input[name="birthday"], input[type="date"]').first
                    if await _visible(birthday):
                        await self._fill_empty(page, birthday, resume.birth_date)
                    else:
                        await self._fill_empty(
                            page,
                            page.locator('[data-qa="resume-person-birth-day"], input[name*="birthDay"]').first,
                            str(birth.day),
                        )
                        await self._fill_empty(
                            page,
                            page.locator('[data-qa="resume-person-birth-year"], input[name*="birthYear"]').first,
                            str(birth.year),
                        )
                        month = page.locator(
                            '[data-qa="resume-person-birth-month"], select[name*="birthMonth"]'
                        ).first
                        if await _visible(month) and not (await month.input_value()).strip():
                            await month.select_option(index=birth.month)
                    personal = (draft_data or {}).get("personal") or {}
                    for value in [
                        personal.get("gender"),
                        *(personal.get("citizenships") or []),
                        *(personal.get("work_authorizations") or []),
                    ]:
                        if value:
                            choice = page.get_by_text(str(value), exact=True).first
                            if await _visible(choice):
                                await human_click(page, choice)
                    if not await self._click_continue(page, scope=first_name):
                        return await self._form_failure(page, "PERSONAL", "Не удалось сохранить личные данные.")
                    continue

                phone = page.locator('input[type="tel"], input[name="phone"]').first
                email = page.locator('input[type="email"], input[name="email"]').first
                if await _visible(phone) or await _visible(email):
                    current_stage = "CONTACTS"
                    visited_screens.append("contacts")
                    contacts = (draft_data or {}).get("contacts") or {}
                    await self._fill_empty(page, phone, str(contacts.get("phone") or ""))
                    await self._fill_empty(page, email, str(contacts.get("email") or ""))
                    telegram = page.locator('input[name*="telegram" i], [data-qa*="telegram" i] input').first
                    await self._fill_empty(page, telegram, str(contacts.get("telegram") or ""))
                    for value in [contacts.get("preferred"), *(contacts.get("methods") or [])]:
                        if value:
                            choice = page.get_by_text(str(value), exact=True).first
                            if await _visible(choice):
                                await human_click(page, choice)
                    scope = phone if await _visible(phone) else email
                    if not await self._click_continue(page, scope=scope):
                        return await self._form_failure(page, "CONTACTS", "Не удалось сохранить контакты.")
                    continue

                salary = page.locator(
                    '[data-qa*="salary" i] input, input[name*="salary" i]'
                ).first
                if await _visible(salary):
                    current_stage = "CONDITIONS"
                    visited_screens.append("conditions")
                    conditions = (draft_data or {}).get("work_conditions") or {}
                    salary_value = conditions.get("salary")
                    await self._fill_empty(page, salary, str(salary_value) if salary_value is not None else "")
                    for value in (
                        conditions.get("employment_types") or []
                    ) + (conditions.get("schedules") or []) + (conditions.get("work_formats") or []) + [
                        conditions.get("relocation"), conditions.get("business_trips")
                    ]:
                        if not value:
                            continue
                        choice = page.get_by_text(str(value), exact=True).first
                        if await _visible(choice):
                            await human_click(page, choice)
                    if not await self._click_continue(page, scope=salary):
                        return await self._form_failure(page, "CONDITIONS", "Не удалось сохранить условия работы.")
                    continue

                add_experience = page.get_by_text(
                    re.compile(r"Добавить (?:место работы|опыт)", re.IGNORECASE)
                ).first
                if experience_index < len(resume.experiences) and await _visible(add_experience):
                    current_stage = "EXPERIENCE"
                    visited_screens.append("experience-list")
                    await human_click(page, add_experience)
                    expected = page.locator(
                        '[data-qa^="resume-profile-experience-specific-company-input"], input[name="company"]'
                    ).first
                    if not await _wait_visible(expected):
                        return self._form_changed("experience-list", visited_screens)
                    continue

                experience_field = page.locator(
                    '[data-qa^="resume-profile-experience-specific-company-input"], input[name="company"], '
                    'input[placeholder*="Компания"]'
                ).first
                if await _visible(experience_field):
                    current_stage = "EXPERIENCE"
                    visited_screens.append("experience")
                    if experience_index >= len(resume.experiences):
                        if not await self._click_continue(page, scope=experience_field):
                            return await self._form_failure(page, "EXPERIENCE", "Не удалось пропустить опыт.")
                        continue
                    item = resume.experiences[experience_index]
                    fields = (
                        (experience_field, item.company),
                        (page.locator('input[name="position"], input[placeholder*="Должность"]').first, item.position),
                        (
                            page.locator(
                                '[data-qa="resume-editor-experience-description-input"], '
                                'textarea[name="description"], textarea[placeholder*="занимались"]'
                            ).first,
                            item.description,
                        ),
                        (page.locator('[data-qa="resume-editor-experience-start-year-input"]').first, item.start_year),
                        (page.locator('[data-qa="resume-editor-experience-end-year-input"]').first, item.end_year or ""),
                        (page.locator('[data-qa="resume-editor-experience-start-month-input"]').first, item.start_month),
                        (page.locator('[data-qa="resume-editor-experience-end-month-input"]').first, item.end_month or ""),
                    )
                    for locator, value in fields:
                        await self._fill_empty(page, locator, str(value or ""))
                    current_job = page.locator(
                        '[data-qa*="experience-current" i] input[type="checkbox"], '
                        'input[name*="current" i][type="checkbox"]'
                    ).first
                    if item.is_current and await _visible(current_job) and not await current_job.is_checked():
                        await human_click(page, current_job)
                    if not await self._click_continue(page, scope=experience_field):
                        return await self._form_failure(page, "EXPERIENCE", "Не удалось сохранить опыт работы.")
                    experience_index += 1
                    continue

                add_education = page.get_by_text(
                    re.compile(r"Добавить (?:образование|учебное заведение)", re.IGNORECASE)
                ).first
                if education_index < len(resume.education) and await _visible(add_education):
                    current_stage = "EDUCATION"
                    visited_screens.append("education-list")
                    await human_click(page, add_education)
                    expected = page.locator(
                        'textarea[name="institution"], [data-qa*="education-institution" i]'
                    ).first
                    if not await _wait_visible(expected):
                        return self._form_changed("education-list", visited_screens)
                    continue

                institution = page.locator(
                    'textarea[name="institution"], [data-qa*="education-institution" i], '
                    'textarea[placeholder*="заведение"], input[name="institution"]'
                ).first
                if await _visible(institution):
                    current_stage = "EDUCATION"
                    visited_screens.append("education")
                    if education_index < len(resume.education):
                        item = resume.education[education_index]
                        for locator, value in (
                            (institution, item.institution),
                            (page.locator('textarea[name="faculty"], input[name="faculty"]').first, item.faculty),
                            (
                                page.locator('textarea[name="specialization"], input[name="specialization"]').first,
                                item.specialization,
                            ),
                            (page.locator('[data-qa="profile-education-year-input"]').first, item.end_year),
                            (page.locator('[data-qa*="education-level" i] input, input[name="level"]').first, item.level),
                        ):
                            await self._fill_empty(page, locator, str(value or ""))
                    if not await self._click_continue(page, scope=institution):
                        return await self._form_failure(page, "EDUCATION", "Не удалось сохранить образование.")
                    education_index += 1
                    continue

                language_input = page.locator(
                    '[data-qa*="language" i] input, input[name*="language" i]'
                ).first
                if await _visible(language_input):
                    current_stage = "LANGUAGES"
                    visited_screens.append("languages")
                    languages = (draft_data or {}).get("languages") or []
                    for language in languages:
                        await self._replace_value(page, language_input, str(language.get("name") or ""))
                        exact = page.get_by_text(str(language.get("name") or ""), exact=True).first
                        if await _visible(exact):
                            await human_click(page, exact)
                    if not await self._click_continue(page, scope=language_input):
                        return await self._form_failure(page, "LANGUAGES", "Не удалось сохранить языки.")
                    continue

                link_input = page.locator(
                    'input[type="url"], input[name*="url" i], [data-qa*="portfolio" i] input'
                ).first
                if await _visible(link_input):
                    current_stage = "LINKS"
                    visited_screens.append("links")
                    links = ((draft_data or {}).get("about") or {}).get("links") or []
                    if link_index < len(links):
                        await self._fill_empty(
                            page, link_input, str(links[link_index].get("url") or "")
                        )
                    if not await self._click_continue(page, scope=link_input):
                        return await self._form_failure(page, "LINKS", "Не удалось сохранить профессиональные ссылки.")
                    link_index += 1
                    continue

                additional_handled = False
                additional = (draft_data or {}).get("additional") or {}
                for key, marker in (
                    ("courses", "course"),
                    ("exams", "exam"),
                    ("certificates", "certificate"),
                    ("recommendations", "recommendation"),
                ):
                    detail_input = page.locator(
                        f'[data-qa*="{marker}" i] input, [data-qa*="{marker}" i] textarea, '
                        f'input[name*="{marker}" i], textarea[name*="{marker}" i]'
                    ).first
                    if not await _visible(detail_input):
                        continue
                    current_stage = key.upper()
                    visited_screens.append(key)
                    items = additional.get(key) or []
                    index = additional_indexes[key]
                    if index < len(items):
                        item = items[index]
                        await self._fill_empty(page, detail_input, str(item.get("name") or ""))
                        organization = page.locator(
                            'input[name*="organization" i], textarea[name*="organization" i]'
                        ).first
                        year = page.locator('input[name*="year" i]').first
                        description = page.locator(
                            'textarea[name*="description" i], [data-qa*="description" i] textarea'
                        ).first
                        await self._fill_empty(page, organization, str(item.get("organization") or ""))
                        await self._fill_empty(page, year, str(item.get("year") or ""))
                        await self._fill_empty(page, description, str(item.get("description") or ""))
                    if not await self._click_continue(page, scope=detail_input):
                        return await self._form_failure(
                            page, key.upper(), f"Не удалось сохранить раздел «{key}»."
                        )
                    additional_indexes[key] += 1
                    additional_handled = True
                    break
                if additional_handled:
                    continue

                driving_block = page.locator(
                    '[data-qa*="driving" i], [data-qa*="driver" i]'
                ).first
                if await _visible(driving_block):
                    current_stage = "DRIVING"
                    visited_screens.append("driving")
                    for license_name in additional.get("driving_licenses") or []:
                        choice = driving_block.get_by_text(str(license_name), exact=True).first
                        if await _visible(choice):
                            await human_click(page, choice)
                    if additional.get("has_car"):
                        car = page.get_by_text(re.compile(r"есть автомобиль", re.IGNORECASE)).first
                        if await _visible(car):
                            await human_click(page, car)
                    if not await self._click_continue(page, scope=driving_block):
                        return await self._form_failure(
                            page, "DRIVING", "Не удалось сохранить водительские сведения."
                        )
                    continue

                skill_level_heading = page.get_by_text(
                    re.compile(r"уровень владения|оцените навык", re.IGNORECASE)
                ).first
                if await _visible(skill_level_heading):
                    current_stage = "SKILL_LEVELS"
                    visited_screens.append("skill-levels")
                    for skill in (draft_data or {}).get("skills") or []:
                        level = str(skill.get("level") or "")
                        if not level:
                            continue
                        skill_name = str(skill.get("name") or "")
                        block = page.locator(
                            '[data-qa*="skill" i], [role="group"]'
                        ).filter(has_text=skill_name).first
                        option = block.get_by_text(level, exact=True).first
                        if await _visible(option):
                            await human_click(page, option)
                    if not await self._click_continue(page, scope=skill_level_heading):
                        return await self._form_failure(
                            page, "SKILL_LEVELS", "Не удалось сохранить уровни навыков."
                        )
                    continue

                skill_input = page.locator(
                    '[data-qa*="skill" i] input, input[placeholder*="навык"], '
                    'input[placeholder*="Поиск"], input[name*="skill"]'
                ).first
                if await _visible(skill_input):
                    current_stage = "SKILLS"
                    visited_screens.append("skills")
                    for skill in resume.skills:
                        await self._replace_value(page, skill_input, skill)
                        exact = page.get_by_text(skill, exact=True).first
                        if await _wait_visible(exact, timeout=2500):
                            await human_click(page, exact)
                        else:
                            await skill_input.press("Enter")
                    if not await self._click_continue(page, scope=skill_input):
                        return await self._form_failure(page, "SKILLS", "Не удалось сохранить навыки.")
                    continue

                about = page.locator(
                    '[data-qa="resume-about-me-input"], textarea[name="about"], '
                    'textarea[placeholder*="О себе"]'
                ).first
                if await _visible(about):
                    current_stage = "ABOUT"
                    visited_screens.append("about")
                    await self._fill_empty(page, about, resume.about)
                    if not await self._click_continue(page, scope=about):
                        return await self._form_failure(page, "ABOUT", "Не удалось сохранить раздел «О себе».")
                    continue

                publish = page.locator(
                    '[data-qa="resume-publish"], [data-qa="resume-save"], '
                    'button:has-text("Опубликовать"), button:has-text("Сохранить и опубликовать")'
                ).first
                if await _visible(publish):
                    current_stage = "PUBLISH"
                    visited_screens.append("publish")
                    visibility = str(
                        ((draft_data or {}).get("publication") or {}).get("visibility") or ""
                    )
                    if visibility:
                        visibility_option = page.get_by_text(visibility, exact=True).first
                        if await _visible(visibility_option):
                            await human_click(page, visibility_option)
                    old_resume = page.locator(
                        '[data-qa*="resume-selector" i] input[type="radio"]:checked, '
                        '[data-qa*="resume-selector" i] input[type="checkbox"]:checked'
                    )
                    if await old_resume.count() > 0:
                        return {
                            "status": "NEEDS_ACTION",
                            "code": "OLD_RESUME_SELECTED",
                            "stage": "PUBLISH",
                            "message": "hh.ru выбрал существующее резюме. Подтвердите создание нового объекта вручную.",
                            "required_action": "CONFIRM_NEW_RESUME",
                        }
                    await human_click(page, publish)
                    return {"status": "SUBMITTED", "recognized_screens": visited_screens}

                if _resume_id_from_url(page.url):
                    return {"status": "SUBMITTED", "recognized_screens": visited_screens}
                return self._form_changed("unknown", visited_screens)
            return self._form_changed("loop-limit", visited_screens)
        except PatchrightTimeoutError:
            code = "HH_NAVIGATION_TIMEOUT" if current_stage == "OPEN" else "HH_STEP_TIMEOUT"
            result_uncertain = current_stage == "PUBLISH" or bool(
                _resume_id_from_url(page.url, allow_edit=True)
            )
            logger.warning(
                "Resume wizard timeout code=%s stage=%s path=%s recognized=%s",
                code,
                current_stage,
                _safe_page_path(page),
                visited_screens,
            )
            return {
                "status": "UNCERTAIN" if result_uncertain else "NEEDS_ACTION",
                "code": "EXTERNAL_RESULT_UNCERTAIN" if result_uncertain else code,
                "stage": current_stage,
                "message": (
                    "hh.ru мог сохранить резюме. Перед повтором выполните сверку результата."
                    if result_uncertain
                    else
                    "hh.ru не ответил при открытии мастера. Черновик сохранён; повторите публикацию."
                    if current_stage == "OPEN"
                    else f"hh.ru не подтвердил переход на этапе «{current_stage}». Черновик сохранён."
                ),
                "retryable": not result_uncertain,
                "required_action": "RECONCILE" if result_uncertain else "RETRY_PUBLICATION",
                "recognized_screens": visited_screens,
            }
        except HumanizationError as exc:
            result_uncertain = current_stage == "PUBLISH" or bool(
                _resume_id_from_url(page.url, allow_edit=True)
            )
            logger.warning(
                "Resume wizard action not confirmed operation=%s reason=%s stage=%s path=%s",
                exc.operation,
                exc.reason,
                current_stage,
                _safe_page_path(page),
            )
            return {
                "status": "UNCERTAIN" if result_uncertain else "NEEDS_ACTION",
                "code": (
                    "EXTERNAL_RESULT_UNCERTAIN"
                    if result_uncertain
                    else "HH_ACTION_NOT_CONFIRMED"
                ),
                "stage": current_stage,
                "message": (
                    "hh.ru мог сохранить резюме. Перед повтором выполните сверку результата."
                    if result_uncertain
                    else "hh.ru не подтвердил действие в форме. Черновик сохранён."
                ),
                "retryable": not result_uncertain,
                "required_action": "RECONCILE" if result_uncertain else "RETRY_PUBLICATION",
                "recognized_screens": visited_screens,
            }
        except Exception as exc:
            result_uncertain = current_stage == "PUBLISH" or bool(
                _resume_id_from_url(page.url, allow_edit=True)
            )
            logger.error(
                "Resume wizard failed code=HH_BROWSER_ERROR exception=%s stage=%s path=%s recognized=%s",
                type(exc).__name__,
                current_stage,
                _safe_page_path(page),
                visited_screens,
            )
            return {
                "status": "UNCERTAIN" if result_uncertain else "ERROR",
                "code": "EXTERNAL_RESULT_UNCERTAIN" if result_uncertain else "HH_BROWSER_ERROR",
                "stage": current_stage,
                "message": (
                    "hh.ru мог сохранить резюме. Перед повтором выполните сверку результата."
                    if result_uncertain
                    else "Не удалось обработать текущий экран hh.ru. Черновик сохранён."
                ),
                "retryable": False,
                "required_action": "RECONCILE" if result_uncertain else "RETRY_AFTER_UPDATE",
                "recognized_screens": visited_screens,
            }

    @staticmethod
    async def _replace_value(page: Page, locator: Locator, value: str) -> None:
        if await locator.evaluate("node => node.tagName === 'SELECT'"):
            try:
                await locator.select_option(label=value)
            except Exception:
                await locator.select_option(value=value)
            return
        await locator.fill("")
        if value:
            await human_type(page, locator, value)

    @staticmethod
    async def _fill_empty(page: Page, locator: Locator, value: str) -> None:
        if value and await _visible(locator) and not (await locator.input_value()).strip():
            if await locator.evaluate("node => node.tagName === 'SELECT'"):
                try:
                    await locator.select_option(label=value)
                except Exception:
                    await locator.select_option(value=value)
            else:
                await human_type(page, locator, value)

    @staticmethod
    def _form_changed(screen: str, recognized_screens: list[str] | None = None) -> dict[str, Any]:
        return {
            "status": "NEEDS_ACTION",
            "code": "HH_FORM_CHANGED",
            "stage": "HH_WIZARD",
            "recognized_screen": screen,
            "message": "Разметка мастера hh.ru изменилась. Черновик сохранён; случайные кнопки не нажимались.",
            "required_action": "RETRY_AFTER_UPDATE",
            "retryable": True,
            "recognized_screens": list(recognized_screens or []),
        }

    @staticmethod
    async def _form_failure(page: Page, stage: str, fallback: str) -> dict[str, Any]:
        errors = await page.locator(
            '[aria-invalid="true"], [data-qa*="error" i], [role="alert"]'
        ).all_inner_texts()
        messages = list(dict.fromkeys(text.strip() for text in errors if text.strip()))
        return {
            "status": "NEEDS_INPUT",
            "code": "HH_VALIDATION_ERROR",
            "stage": stage,
            "message": messages[0] if messages else fallback,
            "field_errors": messages,
            "required_action": "EDIT_DRAFT",
        }

    @staticmethod
    async def _click_continue(page: Page, scope: Locator | None = None) -> bool:
        root = page.locator("body")
        if scope is not None:
            form = scope.locator("xpath=ancestor::form[1]")
            if await form.count() > 0:
                root = form
        button = root.locator(
            '[data-qa="resume-submit"], [data-qa="professional-role-submit"], '
            'button:has-text("Сохранить и продолжить"), button:has-text("Продолжить"), '
            'button:has-text("Далее"), button[type="submit"]'
        ).first
        if not await _wait_visible(button):
            return False
        previous_text = await page.locator("body").inner_text()
        try:
            await human_click(page, button)
        except HumanizationError:
            return False
        try:
            await page.wait_for_function(
                "previous => (document.body?.innerText || '') !== previous",
                previous_text,
                timeout=DEFAULT_TRANSITION_TIMEOUT_MS,
            )
        except Exception:
            # Some React transitions keep the same labels. The caller detects
            # the next supported screen and will stop safely if nothing changed.
            pass
        return not bool(await page.locator('[aria-invalid="true"]').count())

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
