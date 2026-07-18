"""Поиск двухисходных вилок по формуле 1/К1_max + 1/К2_max < 1.

Ключевая сложность вилок — сопоставить одно и то же событие у разных БК:
порядок команд может отличаться (Астана — Динамо / Динамо — Астана), а имена
записаны по-разному (Елимай ФК / Елимай). Поэтому:

- событие идентифицируется НЕупорядоченной парой нормализованных команд;
- исход привязан не к позиции (1/2), а к конкретной команде (для победителя)
  или к стороне тотала (over/under для линии pt);
- лучшие кэфы берутся по каждому исходу среди РАЗНЫХ БК.
"""
import logging
import re
from collections import defaultdict
from typing import Iterable

from .config import ARB_MAX_PROFIT, BANKS, START_TS_TOLERANCE
from .models import Arb, KIND_PREMATCH, MarketOdds
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


class _Outcome:
    """Лучший кэф по одному исходу среди всех БК."""

    __slots__ = ("oid", "label", "odds", "bookmaker")

    def __init__(self, oid: str, label: str, odds: float, bookmaker: str) -> None:
        self.oid, self.label, self.odds = oid, label, odds
        self.bookmaker = bookmaker


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
    """
    if not o.market_key.startswith("hcap"):
        return o.market_key
    prefix, h1 = o.market_key.rsplit(":", 1)  # prefix = hcap:<scope>
    a, b = norm_team(o.team1), norm_team(o.team2)
    anchor = h1 if a <= b else _neg_hcap(h1)
    return f"{prefix}:{anchor}"


def _time_clusters(odds: list[MarketOdds]) -> dict[tuple, dict]:
    """Кластеры времени старта по каждому событию (kind, пара команд).

    Одна и та же пара команд может играть НЕСКОЛЬКО матчей (первый и
    ответный, мужской и женский в один день, разные лиги). Если у двух БК
    время старта различается больше допуска — это разные матчи, их кэфы
    нельзя сшивать в одну вилку. Часовые пояса БК уже приведены к общему
    unix-времени, поэтому допуск маленький (START_TS_TOLERANCE).
    """
    ts_by_event: dict[tuple, set] = defaultdict(set)
    for o in odds:
        if o.kind == KIND_PREMATCH and o.start_ts:
            teams = frozenset((norm_team(o.team1), norm_team(o.team2)))
            ts_by_event[(o.kind, teams)].add(o.start_ts)
    clusters: dict[tuple, dict] = {}
    for key, ts_set in ts_by_event.items():
        mapping: dict[float, int] = {}
        cluster, prev = 0, None
        for ts in sorted(ts_set):
            if prev is not None and ts - prev > START_TS_TOLERANCE:
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
            best = g["outcomes"].get(oid)
            if best is None or k > best.odds:
                g["outcomes"][oid] = _Outcome(oid, label, k, o.bookmaker)

    arbs: list[Arb] = []
    for (kind, teams, cluster, market_group), g in groups.items():
        outs = g["outcomes"]
        if len(outs) != 2 or len(g["books"]) < 2:
            continue  # нужен ровно двухисходный рынок и минимум 2 БК
        o1, o2 = outs.values()
        if o1.bookmaker == o2.bookmaker:
            continue  # обе стороны из одной БК — не вилка (маржа)

        margin = 1 / o1.odds + 1 / o2.odds
        if margin >= 1:
            continue

        s = g["sample"]
        profit_pct = (1 / margin - 1) * 100
        # Аномально высокая «доходность» — почти наверняка не вилка, а
        # ошибка сопоставления (разные рынки/матчи у БК). Не показываем:
        # ставка по ней приведёт к потере денег.
        if profit_pct > ARB_MAX_PROFIT:
            log.info("Отброшена подозрительная вилка %.1f%% (%s — %s, %s: "
                     "%s@%s / %s@%s) — похоже на ошибку сопоставления",
                     profit_pct, s.team1, s.team2, s.market,
                     o1.odds, o1.bookmaker, o2.odds, o2.bookmaker)
            continue
        # порядок исходов: для победителя выравниваем к team1/team2 образца
        first, second = _order(s, o1, o2)
        if s.market_key.startswith(("total", "hcap", "bothscore")):
            # метки берём у образца (он ориентирован team1→team2), а не у
            # БК с лучшим кэфом — иначе фора team2 подписалась бы как «Ф1»,
            # если у той БК эта команда идёт первой
            label1, label2 = s.outcome1, s.outcome2
        else:
            label1, label2 = "П1", "П2"
        arbs.append(Arb(
            match_key=(f"{kind}|{'|'.join(sorted(teams))}|{cluster}|"
                       f"{market_group}"),
            sport=s.sport, team1=s.team1, team2=s.team2,
            market=display_market(s.market_key, s.market),
            outcome1=label1, outcome2=label2,
            k1_max=round(first.odds, 3), k1_bookmaker=first.bookmaker,
            k2_max=round(second.odds, 3), k2_bookmaker=second.bookmaker,
            margin=margin, profit_pct=profit_pct,
            kind=kind, start_time=s.start_time, start_ts=s.start_ts,
            stakes={str(b): calc_stakes(first.odds, second.odds, b) for b in BANKS},
        ))

    arbs.sort(key=lambda a: a.profit_pct, reverse=True)
    return arbs


def _order(sample: MarketOdds, o1: _Outcome, o2: _Outcome):
    """Ставит первым исход, соответствующий team1 образца (или ТБ),
    чтобы столбцы в таблице совпадали с отображаемым матчем."""
    if sample.market_key.startswith("total"):
        first = o1 if o1.oid.startswith("over:") else o2
    elif sample.market_key.startswith("bothscore"):
        first = o1 if o1.oid == "bts:yes" else o2
    elif sample.market_key.startswith("hcap"):
        want = f"hcap:{norm_team(sample.team1)}:"
        first = o1 if o1.oid.startswith(want) else o2
    else:
        want = f"team:{norm_team(sample.team1)}"
        first = o1 if o1.oid == want else o2
    second = o2 if first is o1 else o1
    return first, second
