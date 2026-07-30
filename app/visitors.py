"""Кто сидит на сайте: устройства, их адреса и бан по устройству.

Сайт стоит на публичном VPS, и «Доступ к сайту» (app/access.py) отвечает
только на вопрос «пускать или нет». Отвечать на вопрос «а кто вообще сейчас
смотрит вилки» было нечем: в логах uvicorn только IP, а с телефона он
меняется каждый день, и по нему не отличить нового человека от своей же
второй вкладки. Поэтому каждому браузеру выдаётся cookie с идентификатором
УСТРОЙСТВА, и в админке видно живой список: устройство (браузер, ОС, тип),
его адреса, когда заходило и сколько запросов сделало.

Бан. Заблокировать можно само устройство (по cookie) и/или его адрес
(чёрный список IP в app/access.py). Устройство надёжнее: у телефона адрес
плавает, а cookie переживает смену сети. Но и cookie можно почистить,
поэтому бан устройства запоминает ещё и «отпечаток» браузера (User-Agent,
язык, платформа): если из того же адреса приходит тот же самый браузер, но
уже без cookie — он блокируется автоматически как то же устройство. Это не
сейф, а именно «выставить за дверь»: настоящая защита — пароль и строгий
режим белого списка.

Хранение: реестр живёт в памяти (посещения идут пачками — каждая открытая
вкладка опрашивает сервер раз в 5 секунд, писать это в SQLite на каждый
запрос незачем) и раз в VISITORS_FLUSH_INTERVAL сбрасывается в базу.
Решение о бане, наоборот, пишется в базу сразу же: его нельзя потерять
при перезапуске.
"""
import hashlib
import logging
import re
import secrets
import threading
import time

from . import config, db
from .security import sign_value, unsign_value

log = logging.getLogger("visitors")

COOKIE_NAME = "site_device"

_reg: dict[str, dict] = {}
_dirty: set[str] = set()
_lock = threading.RLock()
_loaded = False
_last_flush = 0.0


# ---------------------------------------------------------------------------
# Разбор User-Agent
# ---------------------------------------------------------------------------
# Полноценный парсер UA (ua-parser и т.п.) сюда не тащим: в админке нужно
# «Chrome на Android, телефон», а не точная версия движка. Порядок правил
# важен — почти все браузеры притворяются Chrome и Safari одновременно.

_BOT_RE = re.compile(
    r"bot|crawler|spider|curl|wget|python-requests|httpx|go-http|headless|"
    r"monitor|uptime|scan", re.I)

_BROWSER_RULES = [
    (re.compile(r"YaBrowser/(\d+)"), "Яндекс.Браузер"),
    (re.compile(r"Edg(?:A|iOS)?/(\d+)"), "Edge"),
    (re.compile(r"OPR/(\d+)|Opera/(\d+)"), "Opera"),
    (re.compile(r"SamsungBrowser/(\d+)"), "Samsung Internet"),
    (re.compile(r"Firefox/(\d+)|FxiOS/(\d+)"), "Firefox"),
    (re.compile(r"CriOS/(\d+)"), "Chrome"),
    (re.compile(r"Chrome/(\d+)"), "Chrome"),
    # у iOS между версией и словом Safari стоит ещё Mobile/<build>
    (re.compile(r"Version/(\d+)[\d.]*.*Safari"), "Safari"),
]

_OS_RULES = [
    (re.compile(r"Windows NT ([\d.]+)"),
     lambda m: "Windows " + {"10.0": "10/11", "6.3": "8.1", "6.1": "7"}.get(
         m.group(1), m.group(1))),
    (re.compile(r"Android (\d+)"), lambda m: f"Android {m.group(1)}"),
    (re.compile(r"(?:iPhone|iPad|iPod).*?OS (\d+)[._](\d+)"),
     lambda m: f"iOS {m.group(1)}.{m.group(2)}"),
    (re.compile(r"Mac OS X (\d+)[._](\d+)"),
     lambda m: f"macOS {m.group(1)}.{m.group(2)}"),
    (re.compile(r"CrOS"), lambda m: "ChromeOS"),
    (re.compile(r"Linux"), lambda m: "Linux"),
]


def describe(user_agent: str) -> dict:
    """Человеческое описание устройства: браузер, ОС и тип."""
    ua = user_agent or ""
    if not ua.strip():
        return {"browser": "неизвестно", "os": "", "kind": "неизвестно"}
    if _BOT_RE.search(ua):
        name = ua.split("/")[0].strip()[:40] or "робот"
        return {"browser": name, "os": "", "kind": "бот"}

    browser = "неизвестный браузер"
    for rx, title in _BROWSER_RULES:
        m = rx.search(ua)
        if m:
            version = next((g for g in m.groups() if g), "")
            browser = f"{title} {version}".strip()
            break

    os_name = ""
    for rx, fmt in _OS_RULES:
        m = rx.search(ua)
        if m:
            os_name = fmt(m)
            break

    if "iPad" in ua or ("Android" in ua and "Mobile" not in ua):
        kind = "планшет"
    elif "Mobile" in ua or "iPhone" in ua or "Android" in ua:
        kind = "телефон"
    else:
        kind = "компьютер"
    return {"browser": browser, "os": os_name, "kind": kind}


def _fingerprint(headers: dict, user_agent: str) -> str:
    """Отпечаток браузера по заголовкам — чтобы бан пережил чистку cookie.

    Берём только то, что браузер шлёт сам и что не меняется от страницы к
    странице. Отпечаток не уникален (два одинаковых телефона дадут один и
    тот же), поэтому в одиночку он ничего не блокирует — только вместе с
    совпадением адреса, см. _blocked_twin."""
    raw = "|".join([
        user_agent or "",
        headers.get("accept-language", ""),
        headers.get("sec-ch-ua-platform", ""),
    ])
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Реестр
# ---------------------------------------------------------------------------

def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        try:
            for row in db.list_visitors():
                _reg[row["device_id"]] = row
        except Exception:  # noqa: BLE001
            log.exception("Не удалось прочитать список посетителей из базы")
        _loaded = True


def _headers_of(request) -> dict:
    # request.headers у FastAPI регистронезависим, но в тестах приходит
    # обычный dict — приводим ключи к нижнему регистру сами.
    return {str(k).lower(): v for k, v in dict(request.headers).items()}


def device_id_of(request) -> str | None:
    """Идентификатор устройства из cookie (None — cookie нет или подделана)."""
    return unsign_value(request.cookies.get(COOKIE_NAME))


def _new_entry(device_id: str, ua: str, fp: str, now: float) -> dict:
    d = describe(ua)
    return {
        "device_id": device_id,
        "ip": "",
        "ips": [],
        "user_agent": ua,
        "fingerprint": fp,
        "browser": d["browser"],
        "os": d["os"],
        "kind": d["kind"],
        "first_seen": now,
        "last_seen": now,
        "hits": 0,
        "authed": False,
        "admin": False,
        "last_path": "",
        "blocked": False,
        "blocked_at": None,
        "block_reason": "",
    }


def _blocked_twin(device_id: str, ip: str, fp: str) -> dict | None:
    """Забаненное устройство, которое просто почистило cookie.

    Совпасть должны И отпечаток браузера, И адрес: одного отпечатка мало
    (у двух одинаковых телефонов он общий, и бан цеплял бы посторонних),
    одного адреса — тоже (за домашним роутером сидит вся семья)."""
    if not ip or not fp:
        return None
    for v in _reg.values():
        if not v["blocked"] or v["device_id"] == device_id:
            continue
        if v["fingerprint"] == fp and (v["ip"] == ip or ip in v["ips"]):
            return v
    return None


def observe(request, *, ip: str, path: str = "", authed: bool = False,
            admin: bool = False) -> dict:
    """Отмечает запрос в реестре и говорит, что делать с этим устройством.

    Возвращает {device_id, set_cookie, cookie, blocked}: set_cookie —
    признак того, что cookie устройства нужно выдать в ответе (её ещё нет
    или подпись не сошлась)."""
    _ensure_loaded()
    device_id = device_id_of(request)
    set_cookie = device_id is None
    if device_id is None:
        device_id = secrets.token_urlsafe(12)
    headers = _headers_of(request)
    ua = headers.get("user-agent", "")
    fp = _fingerprint(headers, ua)
    now = time.time()

    with _lock:
        v = _reg.get(device_id)
        if v is None:
            v = _new_entry(device_id, ua, fp, now)
            _reg[device_id] = v
        if ua and ua != v["user_agent"]:
            v["user_agent"] = ua
            v.update({k: val for k, val in describe(ua).items()})
        v["fingerprint"] = fp
        if ip:
            v["ip"] = ip
            if ip not in v["ips"]:
                v["ips"].append(ip)
                del v["ips"][:-5]   # хватит последних адресов
        v["last_seen"] = now
        v["hits"] += 1
        v["last_path"] = path
        v["authed"] = authed
        if admin:
            v["admin"] = True
        if not v["blocked"] and _blocked_twin(device_id, ip, fp) is not None:
            v["blocked"] = True
            v["blocked_at"] = now
            v["block_reason"] = "twin"
            log.info("Устройство %s заблокировано как вернувшееся после "
                     "чистки cookie (адрес %s)", device_id, ip)
            _persist(v)
        blocked = v["blocked"]
        _dirty.add(device_id)
    _maybe_flush(now)
    return {
        "device_id": device_id,
        "set_cookie": set_cookie,
        "cookie": sign_value(device_id),
        "blocked": blocked,
    }


def is_blocked(device_id: str | None) -> bool:
    if not device_id:
        return False
    _ensure_loaded()
    with _lock:
        v = _reg.get(device_id)
        return bool(v and v["blocked"])


def set_blocked(device_id: str, blocked: bool) -> dict | None:
    """Банит или разбанивает устройство. Решение сразу пишется в базу.

    Разбан снимает и автоматические баны «того же устройства без cookie»
    (block_reason == "twin"): их поставил не оператор, а само правило, и
    оставлять их после разбана оригинала — значит держать человека
    заблокированным без единой видимой причины."""
    _ensure_loaded()
    with _lock:
        v = _reg.get(device_id)
        if v is None:
            return None
        v["blocked"] = blocked
        v["blocked_at"] = time.time() if blocked else None
        v["block_reason"] = "manual" if blocked else ""
        _persist(v)
        if not blocked:
            for twin in list(_reg.values()):
                if twin["blocked"] and twin["block_reason"] == "twin" \
                        and twin["fingerprint"] == v["fingerprint"]:
                    twin["blocked"] = False
                    twin["blocked_at"] = None
                    twin["block_reason"] = ""
                    _persist(twin)
        return dict(v)


def forget(device_id: str) -> bool:
    """Убирает устройство из списка (и из базы). Бан при этом снимается —
    забытое устройство при следующем заходе просто появится заново."""
    _ensure_loaded()
    with _lock:
        existed = _reg.pop(device_id, None) is not None
        _dirty.discard(device_id)
    db.delete_visitor(device_id)
    return existed


def snapshot() -> list[dict]:
    """Список устройств для админки, свежие сверху."""
    _ensure_loaded()
    now = time.time()
    with _lock:
        items = [dict(v) for v in _reg.values()]
    for v in items:
        age = now - v["last_seen"]
        v["age_sec"] = age
        v["online"] = age <= config.VISITOR_ONLINE_WINDOW
        v["seen_sec"] = now - v["first_seen"]
        v["ips"] = list(v["ips"])
    items.sort(key=lambda v: v["last_seen"], reverse=True)
    return items


def online_count() -> int:
    return sum(1 for v in snapshot() if v["online"])


# ---------------------------------------------------------------------------
# Сохранение в базу
# ---------------------------------------------------------------------------

def _persist(entry: dict) -> None:
    """Немедленная запись одного устройства (бан/разбан — терять нельзя)."""
    try:
        db.save_visitors([entry])
    except Exception:  # noqa: BLE001
        log.exception("Не удалось сохранить устройство %s",
                      entry.get("device_id"))


def _maybe_flush(now: float) -> None:
    global _last_flush
    if now - _last_flush < config.VISITORS_FLUSH_INTERVAL:
        return
    _last_flush = now
    flush()


def flush() -> None:
    """Сбрасывает накопленные посещения в базу и подрезает список.

    Пишем пачкой и редко: за 30 секунд каждая открытая вкладка успевает
    сделать с десяток запросов, и на каждый из них ходить в SQLite —
    пустая трата процессора, который нужен обходам БК."""
    _ensure_loaded()
    with _lock:
        rows = [dict(_reg[d]) for d in _dirty if d in _reg]
        _dirty.clear()
        _prune_locked()
    if not rows:
        return
    try:
        db.save_visitors(rows)
        db.prune_visitors(config.VISITORS_MAX)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось сохранить список посетителей")


def _prune_locked() -> None:
    """Держит в памяти только последние VISITORS_MAX устройств.

    Забаненные не выбрасываются никогда: реестр — это ещё и список банов,
    и вымывание из него означало бы тихий разбан."""
    alive = [v for v in _reg.values() if not v["blocked"]]
    if len(alive) <= config.VISITORS_MAX:
        return
    alive.sort(key=lambda v: v["last_seen"], reverse=True)
    for v in alive[config.VISITORS_MAX:]:
        _reg.pop(v["device_id"], None)
        _dirty.discard(v["device_id"])


def reset() -> None:
    """Полная очистка реестра — только для тестов."""
    global _loaded, _last_flush
    with _lock:
        _reg.clear()
        _dirty.clear()
        _loaded = False
        _last_flush = 0.0
