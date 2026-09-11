"""Canonical account identifiers shared by storage and migrations."""

import re


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
