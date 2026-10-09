from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class TokenVault:
    def __init__(self, app_secret: str):
        key = base64.urlsafe_b64encode(hashlib.sha256(app_secret.encode("utf-8")).digest())
        self._fernet = Fernet(key)

    def encrypt(self, token: str) -> str:
        return self._fernet.encrypt(token.encode("utf-8")).decode("ascii")

    def decrypt(self, encrypted: str) -> str:
        try:
            return self._fernet.decrypt(encrypted.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("Stored bot token cannot be decrypted with the current APP_SECRET") from exc
