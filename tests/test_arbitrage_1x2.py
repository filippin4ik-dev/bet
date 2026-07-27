"""Быстрые sanity-тесты движка вилок (без сети, без pytest).

Запуск: python3 tests/test_arbitrage_1x2.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.arbitrage import find_arbs, find_arbs_1x2  # noqa: E402
from app.models import KIND_PREMATCH, MarketOdds  # noqa: E402

NOW = 2_000_000_000.0  # далёкое будущее — матч не "начался"


def mk(bookmaker, k1, k2, k3=None, market_key="winner1x2", team1="Спартак",
      team2="Зенит", outcome3=None):
    return MarketOdds(
        bookmaker=bookmaker, sport="Футбол", team1=team1, team2=team2,
        market="Исход (1X2)" if k3 else "Победитель", market_key=market_key,
        outcome1="П1", outcome2="П2", outcome3=outcome3,
        k1=k1, k2=k2, k3=k3, kind=KIND_PREMATCH,
        start_time="01.01 20:00", start_ts=NOW,
        url=f"https://example.com/{bookmaker}",
    )


def test_find_arbs_1x2_basic():
    odds = [
        mk("Fonbet", k1=2.10, k2=3.40, k3=3.60, outcome3="X"),
        mk("BetBoom", k1=1.95, k2=4.20, k3=3.30, outcome3="X"),
    ]
    arbs = find_arbs_1x2(odds)
    assert arbs, "должна найтись хотя бы одна 1X2-вилка"
    best = arbs[0]
    assert best.k1_max == 2.10 and best.k1_bookmaker == "Fonbet"
    assert best.k2_max == 4.20 and best.k2_bookmaker == "BetBoom"
    assert best.kx_max == 3.60 and best.kx_bookmaker == "Fonbet"
    margin = 1 / best.k1_max + 1 / best.kx_max + 1 / best.k2_max
    assert margin < 1, f"маржа должна быть < 1, получили {margin}"
    assert abs(best.margin - margin) < 1e-9
    stakes = best.stakes["1000"]
    payout1 = stakes["stake1"] * best.k1_max
    payoutx = stakes["stakex"] * best.kx_max
    payout2 = stakes["stake2"] * best.k2_max
    # выигрыш должен быть примерно одинаков при любом исходе
    assert abs(payout1 - payoutx) < 5
    assert abs(payout1 - payout2) < 5
    print("OK: test_find_arbs_1x2_basic")


def test_winner1x2_excluded_from_2way_engine():
    """1X2-рынок не должен просачиваться в 2-исходный движок (иначе
    ставка без покрытия ничьей ошибочно показалась бы «безрисковой»)."""
    odds = [
        mk("Fonbet", k1=2.10, k2=3.40, k3=3.60, outcome3="X"),
        mk("BetBoom", k1=1.95, k2=4.20, k3=3.30, outcome3="X"),
    ]
    arbs2 = find_arbs(odds)
    assert not arbs2, (
        "winner1x2 не должен обрабатываться двухисходным движком find_arbs")
    print("OK: test_winner1x2_excluded_from_2way_engine")


def test_no_arb_when_margin_over_one():
    odds = [
        mk("Fonbet", k1=1.90, k2=3.40, k3=3.60, outcome3="X"),
        mk("BetBoom", k1=1.85, k2=3.20, k3=3.30, outcome3="X"),
    ]
    arbs = find_arbs_1x2(odds)
    assert not arbs, "маржа книжек >1 — вилки быть не должно"
    print("OK: test_no_arb_when_margin_over_one")


def test_requires_two_bookmakers():
    odds = [mk("Fonbet", k1=2.10, k2=3.40, k3=3.60, outcome3="X")]
    arbs = find_arbs_1x2(odds)
    assert not arbs, "одна БК — не вилка (только 1 источник кэфов)"
    print("OK: test_requires_two_bookmakers")


if __name__ == "__main__":
    test_find_arbs_1x2_basic()
    test_winner1x2_excluded_from_2way_engine()
    test_no_arb_when_margin_over_one()
    test_requires_two_bookmakers()
    print("Все тесты прошли.")
