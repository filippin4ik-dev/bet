"""BetBoom — сайт полностью динамический (SPA), поэтому основной путь —
Selenium; сначала пробуем обычный requests на случай серверного рендера.

ВНИМАНИЕ: селекторы могут потребовать актуализации под текущую вёрстку;
сайт доступен только с российских IP.
"""
from bs4 import BeautifulSoup

from ..models import KIND_LIVE, KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import pair, parse_totals
from .selenium_helper import get_html_via_selenium

# (URL, тип рынка) — между запросами выдерживается случайная пауза 2–5 c
PAGES = [
    ("https://betboom.ru/sport/live", KIND_LIVE),   # Live-линия
    ("https://betboom.ru/sport", KIND_PREMATCH),    # прематч-линия
]


class BetBoomParser(BaseParser):
    name = "BetBoom"

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
        soup = BeautifulSoup(html, "html.parser")
        coef_sel = "[data-testid='odd-button'], .odd-value, .factor-value"
        result = []
        for event in soup.select("[data-testid='event-card'], .event-card, .match-row"):
            teams = [t.get_text(strip=True) for t in event.select(
                "[data-testid='team-name'], .team-title, .competitor-name")]
            if len(teams) != 2:
                continue
            sport_el = event.find_parent(attrs={"data-sport": True})
            sport = sport_el["data-sport"] if sport_el else "Спорт"
            time_el = event.select_one(
                "[data-testid='event-time'], .event-time, time")
            base = dict(
                bookmaker=self.name, sport=sport,
                team1=teams[0], team2=teams[1], kind=kind,
                start_time=time_el.get_text(strip=True) if time_el else None,
            )
            coefs = [c.get_text(strip=True) for c in event.select(coef_sel)]
            k1, k2 = pair(coefs)
            if k1 and k2:
                result.append(MarketOdds(
                    market="Победитель", market_key="winner",
                    outcome1="П1", outcome2="П2", k1=k1, k2=k2, **base))
            result.extend(parse_totals(event, base, coef_sel))
        return result
