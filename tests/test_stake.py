"""Тесты парсера Stake (stake.com): разбор рынков Betradar из GraphQL и
поведение вокруг челленджа Cloudflare (без браузера и сети).

Запуск: python3 -m pytest tests/test_stake.py
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.arbitrage import find_arbs, is_crypto, same_market_maker  # noqa: E402
from app.models import KIND_LIVE, KIND_PREMATCH  # noqa: E402
from app.parsers import stake  # noqa: E402
from app.parsers.stake import StakeParser, _Challenge  # noqa: E402

NOW = 2_000_000_000.0
START = "2033-05-18T03:36:40+00:00"          # NOW + 200 c


def outcome(ext, odds, name="", active=True):
    return {"id": f"o{ext}", "extId": ext, "odds": odds, "name": name,
            "active": active}


def market(template, name, outcomes, specifiers=None, status="active"):
    return {"id": f"m-{template}-{specifiers}", "name": name,
            "status": status, "extId": f"{template}", "specifiers": specifiers,
            "templateExtId": template, "outcomes": outcomes}


def fixture(markets, *, start=START, teams=("Спартак", "Зенит"),
            outright=False, status="upcoming"):
    return {
        "id": "fx1", "slug": "spartak-zenit", "status": status,
        "provider": "betradar", "extId": "sr:match:1",
        "data": {
            "__typename": ("SportFixtureDataOutright" if outright
                           else "SportFixtureDataMatch"),
            "startTime": start, "isOutright": outright,
            "competitors": [{"name": teams[0]}, {"name": teams[1]}],
            "teams": [{"name": teams[0], "qualifier": "home"},
                      {"name": teams[1], "qualifier": "away"}],
        },
        "groups": [{"name": "all", "translation": "Все",
                    "templates": [{"name": "t", "extId": "t",
                                   "markets": markets}]}],
        "_tournament": {"name": "РПЛ", "slug": "premier-league",
                        "category": {"name": "Россия", "slug": "russia",
                                     "sport": {"name": "Футбол",
                                               "slug": "soccer"}}},
    }


@pytest.fixture
def parser(monkeypatch, tmp_path):
    monkeypatch.setattr(stake, "STAKE_SESSION_FILE",
                        str(tmp_path / "session.json"))
    monkeypatch.setattr(stake, "STAKE_PROXY", "")
    p = StakeParser()
    p._ua = "UA"
    p._cookies = {"cf_clearance": "abc"}
    return p


def by_key(odds):
    return {o.market_key: o for o in odds}


# ---------- разбор рынков ----------

def test_1x2_total_handicap_from_betradar_ids(parser):
    fx = fixture([
        market("1", "Исход", [outcome("1", 2.1), outcome("2", 3.4),
                              outcome("3", 3.6)]),
        market("18", "Тотал", [outcome("12", 1.9), outcome("13", 1.95)],
               "total=2.5"),
        market("16", "Фора", [outcome("1714", 2.05), outcome("1715", 1.8)],
               "hcp=-0.5"),
    ])
    got = by_key(parser._parse_fixture(fx, live=False, now=NOW))
    assert set(got) == {"winner1x2", "total:2.5", "hcap::-0.5"}
    w = got["winner1x2"]
    assert (w.k1, w.k3, w.k2) == (2.1, 3.4, 3.6)
    assert (w.outcome1, w.outcome3, w.outcome2) == ("П1", "X", "П2")
    assert w.team1 == "Спартак" and w.team2 == "Зенит"
    assert w.sport == "Футбол · РПЛ"
    assert w.kind == KIND_PREMATCH and w.start_ts == pytest.approx(NOW + 200)
    assert w.bookmaker == "Stake"
    assert w.url == "https://stake.com/ru/sports/soccer/russia/premier-league/spartak-zenit"
    t = got["total:2.5"]
    assert (t.outcome1, t.outcome2, t.k1, t.k2) == ("ТБ 2.5", "ТМ 2.5", 1.9, 1.95)
    h = got["hcap::-0.5"]
    assert (h.outcome1, h.outcome2) == ("Ф1 -0.5", "Ф2 +0.5")


def test_two_way_winner_and_draw_no_bet_are_different_markets(parser):
    fx = fixture([
        market("186", "Победитель", [outcome("4", 1.7), outcome("5", 2.2)]),
        market("11", "Ставка без ничьей", [outcome("4", 1.5),
                                           outcome("5", 2.6)]),
    ])
    got = by_key(parser._parse_fixture(fx, live=False, now=NOW))
    assert set(got) == {"winner", "winner_dnb"}
    assert got["winner"].k1 == 1.7 and got["winner_dnb"].k2 == 2.6


def test_team_total_both_score_odd_even(parser):
    fx = fixture([
        market("19", "Тотал (Спартак)", [outcome("12", 1.8), outcome("13", 2.0)],
               "total=1.5"),
        market("20", "Тотал (Зенит)", [outcome("12", 2.3), outcome("13", 1.6)],
               "total=1.5"),
        market("29", "Обе команды забьют", [outcome("74", 1.75),
                                            outcome("76", 2.05)]),
        market("26", "Чет/Нечет", [outcome("70", 1.9), outcome("72", 1.9)]),
    ])
    got = by_key(parser._parse_fixture(fx, live=False, now=NOW))
    assert "itotal:1::1.5" in got and "itotal:2::1.5" in got
    assert got["itotal:1::1.5"].market == "Тотал 1.5 (Спартак)"
    assert got["itotal:2::1.5"].k1 == 2.3
    assert got["bothscore"].outcome1 == "Да" and got["bothscore"].k1 == 1.75
    # исход 1 — «Чет» (Betradar 72), исход 2 — «Нечет» (70)
    assert got["oddeven"].outcome1 == "Чет" and got["oddeven"].k1 == 1.9


def test_period_scope_from_name_and_from_specifier(parser):
    fx = fixture([
        market("68", "Тотал — 1-й тайм", [outcome("12", 2.0), outcome("13", 1.8)],
               "total=0.5"),
        market("60", "Исход — 1-й тайм", [outcome("1", 2.9), outcome("2", 2.1),
                                          outcome("3", 3.9)]),
        # период не назван словами — только в спецификаторе Betradar
        market("204", "Тотал геймов", [outcome("12", 1.9), outcome("13", 1.9)],
               "setnr=2|total=9.5"),
    ])
    got = by_key(parser._parse_fixture(fx, live=False, now=NOW))
    assert "total:half1:0.5" in got
    assert "winner1x2:half1" in got
    assert got["winner1x2:half1"].market == "Исход (1X2) (half1)"
    set_keys = [k for k in got if k.startswith("total:") and "set2" in k]
    assert set_keys, got.keys()
    assert set_keys[0].endswith(":9.5")


def test_exotic_markets_with_the_same_outcome_ids_are_skipped(parser):
    fx = fixture([
        market("8", "Кто выиграет оставшуюся часть матча",
               [outcome("1", 2.0), outcome("2", 3.0), outcome("3", 3.0)]),
        market("10", "Двойной шанс", [outcome("9", 1.3), outcome("10", 1.2),
                                      outcome("11", 1.4)]),
        market("14", "Фора (3 исхода)", [outcome("1711", 2.0),
                                         outcome("1712", 3.0),
                                         outcome("1713", 3.0)], "hcp=1"),
        market("62", "Тотал — с 1-й по 15-ю минуту",
               [outcome("12", 3.0), outcome("13", 1.3)], "total=0.5"),
    ])
    assert parser._parse_fixture(fx, live=False, now=NOW) == []


def test_suspended_market_and_inactive_outcome_are_ignored(parser):
    fx = fixture([
        market("18", "Тотал", [outcome("12", 1.9), outcome("13", 1.95)],
               "total=2.5", status="suspended"),
        market("18", "Тотал", [outcome("12", 1.9),
                               outcome("13", 1.95, active=False)],
               "total=3.5"),
        market("18", "Тотал", [outcome("12", 1.0), outcome("13", 12.0)],
               "total=0.5"),
    ])
    assert parser._parse_fixture(fx, live=False, now=NOW) == []


def test_bad_1x2_margin_is_dropped(parser):
    fx = fixture([market("1", "Исход", [outcome("1", 1.1), outcome("2", 1.1),
                                        outcome("3", 1.1)])])
    assert parser._parse_fixture(fx, live=False, now=NOW) == []


def test_outrights_started_matches_and_live_kind(parser):
    total = market("18", "Тотал", [outcome("12", 1.9), outcome("13", 1.95)],
                   "total=2.5")
    assert parser._parse_fixture(fixture([total], outright=True),
                                 live=False, now=NOW) == []
    started = fixture([total], start="2033-05-18T03:20:00+00:00")
    assert parser._parse_fixture(started, live=False, now=NOW) == []
    live = parser._parse_fixture(started, live=True, now=NOW)
    assert len(live) == 1 and live[0].kind == KIND_LIVE


def test_same_market_appears_once(parser):
    total = market("18", "Тотал", [outcome("12", 1.9), outcome("13", 1.95)],
                   "total=2.5")
    fx = fixture([total, dict(total, id="dup")])
    assert len(parser._parse_fixture(fx, live=False, now=NOW)) == 1


def test_start_time_formats():
    assert stake._parse_time("2026-09-06T18:00:00Z") == 1788717600.0
    assert stake._parse_time("2026-09-06T18:00:00.000+00:00") == 1788717600.0
    assert stake._parse_time(1788717600) == 1788717600.0
    assert stake._parse_time(1788717600000) == 1788717600.0
    assert stake._parse_time("1788717600") == 1788717600.0
    assert stake._parse_time("завтра") is None
    assert stake._parse_time(None) is None


# ---------- Cloudflare ----------

class _Resp:
    def __init__(self, status, text="", headers=None, payload=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_cloudflare_challenge_is_recognised(parser, monkeypatch):
    monkeypatch.setattr(parser.session, "post", lambda *a, **k: _Resp(
        403, "<title>Just a moment...</title>", {"server": "cloudflare"}))
    with pytest.raises(_Challenge):
        parser._gql("{ sportList { slug } }")


def test_a_challenge_mid_run_is_solved_once_and_the_call_retried(parser, monkeypatch):
    calls = []

    def post(*a, **k):
        calls.append(k.get("json"))
        if len(calls) == 1:
            return _Resp(403, "Just a moment", {"cf-mitigated": "challenge"})
        return _Resp(200, payload={"data": {"sportList": [{"slug": "soccer"}]}})

    solved = []
    monkeypatch.setattr(parser.session, "post", post)
    monkeypatch.setattr(parser, "_solve", lambda force=False: solved.append(force) or True)
    data = parser._gql_retry("{ sportList { slug } }")
    assert data == {"sportList": [{"slug": "soccer"}]}
    assert solved == [True] and len(calls) == 2


def test_failed_solve_propagates_the_challenge(parser, monkeypatch):
    monkeypatch.setattr(parser.session, "post", lambda *a, **k: _Resp(
        403, "Just a moment", {"server": "cloudflare"}))
    monkeypatch.setattr(parser, "_solve", lambda force=False: False)
    with pytest.raises(_Challenge):
        parser._gql_retry("{ sportList { slug } }")


def test_solve_respects_the_cooldown(parser, monkeypatch):
    runs = []
    monkeypatch.setattr(parser, "_solve_in_browser",
                        lambda: runs.append(1) or False)
    parser._cookies = {}
    assert parser._solve() is False
    assert parser._solve() is False        # вторая попытка не гоняет браузер
    assert runs == [1]
    assert "следующая попытка" in parser.status_note
    parser._solve_after = 0.0
    assert parser._solve() is False
    assert runs == [1, 1]


def test_successful_solve_is_saved_and_reloaded(parser, monkeypatch, tmp_path):
    def solve():
        parser._ua = "Mozilla/5.0 real"
        parser._cookies = {"cf_clearance": "xyz", "__cf_bm": "1"}
        return True

    parser._cookies = {}
    monkeypatch.setattr(parser, "_solve_in_browser", solve)
    assert parser._solve() is True
    saved = json.load(open(stake.STAKE_SESSION_FILE, encoding="utf-8"))
    assert saved["ua"] == "Mozilla/5.0 real"
    assert saved["cookies"]["cf_clearance"] == "xyz"
    fresh = StakeParser()
    assert fresh._have_clearance()
    assert fresh._headers()["User-Agent"] == "Mozilla/5.0 real"


def test_no_clearance_and_no_solve_means_no_requests(parser, monkeypatch):
    parser._cookies = {}
    parser._solve_after = time.monotonic() + 100
    monkeypatch.setattr(parser.session, "post",
                        lambda *a, **k: pytest.fail("сеть не должна дёргаться"))
    assert parser.fetch_odds() == []


def test_prematch_type_falls_back_until_graphql_accepts(parser, monkeypatch):
    asked = []

    def gql(query, variables=None, timeout=None):
        asked.append(variables["type"])
        if variables["type"] == "upcoming":
            raise ValueError('GraphQL: Value "upcoming" does not exist in '
                             '"SportSearchEnum" enum.')
        return {"slugSport": {"tournamentList": []}}

    monkeypatch.setattr(parser, "_gql_retry", gql)
    assert parser._fixtures("soccer", live=False, deadline=time.monotonic() + 10) == []
    assert asked == ["upcoming", "all"]
    assert parser._prematch_type == "all"
    parser._fixtures("tennis", live=False, deadline=time.monotonic() + 10)
    assert asked[-1] == "all"           # запомнили, больше не перебираем


def test_fixtures_paginate_tournaments(parser, monkeypatch):
    monkeypatch.setattr(stake, "STAKE_TOURNAMENTS_PER_PAGE", 2)
    pages = {0: [{"fixtureList": [{"id": 1}]}, {"fixtureList": [{"id": 2}, {"id": 3}]}],
             2: [{"fixtureList": [{"id": 4}]}]}
    offsets = []

    def gql(query, variables=None, timeout=None):
        offsets.append(variables["to"])
        return {"slugSport": {"tournamentList": pages.get(variables["to"], [])}}

    monkeypatch.setattr(parser, "_gql_retry", gql)
    parser._prematch_type = "upcoming"
    got = parser._fixtures("soccer", live=False, deadline=time.monotonic() + 10)
    assert [f["id"] for f in got] == [1, 2, 3, 4]
    assert offsets == [0, 2]            # вторая страница неполная — стоп
    assert all("_tournament" in f for f in got)


def test_sports_filter_and_specials(parser, monkeypatch):
    monkeypatch.setattr(parser, "_gql_retry", lambda q, v=None, t=None: {
        "sportList": [{"slug": "soccer"}, {"slug": "politics-entertainment"},
                      {"slug": "tennis"}]})
    assert parser._sports() == ["soccer", "tennis"]
    monkeypatch.setattr(stake, "STAKE_SPORTS", ("tennis",))
    assert parser._sports() == ["tennis"]


# ---------- место в наборе БК ----------

def test_stake_is_crypto_and_pairs_with_every_other_crypto_site():
    assert is_crypto("Stake")
    for other in ("1win", "bc.game", "Roobet", "Rainbet", "500.casino"):
        assert not same_market_maker("Stake", other)
    from app.models import MarketOdds

    def total(bk, over, under):
        return MarketOdds(bookmaker=bk, sport="Футбол", team1="Спартак",
                          team2="Зенит", market="Тотал 2.5",
                          market_key="total:2.5", outcome1="ТБ 2.5",
                          outcome2="ТМ 2.5", k1=over, k2=under,
                          kind=KIND_PREMATCH, start_ts=NOW)

    assert len(find_arbs([total("Stake", 2.2, 1.7), total("1win", 1.7, 2.2)])) == 1
    assert find_arbs([total("Stake", 2.2, 1.7), total("Fonbet", 1.7, 2.2)]) == []
