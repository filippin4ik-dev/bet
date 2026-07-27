"""Безопасность админки: подпись сессионных cookie и шифрование логинов/
паролей аккаунтов БК в базе.

Никакой пароль администратора или БК никогда не пишется в базу/куки
в открытом виде:
- пароль администратора сравнивается через hmac.compare_digest
  (без хранения — он либо совпадает с ADMIN_PASSWORD, либо нет);
- логин/пароль аккаунта БК шифруются Fernet (симметричное AES-based
  шифрование) ключом, производным от SECRET_KEY, перед записью в SQLite;
- сессия админки — это подписанный HMAC-токен (логин + время истечения),
  не JWT-библиотека, чтобы не тащить лишнюю зависимость.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import config

log = logging.getLogger("security")


def _load_or_create_secret() -> str:
    """SECRET_KEY из окружения, либо персистентный файл, либо генерируем."""
    if config.SECRET_KEY:
        return config.SECRET_KEY
    path = Path(config.SECRET_KEY_FILE)
    if path.exists():
        val = path.read_text(encoding="utf-8").strip()
        if val:
            return val
    val = secrets.token_urlsafe(48)
    try:
        path.write_text(val, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        log.warning(
            "SECRET_KEY не задан — сгенерирован и сохранён в %s. "
            "На проде задайте SECRET_KEY явно и не теряйте файл "
            "(иначе расшифровать сохранённые пароли БК не получится).",
            path)
    except OSError as exc:
        log.warning("Не удалось сохранить SECRET_KEY в %s (%s) — при "
                    "перезапуске сгенерируется новый, сохранённые пароли "
                    "БК придётся ввести заново.", path, exc)
    return val


_SECRET = _load_or_create_secret()


def _fernet() -> Fernet:
    key = hashlib.sha256(_SECRET.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_str(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_str(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        log.error("Не удалось расшифровать сохранённые данные аккаунта — "
                  "изменился SECRET_KEY? Значение недоступно.")
        return ""


def verify_admin_password(username: str, password: str) -> bool:
    return (hmac.compare_digest(username, config.ADMIN_USERNAME)
            and hmac.compare_digest(password, config.ADMIN_PASSWORD))


def _sign(payload: str) -> str:
    sig = hmac.new(_SECRET.encode("utf-8"), payload.encode("utf-8"),
                   hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")


def create_session_token(username: str) -> str:
    body = json.dumps(
        {"u": username, "exp": time.time() + config.ADMIN_SESSION_TTL},
        separators=(",", ":"))
    body_b64 = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
    return f"{body_b64}.{_sign(body_b64)}"


def verify_session_token(token: str | None) -> str | None:
    """Возвращает логин пользователя, если токен валиден и не истёк."""
    if not token or "." not in token:
        return None
    body_b64, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(body_b64)):
        return None
    try:
        body = json.loads(base64.urlsafe_b64decode(body_b64.encode("ascii")))
    except (ValueError, UnicodeDecodeError):
        return None
    if body.get("exp", 0) < time.time():
        return None
    username = body.get("u")
    return username if isinstance(username, str) else None
