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

from .config import (ARB_MAX_PROFIT, BANKS, FUZZY_NAME_MIN_SINGLE_RATIO,
                     FUZZY_NAME_SIM_THRESHOLD, START_TS_TOLERANCE,
                     START_TS_TOLERANCE_COMBAT, START_TS_TOLERANCE_RAPID)
from .models import Arb, Arb3, KIND_PREMATCH, MarketOdds
from .parsers.html_utils import (canon_market_key, display_market,
                                 neg_hcap as _neg_hcap)

log = logging.getLogger("arbitrage")

# Слова-шумы в названиях команд, мешающие сопоставлению между БК.
# ВАЖНО: маркер женской команды («ж»/«жен») НЕ шум — без него мужской
# и женский матчи одинаковых клубов слились бы в одно событие.
_NOISE = {"фк", "fc", "хк", "hc", "бк", "bc", "clubs", "клуб"}
# Женские маркеры приводим к одному виду: «(ж)», «жен», «women», «w»
_FEMALE = {"ж", "жен", "женщины", "w", "women"}

# Заглушки вместо имён команд. БК ставит их, когда состав пары ещё не
# объявлен, — и это НЕ имя: у Fonbet таких «событий» под сотню, у Melbet
# ещё больше, и все они сходятся в одну пару «хозяева/гости». Тогда каждый
# такой матч одной БК сопоставляется с каждым таким матчем другой, а это
# разные матчи разных лиг: кэфы у них любые, и «вилки» из этой каши —
# ложные. На живой линии шести БК так набиралось 82 вилки из 137.
_PLACEHOLDER_TEAMS = {
    "хозяева", "хозяин", "гости", "гость",
    "1 команда", "2 команда", "1 команда я", "2 команда я",
    "home", "away", "1 team", "2 team", "team1", "team2",
}


# Имена команд повторяются в сотнях тысяч котировок (одно событие — десятки
# рынков у каждой БК), а нормализация не из дешёвых: на живой линии выходило
# около 4 млн вызовов и почти половина времени пересчёта вилок. Функция
# чистая, поэтому просто кэшируем результат по имени; размер кэша ограничен —
# уникальных имён за сутки десятки тысяч.
@functools.lru_cache(maxsize=200_000)
def norm_team(name: str) -> str:
    name = name.lower().replace("ё", "е")
    name = re.sub(r"[^\w\s]", " ", name)
    words = []
    for w in name.split():
        if not w or w in _NOISE:
            continue
        words.append("ж" if w in _FEMALE else w)
    return " ".join(sorted(words))  # порядок слов тоже не важен


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
        if not o.start_ts:
            continue
        if not _named(o.team1, o.team2):
            continue
        t1, t2 = norm_team(o.team1), norm_team(o.team2)
        root = o.sport.split("·")[0].strip().lower().replace("ё", "е")
        key = (root, frozenset((t1, t2)))
        tol = _start_tolerance(o.sport)
        ev = events.get(key)
        if ev is None:
            events[key] = {"books": {o.bookmaker}, "lo": o.start_ts,
                           "hi": o.start_ts, "tol": tol}
        else:
            ev["books"].add(o.bookmaker)
            ev["lo"] = min(ev["lo"], o.start_ts)
            ev["hi"] = max(ev["hi"], o.start_ts)
            ev["tol"] = max(ev["tol"], tol)

    buckets: dict[str, list[tuple]] = defaultdict(list)
    for (root, teams), ev in events.items():
        pair = tuple(teams)
        buckets[root].append((pair, _name_sig(pair[0]), _name_sig(pair[1]),
                              ev["books"], ev["lo"], ev["hi"], ev["tol"]))

    dsu = _UnionFind()
    for root, items in buckets.items():
        items.sort(key=lambda x: x[4])  # по началу окна старта
        cap = _fuzzy_window_cap(root)
        n = len(items)
        for i in range(n):
            pair_i, sig_i0, sig_i1, books_i, _lo_i, hi_i, tol_i = items[i]
            single_i = len(books_i) == 1
            for j in range(i + 1, n):
                pair_j, sig_j0, sig_j1, books_j, lo_j, _hi_j, tol_j = items[j]
                # нижняя оценка расстояния между временами старта: окна
                # отсортированы по началу, поэтому оценка только растёт
                gap = lo_j - hi_i
                if gap > cap:
                    break
                if single_i and books_i == books_j:
                    continue  # оба написания только у ОДНОЙ БК — разные матчи
                if gap > max(tol_i, tol_j):
                    continue
                straight_ok = (_may_match(sig_i0, sig_j0)
                              and _may_match(sig_i1, sig_j1))
                swapped_ok = (_may_match(sig_i0, sig_j1)
                             and _may_match(sig_i1, sig_j0))
                if not straight_ok and not swapped_ok:
                    continue  # ни в одной ориентации имена не дотянут до порога
                straight = swapped = -1.0
                if straight_ok:
                    r00 = difflib.SequenceMatcher(None, pair_i[0], pair_j[0]).ratio()
                    r11 = difflib.SequenceMatcher(None, pair_i[1], pair_j[1]).ratio()
                    straight = (r00 + r11) / 2
                if swapped_ok:
                    r01 = difflib.SequenceMatcher(None, pair_i[0], pair_j[1]).ratio()
                    r10 = difflib.SequenceMatcher(None, pair_i[1], pair_j[0]).ratio()
                    swapped = (r01 + r10) / 2
                if straight >= swapped:
                    avg, ra, rb = straight, r00, r11
                    match0, match1 = pair_j[0], pair_j[1]
                else:
                    avg, ra, rb = swapped, r01, r10
                    match0, match1 = pair_j[1], pair_j[0]
                if avg < FUZZY_NAME_SIM_THRESHOLD:
                    continue
                if ra < FUZZY_NAME_MIN_SINGLE_RATIO \
                        or rb < FUZZY_NAME_MIN_SINGLE_RATIO:
                    continue
                if pair_i[0] != match0:
                    dsu.union(pair_i[0], match0)
                if pair_i[1] != match1:
                    dsu.union(pair_i[1], match1)

    return {name: dsu.find(name) for name in dsu._parent}


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
_COMBAT_ROOTS = {"единоборства", "смешанные единоборства", "бокс", "mma",
                 "ufc", "кикбоксинг", "муай-тай", "бои без правил"}

# «Быстрые» турниры/лиги, где одна и та же пара соперников играет НЕСКОЛЬКО
# матчей за вечер с интервалом в минуты — виртуальный футбол и похожие
# скоростные форматы обычно прямо указывают длительность матча в названии
# турнира/чемпионата, например «FC 26. ... 2x3 мин.» или «H2H LIGA-3. 2x4
# мин.». Для них НЕЛЬЗЯ расширять START_TS_TOLERANCE (см. ниже) — иначе два
# разных матча той же пары в одну сессию склеятся в одну «вилку».
_RAPID_FIXTURE_RE = re.compile(r"(?i)\d+\s*[xх]\s*\d+\s*мин")


@functools.lru_cache(maxsize=20_000)
def _start_tolerance(sport: str) -> float:
    """Допуск расхождения времени старта для вида спорта котировки.

    Вызывается по каждой котировке (миллионы раз за пересчёт) при считанных
    сотнях разных значений — поэтому с кэшем."""
    root = sport.split("·")[0].strip().lower().replace("ё", "е")
    if root in _COMBAT_ROOTS:
        return START_TS_TOLERANCE_COMBAT
    if _RAPID_FIXTURE_RE.search(sport):
        return START_TS_TOLERANCE_RAPID
    return START_TS_TOLERANCE


def _time_clusters(odds: list[MarketOdds],
                   name_map: dict[str, str] | None = None) -> dict[tuple, dict]:
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

    name_map (build_name_canon_map) сливает разные написания имени одной
    команды между БК ДО группировки по паре команд — иначе такие пары
    вообще не встретились бы в одном ключе события.
    """
    ts_by_event: dict[tuple, set] = defaultdict(set)
    tol_by_event: dict[tuple, float] = {}
    for o in odds:
        if o.kind == KIND_PREMATCH and o.start_ts \
                and _named(o.team1, o.team2):
            teams = frozenset((_canon(name_map, norm_team(o.team1)),
                               _canon(name_map, norm_team(o.team2))))
            key = (o.kind, teams)
            ts_by_event[key].add(o.start_ts)
            tol = _start_tolerance(o.sport)
            if tol > tol_by_event.get(key, 0.0):
                tol_by_event[key] = tol
    clusters: dict[tuple, dict] = {}
    for key, ts_set in ts_by_event.items():
        tol = tol_by_event.get(key, START_TS_TOLERANCE)
        mapping: dict[float, int] = {}
        cluster, prev = 0, None
        for ts in sorted(ts_set):
            if prev is not None and ts - prev > tol:
                cluster += 1
            mapping[ts] = cluster
            prev = ts
        clusters[key] = mapping
    return clusters


def find_arbs(odds: Iterable[MarketOdds],
              name_map: dict[str, str] | None = None) -> list[Arb]:
    odds = list(odds)
    if name_map is None:
        name_map = build_name_canon_map(odds)
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
                margin = 1 / o1.odds + 1 / o2.odds
                if margin >= 1:
                    continue
                profit_pct = (1 / margin - 1) * 100
                # Аномально высокая «доходность» — почти наверняка не
                # вилка, а ошибка сопоставления (разные рынки/матчи у БК).
                # Не показываем: ставка по ней приведёт к потере денег.
                if profit_pct > ARB_MAX_PROFIT:
                    if not warned:
                        log.info(
                            "Отброшена подозрительная вилка %.1f%% "
                            "(%s — %s, %s: %s@%s / %s@%s) — похоже на "
                            "ошибку сопоставления",
                            profit_pct, s.team1, s.team2, s.market,
                            o1.odds, o1.bookmaker, o2.odds, o2.bookmaker)
                        warned = True
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
                arbs.append(Arb(
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
                ))

    arbs.sort(key=lambda a: a.profit_pct, reverse=True)
    return arbs


def find_arbs_1x2(odds: Iterable[MarketOdds],
                  name_map: dict[str, str] | None = None) -> list[Arb3]:
    """Ищет ТРЁХисходные вилки на рынке «Исход 1X2» (П1/X/П2).

    Отдельный движок от find_arbs: рынок с тремя взаимоисключающими
    исходами требует перебора троек кэфов (а не пар), поэтому логика
    группировки/сопоставления событий дублирует find_arbs, но подбор
    комбинаций и формула маржи — трёхсторонние.
    """
    odds = list(odds)
    if name_map is None:
        name_map = build_name_canon_map(odds)
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
        key = (o.kind, teams, cluster, o.market_key)
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
            if len({c1.bookmaker, cx.bookmaker, c2.bookmaker}) < 2:
                continue  # все три ставки у одной БК — это её же маржа
            margin = 1 / c1.odds + 1 / cx.odds + 1 / c2.odds
            if margin >= 1:
                continue
            profit_pct = (1 / margin - 1) * 100
            if profit_pct > ARB_MAX_PROFIT:
                if not warned:
                    log.info(
                        "Отброшена подозрительная 1X2-вилка %.1f%% "
                        "(%s — %s) — похоже на ошибку сопоставления",
                        profit_pct, s.team1, s.team2)
                    warned = True
                continue
            arbs.append(Arb3(
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
            ))

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
