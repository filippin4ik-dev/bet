"""Тесты допуска расхождения времени старта между БК (_start_tolerance) и
эффекта на склейку событий в find_arbs/find_arbs_1x2.

Запуск: python3 tests/test_start_tolerance.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.arbitrage import _start_tolerance, find_arbs  # noqa: E402
from app.config import (START_TS_TOLERANCE, START_TS_TOLERANCE_COMBAT,  # noqa: E402
                        START_TS_TOLERANCE_RAPID)
from app.models import KIND_PREMATCH, MarketOdds  # noqa: E402

NOW = 2_000_000_000.0


def mk(bookmaker, k1, k2, start_ts, sport="Футбол", team1="Спартак",
      team2="Зенит"):
    return MarketOdds(
        bookmaker=bookmaker, sport=sport, team1=team1, team2=team2,
        market="Победитель", market_key="winner", outcome1="П1",
        outcome2="П2", k1=k1, k2=k2, kind=KIND_PREMATCH,
        start_time="01.01 20:00", start_ts=start_ts,
        url=f"https://example.com/{bookmaker}",
    )


def test_default_tolerance_for_regular_sports():
    for sport in ("Футбол", "Теннис", "Хоккей", "Киберспорт · VCT",
                  "Волейбол · Чемпионат Италии"):
        assert _start_tolerance(sport) == START_TS_TOLERANCE, sport
    print("OK: test_default_tolerance_for_regular_sports")


def test_combat_tolerance():
    for sport in ("Единоборства", "Бокс · Карта вечера", "MMA",
                  "Единоборства · UFC 305"):
        assert _start_tolerance(sport) == START_TS_TOLERANCE_COMBAT, sport
    print("OK: test_combat_tolerance")


def test_rapid_fixture_tolerance():
    samples = (
        "Футбол · FC 26. United Esports Leagues. 2X3 мин. Чехия",
        "Футбол · FC 26. H2H LIGA-3. 2x4 мин.",
        "Киберспорт · Виртуальный турнир 1x1 мин",
    )
    for sport in samples:
        assert _start_tolerance(sport) == START_TS_TOLERANCE_RAPID, sport
    print("OK: test_rapid_fixture_tolerance")


def test_regular_sport_merges_within_hour_gap():
    """Диагностика показала реальные пары с одинаковыми именами команд и
    расхождением start_ts до ~60 мин у обычных видов спорта — такие
    события должны склеиваться (в отличие от старого 30-минутного
    допуска)."""
    gap = 55 * 60  # 55 минут — укладывается в новый допуск (60 мин), но
                   # не в старый (30 мин)
    odds = [
        mk("Winline", k1=2.10, k2=2.05, start_ts=NOW),
        mk("Fonbet", k1=1.95, k2=2.30, start_ts=NOW + gap),
    ]
    arbs = find_arbs(odds)
    assert arbs, "события с одинаковыми командами и разницей 55 мин должны склеиться"
    print("OK: test_regular_sport_merges_within_hour_gap")


def test_rapid_fixture_does_not_merge_within_hour_gap():
    """Для «быстрых» турниров (виртуальный футбол и т.п.) тот же 55-минутный
    разрыв НЕ должен склеивать события — иначе рискуем смешать два разных
    матча одной пары за сессию."""
    gap = 55 * 60
    sport = "Футбол · FC 26. United Esports Leagues. 2x3 мин. Чехия"
    odds = [
        mk("Winline", k1=2.10, k2=2.05, start_ts=NOW, sport=sport),
        mk("Fonbet", k1=1.95, k2=2.30, start_ts=NOW + gap, sport=sport),
    ]
    arbs = find_arbs(odds)
    assert not arbs, (
        "быстрые турниры с разрывом 55 мин не должны склеиваться в одно "
        "событие")
    print("OK: test_rapid_fixture_does_not_merge_within_hour_gap")


if __name__ == "__main__":
    test_default_tolerance_for_regular_sports()
    test_combat_tolerance()
    test_rapid_fixture_tolerance()
    test_regular_sport_merges_within_hour_gap()
    test_rapid_fixture_does_not_merge_within_hour_gap()
    print("Все тесты прошли.")
