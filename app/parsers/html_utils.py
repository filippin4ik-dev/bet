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


# Канонический субъект рынка: приводим РАЗНЫЕ формулировки БК к одному
# токену, иначе «тотал углов» одной БК не сопоставится с «угловые» другой.
# Периоды/части матча и «предмет» тотала/форы (углы, карты, ...) собираются
# в один упорядоченный ключ. Порядок пар (предмет, токен) важен: сначала
# более длинные/специфичные подстроки. «Голы»/«матч» — основной рынок,
# отдельного токена не дают (совпадают с рынком всего матча).
_SUBJECT_TOKENS = [
    ("углов", "corners"),          # угловые
    ("красн", "redcards"),         # красные карточки
    ("желт", "cards"), ("карточ", "cards"),  # жёлтые карты / карточки
    ("карт", "maps"),              # карты (киберспорт) / карты (футбол)
    ("двойн", "doublefaults"),     # двойные ошибки (теннис)
    ("эйс", "aces"),               # эйсы (теннис)
    ("фол", "fouls"),              # фолы
    ("офсайд", "offsides"),        # офсайды
    ("створ", "shotsontarget"),    # удары в створ (уточняет shots)
    ("от ворот", "goalkicks"),     # удары от ворот (уточняет shots)
    ("удар", "shots"),             # удары
    ("голев", "assists"),          # голевые передачи
    ("попыт", "tries"),            # попытки (регби)
    ("вброс", "throwins"), ("аут", "throwins"),  # вбросы аутов
    ("сет", "sets"),               # сеты (как предмет счёта, теннис)
    ("иннинг", "innings"),         # иннинги (бейсбол)
    ("сейв", "saves"),             # сейвы
    ("перехват", "interceptions"),  # перехваты
    ("обводк", "dribbles"),        # успешные обводки
    ("отбор", "tackles"),          # успешные отборы
    ("видеопросмотр", "var"),      # видеопросмотры (VAR)
    ("штанг", "woodwork"), ("перекладин", "woodwork"),  # каркас ворот
    ("замен", "subs"),             # замены
    ("раунд", "rounds"),           # раунды (киберспорт, бокс)
    ("убийств", "kills"),          # убийства (киберспорт)
    # «геймы»/«очки» НЕ считаем отдельным предметом: это основная единица
    # счёта в теннисе/баскетболе, и разные БК называют такой рынок по-разному
    # («Тотал» vs «Тотал по геймам»). Держим их как основной рынок (scope ""),
    # чтобы тоталы/форы этих видов спорта сопоставлялись между БК.
]

# Период/часть матча: ordinal + существительное → period-токен.
_PERIOD_RE = re.compile(
    r"(\d+)\s*-?\s*(?:й|я|е|ый|ой|ая|ое|го|ой)?\s*"
    r"(тайм|сет|период|четверт|половин|иннинг|карт)")
_PERIOD_ROOT = {
    "тайм": "half", "период": "period", "четверт": "quarter",
    "половин": "half", "иннинг": "inning", "сет": "set", "карт": "map",
}

# Слова, которые НЕ являются предметом рынка: вид рынка, основная единица
# счёта, служебные слова. Не попадают в запасной (fallback) токен.
_SCOPE_STOPWORDS = {
    "тотал", "тоталы", "фора", "форы", "гандикап", "исход", "исходы",
    "победитель", "матч", "матча", "матче", "игра", "игры",
    "гол", "гола", "голы", "голов", "забьет", "забьют",
    "очко", "очка", "очки", "очков", "гейм", "гейма", "геймы", "геймов",
    "геймам", "баллы", "баллов",
    "основное", "осн", "время", "общий", "общее", "общая",
    "команд", "команда", "команды", "командам", "обеих", "обе", "оба",
    "кол", "во", "количество", "число",
    "больше", "меньше", "чет", "нечет",
}
_WORD_RE = re.compile(r"[а-яa-z]+")


def market_scope(text: str | None) -> str:
    """Канонический субъект/период рынка для ключа (или "" для основного).

    Пропускаем через один нормализатор и названия дочерних событий Fonbet
    («1-й тайм угловые», «желтые карты»), и имена рынков BetBoom («Тотал
    угловых», «Фора по геймам»). Возвращаем упорядоченный набор токенов —
    так одинаковый по смыслу рынок совпадает между БК независимо от
    формулировки.

    ВАЖНО: незнакомый предмет («оказание помощи мед. бригады») больше НЕ
    сливается с основным рынком — из его слов строится запасной токен.
    Такой рынок может не сопоставиться с другой БК (другая формулировка),
    зато не даст ЛОЖНУЮ вилку «экзотика против основного рынка».
    """
    if not text:
        return ""
    t = text.lower().replace("ё", "е")
    tokens: list[str] = []

    for m in _PERIOD_RE.finditer(t):
        num, root = m.group(1), m.group(2)
        base = _PERIOD_ROOT.get(root)
        # «сет»/«карта» как ПЕРИОД (1-й сет, 2-я карта) — только с
        # порядковым номером; голые «сетов»/«карт» уходят в предметные
        # токены sets/maps ниже.
        if base:
            tokens.append(f"{base}{num}")

    subject_found = False
    for needle, token in _SUBJECT_TOKENS:
        if needle in t and token not in tokens:
            # «сет»/«карта» как ПРЕДМЕТ счёта — только без порядкового
            # префикса (иначе «1-й сет» уже учтён как период выше)
            if token in ("sets", "maps") and re.search(
                    r"\d+\s*-?\s*(?:й|я|го)?\s*" + needle, t):
                continue
            tokens.append(token)
            subject_found = True

    if not subject_found:
        # Предмет не распознан — строим запасной токен из значимых слов
        # (обрезаем окончания, чтобы «замены» и «замен» совпали).
        extra = sorted({w[:7] for w in _WORD_RE.findall(t)
                        if len(w) >= 3 and w not in _SCOPE_STOPWORDS
                        and not _PERIOD_RE.search(f"1 {w}")})
        tokens.extend(extra)

    return "+".join(sorted(set(tokens)))


def format_start(ts: float) -> str:
    """Форматирует unix-время начала матча в «ДД.ММ ЧЧ:ММ» (пояс БК)."""
    tz = timezone(timedelta(hours=BK_TZ_OFFSET))
    return datetime.fromtimestamp(ts, tz).strftime("%d.%m %H:%M")


def fmt_total(pt) -> str:
    """Единый формат линии тотала: «2.5», «2» (без хвостовых нулей).

    Все парсеры и движок обязаны форматировать линию одинаково — иначе
    тоталы разных БК не сопоставятся (напр. «2.0» ≠ «2»)."""
    try:
        f = float(str(pt).replace(",", "."))
    except (TypeError, ValueError):
        return str(pt).strip()
    if f == int(f):
        return str(int(f))
    return ("%g" % f)


def fmt_hcap(v) -> str:
    """Единый формат линии форы со знаком: «-1.5», «+1», «0».

    Знак обязателен: фора team1(-1.5) и team1(+1.5) — РАЗНЫЕ рынки. По
    знаку и величине движок сшивает противоположные исходы разных БК."""
    try:
        f = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return str(v).strip()
    if f == 0:
        return "0"
    return ("+" if f > 0 else "-") + ("%g" % abs(f))


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
