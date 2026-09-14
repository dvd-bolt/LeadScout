"""Canonical account identifiers shared by storage and migrations."""

import re

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_hh_login(value: str) -> str:
    """Return a canonical login accepted by the hh.ru interactive flow.

    Phone input on hh.ru uses a Russian national-number field.  Canonicalising
    here keeps account matching and the browser flow consistent, and rejects
    impossible input before a browser slot is acquired.
    """

    raw = "".join(str(value or "").split())
    if "@" in raw:
        if not _EMAIL_RE.fullmatch(raw):
            raise ValueError("Введите корректный email или российский номер телефона.")
        return raw

    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        national = digits
    elif len(digits) == 11 and digits.startswith(("7", "8")):
        national = digits[1:]
    else:
        raise ValueError("Введите российский номер в формате +7XXXXXXXXXX, 8XXXXXXXXXX или XXXXXXXXXX.")
    return f"+7{national}"


def hh_national_phone(value: str) -> str:
    """Extract the exactly ten digits expected by hh.ru's phone control."""

    canonical = validate_hh_login(value)
    if "@" in canonical:
        raise ValueError("Email does not have a national phone representation.")
    return canonical[-10:]


def normalize_login(value: str) -> str:
    cleaned = value.strip().lower()
    if "@" in cleaned:
        return cleaned
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) == 10:
        return "7" + digits
    if len(digits) == 11 and digits.startswith("8"):
        return "7" + digits[1:]
    return digits
