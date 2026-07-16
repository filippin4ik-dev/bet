"""Поиск двухисходных вилок (П1/П2) по формуле 1/К1_max + 1/К2_max < 1."""
from collections import defaultdict
from typing import Iterable

from .config import BANKS
from .models import Arb, MatchOdds


def calc_stakes(k1: float, k2: float, bank: float) -> dict:
    """Распределение банка между двумя исходами так, чтобы выигрыш был
    одинаковым при любом исходе.

    stake1 = bank * (1/k1) / S,  stake2 = bank * (1/k2) / S,  S = 1/k1 + 1/k2
    """
    s = 1 / k1 + 1 / k2
    stake1 = bank * (1 / k1) / s
    stake2 = bank - stake1
    payout = stake1 * k1  # = stake2 * k2 (с точностью до округления)
    return {
        "stake1": round(stake1),
        "stake2": round(stake2),
        "payout": round(payout, 2),
        "profit": round(payout - bank, 2),
    }


def find_arbs(odds: Iterable[MatchOdds]) -> list[Arb]:
    """Группирует котировки всех БК по матчам, берёт максимальные К1/К2
    и возвращает найденные вилки, отсортированные по доходности."""
    by_match: dict[str, list[MatchOdds]] = defaultdict(list)
    for o in odds:
        if o.k1 and o.k2 and o.k1 > 1 and o.k2 > 1:
            by_match[o.match_key].append(o)

    arbs: list[Arb] = []
    for match_key, quotes in by_match.items():
        if len(quotes) < 2:
            continue  # вилка возможна только при котировках минимум от 2 БК

        best1 = max(quotes, key=lambda q: q.k1)
        best2 = max(quotes, key=lambda q: q.k2)
        if best1.bookmaker == best2.bookmaker:
            # Внутри одной БК маржа гарантирует 1/К1+1/К2 > 1 — не вилка.
            continue

        margin = 1 / best1.k1 + 1 / best2.k2
        if margin >= 1:
            continue

        profit_pct = (1 / margin - 1) * 100
        arbs.append(Arb(
            match_key=match_key,
            sport=best1.sport,
            team1=best1.team1,
            team2=best1.team2,
            k1_max=round(best1.k1, 3),
            k1_bookmaker=best1.bookmaker,
            k2_max=round(best2.k2, 3),
            k2_bookmaker=best2.bookmaker,
            margin=margin,
            profit_pct=profit_pct,
            stakes={str(b): calc_stakes(best1.k1, best2.k2, b) for b in BANKS},
        ))

    arbs.sort(key=lambda a: a.profit_pct, reverse=True)
    return arbs
