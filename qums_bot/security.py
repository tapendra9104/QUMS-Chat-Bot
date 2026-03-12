from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet


def _fernet(secret: str) -> Fernet:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_text(secret: str, value: str) -> str:
    if not value:
        return ""
    return _fernet(secret).encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_text(secret: str, value: str) -> str:
    if not value:
        return ""
    return _fernet(secret).decrypt(value.encode("utf-8")).decode("utf-8")
