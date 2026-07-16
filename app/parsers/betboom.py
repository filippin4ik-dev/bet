"""BetBoom — сайт полностью динамический (SPA), поэтому основной путь —
Selenium; сначала пробуем обычный requests на случай серверного рендера.

ВНИМАНИЕ: селекторы могут потребовать актуализации под текущую вёрстку;
сайт доступен только с российских IP.
"""
from bs4 import BeautifulSoup

from ..models import MatchOdds
from .base import BaseParser
from .selenium_helper import get_html_via_selenium

LINE_URL = "https://betboom.ru/sport"


class BetBoomParser(BaseParser):
    name = "BetBoom"

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
        for event in soup.select("[data-testid='event-card'], .event-card, .match-row"):
            teams = [t.get_text(strip=True) for t in event.select(
                "[data-testid='team-name'], .team-title, .competitor-name")]
            coefs = [c.get_text(strip=True) for c in event.select(
                "[data-testid='odd-button'], .odd-value, .factor-value")]
            if len(teams) != 2 or len(coefs) != 2:
                continue  # берём только двухисходные рынки (П1/П2 без X)
            try:
                k1, k2 = (float(c.replace(",", ".")) for c in coefs)
            except ValueError:
                continue
            sport_el = event.find_parent(attrs={"data-sport": True})
            sport = sport_el["data-sport"] if sport_el else "Спорт"
            result.append(MatchOdds(
                bookmaker=self.name, sport=sport,
                team1=teams[0], team2=teams[1], k1=k1, k2=k2,
            ))
        return result
