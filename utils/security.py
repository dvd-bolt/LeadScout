"""
LeadScout AI — Модуль шифрования сессий (Fernet AES-256).
Обеспечивает защищенное хранение кук и LocalStorage пользователей в базе данных.
"""

import base64
import hashlib
import json
import logging
from cryptography.fernet import Fernet
from config import SESSION_ENCRYPTION_KEY

logger = logging.getLogger(__name__)


class SessionSecurityManager:
    """Менеджер шифрования и расшифровки браузерных сессий."""

    def __init__(self, key_str: str = SESSION_ENCRYPTION_KEY):
        cleaned_key = key_str.strip() if key_str else ""
        if not cleaned_key:
            # Предупреждение в лог, если SESSION_ENCRYPTION_KEY не задан в .env
            logger.warning("SESSION_ENCRYPTION_KEY не задан в .env! Инициализируется резервный сессионный ключ.")
            cleaned_key = "LeadScout_AI_Secure_Default_Session_Key_2026"

        if cleaned_key.startswith("b'") and cleaned_key.endswith("'"):
            cleaned_key = cleaned_key[2:-1]

        try:
            self.cipher = Fernet(cleaned_key.encode("utf-8"))
        except Exception:
            # Создаем устойчивый 32-байтовый Fernet-ключ через SHA-256
            hashed = hashlib.sha256(cleaned_key.encode("utf-8")).digest()
            valid_fernet_key = base64.urlsafe_b64encode(hashed)
            self.cipher = Fernet(valid_fernet_key)

    def encrypt_storage_state(self, state_dict: dict) -> bytes:
        """Шифрует словарь storage_state (cookies + localStorage) в байты."""
        raw_json = json.dumps(state_dict).encode("utf-8")
        return self.cipher.encrypt(raw_json)

    def decrypt_storage_state(self, encrypted_data: bytes) -> dict:
        """Расшифровывает байты в словарь storage_state."""
        decrypted_json = self.cipher.decrypt(encrypted_data).decode("utf-8")
        return json.loads(decrypted_json)
