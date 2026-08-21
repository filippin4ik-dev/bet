"""Валюта интерфейса и курс доллара к рублю.

Сканер считает деньги в рублях: банк, суммы плеч, лимит ставки, стартовый
баланс игрока — всё это рубли, потому что и БК в наборе российские. Но
одна БК в наборе — крипто-площадка (bc.game работает в USDT и показывает
баланс в долларах), да и смотреть вилки удобнее в той валюте, которой
человек считает свои деньги. Поэтому:

- РАСЧЁТНАЯ валюта одна и та же всегда — рубль (BASE). В ней приходят
  суммы из движка вилок и в ней хранятся балансы игроков: менять базу на
  ходу означало бы переписать смысл уже сохранённых чисел в базе;
- ВАЛЮТА ОТОБРАЖЕНИЯ выбирается в админке и применяется на сайте: суммы
  пересчитываются курсом, а банк берётся из набора, привычного для этой
  валюты (5 000 ₽ и $50, а не «$54.32»);
- КУРС доллара к рублю задаётся в админке руками или подтягивается с ЦБ
  РФ одной кнопкой. Пока курс не задан, доллар недоступен: пересчитать
  выдуманным курсом хуже, чем не пересчитывать вовсе — по таким суммам
  ставят реальные деньги.

Курс нужен не только показу: баланс крипто-БК приходит в долларах, и без
пересчёта лимит ставки по ней завышался бы почти в сто раз (см.
app/accounts_manager.py).
"""
import logging
import threading
import time

import requests

from . import config, db

log = logging.getLogger("currency")

# Валюта, в которой считает движок и в которой хранятся суммы в базе.
BASE = "RUB"

# Валюты отображения. banks — набор «круглых» банков для этой валюты
# (выпадающий список на сайте), step — шаг ручного ввода суммы.
CURRENCIES: dict[str, dict] = {
    "RUB": {"code": "RUB", "symbol": "₽", "name": "Рубль",
            "banks": [1000, 5000, 10000], "step": 100},
    "USD": {"code": "USD", "symbol": "$", "name": "Доллар США",
            "banks": [10, 50, 100], "step": 5},
    "USDT": {"code": "USDT", "symbol": "₮", "name": "USDT (крипто)",
             "banks": [10, 50, 100], "step": 5},
}

# USDT — стейблкоин, привязанный к доллару один к одному: отдельного курса
# для него не бывает, поэтому считается он по курсу доллара.
_DOLLAR_PEGGED = ("USD", "USDT")

_KEY_CURRENCY = "display_currency"
_KEY_RATE = "usd_rub_rate"
_KEY_RATE_AT = "usd_rub_rate_at"
_KEY_RATE_SRC = "usd_rub_rate_source"

# Настройки читаются на каждый опрос вилок (раз в 5 с каждой открытой
# вкладкой) — кэшируем на пару секунд, как в app/access.py.
_CACHE_TTL = 2.0
_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


def _cached(key: str, loader):
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_TTL:
            return hit[1]
    value = loader()
    with _lock:
        _cache[key] = (now, value)
    return value


def invalidate_cache() -> None:
    with _lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# Валюта отображения
# ---------------------------------------------------------------------------

def normalize(code: str | None) -> str:
    """Код валюты в известном виде; неизвестный код — база (рубль)."""
    code = str(code or "").strip().upper()
    return code if code in CURRENCIES else BASE


def currency() -> str:
    """Валюта отображения. Доллар без заданного курса не отдаём."""
    def load() -> str:
        code = normalize(db.get_setting(_KEY_CURRENCY)
                         or config.DISPLAY_CURRENCY)
        if code != BASE and not _rate_value():
            return BASE
        return code
    return _cached(_KEY_CURRENCY, load)


def set_currency(code: str) -> str:
    code = str(code or "").strip().upper()
    if code not in CURRENCIES:
        raise ValueError(
            "Валюта: " + ", ".join(CURRENCIES) + f" (получено «{code}»)")
    if code != BASE and not _rate_value():
        raise ValueError("Сначала задайте курс доллара к рублю — без него "
                         "пересчитывать суммы нечем.")
    db.set_setting(_KEY_CURRENCY, code)
    invalidate_cache()
    return code


def info(code: str | None = None) -> dict:
    return CURRENCIES[normalize(code) if code else currency()]


# ---------------------------------------------------------------------------
# Курс доллара к рублю
# ---------------------------------------------------------------------------

# Границы разумного курса: опечатка вида «9» или «9000» вместо «90»
# перекосит все суммы на сайте, а заметить это по таблице непросто.
RATE_MIN, RATE_MAX = 1.0, 100_000.0


def _rate_value() -> float:
    """Курс из базы, иначе из окружения; 0.0 — курс не задан."""
    def load() -> float:
        raw = db.get_setting(_KEY_RATE)
        try:
            rate = float(raw) if raw else float(config.USD_RUB_RATE or 0)
        except (TypeError, ValueError):
            rate = 0.0
        return rate if RATE_MIN <= rate <= RATE_MAX else 0.0
    return _cached(_KEY_RATE, load)


def usd_rub() -> float | None:
    """Сколько рублей в одном долларе (None — курс не задан)."""
    return _rate_value() or None


def set_usd_rub(rate: float, source: str = "manual") -> float:
    try:
        value = float(str(rate).replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError("Курс должен быть числом, например 92.5")
    if not RATE_MIN <= value <= RATE_MAX:
        raise ValueError(f"Курс вне разумных границ ({RATE_MIN:g}–"
                         f"{RATE_MAX:g} ₽ за доллар)")
    db.set_setting(_KEY_RATE, repr(round(value, 4)))
    db.set_setting(_KEY_RATE_AT, str(int(time.time())))
    db.set_setting(_KEY_RATE_SRC, source)
    invalidate_cache()
    return round(value, 4)


def rate_updated_at() -> float | None:
    raw = db.get_setting(_KEY_RATE_AT)
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None


def rate_source() -> str:
    """Откуда взят курс: «cbr», «manual», «env» или «none»."""
    if db.get_setting(_KEY_RATE):
        return db.get_setting(_KEY_RATE_SRC) or "manual"
    return "env" if _rate_value() else "none"


def fetch_cbr_rate() -> float:
    """Курс доллара с сайта ЦБ РФ (официальный курс на сегодня)."""
    try:
        resp = requests.get(config.CBR_RATE_URL, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        value = resp.json()["Valute"]["USD"]["Value"]
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Не удалось получить курс ЦБ ({exc}). Введите "
                         "курс руками.")
    return set_usd_rub(value, source="cbr")


# ---------------------------------------------------------------------------
# Пересчёт
# ---------------------------------------------------------------------------

def rub_per_unit(code: str | None = None) -> float | None:
    """Сколько рублей в одной единице валюты (None — курс неизвестен)."""
    code = normalize(code) if code else currency()
    if code == BASE:
        return 1.0
    if code in _DOLLAR_PEGGED:
        return usd_rub()
    return None


def to_rub(amount: float | None, code: str | None = None) -> float | None:
    rate = rub_per_unit(code)
    if amount is None or rate is None:
        return None
    return round(amount * rate, 2)


def from_rub(amount: float | None, code: str | None = None) -> float | None:
    rate = rub_per_unit(code)
    if amount is None or not rate:
        return None
    return round(amount / rate, 2)


def bookmaker_currency(bookmaker: str) -> str:
    """В какой валюте работает БК: крипто-площадки считают в долларах."""
    name = str(bookmaker or "").strip().lower()
    return "USDT" if name in config.CRYPTO_BOOKMAKERS else BASE


def state() -> dict:
    """Состояние валюты для сайта и админки."""
    code = currency()
    rate = usd_rub()
    return {
        "currency": code,
        "symbol": CURRENCIES[code]["symbol"],
        "name": CURRENCIES[code]["name"],
        "banks": list(CURRENCIES[code]["banks"]),
        "step": CURRENCIES[code]["step"],
        "base": BASE,
        # сколько единиц валюты отображения в одном рубле — этим числом
        # сайт пересчитывает присланные движком рублёвые суммы
        "per_rub": round(1 / rub_per_unit(code), 6) if rub_per_unit(code)
        else None,
        "usd_rub": rate,
        "rate_source": rate_source(),
        "rate_updated_at": rate_updated_at(),
        "crypto_bookmakers": sorted(config.CRYPTO_BOOKMAKERS),
        "currencies": [
            {"code": c["code"], "symbol": c["symbol"], "name": c["name"]}
            for c in CURRENCIES.values()
        ],
    }
