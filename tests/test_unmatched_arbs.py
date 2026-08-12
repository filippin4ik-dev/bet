"""Тесты поиска вилок, которые НЕ дошли до перебора кэфов.

Вилка возможна, только если движок признал два события у разных БК одним
матчем. Сшивка идёт по паре имён и времени начала, и оба признака у БК
расходятся: «Сувон» против «Сувон Самсунг», старт 14:00 против 17:00.
Раньше такой кандидат исчезал молча — вилки по нему просто не было в
списке, и узнать о ней было неоткуда. Теперь пара проверяется
принудительной склейкой и, если вилка появляется, уходит на вкладку
«Отсеянные» с обоими написаниями.

Отдельно проверяется, что проход НЕ выдумывает вилки там, где событий
действительно два: разные пары команд с общим словом в названии, повтор
пары у ОДНОЙ БК, слишком далёкое время начала.

Запуск: python3 -m pytest tests/test_unmatched_arbs.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.arbitrage import (REJECT_UNMATCHED_NAME,  # noqa: E402
                           REJECT_UNMATCHED_TIME, _time_clusters,
                           build_name_canon_map, find_arbs)
from app.models import KIND_PREMATCH, MarketOdds  # noqa: E402
from app.unmatched import find_unmatched_arbs  # noqa: E402

NOW = 2_000_000_000.0
FOOTBALL = "Футбол · Корея. К-Лига"


def total(bookmaker, team1, team2, over, under, start_ts=NOW,
          sport=FOOTBALL, market_key="total:2.5"):
    return MarketOdds(
        bookmaker=bookmaker, sport=sport, team1=team1, team2=team2,
        market="Тотал 2.5", market_key=market_key,
        outcome1="ТБ 2.5", outcome2="ТМ 2.5", k1=over, k2=under,
        kind=KIND_PREMATCH, start_time="01.01 14:00", start_ts=start_ts,
        url=f"https://example.com/{bookmaker}")


def winner1x2(bookmaker, team1, team2, k1, kx, k2, start_ts=NOW):
    return MarketOdds(
        bookmaker=bookmaker, sport=FOOTBALL, team1=team1, team2=team2,
        market="Исход (1X2)", market_key="winner1x2",
        outcome1="П1", outcome2="П2", outcome3="X",
        k1=k1, k2=k2, k3=kx, kind=KIND_PREMATCH,
        start_time="01.01 14:00", start_ts=start_ts)


def search(odds):
    """Полный проход движка: обычные вилки + несшитые кандидаты."""
    name_map = build_name_canon_map(odds)
    splits = []
    clusters = _time_clusters(odds, name_map, splits=splits)
    arbs = find_arbs(odds, name_map, time_clusters=clusters)
    unmatched, unmatched3 = find_unmatched_arbs(odds, name_map, splits)
    return arbs, unmatched, unmatched3


# ---------- разные написания названий ----------

def test_arb_lost_to_different_spelling_is_reported():
    """«Сувон Самсунг» у одной БК и «Сувон» у другой — событие не
    склеилось, вилка по нему не нашлась. Она должна попасть в отсеянные."""
    odds = [
        total("Fonbet", "Сувон Самсунг", "Кимчхон Сангму", 2.30, 1.70),
        total("Winline", "Сувон", "Кимчхон Санму", 1.80, 2.25),
    ]
    arbs, unmatched, _ = search(odds)
    assert arbs == [], "события не склеились — обычная вилка невозможна"
    assert len(unmatched) == 1
    found = unmatched[0]
    assert found.reject_code == REJECT_UNMATCHED_NAME
    assert found.profit_pct > 0
    # в причине видны ОБА написания и обе БК: проверять руками нужно именно
    # то, что движок счёл одним матчем
    assert "Сувон Самсунг" in found.reject_reason
    assert "Сувон —" in found.reject_reason
    assert "Fonbet" in found.reject_reason
    assert "Winline" in found.reject_reason
    # плечи — из РАЗНЫХ БК, иначе это вилка внутри одного события
    assert {found.k1_bookmaker, found.k2_bookmaker} == {"Fonbet", "Winline"}


def test_three_outcome_arb_is_reported_too():
    """Тройка П1/X/П2 теряется от расхождения имён так же, как пара."""
    odds = [
        winner1x2("Fonbet", "Сувон Самсунг", "Кимчхон Сангму", 2.60, 3.30,
                  3.00),
        winner1x2("BetBoom", "Сувон", "Кимчхон Санму", 2.20, 3.90, 3.10),
    ]
    _, _, unmatched3 = search(odds)
    assert len(unmatched3) >= 1
    best = unmatched3[0]
    assert best.reject_code == REJECT_UNMATCHED_NAME
    assert best.kx_max and best.k1_max and best.k2_max


def test_no_arb_after_merge_means_no_row():
    """Похожие названия сами по себе — не повод для строки: показываем
    только то, из-за чего теряется ВИЛКА."""
    odds = [
        total("Fonbet", "Сувон Самсунг", "Кимчхон Сангму", 1.80, 1.70),
        total("Winline", "Сувон", "Кимчхон Санму", 1.75, 1.90),
    ]
    _, unmatched, unmatched3 = search(odds)
    assert unmatched == [] and unmatched3 == []


def test_different_teams_sharing_a_word_are_not_merged():
    """«Динамо Москва — Спартак Москва» и «Динамо Киев — Спартак Варна»
    похожи на три четверти, но матча тут два, а не один."""
    odds = [
        total("Fonbet", "Динамо Москва", "Спартак Москва", 2.30, 1.70),
        total("Winline", "Динамо Киев", "Спартак Варна", 1.80, 2.25),
    ]
    _, unmatched, _ = search(odds)
    assert unmatched == [], [a.reject_reason for a in unmatched]


def test_same_bookmaker_twice_is_not_a_spelling_problem():
    """Два написания у ОДНОЙ конторы — это два её матча, а не расхождение
    в имени: сшивать их значило бы свести цены разных событий."""
    odds = [
        total("Fonbet", "Сувон Самсунг", "Кимчхон Сангму", 2.30, 1.70),
        total("Fonbet", "Сувон", "Кимчхон Санму", 1.80, 2.25),
    ]
    _, unmatched, _ = search(odds)
    assert unmatched == []


def test_far_apart_events_are_not_candidates():
    """Похожие названия, но матчи через двое суток — это первый и
    ответный матчи, а не одно событие."""
    odds = [
        total("Fonbet", "Сувон Самсунг", "Кимчхон Сангму", 2.30, 1.70),
        total("Winline", "Сувон", "Кимчхон Санму", 1.80, 2.25,
              start_ts=NOW + 48 * 3600),
    ]
    _, unmatched, _ = search(odds)
    assert unmatched == []


def test_names_the_engine_merged_itself_are_not_reported_again():
    """Пары, которые движок склеил сам, — обычные вилки, и в отсеянных им
    делать нечего (иначе одна вилка показалась бы дважды)."""
    odds = [
        total("Fonbet", "Виктория Плзень", "Славия Прага", 2.30, 1.70),
        total("Winline", "Виктория Пльзень", "Славия Прага", 1.80, 2.25),
    ]
    arbs, unmatched, _ = search(odds)
    assert len(arbs) == 1, "написания близкие — движок сливает их сам"
    assert unmatched == []


# ---------- расхождение по времени начала ----------

def test_arb_lost_to_start_time_gap_is_reported():
    """Одинаковые названия, но старт разошёлся на три часа при допуске в
    час: событие разъехалось на два, вилка потерялась."""
    odds = [
        total("Fonbet", "Спартак", "Зенит", 2.30, 1.70, sport="Футбол · РПЛ"),
        total("BetBoom", "Спартак", "Зенит", 1.80, 2.25,
              sport="Футбол · РПЛ", start_ts=NOW + 3 * 3600),
    ]
    arbs, unmatched, _ = search(odds)
    assert arbs == []
    assert len(unmatched) == 1
    found = unmatched[0]
    assert found.reject_code == REJECT_UNMATCHED_TIME
    assert "Fonbet" in found.reject_reason and "BetBoom" in found.reject_reason
    assert "расхождение" in found.reject_reason


def test_repeat_fixture_of_one_bookmaker_is_not_reported():
    """Одна БК выставила пару дважды за вечер — это два матча, и склеивать
    их нельзя: получилась бы «вилка» из цен разных игр."""
    odds = [
        total("Fonbet", "Спартак", "Зенит", 2.30, 1.70, sport="Футбол · РПЛ"),
        total("Fonbet", "Спартак", "Зенит", 1.80, 2.25, sport="Футбол · РПЛ",
              start_ts=NOW + 3 * 3600),
    ]
    _, unmatched, _ = search(odds)
    assert unmatched == []


def test_time_gap_within_tolerance_is_a_normal_arb():
    """Расхождение внутри допуска сшивается как обычно — вилка обычная, а
    не отсеянная."""
    odds = [
        total("Fonbet", "Спартак", "Зенит", 2.30, 1.70, sport="Футбол · РПЛ"),
        total("BetBoom", "Спартак", "Зенит", 1.80, 2.25,
              sport="Футбол · РПЛ", start_ts=NOW + 600),
    ]
    arbs, unmatched, _ = search(odds)
    assert len(arbs) == 1
    assert unmatched == []


# ---------- служебное ----------

def test_pass_is_bounded_by_max_pairs():
    """Бюджет проверок ограничен: линия большая, а разбирать список всё
    равно приходится сверху вниз."""
    clubs = ["Ульсан", "Инчхон", "Тэгу", "Кванджу", "Соннам", "Чханвон",
             "Пхохан", "Йонъин", "Чхонан", "Кимпхо"]
    odds = []
    for i, club in enumerate(clubs):
        odds.append(total("Fonbet", f"{club} Хёндэ", f"{club} Моторс",
                          2.30, 1.70))
        odds.append(total("Winline", club, f"{club} Мотор", 1.80, 2.25))
    name_map = build_name_canon_map(odds)
    splits = []
    _time_clusters(odds, name_map, splits=splits)
    limited = find_unmatched_arbs(odds, name_map, splits, max_pairs=3)[0]
    full = find_unmatched_arbs(odds, name_map, splits)[0]
    assert 0 < len(limited) <= 3
    assert len(full) == len(clubs)


def test_odds_without_candidates_cost_nothing():
    """Линия без похожих названий не даёт ни одного кандидата."""
    odds = [
        total("Fonbet", "Спартак", "Зенит", 2.30, 1.70),
        total("Winline", "Спартак", "Зенит", 1.80, 2.25),
    ]
    arbs, unmatched, unmatched3 = search(odds)
    assert len(arbs) == 1
    assert unmatched == [] and unmatched3 == []
