"""Лига Ставок — парсинг HTML линии (requests + BeautifulSoup),
с запасным вариантом через Selenium.

ВНИМАНИЕ: селекторы могут потребовать актуализации под текущую вёрстку;
сайт доступен только с российских IP.
"""
from bs4 import BeautifulSoup

from ..models import KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import SOUP_PARSER, pair, parse_start_ts, parse_totals
from .selenium_helper import get_html_via_selenium

# Только прематч-линия (live отключён)
PAGES = [
    ("https://www.ligastavok.ru/bets", KIND_PREMATCH),
]


class LigaStavokParser(BaseParser):
    name = "Liga Stavok"

    def fetch_odds(self) -> list[MarketOdds]:
        odds: list[MarketOdds] = []
        for url, kind in PAGES:
            try:
                html = self.get_html(url, delay=True)
            except Exception:  # noqa: BLE001
                html = None
            page_odds = self._parse_html(html, kind) if html else []
            if not page_odds:
                html = get_html_via_selenium(url)
                if html:
                    page_odds = self._parse_html(html, kind)
            odds.extend(page_odds)
        return odds

    def _parse_html(self, html: str, kind: str) -> list[MarketOdds]:
        soup = BeautifulSoup(html, SOUP_PARSER)
        coef_sel = ".bui-outcome__value, .outcome-value, .rate"
        result = []
        for event in soup.select("[itemtype*='SportsEvent'], .bui-event-row, .event"):
            teams = [t.get_text(strip=True) for t in event.select(
                ".bui-event-row__competitor, .competitor, .team")]
            if len(teams) != 2:
                continue
            sport_el = event.find_parent(attrs={"data-sport-name": True})
            sport = sport_el["data-sport-name"] if sport_el else "Спорт"
            time_el = event.select_one(".bui-event-row__time, .event-time, time")
            start_time = time_el.get_text(strip=True) if time_el else None
            base = dict(
                bookmaker=self.name, sport=sport,
                team1=teams[0], team2=teams[1], kind=kind,
                start_time=start_time,
                start_ts=parse_start_ts(start_time),
            )
            coefs = [c.get_text(strip=True) for c in event.select(coef_sel)]
            k1, k2 = pair(coefs)
            if k1 and k2:
                result.append(MarketOdds(
                    market="Победитель", market_key="winner",
                    outcome1="П1", outcome2="П2", k1=k1, k2=k2, **base))
            result.extend(parse_totals(event, base, coef_sel))
        return result
