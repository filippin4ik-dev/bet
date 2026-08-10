"""Общие помощники для HTML-парсеров (Winline / BetBoom / Лига Ставок)."""
import functools
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
    ("фол", "fouls"), ("нарушен", "fouls"),  # фолы / нарушения
    ("офсайд", "offsides"), ("вне игры", "offsides"),  # офсайды
    ("навылет", "aces"),           # подачи навылет = эйсы
    ("створ", "shotsontarget"),    # удары в створ (уточняет shots)
    ("от ворот", "goalkicks"),     # удары от ворот (уточняет shots)
    ("удар", "shots"),             # удары
    ("голев", "assists"),          # голевые передачи
    ("попыт", "tries"),            # попытки (регби)
    ("вброс", "throwins"), ("вбрасыв", "throwins"),  # вбросы/вбрасывания
    ("аут", "throwins"),           # ауты
    ("сет", "sets"),               # сеты (как предмет счёта, теннис)
    ("парти", "sets"),             # партии (волейбол/наст. теннис) = сеты
    ("иннинг", "innings"),         # иннинги (бейсбол)
    ("сейв", "saves"),             # сейвы
    ("перехват", "interceptions"),  # перехваты
    ("обводк", "dribbles"),        # успешные обводки
    ("отбор", "tackles"),          # успешные отборы
    ("видеопросмотр", "var"),      # видеопросмотры (VAR)
    ("штанг", "woodwork"), ("перекладин", "woodwork"),  # каркас ворот
    ("замен", "subs"),             # замены
    # Киберспорт. Объекты карты (дракон, барон, Рошан, башни, ингибиторы,
    # казармы) — САМОСТОЯТЕЛЬНЫЕ рынки, а не убийства чемпионов, хотя у
    # Winline они и подписаны через то же слово: «тотал убийств драконов»,
    # «фора убийств барона». Без своих токенов все они схлопывались в
    # kills и сшивались с обычной «форой убийств» другой БК — ровно такая
    # ложная вилка и приехала с матча LCP (BetBoom «фора убийств» против
    # Winline «фора убийств дракона»).
    ("элементальн", "elemental"),  # элементальные драконы (без Элдера)
    ("дракон", "dragons"),
    ("барон", "baron"),
    ("рошан", "roshan"),
    ("ингибитор", "inhibitors"),
    ("казарм", "barracks"),
    ("башн", "towers"), ("башен", "towers"), ("вышк", "towers"),
    ("кровь", "firstblood"),       # первая кровь
    ("убийств", "kills"),          # убийства чемпионов (киберспорт)
    # «геймы»/«очки»/«раунды» НЕ считаем отдельным предметом: это основная
    # единица счёта в теннисе/баскетболе/MMA/боксе/CS, и разные БК называют
    # такой рынок по-разному («Тотал» vs «Тотал по геймам», «Тотал» vs
    # «Тотал раундов» в MMA). Держим их как основной рынок (scope ""), чтобы
    # тоталы/форы этих видов спорта сопоставлялись между БК. Конкретный
    # гейм/раунд С НОМЕРОМ («5-й гейм», «3-й раунд») — период, см. ниже.
]

# Период/часть матча: ordinal + существительное → period-токен.
# «гейм»/«раунд» — только В ЕДИНСТВЕННОМ ЧИСЛЕ («5-й гейм», «3-й раунд»,
# допускается родительный: «5-го гейма»): это КОНКРЕТНЫЙ отрезок матча.
# Множественное («геймов/геймы», «раундов/раунды» — основная единица
# счёта: «тотал геймов», «тотал раундов» в MMA/боксе/CS) НЕ период — его
# отсекает негативный lookahead (?![а-я]). Без этого «исход 1-го сета» и
# «исход 1-й сет 5-й гейм» давали один scope (set1) и склеивались в один
# рынок — ложная вилка.
# Окончание порядкового после цифры — любые 1-2 буквы («1-й», «1-м», «1-ом»,
# «1-го», «2-я», «1 тайм» без окончания). Раньше список окончаний был
# закрытым и НЕ включал предложный падеж: «Обе забьют в 1-м периоде»
# (Betcity) периодом не распознавалось, scope выходил пустым — и рынок
# периода сшивался с рынком ВСЕГО матча в ложную вилку.
_PERIOD_RE = re.compile(
    r"(\d+)\s*-?\s*(?:[а-я]{1,2})?\s*"
    r"(овертайм|тайм|сет|период|четверт|половин|иннинг|карт|парти"
    r"|гейм(?:а|е)?(?![а-я])|раунд(?:а|е)?(?![а-я]))")
_PERIOD_ROOT = {
    "тайм": "half", "период": "period", "четверт": "quarter",
    "половин": "half", "иннинг": "inning", "сет": "set", "карт": "map",
    "гейм": "game", "раунд": "round", "овертайм": "ot", "парти": "set",
}

# Порядковое СЛОВОМ перед названием периода: «в первом тайме» (Betcity),
# «второй сет» — тот же период, что «1-й тайм»/«2-й сет» у других БК.
# Приводим к цифровой форме ДО разбора периодов, иначе слово уходило в
# запасной токен («перв») и рынок не сшивался с той же росписью другой БК.
# Окончание перечислено полностью, а не «любые буквы»: иначе с порядковым
# смешивались бы КОЛИЧЕСТВЕННЫЕ числительные («пять сетов» — это тотал, а
# не 5-й сет), у которых окончание «-ь».
_WORD_ORDINAL_RE = re.compile(
    r"\b(перв|втор|трет|четверт|пят|шест|седьм|восьм|девят|десят)"
    r"(?:ый|ий|ой|ая|ое|ые|ого|его|ом|ем|ую|ых|ья|ье|ьего|ьем)\s+"
    r"(овертайм|тайм|сет|период|четверт|половин|иннинг|карт|парти|гейм|раунд)")
_WORD_ORDINAL = {
    "перв": "1", "втор": "2", "трет": "3", "четверт": "4", "пят": "5",
    "шест": "6", "седьм": "7", "восьм": "8", "девят": "9", "десят": "10",
}

# Слова, которые НЕ являются предметом рынка: вид рынка, основная единица
# счёта, служебные слова. Не попадают в запасной (fallback) токен.
_SCOPE_STOPWORDS = {
    "тотал", "тотала", "тоталы", "фора", "форы", "гандикап",
    "исход", "исходы",
    # «Доп. тоталы 1-й тайм» (Betcity) — это те же тоталы тайма, слово
    # «доп.» не должно попадать в scope запасным токеном.
    "доп", "дополнительный", "дополнительные",
    # «Индивидуальный тотал»/«Инд. тотал» — то, что тотал командный, уже
    # несёт ключ рынка (itotal:<сторона>), в scope это слово не нужно.
    "инд", "индивид", "индивидуальный", "индивидуальные",
    "победитель", "победа", "матч", "матча",
    "матче", "игра", "игры",
    # Итог/результат матча — тот же основной рынок. Падежи перечислены
    # полностью: слово, которого нет в списке, уходит в ЗАПАСНОЙ токен, и
    # рынок с ним не сходится ни с одной другой БК — так «Итог матча» и
    # «Тотал результативности» молча теряли все свои вилки.
    "итог", "итога", "итоге", "итоги", "итогов", "итоговая", "итоговый",
    "итоговое", "итоговые", "итогового",
    "результат", "результате", "результата", "результативности",
    # Гол и мяч — одно и то же (БК пишут и «тотал голов», и «тотал мячей»)
    "гол", "гола", "голу", "голы", "голов", "голам", "голах",
    "мяч", "мяча", "мячу", "мячи", "мячей", "мячам", "мячах",
    # «Забитые» голы — это и есть тотал голов. «Пропущенные» намеренно НЕ
    # в списке: индивидуальный тотал пропущенных — ДРУГОЙ рынок, слить его
    # с забитыми значило бы выдать ложную вилку.
    "забьет", "забьют", "забитый", "забитые", "забитых", "забито",
    "очко", "очка", "очку", "очки", "очков", "очкам", "очках",
    "гейм", "гейма", "геймы", "геймов", "геймам",
    "балл", "балла", "баллы", "баллов", "баллам",
    "раунд", "раунда", "раунды", "раундов", "раундам",
    "основное", "основной", "основного", "основным", "осн",
    "время", "времени", "общий", "общее", "общая",
    "команд", "команда", "команды", "командам", "обеих", "обе", "оба",
    "кол", "во", "количество", "число",
    "больше", "меньше", "чет", "нечет",
    # «Азиатская фора/тотал» — та же линия форы/тотала, только с
    # дробным шагом (без пуша). Не отдельный предмет рынка (LeonBet).
    "азиатская", "азиатский",
    # «Фактический исход» — так Betcity подписывает обычный рынок
    # «Исход»/1X2 (в т.ч. по угловым/картам/фолам и т.п.) — не отдельный
    # предмет, иначе базовый 1X2 не сошьётся с другими БК (scope разойдётся
    # с их пустым scope "").
    "фактический", "фактически",
}
_WORD_RE = re.compile(r"[а-яa-z]+")

# УТОЧНЯЮЩИЙ токен вытесняет ОБЩИЙ: формулировка БК почти всегда содержит и
# то и другое слово («убийства дракона», «угловые удары», «жёлтые карты»), а
# соседняя БК пишет только уточнение («тотал драконов», «тотал угловых»,
# «тотал карточек»). Без вытеснения у первой рынок выходил [dragons, kills] /
# [corners, shots] / [cards, maps], у второй — [dragons] / [corners] /
# [cards], scope расходился, и один и тот же рынок попадал в РАЗНЫЕ группы —
# вилка по нему не находилась вовсе. Проверено на живых подписях: так
# молча терялись все вилки по угловым и по карточкам между БК, которые
# называют их «угловыми ударами» и «жёлтыми картами».
_REFINES = {
    # киберспорт: объект карты — это не «убийства чемпионов»
    "dragons": ("kills",), "baron": ("kills",), "roshan": ("kills",),
    "towers": ("kills",), "inhibitors": ("kills",), "barracks": ("kills",),
    # элементальные драконы (без Элдера) — своя линия, а не все драконы
    "elemental": ("dragons",),
    # футбол: угловой удар — это угловой, а не удар по воротам
    "corners": ("shots",),
    "shotsontarget": ("shots",), "goalkicks": ("shots",),
    # «жёлтые/красные карты» — это карточки, а не карты киберспорта
    "cards": ("maps",), "redcards": ("cards", "maps"),
}

# Предмет, который не поймать подстрокой: значение несёт оборот целиком.
# «Убийства С 10 МИНУТ» — это не тотал убийств за весь матч; номер минуты
# к этому месту уже вырезан вместе с параметрами линии, поэтому все такие
# рынки попадают в один токен. Внутри одной БК это безопасно (вилки ищутся
# только между разными), а BetBoom рынки с «мин.» вообще пропускает.
_SUBJECT_PATTERNS = [
    (re.compile(r"\bс\s*\d*\s*минут"), "aftermin"),
]


@functools.lru_cache(maxsize=100_000)
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

    Подписей рынков на всю линию — считанные десятки, а зовут эту функцию
    на каждый рынок каждого события (у Betcity с полной росписью это сотни
    тысяч вызовов за обход), и внутри неё несколько регулярных выражений.
    Функция чистая, поэтому просто кэшируем результат по подписи: время,
    отданное разбору, — это время, отнятое у самих обходов БК.
    """
    if not text:
        return ""
    t = text.lower().replace("ё", "е")
    t = _WORD_ORDINAL_RE.sub(
        lambda m: f"{_WORD_ORDINAL[m.group(1)]} {m.group(2)}", t)
    tokens: list[str] = []

    for m in _PERIOD_RE.finditer(t):
        num, root = m.group(1), m.group(2)
        # родительный падеж ед. числа («5-го гейма/раунда») → именительный
        base = _PERIOD_ROOT.get(root) or _PERIOD_ROOT.get(root.rstrip("ае"))
        # «сет»/«карта» как ПЕРИОД (1-й сет, 2-я карта) — только с
        # порядковым номером; голые «сетов»/«карт» уходят в предметные
        # токены sets/maps ниже.
        if base:
            tokens.append(f"{base}{num}")

    subject_found = False
    for needle, token in _SUBJECT_TOKENS:
        if needle == "карт" and "карточ" in t:
            continue  # «карточки» — это cards, а не карты (maps)
        if needle in t and token not in tokens:
            # «сет»/«карта» как ПРЕДМЕТ счёта — только без порядкового
            # префикса (иначе «1-й сет» уже учтён как период выше)
            if token in ("sets", "maps") and re.search(
                    r"\d+\s*-?\s*(?:й|я|го)?\s*" + needle, t):
                continue
            tokens.append(token)
            subject_found = True

    for pattern, token in _SUBJECT_PATTERNS:
        if token not in tokens and pattern.search(t):
            tokens.append(token)
            subject_found = True

    general = {gen for tk in tokens for gen in _REFINES.get(tk, ())}
    if general:
        tokens = [tk for tk in tokens if tk not in general]

    if not subject_found:
        # Предмет не распознан — строим запасной токен из значимых слов
        # (обрезаем окончания, чтобы «замены» и «замен» совпали).
        extra = sorted({w[:7] for w in _WORD_RE.findall(t)
                        if len(w) >= 3 and w not in _SCOPE_STOPWORDS
                        and not _PERIOD_RE.search(f"1 {w}")})
        tokens.extend(extra)

    return "+".join(sorted(set(tokens)))


# Русские подписи токенов scope — для отображения рынков пользователю
_TOKEN_RU = {
    "corners": "угловые", "cards": "ЖК", "redcards": "КК",
    "fouls": "фолы", "offsides": "офсайды", "shots": "удары",
    "shotsontarget": "удары в створ", "goalkicks": "удары от ворот",
    "throwins": "ауты", "assists": "голевые передачи", "tries": "попытки",
    "sets": "сеты", "innings": "иннинги", "saves": "сейвы",
    "interceptions": "перехваты", "dribbles": "обводки",
    "tackles": "отборы", "var": "видеопросмотры", "woodwork": "каркас",
    "subs": "замены", "rounds": "раунды", "kills": "убийства",
    "maps": "карты", "doublefaults": "двойные ошибки", "aces": "эйсы",
    "dragons": "драконы", "elemental": "элементальные", "baron": "барон",
    "roshan": "Рошан", "towers": "башни", "inhibitors": "ингибиторы",
    "barracks": "казармы", "firstblood": "первая кровь",
    "aftermin": "с N-й минуты",
}
_PERIOD_RU = {
    "half": "тайм", "period": "период", "quarter": "четверть",
    "set": "сет", "map": "карта", "inning": "иннинг", "game": "гейм",
    "round": "раунд", "ot": "овертайм",
}
_PERIOD_TOKEN_RE = re.compile(
    r"^(half|period|quarter|set|map|inning|game|round|ot)(\d+)$")


def scope_label(scope: str) -> str:
    """Человекочитаемая подпись scope: «map1+rounds» → «1-я карта, раунды».

    Неизвестные (запасные) токены показываем как есть."""
    if not scope:
        return ""
    parts = []
    for tok in scope.split("+"):
        m = _PERIOD_TOKEN_RE.match(tok)
        if m:
            base, num = _PERIOD_RU[m.group(1)], m.group(2)
            suffix = "-я" if base in ("четверть", "карта") else "-й"
            parts.append(f"{num}{suffix} {base}")
        else:
            parts.append(_TOKEN_RU.get(tok, tok))
    return ", ".join(parts)


def neg_hcap(line: str) -> str:
    """Противоположная фора: «-1.5» → «+1.5», «+1» → «-1», «0» → «0»."""
    line = line.strip()
    if line in ("0", "+0", "-0", ""):
        return "0"
    if line.startswith("-"):
        return "+" + line[1:]
    if line.startswith("+"):
        return "-" + line[1:]
    return "-" + line


@functools.lru_cache(maxsize=100_000)
def canon_market_key(key: str) -> str:
    """Единая форма ключа рынка: один и тот же рынок у разных БК — один ключ.

    Форму ключа задаёт парсер, и до этой функции парсеры расходились в
    записи рынка БЕЗ scope (то есть рынка всего матча — самого ходового):
    Betcity и LeonBet писали фору как «hcap:-1.5», а Winline, BetBoom,
    Fonbet, Melbet, bc.game и Лига Ставок — как «hcap::-1.5», с пустым
    сегментом на месте scope. Ключи не совпадали, поэтому основная фора
    матча НИКОГДА не сшивалась между этими двумя группами БК, и все вилки
    по ней терялись молча: в интерфейсе рынок у обеих БК подписан
    одинаково («Фора -1.5» — display_market обе формы читает), а в одну
    группу движок их не сводил.

    Здесь недостающий пустой scope достраивается, а линия приводится к
    общему виду (fmt_hcap/fmt_total): «1.5» и «+1.5» — одна и та же фора,
    «2.0» и «2» — один и тот же тотал.

    Заодно ключ добивается до той формы, которую движок разбирает по
    сегментам («hcap:<scope>:<линия>», «itotal:<сторона>:<scope>:<линия>»).
    Это не косметика: на ключе неожиданной формы разбор падал с
    ValueError — а падал он внутри общего пересчёта, то есть ОДНА кривая
    котировка одной БК уносила разом ВСЕ вилки по всем БК, и так каждый
    цикл, пока эта котировка жива. Лучше не понять один рынок, чем
    потерять всю линию.

    Функция чистая, а зовёт её движок дважды на КАЖДУЮ котировку (разбор
    исходов и группировка) — это миллионы вызовов за пересчёт при считанных
    сотнях разных ключей на всю линию, поэтому с кэшем.
    """
    if key.startswith("hcap"):
        parts = _key_parts(key, 3)     # hcap:<scope>:<линия>
        return ":".join(parts[:-1] + [fmt_hcap(parts[-1])])
    if key.startswith("itotal"):
        parts = _key_parts(key, 4)     # itotal:<сторона>:<scope>:<линия>
        return ":".join(parts[:-1] + [fmt_total(parts[-1])])
    if key.startswith("total") and ":" in key:
        # у тотала канон БЕЗ scope — двухчастный («total:2.5»), поэтому
        # пустой сегмент здесь не достраивается, а убирается
        parts = _key_parts(key, 2)     # total[:<scope>]:<линия>
        return ":".join(parts[:-1] + [fmt_total(parts[-1])])
    return key


def _key_parts(key: str, want: int) -> list[str]:
    """Сегменты ключа рынка: пустые сегменты в середине убираются, а до
    нужной длины ключ достраивается пустыми — перед линией.

    Пустой сегмент в середине значит «scope не задан», и записать его можно
    и одним двоеточием, и ни одного: «hcap:-1.5» и «hcap::-1.5» — одна и та
    же фора всего матча. Пока формы не сводились к одной, рынок жил в двух
    РАЗНЫХ группах и вилка по нему не находилась.

    want — сколько сегментов у ключа МИНИМУМ (у форы 3, у инд. тотала 4, у
    тотала 2: там канон без scope как раз двухчастный). Лишние сегменты не
    трогаем: непустой scope — это настоящий отдельный рынок (тотал по
    сетам — не тотал матча), а не мусор."""
    head, *rest = key.split(":")
    line = rest.pop() if rest else ""
    rest = [p for p in rest if p]      # пустой «scope» ничего не значит
    while len(rest) < want - 2:
        rest.append("")
    return [head] + rest + [line]


def display_market(key: str, fallback: str) -> str:
    """Единое человекочитаемое имя рынка по его ключу.

    Разные БК подписывают один рынок по-разному («Тотал throwins 32.5» /
    «Тотал 32.5»); в таблицах показываем каноничное имя со scope на русском:
    «Тотал 32.5 (ауты)», «Фора -1.5 (1-я карта)»."""
    parts = key.split(":")
    kind = parts[0]
    if kind == "total":
        scope = parts[1] if len(parts) >= 3 else ""
        lbl = scope_label(scope)
        return f"Тотал {parts[-1]}" + (f" ({lbl})" if lbl else "")
    if kind == "hcap":
        scope = parts[1] if len(parts) >= 3 else ""
        lbl = scope_label(scope)
        return f"Фора {parts[-1]}" + (f" ({lbl})" if lbl else "")
    if kind == "winner":
        scope = parts[1] if len(parts) >= 2 else ""
        lbl = scope_label(scope)
        return "Победитель" + (f" ({lbl})" if lbl else "")
    if kind == "winner1x2":
        scope = parts[1] if len(parts) >= 2 else ""
        lbl = scope_label(scope)
        return "Исход (1X2)" + (f" ({lbl})" if lbl else "")
    if kind == "bothscore":
        scope = parts[1] if len(parts) >= 2 else ""
        lbl = scope_label(scope)
        return "Обе забьют" + (f" ({lbl})" if lbl else "")
    if kind == "oddeven":
        scope = parts[1] if len(parts) >= 2 else ""
        lbl = scope_label(scope)
        return "Чет/Нечет" + (f" ({lbl})" if lbl else "")
    # itotal: имя команды есть только в подписи парсера — показываем её
    return fallback


# Транслитерация для слагов в адресах страниц событий. Точную схему БК
# повторить нельзя (у каждой своя), но роутеры смотрят на числовые id, а
# слаг им нужен лишь как заполнитель сегмента — см. _event_url парсеров.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slugify(text: str | None, fallback: str = "event") -> str:
    """Кусок адреса из названия: «Лига Европы УЕФА» → «liga-evropy-uefa».

    Пустое или бессмысленное название превращается в fallback: сегмент
    адреса не может быть пустым, иначе путь схлопнется и роутер БК не
    узнает маршрут."""
    out = []
    for ch in (text or "").strip().lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii() and ch.isalnum():
            out.append(ch)
        else:
            out.append("-")
    return re.sub(r"-+", "-", "".join(out)).strip("-") or fallback


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


def sane_1x2_margin(k1: float, kx: float, k2: float) -> bool:
    """Защитная проверка для рынка «Исход 1X2» (победитель+ничья).

    Собственная маржа ОДНОЙ БК на этом рынке (1/К1+1/Кx+1/К2) у реальной
    линии практически всегда лежит в диапазоне ~1.02–1.35 (2–35 %
    накрутка букмекера). Если парсер перепутал порядок исходов в фиде
    (например, у Winline это позиционный бинарный протокол, а не именованные
    поля), маржа обычно вылезает за этот коридор — такую котировку
    безопаснее не показывать, чем предложить ставку по неверной разметке
    исходов (риск потери денег при автоматической ставке)."""
    if not (k1 and kx and k2 and k1 > 1 and kx > 1 and k2 > 1):
        return False
    margin = 1 / k1 + 1 / kx + 1 / k2
    return 1.0 < margin < 1.35


def sane_pair_margin(k1: float, k2: float) -> bool:
    """Та же защитная проверка, что sane_1x2_margin, но для двух исходов.

    У настоящей пары взаимоисключающих исходов ОДНОЙ БК маржа
    (1/К1 + 1/К2) всегда больше единицы (иначе БК торговала бы себе в
    убыток) и на практике не превышает ~1.35. Выход за коридор означает,
    что в пару попали исходы РАЗНЫХ рынков — например «больше 2.5» и
    «меньше 3.5». Показать такую пару опаснее, чем потерять рынок: на неё
    поставят деньги как на вилку."""
    if not (k1 and k2 and k1 > 1 and k2 > 1):
        return False
    margin = 1 / k1 + 1 / k2
    return 1.0 < margin < 1.35


def num(text: str) -> float | None:
    try:
        return float(text.replace(",", ".").strip())
    except (ValueError, AttributeError):
        return None


def pair(coefs: list[str]) -> tuple[float | None, float | None]:
    if len(coefs) != 2:
        return None, None
    return num(coefs[0]), num(coefs[1])


# Разметка исхода тотала: «ТБ»/«Б»/«больше»/«over» против «ТМ»/«М»/
# «меньше»/«under». Однобуквенные подписи отделены границами слова, иначе
# «м» поймается в любом слове.
_OVER_RE = re.compile(r"(?<![а-яa-z])(тб|б|больше|более|over|more)(?![а-яa-z])",
                      re.IGNORECASE)
_UNDER_RE = re.compile(r"(?<![а-яa-z])(тм|м|меньше|менее|under|less)(?![а-яa-z])",
                       re.IGNORECASE)
_HINT_ATTRS = ("class", "title", "aria-label", "data-outcome", "data-type",
               "data-name", "data-selection", "data-outcome-name")


def _outcome_hint(el) -> str:
    """Слова вокруг кэфа, по которым виден исход: свои и родительские
    атрибуты плюс текст родителя без самого кэфа."""
    parts = []
    for node in (el, el.parent):
        attrs = getattr(node, "attrs", None)
        if not attrs:
            continue
        for name in _HINT_ATTRS:
            value = attrs.get(name)
            if value:
                parts.append(" ".join(value) if isinstance(value, list)
                             else str(value))
    parent = el.parent
    if parent is not None:
        own = el.get_text(strip=True)
        text = parent.get_text(" ", strip=True)
        parts.append(text.replace(own, " ", 1) if own else text)
    return " ".join(parts)


def _over_under(block, coef_selector: str) -> tuple[float | None, float | None]:
    """Кэфы тотала как (больше, меньше).

    Порядок ТБ/ТМ в вёрстке у БК не закреплён, а перепутанные местами
    стороны — хуже потерянного рынка: ключ у них тот же, и движок сведёт
    ТБ одной БК с ТБ другой по ЧУЖОЙ цене, выдав ложную вилку. Поэтому
    сначала пробуем опознать стороны по подписи и берём порядок вёрстки
    только тогда, когда подписи нет вовсе."""
    cells = block.select(coef_selector)
    if len(cells) != 2:
        return None, None
    first, second = (num(c.get_text(strip=True)) for c in cells)
    hints = [_outcome_hint(c) for c in cells]
    marks = [(bool(_OVER_RE.search(h)), bool(_UNDER_RE.search(h)))
             for h in hints]
    # сторону засчитываем, только если подпись говорит однозначно: и «Б», и
    # «М» в одной подписи — не разметка исхода, а случайные слова
    over_first, under_first = marks[0]
    over_second, under_second = marks[1]
    if over_first != under_first and over_second != under_second:
        if over_first and under_second:
            return first, second
        if under_first and over_second:
            return second, first
    return first, second


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
        over, under = _over_under(block, coef_selector)
        if pt and over and under:
            # линия со страницы приходит как есть («2,5», «2.50») — в ключ
            # она обязана попасть в общем формате, иначе тотал не сойдётся
            # с той же линией другой БК
            pt = fmt_total(pt)
            out.append(MarketOdds(
                market=f"Тотал {pt}", market_key=f"total:{pt}",
                outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                k1=over, k2=under, **base))
    return out
