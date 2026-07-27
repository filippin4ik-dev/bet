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
import itertools
import logging
import re
from collections import defaultdict
from typing import Iterable

from .config import (ARB_MAX_PROFIT, BANKS, START_TS_TOLERANCE,
                     START_TS_TOLERANCE_COMBAT, START_TS_TOLERANCE_RAPID)
from .models import Arb, Arb3, KIND_PREMATCH, MarketOdds
from .parsers.html_utils import display_market, neg_hcap as _neg_hcap

log = logging.getLogger("arbitrage")

# Слова-шумы в названиях команд, мешающие сопоставлению между БК.
# ВАЖНО: маркер женской команды («ж»/«жен») НЕ шум — без него мужской
# и женский матчи одинаковых клубов слились бы в одно событие.
_NOISE = {"фк", "fc", "хк", "hc", "бк", "bc", "clubs", "клуб"}
# Женские маркеры приводим к одному виду: «(ж)», «жен», «women», «w»
_FEMALE = {"ж", "жен", "женщины", "w", "women"}


def norm_team(name: str) -> str:
    name = name.lower().replace("ё", "е")
    name = re.sub(r"[^\w\s]", " ", name)
    words = []
    for w in name.split():
        if not w or w in _NOISE:
            continue
        words.append("ж" if w in _FEMALE else w)
    return " ".join(sorted(words))  # порядок слов тоже не важен


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


def _explode(o: MarketOdds):
    """Разбивает котировку на два канонических исхода (id, метка, кэф).

    id — устойчивый между БК идентификатор исхода:
    - победитель: нормализованное имя команды;
    - тотал: 'over:<pt>' / 'under:<pt>';
    - фора: 'hcap:<команда>:<знаковая линия>' — исход привязан к КОМАНДЕ и
      её линии. Фора team1(-1.5) сшивается с team2(+1.5) другой БК как две
      стороны одного рынка, а team1(+1.5) — уже другой рынок (другой
      фаворит), ложных вилок не будет.
    """
    if o.market_key.startswith("itotal"):
        # itotal:<сторона 1|2>:<scope>:<линия> — тотал ОДНОЙ команды.
        # Исход привязан к нормализованному имени команды: у БК с
        # перевёрнутым порядком команд тот же рынок сшивается корректно.
        _, side, scope, pt = o.market_key.split(":", 3)
        team = norm_team(o.team1 if side == "1" else o.team2)
        return [
            (f"itover:{team}:{scope}:{pt}", o.outcome1, o.k1),
            (f"itunder:{team}:{scope}:{pt}", o.outcome2, o.k2),
        ]
    if o.market_key.startswith("total"):
        pt = o.market_key.split(":", 1)[1] if ":" in o.market_key else ""
        return [
            (f"over:{pt}", o.outcome1, o.k1),
            (f"under:{pt}", o.outcome2, o.k2),
        ]
    if o.market_key.startswith("hcap"):
        # hcap:<scope>:<линия team1>
        h1 = o.market_key.rsplit(":", 1)[1]
        return [
            (f"hcap:{norm_team(o.team1)}:{h1}", o.outcome1, o.k1),
            (f"hcap:{norm_team(o.team2)}:{_neg_hcap(h1)}", o.outcome2, o.k2),
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
        (f"team:{norm_team(o.team1)}", o.outcome1, o.k1),
        (f"team:{norm_team(o.team2)}", o.outcome2, o.k2),
    ]


def _market_group(o: MarketOdds) -> str:
    """Ключ рынка, ОДИНАКОВЫЙ у обеих БК независимо от порядка команд.

    Для победителя/тотала подходит сам market_key. Для форы линия зависит
    от того, какая команда «первая», поэтому привязываем знак к
    алфавитно-первой нормализованной команде — тогда обе стороны рынка
    (и при перевёрнутом порядке команд у другой БК) попадают в одну группу.
    Индивидуальный тотал привязываем к нормализованному ИМЕНИ команды —
    сторона (1/2) у разных БК может быть разной.
    """
    if o.market_key.startswith("itotal"):
        _, side, scope, pt = o.market_key.split(":", 3)
        team = norm_team(o.team1 if side == "1" else o.team2)
        return f"itotal:{team}:{scope}:{pt}"
    if not o.market_key.startswith("hcap"):
        return o.market_key
    prefix, h1 = o.market_key.rsplit(":", 1)  # prefix = hcap:<scope>
    a, b = norm_team(o.team1), norm_team(o.team2)
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


def _start_tolerance(sport: str) -> float:
    """Допуск расхождения времени старта для вида спорта котировки."""
    root = sport.split("·")[0].strip().lower().replace("ё", "е")
    if root in _COMBAT_ROOTS:
        return START_TS_TOLERANCE_COMBAT
    if _RAPID_FIXTURE_RE.search(sport):
        return START_TS_TOLERANCE_RAPID
    return START_TS_TOLERANCE


def _time_clusters(odds: list[MarketOdds]) -> dict[tuple, dict]:
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
    """
    ts_by_event: dict[tuple, set] = defaultdict(set)
    tol_by_event: dict[tuple, float] = {}
    for o in odds:
        if o.kind == KIND_PREMATCH and o.start_ts:
            teams = frozenset((norm_team(o.team1), norm_team(o.team2)))
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


def find_arbs(odds: Iterable[MarketOdds]) -> list[Arb]:
    odds = list(odds)
    time_clusters = _time_clusters(odds)

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
        teams = frozenset((norm_team(o.team1), norm_team(o.team2)))
        if len(teams) < 2:
            continue
        # разные матчи одной пары команд (по времени старта) — разные группы
        cluster = 0
        if o.kind == KIND_PREMATCH and o.start_ts:
            cluster = time_clusters.get((o.kind, teams), {}).get(o.start_ts, 0)
        # вид рынка: winner / winner:<роспись> / total:<pt> / hcap:<линия>.
        # Для форы группа привязана к алфавитно-первой команде (см. _market_group)
        market_group = _market_group(o)
        key = (o.kind, teams, cluster, market_group)
        g = groups[key]
        g["sample"] = g["sample"] or o
        g["books"].add(o.bookmaker)
        for oid, label, k in _explode(o):
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
                first, second = _order(s, o1, o2)
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


def find_arbs_1x2(odds: Iterable[MarketOdds]) -> list[Arb3]:
    """Ищет ТРЁХисходные вилки на рынке «Исход 1X2» (П1/X/П2).

    Отдельный движок от find_arbs: рынок с тремя взаимоисключающими
    исходами требует перебора троек кэфов (а не пар), поэтому логика
    группировки/сопоставления событий дублирует find_arbs, но подбор
    комбинаций и формула маржи — трёхсторонние.
    """
    odds = list(odds)
    time_clusters = _time_clusters(odds)

    groups: dict[tuple, dict] = defaultdict(lambda: {
        "outcomes": {}, "sample": None, "books": set(),
    })

    for o in odds:
        if not o.market_key.startswith("winner1x2"):
            continue
        if not (o.k1 and o.k2 and o.k3
                and o.k1 > 1 and o.k2 > 1 and o.k3 > 1):
            continue
        teams = frozenset((norm_team(o.team1), norm_team(o.team2)))
        if len(teams) < 2:
            continue
        cluster = 0
        if o.kind == KIND_PREMATCH and o.start_ts:
            cluster = time_clusters.get((o.kind, teams), {}).get(o.start_ts, 0)
        key = (o.kind, teams, cluster, o.market_key)
        g = groups[key]
        g["sample"] = g["sample"] or o
        g["books"].add(o.bookmaker)
        t1_id = f"team:{norm_team(o.team1)}"
        t2_id = f"team:{norm_team(o.team2)}"
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
        want_t1 = f"team:{norm_team(s.team1)}"
        want_t2 = f"team:{norm_team(s.team2)}"
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


def _order(sample: MarketOdds, o1: _Outcome, o2: _Outcome):
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
        want = f"hcap:{norm_team(sample.team1)}:"
        first = o1 if o1.oid.startswith(want) else o2
    else:
        want = f"team:{norm_team(sample.team1)}"
        first = o1 if o1.oid == want else o2
    second = o2 if first is o1 else o1
    return first, second
