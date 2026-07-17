"""Тесты логики поиска вилок: запуск `python3 -m pytest tests/ -q`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.arbitrage import calc_stakes, find_arbs  # noqa: E402
from app.models import MarketOdds  # noqa: E402

TS = 2_000_000_000.0


def mo(bk, t1, t2, mkey, k1, k2, ts=TS, market="Победитель",
       o1="П1", o2="П2"):
    return MarketOdds(bookmaker=bk, sport="Теннис", team1=t1, team2=t2,
                      market=market, market_key=mkey, outcome1=o1,
                      outcome2=o2, k1=k1, k2=k2, start_ts=ts,
                      start_time="Сегодня 12:00")


def test_basic_arb_with_reversed_team_order():
    """Вилка находится, даже если БК записали команды в разном порядке."""
    arbs = find_arbs([
        mo("Winline", "Иванов", "Петров", "winner", 2.1, 1.8),
        # у Fonbet первым идёт Петров: k1 — кэф на Петрова
        mo("Fonbet", "Петров", "Иванов", "winner", 2.2, 1.9),
    ])
    assert len(arbs) == 1
    a = arbs[0]
    # лучший кэф на Иванова 2.1 (Winline), на Петрова 2.2 (Fonbet)
    assert (a.k1_max, a.k1_bookmaker) == (2.1, "Winline")
    assert (a.k2_max, a.k2_bookmaker) == (2.2, "Fonbet")
    expected = (1 / (1 / 2.1 + 1 / 2.2) - 1) * 100
    assert abs(a.profit_pct - expected) < 1e-9


def test_winner_and_total_never_mix():
    """П1 не может попасть в вилку с тоталом: разные market_key."""
    arbs = find_arbs([
        mo("Winline", "Иванов", "Петров", "winner", 2.5, 1.6),
        mo("Fonbet", "Иванов", "Петров", "total:21.5", 2.5, 1.6,
           market="Тотал 21.5", o1="ТБ 21.5", o2="ТМ 21.5"),
    ])
    assert arbs == []


def test_set_market_never_mixes_with_match():
    """Победитель сета не сравнивается с победителем матча."""
    arbs = find_arbs([
        mo("Winline", "Иванов", "Петров", "winner:1сет", 2.6, 1.55),
        mo("Fonbet", "Иванов", "Петров", "winner", 1.55, 2.6),
    ])
    assert arbs == []


def test_different_start_times_are_different_events():
    """Лига Про: те же соперники в разное время — разные матчи."""
    arbs = find_arbs([
        mo("Winline", "Иванов", "Петров", "winner", 2.15, 1.7, ts=TS),
        mo("Fonbet", "Иванов", "Петров", "winner", 1.7, 2.15,
           ts=TS + 3 * 3600),
    ])
    assert arbs == []


def test_small_start_time_skew_is_tolerated():
    arbs = find_arbs([
        mo("Winline", "Иванов", "Петров", "winner", 2.15, 1.7, ts=TS),
        mo("Fonbet", "Иванов", "Петров", "winner", 1.7, 2.15, ts=TS + 300),
    ])
    assert len(arbs) == 1


def test_anomalous_profit_is_dropped():
    """Доходность в десятки процентов — ошибка данных, не показываем."""
    arbs = find_arbs([
        mo("Winline", "Иванов", "Петров", "winner", 3.5, 1.5),
        mo("Fonbet", "Иванов", "Петров", "winner", 1.4, 3.5),
    ])
    assert arbs == []


def test_totals_same_line_only():
    """Тотал сопоставляется только с тоталом ТОЙ ЖЕ линии."""
    arbs = find_arbs([
        mo("Winline", "Иванов", "Петров", "total:21.5", 2.15, 1.8,
           market="Тотал 21.5", o1="ТБ 21.5", o2="ТМ 21.5"),
        mo("Fonbet", "Иванов", "Петров", "total:21.5", 1.8, 2.15,
           market="Тотал 21.5", o1="ТБ 21.5", o2="ТМ 21.5"),
        mo("Fonbet", "Иванов", "Петров", "total:22.5", 1.2, 4.5,
           market="Тотал 22.5", o1="ТБ 22.5", o2="ТМ 22.5"),
    ])
    assert len(arbs) == 1
    a = arbs[0]
    assert a.outcome1.startswith("ТБ") and a.outcome2.startswith("ТМ")
    assert (a.k1_max, a.k1_bookmaker) == (2.15, "Winline")
    assert (a.k2_max, a.k2_bookmaker) == (2.15, "Fonbet")


def test_single_bookmaker_is_not_arb():
    arbs = find_arbs([mo("Winline", "Иванов", "Петров", "winner", 2.2, 2.2)])
    assert arbs == []


def test_calc_stakes_equal_payout():
    st = calc_stakes(2.1, 2.2, 10000)
    assert st["profit"] > 0
    assert abs(st["stake1"] + st["stake2"] - 10000) <= 1
    # выигрыш одинаков при любом исходе (с точностью до округления ставок)
    assert abs(st["stake1"] * 2.1 - st["stake2"] * 2.2) < 60
