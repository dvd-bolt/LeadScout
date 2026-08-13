"""Validation and safe rendering helpers shared by handlers and workers."""

from __future__ import annotations

import html
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

HH_VACANCY_PATH = re.compile(r"^/vacancy/(\d+)(?:/)?$")
TELEGRAM_HTML_TAG = re.compile(
    r"</?(?:b|strong|i|em|u|ins|s|strike|del|span|tg-spoiler|a|code|pre|blockquote)"
    r"(?:\s+[^>]*)?>",
    re.IGNORECASE,
)


def escape_html(value: object) -> str:
    return html.escape(str(value), quote=True)


def strip_telegram_html(value: str) -> str:
    """Convert our limited Telegram HTML output into safe plain text."""
    return html.unescape(TELEGRAM_HTML_TAG.sub("", value))


def parse_callback_id(data: str | None, prefix: str) -> int | None:
    if not data or not data.startswith(prefix):
        return None
    raw = data[len(prefix):]
    if not raw.isascii() or not raw.isdigit():
        return None
    value = int(raw)
    return value if value > 0 else None


def normalize_proxy_url(raw: str) -> str:
    value = raw.strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https", "socks5"}:
        raise ValueError("Допустимы прокси http, https и socks5")
    if not parsed.hostname or parsed.port is None:
        raise ValueError("Укажите хост и порт прокси")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("URL прокси не должен содержать путь, query или fragment")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    credentials = ""
    if parsed.username is not None:
        credentials = quote(unquote(parsed.username), safe="")
        if parsed.password is not None:
            credentials += ":" + quote(unquote(parsed.password), safe="")
        credentials += "@"
    return f"{parsed.scheme.lower()}://{credentials}{host}:{parsed.port}"


def mask_proxy_url(raw: str | None) -> str:
    if not raw:
        return "Не задан"
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or "?"
        port = f":{parsed.port}" if parsed.port else ""
        auth = "***:***@" if parsed.username is not None else ""
        return f"{parsed.scheme}://{auth}{host}{port}"
    except ValueError:
        return "Некорректный URL"


def normalize_hh_vacancy_url(raw: str) -> str | None:
    try:
        parsed = urlsplit(raw.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}:
        return None
    if host != "hh.ru" and not host.endswith(".hh.ru"):
        return None
    match = HH_VACANCY_PATH.fullmatch(parsed.path)
    if not match:
        return None
    return urlunsplit(("https", host, f"/vacancy/{match.group(1)}", "", ""))


def split_text(text: str, limit: int = 4096) -> list[str]:
    """Split plain text at line/space boundaries without dropping content."""
    if limit < 32:
        raise ValueError("limit is too small")
    remaining = text
    chunks: list[str] = []
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining or not chunks:
        chunks.append(remaining)
    return chunks
