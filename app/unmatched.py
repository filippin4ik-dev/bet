"""Вилки, которые не дошли до списка: событие не сшилось между БК.

Вилка живёт МЕЖДУ БК, а чтобы их кэфы вообще встретились, движок должен
признать два события у разных контор одним матчем. Признаков для этого
два — пара имён команд и время начала, — и оба у разных БК регулярно
расходятся:

- «Сувон» и «Сувон Самсунг», «Аль-Иттихад Джидда» и «Аль-Иттихад»,
  «Кимчхон Санму» и «Кимчхон Сангму» — одна команда, разные написания.
  Часть таких пар движок сливает сам (build_name_canon_map), но только
  при высокой похожести: слить два РАЗНЫХ клуба с близкими названиями
  хуже, чем не слить один — в первом случае в «вилку» сведутся цены
  разных матчей, и по ней поставят деньги;
- время начала одного матча у БК иногда отличается на часы, и тогда
  допуск (_time_clusters) разводит событие на два.

И то и другое было молчаливым: кандидат не появлялся нигде, вилка по нему
просто не находилась, и понять, что она была, было неоткуда. Здесь такие
пары проверяются: движок пробует склеить их ПРИНУДИТЕЛЬНО и смотрит,
появляется ли вилка. Если появляется — строка уходит на вкладку
«Отсеянные» с обоими написаниями (или с обоими временами), чтобы человек
открыл обе БК и решил сам, один это матч или нет. Автоматически такие
пары не склеиваются: ставка по ошибочной склейке — это ставка на два
разных матча.

Кандидаты ищутся по ОБЩИМ СЛОВАМ в названиях, а не перебором всех пар
подряд: у разных написаний одной команды почти всегда есть общее слово
(«сувон», «иттихад», «кимчхон»), а перебор всего со всем на линии в сотни
тысяч котировок стоил бы дороже самого пересчёта вилок. Своим порогом
похожести (FUZZY_NAME_NEAR_MISS_MIN) отсекается остальное: однофамильцы и
просто разные команды одной лиги.
"""
import difflib
import logging
from collections import defaultdict

from .arbitrage import (REJECT_UNMATCHED_NAME, REJECT_UNMATCHED_TIME,
                        TimeSplit, _canon, _named, _start_tolerance,
                        find_arbs, find_arbs_1x2, norm_team, sport_root)
from .config import (ARB_UNMATCHED_CANDIDATES_MAX, ARB_UNMATCHED_MAX_PAIRS,
                     FUZZY_NAME_NEAR_MISS_MIN)
from .models import KIND_PREMATCH, Arb, Arb3, MarketOdds
from .parsers.html_utils import format_start

log = logging.getLogger("unmatched")

# Сколько строк максимум брать от ОДНОЙ пары событий. Несшитая пара даёт
# вилку обычно не на одном рынке, а на десятке сразу (соседние линии
# тоталов, форы, победитель) — все они об одном и том же, и проверять их
# человеку придётся одним действием: открыть обе БК и сверить, тот ли это
# матч. Поэтому от пары берём самые доходные.
_MAX_PER_PAIR = 3

# Слово короче этого в общие не годится: «фк», «ак», «сан» встречаются у
# десятков разных команд и общими словами не являются — по ним кандидатом
# станет пол-лиги.
_MIN_TOKEN_LEN = 4

# Слово, встречающееся в стольких названиях вида спорта, для поиска
# бесполезно («юнайтед», «динамо», «сити»): пары по нему — это перебор
# всего со всем, ради которого поиск по словам и затевался.
_MAX_TOKEN_EVENTS = 40

# Похожесть, с которой два написания считаются заведомо одной командой
# (см. _same_team). Отличие на пару букв — опечатка или транслитерация;
# «москва» против «киев» столько не наберёт.
_SAME_TEAM_RATIO = 0.8


class _Event:
    """Одно событие в понимании движка: пара имён + чьи это котировки."""

    __slots__ = ("pair", "title", "books", "lo", "hi", "tol", "odds")

    def __init__(self, pair: tuple[str, str]) -> None:
        self.pair = pair
        # как пару написала сама БК: сравнение идёт по нормализованным
        # именам («самсунг сувон»), а человеку показывать надо исходные
        self.title = ""
        self.books: set[str] = set()
        self.lo: float | None = None
        self.hi: float | None = None
        self.tol = 0.0
        self.odds: list[MarketOdds] = []

    def add(self, o: MarketOdds) -> None:
        self.title = self.title or f"{o.team1} — {o.team2}"
        self.books.add(o.bookmaker)
        self.odds.append(o)
        self.tol = max(self.tol, _start_tolerance(o.sport))
        if o.start_ts:
            self.lo = o.start_ts if self.lo is None else min(self.lo,
                                                             o.start_ts)
            self.hi = o.start_ts if self.hi is None else max(self.hi,
                                                             o.start_ts)


class NameNearMiss:
    """Два события с похожими, но не совпавшими названиями."""

    __slots__ = ("a", "b", "sim", "aligned")

    def __init__(self, a: _Event, b: _Event, sim: float,
                 aligned: tuple[str, str]) -> None:
        self.a, self.b, self.sim = a, b, sim
        # имена b в порядке имён a: у БК порядок команд бывает разный
        self.aligned = aligned


def find_unmatched_arbs(odds: list[MarketOdds], name_map: dict[str, str],
                        splits: list[TimeSplit],
                        max_pairs: int = ARB_UNMATCHED_MAX_PAIRS,
                        ) -> tuple[list[Arb], list[Arb3]]:
    """Вилки, которые появились бы, склейся эти события в одно.

    splits — расхождения по времени старта, собранные основным пересчётом
    (_time_clusters); расхождения в названиях ищутся здесь. Возвращает
    двух- и трёхисходные вилки с проставленной причиной отсева.
    """
    events = _collect_events(odds)
    arbs: list[Arb] = []
    arbs3: list[Arb3] = []

    for cand in _name_candidates(events, name_map)[:max_pairs]:
        # принудительная склейка: имена второго события подменяются
        # именами первого. Карта нужна маленькая — в подмножестве только
        # эти два события
        forced = {cand.a.pair[0]: cand.a.pair[0],
                  cand.a.pair[1]: cand.a.pair[1],
                  cand.aligned[0]: cand.a.pair[0],
                  cand.aligned[1]: cand.a.pair[1]}
        _collect(arbs, arbs3, cand.a.odds + cand.b.odds, forced,
                 cand.a.books, cand.b.books, REJECT_UNMATCHED_NAME,
                 _name_reason(cand))

    by_canon = _by_canon(events, name_map, splits)
    for split in sorted(splits, key=lambda s: s.gap)[:max_pairs]:
        side_a, side_b = _split_sides(by_canon.get(split.teams, ()), split)
        if not side_a or not side_b:
            continue
        _collect(arbs, arbs3, side_a + side_b, name_map, split.books_a,
                 split.books_b, REJECT_UNMATCHED_TIME, _time_reason(split))

    arbs.sort(key=lambda a: a.profit_pct, reverse=True)
    arbs3.sort(key=lambda a: a.profit_pct, reverse=True)
    return arbs, arbs3


def _collect_events(odds: list[MarketOdds]) -> dict[tuple, _Event]:
    """Котировки, разложенные по событиям: (вид спорта, пара имён).

    Имена берутся СЫРЫЕ нормализованные, без канона: канон как раз и
    склеил бы то, что мы ищем, — расхождение в написании.
    """
    events: dict[tuple, _Event] = {}
    for o in odds:
        if not _named(o.team1, o.team2):
            continue
        pair = (norm_team(o.team1), norm_team(o.team2))
        key = (sport_root(o.sport), frozenset(pair))
        ev = events.get(key)
        if ev is None:
            ev = events[key] = _Event(pair)
        ev.add(o)
    return events


def _name_candidates(events: dict[tuple, _Event],
                     name_map: dict[str, str]) -> list[NameNearMiss]:
    """Пары событий с похожими названиями, которые движок не склеил.

    Порядок — от самых похожих: если бюджет проверок кончится, оборвётся
    он на наименее правдоподобных парах.
    """
    by_root: dict[str, list[_Event]] = defaultdict(list)
    for (root, _pair), ev in events.items():
        by_root[root].append(ev)

    found: list[NameNearMiss] = []
    for root, items in by_root.items():
        _root_candidates(items, name_map, found)
        if len(found) >= ARB_UNMATCHED_CANDIDATES_MAX:
            break
    found.sort(key=lambda c: -c.sim)
    return found


def _root_candidates(items: list[_Event], name_map: dict[str, str],
                     found: list[NameNearMiss]) -> None:
    """Кандидаты внутри одного вида спорта — по общим словам названий."""
    postings: dict[str, set[int]] = defaultdict(set)
    for i, ev in enumerate(items):
        for name in ev.pair:
            for token in _tokens(name):
                postings[token].add(i)

    seen: set[tuple[int, int]] = set()
    for i, ev in enumerate(items):
        near0 = _sharing(postings, ev.pair[0])
        near1 = _sharing(postings, ev.pair[1])
        # обе команды события должны иметь общее слово с кандидатом —
        # совпадения по одной команде («Динамо Москва — Спартак» и
        # «Динамо Киев — Шахтёр») в кандидаты не годятся
        for j in near0 & near1:
            if j == i:
                continue
            pair_ij = (i, j) if i < j else (j, i)
            if pair_ij in seen:
                continue
            seen.add(pair_ij)
            cand = _near_miss(ev, items[j], name_map)
            if cand is not None:
                found.append(cand)
                if len(found) >= ARB_UNMATCHED_CANDIDATES_MAX:
                    return


def _near_miss(a: _Event, b: _Event,
               name_map: dict[str, str]) -> NameNearMiss | None:
    """Пара событий, если это кандидат на несостоявшуюся склейку."""
    if not a.books.isdisjoint(b.books):
        # одна БК по обе стороны — значит она сама выставила два матча
        # этой пары, и это РАЗНЫЕ матчи, а не расхождение в написании
        return None
    if _canon_pair(a, name_map) == _canon_pair(b, name_map):
        return None            # движок их и так уже склеил
    if not _time_fits(a, b):
        return None
    # порядок команд у БК бывает разный, поэтому считаем обе ориентации:
    # лучшая даёт и похожесть, и то, какое имя какому соответствует
    straight = (_ratio(a.pair[0], b.pair[0])
                + _ratio(a.pair[1], b.pair[1])) / 2
    swapped = (_ratio(a.pair[0], b.pair[1])
               + _ratio(a.pair[1], b.pair[0])) / 2
    sim = max(straight, swapped)
    if sim < FUZZY_NAME_NEAR_MISS_MIN:
        return None
    aligned = b.pair if straight >= swapped else (b.pair[1], b.pair[0])
    if not (_same_team(a.pair[0], aligned[0])
            or _same_team(a.pair[1], aligned[1])):
        # Обе команды разошлись «по-настоящему» — это, скорее всего, две
        # разные пары одной лиги, а не одно событие. «Динамо Москва —
        # Спартак Москва» и «Динамо Киев — Спартак Варна» похожи на 75 %,
        # общее слово есть у обеих сторон, но матча тут два. Одного
        # надёжного совпадения хватает: у настоящего расхождения в
        # написании вторая команда почти всегда записана одинаково.
        return None
    return NameNearMiss(a, b, sim, aligned)


def _same_team(a: str, b: str) -> bool:
    """Два написания — заведомо одна команда, а не просто похожие слова.

    Таких признаков два: одно название целиком входит в другое («Сувон» и
    «Сувон Самсунг», «Аль-Иттихад» и «Аль-Иттихад Джидда») или отличается
    на опечатку/транслитерацию («Кимчхон Сангму» и «Кимчхон Санму»)."""
    wa, wb = set(a.split()), set(b.split())
    return wa <= wb or wb <= wa or _ratio(a, b) >= _SAME_TEAM_RATIO


def _canon_pair(ev: _Event, name_map: dict[str, str]) -> frozenset:
    return frozenset((_canon(name_map, ev.pair[0]),
                      _canon(name_map, ev.pair[1])))


def _time_fits(a: _Event, b: _Event) -> bool:
    """События начинаются достаточно близко, чтобы быть одним матчем.

    Без времени (лайв у части БК его не присылает) проверять нечем —
    считаем, что подходит: имена уже похожи, а лайв идёт сейчас."""
    if a.lo is None or b.lo is None:
        return True
    gap = max(a.lo, b.lo) - min(a.hi, b.hi)
    return gap <= max(a.tol, b.tol)


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _tokens(name: str) -> list[str]:
    return [w for w in name.split() if len(w) >= _MIN_TOKEN_LEN]


def _sharing(postings: dict[str, set[int]], name: str) -> set[int]:
    """События, у которых есть общее слово с этим названием."""
    out: set[int] = set()
    for token in _tokens(name):
        ids = postings.get(token)
        if ids and len(ids) <= _MAX_TOKEN_EVENTS:
            out |= ids
    return out


def _by_canon(events: dict[tuple, _Event], name_map: dict[str, str],
              splits: list[TimeSplit]) -> dict[frozenset, list[MarketOdds]]:
    """Котировки по каноничной паре имён — для расхождений по времени.

    Только для пар из splits: их считанные десятки, а событий тысячи."""
    want = {s.teams for s in splits}
    out: dict[frozenset, list[MarketOdds]] = defaultdict(list)
    if not want:
        return out
    for ev in events.values():
        canon = _canon_pair(ev, name_map)
        if canon in want:
            out[canon].extend(ev.odds)
    return out


def _split_sides(odds, split: TimeSplit
                 ) -> tuple[list[MarketOdds], list[MarketOdds]]:
    """Котировки события по обе стороны разрыва во времени.

    БК фильтруем вместе со временем: у пары бывает и больше двух матчей
    (турнир, групповой этап), а сравнивать нужно ровно те два кластера,
    между которыми прошёл разрыв."""
    side_a, side_b = [], []
    for o in odds:
        if o.kind != KIND_PREMATCH or not o.start_ts:
            continue
        if o.bookmaker in split.books_a and o.start_ts <= split.ts_a:
            side_a.append(o)
        elif o.bookmaker in split.books_b and o.start_ts >= split.ts_b:
            side_b.append(o)
    return side_a, side_b


def _collect(arbs: list[Arb], arbs3: list[Arb3], subset: list[MarketOdds],
             name_map: dict[str, str], books_a: set[str], books_b: set[str],
             code: str, reason: str) -> None:
    """Ищет вилки в склеенной паре событий и помечает их причиной отсева.

    Кластеры времени тут заведомо пустые: события уже признаны одним
    матчем (в этом и состоит проверка), и разбивать их обратно по времени
    незачем — иначе пара, которую как раз развело время, всегда давала бы
    пусто.

    Потолок доходности работает обычный (ARB_MAX_PROFIT): «вилка» на 40 %
    из склейки двух похожих названий почти наверняка означает, что это
    всё-таки РАЗНЫЕ матчи, и звать на неё человека незачем.
    """
    found = [a for a in find_arbs(subset, name_map, time_clusters={})
             if _spans(books_a, books_b, a.k1_bookmaker, a.k2_bookmaker)]
    found3 = [a for a in find_arbs_1x2(subset, name_map, time_clusters={})
              if _spans(books_a, books_b, a.k1_bookmaker, a.kx_bookmaker,
                        a.k2_bookmaker)]
    for arb in found[:_MAX_PER_PAIR]:
        arb.reject_code, arb.reject_reason = code, reason
        arbs.append(arb)
    for arb in found3[:_MAX_PER_PAIR]:
        arb.reject_code, arb.reject_reason = code, reason
        arbs3.append(arb)


def _spans(books_a: set[str], books_b: set[str], *bookmakers: str) -> bool:
    """Вилка стоит на ОБОИХ событиях, а не внутри одного из них.

    Вилки внутри одного события движок и так нашёл; интересна только та,
    что появилась ИМЕННО от склейки."""
    return any(b in books_a for b in bookmakers) \
        and any(b in books_b for b in bookmakers)


def _name_reason(cand: NameNearMiss) -> str:
    return (f"события не склеились по названиям: "
            f"«{cand.a.title}» ({_bks(cand.a.books)}) и "
            f"«{cand.b.title}» ({_bks(cand.b.books)}) похожи на "
            f"{cand.sim * 100:.0f} % — для автоматической склейки этого "
            f"мало. Один и тот же матч? Тогда вилка настоящая. Разные "
            f"команды — ставить нельзя")


def _time_reason(split: TimeSplit) -> str:
    return (f"события не склеились по времени начала: "
            f"{_bks(split.books_a)} — {format_start(split.ts_a)}, "
            f"{_bks(split.books_b)} — {format_start(split.ts_b)} "
            f"(расхождение {_span(split.gap)} при допуске "
            f"{_span(split.tol)}). Обычно так расходятся первый и ответный "
            f"матчи одной пары, но бывает и неверное время у одной БК")


def _bks(books: set[str]) -> str:
    return ", ".join(sorted(books))


def _span(seconds: float) -> str:
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} мин"
    return f"{minutes / 60:.1f} ч"
