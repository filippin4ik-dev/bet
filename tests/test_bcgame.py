"""Тесты разбора рынков bc.game/BetBy (app/parsers/bcgame.py) — офлайн, на
синтетических словарях в формате реального фида (api/v4/prematch/… плюс
справочник api/v3/descriptions/…/markets/ru), без сети.

Главное, что здесь проверяется, — рынок «Исход 1X2»: номера его исходов
парсер берёт из справочника, а не угадывает, и похожие на вид рынки
(двойной шанс, точный счёт, фора с ничьей) тройкой П1/X/П2 не считает.
"""
import time
from dataclasses import replace

from app.arbitrage import find_arbs
from app.models import KIND_PREMATCH, MarketOdds
from app.parsers.bcgame import BCGameParser

NOW = time.time()
FUTURE = NOW + 3600


def _out(oid, name):
    return {"id": oid, "name": name}


def _desc(name, market_type, outcomes, specifiers=None):
    """Описание рынка в формате справочника BetBy."""
    return {
        "name": name,
        "market_type": market_type,
        "specifiers": specifiers or [],
        "variants": {"": [{"outcomes": outcomes}]},
    }


C1, DRAW, C2 = "{$competitor1}", "ничья", "{$competitor2}"

MARKETS = {
    # Исход 1X2 всего матча и первой половины
    "1": _desc("1x2", "Result", [_out("1", C1), _out("2", DRAW),
                                 _out("3", C2)]),
    "60": _desc("Первая половина - 1x2", "Result",
                [_out("1", C1), _out("2", DRAW), _out("3", C2)]),
    # Двойной шанс: три исхода, но каждый покрывает ДВА события
    "10": _desc("Двойной шанс", "Result",
                [_out("9", f"{C1} или ничья"), _out("10", f"{C1} или {C2}"),
                 _out("11", f"ничья или {C2}")]),
    # Точный счёт и фора: подписи те же, но с параметром — не исход матча
    "14": _desc("Фора {hcp}", "HandicapCorrectScore",
                [_out("1711", f"{C1} ({{hcp}})"), _out("1712", "ничья ({hcp})"),
                 _out("1713", f"{C2} ({{hcp}})")], ["hcp"]),
    # Досрочная выплата — считается по своим правилам, не сшивается
    "900001": _desc("1x2 (досрочная выплата)", "ResultEP",
                    [_out("1", C1), _out("2", DRAW), _out("3", C2)]),
    # В живом справочнике есть рынок, где обе крайние подписи указывают на
    # одну команду: разметку такого рынка понять нельзя
    "50120": _desc("4 Day Match", "Result",
                   [_out("1", C1), _out("2", DRAW), _out("3", C1)]),
    # Двухисходные рынки
    "186": _desc("Победитель", "Result", [_out("4", C1), _out("5", C2)]),
    "11": _desc("Ставка без ничьей", "Result",
                [_out("4", C1), _out("5", C2)]),
    "18": _desc("Тотал", "Total", [_out("12", "больше {total}"),
                                   _out("13", "меньше {total}")], ["total"]),
    "16": _desc("Фора", "Handicap", [_out("1714", f"{C1} ({{+hcp}})"),
                                     _out("1715", f"{C2} ({{-hcp}})")],
                ["hcp"]),
    "29": _desc("Обе команды забьют", "YesNo",
                [_out("74", "да"), _out("76", "нет")]),
}


def _k(**odds):
    return {oid: {"k": k} for oid, k in odds.items()}


def _event(markets, team1="Спартак", team2="Зенит", scheduled=None):
    return {
        "desc": {
            "type": "match", "sport": "1", "category": "10",
            "tournament": "100", "slug": "spartak-zenit",
            "scheduled": scheduled if scheduled is not None else FUTURE,
            "competitors": [{"name": team1}, {"name": team2}],
        },
        "markets": markets,
    }


def _parse(markets, **event_kwargs):
    p = BCGameParser()
    p._markets = MARKETS
    return p._parse_event(
        "555", _event(markets, **event_kwargs),
        {"1": {"name": "Футбол", "slug": "soccer"}},
        {"10": {"name": "Россия", "slug": "russia"}},
        {"100": {"name": "РПЛ", "slug": "rpl"}},
        False, NOW)


def _by_key(odds):
    return {o.market_key: o for o in odds}


# ---------- Исход 1X2 ----------

def test_1x2_outcome_ids_come_from_the_market_dictionary():
    odds = _by_key(_parse({"1": {"": _k(**{"1": 2.4, "2": 3.2, "3": 3.1})}}))
    o = odds["winner1x2"]
    assert (o.k1, o.k3, o.k2) == (2.4, 3.2, 3.1)
    assert (o.outcome1, o.outcome3, o.outcome2) == ("П1", "X", "П2")
    assert o.market == "Исход (1X2)"


def test_1x2_of_a_half_keeps_its_scope():
    odds = _by_key(_parse({"60": {"": _k(**{"1": 2.6, "2": 3.0, "3": 3.4})}}))
    assert "winner1x2:half1" in odds


def test_1x2_with_impossible_margin_is_dropped():
    """Маржа ниже единицы означает, что исходы перепутаны местами."""
    assert _parse({"1": {"": _k(**{"1": 5.0, "2": 5.0, "3": 5.0})}}) == []


def test_double_chance_is_not_a_1x2():
    assert _parse({"10": {"": _k(**{"9": 1.3, "10": 1.2, "11": 1.4})}}) == []


def test_handicap_with_a_draw_outcome_is_not_a_1x2():
    assert _parse({"14": {"hcp=1.5": _k(**{"1711": 1.9, "1712": 3.4,
                                           "1713": 3.1})}}) == []


def test_early_payout_1x2_is_not_matched_with_the_match_result():
    assert _parse({"900001": {"": _k(**{"1": 2.4, "2": 3.2,
                                        "3": 3.1})}}) == []


def test_a_market_naming_one_team_twice_is_dropped():
    assert _parse({"50120": {"": _k(**{"1": 2.4, "2": 3.2, "3": 3.1})}}) == []


# ---------- двухисходные рынки ----------

def test_draw_no_bet_is_its_own_market():
    """Ничья возвращает ставку — с обычным победителем такой рынок
    сшивать нельзя, иначе на ничьей одна нога вернётся, а вторая
    проиграет."""
    odds = _by_key(_parse({"11": {"": _k(**{"4": 1.5, "5": 2.7})}}))
    assert "winner_dnb" in odds
    assert "winner" not in odds
    assert odds["winner_dnb"].market == "Ставка без ничьей"


def test_plain_winner_stays_a_winner():
    odds = _by_key(_parse({"186": {"": _k(**{"4": 1.8, "5": 2.1})}}))
    assert odds["winner"].k1 == 1.8


def test_draw_no_bet_does_not_pair_with_a_plain_winner():
    """Ту же вилку движок нашёл бы, будь у рынка ключ обычного победителя:
    коэффициенты здесь заведомо вилочные. Проверяем именно то, что до
    движка отдельный ключ доходит целым — канон рынков сводит формы одного
    ключа к одной, и «winner_dnb» легко было бы срезать до «winner»."""
    dnb = _parse({"11": {"": _k(**{"4": 2.1, "5": 1.7})}})[0]
    rival = MarketOdds(
        bookmaker="Winline", sport="Футбол", team1="Спартак", team2="Зенит",
        market="Победитель", market_key="winner", outcome1="П1",
        outcome2="П2", k1=1.7, k2=2.1, kind=KIND_PREMATCH,
        start_ts=dnb.start_ts, start_time=dnb.start_time)
    assert dnb.market_key == "winner_dnb"
    assert find_arbs([dnb, rival]) == []
    # а между собой такие рынки сшиваются: вилка по ним настоящая
    twin = replace(rival, bookmaker="Fonbet", market_key="winner_dnb")
    assert len(find_arbs([dnb, twin])) == 1


def test_total_and_handicap():
    odds = _by_key(_parse({
        "18": {"total=2.5": _k(**{"12": 1.85, "13": 1.95})},
        "16": {"hcp=-1.5": _k(**{"1714": 2.3, "1715": 1.6})},
    }))
    assert odds["total:2.5"].outcome1 == "ТБ 2.5"
    assert odds["hcap::-1.5"].outcome2 == "Ф2 +1.5"


def test_both_teams_to_score():
    odds = _by_key(_parse({"29": {"": _k(**{"74": 1.7, "76": 2.1})}}))
    assert odds["bothscore"].outcome1 == "Да"


# ---------- общее ----------

def test_started_match_is_not_prematch():
    assert _parse({"1": {"": _k(**{"1": 2.4, "2": 3.2, "3": 3.1})}},
                  scheduled=NOW - 60) == []


def test_event_url_points_at_the_match_page():
    odds = _parse({"1": {"": _k(**{"1": 2.4, "2": 3.2, "3": 3.1})}})
    assert "bt-path" in odds[0].url
    assert "spartak-zenit-555" in odds[0].url
