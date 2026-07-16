"""Winline — динамический сайт (Angular SPA), парсится через Selenium.

Только ПРЕМАТЧ. Общая страница /stavki/sport показывает лишь верхушку
топ-событий (~10 матчей), поэтому обходим отдельные страницы двухисходных
видов спорта и прокручиваем ленту до конца — так собираются все матчи.

Структура DOM (проверена на живой странице):
- .event-card — карточка матча
- .body-left__names > .name — две команды
- .header-left__time — время начала («Завтра 09:00», «19.07 15:00») либо
  live-статус («1сет», «2Т 82'») — live-карточки пропускаем
- ww-feature-event-market-dsk — один рынок; кнопки .coefficient-button,
  тип по классу coefficient-button_generic2 (исход 1-2 без ничьей),
  _generic3 (1-X-2 с ничьей — пропускаем), _total2 (ТБ/ТМ),
  _handicap2 (фора — пропускаем). Линия тотала — в .coefficient-middle.

При смене вёрстки правьте селекторы ниже; логика поиска вилок не меняется.
"""
import time

from bs4 import BeautifulSoup

from ..config import SCROLL_SECONDS
from ..models import KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import SOUP_PARSER, num, parse_start_ts, scope_key
from .selenium_helper import SeleniumSession

# Страницы двухисходных видов спорта (прематч-линия)
PAGES = [
    ("https://winline.ru/stavki/sport/tennis/", "Теннис"),
    ("https://winline.ru/stavki/sport/nastolijnyj_tennis/", "Настольный теннис"),
    ("https://winline.ru/stavki/sport/basketbol/", "Баскетбол"),
    ("https://winline.ru/stavki/sport/volejbol/", "Волейбол"),
    ("https://winline.ru/stavki/sport/xokkej/", "Хоккей"),
    ("https://winline.ru/stavki/sport/kibersport/", "Киберспорт"),
]


class WinlineParser(BaseParser):
    name = "Winline"

    def fetch_odds(self) -> list[MarketOdds]:
        by_key: dict[str, MarketOdds] = {}
        with SeleniumSession() as s:
            if s.driver is None:
                return []
            for url, sport in PAGES:
                self._delay()
                snaps = s.render_snapshots(url, wait_seconds=20,
                                           scroll_seconds=SCROLL_SECONDS)
                for html in snaps:
                    for o in self._parse_html(html, sport):
                        by_key[o.match_key] = o  # дубли между снимками
        return list(by_key.values())

    def _parse_html(self, html: str, sport: str) -> list[MarketOdds]:
        soup = BeautifulSoup(html, SOUP_PARSER)
        result: list[MarketOdds] = []
        now = time.time()

        for card in soup.select(".event-card"):
            names = [n.get_text(strip=True)
                     for n in card.select(".body-left__names > .name")]
            if len(names) < 2:
                continue
            team1, team2 = names[0], names[1]

            time_el = card.select_one(".header-left__time")
            start_time = time_el.get_text(" ", strip=True) if time_el else None
            start_ts = parse_start_ts(start_time, now)
            # Не распознали время начала — это live-карточка («1сет»,
            # «2Т 82'», «Перерыв») или неизвестный формат. Пропускаем:
            # работаем только с прематчем.
            if start_ts is None or start_ts <= now:
                continue

            for row in card.select(".card__body"):
                # Метка строки: «Матч» — .match-row-label, дочерние росписи
                # («1 сет», «2 сет») — .period-name. Без метки периода кэфы
                # сета записались бы как кэфы всего матча!
                label_el = row.select_one(".match-row-label, .period-name")
                label = label_el.get_text(strip=True) if label_el else ""
                row_sport = sport if label in ("", "Матч") else f"{sport} · {label}"
                base = dict(bookmaker=self.name, sport=row_sport,
                            team1=team1, team2=team2, kind=KIND_PREMATCH,
                            start_time=start_time, start_ts=start_ts)
                for market in row.select("ww-feature-event-market-dsk"):
                    parsed = self._parse_market(market, base, label)
                    if parsed:
                        result.append(parsed)
        return result

    def _parse_market(self, market, base: dict, label: str) -> MarketOdds | None:
        btns = market.select(".coefficient-button")
        classes = {c for b in btns for c in b.get("class", [])}
        vals = [num(b.get_text(strip=True)) for b in btns]

        scope = "" if not label or label == "Матч" else scope_key(label)

        # Победитель без ничьей: ровно 2 исхода (generic2)
        if "coefficient-button_generic2" in classes and len(vals) == 2:
            k1, k2 = vals
            if k1 and k2:
                return MarketOdds(
                    market="Победитель" if not scope
                    else f"Победитель ({label})",
                    market_key=f"winner:{scope}" if scope else "winner",
                    outcome1="П1", outcome2="П2",
                    k1=k1, k2=k2, **base)

        # Тотал больше/меньше (total2) + линия из .coefficient-middle
        if "coefficient-button_total2" in classes and len(vals) == 2:
            mid = market.select_one(".coefficient-middle")
            pt = mid.get_text(strip=True) if mid else None
            over, under = vals
            if pt and over and under:
                key = f"total:{scope}:{pt}" if scope else f"total:{pt}"
                return MarketOdds(
                    market=f"Тотал {label} {pt}" if scope else f"Тотал {pt}",
                    market_key=key,
                    outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                    k1=over, k2=under, **base)
        return None
