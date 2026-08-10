"""Тесты на вилки, которые движок раньше ПРОПУСКАЛ.

Каждый тест здесь — реальный класс потери: рынок у обеих БК есть, вилка по
нему арифметически существует, а движок её не показывал. Такие пропуски
особенно неприятны тем, что снаружи не видно ничего: ни ошибки, ни пустой
БК — просто вилок меньше, чем могло быть.

Запуск: python3 -m pytest tests/test_missed_arbs.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.arbitrage import (build_name_canon_map, find_arbs,  # noqa: E402
                           norm_team)
from app.models import KIND_LIVE, KIND_PREMATCH, MarketOdds  # noqa: E402
from app.parsers.html_utils import (canon_market_key,  # noqa: E402
                                    market_scope)

NOW = 2_000_000_000.0


def mk(bookmaker, market_key, k1, k2, outcome1="Ф1 -1.5", outcome2="Ф2 +1.5",
       team1="Спартак", team2="Зенит", start_ts=NOW, sport="Футбол"):
    return MarketOdds(
        bookmaker=bookmaker, sport=sport, team1=team1, team2=team2,
        market=market_key, market_key=market_key,
        outcome1=outcome1, outcome2=outcome2, k1=k1, k2=k2,
        kind=KIND_PREMATCH, start_time="01.01 20:00", start_ts=start_ts)


# ---------- ключ рынка: разная форма у разных БК ----------

def test_main_handicap_matches_across_key_forms():
    """Фора всего матча у Betcity/LeonBet записывалась как «hcap:-1.5», у
    остальных БК — как «hcap::-1.5». Один и тот же рынок попадал в разные
    группы, и вилка по нему не находилась НИКОГДА."""
    odds = [
        mk("Betcity", "hcap:-1.5", 2.20, 1.90),
        mk("Winline", "hcap::-1.5", 1.80, 2.30),
    ]
    arbs = find_arbs(odds)
    assert len(arbs) == 1, "фора матча обязана сшиваться между БК"
    assert {arbs[0].k1_bookmaker, arbs[0].k2_bookmaker} == {"Betcity",
                                                            "Winline"}
    print("OK: test_main_handicap_matches_across_key_forms")


def test_handicap_sides_stay_correct_across_key_forms():
    """Сшили — но не перепутали стороны: Ф1 одной БК должна встать против
    Ф2 другой, а не против её же Ф1 (иначе «вилка» ставится на один исход
    дважды)."""
    odds = [
        mk("Betcity", "hcap:-1.5", 2.20, 1.90),
        mk("Winline", "hcap::-1.5", 1.80, 2.30),
    ]
    arb = find_arbs(odds)[0]
    assert (arb.k1_max, arb.k1_bookmaker) == (2.20, "Betcity")
    assert (arb.k2_max, arb.k2_bookmaker) == (2.30, "Winline")
    print("OK: test_handicap_sides_stay_correct_across_key_forms")


def test_handicap_line_format_is_normalised():
    """Линия «1.5» без знака и «+1.5» — одна и та же фора: расхождение в
    формате не должно разводить рынок по разным группам."""
    assert canon_market_key("hcap::1.5") == canon_market_key("hcap::+1.5")
    assert canon_market_key("hcap:1.5") == canon_market_key("hcap::+1.5")
    assert canon_market_key("hcap:half1:-1.5") == "hcap:half1:-1.5"
    print("OK: test_handicap_line_format_is_normalised")


def test_total_line_format_is_normalised():
    """«2.0» и «2» — один и тот же тотал."""
    assert canon_market_key("total:2.0") == canon_market_key("total:2")
    assert canon_market_key("total:corners:9.50") == "total:corners:9.5"
    assert canon_market_key("itotal:1::2.50") == "itotal:1::2.5"
    print("OK: test_total_line_format_is_normalised")


def test_canon_leaves_other_markets_alone():
    """Остальные ключи трогать нельзя — их форма и так общая у всех БК."""
    for key in ("winner", "winner:half1", "winner1x2", "bothscore",
                "oddeven:period2"):
        assert canon_market_key(key) == key
    print("OK: test_canon_leaves_other_markets_alone")


def test_different_handicap_lines_still_not_merged():
    """Приведение формы не должно склеивать РАЗНЫЕ линии форы: -1.5 и -2.5
    — разные рынки, вилки между ними не существует."""
    odds = [
        mk("Betcity", "hcap:-1.5", 2.20, 1.90),
        mk("Winline", "hcap::-2.5", 1.80, 2.30),
    ]
    assert not find_arbs(odds), "разные линии форы — разные рынки"
    print("OK: test_different_handicap_lines_still_not_merged")


def test_period_handicap_not_merged_with_full_match():
    """Фора 1-го тайма и фора всего матча — разные рынки, несмотря на
    одинаковую линию."""
    odds = [
        mk("Betcity", "hcap:half1:-1.5", 2.20, 1.90),
        mk("Winline", "hcap::-1.5", 1.80, 2.30),
    ]
    assert not find_arbs(odds), "рынок тайма нельзя сшивать с рынком матча"
    print("OK: test_period_handicap_not_merged_with_full_match")


# ---------- фаззи-слияние имён: третья БК без пары ----------

def _named(bookmaker, team1, k1, k2, team2="Партизан"):
    return mk(bookmaker, "winner", k1, k2, outcome1="П1", outcome2="П2",
              team1=team1, team2=team2)


def test_third_bookmaker_spelling_merges_when_two_others_agree():
    """Две БК пишут имя одинаково, третья — с опечаткой. Раньше общее
    написание выбывало из фаззи-поиска как «уже совпавшее у 2+ БК», третьей
    БК было не с чем слиться, и её кэфы вообще не попадали в событие."""
    odds = [
        _named("Winline", "Црвена Звезда", 2.05, 2.05),
        _named("Fonbet", "Црвена Звезда", 2.05, 2.05),
        _named("Betcity", "Црвена Зведза", 2.30, 1.95),
    ]
    name_map = build_name_canon_map(odds)
    a = name_map.get(norm_team("Црвена Звезда"))
    b = name_map.get(norm_team("Црвена Зведза"))
    assert a is not None and a == b, (
        "написание третьей БК должно слиться с общим написанием остальных")

    arbs = find_arbs(odds)
    assert any(arb.k2_bookmaker == "Betcity" or arb.k1_bookmaker == "Betcity"
               for arb in arbs), "вилка с третьей БК должна находиться"
    print("OK: test_third_bookmaker_spelling_merges_when_two_others_agree")


def test_two_bookmakers_each_side_of_a_spelling_merge():
    """Крайний случай той же ошибки: две БК пишут «Плзень», две другие —
    «Пльзень». Раньше обе группы считались «уже совпавшими» и не
    сравнивались между собой вовсе — терялись все вилки между группами."""
    odds = [
        _named("Winline", "Виктория Плзень", 2.05, 2.05, team2="Зброевка"),
        _named("Fonbet", "Виктория Плзень", 2.05, 2.05, team2="Зброевка"),
        _named("Betcity", "Виктория Пльзень", 2.30, 1.95, team2="Зброевка"),
        _named("LeonBet", "Виктория Пльзень", 2.30, 1.95, team2="Зброевка"),
    ]
    name_map = build_name_canon_map(odds)
    a = name_map.get(norm_team("Виктория Плзень"))
    b = name_map.get(norm_team("Виктория Пльзень"))
    assert a is not None and a == b, (
        "два разных написания, у каждого по две БК, тоже должны сливаться")
    print("OK: test_two_bookmakers_each_side_of_a_spelling_merge")


def test_same_single_bookmaker_spellings_still_not_merged():
    """Защита остаётся: если оба написания встречаются ТОЛЬКО у одной и той
    же БК, это два разных её матча, а не опечатка между БК."""
    odds = [
        _named("Winline", "Кимчхон Санму", 2.05, 2.05, team2="Пхохан"),
        mk("Winline", "winner", 1.95, 2.30, outcome1="П1", outcome2="П2",
           team1="Кимчхон Сангму", team2="Пхохан", start_ts=NOW + 300),
    ]
    name_map = build_name_canon_map(odds)
    a = norm_team("Кимчхон Санму")
    b = norm_team("Кимчхон Сангму")
    assert name_map.get(a, a) != name_map.get(b, b), (
        "разные написания у ОДНОЙ БК — это разные матчи")
    print("OK: test_same_single_bookmaker_spellings_still_not_merged")


def test_one_malformed_quote_does_not_wipe_out_every_arb():
    """Ключ рынка неожиданной формы («hcap» без сегментов, «itotal» без
    scope) разбирался по сегментам и падал с ValueError — но падал ВНУТРИ
    общего пересчёта. То есть одна кривая котировка одной БК уносила разом
    ВСЕ вилки по всем БК, и так каждый цикл, пока эта котировка жива
    (до ODDS_TTL — получаса). Не понять один рынок можно, потерять всю
    линию — нет."""
    healthy = [
        mk("Winline", "winner", 2.20, 1.90, outcome1="П1", outcome2="П2"),
        mk("Fonbet", "winner", 1.80, 2.30, outcome1="П1", outcome2="П2"),
    ]
    assert len(find_arbs(healthy)) == 1
    for broken in ("hcap", "hcap:", "itotal", "itotal:1:2.5", "total"):
        odds = healthy + [mk("Betcity", broken, 2.0, 2.0)]
        assert len(find_arbs(odds)) == 1, (
            f"котировка с ключом {broken!r} не должна ронять пересчёт")
    print("OK: test_one_malformed_quote_does_not_wipe_out_every_arb")


def test_canon_fills_in_missing_key_segments():
    """Недостающий scope достраивается на своё место — перед линией, а не
    в конец (иначе линия рынка стала бы его уточнением)."""
    assert canon_market_key("hcap") == "hcap::"
    assert canon_market_key("hcap:-1.5") == "hcap::-1.5"
    assert canon_market_key("itotal:1:2.5") == "itotal:1::2.5"
    assert canon_market_key("itotal:2:half1:1.5") == "itotal:2:half1:1.5"
    print("OK: test_canon_fills_in_missing_key_segments")


def test_live_quotes_get_fuzzy_name_merging_too():
    """Лайв-сканер держит ТОЛЬКО лайв-котировки, а фаззи-слияние имён брало
    в работу один прематч — карта имён у лайва выходила пустой всегда, и
    любое расхождение в написании стоило лайв-вилки целиком."""
    live = [
        MarketOdds(bookmaker=bk, sport="Футбол", team1=team1,
                   team2="Партизан", market="Победитель",
                   market_key="winner", outcome1="П1", outcome2="П2",
                   k1=k1, k2=k2, kind=KIND_LIVE, start_ts=NOW,
                   start_time="01.01 20:00")
        for bk, team1, k1, k2 in (
            ("Winline", "Црвена Звезда", 2.20, 1.90),
            ("Fonbet", "Црвена Зведза", 1.80, 2.30))
    ]
    assert build_name_canon_map(live), "лайв тоже должен сливать написания"
    arbs = find_arbs(live)
    assert len(arbs) == 1
    assert arbs[0].kind == KIND_LIVE
    print("OK: test_live_quotes_get_fuzzy_name_merging_too")


def test_live_quote_without_start_time_still_merges_names():
    """Лайв-парсеры, в отличие от прематчевых, время старта НЕ требуют:
    матч уже идёт, отбрасывать его не за что, — и у BetBoom, Fonbet, Melbet
    и bc.game оно вполне может не прийти. А слияние написаний имён брало
    только котировки со временем старта: БК без него выпадала из карты имён,
    и её кэфы не попадали в событие. В лайве это тем обиднее, что событие по
    времени там и не разбивается (_time_clusters работает по прематчу) —
    якоря нет ни у кого, терять было нечего."""
    def live(bk, team1, k1, k2, start_ts):
        return MarketOdds(
            bookmaker=bk, sport="Футбол", team1=team1, team2="Партизан",
            market="Победитель", market_key="winner",
            outcome1="П1", outcome2="П2", k1=k1, k2=k2,
            kind=KIND_LIVE, start_ts=start_ts, start_time=None)

    odds = [live("Winline", "Црвена Звезда", 2.20, 1.90, NOW),
            live("Fonbet", "Црвена Зведза", 1.80, 2.30, None)]
    name_map = build_name_canon_map(odds)
    assert name_map.get(norm_team("Црвена Звезда")) == \
        name_map.get(norm_team("Црвена Зведза")) is not None
    assert len(find_arbs(odds)) == 1
    print("OK: test_live_quote_without_start_time_still_merges_names")


def test_prematch_without_start_time_is_still_left_alone():
    """Обратная граница: в прематче окно по времени старта — единственное,
    что отделяет первый матч пары от ответного, и без него сливать имена
    нельзя. Такая котировка в карту имён по-прежнему не попадает."""
    odds = [
        _named("Winline", "Црвена Звезда", 2.20, 1.90),
        mk("Fonbet", "winner", 1.80, 2.30, outcome1="П1", outcome2="П2",
           team1="Црвена Зведза", team2="Партизан", start_ts=None),
    ]
    assert not build_name_canon_map(odds), (
        "прематч без времени старта сливать имена не должен")
    print("OK: test_prematch_without_start_time_is_still_left_alone")


# ---------- подпись рынка: одно и то же разными словами ----------

def _scopes(*names):
    return {market_scope(n) for n in names}


def test_corner_market_is_the_same_whatever_it_is_called():
    """«Угловые удары» давали scope corners+shots, а «Тотал угловых» —
    corners: угловой попадал ещё и в токен ударов по воротам. Рынок
    расходился по двум группам, и вилки по угловым между такими БК не
    находились вовсе."""
    assert len(_scopes("Тотал угловых", "Тотал угловых ударов",
                       "Фора по угловым", "Угловые")) == 1
    assert market_scope("Тотал угловых") == "corners"
    print("OK: test_corner_market_is_the_same_whatever_it_is_called")


def test_cards_market_is_the_same_whatever_it_is_called():
    """«Жёлтые карты» уходили в карты киберспорта (cards+maps), а «Жёлтые
    карточки» — в cards. Тотал ЖК — ходовой футбольный рынок, и все вилки
    по нему между такими БК терялись молча."""
    assert len(_scopes("Жёлтые карточки", "Желтые карты", "Тотал карточек",
                       "Тотал жёлтых карт")) == 1
    assert market_scope("Тотал карточек") == "cards"
    assert len(_scopes("Красные карточки", "Красные карты")) == 1
    assert market_scope("Красные карты") == "redcards"
    print("OK: test_cards_market_is_the_same_whatever_it_is_called")


def test_refined_market_is_not_merged_with_the_general_one():
    """Вытеснение общего токена не должно склеивать РАЗНЫЕ рынки: угловые
    — это не удары, карточки — не карты киберспорта, красные — не все
    карточки."""
    assert market_scope("Тотал угловых") != market_scope("Тотал ударов")
    assert market_scope("Тотал карточек") != market_scope("Тотал карт")
    assert market_scope("Тотал красных карточек") != \
        market_scope("Тотал карточек")
    assert market_scope("Тотал ударов в створ") != market_scope("Тотал ударов")
    print("OK: test_refined_market_is_not_merged_with_the_general_one")


def test_main_market_survives_every_wording():
    """Основной рынок — самый ходовой, и любое слово, которого нет в
    словаре служебных, уводило его в ЗАПАСНОЙ токен: «Тотал мячей», «Итог
    матча», «Тотал по очкам» переставали быть основным рынком и не
    сшивались уже НИ С ОДНОЙ БК."""
    assert _scopes(
        "Тотал", "Фора", "Исход", "Тотал голов", "Тотал мячей",
        "Тотал забитых мячей", "Количество мячей", "Фора по мячам",
        "Тотал по очкам", "Фора по очкам", "Тотал по баллам",
        "Фора по голам", "Итог матча", "Итоговый результат",
        "Тотал результативности", "Основной исход",
        "Исход основного времени") == {""}
    print("OK: test_main_market_survives_every_wording")


def test_individual_total_conceded_is_not_the_same_as_scored():
    """Обратная защита: «пропущенные» намеренно оставлены отдельным
    рынком — слить их с забитыми значило бы выдать ложную вилку."""
    assert market_scope("Тотал пропущенных мячей") != market_scope(
        "Тотал забитых мячей")
    print("OK: test_individual_total_conceded_is_not_the_same_as_scored")


def test_synonym_markets_meet_each_other():
    """Разные БК называют один рынок разными словами: «Нарушения» и
    «Фолы», «Вбрасывания» и «Вбросы», «Положение вне игры» и «Офсайды»."""
    assert len(_scopes("Тотал фолов", "Тотал нарушений")) == 1
    assert len(_scopes("Тотал аутов", "Тотал вбрасываний")) == 1
    assert len(_scopes("Офсайды", "Положение вне игры")) == 1
    assert len(_scopes("Тотал эйсов", "Тотал подач навылет")) == 1
    assert len(_scopes("Тотал башен", "Башни", "Вышки")) == 1
    print("OK: test_synonym_markets_meet_each_other")


def test_corner_total_arb_is_found_between_differently_worded_books():
    """Тот же баг целиком, от подписи БК до вилки."""
    from app.parsers.html_utils import market_scope as scope
    odds = [
        mk("Winline", f"total:{scope('Тотал угловых')}:9.5", 2.20, 1.90,
           outcome1="ТБ 9.5", outcome2="ТМ 9.5"),
        mk("BetBoom", f"total:{scope('Тотал угловых ударов')}:9.5",
           1.80, 2.30, outcome1="ТБ 9.5", outcome2="ТМ 9.5"),
    ]
    assert len(find_arbs(odds)) == 1, "вилка по угловым обязана находиться"
    print("OK: test_corner_total_arb_is_found_between_differently_worded_books")


def test_empty_scope_segment_does_not_split_a_total():
    """«total::2.5» и «total:2.5» — один и тот же тотал матча."""
    assert canon_market_key("total::2.5") == canon_market_key("total:2.5")
    assert canon_market_key("hcap:::-1.5") == canon_market_key("hcap:-1.5")
    assert canon_market_key("total:corners:9.5") == "total:corners:9.5"
    odds = [
        mk("Winline", "total::2.5", 2.20, 1.90, outcome1="ТБ", outcome2="ТМ"),
        mk("Fonbet", "total:2.5", 1.80, 2.30, outcome1="ТБ", outcome2="ТМ"),
    ]
    assert len(find_arbs(odds)) == 1
    print("OK: test_empty_scope_segment_does_not_split_a_total")


def test_prefilter_never_rejects_a_pair_difflib_would_accept():
    """Предфильтр по «паспорту» имени (длина + маска символов) обязан быть
    честным: он вправе пропустить лишнее, но не вправе отсечь пару, которую
    difflib признал бы достаточно похожей."""
    import difflib
    import random

    from app.arbitrage import _may_match, _name_sig
    from app.config import FUZZY_NAME_MIN_SINGLE_RATIO as floor

    rnd = random.Random(4242)
    alpha = "абвгдежзийклмнопрстуфхцчшщыэюя 0123456789abcdefghijklmnopqrstuvwxyz"
    for _ in range(20_000):
        a = "".join(rnd.choice(alpha) for _ in range(rnd.randint(1, 20)))
        b = list(a)
        for _ in range(rnd.randint(0, 4)):
            if not b:
                break
            i = rnd.randrange(len(b))
            roll = rnd.random()
            if roll < 0.34:
                b[i] = rnd.choice(alpha)
            elif roll < 0.67:
                b.pop(i)
            else:
                b.insert(i, rnd.choice(alpha))
        b = "".join(b)
        if not b:
            continue
        if difflib.SequenceMatcher(None, a, b).ratio() >= floor:
            assert _may_match(_name_sig(a), _name_sig(b)), (
                f"предфильтр отсёк годную пару: «{a}» / «{b}»")
    print("OK: test_prefilter_never_rejects_a_pair_difflib_would_accept")


if __name__ == "__main__":
    test_main_handicap_matches_across_key_forms()
    test_handicap_sides_stay_correct_across_key_forms()
    test_handicap_line_format_is_normalised()
    test_total_line_format_is_normalised()
    test_canon_leaves_other_markets_alone()
    test_different_handicap_lines_still_not_merged()
    test_period_handicap_not_merged_with_full_match()
    test_third_bookmaker_spelling_merges_when_two_others_agree()
    test_two_bookmakers_each_side_of_a_spelling_merge()
    test_same_single_bookmaker_spellings_still_not_merged()
    test_one_malformed_quote_does_not_wipe_out_every_arb()
    test_canon_fills_in_missing_key_segments()
    test_live_quotes_get_fuzzy_name_merging_too()
    test_live_quote_without_start_time_still_merges_names()
    test_prematch_without_start_time_is_still_left_alone()
    test_corner_market_is_the_same_whatever_it_is_called()
    test_cards_market_is_the_same_whatever_it_is_called()
    test_refined_market_is_not_merged_with_the_general_one()
    test_main_market_survives_every_wording()
    test_individual_total_conceded_is_not_the_same_as_scored()
    test_synonym_markets_meet_each_other()
    test_corner_total_arb_is_found_between_differently_worded_books()
    test_empty_scope_segment_does_not_split_a_total()
    test_prefilter_never_rejects_a_pair_difflib_would_accept()
    print("Все тесты прошли.")
