"""Безопасность: сессионные cookie, шифрование реквизитов БК в базе и
заголовки, которыми браузер защищает страницу от подмены и перехвата.

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
import ipaddress
import json
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

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
    # Сравниваем БАЙТЫ: compare_digest на строках с не-ASCII (а пароль
    # вполне может быть русским) падает с TypeError, и вместо «неверный
    # пароль» пользователь получал бы ошибку сервера.
    return (hmac.compare_digest(username.encode("utf-8"),
                                config.ADMIN_USERNAME.encode("utf-8"))
            and hmac.compare_digest(password.encode("utf-8"),
                                    config.ADMIN_PASSWORD.encode("utf-8")))


# ---------------------------------------------------------------------------
# Пароль доступа к сайту (задаётся в админке, хранится в базе)
# ---------------------------------------------------------------------------

_PBKDF2_ROUNDS = 200_000


def hash_password(plain: str) -> str:
    """Хэш пароля для хранения в базе: PBKDF2-HMAC-SHA256 со случайной солью.

    Пароль доступа к сайту оператор задаёт в админке, поэтому в открытом
    виде он не хранится нигде — в базе лежит только хэш, а проверка идёт
    через compare_digest (см. verify_password_hash)."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt,
                             _PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ROUNDS, base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(dk).decode("ascii"))


def verify_password_hash(stored: str, plain: str) -> bool:
    try:
        algo, rounds, salt_b64, dk_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(dk_b64.encode("ascii"))
    except (ValueError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, int(rounds))
    return hmac.compare_digest(dk, expected)


def _sign(payload: str) -> str:
    sig = hmac.new(_SECRET.encode("utf-8"), payload.encode("utf-8"),
                   hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------
# Cookie и заголовки безопасности
# ---------------------------------------------------------------------------

def _proxy_header(request, name: str) -> str:
    """Значение заголовка от обратного прокси — только если запрос пришёл
    из локальной сети, то есть от нашего же nginx. Иначе посетитель мог бы
    объявить своё соединение защищённым и получить cookie без флага
    Secure (см. app/access.py::client_ip — там та же логика для адреса)."""
    if not config.SITE_TRUST_PROXY:
        return ""
    peer = getattr(getattr(request, "client", None), "host", "") or ""
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return ""
    if not (addr.is_loopback or addr.is_private):
        return ""
    return request.headers.get(name, "").split(",")[0].strip()


def is_https(request) -> bool:
    """Пришёл ли запрос по HTTPS (с учётом обратного прокси)."""
    if _proxy_header(request, "x-forwarded-proto").lower() == "https":
        return True
    return getattr(getattr(request, "url", None), "scheme", "") == "https"


def cookie_secure(request) -> bool:
    """Ставить ли флаг Secure (cookie только по HTTPS).

    По умолчанию — «auto»: по HTTPS ставим, по HTTP нет. Иначе на сервере
    без сертификата (первый запуск, доступ по IP) cookie не доехала бы до
    браузера вовсе и войти было бы невозможно. COOKIE_SECURE=1 включает
    флаг всегда — так и надо делать, когда домен и сертификат уже есть
    (см. deploy/setup_domain.sh)."""
    mode = config.COOKIE_SECURE
    if mode in ("1", "true", "yes", "always"):
        return True
    if mode in ("0", "false", "no", "never"):
        return False
    return is_https(request)


def set_session_cookie(response, request, name: str, value: str, *,
                       ttl: float, samesite: str = "lax") -> None:
    """Ставит сессионную cookie с полным набором защит.

    HttpOnly — чтобы её не достал скрипт со страницы (XSS не превращается
    в угон сессии), SameSite — чтобы браузер не отправлял её по запросу с
    чужого сайта (CSRF), Secure — чтобы она не ушла по открытому HTTP,
    где её можно перехватить."""
    response.set_cookie(name, value, max_age=int(ttl), httponly=True,
                        samesite=samesite, secure=cookie_secure(request),
                        path="/")


def clear_session_cookie(response, name: str) -> None:
    response.delete_cookie(name, path="/")


def security_headers(request, *, api: bool) -> dict:
    """Заголовки безопасности ответа.

    - HSTS велит браузеру ходить на домен только по HTTPS (даже если
      человек набрал адрес руками) — ставим только на HTTPS-ответах,
      иначе заперли бы сами себя на сервере без сертификата;
    - CSP запрещает грузить чужие скрипты и встраивать сайт в рамку: свои
      скрипты и стили лежат в /static, внешних источников у сайта нет;
    - no-store на ответы API: вилки, балансы и списки игроков не должны
      оставаться ни в кэше браузера, ни у прокси по пути.
    """
    headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Content-Security-Policy": (
            "default-src 'self'; "
            # инлайновые иконки страниц — data:-URL со svg
            "img-src 'self' data:; "
            "style-src 'self'; "
            "script-src 'self'; "
            # звук уведомления генерируется WebAudio, внешних медиа нет
            "media-src 'self'; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "form-action 'self'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'; "
            "object-src 'none'"),
    }
    if is_https(request):
        headers["Strict-Transport-Security"] = \
            f"max-age={int(config.HSTS_MAX_AGE)}; includeSubDomains"
    if api:
        headers["Cache-Control"] = "no-store"
        headers["Pragma"] = "no-cache"
    return headers


def same_origin(request) -> bool:
    """Пришёл ли изменяющий запрос с нашей же страницы.

    Заголовок Origin браузер подставляет сам и подделать его со страницы
    нельзя. Ни Origin, ни Referer нет — это не браузер (curl, systemd,
    мобильное приложение), и подделывать нечего: там нет чужой страницы,
    которая могла бы отправить запрос вашей cookie."""
    origin = request.headers.get("origin") or ""
    if not origin:
        referer = request.headers.get("referer") or ""
        if not referer:
            return True
        origin = referer
    try:
        host = urlsplit(origin).netloc.lower()
    except ValueError:
        return False
    if not host:
        return False
    allowed = {(request.headers.get("host") or "").lower()}
    allowed |= {h.lower() for h in config.TRUSTED_HOSTS}
    # сравниваем и с портом, и без него: за nginx браузер видит 443, а в
    # заголовке Host порт может отсутствовать
    bare = {h.split(":")[0] for h in allowed if h}
    return host in allowed or host.split(":")[0] in bare


def create_session_token(username: str, ttl: float | None = None) -> str:
    body = json.dumps(
        {"u": username, "exp": time.time() + (ttl or config.ADMIN_SESSION_TTL)},
        separators=(",", ":"))
    body_b64 = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
    return f"{body_b64}.{_sign(body_b64)}"


def verify_session_token(token: str | None) -> str | None:
    """Возвращает логин пользователя, если токен валиден и не истёк."""
    if not token or "." not in token:
        return None
    body_b64, sig = token.rsplit(".", 1)
    # cookie приходит от клиента, там может быть что угодно, включая
    # не-ASCII: на строках compare_digest в таком случае бросает TypeError,
    # и битая cookie валила бы запрос с ошибкой сервера вместо «войдите».
    if not hmac.compare_digest(sig.encode("utf-8", "replace"),
                               _sign(body_b64).encode("ascii")):
        return None
    try:
        body = json.loads(base64.urlsafe_b64decode(body_b64.encode("ascii")))
    except (ValueError, UnicodeDecodeError):
        return None
    if body.get("exp", 0) < time.time():
        return None
    username = body.get("u")
    return username if isinstance(username, str) else None
