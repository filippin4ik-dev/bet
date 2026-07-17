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
  _handicap2 (фора Ф1/Ф2). Линия тотала — в .coefficient-middle;
  у форы там же знаки сторон, напр. «- 2.5 +» (Ф1 −2.5 / Ф2 +2.5).

При смене вёрстки правьте селекторы ниже; логика поиска вилок не меняется.
"""
import logging
import re
import time

from bs4 import BeautifulSoup

from ..config import SCROLL_SECONDS
from ..models import KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import (SOUP_PARSER, fmt_hcap, fmt_total, market_scope, num,
                         parse_start_ts)
from .selenium_helper import SeleniumSession

log = logging.getLogger("parsers.winline")

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
                page_odds = self._fetch_page(s, url, sport)
                for o in page_odds:
                    by_key[o.match_key] = o  # дубли между снимками
        return list(by_key.values())

    def _fetch_page(self, s: SeleniumSession, url: str,
                    sport: str) -> list[MarketOdds]:
        """Рендерит страницу вида спорта; при пустом результате повторяет.

        Angular-SPA Winline подтягивает события своим websocket'ом уже после
        загрузки страницы. Иногда снимки DOM снимаются до того, как лента
        наполнилась, и вид спорта приходит пустым (гонка рендеринга). Если
        матчей не нашли вовсе — даём странице больше времени и пробуем ещё
        раз, прежде чем сдаться."""
        for attempt in range(2):
            self._delay()
            # на повторе ждём и прокручиваем дольше — лента точно наполнится
            wait = 20 if attempt == 0 else 30
            scroll = SCROLL_SECONDS if attempt == 0 else SCROLL_SECONDS + 15
            snaps = s.render_snapshots(url, wait_seconds=wait,
                                       scroll_seconds=scroll)
            odds: dict[str, MarketOdds] = {}
            for html in snaps:
                for o in self._parse_html(html, sport):
                    odds[o.match_key] = o
            if odds:
                return list(odds.values())
            log.info("Winline: %s — 0 матчей (попытка %d/2)%s",
                     url, attempt + 1,
                     ", повтор" if attempt == 0 else ", пропускаю")
        return []

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
                scope = "" if label in ("", "Матч") else market_scope(label)
                base = dict(bookmaker=self.name, sport=row_sport,
                            team1=team1, team2=team2, kind=KIND_PREMATCH,
                            start_time=start_time, start_ts=start_ts)
                for market in row.select("ww-feature-event-market-dsk"):
                    parsed = self._parse_market(market, base, label, scope)
                    if parsed:
                        result.append(parsed)
        return result

    def _parse_market(self, market, base: dict, label: str,
                      scope: str) -> MarketOdds | None:
        btns = market.select(".coefficient-button")
        classes = {c for b in btns for c in b.get("class", [])}
        vals = [num(b.get_text(strip=True)) for b in btns]

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

        # Тотал (total2) + линия из .coefficient-middle. ВАЖНО: колонки
        # Winline идут «М - Б» (заголовок sport-header), т.е. первая
        # кнопка — Меньше, вторая — Больше.
        if "coefficient-button_total2" in classes and len(vals) == 2:
            mid = market.select_one(".coefficient-middle")
            pt = fmt_total(mid.get_text(strip=True)) if mid else None
            under, over = vals
            if pt and over and under:
                key = f"total:{scope}:{pt}" if scope else f"total:{pt}"
                return MarketOdds(
                    market=f"Тотал {label} {pt}" if scope else f"Тотал {pt}",
                    market_key=key,
                    outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                    k1=over, k2=under, **base)

        # Фора (handicap2): столбцы Ф1 | Ф2 (первая кнопка — team1). Линия и
        # знаки сторон — в .coefficient-middle, напр. «- 2.5 +» → Ф1 −2.5,
        # Ф2 +2.5. Знаки читаем из текста (у фаворита бывает и «+ X -»).
        if "coefficient-button_handicap2" in classes and len(vals) == 2:
            mid = market.select_one(".coefficient-middle")
            mtext = mid.get_text(" ", strip=True) if mid else ""
            hcap = self._parse_hcap_middle(mtext)
            k1, k2 = vals
            if hcap and k1 and k2:
                h1, h2 = hcap
                key = f"hcap:{scope}:{h1}"
                return MarketOdds(
                    market=f"Фора {label} {h1}" if scope else f"Фора {h1}",
                    market_key=key,
                    outcome1=f"Ф1 {h1}", outcome2=f"Ф2 {h2}",
                    k1=k1, k2=k2, **base)
        return None

    @staticmethod
    def _parse_hcap_middle(text: str) -> tuple[str, str] | None:
        """«- 2.5 +» → ('-2.5', '+2.5'); «+ 1.5 -» → ('+1.5', '-1.5').

        Первый знак — сторона Ф1 (team1), второй — Ф2 (team2). Величина
        одна, стороны противоположны."""
        m = re.search(r"([+\-])\s*([\d.,]+)\s*([+\-])", text)
        if not m:
            return None
        s1, val, s2 = m.group(1), m.group(2), m.group(3)
        if s1 == s2:
            return None  # оба знака одинаковы — не распознали стороны
        h1 = fmt_hcap(val if s1 == "+" else "-" + val)
        h2 = fmt_hcap(val if s2 == "+" else "-" + val)
        return h1, h2
