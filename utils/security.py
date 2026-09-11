"""Encryption helpers for persisted browser sessions."""

from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken

from leadscout.core.config import SESSION_ENCRYPTION_KEY


class SessionDecryptionError(ValueError):
    """The stored session cannot be decrypted with the configured key."""


class SessionSecurityManager:
    def __init__(self, key_str: str | None = None):
        key = (key_str if key_str is not None else SESSION_ENCRYPTION_KEY).strip()
        if not key:
            raise ValueError("SESSION_ENCRYPTION_KEY is required")
        try:
            self.cipher = Fernet(key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise ValueError("SESSION_ENCRYPTION_KEY must be a valid Fernet key") from exc

    def encrypt_storage_state(self, state_dict: dict) -> bytes:
        raw_json = json.dumps(state_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self.cipher.encrypt(raw_json)

    def decrypt_storage_state(self, encrypted_data: bytes) -> dict:
        if not encrypted_data:
            raise SessionDecryptionError("Stored session is empty")
        try:
            decoded = self.cipher.decrypt(encrypted_data).decode("utf-8")
            value = json.loads(decoded)
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionDecryptionError("Stored session is invalid or encrypted with another key") from exc
        if not isinstance(value, dict):
            raise SessionDecryptionError("Stored session has an invalid format")
        return value
