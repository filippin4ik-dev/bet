"""Поиск двухисходных вилок по формуле 1/К1_max + 1/К2_max < 1.

Ключевая сложность вилок — сопоставить одно и то же событие у разных БК:
порядок команд может отличаться (Астана — Динамо / Динамо — Астана), а имена
записаны по-разному (Елимай ФК / Елимай). Поэтому:

- событие идентифицируется НЕупорядоченной парой нормализованных команд
  И временем начала (в Лиге Про одни и те же соперники играют по несколько
  раз в день — без времени кэфы РАЗНЫХ матчей склеивались бы в ложную вилку);
- исход привязан не к позиции (1/2), а к конкретной команде (для победителя)
  или к стороне тотала (over/under для линии pt);
- лучшие кэфы берутся по каждому исходу среди РАЗНЫХ БК;
- рынок сопоставляется только с ТЕМ ЖЕ рынком (winner c winner той же
  росписи, тотал — с тоталом той же линии): П1 никогда не пересекается
  с тоталом, у них разные market_key.
"""
import logging
import re
from collections import defaultdict
from typing import Iterable

from .config import ARB_MAX_PROFIT_PCT, BANKS, START_TS_TOLERANCE
from .models import Arb, MarketOdds

log = logging.getLogger("arbitrage")

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


def _split_by_start(olist: list[MarketOdds]) -> list[list[MarketOdds]]:
    """Разбивает котировки одного рынка на кластеры по времени старта.

    Часы БК на один и тот же матч совпадают (перестраховка — допуск
    START_TS_TOLERANCE), а матчи одних соперников в разное время (Лига Про
    играет по несколько раз в день) — это РАЗНЫЕ события: их кэфы нельзя
    склеивать в одну вилку. Котировки без распознанного времени старта
    в кластеры с временем не подмешиваются.
    """
    timed = sorted((o for o in olist if o.start_ts is not None),
                   key=lambda o: o.start_ts)
    clusters: list[list[MarketOdds]] = []
    for o in timed:
        if clusters and o.start_ts - clusters[-1][0].start_ts <= START_TS_TOLERANCE:
            clusters[-1].append(o)
        else:
            clusters.append([o])
    untimed = [o for o in olist if o.start_ts is None]
    if untimed:
        clusters.append(untimed)
    return clusters


def find_arbs(odds: Iterable[MarketOdds]) -> list[Arb]:
    # Группа = одно событие + один рынок, независимо от БК и порядка команд:
    # kind | пара_команд | market_key, внутри — кластер по времени старта.
    # market_key гарантирует «одинаковость» рынка: winner сравнивается
    # только с winner (той же росписи), total — только с total той же линии;
    # П1 никогда не попадёт в пару к тоталу.
    pre: dict[tuple, list[MarketOdds]] = defaultdict(list)
    for o in odds:
        if not (o.k1 and o.k2 and o.k1 > 1 and o.k2 > 1):
            continue
        teams = frozenset((norm_team(o.team1), norm_team(o.team2)))
        if len(teams) < 2:
            continue
        pre[(o.kind, teams, o.market_key)].append(o)

    groups: list[tuple[tuple, dict]] = []
    for (kind, teams, market_group), olist in pre.items():
        for cluster in _split_by_start(olist):
            g = {"outcomes": {}, "sample": cluster[0], "books": set()}
            for o in cluster:
                g["books"].add(o.bookmaker)
                for oid, label, k in _explode(o):
                    best = g["outcomes"].get(oid)
                    if best is None or k > best.odds:
                        g["outcomes"][oid] = _Outcome(oid, label, k,
                                                      o.bookmaker)
            groups.append(((kind, teams, market_group), g))

    arbs: list[Arb] = []
    for (kind, teams, market_group), g in groups:
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
        if profit_pct > ARB_MAX_PROFIT_PCT:
            # Реальные прематч-вилки — единицы процентов. Аномальная
            # «доходность» почти всегда означает ошибку сопоставления
            # или устаревший кэф — такое не показываем, только логируем.
            log.warning(
                "Отброшена подозрительная вилка %.1f%% (> %s%%): %s — %s "
                "[%s] %s %.2f (%s) / %s %.2f (%s)",
                profit_pct, ARB_MAX_PROFIT_PCT, s.team1, s.team2, s.market,
                o1.label, o1.odds, o1.bookmaker,
                o2.label, o2.odds, o2.bookmaker)
            continue
        # порядок исходов: для победителя выравниваем к team1/team2 образца
        first, second = _order(s, o1, o2)
        if s.market_key.startswith("total"):
            label1, label2 = first.label, second.label   # ТБ pt / ТМ pt
        else:
            label1, label2 = "П1", "П2"
        bucket = round((g["sample"].start_ts or 0) / START_TS_TOLERANCE)
        arbs.append(Arb(
            match_key=f"{kind}|{'|'.join(sorted(teams))}|{bucket}|{market_group}",
            sport=s.sport, team1=s.team1, team2=s.team2,
            market=s.market,
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
    else:
        want = f"team:{norm_team(sample.team1)}"
        first = o1 if o1.oid == want else o2
    second = o2 if first is o1 else o1
    return first, second
