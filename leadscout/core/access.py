"""Stable access errors, role capabilities and safe identifiers."""


class AccessError(Exception):
    def __init__(self, code: str, message: str, status: int = 403):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def telegram_id(value) -> int:
    text = str(value)
    if not text.isascii() or not text.isdecimal() or not 0 < int(text) <= 2**63 - 1:
        raise AccessError("INVALID_ID", "Укажите положительный Telegram ID цифрами.", 422)
    return int(text)


def capabilities(role: str) -> list[str]:
    if role == "ROOT":
        return ["admin:view", "members:manage", "tasks:stop", "tasks:stop-root"]
    if role == "ADMIN":
        return ["admin:view", "tasks:stop"]
    return []
