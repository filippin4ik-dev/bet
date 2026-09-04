"""Поиск двухисходных вилок по формуле 1/К1_max + 1/К2_max < 1.

Ключевая сложность вилок — сопоставить одно и то же событие у разных БК:
порядок команд может отличаться (Астана — Динамо / Динамо — Астана), а имена
записаны по-разному (Елимай ФК / Елимай). Поэтому:

- событие идентифицируется НЕупорядоченной парой нормализованных команд;
- исход привязан не к позиции (1/2), а к конкретной команде (для победителя)
  или к стороне тотала (over/under для линии pt);
- перебираются ВСЕ комбинации пар БК (кэф исхода 1 у одной БК × кэф
  исхода 2 у другой): выводится каждая комбинация с 1/К1 + 1/К2 < 1,
  а не только лучшая — пользователь видит все вилки, включая те, где
  кэф ниже максимального.
"""
import difflib
import functools
import itertools
import logging
import re
from collections import defaultdict
from typing import Iterable

from .config import (ARB_MAX_PROFIT, ARB_REJECTED_MAX,
                     ARB_UNMATCHED_CANDIDATES_MAX, BANKS, BOOKMAKER_FAMILIES,
                     COMBAT_ROUNDS_DEFAULT_RULE, COMBAT_ROUNDS_RULE_LABELS,
                     COMBAT_ROUNDS_RULES, FUZZY_NAME_CONTAINMENT,
                     FUZZY_NAME_CONTAINMENT_RATIO, FUZZY_NAME_EXTRA_WORDS,
                     FUZZY_NAME_MIN_SINGLE_RATIO, FUZZY_NAME_MIN_WORD,
                     FUZZY_NAME_SIM_THRESHOLD, START_TS_TOLERANCE,
                     START_TS_TOLERANCE_COMBAT, START_TS_TOLERANCE_RAPID)
from .models import Arb, Arb3, KIND_LIVE, KIND_PREMATCH, MarketOdds
from .parsers.html_utils import (canon_market_key, display_market, key_scope,
                                 neg_hcap as _neg_hcap)

log = logging.getLogger("arbitrage")

# Латиница, неотличимая на вид от кириллицы. БК смешивают раскладки прямо
# посреди слова: в живой линии рядом лежат «Эльверсберг» русской «е» и
# «Эльвeрsberg» латинской — на вид одно и то же, побуквенно разное.
# Складываем латиницу в кириллицу: одинаково выглядящие строки становятся
# одинаковыми и сравниваются как надо.
#
# Из-за складки все словари ниже тоже должны быть в сложенном виде («fc»
# превращается в «fс» с кириллической «с»), поэтому они и строятся через
# _fold, а не записаны буквами.
_HOMOGLYPHS = str.maketrans("acekmopxy", "асекморху")


def _fold(word: str) -> str:
    return word.translate(_HOMOGLYPHS)


# Слова-шумы в названиях команд, мешающие сопоставлению между БК.
# ВАЖНО: маркер женской команды («ж»/«жен») НЕ шум — без него мужской
# и женский матчи одинаковых клубов слились бы в одно событие.
_NOISE = {_fold(w) for w in
          ("фк", "fc", "хк", "hc", "бк", "bc", "clubs", "клуб")}
# Женские маркеры приводим к одному виду: «(ж)», «жен», «women», «w»
_FEMALE = {_fold(w) for w in ("ж", "жен", "женщины", "w", "women")}

# Номер дубля/резерва: «Серро Портеньо II» и «Серро Портеньо 2» — одна и та
# же команда, но побуквенно это разные строки, и на пороге похожести пара
# не проходила. Больше четырёх составов у клуба не бывает, а «i» и «v»
# по отдельности слишком часто оказываются инициалом или предлогом, чтобы
# считать их римскими цифрами.
_ROMAN = {"ii": "2", "iii": "3", "iv": "4"}

# Заглушки вместо имён команд. БК ставит их, когда состав пары ещё не
# объявлен, — и это НЕ имя: у Fonbet таких «событий» под сотню, у Melbet
# ещё больше, и все они сходятся в одну пару «хозяева/гости». Тогда каждый
# такой матч одной БК сопоставляется с каждым таким матчем другой, а это
# разные матчи разных лиг: кэфы у них любые, и «вилки» из этой каши —
# ложные. На живой линии шести БК так набиралось 82 вилки из 137.
_PLACEHOLDER_TEAMS = {_fold(t) for t in (
    "хозяева", "хозяин", "гости", "гость",
    "1 команда", "2 команда", "1 команда я", "2 команда я",
    "home", "away", "1 team", "2 team", "team1", "team2",
)}


# Имена команд повторяются в сотнях тысяч котировок (одно событие — десятки
# рынков у каждой БК), а нормализация не из дешёвых: на живой линии выходило
# около 4 млн вызовов и почти половина времени пересчёта вилок. Функция
# чистая, поэтому просто кэшируем результат по имени; размер кэша ограничен —
# уникальных имён за сутки десятки тысяч.
@functools.lru_cache(maxsize=200_000)
def norm_team(name: str) -> str:
    name = name.lower().replace("ё", "е").translate(_HOMOGLYPHS)
    name = re.sub(r"[^\w\s]", " ", name)
    words = []
    for w in name.split():
        if not w or w in _NOISE:
            continue
        if w in _FEMALE:
            words.append("ж")
            continue
        words.append(_ROMAN.get(w, w))
    return " ".join(sorted(words))  # порядок слов тоже не важен


# Один и тот же вид спорта под разными подписями. Корень делит линию на
# части, внутри которых движок ищет похожие написания имён (см.
# build_name_canon_map) и выбирает допуск времени старта (_start_tolerance):
# пока подписи расходились, «ММА», «UFC» и «Единоборства/UFC» жили каждая
# сама по себе, и на живой линии из 255 таких событий с другой БК склеились
# единицы — при том что у «Единоборства» склеивается две трети. Тот же
# счёт у гэльских игр («Гэльский футбол» и «Херлинг» — 146 событий, ноль
# склеек) и у регби, где подпись отличается регистром и пробелом.
#
# Заметьте: сводить сюда РАЗНЫЕ виды спорта нельзя (падел и пиклбол
# похожи, но это не одна игра) — общий корень нужен там, где БК спорят о
# названии, а не о сути.
#
# Ключи приводятся той же складкой раскладок, что и сама подпись: иначе
# латинская «ufc» из таблицы никогда не совпала бы с уже сложенной «ufс».
_SPORT_ALIASES = {_fold(k): v for k, v in {
    "мма": "единоборства",
    "mma": "единоборства",
    "ufc": "единоборства",
    "единоборства/ufc": "единоборства",
    "смешанные единоборства": "единоборства",
    "бои без правил": "единоборства",
    "кулачные бои": "единоборства",
    "регби лига": "регби",
    "регбилиг": "регби",
    "регби юнион": "регби",
    "регби союз": "регби",
    "гэльский футбол": "гэльский спорт",
    "гэльские виды спорта": "гэльский спорт",
    "херлинг": "гэльский спорт",
    "падел": "падел-теннис",
    "падель": "падел-теннис",
    "падел теннис": "падел-теннис",
    "падель теннис": "падел-теннис",
    # весь киберспорт BetBoom подписывает одним словом «Кибер» — то есть
    # четыре тысячи её котировок не сравнивались с киберспортом остальных
    "кибер": "киберспорт",
}.items()}


@functools.lru_cache(maxsize=20_000)
def sport_root(sport: str) -> str:
    """Корневой вид спорта из подписи котировки: «Футбол · Россия. РПЛ» →
    «футбол». Разные виды спорта не сопоставляются между собой, а лига в
    названии у каждой БК своя."""
    root = _fold(sport.split("·")[0].strip().lower().replace("ё", "е"))
    return _SPORT_ALIASES.get(root, root)


# Псевдо-лиги «Статистика»: в графе команды там стоит не команда, а ЧТО
# считают — «Крылья Советов удары по воротам», «Южная Корея (ж) (3-х очк.
# попадания)». Написания соседних таких «команд» отличаются на пару букв,
# а означают РАЗНЫЕ рынки, и фаззи-склейка имён сводила их в одно событие:
# на живой линии оттуда шли «вилки» по 30–66 % — то есть ставка на удары
# ПО воротам против ставки на удары ОТ ворот, проигрышная обеими ногами.
# Такие события сшиваются между БК только точным совпадением имён.
_STATS_LEAGUE_RE = re.compile(r"(?i)статистик")


def is_stats_league(sport: str) -> bool:
    """Событие из псевдо-лиги «Статистика» (см. _STATS_LEAGUE_RE)?"""
    return bool(_STATS_LEAGUE_RE.search(sport))


def _named(team1: str, team2: str) -> bool:
    """Пара опознаётся по именам, а не заглушками «Хозяева»/«Гости».

    Без имён событие не отличить от любого другого такого же, а вилка на
    двух РАЗНЫХ матчах — ставка в никуда."""
    t1, t2 = norm_team(team1), norm_team(team2)
    return bool(t1) and bool(t2) and t1 != t2 \
        and t1 not in _PLACEHOLDER_TEAMS and t2 not in _PLACEHOLDER_TEAMS


def _team_pair_similarity(a: tuple[str, str], b: tuple[str, str]) -> float:
    """Похожесть двух пар нормализованных имён команд (порядок не важен) —
    среднее по лучшему сопоставлению команда↔команда. Та же формула, что и
    в диагностике (app/diagnose_overlap.py, «почти совпадения»)."""
    straight = (difflib.SequenceMatcher(None, a[0], b[0]).ratio()
               + difflib.SequenceMatcher(None, a[1], b[1]).ratio()) / 2
    swapped = (difflib.SequenceMatcher(None, a[0], b[1]).ratio()
              + difflib.SequenceMatcher(None, a[1], b[0]).ratio()) / 2
    return max(straight, swapped)


class _UnionFind:
    """DSU для слияния РАЗНЫХ написаний имени одной и той же команды."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        p = self._parent.setdefault(x, x)
        if p != x:
            root = self.find(p)
            self._parent[x] = root
            return root
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # какой из двух станет корнем — не важно (это внутренний ключ,
            # наружу не показывается), лишь бы стабильно на одних данных
            if ra < rb:
                self._parent[rb] = ra
            else:
                self._parent[ra] = rb


def _fuzzy_window_cap(root: str) -> float:
    """Верхняя граница окна поиска фаззи-кандидатов по времени старта для
    корня вида спорта (сама граница финального решения не определяет —
    решение о склейке события всё равно принимает _time_clusters со своим
    допуском; здесь только чтобы не сравнивать вообще всё со всем). Больше
    максимального реального допуска смысла нет — такая пара всё равно не
    склеится позже."""
    return START_TS_TOLERANCE_COMBAT if root in _COMBAT_ROOTS \
        else START_TS_TOLERANCE


@functools.lru_cache(maxsize=200_000)
def _squad_marks(name: str) -> frozenset[str]:
    """Номера составов в имени: «Серро Портеньо 2» → {'2'}.

    Дубль клуба играет в своей лиге и со своими соперниками — это ДРУГАЯ
    команда, хотя от основы её отличает одна цифра. Побуквенно же они
    почти неразличимы, а по словам дубль — это основа плюс одно слово, то
    есть и порог похожести, и «вложение» сводят их в одно событие.

    Годятся только ОТДЕЛЬНО стоящие цифры от 2 до 9. Числа подлиннее в
    названии клуба означают не состав, а год основания («Шальке 04»,
    «1899 Хоффенхайм») или возраст («Хеллеруп ИК U19»), и требовать их
    совпадения нельзя: как раз эти написания у БК и разъезжаются. Единица
    отдельно тоже не состав, а номер клуба в городе («1. ФК Магдебург»).
    """
    return frozenset(w for w in name.split() if w in _SQUAD_DIGITS)


_SQUAD_DIGITS = frozenset("23456789")


@functools.lru_cache(maxsize=200_000)
def _name_sig(name: str) -> tuple[int, int]:
    """«Паспорт» имени для дешёвого предфильтра: длина и битовая маска
    встречающихся в нём символов.

    Имена повторяются в десятках рынков каждого события, поэтому с кэшем.
    Коллизии символов в маске (её ширина 61 бит) безопасны: они делают
    предфильтр только МЯГЧЕ, а значит не могут отсечь годную пару."""
    mask = 0
    for ch in name:
        mask |= 1 << (ord(ch) % 61)
    return len(name), mask


def _may_match(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """Может ли пара имён вообще дотянуть до FUZZY_NAME_MIN_SINGLE_RATIO —
    проверка ДО дорогого difflib, по одним лишь «паспортам» имён.

    Символ, которого во второй строке нет вовсе, не попадёт ни в один общий
    блок. Значит длина совпавшей части не больше, чем длина строки минус
    число её собственных символов-«чужаков», а похожесть
    (ratio = 2*совпало/(len_a+len_b)) не больше, чем даёт эта оценка сверху.
    Оценка честная (не отсекает ничего, что difflib признал бы похожим) и
    стоит два popcount вместо перебора блоков.

    Заодно покрывает и старую проверку длин: у строк с одинаковым набором
    символов оценка вырождается ровно в неё."""
    la, mask_a = a
    lb, mask_b = b
    fit = min(la - (mask_a & ~mask_b).bit_count(),
              lb - (mask_b & ~mask_a).bit_count())
    return 2 * fit >= FUZZY_NAME_MIN_SINGLE_RATIO * (la + lb)


# Сколько слов максимум разбираем при поиске вложения. Имя из десятка слов —
# это не команда, а строка мусора; полный перебор его подмножеств стоил бы
# дороже всего остального вместе взятого.
_CONTAIN_MAX_WORDS = 8


@functools.lru_cache(maxsize=200_000)
def _name_words(name: str) -> frozenset[str]:
    return frozenset(name.split())


def _containment_map(names: Iterable[str]) -> dict[str, str]:
    """Соответствие «сокращённое имя → его полное написание» внутри одного
    вида спорта, и только там, где полное написание ЕДИНСТВЕННОЕ.

    Сокращения — отдельная от опечаток беда: «Сувон» и «Сувон Самсунг» это
    одна команда, но побуквенная похожесть у них 0.55, то есть намного ниже
    FUZZY_NAME_SIM_THRESHOLD, и событие между БК не сшивалось (README про
    это прямо говорит как про известную потерю — такие вилки было видно
    только на вкладке «Отсеянные»).

    Единственность обязательна. «Манчестер» вкладывается и в «Манчестер
    Юнайтед», и в «Манчестер Сити»; слив его с любым из них, мы через DSU
    сделали бы Юнайтед и Сити ОДНОЙ командой — и получили бы вилку из цен
    двух разных матчей. Поэтому имя, у которого продолжений больше одного,
    из соответствия выбрасывается целиком.
    """
    names = {n for n in names if n}
    out: dict[str, set[str]] = defaultdict(set)
    for long in names:
        words = _name_words(long)
        n = len(words)
        if n < 2 or n > _CONTAIN_MAX_WORDS:
            continue
        ordered = sorted(words)
        # подмножества, отличающиеся на 1..FUZZY_NAME_EXTRA_WORDS слов
        for drop in range(1, min(FUZZY_NAME_EXTRA_WORDS, n - 1) + 1):
            for gone in itertools.combinations(ordered, drop):
                short = " ".join(w for w in ordered if w not in gone)
                if short in names and short != long:
                    out[short].add(long)
    return {short: next(iter(longs)) for short, longs in out.items()
            if len(longs) == 1
            and max(len(w) for w in _name_words(short)) >= FUZZY_NAME_MIN_WORD}


def _contained(contain: dict[str, str], a: str, b: str) -> bool:
    """Одно из имён — однозначное сокращение другого (см. _containment_map)."""
    return contain.get(a) == b or contain.get(b) == a


class TimeSplit:
    """Одно событие, разошедшееся у разных БК по времени старта.

    Допуск времени — защита от склейки первого и ответного матчей одной
    пары (см. _time_clusters). Но у той же защиты есть цена: если одна БК
    ошиблась со временем, её кэфы выпадают из события молча. Запись
    сохраняет обе стороны разрыва, чтобы это можно было увидеть.
    """

    __slots__ = ("teams", "ts_a", "ts_b", "books_a", "books_b", "tol",
                 "sport")

    def __init__(self, teams: frozenset, ts_a: float, ts_b: float,
                 books_a: set[str], books_b: set[str], tol: float,
                 sport: str) -> None:
        self.teams = teams
        self.ts_a, self.ts_b = ts_a, ts_b
        self.books_a, self.books_b = books_a, books_b
        self.tol, self.sport = tol, sport

    @property
    def gap(self) -> float:
        return abs(self.ts_b - self.ts_a)


def build_name_canon_map(odds: list[MarketOdds]) -> dict[str, str]:
    """Строит соответствие «сырое нормализованное имя команды» → каноническое,
    сливая РАЗНЫЕ написания одной и той же команды/игрока между БК
    (транслитерация, опечатки, сокращения — см. FUZZY_NAME_SIM_THRESHOLD).

    Диагностика (app/diagnose_overlap.py) на живых данных нашла тысячи пар
    событий с ИДЕНТИЧНЫМИ соперниками и близким временем старта, которые не
    склеивались только из-за разного написания имени одной из команд у
    разных БК («Кимчхон Санму»/«Кимчхон Сангму», «Виктория Плзень»/
    «Виктория Пльзень» и т.п.) — из-за этого терялись реальные вилки.

    Алгоритм: сводим линию к одной записи на «корень вида спорта + пара
    нормализованных имён» (какие БК её показывают и в какое окно времени),
    группируем записи по корню вида спорта и внутри группы (отсортировав по
    началу окна) сравниваем близкие по времени пары имён — если похожесть
    пары высокая (FUZZY_NAME_SIM_THRESHOLD) и каждая команда по отдельности
    тоже похожа (FUZZY_NAME_MIN_SINGLE_RATIO, защита от «одна команда 100%
    совпала, другая — нет»), объединяем соответствующие ИМЕНА через DSU. НЕ
    объединяем два написания, которые встречаются ТОЛЬКО у одной и той же
    БК, — это заведомо разные матчи. Итоговая склейка события ВСЁ РАВНО
    дополнительно проверяется по времени старта в _time_clusters с допуском
    конкретного вида спорта — ошибочное слияние имён само по себе ложную
    вилку не создаст.

    ВАЖНО: в сравнении участвуют ВСЕ написания, включая те, что уже совпали
    точно у 2+ БК. Раньше такие отбрасывались ради экономии сравнений — и
    это молча теряло вилки тем чаще, чем больше БК в наборе: если две БК
    пишут «Црвена Звезда» одинаково, а третья — «Црвена Зведза», то общее
    написание выбывало из поиска как «уже совпавшее», третья БК оставалась
    без пары, и её кэфы не попадали в событие вообще. Экономия теперь
    другая и безопасная: одинаковые написания разных БК схлопываются в ОДНУ
    запись (сравнивать «Црвена Звезда» с «Црвена Зведза» пять раз, по разу
    на БК, незачем).

    Пары, которые сюда НЕ прошли (похожи, но ниже порога), не пропадают
    совсем: их отдельно, по общим словам в названиях, ищет
    app/unmatched.py — и показывает на вкладке «Отсеянные», если из-за
    несклейки теряется вилка.
    """
    # одна запись на (корень вида спорта, пара имён): какие БК её дают и в
    # какое окно времени укладываются их времена старта
    events: dict[tuple, dict] = {}
    for o in odds:
        # Время старта нужно как якорь окна кандидатов; лайв тоже участвует
        # — у лайв-котировки это время начала уже идущего матча, якорь не
        # хуже прематчевого. Раньше лайв отсекался, а лайв-сканер держит
        # ТОЛЬКО лайв — то есть карта имён у него выходила пустой всегда, и
        # любое расхождение в написании стоило лайв-вилки целиком.
        #
        # Лайв-котировка БЕЗ времени старта участвует тоже, просто без окна
        # (см. ниже). Лайв-парсеры, в отличие от прематчевых, время старта
        # НЕ требуют — матч уже идёт, и отбрасывать его не за что, — так что
        # у BetBoom, Fonbet, Melbet и bc.game оно вполне может не прийти. А
        # событие в лайве по времени и не разбивается (_time_clusters
        # работает только по прематчу): якоря там нет ни у кого, терять
        # нечего. Прематч без времени старта по-прежнему мимо: там окно —
        # единственное, что отделяет первый матч пары от ответного.
        if not o.start_ts and o.kind != KIND_LIVE:
            continue
        if not _named(o.team1, o.team2):
            continue
        if is_stats_league(o.sport):
            continue  # там имя — это рынок, а не команда (см. выше)
        t1, t2 = norm_team(o.team1), norm_team(o.team2)
        root = sport_root(o.sport)
        key = (root, frozenset((t1, t2)))
        tol = _start_tolerance(o.sport)
        ev = events.get(key)
        if ev is None:
            events[key] = {"books": {o.bookmaker}, "lo": o.start_ts,
                           "hi": o.start_ts, "tol": tol}
            continue
        ev["books"].add(o.bookmaker)
        ev["tol"] = max(ev["tol"], tol)
        if o.start_ts:
            # у события хватит одной котировки со временем, чтобы получить
            # окно: остальные БК того же события просто попадут в него
            ev["lo"] = o.start_ts if ev["lo"] is None \
                else min(ev["lo"], o.start_ts)
            ev["hi"] = o.start_ts if ev["hi"] is None \
                else max(ev["hi"], o.start_ts)

    # события с окном — отдельно от бессрочных: у первых кандидатов
    # ограничивает время, вторых сравниваем со всем корнем вида спорта
    timed: dict[str, list[tuple]] = defaultdict(list)
    loose: dict[str, list[tuple]] = defaultdict(list)
    for (root, teams), ev in events.items():
        pair = tuple(teams)
        item = (pair, _name_sig(pair[0]), _name_sig(pair[1]),
                ev["books"], ev["lo"], ev["hi"], ev["tol"])
        (timed if ev["lo"] is not None else loose)[root].append(item)

    # «Сокращённое имя → полное» считается ОДИН раз на вид спорта и по всем
    # его именам сразу: единственность продолжения (см. _containment_map)
    # можно проверить только зная их все, а не по отдельной паре событий.
    contain_by_root: dict[str, dict[str, str]] = {}
    if FUZZY_NAME_CONTAINMENT:
        for root in set(timed) | set(loose):
            names = {name
                     for item in itertools.chain(timed.get(root, ()),
                                                 loose.get(root, ()))
                     for name in item[0]}
            contain_by_root[root] = _containment_map(names)

    dsu = _UnionFind()
    for root, items in timed.items():
        items.sort(key=lambda x: x[4])  # по началу окна старта
        cap = _fuzzy_window_cap(root)
        contain = contain_by_root.get(root)
        n = len(items)
        for i in range(n):
            hi_i, tol_i = items[i][5], items[i][6]
            for j in range(i + 1, n):
                # нижняя оценка расстояния между временами старта: окна
                # отсортированы по началу, поэтому оценка только растёт
                gap = items[j][4] - hi_i
                if gap > cap:
                    break
                if gap > max(tol_i, items[j][6]):
                    continue
                _merge_if_alike(dsu, items[i], items[j], contain)

    for root, items in loose.items():
        # бессрочных событий немного (только лайв и только там, где БК не
        # прислала время), поэтому сравнение со всем корнем не дорого
        rest = timed.get(root, ())
        contain = contain_by_root.get(root)
        for i, item in enumerate(items):
            for other in itertools.chain(items[i + 1:], rest):
                _merge_if_alike(dsu, item, other, contain)

    return {name: dsu.find(name) for name in dsu._parent}


def _team_ratio(x: str, y: str, sig_x: tuple, sig_y: tuple,
                contain: dict[str, str]) -> float:
    """Похожесть имён одной команды: побуквенная или «вложение».

    Побуквенную считает difflib, но только если дешёвый предфильтр не
    отверг пару сразу. Вложение (сокращённое имя против полного) difflib не
    ловит в принципе — «сувон» против «самсунг сувон» это 0.55, — поэтому
    оно проверяется отдельно и предфильтр на него не распространяется:
    предфильтр оценивает как раз побуквенное сходство, которого здесь и не
    должно быть.
    """
    if _squad_marks(x) != _squad_marks(y):
        return -1.0    # основа и дубль — разные команды (см. _squad_marks)
    if contain and _contained(contain, x, y):
        return FUZZY_NAME_CONTAINMENT_RATIO
    if not _may_match(sig_x, sig_y):
        return -1.0
    return difflib.SequenceMatcher(None, x, y).ratio()


def _merge_if_alike(dsu: _UnionFind, a: tuple, b: tuple,
                    contain: dict[str, str] | None = None) -> None:
    """Сливает имена двух пар команд, если они достаточно похожи.

    Решение о времени старта принимает вызывающий — здесь только имена."""
    pair_a, sig_a0, sig_a1, books_a = a[0], a[1], a[2], a[3]
    pair_b, sig_b0, sig_b1, books_b = b[0], b[1], b[2], b[3]
    if len(books_a) == 1 and books_a == books_b:
        return  # оба написания только у ОДНОЙ БК — это разные её матчи
    contain = contain or {}
    r00 = _team_ratio(pair_a[0], pair_b[0], sig_a0, sig_b0, contain)
    r11 = _team_ratio(pair_a[1], pair_b[1], sig_a1, sig_b1, contain)
    straight_ok = r00 >= 0 and r11 >= 0
    r01 = _team_ratio(pair_a[0], pair_b[1], sig_a0, sig_b1, contain)
    r10 = _team_ratio(pair_a[1], pair_b[0], sig_a1, sig_b0, contain)
    swapped_ok = r01 >= 0 and r10 >= 0
    if not straight_ok and not swapped_ok:
        return  # ни в одной ориентации имена не дотянут до порога
    straight = (r00 + r11) / 2 if straight_ok else -1.0
    swapped = (r01 + r10) / 2 if swapped_ok else -1.0
    if straight >= swapped:
        avg, ra, rb = straight, r00, r11
        match0, match1 = pair_b[0], pair_b[1]
    else:
        avg, ra, rb = swapped, r01, r10
        match0, match1 = pair_b[1], pair_b[0]
    if avg < FUZZY_NAME_SIM_THRESHOLD \
            or min(ra, rb) < FUZZY_NAME_MIN_SINGLE_RATIO:
        # Похоже, но не настолько, чтобы решать за человека: ошибочное
        # слияние сводит в «вилку» цены РАЗНЫХ матчей. Такие пары
        # разбирает app/unmatched.py — показывает их человеку, если из-за
        # несклейки теряется вилка.
        return
    if pair_a[0] != match0:
        dsu.union(pair_a[0], match0)
    if pair_a[1] != match1:
        dsu.union(pair_a[1], match1)


def _canon(name_map: dict[str, str] | None, name: str) -> str:
    return name_map.get(name, name) if name_map else name


def calc_stakes(k1: float, k2: float, bank: float) -> dict:
    """Распределение банка между двумя исходами (равный выигрыш при любом)."""
    s = 1 / k1 + 1 / k2
    stake1 = bank * (1 / k1) / s
    stake2 = bank - stake1
    payout = stake1 * k1
    return {
        "stake1": round(stake1),
        "stake2": round(stake2),
        "payout": round(payout, 2),
        "profit": round(payout - bank, 2),
    }


def calc_stakes3(k1: float, kx: float, k2: float, bank: float) -> dict:
    """То же для трёхисходной вилки (рынок «Исход 1X2»: П1/X/П2)."""
    s = 1 / k1 + 1 / kx + 1 / k2
    stake1 = bank * (1 / k1) / s
    stakex = bank * (1 / kx) / s
    stake2 = bank - stake1 - stakex
    payout = stake1 * k1
    return {
        "stake1": round(stake1),
        "stakex": round(stakex),
        "stake2": round(stake2),
        "payout": round(payout, 2),
        "profit": round(payout - bank, 2),
    }


class _Outcome:
    """Лучший кэф по одному исходу у ОДНОЙ БК."""

    __slots__ = ("oid", "label", "odds", "bookmaker", "url")

    def __init__(self, oid: str, label: str, odds: float, bookmaker: str,
                 url: str | None = None) -> None:
        self.oid, self.label, self.odds = oid, label, odds
        self.bookmaker = bookmaker
        self.url = url


def _explode(o: MarketOdds, name_map: dict[str, str] | None = None):
    """Разбивает котировку на два канонических исхода (id, метка, кэф).

    id — устойчивый между БК идентификатор исхода:
    - победитель: нормализованное (и, если есть, фаззи-каноничное — см.
      build_name_canon_map) имя команды;
    - тотал: 'over:<pt>' / 'under:<pt>';
    - фора: 'hcap:<команда>:<знаковая линия>' — исход привязан к КОМАНДЕ и
      её линии. Фора team1(-1.5) сшивается с team2(+1.5) другой БК как две
      стороны одного рынка, а team1(+1.5) — уже другой рынок (другой
      фаворит), ложных вилок не будет.

    name_map — соответствие «сырое имя → каноническое» (build_name_canon_map):
    без него, если БК пишут имя команды чуть по-разному, «Победитель»/фора/
    инд.тотал не сошьются между БК даже при совпавшем событии.
    """
    key = canon_market_key(o.market_key)
    if key.startswith("itotal"):
        # itotal:<сторона 1|2>:<scope>:<линия> — тотал ОДНОЙ команды.
        # Исход привязан к нормализованному имени команды: у БК с
        # перевёрнутым порядком команд тот же рынок сшивается корректно.
        _, side, scope, pt = key.split(":", 3)
        team = _canon(name_map, norm_team(o.team1 if side == "1" else o.team2))
        return [
            (f"itover:{team}:{scope}:{pt}", o.outcome1, o.k1),
            (f"itunder:{team}:{scope}:{pt}", o.outcome2, o.k2),
        ]
    if key.startswith("total"):
        pt = key.split(":", 1)[1] if ":" in key else ""
        return [
            (f"over:{pt}", o.outcome1, o.k1),
            (f"under:{pt}", o.outcome2, o.k2),
        ]
    if key.startswith("hcap"):
        # hcap:<scope>:<линия team1>
        h1 = key.rsplit(":", 1)[1]
        t1 = _canon(name_map, norm_team(o.team1))
        t2 = _canon(name_map, norm_team(o.team2))
        return [
            (f"hcap:{t1}:{h1}", o.outcome1, o.k1),
            (f"hcap:{t2}:{_neg_hcap(h1)}", o.outcome2, o.k2),
        ]
    if o.market_key.startswith("bothscore"):
        # «Обе забьют»: исходы Да/Нет не зависят от порядка команд
        return [
            ("bts:yes", o.outcome1, o.k1),
            ("bts:no", o.outcome2, o.k2),
        ]
    if o.market_key.startswith("oddeven"):
        # «Чет/Нечет»: исходы не зависят от порядка команд
        return [
            ("oe:even", o.outcome1, o.k1),
            ("oe:odd", o.outcome2, o.k2),
        ]
    # победитель (в т.ч. дочерние росписи вроде winner:2сет)
    return [
        (f"team:{_canon(name_map, norm_team(o.team1))}", o.outcome1, o.k1),
        (f"team:{_canon(name_map, norm_team(o.team2))}", o.outcome2, o.k2),
    ]


def _market_group(o: MarketOdds, name_map: dict[str, str] | None = None) -> str:
    """Ключ рынка, ОДИНАКОВЫЙ у обеих БК независимо от порядка команд.

    Для победителя/тотала подходит сам market_key. Для форы линия зависит
    от того, какая команда «первая», поэтому привязываем знак к
    алфавитно-первой нормализованной (каноничной — см. name_map) команде —
    тогда обе стороны рынка (и при перевёрнутом порядке команд у другой БК)
    попадают в одну группу. Без канонизации имени алфавитный порядок мог
    отличаться между БК из-за опечатки/транслитерации в имени — и анкер
    фор выбирался бы по-разному, ломая сопоставление. Индивидуальный тотал
    привязываем к нормализованному ИМЕНИ команды — сторона (1/2) у разных
    БК может быть разной.

    Ключ парсера сначала приводится к канону (canon_market_key): БК
    расходились в записи рынка без scope и в формате линии, и один и тот
    же рынок попадал в РАЗНЫЕ группы — вилка по нему не находилась вовсе.
    """
    key = canon_market_key(o.market_key)
    if key.startswith("itotal"):
        _, side, scope, pt = key.split(":", 3)
        team = _canon(name_map, norm_team(o.team1 if side == "1" else o.team2))
        return f"itotal:{team}:{scope}:{pt}"
    if not key.startswith("hcap"):
        return key
    prefix, h1 = key.rsplit(":", 1)  # prefix = hcap:<scope>
    a = _canon(name_map, norm_team(o.team1))
    b = _canon(name_map, norm_team(o.team2))
    anchor = h1 if a <= b else _neg_hcap(h1)
    return f"{prefix}:{anchor}"


# Корневые виды спорта, где время начала — ОЦЕНОЧНОЕ (бой на карде MMA/
# бокса начинается «после предыдущего») и у разных БК расходится на часы.
# Для них действует большой допуск START_TS_TOLERANCE_COMBAT: одна пара
# бойцов не дерётся дважды за день, ложной склейки не будет.
#
# Подписи «ММА», «UFC», «Смешанные единоборства» и «Бои без правил» сюда
# больше не входят: их сводит к «единоборствам» ещё sport_root
# (_SPORT_ALIASES). Раньше эти три подписи мимо набора и проходили — и
# бои с них считались обычным, часовым допуском, хотя расходятся у БК как
# раз на часы.
_COMBAT_ROOTS = {"единоборства", "бокс", "кикбоксинг", "муай-тай"}

# «Быстрые» турниры/лиги, где одна и та же пара соперников играет НЕСКОЛЬКО
# матчей за вечер с интервалом в минуты — виртуальный футбол и похожие
# скоростные форматы обычно прямо указывают длительность матча в названии
# турнира/чемпионата, например «FC 26. ... 2x3 мин.» или «H2H LIGA-3. 2x4
# мин.». Для них НЕЛЬЗЯ расширять START_TS_TOLERANCE (см. ниже) — иначе два
# разных матча той же пары в одну сессию склеятся в одну «вилку».
#
# Длительность считается и по-английски: у крипто-БК на платформе BetBy
# турниры подписаны латиницей («NBA 2K26 · H2H GG League (4x5 min)»), и на
# «мин» они не отзывались — то есть самым опасным лигам, где одна пара
# играет каждые десять минут, доставался обычный часовой допуск.
_RAPID_FIXTURE_RE = re.compile(r"(?i)\d+\s*[xх]\s*\d+\s*(?:мин|min)")


@functools.lru_cache(maxsize=20_000)
def _start_tolerance(sport: str) -> float:
    """Допуск расхождения времени старта для вида спорта котировки.

    Вызывается по каждой котировке (миллионы раз за пересчёт) при считанных
    сотнях разных значений — поэтому с кэшем."""
    root = sport_root(sport)
    if root in _COMBAT_ROOTS:
        return START_TS_TOLERANCE_COMBAT
    if _RAPID_FIXTURE_RE.search(sport):
        return START_TS_TOLERANCE_RAPID
    return START_TS_TOLERANCE


# ---------- отсев кандидатов: почему вилка НЕ показана ----------
#
# Отсев раньше был молчаливым: кандидат просто не попадал в список, и
# понять, спрятал движок чужую ошибку БК или свою собственную, можно было
# только по строчке в логе. Теперь у каждой причины есть код, а сами
# отсеянные кандидаты движок отдаёт вызывающему (параметр rejected) —
# сканер держит их для вкладки «Отсеянные», где строку видно целиком и её
# можно проверить руками на сайтах БК.
REJECT_MAX_PROFIT = "max_profit"
REJECT_COMBAT_ROUNDS = "combat_rounds"
# Кандидаты, до перебора кэфов вообще не дошедшие: событие не сшилось между
# БК (разное написание имён или разное время старта). Их находит отдельный
# проход — app/unmatched.py.
REJECT_UNMATCHED_NAME = "unmatched_name"
REJECT_UNMATCHED_TIME = "unmatched_time"

# Виды рынков, единица счёта которых в единоборствах — РАУНД (тотал
# раундов, фора по раундам, чет/нечет раундов). Победитель боя от правил
# подсчёта раундов не зависит и в список не входит.
_ROUND_COUNT_MARKETS = ("total", "itotal", "hcap", "oddeven")


def _families() -> dict[str, int]:
    """Имя БК → номер её семьи-маркетмейкера (см. BOOKMAKER_FAMILIES)."""
    out: dict[str, int] = {}
    for i, group in enumerate(BOOKMAKER_FAMILIES):
        for bk in group:
            out[bk] = i
    return out


_FAMILY_OF = _families()


def same_market_maker(bk1: str, bk2: str) -> bool:
    """Считает ли этим двум БК линию один и тот же маркетмейкер.

    Такая пара — не вилка, даже когда 1/К1 + 1/К2 < 1. Площадки одной
    платформы берут кэфы из общего фида и отличаются только своей маржой:
    прибыль между ними — это либо мгновенный рассинхрон фида, либо ошибка
    разбора, а ставка ложится в ОДНУ книгу риска, где обе ноги режут
    вместе. См. BOOKMAKER_FAMILIES.
    """
    a = _FAMILY_OF.get(bk1.strip().lower())
    return a is not None and a == _FAMILY_OF.get(bk2.strip().lower())


def _rounds_rule(bookmaker: str) -> str:
    """По какому правилу БК засчитывает раунд (см. COMBAT_ROUNDS_RULES)."""
    return COMBAT_ROUNDS_RULES.get(bookmaker.strip().lower(),
                                   COMBAT_ROUNDS_DEFAULT_RULE)


def _rule_label(rule: str) -> str:
    return COMBAT_ROUNDS_RULE_LABELS.get(rule, rule)


def _counts_rounds(sport: str, market_key: str) -> bool:
    """Рынок считает РАУНДЫ боя: тотал/фора/чет-нечет всего боя в
    единоборствах. Рынки с уточнением (scope) — «тотал ударов», «исход
    2-го раунда» — считают не раунды, их правило не касается."""
    if sport_root(sport) not in _COMBAT_ROOTS:
        return False
    key = canon_market_key(market_key)
    return key.startswith(_ROUND_COUNT_MARKETS) and not key_scope(key)


def _reject_reason(sample: MarketOdds, bk1: str, bk2: str, profit_pct: float,
                   max_profit: float) -> tuple[str, str] | None:
    """Почему этот кандидат нельзя показывать вилкой: (код, объяснение).

    None — с кандидатом всё в порядке, это настоящая вилка.
    """
    if _counts_rounds(sample.sport, sample.market_key):
        r1, r2 = _rounds_rule(bk1), _rounds_rule(bk2)
        if r1 != r2:
            # Одинаковая на вид линия «Тотал 2.5» у этих БК означает РАЗНЫЕ
            # события: бой, остановленный в середине раунда, у одной БК
            # считается как N раундов, у другой — как N+1. ТБ у одной и ТМ
            # у другой могут проиграть ОБА — это не вилка, а минус.
            return (REJECT_COMBAT_ROUNDS,
                    f"{bk1} и {bk2} считают раунды по-разному "
                    f"({bk1}: {_rule_label(r1)}; {bk2}: {_rule_label(r2)}) — "
                    f"при остановке боя в середине раунда обе ставки могут "
                    f"проиграть")
    if profit_pct > max_profit:
        return (REJECT_MAX_PROFIT,
                f"доходность выше потолка ARB_MAX_PROFIT={max_profit:g}% — "
                f"обычно это ошибка сопоставления (разные матчи или рынки "
                f"у БК), но бывает и настоящая ошибка букмекера")
    return None


def _has_room(rejected: list | None) -> bool:
    """Есть ли ещё место в списке отсеянных (см. ARB_REJECTED_MAX).

    Список нужен для ручного разбора, а не как архив: одна ошибка
    сопоставления даёт десятки строк, и без потолка он рос бы вместе с
    линией. Всё, что сверх потолка, остаётся только в логе."""
    return rejected is not None and len(rejected) < ARB_REJECTED_MAX


def _ambiguous_pairs(ts_by_book: dict[tuple, set],
                     combat: set[tuple]) -> set[tuple]:
    """События, где пара соперников встречается сегодня НЕ ОДИН раз.

    Признак берём у самой БК: если одна контора выставила два матча одной
    пары с разным временем старта, значит соперники играют дважды — и
    широкий допуск по времени становится опасен. Третья БК, у которой этот
    матч записан с расхождением в час, приклеится к соседнему матчу, и
    получится «вилка» из цен двух РАЗНЫХ матчей.

    Так ломался, например, Esports World Cup: Fonbet выставлял Wildcard —
    FURIA дважды, по Counter-Strike в 21:00 и по Rainbow Six в 17:00, а
    Winline дисциплину в названии не указывал вовсе («Киберспорт · Esports
    World Cup», старт 20:00). Час разницы укладывался в допуск, цена
    Rainbow Six считалась против цены Counter-Strike, и выходила «вилка»
    на 65 %. Различать дисциплины по названию нельзя — у Winline его в
    строке просто нет; зато сам факт повтора пары виден из данных.

    Единоборства исключены: там допуск большой намеренно (БК сильно
    расходятся в оценке времени боя), а одна пара бойцов дважды за день
    не дерётся.
    """
    ambiguous: set[tuple] = set()
    for (key, _bk), ts_set in ts_by_book.items():
        if key in ambiguous or key in combat or len(ts_set) < 2:
            continue
        if max(ts_set) - min(ts_set) > START_TS_TOLERANCE_RAPID:
            ambiguous.add(key)
    return ambiguous


def _time_clusters(odds: list[MarketOdds],
                   name_map: dict[str, str] | None = None,
                   splits: list[TimeSplit] | None = None) -> dict[tuple, dict]:
    """Кластеры времени старта по каждому событию (kind, пара команд).

    Одна и та же пара команд может играть НЕСКОЛЬКО матчей (первый и
    ответный, мужской и женский в один день, разные лиги, повторы у
    «быстрых» турниров — см. _RAPID_FIXTURE_RE). Если у двух БК время
    старта различается больше допуска — это разные матчи, их кэфы нельзя
    сшивать в одну вилку. Часовые пояса БК уже приведены к общему
    unix-времени; для обычных видов спорта допуск умеренный
    (START_TS_TOLERANCE), для «быстрых» турниров с повторами пары —
    строгий (START_TS_TOLERANCE_RAPID), для единоборств — большой
    (START_TS_TOLERANCE_COMBAT): там время боя оценочное.

    Отдельно строгий допуск включается там, где повтор пары виден прямо в
    данных: одна БК выставила два матча одних и тех же соперников — см.
    _ambiguous_pairs. Название турнира об этом знать не обязано.

    name_map (build_name_canon_map) сливает разные написания имени одной
    команды между БК ДО группировки по паре команд — иначе такие пары
    вообще не встретились бы в одном ключе события.

    splits — необязательный список, куда складываются события, которые
    допуск РАЗВЁЛ по разным кластерам, причём в разных кластерах оказались
    разные БК (см. TimeSplit). Обычно это защита от склейки первого и
    ответного матчей, но иногда — просто ошибка времени у одной БК, и
    тогда её кэфы молча выпадают из события. Вкладка «Отсеянные»
    показывает такие случаи, если из-за них теряется вилка.
    """
    ts_by_event: dict[tuple, set] = defaultdict(set)
    tol_by_event: dict[tuple, float] = {}
    sport_by_event: dict[tuple, str] = {}
    combat: set[tuple] = set()
    # (событие, БК) -> времена старта у ЭТОЙ БК: по ним видно, что пара
    # встречается сегодня не один раз (см. _ambiguous ниже)
    ts_by_book: dict[tuple, set] = defaultdict(set)
    for o in odds:
        if o.kind == KIND_PREMATCH and o.start_ts \
                and _named(o.team1, o.team2):
            teams = frozenset((_canon(name_map, norm_team(o.team1)),
                               _canon(name_map, norm_team(o.team2))))
            key = (o.kind, teams)
            ts_by_event[key].add(o.start_ts)
            ts_by_book[(key, o.bookmaker)].add(o.start_ts)
            sport_by_event.setdefault(key, o.sport)
            tol = _start_tolerance(o.sport)
            if tol == START_TS_TOLERANCE_COMBAT:
                combat.add(key)
            if tol > tol_by_event.get(key, 0.0):
                tol_by_event[key] = tol
    ambiguous = _ambiguous_pairs(ts_by_book, combat)
    clusters: dict[tuple, dict] = {}
    for key, ts_set in ts_by_event.items():
        tol = tol_by_event.get(key, START_TS_TOLERANCE)
        if key in ambiguous:
            tol = min(tol, START_TS_TOLERANCE_RAPID)
        mapping: dict[float, int] = {}
        cluster, prev = 0, None
        for ts in sorted(ts_set):
            if prev is not None and ts - prev > tol:
                cluster += 1
            mapping[ts] = cluster
            prev = ts
        clusters[key] = mapping
        if splits is not None and cluster:
            _record_splits(splits, key, mapping, ts_by_book, tol,
                           sport_by_event.get(key, ""))
    return clusters


def _record_splits(splits: list[TimeSplit], key: tuple, mapping: dict,
                   ts_by_book: dict[tuple, set], tol: float,
                   sport: str) -> None:
    """Запоминает соседние кластеры одного события, в которых стоят РАЗНЫЕ
    БК (см. TimeSplit).

    Только соседние: разрыв между крайними кластерами — это уже заведомо
    разные матчи (первый и ответный через неделю), а спорным бывает
    ближайший. Кластер, где стоит та же БК, что и в соседнем, пропускаем:
    контора, выставившая пару дважды, лучше всех знает, что матчей два.
    """
    books_by_cluster: dict[int, set[str]] = defaultdict(set)
    ts_by_cluster: dict[int, list[float]] = defaultdict(list)
    for (ev_key, bk), ts_set in ts_by_book.items():
        if ev_key != key:
            continue
        for ts in ts_set:
            books_by_cluster[mapping[ts]].add(bk)
    for ts, cl in mapping.items():
        ts_by_cluster[cl].append(ts)
    for cl in range(max(mapping.values())):
        books_a, books_b = books_by_cluster[cl], books_by_cluster[cl + 1]
        if not books_a.isdisjoint(books_b):
            continue
        if len(splits) >= ARB_UNMATCHED_CANDIDATES_MAX:
            return
        splits.append(TimeSplit(
            key[1], max(ts_by_cluster[cl]), min(ts_by_cluster[cl + 1]),
            books_a, books_b, tol, sport))


def find_arbs(odds: Iterable[MarketOdds],
              name_map: dict[str, str] | None = None,
              max_profit: float | None = None,
              rejected: list[Arb] | None = None,
              time_clusters: dict[tuple, dict] | None = None) -> list[Arb]:
    """max_profit — потолок доходности; по умолчанию ARB_MAX_PROFIT.

    Задавать его отдельно нужно диагностике (app.diagnose_overlap): она
    считает линию дважды и показывает, что именно потолок отбросил. Иначе
    отсев виден только строкой в логе, и понять, прячет ли порог ошибку
    сопоставления или настоящую щедрость БК, нельзя.

    rejected — необязательный список, куда складываются ОТСЕЯННЫЕ
    кандидаты (арифметически вилка есть, но показывать её нельзя — см.
    _reject_reason): у каждого проставлены reject_code и reject_reason. По
    нему живёт вкладка «Отсеянные»: причина отсева видна словами, и строку
    можно проверить руками на сайтах БК. Без списка поведение прежнее —
    такие кандидаты просто пропускаются.

    time_clusters — готовые кластеры времени старта (_time_clusters). Оба
    движка считают их по одной и той же линии, поэтому сканер считает их
    один раз и передаёт сюда. Пустой словарь означает «не разбивать
    событие по времени вовсе» — так проверяются пары, которые как раз и
    развёл допуск времени (app/unmatched.py).
    """
    if max_profit is None:
        max_profit = ARB_MAX_PROFIT
    odds = list(odds)
    if name_map is None:
        name_map = build_name_canon_map(odds)
    if time_clusters is None:
        time_clusters = _time_clusters(odds, name_map)

    # market_group одинаков у одного рынка одного события независимо от БК
    # и порядка команд: kind | пара_команд | кластер_времени | вид_рынка
    groups: dict[tuple, dict] = defaultdict(lambda: {
        "outcomes": {}, "sample": None, "books": set(),
    })

    for o in odds:
        if o.market_key.startswith("winner1x2"):
            # Трёхисходный рынок «Исход 1X2» — отдельный движок
            # find_arbs_1x2. Смешивать его сюда ОПАСНО: если считать вилку
            # только по П1/П2, игнорируя цену на ничью, ставка не покрывает
            # исход «ничья» — это НЕ гарантированная прибыль, а обычная
            # игра на «без ничьей», хоть маржа 1/К1+1/К2 и меньше 1.
            continue
        if not (o.k1 and o.k2 and o.k1 > 1 and o.k2 > 1):
            continue
        if o.k3:
            # трёхисходный рынок (исход 1X2): пара П1/П2 — НЕ весь рынок,
            # ничья не покрыта. Считать его двухисходным значит показать
            # вилку, которой нет: ставка по ней теряет деньги на ничьей.
            # Такие рынки разбирает find_arbs_1x2, по всем трём исходам.
            continue
        if not _named(o.team1, o.team2):
            continue
        teams = frozenset((_canon(name_map, norm_team(o.team1)),
                           _canon(name_map, norm_team(o.team2))))
        if len(teams) < 2:
            continue
        # разные матчи одной пары команд (по времени старта) — разные группы
        cluster = 0
        if o.kind == KIND_PREMATCH and o.start_ts:
            cluster = time_clusters.get((o.kind, teams), {}).get(o.start_ts, 0)
        # вид рынка: winner / winner:<роспись> / total:<pt> / hcap:<линия>.
        # Для форы группа привязана к алфавитно-первой команде (см. _market_group)
        market_group = _market_group(o, name_map)
        key = (o.kind, teams, cluster, market_group)
        g = groups[key]
        g["sample"] = g["sample"] or o
        g["books"].add(o.bookmaker)
        for oid, label, k in _explode(o, name_map):
            # по каждому исходу храним лучший кэф КАЖДОЙ БК отдельно —
            # дальше перебираются все комбинации пар БК
            per_bk = g["outcomes"].setdefault(oid, {})
            cur = per_bk.get(o.bookmaker)
            if cur is None or k > cur.odds:
                per_bk[o.bookmaker] = _Outcome(oid, label, k, o.bookmaker,
                                               o.url)

    arbs: list[Arb] = []
    for (kind, teams, cluster, market_group), g in groups.items():
        outs = g["outcomes"]
        if len(outs) != 2 or len(g["books"]) < 2:
            continue  # нужен ровно двухисходный рынок и минимум 2 БК
        side_a, side_b = outs.values()
        s = g["sample"]
        warned = False
        # ВСЕ комбинации «исход 1 у БК X × исход 2 у БК Y» (X ≠ Y):
        # показываем каждую вилку, а не только пару с максимальными кэфами
        for o1 in side_a.values():
            for o2 in side_b.values():
                if o1.bookmaker == o2.bookmaker:
                    continue  # обе стороны из одной БК — не вилка (маржа)
                if same_market_maker(o1.bookmaker, o2.bookmaker):
                    continue  # линию обеим считает одна платформа
                margin = 1 / o1.odds + 1 / o2.odds
                if margin >= 1:
                    continue
                profit_pct = (1 / margin - 1) * 100
                # Кандидат может быть не вилкой, а ошибкой сопоставления
                # (аномальная доходность) или ставкой на два РАЗНЫХ по
                # правилам расчёта рынка (счёт раундов в единоборствах).
                # В основной список такое не показываем: ставка по нему
                # приведёт к потере денег.
                reason = _reject_reason(s, o1.bookmaker, o2.bookmaker,
                                        profit_pct, max_profit)
                if reason and not warned:
                    log.info("Отсеяна вилка %.1f%% (%s — %s, %s: %s@%s / "
                             "%s@%s): %s", profit_pct, s.team1, s.team2,
                             s.market, o1.odds, o1.bookmaker, o2.odds,
                             o2.bookmaker, reason[1])
                    warned = True
                if reason and not _has_room(rejected):
                    continue
                # порядок исходов: для победителя выравниваем к
                # team1/team2 образца
                first, second = _order(s, o1, o2, name_map)
                if s.market_key.startswith(("total", "hcap", "bothscore",
                                            "itotal", "oddeven")):
                    # метки берём у образца (он ориентирован team1→team2),
                    # а не у БК с лучшим кэфом — иначе фора team2
                    # подписалась бы как «Ф1», если у той БК эта команда
                    # идёт первой
                    label1, label2 = s.outcome1, s.outcome2
                else:
                    label1, label2 = "П1", "П2"
                arb = Arb(
                    # пара БК — часть ключа: у одного рынка может быть
                    # несколько вилок одновременно (разные пары БК)
                    match_key=(f"{kind}|{'|'.join(sorted(teams))}|{cluster}|"
                               f"{market_group}|"
                               f"{first.bookmaker}|{second.bookmaker}"),
                    sport=s.sport, team1=s.team1, team2=s.team2,
                    market=display_market(s.market_key, s.market),
                    outcome1=label1, outcome2=label2,
                    k1_max=round(first.odds, 3), k1_bookmaker=first.bookmaker,
                    k2_max=round(second.odds, 3), k2_bookmaker=second.bookmaker,
                    k1_url=first.url, k2_url=second.url,
                    margin=margin, profit_pct=profit_pct,
                    kind=kind, start_time=s.start_time, start_ts=s.start_ts,
                    stakes={str(b): calc_stakes(first.odds, second.odds, b)
                            for b in BANKS},
                )
                if reason:
                    arb.reject_code, arb.reject_reason = reason
                    rejected.append(arb)
                    continue
                arbs.append(arb)

    arbs.sort(key=lambda a: a.profit_pct, reverse=True)
    return arbs


def find_arbs_1x2(odds: Iterable[MarketOdds],
                  name_map: dict[str, str] | None = None,
                  max_profit: float | None = None,
                  rejected: list[Arb3] | None = None,
                  time_clusters: dict[tuple, dict] | None = None
                  ) -> list[Arb3]:
    """Ищет ТРЁХисходные вилки на рынке «Исход 1X2» (П1/X/П2).

    Отдельный движок от find_arbs: рынок с тремя взаимоисключающими
    исходами требует перебора троек кэфов (а не пар), поэтому логика
    группировки/сопоставления событий дублирует find_arbs, но подбор
    комбинаций и формула маржи — трёхсторонние.

    max_profit — потолок доходности, rejected — список отсеянных
    кандидатов, time_clusters — готовые кластеры времени старта; всё
    работает как в find_arbs.
    """
    if max_profit is None:
        max_profit = ARB_MAX_PROFIT
    odds = list(odds)
    if name_map is None:
        name_map = build_name_canon_map(odds)
    if time_clusters is None:
        time_clusters = _time_clusters(odds, name_map)

    groups: dict[tuple, dict] = defaultdict(lambda: {
        "outcomes": {}, "sample": None, "books": set(),
    })

    for o in odds:
        if not o.market_key.startswith("winner1x2"):
            continue
        if not (o.k1 and o.k2 and o.k3
                and o.k1 > 1 and o.k2 > 1 and o.k3 > 1):
            continue
        if not _named(o.team1, o.team2):
            continue
        teams = frozenset((_canon(name_map, norm_team(o.team1)),
                           _canon(name_map, norm_team(o.team2))))
        if len(teams) < 2:
            continue
        cluster = 0
        if o.kind == KIND_PREMATCH and o.start_ts:
            cluster = time_clusters.get((o.kind, teams), {}).get(o.start_ts, 0)
        key = (o.kind, teams, cluster, canon_market_key(o.market_key))
        g = groups[key]
        g["sample"] = g["sample"] or o
        g["books"].add(o.bookmaker)
        t1_id = f"team:{_canon(name_map, norm_team(o.team1))}"
        t2_id = f"team:{_canon(name_map, norm_team(o.team2))}"
        for oid, label, k in ((t1_id, o.outcome1, o.k1),
                              ("draw", o.outcome3 or "X", o.k3),
                              (t2_id, o.outcome2, o.k2)):
            per_bk = g["outcomes"].setdefault(oid, {})
            cur = per_bk.get(o.bookmaker)
            if cur is None or k > cur.odds:
                per_bk[o.bookmaker] = _Outcome(oid, label, k, o.bookmaker,
                                               o.url)

    arbs: list[Arb3] = []
    for (kind, teams, cluster, market_group), g in groups.items():
        outs = g["outcomes"]
        if len(outs) != 3 or len(g["books"]) < 2:
            continue  # нужны все 3 исхода и минимум 2 БК
        s = g["sample"]
        want_t1 = f"team:{_canon(name_map, norm_team(s.team1))}"
        want_t2 = f"team:{_canon(name_map, norm_team(s.team2))}"
        if want_t1 not in outs or want_t2 not in outs or "draw" not in outs:
            continue
        legs = [outs[want_t1], outs["draw"], outs[want_t2]]
        warned = False
        # ВСЕ комбинации троек БК (не только максимальные кэфы) — как и
        # в двухисходном движке, показываем каждую валидную вилку
        for c1, cx, c2 in itertools.product(
                *(list(d.values()) for d in legs)):
            books = {c1.bookmaker, cx.bookmaker, c2.bookmaker}
            if len(books) < 2:
                continue  # все три ставки у одной БК — это её же маржа
            if all(same_market_maker(x, y)
                   for x, y in itertools.combinations(books, 2)):
                continue  # линию всем трём плечам считает одна платформа
            margin = 1 / c1.odds + 1 / cx.odds + 1 / c2.odds
            if margin >= 1:
                continue
            profit_pct = (1 / margin - 1) * 100
            reason = _reject_reason(s, c1.bookmaker, c2.bookmaker,
                                    profit_pct, max_profit)
            if reason and not warned:
                log.info("Отсеяна 1X2-вилка %.1f%% (%s — %s): %s",
                         profit_pct, s.team1, s.team2, reason[1])
                warned = True
            if reason and not _has_room(rejected):
                continue
            arb = Arb3(
                match_key=(f"{kind}|{'|'.join(sorted(teams))}|{cluster}|"
                           f"{market_group}|"
                           f"{c1.bookmaker}|{cx.bookmaker}|{c2.bookmaker}"),
                sport=s.sport, team1=s.team1, team2=s.team2,
                market=display_market(s.market_key, s.market),
                outcome1="П1", outcomex="X", outcome2="П2",
                k1_max=round(c1.odds, 3), k1_bookmaker=c1.bookmaker,
                k1_url=c1.url,
                kx_max=round(cx.odds, 3), kx_bookmaker=cx.bookmaker,
                kx_url=cx.url,
                k2_max=round(c2.odds, 3), k2_bookmaker=c2.bookmaker,
                k2_url=c2.url,
                margin=margin, profit_pct=profit_pct,
                kind=kind, start_time=s.start_time, start_ts=s.start_ts,
                stakes={str(b): calc_stakes3(c1.odds, cx.odds, c2.odds, b)
                        for b in BANKS},
            )
            if reason:
                arb.reject_code, arb.reject_reason = reason
                rejected.append(arb)
                continue
            arbs.append(arb)

    arbs.sort(key=lambda a: a.profit_pct, reverse=True)
    return arbs


def _order(sample: MarketOdds, o1: _Outcome, o2: _Outcome,
          name_map: dict[str, str] | None = None):
    """Ставит первым исход, соответствующий team1 образца (или ТБ),
    чтобы столбцы в таблице совпадали с отображаемым матчем."""
    if sample.market_key.startswith("itotal"):
        first = o1 if o1.oid.startswith("itover:") else o2
    elif sample.market_key.startswith("total"):
        first = o1 if o1.oid.startswith("over:") else o2
    elif sample.market_key.startswith("bothscore"):
        first = o1 if o1.oid == "bts:yes" else o2
    elif sample.market_key.startswith("oddeven"):
        first = o1 if o1.oid == "oe:even" else o2
    elif sample.market_key.startswith("hcap"):
        want = f"hcap:{_canon(name_map, norm_team(sample.team1))}:"
        first = o1 if o1.oid.startswith(want) else o2
    else:
        want = f"team:{_canon(name_map, norm_team(sample.team1))}"
        first = o1 if o1.oid == want else o2
    second = o2 if first is o1 else o1
    return first, second
