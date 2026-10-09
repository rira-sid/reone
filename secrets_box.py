"""Encrypts seller secrets (WhatsApp tokens, Razorpay keys) before they're stored in the DB.

SECRET_KEY must be set on the server before any seller connects their accounts, and must never
change afterwards - secrets encrypted under an old key can't be read back.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    print("WARNING: SECRET_KEY is not set - using an insecure development key for stored secrets.")

_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256((SECRET_KEY or "reone-dev-key").encode()).digest()))


def encrypt(value: str | None) -> str | None:
    return _fernet.encrypt(value.encode()).decode() if value else None


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet.decrypt(value.encode()).decode()
    except InvalidToken:
        print("WARNING: could not decrypt a stored secret - was SECRET_KEY changed?")
        return None
