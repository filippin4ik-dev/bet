"""Тесты Roobet (зеркало roo916.com) — второй площадки на платформе BetBy.

Разбор фида у неё общий с bc.game (app/parsers/betby.py), поэтому здесь
проверяется не разбор рынков (это делает tests/test_bcgame.py), а ровно то,
чем площадки различаются, и главное правило их совместной работы:

  вилка между двумя площадками ОДНОЙ платформы — не вилка.

Линию им считает один и тот же поставщик, а расходятся кэфы только маржой
бренда. На живой линии из 16 740 общих двухисходных рынков лучшая пара
кэфов не дала прибыли ни разу, зато мгновенные рассинхроны двух снимков
одного фида давали до сотни «вилок» доходностью до 18 % — все ненастоящие:
обе ноги лежат в одной книге риска и режутся вместе.

Запуск: python3 -m pytest tests/test_roobet.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.arbitrage import (find_arbs, find_arbs_1x2,  # noqa: E402
                           same_market_maker)
from app.models import KIND_PREMATCH, MarketOdds  # noqa: E402
from app.parsers.bcgame import BCGameParser  # noqa: E402
from app.parsers.roobet import RoobetParser  # noqa: E402

NOW = 2_000_000_000.0


def total(bookmaker, over, under, team1="Спартак", team2="Зенит"):
    return MarketOdds(
        bookmaker=bookmaker, sport="Футбол · РПЛ", team1=team1, team2=team2,
        market="Тотал 2.5", market_key="total:2.5", outcome1="ТБ 2.5",
        outcome2="ТМ 2.5", k1=over, k2=under, kind=KIND_PREMATCH,
        start_time="01.01 20:00", start_ts=NOW)


def winner1x2(bookmaker, k1, kx, k2):
    return MarketOdds(
        bookmaker=bookmaker, sport="Футбол · РПЛ", team1="Спартак",
        team2="Зенит", market="Исход (1X2)", market_key="winner1x2",
        outcome1="П1", outcome2="П2", outcome3="X", k1=k1, k2=k2, k3=kx,
        kind=KIND_PREMATCH, start_time="01.01 20:00", start_ts=NOW)


# ---------- площадки одной платформы ----------

def test_two_betby_sites_are_one_market_maker():
    assert same_market_maker("bc.game", "Roobet")
    assert same_market_maker("Roobet", "bc.game")
    assert same_market_maker("BC.GAME", "roobet"), "регистр не важен"


def test_a_russian_bookmaker_is_nobodys_twin():
    assert not same_market_maker("bc.game", "Fonbet")
    assert not same_market_maker("Fonbet", "Winline")


def test_no_arb_between_sites_of_one_platform():
    """Арифметически вилка есть, но ставить по ней нельзя: это рассинхрон
    двух снимков ОДНОГО фида, а не расхождение двух контор."""
    odds = [total("bc.game", 2.30, 1.70), total("Roobet", 1.80, 2.25)]
    assert find_arbs(odds) == []


def test_no_1x2_arb_between_sites_of_one_platform():
    odds = [winner1x2("bc.game", 3.40, 3.60, 3.50),
            winner1x2("Roobet", 3.50, 3.70, 3.40)]
    assert find_arbs_1x2(odds) == []


def test_the_same_site_still_pairs_with_another_crypto_bookmaker():
    """Запрет касается только пары площадок платформы: против крипто-БК с
    другой платформы (1win) каждая из них работает как обычно — ради этого
    они и подключены."""
    odds = [total("Roobet", 2.30, 1.70), total("1win", 1.80, 2.25)]
    arbs = find_arbs(odds)
    assert len(arbs) == 1
    assert {arbs[0].k1_bookmaker, arbs[0].k2_bookmaker} == {"Roobet", "1win"}


def test_a_third_leg_outside_the_family_saves_a_1x2_arb():
    """В тройке 1X2 запрет срабатывает, только когда ВСЕ плечи из одной
    платформы: пара «Roobet + 1win» вилку по-прежнему даёт."""
    odds = [winner1x2("bc.game", 3.40, 3.60, 3.50),
            winner1x2("Roobet", 3.50, 3.70, 3.40),
            winner1x2("1win", 4.20, 4.30, 4.10)]
    arbs = find_arbs_1x2(odds)
    assert arbs, "вилка с участием сторонней БК должна остаться"
    for a in arbs:
        books = {a.k1_bookmaker, a.kx_bookmaker, a.k2_bookmaker}
        assert "1win" in books, ("вилка целиком внутри платформы не "
                                 "должна показываться")


# ---------- приметы площадки ----------

def test_roobet_reuses_the_shared_betby_parsing():
    """Разбор фида общий: у Roobet нет своей копии логики рынков."""
    assert RoobetParser._market is BCGameParser._market
    assert RoobetParser._parse_event is BCGameParser._parse_event


def test_the_two_sites_differ_where_they_should():
    bc, roo = BCGameParser(), RoobetParser()
    assert bc.name != roo.name
    assert bc.default_brand != roo.default_brand, "бренды разные"
    assert roo.sports_url.startswith("http")


def test_event_link_points_at_the_configured_mirror():
    """Ссылка на матч ведёт на сайт СВОЕЙ площадки, а не на bc.game."""
    roo = RoobetParser()
    desc = {"sport": "1", "category": "10", "tournament": "100",
            "slug": "spartak-zenit"}
    url = roo._event_url("555", desc, {"1": {"slug": "soccer"}},
                         {"10": {"slug": "russia"}}, {"100": {"slug": "rpl"}})
    assert url.startswith(roo.sports_url + "?")
    assert "bc.game" not in url
    assert "spartak-zenit-555" in url


def test_a_site_without_a_page_simply_has_no_link():
    """Площадка без известного адреса раздела не выдумывает ссылку: в
    таблице лучше пусто, чем кнопка в никуда."""
    class _Nameless(RoobetParser):
        sports_url = ""

    assert _Nameless()._event_url("555", {}, {}, {}, {}) is None


def test_brand_falls_back_to_config_when_the_site_is_silent():
    """Сайт Roobet закрыт Cloudflare — если brand_id узнать не вышло,
    берём значение из настроек, а не остаёмся без линии."""
    class _Offline(RoobetParser):
        def _discover_brand(self):
            raise RuntimeError("Cloudflare")

    p = _Offline()
    assert p._brand() == RoobetParser.default_brand


def test_discovered_brand_wins_over_the_default():
    class _Fresh(RoobetParser):
        def _discover_brand(self):
            return "1234567890"

    assert _Fresh()._brand() == "1234567890"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
