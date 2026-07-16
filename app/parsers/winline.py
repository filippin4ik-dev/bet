"""Winline — парсинг HTML линии (requests + BeautifulSoup).

ВНИМАНИЕ: разметка сайта периодически меняется и доступна только
с российских IP. CSS-селекторы ниже могут потребовать актуализации —
структура парсера при этом сохраняется. При пустом ответе можно
подключить selenium_helper.get_html_via_selenium.
"""
from bs4 import BeautifulSoup

from ..models import MatchOdds
from .base import BaseParser
from .selenium_helper import get_html_via_selenium

LINE_URL = "https://winline.ru/stavki/sport"


class WinlineParser(BaseParser):
    name = "Winline"

    def fetch_odds(self) -> list[MatchOdds]:
        html = self.get_html(LINE_URL, delay=True)
        odds = self._parse_html(html)
        if not odds:
            # Статики не хватило — пробуем отрендерить JS через Selenium
            html = get_html_via_selenium(LINE_URL)
            if html:
                odds = self._parse_html(html)
        return odds

    def _parse_html(self, html: str) -> list[MatchOdds]:
        soup = BeautifulSoup(html, "html.parser")
        result = []
        for event in soup.select("[data-event-id], .event-row, .sport-event"):
            teams = [t.get_text(strip=True)
                     for t in event.select(".team-name, .event-team")]
            coefs = [c.get_text(strip=True)
                     for c in event.select(".coefficient, .event-coef, .coef")]
            # Двухисходный рынок: ровно 2 команды и ровно 2 кэфа (без X)
            if len(teams) != 2 or len(coefs) != 2:
                continue
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
