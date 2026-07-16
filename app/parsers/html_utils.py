"""Общие помощники для HTML-парсеров (Winline / BetBoom / Лига Ставок)."""
import re
import time as _time
from datetime import datetime, timedelta, timezone

from ..config import BK_TZ_OFFSET
from ..models import MarketOdds

# lxml в разы быстрее html.parser — важно: за цикл разбираются десятки
# многомегабайтных снимков DOM. Если lxml не установлен, работаем на
# встроенном парсере.
try:
    import lxml  # noqa: F401
    SOUP_PARSER = "lxml"
except ImportError:  # pragma: no cover
    SOUP_PARSER = "html.parser"

# Русские месяцы (полные и сокращённые формы — сравнение по префиксу)
_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "ма": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}

_RE_TIME = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
_RE_DATE_NUM = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?!\d)")
_RE_DATE_WORD = re.compile(r"(?<!\d)(\d{1,2})\s+([а-яё]+)")


def _month_by_word(word: str) -> int | None:
    word = word.lower().replace("ё", "е")
    for prefix, mo in _MONTHS.items():
        if word.startswith(prefix):
            return mo
    return None


def parse_start_ts(text: str | None, now: float | None = None) -> float | None:
    """Распознаёт время начала матча из текста БК → unix-время (или None).

    Понимает форматы: «22:00», «16.07 22:00», «16.07.2026 22:00»,
    «16 июля 22:00», «Сегодня 22:00», «Завтра 22:00». Часовой пояс БК —
    BK_TZ_OFFSET (по умолчанию МСК).
    """
    if not text:
        return None
    t = text.lower().replace("ё", "е")

    m = _RE_TIME.search(t)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        return None

    tz = timezone(timedelta(hours=BK_TZ_OFFSET))
    now_dt = datetime.fromtimestamp(now if now is not None else _time.time(), tz)

    day = None
    explicit_date = False
    dm = _RE_DATE_NUM.search(t)
    if dm:
        d, mo = int(dm.group(1)), int(dm.group(2))
        year = int(dm.group(3)) if dm.group(3) else now_dt.year
        if year < 100:
            year += 2000
        try:
            day = now_dt.replace(year=year, month=mo, day=d)
            explicit_date = True
        except ValueError:
            day = None
        # «31.12» в январе — это прошедший декабрь? Без года считаем,
        # что дата из ближайшего будущего/недавнего прошлого.
        if day is not None and not dm.group(3) and (now_dt - day).days > 180:
            day = day.replace(year=year + 1)
    if day is None:
        wm = _RE_DATE_WORD.search(t)
        if wm:
            mo = _month_by_word(wm.group(2))
            if mo:
                try:
                    day = now_dt.replace(month=mo, day=int(wm.group(1)))
                    explicit_date = True
                except ValueError:
                    day = None
                if day is not None and (now_dt - day).days > 180:
                    day = day.replace(year=day.year + 1)
    if day is None:
        if "завтра" in t:
            day = now_dt + timedelta(days=1)
            explicit_date = True
        elif "сегодня" in t:
            day = now_dt
            explicit_date = True
        else:
            day = now_dt  # только «ЧЧ:ММ» — считаем, что сегодня

    dt = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
    # Голое «22:00», которое уже прошло больше часа назад — скорее завтра
    if not explicit_date and dt < now_dt - timedelta(hours=1):
        dt += timedelta(days=1)
    return dt.timestamp()


def scope_key(label: str | None) -> str:
    """Нормализует название дочерней росписи для ключа рынка.

    «2-й сет» и «2сет» должны дать один ключ, чтобы рынки правильно
    сопоставлялись между БК и, главное, НЕ группировались с рынками
    всего матча.
    """
    if not label:
        return ""
    t = label.lower().replace("ё", "е")
    t = re.sub(r"(\d)\s*-?\s*(й|я|е|ый|ой|ая|ое)\b", r"\1", t)
    return re.sub(r"[^\w]+", "", t)


def format_start(ts: float) -> str:
    """Форматирует unix-время начала матча в «ДД.ММ ЧЧ:ММ» (пояс БК)."""
    tz = timezone(timedelta(hours=BK_TZ_OFFSET))
    return datetime.fromtimestamp(ts, tz).strftime("%d.%m %H:%M")


def num(text: str) -> float | None:
    try:
        return float(text.replace(",", ".").strip())
    except (ValueError, AttributeError):
        return None


def pair(coefs: list[str]) -> tuple[float | None, float | None]:
    if len(coefs) != 2:
        return None, None
    return num(coefs[0]), num(coefs[1])


def parse_totals(event, base: dict, coef_selector: str) -> list[MarketOdds]:
    """Достаёт двухисходные тоталы (ТБ/ТМ) с карточки события.

    Ищем блоки рынка «тотал» с параметром линии (data-param / .param) и
    парой кэфов больше/меньше. Если сайт их не размечает — пустой список.
    """
    out = []
    selectors = "[data-market='total'], .market-total, .total-row, .total"
    for block in event.select(selectors):
        pt = block.get("data-param") or block.get("data-total")
        if not pt:
            label = block.select_one(".total-value, .param, .handicap")
            pt = label.get_text(strip=True) if label else None
        coefs = [c.get_text(strip=True) for c in block.select(coef_selector)]
        over, under = pair(coefs)
        if pt and over and under:
            out.append(MarketOdds(
                market=f"Тотал {pt}", market_key=f"total:{pt}",
                outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                k1=over, k2=under, **base))
    return out
