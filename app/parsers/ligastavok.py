"""Лига Ставок — парсинг HTML линии (requests + BeautifulSoup),
с запасным вариантом через Selenium.

ВНИМАНИЕ: селекторы могут потребовать актуализации под текущую вёрстку;
сайт доступен только с российских IP.
"""
from bs4 import BeautifulSoup

from ..models import MatchOdds
from .base import BaseParser
from .selenium_helper import get_html_via_selenium

LINE_URL = "https://www.ligastavok.ru/bets"


class LigaStavokParser(BaseParser):
    name = "Liga Stavok"

    def fetch_odds(self) -> list[MatchOdds]:
        try:
            html = self.get_html(LINE_URL, delay=True)
        except Exception:  # noqa: BLE001
            html = None
        odds = self._parse_html(html) if html else []
        if not odds:
            html = get_html_via_selenium(LINE_URL)
            if html:
                odds = self._parse_html(html)
        return odds

    def _parse_html(self, html: str) -> list[MatchOdds]:
        soup = BeautifulSoup(html, "html.parser")
        result = []
        for event in soup.select("[itemtype*='SportsEvent'], .bui-event-row, .event"):
            teams = [t.get_text(strip=True) for t in event.select(
                ".bui-event-row__competitor, .competitor, .team")]
            coefs = [c.get_text(strip=True) for c in event.select(
                ".bui-outcome__value, .outcome-value, .rate")]
            if len(teams) != 2 or len(coefs) != 2:
                continue  # только двухисходные рынки (П1/П2 без X)
            try:
                k1, k2 = (float(c.replace(",", ".")) for c in coefs)
            except ValueError:
                continue
            sport_el = event.find_parent(attrs={"data-sport-name": True})
            sport = sport_el["data-sport-name"] if sport_el else "Спорт"
            result.append(MatchOdds(
                bookmaker=self.name, sport=sport,
                team1=teams[0], team2=teams[1], k1=k1, k2=k2,
            ))
        return result
