"""Лига Ставок — парсинг HTML линии (requests + BeautifulSoup),
с запасным вариантом через Selenium.

ВНИМАНИЕ: селекторы могут потребовать актуализации под текущую вёрстку;
сайт доступен только с российских IP.
"""
from bs4 import BeautifulSoup

from ..models import KIND_LIVE, KIND_PREMATCH, MatchOdds
from .base import BaseParser
from .selenium_helper import get_html_via_selenium

# (URL, тип рынка) — между запросами выдерживается случайная пауза 2–5 c
PAGES = [
    ("https://www.ligastavok.ru/bets/live", KIND_LIVE),  # Live-линия
    ("https://www.ligastavok.ru/bets", KIND_PREMATCH),   # прематч-линия
]


class LigaStavokParser(BaseParser):
    name = "Liga Stavok"

    def fetch_odds(self) -> list[MatchOdds]:
        odds: list[MatchOdds] = []
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

    def _parse_html(self, html: str, kind: str) -> list[MatchOdds]:
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
            time_el = event.select_one(".bui-event-row__time, .event-time, time")
            result.append(MatchOdds(
                bookmaker=self.name, sport=sport,
                team1=teams[0], team2=teams[1], k1=k1, k2=k2,
                kind=kind,
                start_time=time_el.get_text(strip=True) if time_el else None,
            ))
        return result
