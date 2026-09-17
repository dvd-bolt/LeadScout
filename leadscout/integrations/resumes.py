"""Safe PDF parsing and hh.ru resume synchronization."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import weakref
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
_PROFESSION_INPUT_SELECTOR = (
    'input[data-qa="resume-profile-position-input"], '
    '[data-qa="resume-profile-position-input"] input, '
    '[data-qa="resume-profile-position-input"][contenteditable="true"], '
    '[data-qa="resume-profile-position-input"] [contenteditable="true"], '
    'input[data-qa="professional-role-search-input"], '
    '[data-qa="professional-role-search-input"] input, '
    '[data-qa="professional-role-search-input"] [contenteditable="true"], '
    'input[data-qa="resume-title-input"], [data-qa="resume-title-input"] input, '
    'input[placeholder*="профессию"], input[placeholder*="Должность"]'
)
_COOKIE_ACCEPT_SELECTOR = (
    '[data-qa="cookies-policy-informer-accept"], '
    'button:has-text("Принять cookies"), button:has-text("Принять cookie")'
)
_VALIDATION_SELECTOR = (
    '[aria-invalid="true"], [aria-errormessage], [data-qa*="error" i], '
    '[data-qa*="validation" i], [role="alert"], form [class*="error" i]'
)
_PROFESSION_SELECTION_TIMEOUT_MS = 5_000
_RESUME_NETWORK_EVENTS: weakref.WeakKeyDictionary[Page, list[dict[str, Any]]] = (
    weakref.WeakKeyDictionary()
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


def _first_visible(locator: Locator) -> Locator:
    """Select the rendered copy when hh.ru keeps hidden desktop/mobile duplicates."""
    return locator.filter(visible=True).first


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
            // A stale applicant session is currently redirected to the public
            // regional home page. That page uses a phone sign-up form rather
            // than a password field or the old "Вход на hh.ru" heading.
            if ((location.pathname === '/' || location.pathname === '') &&
                visible('[data-qa="login"], [data-qa="mainmenu_profile-link"]') &&
                visible('[data-qa="auth-form"], [data-qa="account-signup-email"], [data-qa="signup"]')) {
                return {code: 'LOGIN_REQUIRED', message: 'Сессия hh.ru истекла. Войдите заново.'};
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
                const visible = (node) => {
                    if (!node || node.closest('[hidden], [aria-hidden="true"]')) return false;
                    const style = getComputedStyle(node);
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                        node.getClientRects().length > 0;
                };
                const selectors = [
                    'input[data-qa="resume-profile-position-input"]',
                    '[data-qa="resume-profile-position-input"] input',
                    'input[data-qa="professional-role-search-input"]',
                    '[data-qa="professional-role-search-input"] input',
                    'input[data-qa="resume-title-input"]',
                    '[data-qa="resume-title-input"] input',
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
                return selectors.some((selector) =>
                    [...document.querySelectorAll(selector)].some(visible)
                ) ||
                    /(?:Укажу профессию|Добавить (?:место работы|опыт|образование|учебное заведение)|уровень владения|оцените навык|войти в аккаунт|вход на hh\.ru|captcha|капч|не робот|код подтверждения)/i.test(text);
            }""",
            timeout=timeout,
        )
        return True
    except PatchrightTimeoutError:
        return False


def _safe_url_path(url: str) -> str:
    """Return a diagnostic URL path without query data or user content."""
    match = re.match(r"https?://[^/]+(?P<path>/[^?#]*)", url or "")
    return match.group("path") if match else ""


def _safe_page_path(page: Page) -> str:
    return _safe_url_path(page.url)


def _install_resume_network_diagnostics(page: Page) -> None:
    """Remember failed hh.ru requests without retaining query strings or payloads."""
    events: list[dict[str, Any]] = []
    _RESUME_NETWORK_EVENTS[page] = events

    def append(event: dict[str, Any]) -> None:
        events.append(event)
        del events[:-20]

    def hh_target(url: str) -> tuple[str, str] | None:
        match = re.match(r"https?://(?P<host>[^/:?#]+)(?P<path>/[^?#]*)?", url or "")
        if not match:
            return None
        host = match.group("host").lower()
        if host != "hh.ru" and not host.endswith(".hh.ru"):
            return None
        return host, match.group("path") or "/"

    def on_response(response: Any) -> None:
        try:
            if (
                response.status < 400
                or response.request.resource_type
                not in {"document", "xhr", "fetch", "script", "stylesheet"}
                or not (target := hh_target(response.url))
            ):
                return
            append(
                {
                    "kind": "response",
                    "host": target[0],
                    "path": target[1],
                    "method": response.request.method,
                    "resource": response.request.resource_type,
                    "status": response.status,
                }
            )
        except Exception:
            return

    def on_request_failed(request: Any) -> None:
        try:
            if (
                request.resource_type
                not in {"document", "xhr", "fetch", "script", "stylesheet"}
                or not (target := hh_target(request.url))
            ):
                return
            append(
                {
                    "kind": "request_failed",
                    "host": target[0],
                    "path": target[1],
                    "method": request.method,
                    "resource": request.resource_type,
                }
            )
        except Exception:
            return

    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)


async def _dismiss_hh_cookie_notice(page: Page) -> None:
    button = _first_visible(page.locator(_COOKIE_ACCEPT_SELECTOR))
    if not await _visible(button):
        return
    await human_click(page, button)
    try:
        await button.wait_for(state="hidden", timeout=3_000)
    except Exception:
        diagnostic = await _resume_screen_diagnostic(page)
        logger.warning(
            "HH_RESUME event=cookie_notice_remained path=%s wizard=%s "
            "diagnostic_failures=%s controls=%s",
            diagnostic["path"],
            diagnostic["wizard"],
            diagnostic["diagnostic_failures"],
            diagnostic["controls"],
        )
        return
    logger.info("HH_RESUME event=cookie_notice_dismissed path=%s", _safe_page_path(page))


async def _resume_screen_diagnostic(page: Page) -> dict[str, Any]:
    """Collect selector-only diagnostics without values, page text, or query data."""
    failures: list[str] = []
    controls: list[dict[str, Any]] = []
    try:
        control_locator = page.locator(
            "main [data-qa], main input, main textarea, main select, main button, main [role]"
        ).filter(visible=True)
        if await control_locator.count() == 0:
            control_locator = page.locator(
                "[data-qa], input, textarea, select, button, [role]"
            ).filter(visible=True)
        controls = await control_locator.evaluate_all(
            r"""nodes => nodes.slice(0, 80).map((node) => {
                try {
                    return {
                        tag: (node.tagName || '').toLowerCase(),
                        qa: (node.getAttribute('data-qa') || '').slice(0, 100),
                        role: (node.getAttribute('role') || '').slice(0, 40),
                        type: (node.getAttribute('type') || '').slice(0, 40),
                        disabled: Boolean(node.disabled) || node.getAttribute('aria-disabled') === 'true',
                        checked: Boolean(node.checked) || node.getAttribute('aria-checked') === 'true',
                        expanded: (node.getAttribute('aria-expanded') || '').slice(0, 10),
                        invalid: (node.getAttribute('aria-invalid') || '').slice(0, 10)
                    };
                } catch (_) {
                    return {tag: 'unknown', qa: '', role: '', type: ''};
                }
            })"""
        )
    except Exception:
        failures.append("controls")

    profession_input = _first_visible(page.locator(_PROFESSION_INPUT_SELECTOR))
    profession_visible = False
    profession_has_value = False
    profession_expanded = ""
    try:
        profession_visible = await _visible(profession_input)
        if profession_visible:
            profession_has_value = bool(
                await profession_input.evaluate(
                    "node => Boolean(String(node.value || node.textContent || '').trim())"
                )
            )
            profession_expanded = str(
                await profession_input.get_attribute("aria-expanded") or ""
            )[:10]
    except Exception:
        failures.append("profession_input")

    continue_button = _first_visible(
        page.locator(
            '[data-qa="professional-role-submit"], [data-qa="resume-submit"], '
            'button[type="submit"]'
        )
    )
    continue_visible = False
    continue_disabled = False
    continue_qa = ""
    try:
        continue_visible = await _visible(continue_button)
        if continue_visible:
            continue_disabled = bool(
                await continue_button.is_disabled()
                or await continue_button.get_attribute("aria-disabled") == "true"
            )
            continue_qa = str(await continue_button.get_attribute("data-qa") or "")[:100]
    except Exception:
        failures.append("continue_button")

    suggestion_count = 0
    try:
        suggestion_count = await page.locator(
            '[role="option"], [data-qa="suggest-item-cell"], '
            '[data-qa="professional-role-item"]'
        ).filter(visible=True).count()
    except Exception:
        failures.append("suggestions")

    specialization_visible = False
    try:
        direct_specialization = page.locator(
            '[data-qa*="specialization" i], [name*="specialization" i]'
        ).filter(visible=True)
        specialization_visible = await direct_specialization.count() > 0
        if not specialization_visible:
            specialization_visible = await _visible(
                _first_visible(page.get_by_text(re.compile(r"специализац", re.IGNORECASE)))
            )
    except Exception:
        failures.append("specialization")

    cookie_visible = False
    try:
        cookie_visible = await _visible(
            _first_visible(page.locator('[data-qa="cookies-policy-informer-accept"]'))
        )
    except Exception:
        failures.append("cookie")

    active: dict[str, Any] | None = None
    try:
        active = await page.evaluate(
            r"""() => {
                const node = document.activeElement;
                if (!node) return null;
                return {
                    tag: (node.tagName || '').toLowerCase(),
                    qa: (node.getAttribute?.('data-qa') || '').slice(0, 100),
                    role: (node.getAttribute?.('role') || '').slice(0, 40),
                    type: (node.getAttribute?.('type') || '').slice(0, 40)
                };
            }"""
        )
    except Exception:
        failures.append("active_element")

    return {
        "path": _safe_page_path(page),
        "frames": len(page.frames),
        "controls": controls,
        "wizard": {
            "profession_input_visible": profession_visible,
            "profession_input_has_value": profession_has_value,
            "profession_input_expanded": profession_expanded,
            "continue_visible": continue_visible,
            "continue_disabled": continue_disabled,
            "continue_qa": continue_qa,
            "suggestion_count": suggestion_count,
            "specialization_section_visible": specialization_visible,
            "cookie_notice_visible": cookie_visible,
            "active": active,
        },
        "diagnostic_failures": failures,
        "network": list(_RESUME_NETWORK_EVENTS.get(page, [])),
    }


async def _select_and_confirm_profession_option(
    page: Page, title_input: Locator, selected_option: Locator
) -> bool:
    input_handle = await title_input.element_handle()
    option_handle = await selected_option.element_handle()
    await human_click(page, selected_option)
    try:
        await page.wait_for_function(
            r"""args => {
                const visible = (node) => {
                    if (!node || !node.isConnected ||
                        node.closest('[hidden], [aria-hidden="true"]')) return false;
                    const style = getComputedStyle(node);
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                        node.getClientRects().length > 0;
                };
                if (!visible(args.option)) return true;
                if (args.option.getAttribute('aria-selected') === 'true' ||
                    args.option.getAttribute('aria-checked') === 'true' ||
                    args.option.getAttribute('data-state') === 'checked') return true;
                const expanded = args.input?.getAttribute('aria-expanded');
                return expanded === 'false' && ![...document.querySelectorAll(
                    '[role="option"], [data-qa="suggest-item-cell"], [data-qa="professional-role-item"]'
                )].some(visible);
            }""",
            arg={"input": input_handle, "option": option_handle},
            timeout=_PROFESSION_SELECTION_TIMEOUT_MS,
        )
        return True
    except Exception:
        diagnostic = await _resume_screen_diagnostic(page)
        logger.warning(
            "HH_RESUME event=profession_selection_not_confirmed path=%s "
            "wizard=%s diagnostic_failures=%s network=%s controls=%s",
            diagnostic["path"],
            diagnostic["wizard"],
            diagnostic["diagnostic_failures"],
            diagnostic["network"],
            diagnostic["controls"],
        )
        return False


async def _profession_specialization_state(page: Page) -> dict[str, Any]:
    """Read visible specialization choices without logging their labels."""
    try:
        return dict(
            await page.evaluate(
                r"""() => {
                    const visible = (node) => {
                        if (!node || node.closest('[hidden], [aria-hidden="true"]')) return false;
                        const style = getComputedStyle(node);
                        return style.display !== 'none' && style.visibility !== 'hidden' &&
                            node.getClientRects().length > 0;
                    };
                    const selectable = [
                        'input[type="checkbox"]', 'input[type="radio"]',
                        '[role="checkbox"]', '[role="radio"]', '[role="option"]',
                        'button[aria-pressed]', '[data-qa*="specialization" i]'
                    ].join(',');
                    const direct = [...document.querySelectorAll(
                        '[data-qa*="specialization" i], [name*="specialization" i]'
                    )].filter(visible);
                    const markers = [...document.querySelectorAll('h1,h2,h3,h4,legend,label,span,p')]
                        .filter((node) => {
                            const text = (node.textContent || '').replace(/\s+/g, ' ').trim();
                            return visible(node) && text.length <= 120 && /специализац/i.test(text);
                        });
                    const roots = [...direct];
                    for (const marker of markers) {
                        let root = marker;
                        for (let depth = 0; root && depth < 5; depth += 1, root = root.parentElement) {
                            if (root.querySelector?.(selectable)) {
                                roots.push(root);
                                break;
                            }
                        }
                    }
                    const nodes = [...new Set(roots.flatMap((root) =>
                        root.matches?.(selectable) ? [root] : [...root.querySelectorAll(selectable)]
                    ))].filter(visible);
                    const choices = [];
                    for (const node of nodes) {
                        const control = node.matches('input') ? node :
                            node.querySelector?.('input[type="checkbox"],input[type="radio"]') || node;
                        const ownLabel = node.getAttribute('aria-label') ||
                            control.getAttribute?.('aria-label') || '';
                        const htmlLabel = control.labels?.[0]?.innerText ||
                            control.closest?.('label')?.innerText || '';
                        const label = (ownLabel || htmlLabel || node.innerText || node.textContent || '')
                            .replace(/\s+/g, ' ').trim();
                        if (!label || label.length > 200 ||
                            /^(?:продолжить|далее|сохранить)/i.test(label)) continue;
                        const selected = Boolean(control.checked) ||
                            ['aria-checked', 'aria-selected', 'aria-pressed'].some((name) =>
                                node.getAttribute(name) === 'true' || control.getAttribute?.(name) === 'true'
                            ) || node.getAttribute('data-state') === 'checked';
                        if (!choices.some((item) => item.label === label)) choices.push({label, selected});
                    }
                    return {
                        section_present: direct.length > 0 || markers.length > 0,
                        choices: choices.slice(0, 30)
                    };
                }"""
            )
        )
    except Exception:
        return {"section_present": False, "choices": []}


async def _click_named_choice(page: Page, label: str) -> bool:
    for role in ("checkbox", "radio", "option", "button"):
        choice = _first_visible(page.get_by_role(role, name=label, exact=True))
        if await _visible(choice):
            await human_click(page, choice)
            return True
    choice = _first_visible(page.get_by_text(label, exact=True))
    if await _visible(choice):
        await human_click(page, choice)
        return True
    return False


async def _prepare_profession_specializations(
    page: Page,
    resume_title: str,
    requested: list[str],
) -> dict[str, Any] | None:
    state = await _profession_specialization_state(page)
    if not state.get("section_present"):
        return None
    choices = list(state.get("choices") or [])
    if any(bool(item.get("selected")) for item in choices):
        return None

    normalized = {
        re.sub(r"\s+", " ", str(item.get("label") or "")).strip().casefold(): item
        for item in choices
        if str(item.get("label") or "").strip()
    }
    desired = [value for value in requested if value.strip()]
    source = "draft"
    if not desired and resume_title.strip():
        desired = [resume_title]
        source = "resume_title"

    selected = 0
    missing: list[str] = []
    for value in desired:
        item = normalized.get(re.sub(r"\s+", " ", value).strip().casefold())
        if not item or not await _click_named_choice(page, str(item["label"])):
            missing.append(value)
            continue
        selected += 1

    if selected and not missing:
        logger.info(
            "HH_RESUME event=profession_specialization_selected source=%s count=%s path=%s",
            source,
            selected,
            _safe_page_path(page),
        )
        return None

    diagnostic = await _resume_screen_diagnostic(page)
    logger.warning(
        "HH_RESUME event=profession_specialization_required requested_count=%s "
        "available_count=%s path=%s wizard=%s diagnostic_failures=%s network=%s",
        len(desired),
        len(choices),
        diagnostic["path"],
        diagnostic["wizard"],
        diagnostic["diagnostic_failures"],
        diagnostic["network"],
    )
    return {
        "status": "NEEDS_INPUT",
        "code": "SPECIALIZATION_NOT_FOUND" if missing and requested else "SPECIALIZATION_REQUIRED",
        "stage": "PROFESSION",
        "message": (
            "Сохранённая специализация не найдена на hh.ru. Выберите актуальную специализацию в черновике."
            if missing and requested
            else "hh.ru требует специализацию. Укажите её на шаге «Профессия» в черновике."
        ),
        "required_action": "EDIT_DRAFT",
        "specialization_options": [str(item.get("label") or "") for item in choices],
    }


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

    def contains_pair(first: str, second: str) -> bool:
        left = " ".join(str(first or "").casefold().split())
        right = " ".join(str(second or "").casefold().split())
        if not left or not right:
            return contains(left or right)
        # Values belonging to one row must appear together. Independent
        # membership would accept levels swapped between languages/skills.
        return f"{left} {right}" in haystack or f"{right} {left}" in haystack

    month_names = {
        "1": ("январь", "января", "january"),
        "2": ("февраль", "февраля", "february"),
        "3": ("март", "марта", "march"),
        "4": ("апрель", "апреля", "april"),
        "5": ("май", "мая", "may"),
        "6": ("июнь", "июня", "june"),
        "7": ("июль", "июля", "july"),
        "8": ("август", "августа", "august"),
        "9": ("сентябрь", "сентября", "september"),
        "10": ("октябрь", "октября", "october"),
        "11": ("ноябрь", "ноября", "november"),
        "12": ("декабрь", "декабря", "december"),
    }

    def contains_month(value: str) -> bool:
        normalized = str(value or "").strip().lstrip("0") or "0"
        names = month_names.get(normalized)
        return not names or any(name in haystack for name in names)

    def contains_token(value: str) -> bool:
        token = str(value or "").strip().casefold()
        return not token or re.search(rf"(?<![\w]){re.escape(token)}(?![\w])", haystack) is not None

    experiences = [item for item in data.get("experiences") or [] if item.get("selected", True)]
    if experiences and any(
        not contains(item.get("company", ""))
        or not contains(item.get("position", ""))
        or not contains_month(item.get("start_month", ""))
        or not contains(item.get("start_year", ""))
        or (
            not item.get("is_current")
            and (
                not contains_month(item.get("end_month", ""))
                or not contains(item.get("end_year", ""))
            )
        )
        or (
            item.get("is_current")
            and not any(marker in haystack for marker in ("настоящее время", "по настоящее", "present"))
        )
        or not contains(item.get("description", ""))
        for item in experiences
    ):
        missing.append("Опыт")
    education = [item for item in data.get("education") or [] if item.get("selected", True)]
    if education and any(
        not contains(item.get("institution", ""))
        or not contains(item.get("level", ""))
        or not contains(item.get("faculty", ""))
        or not contains(item.get("specialization", ""))
        or not contains(item.get("end_year", ""))
        for item in education
    ):
        missing.append("Образование")
    skills = data.get("skills") or []
    if skills and any(not contains_pair(item.get("name", ""), item.get("level", "")) for item in skills):
        missing.append("Навыки")
    about = str((data.get("about") or {}).get("text") or "")
    if about and not contains(about):
        missing.append("О себе")
    languages = data.get("languages") or []
    if languages and any(not contains_pair(item.get("name", ""), item.get("level", "")) for item in languages):
        missing.append("Языки")
    links = [item.get("url", "") for item in (data.get("about") or {}).get("links") or [] if item.get("url")]
    if links and any(not contains(link) for link in links):
        missing.append("Ссылки")
    additional = data.get("additional") or {}
    extra_values = [
        value
        for key in ("courses", "exams", "certificates", "recommendations")
        for item in additional.get(key) or []
        for value in (
            item.get("name", ""), item.get("organization", ""),
            item.get("year", ""), item.get("description", ""),
        )
        if value
    ]
    if extra_values and any(not contains(value) for value in extra_values):
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
    currency = str(conditions.get("currency") or "").upper()
    currency_aliases = {
        "RUR": ("rur", "rub", "₽", "руб"),
        "RUB": ("rur", "rub", "₽", "руб"),
        "USD": ("usd", "$", "доллар"),
        "EUR": ("eur", "€", "евро"),
    }
    currency_missing = bool(currency) and not any(
        alias in haystack for alias in currency_aliases.get(currency, (currency.casefold(),))
    )
    if currency_missing or any(value and not contains(value) for value in condition_values):
        missing.append("Условия работы")
    licenses = additional.get("driving_licenses") or []
    car_missing = additional.get("has_car") and not any(
        marker in haystack for marker in ("есть автомобиль", "личный автомобиль", "own car")
    )
    if any(not contains_token(value) for value in licenses) or car_missing:
        missing.append("Транспорт")
    visibility = str((data.get("publication") or {}).get("visibility") or "")
    if visibility and not contains(visibility):
        missing.append("Видимость")
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
                    if len(resume_text) > PDF_MAX_TEXT_CHARS:
                        raise ValueError("Resume text exceeds the supported size")
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
        replace_profile_values = bool(data.get("_confirmed_profile_changes"))
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
                page,
                structured,
                draft_data=data,
                start_url=start_url,
                replace_profile_values=replace_profile_values,
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
        replace_profile_values: bool = False,
    ) -> dict[str, Any]:
        visited_screens: list[str] = []
        current_stage = "OPEN"
        try:
            _install_resume_network_diagnostics(page)
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
                return await self._form_changed(page, "initial", visited_screens)
            await _dismiss_hh_cookie_notice(page)
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
                await _dismiss_hh_cookie_notice(page)
                gate = await _page_gate(page)
                if gate:
                    return {
                        "status": "NEEDS_ACTION",
                        **gate,
                        "stage": "HH_WIZARD",
                        "required_action": gate["code"],
                        "recognized_screens": visited_screens,
                    }
                manual = _first_visible(
                    page.get_by_role("button", name="Укажу профессию", exact=True)
                )
                if not await _visible(manual):
                    manual = _first_visible(page.get_by_text("Укажу профессию", exact=True))
                if await _visible(manual):
                    current_stage = "PROFESSION"
                    await human_click(page, manual)
                    title_wait = _first_visible(page.locator(_PROFESSION_INPUT_SELECTOR))
                    if not await _wait_visible(title_wait):
                        return await self._form_changed(page, "profession", visited_screens)
                    continue

                title_input = _first_visible(page.locator(_PROFESSION_INPUT_SELECTOR))
                if await _visible(title_input):
                    current_stage = "PROFESSION"
                    visited_screens.append("profession")
                    profession_value = str(
                        ((draft_data or {}).get("profession") or {}).get("hh_profession") or resume.title
                    )
                    await self._replace_value(page, title_input, profession_value)
                    options = page.locator(
                        '[role="option"], [data-qa="suggest-item-cell"], '
                        '[data-qa="professional-role-item"]'
                    ).filter(visible=True)
                    if not await _wait_visible(options.first):
                        return {
                            "status": "NEEDS_ACTION",
                            "code": "PROFESSION_NOT_FOUND",
                            "stage": "PROFESSION",
                            "message": "hh.ru не предложил профессию. Выберите её вручную в мастере.",
                        }
                    option_items = await options.evaluate_all(
                        """nodes => nodes.map(node => ({
                            label: (node.textContent || '').trim(),
                            id: node.getAttribute('data-id') || node.getAttribute('data-value') ||
                                node.getAttribute('value') || node.id || ''
                        })).filter(item => item.label)"""
                    )
                    option_texts = [str(item.get("label") or "") for item in option_items]
                    profession_id = str(
                        ((draft_data or {}).get("profession") or {}).get("hh_profession_id") or ""
                    )
                    id_indexes = [
                        i for i, item in enumerate(option_items)
                        if profession_id and str(item.get("id") or "") == profession_id
                    ]
                    exact_indexes = [
                        i for i, text in enumerate(option_texts)
                        if text.casefold() == profession_value.casefold()
                    ]
                    if id_indexes:
                        selected_option = options.nth(id_indexes[0])
                    elif exact_indexes:
                        selected_option = options.nth(exact_indexes[0])
                    elif len(option_texts) == 1:
                        selected_option = options.first
                    else:
                        return {
                            "status": "NEEDS_ACTION",
                            "code": "AMBIGUOUS_PROFESSION",
                            "stage": "PROFESSION",
                            "message": "Профессия неоднозначна. Подтвердите вариант перед продолжением.",
                            "options": option_items[:20],
                        }
                    if not await _select_and_confirm_profession_option(
                        page, title_input, selected_option
                    ):
                        return {
                            "status": "NEEDS_ACTION",
                            "code": "PROFESSION_SELECTION_NOT_CONFIRMED",
                            "stage": "PROFESSION",
                            "message": (
                                "hh.ru не подтвердил выбор профессии из подсказки. "
                                "Черновик сохранён; повторите перенос."
                            ),
                            "required_action": "RETRY_PUBLICATION",
                            "retryable": True,
                        }
                    profession_data = (draft_data or {}).get("profession") or {}
                    specialization_failure = await _prepare_profession_specializations(
                        page,
                        resume.title,
                        [str(value) for value in profession_data.get("specializations") or []],
                    )
                    if specialization_failure:
                        return specialization_failure
                    if not await self._click_continue(
                        page, scope=title_input, stage="PROFESSION"
                    ):
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
                        if replace_profile_values and value and await _visible(locator):
                            await self._replace_value(page, locator, value)
                        else:
                            await self._fill_empty(page, locator, value)
                    city = page.locator('[data-qa="resume-person-area"], input[placeholder*="Город"]').first
                    if await _visible(city) and (
                        replace_profile_values or not (await city.input_value()).strip()
                    ):
                        await self._replace_value(page, city, resume.city)
                        options = page.locator('[role="option"], [data-qa="suggest-item-cell"]')
                        if not await _wait_visible(options.first):
                            return await self._form_failure(page, "PERSONAL", "hh.ru не подтвердил город.")
                        city_options = await options.evaluate_all(
                            """nodes => nodes.map(node => ({
                                label: (node.textContent || '').trim(),
                                id: node.getAttribute('data-id') || node.getAttribute('data-value') ||
                                    node.getAttribute('value') || node.id || ''
                            })).filter(item => item.label)"""
                        )
                        texts = [str(item.get("label") or "") for item in city_options]
                        city_id = str(((draft_data or {}).get("personal") or {}).get("hh_city_id") or "")
                        by_id = [
                            i for i, item in enumerate(city_options)
                            if city_id and str(item.get("id") or "") == city_id
                        ]
                        exact = [i for i, text in enumerate(texts) if text.casefold() == resume.city.casefold()]
                        if by_id:
                            await human_click(page, options.nth(by_id[0]))
                        elif exact:
                            await human_click(page, options.nth(exact[0]))
                        elif len(texts) == 1:
                            await human_click(page, options.first)
                        else:
                            return {
                                "status": "NEEDS_ACTION",
                                "code": "AMBIGUOUS_CITY",
                                "stage": "PERSONAL",
                                "message": "Город неоднозначен. Подтвердите вариант.",
                                "options": city_options[:20],
                            }
                    try:
                        birth = date.fromisoformat(resume.birth_date)
                    except ValueError:
                        return await self._form_failure(page, "PERSONAL", "Укажите точную дату рождения.")
                    birthday = page.locator('input[name="birthday"], input[type="date"]').first
                    if await _visible(birthday):
                        if replace_profile_values:
                            await self._replace_value(page, birthday, resume.birth_date)
                        else:
                            await self._fill_empty(page, birthday, resume.birth_date)
                    else:
                        fill_value = self._replace_value if replace_profile_values else self._fill_empty
                        await fill_value(
                            page,
                            page.locator('[data-qa="resume-person-birth-day"], input[name*="birthDay"]').first,
                            str(birth.day),
                        )
                        await fill_value(
                            page,
                            page.locator('[data-qa="resume-person-birth-year"], input[name*="birthYear"]').first,
                            str(birth.year),
                        )
                        month = page.locator(
                            '[data-qa="resume-person-birth-month"], select[name*="birthMonth"]'
                        ).first
                        if await _visible(month) and (
                            replace_profile_values or not (await month.input_value()).strip()
                        ):
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
                    if not await self._click_continue(
                        page, scope=first_name, stage="PERSONAL"
                    ):
                        return await self._form_failure(page, "PERSONAL", "Не удалось сохранить личные данные.")
                    continue

                phone = page.locator('input[type="tel"], input[name="phone"]').first
                email = page.locator('input[type="email"], input[name="email"]').first
                if await _visible(phone) or await _visible(email):
                    current_stage = "CONTACTS"
                    visited_screens.append("contacts")
                    contacts = (draft_data or {}).get("contacts") or {}
                    for locator, value in (
                        (phone, str(contacts.get("phone") or "")),
                        (email, str(contacts.get("email") or "")),
                    ):
                        if replace_profile_values and value and await _visible(locator):
                            await self._replace_value(page, locator, value)
                        else:
                            await self._fill_empty(page, locator, value)
                    telegram = page.locator('input[name*="telegram" i], [data-qa*="telegram" i] input').first
                    await self._fill_empty(page, telegram, str(contacts.get("telegram") or ""))
                    for value in [contacts.get("preferred"), *(contacts.get("methods") or [])]:
                        if value:
                            choice = page.get_by_text(str(value), exact=True).first
                            if await _visible(choice):
                                await human_click(page, choice)
                    scope = phone if await _visible(phone) else email
                    if not await self._click_continue(page, scope=scope, stage="CONTACTS"):
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
                    currency_value = str(conditions.get("currency") or "")
                    currency = page.locator(
                        '[data-qa*="currency" i] select, select[name*="currency" i], '
                        '[data-qa*="salary" i] select'
                    ).first
                    if currency_value and await _visible(currency):
                        await self._replace_value(page, currency, currency_value)
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
                    if not await self._click_continue(
                        page, scope=salary, stage="CONDITIONS"
                    ):
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
                        return await self._form_changed(page, "experience-list", visited_screens)
                    continue

                experience_field = page.locator(
                    '[data-qa^="resume-profile-experience-specific-company-input"], input[name="company"], '
                    'input[placeholder*="Компания"]'
                ).first
                if await _visible(experience_field):
                    current_stage = "EXPERIENCE"
                    visited_screens.append("experience")
                    if experience_index >= len(resume.experiences):
                        if not await self._click_continue(
                            page, scope=experience_field, stage="EXPERIENCE"
                        ):
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
                    if not await self._click_continue(
                        page, scope=experience_field, stage="EXPERIENCE"
                    ):
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
                        return await self._form_changed(page, "education-list", visited_screens)
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
                            (
                                page.locator(
                                    '[data-qa*="education-level" i] select, select[name="level"], '
                                    '[data-qa*="education-level" i] input, input[name="level"]'
                                ).first,
                                item.level,
                            ),
                        ):
                            await self._fill_empty(page, locator, str(value or ""))
                    if not await self._click_continue(
                        page, scope=institution, stage="EDUCATION"
                    ):
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
                    if not await self._click_continue(
                        page, scope=language_input, stage="LANGUAGES"
                    ):
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
                    if not await self._click_continue(page, scope=link_input, stage="LINKS"):
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
                    if not await self._click_continue(
                        page, scope=detail_input, stage=key.upper()
                    ):
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
                    if not await self._click_continue(
                        page, scope=driving_block, stage="DRIVING"
                    ):
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
                    if not await self._click_continue(
                        page, scope=skill_level_heading, stage="SKILL_LEVELS"
                    ):
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
                    if not await self._click_continue(
                        page, scope=skill_input, stage="SKILLS"
                    ):
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
                    if not await self._click_continue(page, scope=about, stage="ABOUT"):
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
                        if not await _visible(visibility_option):
                            return {
                                "status": "NEEDS_ACTION",
                                "code": "VISIBILITY_NOT_FOUND",
                                "stage": "PUBLISH",
                                "message": "Не удалось выбрать подтверждённую видимость резюме. Публикация остановлена.",
                                "required_action": "SELECT_VISIBILITY",
                            }
                        await human_click(page, visibility_option)
                        visibility_confirmed = await visibility_option.evaluate(
                            """element => {
                                const own = element.matches('input[type=radio],input[type=checkbox]') ? element : null;
                                const nested = element.querySelector?.('input[type=radio],input[type=checkbox]');
                                const label = element.closest?.('label');
                                const labelled = label?.control || label?.querySelector?.('input[type=radio],input[type=checkbox]');
                                const control = own || nested || labelled;
                                if (control) return Boolean(control.checked);
                                const selected = element.getAttribute('aria-selected');
                                const checked = element.getAttribute('aria-checked');
                                return selected === 'true' || checked === 'true';
                            }"""
                        )
                        if not visibility_confirmed:
                            return {
                                "status": "NEEDS_ACTION",
                                "code": "VISIBILITY_NOT_CONFIRMED",
                                "stage": "PUBLISH",
                                "message": "hh.ru не подтвердил выбранную видимость. Публикация остановлена.",
                                "required_action": "SELECT_VISIBILITY",
                            }
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
                # React briefly removes the old step before hydrating the next
                # one.  Treating that loading frame as a changed hh.ru form
                # made otherwise valid imports stop at random transitions.
                if await _wait_for_resume_page_signal(page):
                    continue
                return await self._form_changed(page, "unknown", visited_screens)
            return await self._form_changed(page, "loop-limit", visited_screens)
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
    async def _form_changed(
        page: Page, screen: str, recognized_screens: list[str] | None = None
    ) -> dict[str, Any]:
        diagnostic = await _resume_screen_diagnostic(page)
        logger.warning(
            "Resume wizard form changed screen=%s path=%s frames=%s recognized=%s "
            "wizard=%s diagnostic_failures=%s network=%s controls=%s",
            screen,
            diagnostic["path"],
            diagnostic["frames"],
            list(recognized_screens or []),
            diagnostic["wizard"],
            diagnostic["diagnostic_failures"],
            diagnostic["network"],
            diagnostic["controls"],
        )
        return {
            "status": "NEEDS_ACTION",
            "code": "HH_FORM_CHANGED",
            "stage": "HH_WIZARD",
            "recognized_screen": screen,
            "message": "Разметка мастера hh.ru изменилась. Черновик сохранён; случайные кнопки не нажимались.",
            "required_action": "RETRY_AFTER_UPDATE",
            "retryable": True,
            "recognized_screens": list(recognized_screens or []),
            "diagnostic": diagnostic,
        }

    @staticmethod
    async def _form_failure(page: Page, stage: str, fallback: str) -> dict[str, Any]:
        error_locator = page.locator(_VALIDATION_SELECTOR)
        diagnostic = await _resume_screen_diagnostic(page)
        try:
            visible_errors = await error_locator.evaluate_all(
                r"""nodes => nodes.filter((node) => {
                    if (node.closest('[hidden], [aria-hidden="true"]')) return false;
                    const style = getComputedStyle(node);
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                        node.getClientRects().length > 0;
                }).slice(0, 20).map((node) => ({
                    message: (node.innerText || '').trim(),
                    tag: node.tagName.toLowerCase(),
                    qa: (node.getAttribute('data-qa') || '').slice(0, 100),
                    role: (node.getAttribute('role') || '').slice(0, 40),
                    invalid: (node.getAttribute('aria-invalid') || '').slice(0, 10)
                }))"""
            )
        except Exception:
            visible_errors = []
        messages = list(
            dict.fromkeys(
                str(item.get("message") or "").strip()
                for item in visible_errors
                if str(item.get("message") or "").strip()
            )
        )
        error_controls = [
            {key: value for key, value in item.items() if key != "message"}
            for item in visible_errors
        ]
        logger.warning(
            "HH_RESUME event=step_failed stage=%s path=%s frames=%s "
            "validation_count=%s validation_controls=%s wizard=%s "
            "diagnostic_failures=%s network=%s controls=%s",
            stage,
            diagnostic["path"],
            diagnostic["frames"],
            len(error_controls),
            error_controls,
            diagnostic["wizard"],
            diagnostic["diagnostic_failures"],
            diagnostic["network"],
            diagnostic["controls"],
        )
        return {
            "status": "NEEDS_INPUT",
            "code": "HH_VALIDATION_ERROR",
            "stage": stage,
            "message": messages[0] if messages else fallback,
            "field_errors": messages,
            "required_action": "EDIT_DRAFT",
        }

    @staticmethod
    async def _click_continue(
        page: Page,
        scope: Locator | None = None,
        *,
        stage: str = "HH_WIZARD",
    ) -> bool:
        root = page.locator("body")
        if scope is not None:
            form = scope.locator("xpath=ancestor::form[1]")
            if await form.count() > 0:
                root = form
        button = root.locator("[data-qa=professional-role-submit]").filter(visible=True).first
        if stage != "PROFESSION" or not await _visible(button):
            button = root.locator(
                '[data-qa="resume-submit"], [data-qa="professional-role-submit"], '
            'button:has-text("Сохранить и продолжить"), button:has-text("Продолжить"), '
            'button:has-text("Далее"), button[type="submit"]'
            ).filter(visible=True).first
        if not await _wait_visible(button):
            diagnostic = await _resume_screen_diagnostic(page)
            logger.warning(
                "HH_RESUME event=continue_control_missing stage=%s path=%s "
                "frames=%s wizard=%s diagnostic_failures=%s network=%s controls=%s",
                stage,
                diagnostic["path"],
                diagnostic["frames"],
                diagnostic["wizard"],
                diagnostic["diagnostic_failures"],
                diagnostic["network"],
                diagnostic["controls"],
            )
            return False
        previous_url = page.url
        scope_handle = await scope.element_handle() if scope is not None else None
        button_handle = await button.element_handle()
        logger.info(
            "HH_RESUME event=continue_click stage=%s path=%s button_qa=%s button_type=%s",
            stage,
            _safe_page_path(page),
            str(await button.get_attribute("data-qa") or "")[:100],
            str(await button.get_attribute("type") or "")[:40],
        )
        try:
            await human_click(page, button)
        except HumanizationError as exc:
            diagnostic = await _resume_screen_diagnostic(page)
            logger.warning(
                "HH_RESUME event=continue_click_failed stage=%s operation=%s reason=%s "
                "path=%s frames=%s wizard=%s diagnostic_failures=%s network=%s controls=%s",
                stage,
                exc.operation,
                exc.reason,
                diagnostic["path"],
                diagnostic["frames"],
                diagnostic["wizard"],
                diagnostic["diagnostic_failures"],
                diagnostic["network"],
                diagnostic["controls"],
            )
            return False
        try:
            transition = await page.wait_for_function(
                r"""args => {
                    const visible = (node) => {
                        if (!node || !node.isConnected ||
                            node.closest('[hidden], [aria-hidden="true"]')) return false;
                        const style = getComputedStyle(node);
                        return style.display !== 'none' && style.visibility !== 'hidden' &&
                            node.getClientRects().length > 0;
                    };
                    const invalid = [...document.querySelectorAll(
                        `[aria-invalid="true"], [aria-errormessage],
                         [data-qa*="error" i], [data-qa*="validation" i], [role="alert"],
                         form [class*="error" i]`
                    )].some(visible) || [...document.querySelectorAll(
                        'form input, form textarea, form select'
                    )].some((node) => visible(node) && typeof node.checkValidity === 'function' &&
                        !node.checkValidity());
                    if (invalid) return 'validation';
                    if (location.href !== args.previousUrl ||
                        (args.scope && !visible(args.scope)) ||
                        (args.button && !visible(args.button))) return 'transition';
                    return false;
                }""",
                arg={
                    "previousUrl": previous_url,
                    "scope": scope_handle,
                    "button": button_handle,
                },
                timeout=DEFAULT_TRANSITION_TIMEOUT_MS,
            )
            outcome = await transition.json_value()
        except Exception:
            diagnostic = await _resume_screen_diagnostic(page)
            logger.warning(
                "HH_RESUME event=continue_no_transition stage=%s path=%s "
                "frames=%s wizard=%s diagnostic_failures=%s network=%s controls=%s",
                stage,
                diagnostic["path"],
                diagnostic["frames"],
                diagnostic["wizard"],
                diagnostic["diagnostic_failures"],
                diagnostic["network"],
                diagnostic["controls"],
            )
            return False
        if outcome == "validation":
            diagnostic = await _resume_screen_diagnostic(page)
            logger.warning(
                "HH_RESUME event=continue_validation_error stage=%s path=%s "
                "frames=%s wizard=%s diagnostic_failures=%s network=%s controls=%s",
                stage,
                diagnostic["path"],
                diagnostic["frames"],
                diagnostic["wizard"],
                diagnostic["diagnostic_failures"],
                diagnostic["network"],
                diagnostic["controls"],
            )
            return False
        logger.info(
            "HH_RESUME event=continue_transition_confirmed stage=%s from_path=%s to_path=%s",
            stage,
            _safe_url_path(previous_url),
            _safe_page_path(page),
        )
        return True

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
