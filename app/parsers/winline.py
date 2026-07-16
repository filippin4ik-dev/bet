"""Winline — динамический сайт (Angular SPA), парсится через Selenium.

Структура DOM (проверена на живой странице):
- .ww-feature-sport-list__item — блок вида спорта, заголовок в .sport-header__name
- .event-card — карточка матча
- .body-left__names > .name — две команды
- .header-left__time — время/статус матча
- ww-feature-event-market-dsk — один рынок; кнопки .coefficient-button,
  тип по классу coefficient-button_generic2 (исход 1-2 без ничьей),
  _generic3 (1-X-2 с ничьей — пропускаем), _total2 (ТБ/ТМ),
  _handicap2 (фора — пропускаем). Линия тотала — в .coefficient-middle.

При смене вёрстки правьте селекторы ниже; логика поиска вилок не меняется.
"""
from bs4 import BeautifulSoup

from ..models import KIND_LIVE, KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import num
from .selenium_helper import get_html_via_selenium

# (URL, тип рынка)
PAGES = [
    ("https://winline.ru/now", KIND_LIVE),
    ("https://winline.ru/stavki/sport", KIND_PREMATCH),
]


class WinlineParser(BaseParser):
    name = "Winline"

    def fetch_odds(self) -> list[MarketOdds]:
        odds: list[MarketOdds] = []
        for url, kind in PAGES:
            self._delay()
            html = get_html_via_selenium(url, wait_seconds=12)
            if html:
                odds.extend(self._parse_html(html, kind))
        return odds

    def _parse_html(self, html: str, kind: str) -> list[MarketOdds]:
        soup = BeautifulSoup(html, "html.parser")
        result: list[MarketOdds] = []

        for sport_block in soup.select(".ww-feature-sport-list__item"):
            sport_el = sport_block.select_one(".sport-header__name, .sport-header__title")
            sport = sport_el.get_text(strip=True) if sport_el else "Спорт"

            for card in sport_block.select(".event-card"):
                names = [n.get_text(strip=True)
                         for n in card.select(".body-left__names > .name")]
                if len(names) < 2:
                    continue
                team1, team2 = names[0], names[1]
                time_el = card.select_one(".header-left__time")
                start_time = time_el.get_text(strip=True) if time_el else None

                for row in card.select(".card__body"):
                    label_el = row.select_one(".match-row-label")
                    label = label_el.get_text(strip=True) if label_el else ""
                    row_sport = sport if label in ("", "Матч") else f"{sport} · {label}"
                    base = dict(bookmaker=self.name, sport=row_sport,
                                team1=team1, team2=team2, kind=kind,
                                start_time=start_time)
                    for market in row.select("ww-feature-event-market-dsk"):
                        parsed = self._parse_market(market, base, label)
                        if parsed:
                            result.append(parsed)
        return result

    def _parse_market(self, market, base: dict, label: str) -> MarketOdds | None:
        btns = market.select(".coefficient-button")
        classes = {c for b in btns for c in b.get("class", [])}
        vals = [num(b.get_text(strip=True)) for b in btns]

        # Победитель без ничьей: ровно 2 исхода (generic2)
        if "coefficient-button_generic2" in classes and len(vals) == 2:
            k1, k2 = vals
            if k1 and k2:
                key = "winner" if not label or label == "Матч" else f"winner:{label}"
                return MarketOdds(
                    market="Победитель" if not label or label == "Матч"
                    else f"Победитель ({label})",
                    market_key=key, outcome1="П1", outcome2="П2",
                    k1=k1, k2=k2, **base)

        # Тотал больше/меньше (total2) + линия из .coefficient-middle
        if "coefficient-button_total2" in classes and len(vals) == 2:
            mid = market.select_one(".coefficient-middle")
            pt = mid.get_text(strip=True) if mid else None
            over, under = vals
            if pt and over and under:
                scope = "" if not label or label == "Матч" else f" {label}"
                return MarketOdds(
                    market=f"Тотал{scope} {pt}",
                    market_key=f"total{scope}:{pt}",
                    outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                    k1=over, k2=under, **base)
        return None
