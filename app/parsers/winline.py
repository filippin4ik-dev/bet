"""Winline — парсинг HTML линии (requests + BeautifulSoup): Live + прематч.

ВНИМАНИЕ: разметка сайта периодически меняется и доступна только
с российских IP. CSS-селекторы ниже могут потребовать актуализации —
структура парсера при этом сохраняется. При пустом ответе можно
подключить selenium_helper.get_html_via_selenium.
"""
from bs4 import BeautifulSoup

from ..models import KIND_LIVE, KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import pair, parse_totals
from .selenium_helper import get_html_via_selenium

# (URL, тип рынка) — между запросами выдерживается случайная пауза 2–5 c
PAGES = [
    ("https://winline.ru/now", KIND_LIVE),            # Live-линия
    ("https://winline.ru/stavki/sport", KIND_PREMATCH),  # прематч-линия
]


class WinlineParser(BaseParser):
    name = "Winline"

    def fetch_odds(self) -> list[MarketOdds]:
        odds: list[MarketOdds] = []
        for url, kind in PAGES:
            try:
                html = self.get_html(url, delay=True)
            except Exception:  # noqa: BLE001
                html = None
            page_odds = self._parse_html(html, kind) if html else []
            if not page_odds:
                # Статики не хватило — пробуем отрендерить JS через Selenium
                html = get_html_via_selenium(url)
                if html:
                    page_odds = self._parse_html(html, kind)
            odds.extend(page_odds)
        return odds

    def _parse_html(self, html: str, kind: str) -> list[MarketOdds]:
        soup = BeautifulSoup(html, "html.parser")
        result = []
        for event in soup.select("[data-event-id], .event-row, .sport-event"):
            teams = [t.get_text(strip=True)
                     for t in event.select(".team-name, .event-team")]
            if len(teams) != 2:
                continue
            sport_el = event.find_parent(attrs={"data-sport-name": True})
            sport = sport_el["data-sport-name"] if sport_el else "Спорт"
            time_el = event.select_one(".event-time, .date, time")
            base = dict(
                bookmaker=self.name, sport=sport,
                team1=teams[0], team2=teams[1], kind=kind,
                start_time=time_el.get_text(strip=True) if time_el else None,
            )

            # Победитель (П1/П2)
            coefs = [c.get_text(strip=True)
                     for c in event.select(".coefficient, .event-coef, .coef")]
            k1, k2 = pair(coefs)
            if k1 and k2:
                result.append(MarketOdds(
                    market="Победитель", market_key="winner",
                    outcome1="П1", outcome2="П2", k1=k1, k2=k2, **base))

            # Тоталы больше/меньше (если размечены на карточке)
            result.extend(parse_totals(
                event, base, ".coefficient, .coef, .odd-value"))
        return result
