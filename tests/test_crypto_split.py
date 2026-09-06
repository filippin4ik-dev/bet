"""Тесты разделения крипто- и рублёвых БК и двух новых площадок BetBy.

Три вещи проверяются здесь.

1. Крипто-БК (bc.game, Roobet, 1win, Rainbet, 500.casino) не сшиваются в
   вилку с рублёвыми (Fonbet, Winline, …): деньги на них лежат в USDT, и
   «смешанную» вилку не поставить без перегона денег между валютами.
   Правило живёт в движке (same_money), поэтому смешанных вилок нет ни в
   основном списке, ни среди отсеянных, ни в истории. CRYPTO_SEPARATE=0
   возвращает старое поведение.

2. Rainbet и 500.casino — ещё две площадки на платформе BetBy: разбор у них
   общий с bc.game, между собой и с bc.game/Roobet они не сшиваются, а с
   1win — сшиваются.

3. Полная роспись события BetBy: парсер подменяет рынки события ответом
   ручки /event/<lang>/<id>, кэширует её по отпечатку верхушки и не
   спрашивает у событий без рынков.

Запуск: python3 -m pytest tests/test_crypto_split.py
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.arbitrage import (find_arbs, find_arbs_1x2, is_crypto,  # noqa: E402
                           same_market_maker, same_money)
from app.models import KIND_PREMATCH, MarketOdds  # noqa: E402
from app.parsers import betby  # noqa: E402
from app.parsers.bcgame import BCGameParser  # noqa: E402
from app.parsers.fivehundred import FiveHundredParser  # noqa: E402
from app.parsers.rainbet import RainbetParser  # noqa: E402
from app.parsers.roobet import RoobetParser  # noqa: E402

NOW = 2_000_000_000.0


def total(bookmaker, over, under):
    return MarketOdds(
        bookmaker=bookmaker, sport="Футбол · РПЛ", team1="Спартак",
        team2="Зенит", market="Тотал 2.5", market_key="total:2.5",
        outcome1="ТБ 2.5", outcome2="ТМ 2.5", k1=over, k2=under,
        kind=KIND_PREMATCH, start_time="01.01 20:00", start_ts=NOW)


def winner1x2(bookmaker, k1, kx, k2):
    return MarketOdds(
        bookmaker=bookmaker, sport="Футбол · РПЛ", team1="Спартак",
        team2="Зенит", market="Исход (1X2)", market_key="winner1x2",
        outcome1="П1", outcome2="П2", outcome3="X", k1=k1, k2=k2, k3=kx,
        kind=KIND_PREMATCH, start_time="01.01 20:00", start_ts=NOW)


def books(arb):
    return {b for b in (arb.k1_bookmaker, getattr(arb, "kx_bookmaker", None),
                        arb.k2_bookmaker) if b}


# ---------- кто крипто, кто рубли ----------

def test_all_five_crypto_sites_are_crypto():
    for bk in ("bc.game", "Roobet", "1win", "Rainbet", "500.casino",
               "Stake"):
        assert is_crypto(bk), bk
    for bk in ("Fonbet", "Winline", "BetBoom", "LeonBet", "Betcity"):
        assert not is_crypto(bk), bk


def test_same_money_splits_the_two_worlds():
    assert same_money("bc.game", "1win")
    assert same_money("Fonbet", "Winline", "Betcity")
    assert not same_money("bc.game", "Fonbet")
    assert not same_money("1win", "Winline", "Roobet")
    assert same_money("Rainbet"), "одна БК — не с кем смешиваться"


def test_separation_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "CRYPTO_SEPARATE", False)
    assert same_money("bc.game", "Fonbet")


# ---------- движок вилок ----------

def test_crypto_and_rouble_bookmakers_never_pair():
    """Кэфы заведомо вилочные, но пара «крипто + рубли» не показывается."""
    for crypto in ("bc.game", "Roobet", "1win", "Rainbet", "500.casino"):
        odds = [total(crypto, 2.30, 1.70), total("Fonbet", 1.80, 2.25)]
        assert find_arbs(odds) == [], crypto


def test_crypto_bookmakers_pair_among_themselves():
    odds = [total("1win", 2.30, 1.70), total("Rainbet", 1.80, 2.25)]
    arbs = find_arbs(odds)
    assert len(arbs) == 1
    assert books(arbs[0]) == {"1win", "Rainbet"}


def test_rouble_bookmakers_pair_among_themselves():
    odds = [total("Fonbet", 2.30, 1.70), total("Winline", 1.80, 2.25)]
    arbs = find_arbs(odds)
    assert len(arbs) == 1
    assert books(arbs[0]) == {"Fonbet", "Winline"}


def test_mixed_pool_finds_only_pure_pairs():
    """В общей куче котировок каждая вилка целиком в своём мире."""
    odds = [total("Fonbet", 2.30, 1.70), total("Winline", 1.80, 2.25),
            total("1win", 2.35, 1.72), total("bc.game", 1.82, 2.28)]
    arbs = find_arbs(odds)
    assert arbs, "вилки внутри каждого мира должны найтись"
    for a in arbs:
        kinds = {is_crypto(b) for b in books(a)}
        assert len(kinds) == 1, f"смешанная вилка: {books(a)}"
    found = {frozenset(books(a)) for a in arbs}
    assert frozenset({"Fonbet", "Winline"}) in found
    assert frozenset({"1win", "bc.game"}) in found


def test_1x2_triple_never_mixes_worlds():
    odds = [winner1x2("bc.game", 3.40, 3.60, 3.50),
            winner1x2("Fonbet", 4.20, 4.30, 4.10),
            winner1x2("1win", 3.90, 3.95, 3.85)]
    arbs = find_arbs_1x2(odds)
    assert arbs, "тройка bc.game + 1win должна дать вилку"
    for a in arbs:
        assert "Fonbet" not in books(a), books(a)
        assert books(a) <= {"bc.game", "1win"}


def test_switching_separation_off_restores_mixed_arbs(monkeypatch):
    monkeypatch.setattr(config, "CRYPTO_SEPARATE", False)
    odds = [total("bc.game", 2.30, 1.70), total("Fonbet", 1.80, 2.25)]
    assert len(find_arbs(odds)) == 1


# ---------- Rainbet и 500.casino: площадки BetBy ----------

def test_new_sites_share_the_betby_family():
    for a, b in (("Rainbet", "bc.game"), ("500.casino", "Roobet"),
                 ("Rainbet", "500.casino")):
        assert same_market_maker(a, b), (a, b)
    assert not same_market_maker("Rainbet", "1win")
    assert not same_market_maker("500.casino", "1win")


def test_new_sites_do_not_pair_with_their_platform_twins():
    for twin in ("bc.game", "Roobet", "500.casino"):
        odds = [total("Rainbet", 2.30, 1.70), total(twin, 1.80, 2.25)]
        assert find_arbs(odds) == [], twin


def test_new_sites_pair_with_1win():
    for site in ("Rainbet", "500.casino"):
        odds = [total(site, 2.30, 1.70), total("1win", 1.80, 2.25)]
        arbs = find_arbs(odds)
        assert len(arbs) == 1 and books(arbs[0]) == {site, "1win"}


def test_new_parsers_reuse_the_shared_parsing():
    for cls in (RainbetParser, FiveHundredParser):
        assert cls._market is BCGameParser._market
        assert cls._parse_event is BCGameParser._parse_event


def test_new_parsers_have_their_own_identity():
    seen_brands = set()
    for cls in (BCGameParser, RoobetParser, RainbetParser,
                FiveHundredParser):
        p = cls()
        assert p.default_brand and p.default_brand not in seen_brands
        seen_brands.add(p.default_brand)
        assert p.sports_url.startswith("http")
        assert p.name.lower() in config.CRYPTO_BOOKMAKERS
    assert RainbetParser().sports_url.startswith("https://rainbet.com")
    assert FiveHundredParser().sports_url.startswith("https://500.casino")


def test_500_brand_discovery_reads_boot_settings():
    class _Resp:
        headers = {"Content-Type": "application/json; charset=utf-8"}

        @staticmethod
        def json():
            return {"siteSettings": {"betbyBrandId": 2195256717934206976}}

    p = FiveHundredParser()
    p.session.get = lambda *a, **kw: _Resp()
    assert p._discover_brand() == "2195256717934206976"


def test_500_brand_discovery_ignores_cloudflare_html():
    class _Resp:
        headers = {"Content-Type": "text/html"}

        @staticmethod
        def json():
            raise AssertionError("HTML разбирать не должны")

    p = FiveHundredParser()
    p.session.get = lambda *a, **kw: _Resp()
    assert p._discover_brand() is None
    assert p._brand() == p.default_brand


# ---------- полная роспись события ----------

def _event(markets, scheduled=NOW + 3600):
    return {"desc": {"type": "match", "scheduled": scheduled,
                     "competitors": [{"name": "Спартак"},
                                     {"name": "Зенит"}]},
            "markets": markets}


def _k(**kv):
    return {oid: {"k": str(k)} for oid, k in kv.items()}


@pytest.fixture(autouse=True)
def _fast_and_unblocked(monkeypatch):
    """Тесты не ждут общий темп запросов и не наследуют блокировку."""
    monkeypatch.setattr(betby, "BETBY_FULL_MARKETS_RATE", 10_000.0)
    monkeypatch.setattr(betby, "BETBY_FULL_MARKETS_BURST", 10_000)
    monkeypatch.setattr(betby, "BETBY_FULL_MARKETS_PER_CYCLE", 1000)
    betby._THROTTLE.reset()
    yield
    betby._THROTTLE.reset()


def _parser_with_feed(full_by_event: dict, blocked: set | None = None):
    """Парсер с подменённым фидом: ручка события отдаёт full_by_event, а
    для id из blocked отвечает «access blocked»."""
    p = RainbetParser()
    calls = []

    def fake_event_markets(brand, section, eid):
        calls.append(f"api/v4/{section}/brand/{brand}/event/ru/{eid}")
        if blocked and eid in blocked:
            return None, True
        if eid in full_by_event:
            return full_by_event[eid], False
        return None, False

    p._event_markets = fake_event_markets
    return p, calls


def test_full_markets_replace_the_chunk_top(monkeypatch):
    monkeypatch.setattr("app.parsers.betby.BETBY_FULL_MARKETS_MAX", 100)
    top = {"18": {"total=2.5": _k(**{"12": 1.85, "13": 1.95})}}
    full = {"18": {"total=2.5": _k(**{"12": 1.80, "13": 2.00}),
                   "total=3.5": _k(**{"12": 2.60, "13": 1.48})},
            "16": {"hcp=-1.5": _k(**{"1714": 2.3, "1715": 1.6})}}
    p, calls = _parser_with_feed({"1": full})
    events = {"1": _event(top)}
    note = p._merge_full_markets("b", "prematch", events, False, NOW)
    assert "полная роспись у 1 из 1 событий" in note
    assert calls == ["api/v4/prematch/brand/b/event/ru/1"]
    m = events["1"]["markets"]
    # лестница и фора пришли из полной росписи…
    assert "total=3.5" in m["18"] and "16" in m
    # …а кэф верхушки — из чанка: он свежее
    assert m["18"]["total=2.5"]["12"]["k"] == "1.85"


def test_full_markets_are_cached_until_the_top_moves(monkeypatch):
    monkeypatch.setattr("app.parsers.betby.BETBY_FULL_MARKETS_MAX", 100)
    monkeypatch.setattr("app.parsers.betby.BETBY_FULL_MARKETS_REFRESH", 3600)
    top = {"18": {"total=2.5": _k(**{"12": 1.85, "13": 1.95})}}
    full = {"16": {"hcp=-1.5": _k(**{"1714": 2.3, "1715": 1.6})}}
    p, calls = _parser_with_feed({"1": full})
    p._merge_full_markets("b", "prematch", {"1": _event(top)}, False, NOW)
    events = {"1": _event(dict(top))}
    p._merge_full_markets("b", "prematch", events, False, NOW)
    assert len(calls) == 1, "верхушка не сдвинулась — запроса нет"
    assert "16" in events["1"]["markets"], "роспись пришла из кэша"
    moved = {"18": {"total=2.5": _k(**{"12": 1.90, "13": 1.90})}}
    p._merge_full_markets("b", "prematch", {"1": _event(moved)}, False, NOW)
    assert len(calls) == 2, "кэф верхушки сдвинулся — роспись запрошена"


def test_full_markets_cache_expires(monkeypatch):
    monkeypatch.setattr("app.parsers.betby.BETBY_FULL_MARKETS_MAX", 100)
    monkeypatch.setattr("app.parsers.betby.BETBY_FULL_MARKETS_REFRESH", 0.0)
    top = {"18": {"total=2.5": _k(**{"12": 1.85, "13": 1.95})}}
    p, calls = _parser_with_feed({"1": {}})
    p._merge_full_markets("b", "prematch", {"1": _event(top)}, False, NOW)
    time.sleep(0.01)
    p._merge_full_markets("b", "prematch", {"1": _event(top)}, False, NOW)
    assert len(calls) == 2


def test_events_without_markets_or_started_are_not_asked(monkeypatch):
    monkeypatch.setattr("app.parsers.betby.BETBY_FULL_MARKETS_MAX", 100)
    top = {"18": {"total=2.5": _k(**{"12": 1.85, "13": 1.95})}}
    p, calls = _parser_with_feed({})
    events = {"empty": _event({}),
              "started": _event(top, scheduled=NOW - 10),
              "outright": {"desc": {"type": "outright", "scheduled": NOW + 9,
                                    "competitors": []}, "markets": top}}
    p._merge_full_markets("b", "prematch", events, False, NOW)
    assert calls == []


def test_only_the_nearest_events_get_the_full_line(monkeypatch):
    monkeypatch.setattr("app.parsers.betby.BETBY_FULL_MARKETS_MAX", 2)
    top = {"18": {"total=2.5": _k(**{"12": 1.85, "13": 1.95})}}
    p, calls = _parser_with_feed({})
    events = {"late": _event(top, NOW + 9000),
              "soon": _event(top, NOW + 100),
              "mid": _event(top, NOW + 5000)}
    p._merge_full_markets("b", "prematch", events, False, NOW)
    assert [c.rsplit("/", 1)[-1] for c in calls] == ["soon", "mid"]


def test_failed_full_line_keeps_the_chunk_top(monkeypatch):
    monkeypatch.setattr("app.parsers.betby.BETBY_FULL_MARKETS_MAX", 100)
    top = {"18": {"total=2.5": _k(**{"12": 1.85, "13": 1.95})}}
    p, calls = _parser_with_feed({})   # ручка события молчит
    events = {"1": _event(top)}
    p._merge_full_markets("b", "prematch", events, False, NOW)
    assert events["1"]["markets"] == top


def test_a_cycle_takes_only_a_portion(monkeypatch):
    """За обход — не больше BETBY_FULL_MARKETS_PER_CYCLE запросов; остаток
    доберут следующие обходы, ближайшие события — первыми."""
    monkeypatch.setattr(betby, "BETBY_FULL_MARKETS_MAX", 100)
    monkeypatch.setattr(betby, "BETBY_FULL_MARKETS_PER_CYCLE", 2)
    top = {"18": {"total=2.5": _k(**{"12": 1.85, "13": 1.95})}}
    full = {"16": {"hcp=-1.5": _k(**{"1714": 2.3, "1715": 1.6})}}
    p, calls = _parser_with_feed({"a": full, "b": full, "c": full})
    events = {"c": _event(top, NOW + 300), "a": _event(top, NOW + 100),
              "b": _event(top, NOW + 200)}
    p._merge_full_markets("br", "prematch", events, False, NOW)
    assert [c.rsplit("/", 1)[-1] for c in calls] == ["a", "b"]
    assert "16" in events["a"]["markets"] and "16" not in events["c"]["markets"]
    # следующий обход — свежие события из чанка с той же верхушкой
    events = {"c": _event(top, NOW + 300), "a": _event(top, NOW + 100),
              "b": _event(top, NOW + 200)}
    p._merge_full_markets("br", "prematch", events, False, NOW)
    assert [c.rsplit("/", 1)[-1] for c in calls] == ["a", "b", "c"]


def test_access_blocked_pauses_every_betby_site(monkeypatch):
    """Ответ «access blocked» останавливает запросы росписи у ВСЕХ площадок
    (лимит у узла на адрес), а верхушка из чанка остаётся в линии."""
    monkeypatch.setattr(betby, "BETBY_FULL_MARKETS_MAX", 100)
    monkeypatch.setattr(betby, "BETBY_FULL_MARKETS_WORKERS", 1)
    top = {"18": {"total=2.5": _k(**{"12": 1.85, "13": 1.95})}}
    p, calls = _parser_with_feed({}, blocked={"a"})
    events = {"a": _event(top, NOW + 100), "b": _event(top, NOW + 200)}
    p._merge_full_markets("br", "prematch", events, False, NOW)
    assert [c.rsplit("/", 1)[-1] for c in calls] == ["a"], \
        "после блокировки второе событие не запрашивалось"
    assert events["a"]["markets"] == top
    assert betby._THROTTLE.blocked()
    # соседняя площадка в ту же паузу не ходит на ручку вовсе
    other, other_calls = _parser_with_feed({"z": {}})
    other._merge_full_markets("b2", "prematch", {"z": _event(top)}, False, NOW)
    assert other_calls == []


def test_live_goes_without_the_full_line_by_default():
    assert betby.BETBY_FULL_MARKETS_LIVE is False


def test_throttle_is_a_token_bucket(monkeypatch):
    """Ведро: BURST запросов уходят сразу, дальше — по RATE в секунду, а
    запрос, токен для которого не успеет появиться до дедлайна, не делается."""
    monkeypatch.setattr(betby, "BETBY_FULL_MARKETS_BURST", 3)
    monkeypatch.setattr(betby, "BETBY_FULL_MARKETS_RATE", 1000.0)
    th = betby._Throttle()
    far = time.monotonic() + 60
    assert all(th.acquire(far) for _ in range(3))
    assert th.tokens < 1
    # четвёртый ждёт пополнения (1 мс при 1000/с) — успевает
    assert th.acquire(far)
    monkeypatch.setattr(betby, "BETBY_FULL_MARKETS_RATE", 0.001)
    th.tokens = 0.0
    assert not th.acquire(time.monotonic() + 1), \
        "токен появится через ~1000 с — до дедлайна не успеть"
    th.block()
    assert th.blocked() and th.tokens == 0.0
    th.reset()
    assert not th.blocked() and th.tokens == 3


def test_full_markets_stop_at_the_cycle_deadline(monkeypatch):
    """Когда ведро пусто, обход не ждёт токены дольше своего бюджета."""
    monkeypatch.setattr(betby, "BETBY_FULL_MARKETS_MAX", 100)
    monkeypatch.setattr(betby, "BETBY_FULL_MARKETS_TIMEOUT", 0.05)
    monkeypatch.setattr(betby, "BETBY_FULL_MARKETS_RATE", 0.01)
    betby._THROTTLE.tokens = 1.0
    top = {"18": {"total=2.5": _k(**{"12": 1.85, "13": 1.95})}}
    full = {"16": {"hcp=-1.5": _k(**{"1714": 2.3, "1715": 1.6})}}
    p, calls = _parser_with_feed({"a": full, "b": full})
    events = {"a": _event(top, NOW + 100), "b": _event(top, NOW + 200)}
    t = time.monotonic()
    p._merge_full_markets("br", "prematch", events, False, NOW)
    assert time.monotonic() - t < 2
    assert [c.rsplit("/", 1)[-1] for c in calls] == ["a"]
    assert "16" in events["a"]["markets"] and events["b"]["markets"] == top
