"""Тесты фаззи-слияния разных написаний имени команды между БК
(build_name_canon_map) и его эффекта на find_arbs/Scanner._event_groups.

Запуск: python3 -m pytest tests/test_fuzzy_names.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.arbitrage import (build_name_canon_map, find_arbs,  # noqa: E402
                           norm_team)
from app.models import KIND_PREMATCH, MarketOdds  # noqa: E402
from app.scanner import Scanner  # noqa: E402

NOW = 2_000_000_000.0


def mk(bookmaker, k1, k2, team1, team2, start_ts=NOW, sport="Футбол"):
    return MarketOdds(
        bookmaker=bookmaker, sport=sport, team1=team1, team2=team2,
        market="Победитель", market_key="winner", outcome1="П1",
        outcome2="П2", k1=k1, k2=k2, kind=KIND_PREMATCH,
        start_time="01.01 20:00", start_ts=start_ts,
        url=f"https://example.com/{bookmaker}",
    )


def test_transliteration_variant_merges_into_arb():
    """«Кимчхон Санму»/«Кимчхон Сангму» — одна и та же команда, встреченная
    на живых данных (см. app/diagnose_overlap.py). Без фаззи-слияния имён
    событие не склеилось бы, и реальная вилка терялась."""
    odds = [
        mk("Winline", k1=2.10, k2=2.05,
           team1="Пхохан Стилерс", team2="Кимчхон Санму"),
        mk("Fonbet", k1=1.95, k2=2.30,
           team1="Пхохан Стилерс", team2="Кимчхон Сангму"),
    ]
    arbs = find_arbs(odds)
    assert arbs, "похожие написания одной команды должны склеиться в вилку"
    print("OK: test_transliteration_variant_merges_into_arb")


def test_typo_in_both_teams_merges():
    odds = [
        mk("Winline", k1=2.10, k2=2.05,
           team1="Виктория Плзень", team2="Зброевка Брно"),
        mk("Fonbet", k1=1.95, k2=2.30,
           team1="Виктория Пльзень", team2="Зброевка Брно"),
    ]
    arbs = find_arbs(odds)
    assert arbs
    print("OK: test_typo_in_both_teams_merges")


def test_different_teams_not_merged():
    """Разные матчи с похожим, но не идентичным контекстом НЕ должны
    сливаться — иначе риск ложной вилки. «Йоркшир» vs «Вустершир Роялз» —
    разные соперники, при этом Йоркшир играет и с другим клубом."""
    odds = [
        mk("Winline", k1=2.10, k2=2.05,
           team1="Йоркшир", team2="Вустершир"),
        mk("Fonbet", k1=1.95, k2=2.30,
           team1="Ланкашир", team2="Вустершир"),
    ]
    arbs = find_arbs(odds)
    assert not arbs, "разных соперников нельзя склеивать в одно событие"
    print("OK: test_different_teams_not_merged")


def test_one_team_totally_different_not_merged():
    """Защита от «команда 1 совпала на 100%, команда 2 — случайный сосед
    по среднему похожести»: обе команды пары должны быть похожи
    ИНДИВИДУАЛЬНО (FUZZY_NAME_MIN_SINGLE_RATIO), а не только в среднем."""
    odds = [
        mk("Winline", k1=2.10, k2=2.05,
           team1="Спартак", team2="Зенит"),
        mk("Fonbet", k1=1.95, k2=2.30,
           team1="Спартак", team2="Локомотив"),
    ]
    arbs = find_arbs(odds)
    assert not arbs, ("одна команда совпала, другая совсем другая — это "
                      "не тот же матч")
    print("OK: test_one_team_totally_different_not_merged")


def test_same_bookmaker_never_merged():
    """Одна и та же БК никогда не сливается сама с собой — даже похожие
    (но при этом РАЗНЫЕ после нормализации) имена у ОДНОЙ БК означают два
    разных реальных матча, а не опечатку между БК."""
    odds = [
        mk("Winline", k1=2.10, k2=2.05,
           team1="Кимчхон Санму", team2="Пхохан Стилерс"),
        mk("Winline", k1=1.95, k2=2.30,
           team1="Кимчхон Сангму", team2="Инчхон Юнайтед",
           start_ts=NOW + 300),
    ]
    name_map = build_name_canon_map(odds)
    t_a = norm_team("Кимчхон Санму")
    t_b = norm_team("Кимчхон Сангму")
    assert name_map.get(t_a, t_a) != name_map.get(t_b, t_b) or t_a == t_b, (
        "события ОДНОЙ БК не должны сливать разные написания команды — "
        "это может быть два разных реальных матча")
    print("OK: test_same_bookmaker_never_merged")


def test_event_groups_merge_fuzzy_variant():
    odds = [
        mk("Winline", k1=2.10, k2=2.05,
           team1="Аустрия Вена 2", team2="Блау Вайс Линц"),
        mk("Fonbet", k1=1.95, k2=2.30,
           team1="Аустрия Вена 2", team2="Блау-Вайсс Линц"),
    ]
    groups = Scanner._event_groups(odds)
    assert len(groups) == 1, "должно остаться ровно одно событие"
    merged = next(iter(groups.values()))
    assert {o.bookmaker for o in merged} == {"Winline", "Fonbet"}
    print("OK: test_event_groups_merge_fuzzy_variant")


def test_far_apart_start_ts_not_merged_despite_similar_names():
    """Даже похожие имена не должны склеиваться, если время старта
    расходится намного больше допуска вида спорта — иначе рискуем
    смешать два разных матча той же (или похожей) пары."""
    odds = [
        mk("Winline", k1=2.10, k2=2.05,
           team1="Пхохан Стилерс", team2="Кимчхон Санму",
           start_ts=NOW),
        mk("Fonbet", k1=1.95, k2=2.30,
           team1="Пхохан Стилерс", team2="Кимчхон Сангму",
           start_ts=NOW + 5 * 3600),  # +5 часов — намного больше допуска
    ]
    arbs = find_arbs(odds)
    assert not arbs, "разрыв в 5 часов не должен склеиваться для футбола"
    print("OK: test_far_apart_start_ts_not_merged_despite_similar_names")


if __name__ == "__main__":
    test_transliteration_variant_merges_into_arb()
    test_typo_in_both_teams_merges()
    test_different_teams_not_merged()
    test_one_team_totally_different_not_merged()
    test_same_bookmaker_never_merged()
    test_event_groups_merge_fuzzy_variant()
    test_far_apart_start_ts_not_merged_despite_similar_names()
    print("Все тесты прошли.")
