"""Тесты 1win — спортивного раздела на платформе top-parser.

Разбор проверяется без сети: кадры websocket'а и ответ REST в фикстурах
сняты с живой линии (сентябрь 2026) и урезаны до нескольких групп
рынков. Главное, что здесь проверяется:

- имена команд берутся ПО-РУССКИ из исходов рынка результата (в REST они
  только английские, и с российскими БК такое событие не сойдётся);
- группа рынков распознаётся по своему устройству (renderType и коды
  исходов), а не по числовому id: исход 1X2, победитель, тотал, фора,
  индивидуальный тотал, обе забьют, чет/нечет;
- рынки с другой семантикой (двойной шанс, интервалы «с 1 по 10 минуту»,
  досрочный выигрыш, игроки) в вилку не идут;
- «1-й cет» с латинской «c» (так пишет платформа) даёт scope set1;
- дельты (match-odds) меняют кэфы уже полученного снимка;
- 1win — крипто-БК, и в вилку с bc.game/Roobet она сшивается (это не
  площадка BetBy).

Запуск: python3 -m pytest tests/test_onewin.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.arbitrage import find_arbs, same_market_maker  # noqa: E402
from app.models import KIND_PREMATCH, MarketOdds  # noqa: E402
from app.parsers.onewin import OneWinParser  # noqa: E402

NOW = 1_788_000_000.0   # линия снята в сентябре 2026, матчи — позже

MATCH_FOOTBALL = {
    "id": 39024179, "slug": "arsenal-vs-chelsea", "startAt": 1788708600,
    "homeTeam": {"id": 3, "name": "Arsenal", "position": 1},
    "awayTeam": {"id": 4, "name": "Chelsea", "position": 2},
    "sport": {"id": 18, "isEsport": False, "slug": "football",
              "sportType": "default"},
    "tournament": {"id": 919, "slug": "premier-league"},
    "category": {"id": 153, "slug": "england"},
}

MATCH_TENNIS = {
    "id": 39557688, "slug": "michael-zheng-vs-arthur-gea",
    "startAt": 1788640500,
    "homeTeam": {"id": 52553605, "name": "Michael Zheng", "position": 1},
    "awayTeam": {"id": 52813737, "name": "Arthur Gea", "position": 2},
    "sport": {"id": 33, "isEsport": False, "slug": "tennis",
              "sportType": "default"},
    "tournament": {"id": 41203, "slug": "men"},
}

MATCH_DOTA = {
    "id": 39617413, "slug": "klim-sani4-vs-pipsqueak-4",
    "startAt": 1788685200,
    "homeTeam": {"id": 1726086, "name": "Klim Sani4", "position": 1},
    "awayTeam": {"id": 1642375, "name": "Pipsqueak+4", "position": 2},
    "sport": {"id": 47, "isEsport": True, "slug": "dota-2",
              "sportType": "esport"},
    "tournament": {"id": 47036, "slug": "european-pro-league-masters"},
}


def _odd(oid, name, outcome, cf, v1=None):
    return {"id": oid, "name": name, "outcome": outcome, "cf": cf,
            "status": 1, "vars": {"v1": v1} if v1 is not None else None}


# Снимок футбольного матча — группы как в кадре match-odds-snapshot
FOOTBALL_GROUPS = [
    {"id": "6311", "isBase": True, "baseOrder": 0, "order": 0,
     "name": "Результат матча (основное время)", "renderType": "cols-3",
     "outcomes": ["1", "x", "2"],
     "oddsList": [_odd("r1", "Арсенал", "1", 1.71),
                  _odd("rx", "Ничья", "x", 3.94),
                  _odd("r2", "Челси", "2", 4.91)]},
    {"id": "6379", "isBase": True, "baseOrder": 2, "order": 2000,
     "name": "Тотал", "renderType": "total-2",
     "oddsList": [_odd("t1", "Больше 2.5", "over", 1.69, "2.5"),
                  _odd("t2", "Меньше 2.5", "under", 2.16, "2.5"),
                  _odd("t3", "Больше 3.5", "over", 2.68, "3.5"),
                  _odd("t4", "Меньше 3.5", "under", 1.47, "3.5"),
                  # линия без пары — в вилку не идёт
                  _odd("t5", "Больше 0.5", "over", 1.04, "0.5")]},
    {"id": "6344", "isBase": True, "baseOrder": 3, "order": 3000,
     "name": "Фора", "renderType": "fora-2",
     "oddsList": [_odd("f1", "Арсенал -1.5", "1", 2.79, "-1.5"),
                  _odd("f2", "Челси 1.5", "2", 1.43, "1.5"),
                  _odd("f3", "Арсенал -2", "1", 4.4, "-2"),
                  _odd("f4", "Челси 2", "2", 1.18, "2")]},
    {"id": "6263", "isBase": False, "order": 102000,
     "name": "1-й тайм. Результат", "renderType": "cols-3",
     "oddsList": [_odd("h2", "Челси", "2", 4.92),
                  _odd("h1", "Арсенал", "1", 2.22),
                  _odd("hx", "Ничья", "x", 2.38)]},
    {"id": "153087", "isBase": False, "order": 200000,
     "name": "Индивидуальный тотал Арсенал", "renderType": "total-2",
     "oddsList": [_odd("i1", "Больше 1.5", "over", 1.68, "1.5"),
                  _odd("i2", "Меньше 1.5", "under", 2.18, "1.5")]},
    {"id": "6280", "isBase": False, "order": 27000,
     "name": "Обе команды забьют", "renderType": "cols-2",
     "oddsList": [_odd("b1", "Нет", "no", 2.1), _odd("b2", "Да", "yes", 1.67)]},
    {"id": "6374", "isBase": False, "order": 22000,
     "name": "Нечет/Чет", "renderType": "cols-2",
     "oddsList": [_odd("o1", "Нечетное", "odd", 2.0),
                  _odd("o2", "Четное", "even", 1.85)]},
    {"id": "6483", "isBase": False, "order": 355000,
     "name": "Угловые. Тотал", "renderType": "total-2",
     "oddsList": [_odd("c1", "Больше 9.5", "over", 1.8, "9.5"),
                  _odd("c2", "Меньше 9.5", "under", 1.87, "9.5")]},
    # --- не сшиваются ---
    {"id": "6283", "isBase": False, "order": 15000,
     "name": "Двойной шанс ", "renderType": "cols-3",
     "oddsList": [_odd("d1", "Ничья Или Челси", "x2", 2.15),
                  _odd("d2", "Арсенал Или Ничья", "1x", 1.17),
                  _odd("d3", "Арсенал Или Челси", "12", 1.25)]},
    {"id": "70198", "isBase": False, "order": 100000,
     "name": "Результат с 1 по 10 минуту", "renderType": "cols-3",
     "oddsList": [_odd("m1", "Арсенал", "1", 6.0), _odd("mx", "Ничья", "x", 1.18),
                  _odd("m2", "Челси", "2", 10.0)]},
    {"id": "9001", "isBase": False, "order": 1000,
     "name": "Победитель (Досрочный выигрыш 2:0)", "renderType": "cols-2",
     "oddsList": [_odd("e1", "Арсенал", "1", 2.5), _odd("e2", "Челси", "2", 1.5)]},
    {"id": "9002", "isBase": False, "order": 400000,
     "name": "Игрок забьет", "renderType": "cols-2",
     "oddsList": [_odd("p1", "Сака", "yes", 2.5), _odd("p2", "Палмер", "yes", 3.0)]},
]

TENNIS_GROUPS = [
    {"id": "15813", "isBase": True, "baseOrder": 0, "order": 0,
     "name": "Победитель", "renderType": "cols-2", "outcomes": ["1", "2"],
     "oddsList": [_odd("w1", "Майкл Чжэн", "1", 1.7),
                  _odd("w2", "Артур Жеа", "2", 2.15)]},
    {"id": "16724", "isBase": True, "baseOrder": 1, "order": 50000,
     "name": "1-й cет. Победитель",   # латинская «c» — так у платформы
     "renderType": "cols-2", "outcomes": ["1", "2"],
     "oddsList": [_odd("s1", "Майкл Чжэн", "1", 1.69),
                  _odd("s2", "Артур Жеа", "2", 2.14)]},
    {"id": "16729", "isBase": True, "baseOrder": 2, "order": 60000,
     "name": "1-й cет. Тотал", "renderType": "total-2",
     "oddsList": [_odd("st1", "Больше 8.5", "over", 1.3, "8.5"),
                  _odd("st2", "Меньше 8.5", "under", 3.34, "8.5")]},
    {"id": "82640", "isBase": False, "order": 70000,
     "name": "Фора по сетам", "renderType": "fora-2",
     "oddsList": [_odd("fs1", "Майкл Чжэн -1.5", "1", 2.25, "-1.5"),
                  _odd("fs2", "Артур Жеа 1.5", "2", 1.56, "1.5")]},
]


def snapshot_frame(match_id, groups):
    return '42["u",' + json.dumps({
        "messageType": "match-odds-snapshot",
        "data": {"matchId": match_id, "oddsGroups": groups}}) + "]"


def delta_frame(match_id, groups):
    return '42["u",' + json.dumps({
        "messageType": "match-odds",
        "data": {"matchId": match_id, "oddsGroups": groups}}) + "]"


def parse(match, groups, now=NOW):
    snaps = {}
    OneWinParser._apply_frame(snapshot_frame(match["id"], groups), snaps)
    return OneWinParser()._parse_match(match, snaps[match["id"]],
                                       KIND_PREMATCH, now)


def by_key(odds):
    return {o.market_key: o for o in odds}


# ---------- имена и спорт ----------

def test_team_names_are_russian_from_the_result_market():
    odds = parse(MATCH_FOOTBALL, FOOTBALL_GROUPS)
    assert odds
    assert (odds[0].team1, odds[0].team2) == ("Арсенал", "Челси")


def test_english_rest_names_are_only_a_fallback():
    """Нет рынка результата — берём английские имена из списка матчей:
    лучше событие без сшивки, чем совсем без котировок."""
    groups = [g for g in FOOTBALL_GROUPS if g["renderType"] == "total-2"]
    odds = parse(MATCH_FOOTBALL, groups)
    assert odds
    assert (odds[0].team1, odds[0].team2) == ("Arsenal", "Chelsea")


def test_sport_label_matches_the_other_bookmakers():
    p = OneWinParser()
    assert p._sport(MATCH_FOOTBALL) == "Футбол · Premier League"
    assert p._sport(MATCH_TENNIS) == "Теннис · Men"
    assert p._sport(MATCH_DOTA) == \
        "Киберспорт · Dota 2. European Pro League Masters"


def test_event_url_points_at_the_site_mirror():
    p = OneWinParser()
    url = p._event_url(MATCH_FOOTBALL)
    assert url.startswith(config.ONEWIN_SITE_HOST + "/betting/match/sport/")
    assert url.endswith("arsenal-vs-chelsea-39024179")
    assert "/esport/" in p._event_url(MATCH_DOTA)


def test_a_match_that_already_started_is_not_prematch():
    assert parse(MATCH_FOOTBALL, FOOTBALL_GROUPS,
                 now=MATCH_FOOTBALL["startAt"] + 1) == []


# ---------- рынки ----------

def test_result_1x2_with_draw_leg():
    o = by_key(parse(MATCH_FOOTBALL, FOOTBALL_GROUPS))["winner1x2"]
    assert (o.k1, o.k3, o.k2) == (1.71, 3.94, 4.91)
    assert (o.outcome1, o.outcome3, o.outcome2) == ("П1", "X", "П2")
    assert o.market == "Исход (1X2)"


def test_first_half_result_gets_half1_scope():
    o = by_key(parse(MATCH_FOOTBALL, FOOTBALL_GROUPS))["winner1x2:half1"]
    assert (o.k1, o.k3, o.k2) == (2.22, 2.38, 4.92), \
        "исходы сшиваются по коду outcome, а не по порядку в списке"


def test_totals_pair_over_and_under_by_line():
    keys = by_key(parse(MATCH_FOOTBALL, FOOTBALL_GROUPS))
    assert (keys["total:2.5"].k1, keys["total:2.5"].k2) == (1.69, 2.16)
    assert keys["total:2.5"].outcome1 == "ТБ 2.5"
    assert (keys["total:3.5"].k1, keys["total:3.5"].k2) == (2.68, 1.47)
    assert "total:0.5" not in keys, "линия без второй стороны — не рынок"


def test_handicaps_pair_opposite_lines_of_the_two_teams():
    keys = by_key(parse(MATCH_FOOTBALL, FOOTBALL_GROUPS))
    o = keys["hcap::-1.5"]
    assert (o.k1, o.k2) == (2.79, 1.43)
    assert (o.outcome1, o.outcome2) == ("Ф1 -1.5", "Ф2 +1.5")
    assert keys["hcap::-2"].k2 == 1.18


def test_individual_total_knows_whose_it_is():
    o = by_key(parse(MATCH_FOOTBALL, FOOTBALL_GROUPS))["itotal:1::1.5"]
    assert (o.k1, o.k2) == (1.68, 2.18)
    assert o.market == "Тотал 1.5 (Арсенал)"


def test_both_to_score_and_odd_even():
    keys = by_key(parse(MATCH_FOOTBALL, FOOTBALL_GROUPS))
    assert (keys["bothscore"].k1, keys["bothscore"].k2) == (1.67, 2.1)
    assert keys["bothscore"].outcome1 == "Да"
    # первый исход — «Чет», как у остальных парсеров
    assert (keys["oddeven"].k1, keys["oddeven"].k2) == (1.85, 2.0)


def test_corners_total_gets_corners_scope():
    o = by_key(parse(MATCH_FOOTBALL, FOOTBALL_GROUPS))["total:corners:9.5"]
    assert (o.k1, o.k2) == (1.8, 1.87)


def test_markets_with_other_semantics_are_skipped():
    keys = by_key(parse(MATCH_FOOTBALL, FOOTBALL_GROUPS))
    # ни двойной шанс, ни первые 10 минут, ни досрочный выигрыш, ни
    # игроки не превратились в «победителя» или «исход»
    assert set(keys) == {
        "winner1x2", "winner1x2:half1", "total:2.5", "total:3.5",
        "hcap::-1.5", "hcap::-2", "itotal:1::1.5", "bothscore", "oddeven",
        "total:corners:9.5"}


def test_tennis_winner_and_set_markets_with_latin_c():
    keys = by_key(parse(MATCH_TENNIS, TENNIS_GROUPS))
    assert (keys["winner"].k1, keys["winner"].k2) == (1.7, 2.15)
    assert keys["winner"].team1 == "Майкл Чжэн"
    assert "winner:set1" in keys, "«1-й cет» с латинской c — это первый сет"
    assert (keys["total:set1:8.5"].k1, keys["total:set1:8.5"].k2) == (1.3, 3.34)
    assert (keys["hcap:sets:-1.5"].k1, keys["hcap:sets:-1.5"].k2) == (2.25, 1.56)


def test_suspended_or_unit_odds_are_ignored():
    groups = [{"id": "1", "isBase": True, "order": 0, "name": "Победитель",
               "renderType": "cols-2", "outcomes": ["1", "2"],
               "oddsList": [{**_odd("a", "Майкл Чжэн", "1", 1.7), "status": 0},
                            _odd("b", "Артур Жеа", "2", 2.15)]}]
    assert parse(MATCH_TENNIS, groups) == []


def test_overtime_variant_does_not_shadow_the_regular_time_market():
    """У хоккея «Тотал» и «Тотал (вкл. ОТ)» дают один ключ; остаётся
    основное время — так считают тотал остальные БК."""
    groups = [
        {"id": "1", "isBase": True, "order": 5000, "name": "Тотал (вкл. ОТ)",
         "renderType": "total-2",
         "oddsList": [_odd("a", "Больше 5.5", "over", 1.9, "5.5"),
                      _odd("b", "Меньше 5.5", "under", 1.9, "5.5")]},
        {"id": "2", "isBase": True, "order": 9000, "name": "Тотал",
         "renderType": "total-2",
         "oddsList": [_odd("c", "Больше 5.5", "over", 2.1, "5.5"),
                      _odd("d", "Меньше 5.5", "under", 1.75, "5.5")]},
    ]
    o = by_key(parse(MATCH_FOOTBALL, groups))["total:5.5"]
    assert (o.k1, o.k2) == (2.1, 1.75)


# ---------- кадры websocket'а ----------

def test_delta_frame_updates_odds_of_a_known_snapshot():
    snaps = {}
    OneWinParser._apply_frame(snapshot_frame(1, TENNIS_GROUPS), snaps)
    OneWinParser._apply_frame(delta_frame(1, [
        {"id": "15813", "oddsList": [{"id": "w1", "cf": 1.85}]}]), snaps)
    assert snaps[1]["15813"]["oddsList"]["w1"]["cf"] == 1.85
    assert snaps[1]["15813"]["oddsList"]["w1"]["name"] == "Майкл Чжэн"


def test_delta_for_an_unknown_match_is_ignored():
    snaps = {}
    OneWinParser._apply_frame(delta_frame(7, [
        {"id": "1", "oddsList": [{"id": "x", "cf": 2.0}]}]), snaps)
    assert snaps == {}


def test_garbage_frames_do_not_break_the_reader():
    snaps = {}
    for frame in ('42["u",{"messageType":"pong"}]', "42[", '42["u"]',
                  '42["u",{"messageType":"match-odds-snapshot","data":[]}]'):
        OneWinParser._apply_frame(frame, snaps)
    assert snaps == {}


def test_ws_url_carries_language_and_partner():
    url = OneWinParser()._ws_url()
    assert url.startswith(config.ONEWIN_WS_URL)
    assert f"Language={config.ONEWIN_LANG}" in url
    assert f"externalPartnerId={config.ONEWIN_PARTNER_ID}" in url
    assert "EIO=4" in url and "transport=websocket" in url


# ---------- место в наборе БК ----------

def test_onewin_is_a_crypto_bookmaker_and_not_a_betby_twin():
    assert "1win" in config.CRYPTO_BOOKMAKERS
    assert not same_market_maker("1win", "bc.game")
    assert not same_market_maker("1win", "Roobet")


def test_onewin_pairs_with_bcgame_into_an_arb():
    def total(bk, over, under):
        return MarketOdds(
            bookmaker=bk, sport="Футбол · РПЛ", team1="Спартак",
            team2="Зенит", market="Тотал 2.5", market_key="total:2.5",
            outcome1="ТБ 2.5", outcome2="ТМ 2.5", k1=over, k2=under,
            kind=KIND_PREMATCH, start_time="01.01 20:00", start_ts=NOW)
    arbs = find_arbs([total("bc.game", 2.30, 1.70), total("1win", 1.80, 2.25)])
    assert len(arbs) == 1
    assert {arbs[0].k1_bookmaker, arbs[0].k2_bookmaker} == {"bc.game", "1win"}


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
