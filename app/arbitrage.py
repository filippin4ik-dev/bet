"""Поиск двухисходных вилок по формуле 1/К1_max + 1/К2_max < 1.

Ключевая сложность вилок — сопоставить одно и то же событие у разных БК:
порядок команд может отличаться (Астана — Динамо / Динамо — Астана), а имена
записаны по-разному (Елимай ФК / Елимай). Поэтому:

- событие идентифицируется НЕупорядоченной парой нормализованных команд;
- исход привязан не к позиции (1/2), а к конкретной команде (для победителя)
  или к стороне тотала (over/under для линии pt);
- лучшие кэфы берутся по каждому исходу среди РАЗНЫХ БК.
"""
import re
from collections import defaultdict
from typing import Iterable

from .config import BANKS
from .models import Arb, MarketOdds

# Слова-шумы в названиях команд, мешающие сопоставлению между БК
_NOISE = {"фк", "fc", "хк", "hc", "бк", "bc", "жен", "муж", "ж", "clubs", "клуб"}


def norm_team(name: str) -> str:
    name = name.lower().replace("ё", "е")
    name = re.sub(r"[^\w\s]", " ", name)
    words = [w for w in name.split() if w and w not in _NOISE]
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
    - тотал: 'over:<pt>' / 'under:<pt>'.
    """
    if o.market_key.startswith("total"):
        pt = o.market_key.split(":", 1)[1] if ":" in o.market_key else ""
        return [
            (f"over:{pt}", o.outcome1, o.k1),
            (f"under:{pt}", o.outcome2, o.k2),
        ]
    # победитель (в т.ч. дочерние росписи вроде winner:2сет)
    return [
        (f"team:{norm_team(o.team1)}", o.outcome1, o.k1),
        (f"team:{norm_team(o.team2)}", o.outcome2, o.k2),
    ]


def find_arbs(odds: Iterable[MarketOdds]) -> list[Arb]:
    # market_group одинаков у одного рынка одного события независимо от БК
    # и порядка команд: kind | пара_команд | вид_рынка
    groups: dict[tuple, dict] = defaultdict(lambda: {
        "outcomes": {}, "sample": None, "books": set(),
    })

    for o in odds:
        if not (o.k1 and o.k2 and o.k1 > 1 and o.k2 > 1):
            continue
        teams = frozenset((norm_team(o.team1), norm_team(o.team2)))
        if len(teams) < 2:
            continue
        # вид рынка: winner / winner:<роспись> / total:<pt>
        if o.market_key.startswith("total"):
            market_group = o.market_key  # включает линию pt
        else:
            market_group = o.market_key  # winner или winner:<label>
        key = (o.kind, teams, market_group)
        g = groups[key]
        g["sample"] = g["sample"] or o
        g["books"].add(o.bookmaker)
        for oid, label, k in _explode(o):
            best = g["outcomes"].get(oid)
            if best is None or k > best.odds:
                g["outcomes"][oid] = _Outcome(oid, label, k, o.bookmaker)

    arbs: list[Arb] = []
    for (kind, teams, market_group), g in groups.items():
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
        # порядок исходов: для победителя выравниваем к team1/team2 образца
        first, second = _order(s, o1, o2)
        if s.market_key.startswith("total"):
            label1, label2 = first.label, second.label   # ТБ pt / ТМ pt
        else:
            label1, label2 = "П1", "П2"
        arbs.append(Arb(
            match_key=f"{kind}|{'|'.join(sorted(teams))}|{market_group}",
            sport=s.sport, team1=s.team1, team2=s.team2,
            market=s.market,
            outcome1=label1, outcome2=label2,
            k1_max=round(first.odds, 3), k1_bookmaker=first.bookmaker,
            k2_max=round(second.odds, 3), k2_bookmaker=second.bookmaker,
            margin=margin, profit_pct=profit_pct,
            kind=kind, start_time=s.start_time,
            stakes={str(b): calc_stakes(first.odds, second.odds, b) for b in BANKS},
        ))

    arbs.sort(key=lambda a: a.profit_pct, reverse=True)
    return arbs


def _order(sample: MarketOdds, o1: _Outcome, o2: _Outcome):
    """Ставит первым исход, соответствующий team1 образца (или ТБ),
    чтобы столбцы в таблице совпадали с отображаемым матчем."""
    if sample.market_key.startswith("total"):
        first = o1 if o1.oid.startswith("over:") else o2
    else:
        want = f"team:{norm_team(sample.team1)}"
        first = o1 if o1.oid == want else o2
    second = o2 if first is o1 else o1
    return first, second
