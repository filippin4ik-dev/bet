"""Тесты приведения подписей к общему виду: корень вида спорта, раскладка
букв, номер дубля и псевдо-лиги «Статистика».

Всё это — разные способы одной и той же беды: БК пишут об одном и том же
разными знаками, движок видит разные строки и не склеивает событие. Каждый
случай ниже взят с живой линии (см. app/diagnose_overlap.py).

Запуск: python3 -m pytest tests/test_sport_labels.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.arbitrage import (_start_tolerance, build_name_canon_map,  # noqa: E402
                           find_arbs, is_stats_league, norm_team, sport_root)
from app.config import (START_TS_TOLERANCE,  # noqa: E402
                        START_TS_TOLERANCE_COMBAT, START_TS_TOLERANCE_RAPID)
from app.models import KIND_PREMATCH, MarketOdds  # noqa: E402

NOW = 2_000_000_000.0


def mk(bookmaker, k1, k2, team1, team2, start_ts=NOW, sport="Футбол"):
    return MarketOdds(
        bookmaker=bookmaker, sport=sport, team1=team1, team2=team2,
        market="Победитель", market_key="winner", outcome1="П1",
        outcome2="П2", k1=k1, k2=k2, kind=KIND_PREMATCH,
        start_time="01.01 20:00", start_ts=start_ts,
        url=f"https://example.com/{bookmaker}",
    )


# ---------- корень вида спорта: одно и то же под разными подписями ----------

def test_combat_labels_share_one_root():
    """«ММА», «UFC» и «Единоборства/UFC» — это единоборства.

    Корень делит линию на части, внутри которых ищутся похожие написания
    имён. Пока подписи расходились, бойцы одного турнира у разных БК
    сравнивались не друг с другом, а ни с кем."""
    for label in ("ММА", "MMA", "UFC", "Единоборства/UFC",
                  "Смешанные единоборства", "Бои без правил"):
        assert sport_root(f"{label} · UFC 320") == "единоборства", label
    print("OK: test_combat_labels_share_one_root")


def test_combat_labels_get_the_combat_start_tolerance():
    """Бой на карде начинается «после предыдущего», и время старта у БК
    расходится на часы. Подписи «ММА» и «UFC» мимо набора единоборств не
    проходили, и таким боям доставался обычный часовой допуск."""
    for label in ("ММА", "UFC", "Единоборства"):
        assert _start_tolerance(f"{label} · UFC 320") \
            == START_TS_TOLERANCE_COMBAT, label
    print("OK: test_combat_labels_get_the_combat_start_tolerance")


def test_rugby_and_gaelic_labels_share_one_root():
    for label in ("Регби", "Регби Лига", "Регбилиг", "Регби Юнион",
                  "Регби союз"):
        assert sport_root(label) == "регби", label
    for label in ("Гэльский спорт", "Гэльский футбол", "Херлинг"):
        assert sport_root(label) == "гэльский спорт", label
    print("OK: test_rugby_and_gaelic_labels_share_one_root")


def test_betboom_cyber_label_joins_the_rest():
    """BetBoom подписывает весь киберспорт одним словом «Кибер»."""
    assert sport_root("Кибер") == sport_root("Киберспорт · CS2")
    print("OK: test_betboom_cyber_label_joins_the_rest")


def test_different_sports_keep_their_own_roots():
    """Свести под один корень можно спор о названии, но не разные игры."""
    assert sport_root("Пиклбол") != sport_root("Падел-теннис")
    assert sport_root("Бокс") != sport_root("Единоборства")
    assert sport_root("Футбол") != sport_root("Футзал")
    print("OK: test_different_sports_keep_their_own_roots")


def test_combat_names_merge_across_different_sport_labels():
    """Итог всего вышесказанного: бой, подписанный у одной БК «ММА», а у
    другой «Единоборства», склеивается — с поправкой на разное написание
    имени бойца и на расхождение времени начала."""
    odds = [
        mk("Winline", k1=2.10, k2=2.05, team1="Амил Тутич",
           team2="Иван Киш", sport="ММА · Brave FC"),
        mk("Fonbet", k1=1.95, k2=2.30, team1="Амиль Тутич",
           team2="Иван Киш", sport="Единоборства · MMA. Brave FC MMA",
           start_ts=NOW + 2 * 3600),
    ]
    assert find_arbs(odds), "бой один и тот же, подписи разные"
    print("OK: test_combat_names_merge_across_different_sport_labels")


# ---------- раскладка: латиница, неотличимая от кириллицы ----------

def test_latin_lookalikes_fold_into_cyrillic():
    """«Баскетбол 3x3» латинской «x» и «3х3» кириллической — один спорт."""
    assert sport_root("Баскетбол 3x3") == sport_root("Баскетбол 3х3")
    print("OK: test_latin_lookalikes_fold_into_cyrillic")


def test_mixed_layout_team_name_matches_exactly():
    """«Эльвeрсберг» с латинской «e» — то же имя, что и с русской.

    Такая пара и раньше могла дотянуть до порога похожести, но теперь она
    совпадает ТОЧНО, то есть не зависит ни от порога, ни от того, нашёлся
    ли для неё кандидат в окне времени."""
    assert norm_team("Эльвeрсберг") == norm_team("Эльверсберг")
    print("OK: test_mixed_layout_team_name_matches_exactly")


def test_folding_keeps_the_noise_words_working():
    """Складка раскладок не должна ломать словарь слов-шумов: «ФК» и «FC»
    отбрасываются, а женский маркер остаётся на месте."""
    assert norm_team("ФК Ростов") == norm_team("FC Ростов") == "ростов"
    assert norm_team("Герта (жен)") != norm_team("Герта")
    print("OK: test_folding_keeps_the_noise_words_working")


def test_placeholder_teams_still_recognised():
    """Заглушки вместо имён («Хозяева»/«Home») по-прежнему не имена."""
    odds = [
        mk("Winline", k1=2.10, k2=2.05, team1="Хозяева", team2="Гости"),
        mk("Fonbet", k1=1.95, k2=2.30, team1="Home", team2="Away"),
    ]
    assert not find_arbs(odds), "«хозяева/гости» — это не пара команд"
    print("OK: test_placeholder_teams_still_recognised")


# ---------- номер дубля: «II» и «2» ----------

def test_reserve_team_numerals_match():
    assert norm_team("Серро Портеньо II") == norm_team("Серро Портеньо 2")
    assert norm_team("Айнтрахт Франкфурт III") \
        == norm_team("Айнтрахт Франкфурт 3")
    print("OK: test_reserve_team_numerals_match")


def test_reserve_number_still_separates_teams():
    """Дубль — не основа: сводить их в одно событие нельзя."""
    odds = [
        mk("Winline", k1=2.10, k2=2.05,
           team1="Серро Портеньо II", team2="Насьональ Асунсьон II"),
        mk("Fonbet", k1=1.95, k2=2.30,
           team1="Серро Портеньо", team2="Насьональ Асунсьон"),
    ]
    assert not find_arbs(odds), "дубль и основа — разные матчи"
    print("OK: test_reserve_number_still_separates_teams")


# ---------- псевдо-лиги «Статистика» ----------

def _stats(bookmaker, k1, k2, team1, team2):
    return mk(bookmaker, k1, k2, team1, team2,
              sport="Футбол · Россия. Премьер-Лига. Статистика")


def test_stats_league_is_recognised():
    assert is_stats_league("Футбол · Россия. Премьер-Лига. Статистика")
    assert is_stats_league("Баскетбол · Чемпионат мира. Женщины. Статистика")
    assert not is_stats_league("Футбол · Россия. Премьер-Лига")
    print("OK: test_stats_league_is_recognised")


def test_stats_pseudo_teams_are_not_fuzzy_merged():
    """«Удары ПО воротам» и «удары ОТ ворот» отличаются на две буквы, а
    это разные рынки: побуквенно они похожи на 0,93 — выше порога склейки.
    Ставка по такой «вилке» проигрывает обеими ногами."""
    odds = [
        _stats("Winline", 2.10, 2.05,
               "Крылья Советов удары по воротам", "Краснодар удары по воротам"),
        _stats("Fonbet", 1.95, 2.30,
               "Крылья Советов удары от ворот", "Краснодар удары от ворот"),
    ]
    assert not build_name_canon_map(odds), \
        "имена из «Статистики» вообще не участвуют в фаззи-склейке"
    assert not find_arbs(odds), "это два разных рынка, а не вилка"
    print("OK: test_stats_pseudo_teams_are_not_fuzzy_merged")


def test_stats_league_still_matches_on_exact_names():
    """Отказ от фаззи — не отказ от вилок: точное совпадение работает."""
    odds = [
        _stats("Winline", 2.10, 2.05,
               "Зенит удары по воротам", "ЦСКА удары по воротам"),
        _stats("Fonbet", 1.95, 2.30,
               "Зенит удары по воротам", "ЦСКА удары по воротам"),
    ]
    assert find_arbs(odds), "одинаковые имена склеиваются и без фаззи"
    print("OK: test_stats_league_still_matches_on_exact_names")


# ---------- «быстрые» турниры: длительность матча по-английски ----------

def test_rapid_fixture_tolerance_reads_english_minutes():
    """У крипто-БК на BetBy длительность подписана латиницей: «(4x5 min)».

    Пока допуск смотрел только на «мин», самым опасным лигам — где одна
    пара играет каждые десять минут — доставался обычный часовой допуск,
    и два РАЗНЫХ матча этой пары склеивались в одну «вилку»."""
    assert _start_tolerance("NBA 2K26 · H2H GG League (4x5 min)") \
        == START_TS_TOLERANCE_RAPID
    assert _start_tolerance("Киберфутбол · H2H LIGA-3. 2x4 мин.") \
        == START_TS_TOLERANCE_RAPID
    assert _start_tolerance("Футбол · Россия. РПЛ") == START_TS_TOLERANCE
    print("OK: test_rapid_fixture_tolerance_reads_english_minutes")


if __name__ == "__main__":
    test_combat_labels_share_one_root()
    test_combat_labels_get_the_combat_start_tolerance()
    test_rugby_and_gaelic_labels_share_one_root()
    test_betboom_cyber_label_joins_the_rest()
    test_different_sports_keep_their_own_roots()
    test_combat_names_merge_across_different_sport_labels()
    test_latin_lookalikes_fold_into_cyrillic()
    test_mixed_layout_team_name_matches_exactly()
    test_folding_keeps_the_noise_words_working()
    test_placeholder_teams_still_recognised()
    test_reserve_team_numerals_match()
    test_reserve_number_still_separates_teams()
    test_stats_league_is_recognised()
    test_stats_pseudo_teams_are_not_fuzzy_merged()
    test_stats_league_still_matches_on_exact_names()
    print("Все тесты прошли.")
