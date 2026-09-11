"""Small Telegram keyboards that deep-link into the Mini App."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo

from leadscout.core.config import APP_URL


def mini_app_url(target: str = "", *, app_url: str = APP_URL, **identifiers: int | None) -> str | None:
    if not app_url.startswith("https://"):
        return None
    split = urlsplit(app_url)
    query = dict(parse_qsl(split.query, keep_blank_values=True))
    if target:
        query["target"] = target
    for name in ("account_id", "apply_id", "snapshot_id"):
        value = identifiers.get(name)
        if value is not None and int(value) > 0:
            query[name] = str(int(value))
    return urlunsplit((split.scheme, split.netloc, split.path or "/", urlencode(query), split.fragment or "/"))


def get_mini_app_keyboard(target: str = "", *, app_url: str = APP_URL, **identifiers) -> InlineKeyboardMarkup | None:
    url = mini_app_url(target, app_url=app_url, **identifiers)
    if not url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть LeadScout", web_app=WebAppInfo(url=url))]]
    )


def get_questionnaire_confirmation_keyboard(
    apply_id: int, *, app_url: str = APP_URL, account_id: int | None = None
) -> InlineKeyboardMarkup | None:
    return get_mini_app_keyboard("questionnaire", app_url=app_url, apply_id=apply_id, account_id=account_id)


def get_entry_keyboard(*, app_url: str = APP_URL) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    url = mini_app_url(app_url=app_url)
    if url:
        rows.append([KeyboardButton(text="Открыть LeadScout", web_app=WebAppInfo(url=url))])
    rows.extend(
        [
            [KeyboardButton(text="⛔ Остановить"), KeyboardButton(text="❓ Помощь")],
        ]
    )
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def get_stop_keyboard(accounts: list[dict], *, app_url: str = APP_URL) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"⛔ {item.get('account_name') or item.get('phone_or_email') or item['id']}",
                callback_data=f"stop_account:{int(item['id'])}",
            )
        ]
        for item in accounts
    ]
    if len(accounts) > 1:
        rows.append([InlineKeyboardButton(text="⛔ Остановить все", callback_data="stop_all:confirm")])
    url = mini_app_url("accounts", app_url=app_url)
    if url:
        rows.append([InlineKeyboardButton(text="Управление аккаунтами", web_app=WebAppInfo(url=url))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


__all__ = [
    "get_entry_keyboard",
    "get_mini_app_keyboard",
    "get_questionnaire_confirmation_keyboard",
    "get_stop_keyboard",
    "mini_app_url",
]
