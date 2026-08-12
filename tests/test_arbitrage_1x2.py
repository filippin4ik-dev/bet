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


# ---------- полнота перебора троек ----------

def _three_books():
    """Фонбет, БетБум и Винлайн на одном событии; ни у одной из них своей
    вилки нет — все вилки только МЕЖДУ конторами."""
    return [
        mk("Fonbet", k1=2.60, k2=3.00, k3=3.30, outcome3="X"),
        mk("BetBoom", k1=2.20, k2=3.10, k3=3.90, outcome3="X"),
        mk("Winline", k1=2.30, k2=3.80, k3=3.20, outcome3="X"),
    ]


def test_every_bookmaker_triple_is_checked():
    """Перебираются ВСЕ комбинации «П1 у одной БК × X у другой × П2 у
    третьей», а не только тройка с максимальными кэфами.

    Иначе терялись бы вилки, где лучший кэф на один исход стоит у БК,
    чей кэф на другой исход слабый: сама по себе такая нога вилку не
    делает, а в паре с третьей конторой — делает."""
    arbs = find_arbs_1x2(_three_books())
    combos = {(a.k1_bookmaker, a.kx_bookmaker, a.k2_bookmaker) for a in arbs}
    # все три плеча у разных БК — та самая «тройка» из трёх контор
    assert ("Fonbet", "BetBoom", "Winline") in combos, combos
    # и комбинации с двумя БК тоже: третья контора не обязана участвовать
    assert ("Fonbet", "BetBoom", "BetBoom") in combos, combos
    assert ("Fonbet", "Fonbet", "Winline") in combos, combos
    # ни одной тройки целиком у одной БК: это её маржа, а не вилка
    assert all(len(set(c)) > 1 for c in combos)
    print("OK: test_every_bookmaker_triple_is_checked")


def test_no_profitable_triple_is_missed():
    """Список вилок совпадает с прямым перебором всех троек «в лоб»."""
    odds = _three_books()
    by_bk = {o.bookmaker: o for o in odds}
    expected = set()
    for b1, o1 in by_bk.items():
        for bx, ox in by_bk.items():
            for b2, o2 in by_bk.items():
                if len({b1, bx, b2}) < 2:
                    continue
                if 1 / o1.k1 + 1 / ox.k3 + 1 / o2.k2 < 1:
                    expected.add((b1, bx, b2))
    found = {(a.k1_bookmaker, a.kx_bookmaker, a.k2_bookmaker)
             for a in find_arbs_1x2(odds)}
    assert found == expected, (found ^ expected)
    print("OK: test_no_profitable_triple_is_missed")


def test_triple_survives_reversed_team_order():
    """У одной БК команды записаны в обратном порядке — событие всё равно
    одно, и тройка между тремя конторами должна находиться."""
    odds = _three_books()
    flipped = mk("Winline", k1=3.80, k2=2.30, k3=3.20, outcome3="X",
                 team1="Зенит", team2="Спартак")
    odds[2] = flipped
    arbs = find_arbs_1x2(odds)
    combos = {(a.k1_bookmaker, a.kx_bookmaker, a.k2_bookmaker) for a in arbs}
    assert ("Fonbet", "BetBoom", "Winline") in combos, combos
    best = max(arbs, key=lambda a: a.profit_pct)
    # П1 и П2 привязаны к командам образца, а не к порядку колонок у БК
    assert best.k1_max == 2.60 and best.k2_max == 3.80
    print("OK: test_triple_survives_reversed_team_order")


def test_best_odds_of_each_bookmaker_are_used():
    """Если БК прислала рынок дважды (основная линия и дубль), в переборе
    участвует её ЛУЧШИЙ кэф по каждому исходу."""
    odds = _three_books()
    odds.append(mk("BetBoom", k1=2.20, k2=3.10, k3=4.20, outcome3="X"))
    best = max(find_arbs_1x2(odds), key=lambda a: a.profit_pct)
    assert best.kx_max == 4.20 and best.kx_bookmaker == "BetBoom"
    print("OK: test_best_odds_of_each_bookmaker_are_used")


if __name__ == "__main__":
    test_find_arbs_1x2_basic()
    test_winner1x2_excluded_from_2way_engine()
    test_no_arb_when_margin_over_one()
    test_requires_two_bookmakers()
    test_every_bookmaker_triple_is_checked()
    test_no_profitable_triple_is_missed()
    test_triple_survives_reversed_team_order()
    test_best_odds_of_each_bookmaker_are_used()
    print("Все тесты прошли.")
