"""Закрытый доступ к сайту: пароль на входе и белый список IP.

Сканер стоит на VPS с публичным адресом, и без защиты его видит любой, кто
знает IP: линия всех БК, найденные вилки, а по ссылке /admin — ещё и
аккаунты. Поэтому весь сайт закрывается «шлюзом» (middleware в app/main.py):

- пароль доступа задаёт оператор в админке, в базе хранится только его хэш
  (PBKDF2, см. security.hash_password); начальное значение можно передать
  переменной окружения SITE_PASSWORD;
- белый список IP/подсетей пускает свои адреса (дом, офис, VPN) БЕЗ пароля;
- строгий режим (ip_only) пускает ТОЛЬКО адреса из списка и не показывает
  форму пароля вообще — для случая «сайт вижу только я со своего адреса»;
- чёрный список (ip_banned) не пускает конкретные адреса ВСЕГДА, даже когда
  сайт в остальном открыт: выгнать одного посетителя нужно уметь, не
  закрывая сайт от всех остальных (список наполняется кнопкой «Забанить IP»
  в разделе админки «Кто на сайте», см. app/visitors.py).

Если пароль не задан И список пуст, сайт остаётся открытым (при старте в
лог уходит предупреждение) — иначе первый же запуск запирал бы оператора
снаружи.

Пароль админки тоже подходит для входа на сайт: держать в голове два
пароля незачем, а права админки заведомо шире.
"""
import ipaddress
import logging
import threading
import time

from fastapi import Request

from . import config, db
from .security import (create_session_token, hash_password,
                       verify_admin_password, verify_password_hash,
                       verify_session_token)

log = logging.getLogger("access")

COOKIE_NAME = "site_session"

_KEY_PASSWORD = "site_password_hash"
_KEY_WHITELIST = "site_ip_whitelist"
_KEY_IP_ONLY = "site_ip_only"
_KEY_BLACKLIST = "site_ip_blacklist"

# IP → [время неудачных попыток]. Защита от перебора пароля: словарь живёт
# в памяти процесса, после перезапуска счётчики обнуляются (этого хватает —
# перебор идёт минутами, а не сутками).
_fails: dict[str, list[float]] = {}
_fails_lock = threading.Lock()

# Настройки шлюза читаются на КАЖДЫЙ запрос (включая опрос вилок раз в 5 с
# каждой открытой вкладкой), поэтому значения из базы кэшируются на пару
# секунд; любое изменение из админки кэш сбрасывает.
_CACHE_TTL = 2.0
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def _cached(key: str, loader):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_TTL:
            return hit[1]
    value = loader()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def invalidate_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# Пароль
# ---------------------------------------------------------------------------

def password_hash() -> str | None:
    """Хэш пароля доступа из базы (заданный в админке)."""
    return _cached(_KEY_PASSWORD, lambda: db.get_setting(_KEY_PASSWORD))


def password_set() -> bool:
    return bool(password_hash() or config.SITE_PASSWORD)


def password_source() -> str:
    """Откуда взят пароль: «db» (задан в админке), «env» или «none»."""
    if password_hash():
        return "db"
    return "env" if config.SITE_PASSWORD else "none"


def set_password(plain: str) -> None:
    db.set_setting(_KEY_PASSWORD, hash_password(plain))
    invalidate_cache()


def clear_password() -> None:
    """Убирает пароль, заданный в админке (вернётся SITE_PASSWORD, если он
    задан в окружении)."""
    db.set_setting(_KEY_PASSWORD, "")
    invalidate_cache()


def check_password(plain: str) -> bool:
    """Подходит ли пароль: заданный в админке, из окружения или админский."""
    if not plain:
        return False
    stored = password_hash()
    if stored:
        if verify_password_hash(stored, plain):
            return True
    elif config.SITE_PASSWORD and plain == config.SITE_PASSWORD:
        return True
    # пароль админки открывает и сайт: два пароля помнить незачем
    return verify_admin_password(config.ADMIN_USERNAME, plain)


# ---------------------------------------------------------------------------
# Белый список IP
# ---------------------------------------------------------------------------

def parse_whitelist(text: str) -> list[str]:
    """Разбирает список адресов/подсетей, отбрасывая мусор и повторы.

    Принимает и запятые, и переводы строк, и пробелы; одиночный адрес
    превращается в /32 (или /128 для IPv6) — так его удобно сравнивать
    с адресом клиента."""
    out: list[str] = []
    for chunk in str(text or "").replace(",", "\n").replace(";", "\n").split():
        try:
            net = ipaddress.ip_network(chunk.strip(), strict=False)
        except ValueError:
            raise ValueError(f"«{chunk}» не похоже на IP или подсеть "
                             f"(примеры: 203.0.113.7, 198.51.100.0/24)")
        text_net = str(net)
        if text_net not in out:
            out.append(text_net)
    return out


def whitelist() -> list[str]:
    """Действующий белый список (из базы, иначе из переменной окружения)."""
    def load() -> list[str]:
        raw = db.get_setting(_KEY_WHITELIST)
        if raw is None:
            raw = config.SITE_IP_WHITELIST
        try:
            return parse_whitelist(raw)
        except ValueError as exc:
            log.warning("Белый список IP игнорируется: %s", exc)
            return []
    return _cached(_KEY_WHITELIST, load)


def set_whitelist(text: str) -> list[str]:
    entries = parse_whitelist(text)
    db.set_setting(_KEY_WHITELIST, "\n".join(entries))
    invalidate_cache()
    return entries


def ip_blacklist() -> list[str]:
    """Забаненные адреса и подсети (кнопка «Забанить IP» в разделе
    «Кто на сайте», см. app/visitors.py)."""
    def load() -> list[str]:
        raw = db.get_setting(_KEY_BLACKLIST)
        if raw is None:
            raw = config.SITE_IP_BLACKLIST
        try:
            return parse_whitelist(raw)
        except ValueError as exc:
            log.warning("Чёрный список IP игнорируется: %s", exc)
            return []
    return _cached(_KEY_BLACKLIST, load)


def set_ip_blacklist(text: str) -> list[str]:
    entries = parse_whitelist(text)
    db.set_setting(_KEY_BLACKLIST, "\n".join(entries))
    invalidate_cache()
    return entries


def ip_banned(ip: str) -> bool:
    return ip_in_whitelist(ip, ip_blacklist())


def ip_only() -> bool:
    """Строгий режим: пускать только адреса из белого списка."""
    return bool(_cached(
        _KEY_IP_ONLY,
        lambda: db.get_bool_setting(_KEY_IP_ONLY, config.SITE_IP_ONLY)))


def set_ip_only(value: bool) -> None:
    db.set_bool_setting(_KEY_IP_ONLY, value)
    invalidate_cache()


def ip_in_whitelist(ip: str, entries: list[str] | None = None) -> bool:
    entries = whitelist() if entries is None else entries
    if not entries:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in ipaddress.ip_network(e) for e in entries)


def _is_local(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private


def client_ip(request: Request) -> str:
    """Адрес клиента с учётом обратного прокси.

    X-Forwarded-For учитывается, только если запрос ПРИШЁЛ из локальной
    сети — то есть от нашего же nginx (deploy/nginx.conf). Иначе любой
    посетитель, ткнувшись в uvicorn напрямую, подобрал бы себе адрес из
    белого списка одним заголовком. В цепочке берём ПОСЛЕДНИЙ адрес: его
    добавил сам nginx, а всё, что прислал клиент, стоит перед ним."""
    peer = request.client.host if request.client else ""
    if not config.SITE_TRUST_PROXY or not _is_local(peer):
        return peer
    chain = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in chain.split(",") if p.strip()]
    if parts:
        return parts[-1]
    real = request.headers.get("x-real-ip", "").strip()
    return real or peer


# ---------------------------------------------------------------------------
# Состояние шлюза и проверка запроса
# ---------------------------------------------------------------------------

def gate_enabled() -> bool:
    """Закрыт ли сайт вообще (иначе — открыт всем, как раньше)."""
    return bool(password_set() or whitelist())


def session_cookie(username: str = "site") -> str:
    return create_session_token(username, ttl=config.SITE_SESSION_TTL)


def valid_session(request: Request) -> bool:
    """Есть ли у запроса действующая сессия сайта ИЛИ админки."""
    return bool(verify_session_token(request.cookies.get(COOKIE_NAME))
                or admin_session(request))


def admin_session(request: Request) -> bool:
    """Есть ли действующая сессия АДМИНКИ.

    Она обходит любые баны (и по устройству, и по адресу): иначе оператор,
    забанивший сам себя с чужого адреса, остался бы снаружи без ssh."""
    from .admin_api import COOKIE_NAME as ADMIN_COOKIE
    return bool(verify_session_token(request.cookies.get(ADMIN_COOKIE)))


def check_request(request: Request) -> tuple[bool, str]:
    """Пускать ли запрос. Возвращает (можно, причина отказа).

    Причина: "" — пускаем, "banned" — адрес в чёрном списке, "ip" —
    строгий режим и адрес не в списке, "password" — нужен пароль."""
    ip = client_ip(request)
    # Бан адреса действует, даже когда сайт в остальном открыт: выгнать
    # конкретного посетителя нужно уметь, не закрывая сайт паролем.
    if ip_banned(ip) and not admin_session(request):
        return False, "banned"
    if not gate_enabled():
        return True, ""
    entries = whitelist()
    if ip_in_whitelist(ip, entries):
        return True, ""
    if entries and ip_only():
        return False, "ip"
    if valid_session(request):
        return True, ""
    if not password_set():
        # список задан, но строгий режим выключен и пароля нет: пускать всех
        # по паролю нельзя (его нет), а запирать наглухо — не то, о чём
        # просили. Открываем: защита начнётся, как только появится пароль.
        return True, ""
    return False, "password"


# ---------------------------------------------------------------------------
# Защита от перебора пароля
# ---------------------------------------------------------------------------

def too_many_fails(ip: str) -> bool:
    now = time.monotonic()
    with _fails_lock:
        tries = [t for t in _fails.get(ip, ())
                 if now - t < config.SITE_LOGIN_FAIL_WINDOW]
        _fails[ip] = tries
        return len(tries) >= config.SITE_LOGIN_MAX_FAILS


def note_fail(ip: str) -> None:
    now = time.monotonic()
    with _fails_lock:
        tries = [t for t in _fails.get(ip, ())
                 if now - t < config.SITE_LOGIN_FAIL_WINDOW]
        tries.append(now)
        _fails[ip] = tries


def note_success(ip: str) -> None:
    with _fails_lock:
        _fails.pop(ip, None)


def state(request: Request | None = None) -> dict:
    """Состояние защиты для админки и страницы входа."""
    entries = whitelist()
    return {
        "gate_enabled": gate_enabled(),
        "password_set": password_set(),
        "password_source": password_source(),
        "whitelist": entries,
        "ip_blacklist": ip_blacklist(),
        "ip_only": bool(entries and ip_only()),
        "current_ip": client_ip(request) if request is not None else "",
        "session_ttl_days": round(config.SITE_SESSION_TTL / 86400, 1),
    }
