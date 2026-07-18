"""Лига Ставок — прематч-линия через Selenium (сайт за анти-ботом Qrator).

www.ligastavok.ru закрыт анти-ботом Qrator: любой запрос без специальной
cookie получает 401 со страницей JS-челленджа (proof-of-work + редирект).
Поэтому:

1. Страница рендерится общим headless-Chrome (SeleniumSession): браузер сам
   решает челлендж, после чего SPA дорисовывает линию. Ждём прохождения
   челленджа до LIGASTAVOK_CHALLENGE_WAIT секунд, затем прокручиваем ленту
   (ленивый список рисует только видимые матчи) и снимаем DOM на каждом шаге.
2. Полученные Qrator-cookie переносятся в requests-сессию: следующие циклы
   сначала пробуют быстрый HTTP-запрос и лишь при неудаче — браузер.

ВАЖНО: Qrator пропускает только «жилые» российские IP. С IP дата-центров
(в т.ч. зарубежных VPS) proof-of-work отклоняется (403 qrerror) даже в
настоящем Chrome — в этом случае Лига Ставок отдаст 0 котировок, о чём
парсер честно пишет в лог. Запускайте сканер на российском IP/прокси.

Селекторы вёрстки могут потребовать актуализации; парсер пробует несколько
поколений разметки и обобщённый разбор (пара команд + пара кэфов).
"""
import logging
import re

from bs4 import BeautifulSoup

from ..config import LIGASTAVOK_CHALLENGE_WAIT, SCROLL_SECONDS
from ..models import KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import (SOUP_PARSER, num, pair, parse_start_ts,
                         parse_totals)
from .selenium_helper import SeleniumSession

log = logging.getLogger("parsers.ligastavok")

# Только прематч-линия (live отключён)
PAGES = [
    ("https://www.ligastavok.ru/bets", KIND_PREMATCH),
]

# Признаки страницы-заглушки Qrator (челлендж не пройден)
_QRATOR_MARKERS = ("__qrator", "qauth", "qrerror", "пройдите проверку")

# Селекторы нескольких поколений вёрстки: строка события и внутри неё —
# команды, время, кэфы. Обобщённый вариант в конце.
_EVENT_SELECTORS = (
    "[itemtype*='SportsEvent'], .bui-event-row, .event, "
    "[class*='event-row'], [data-test*='event']"
)
_TEAM_SELECTORS = (
    ".bui-event-row__competitor, .competitor, .team, "
    "[class*='competitor'], [itemprop='name']"
)
_TIME_SELECTORS = (
    ".bui-event-row__time, .event-time, time, [class*='event-time'], "
    "[class*='date']"
)
_COEF_SELECTORS = (
    ".bui-outcome__value, .outcome-value, .rate, "
    "[class*='outcome'] [class*='value'], [class*='coef']"
)


class LigaStavokParser(BaseParser):
    name = "Liga Stavok"

    def __init__(self) -> None:
        super().__init__()
        self._qrator_warned = False
        self._have_cookies = False

    def fetch_odds(self) -> list[MarketOdds]:
        odds: list[MarketOdds] = []
        for url, kind in PAGES:
            # 1) быстрый путь: HTTP с ранее полученными Qrator-cookie
            page_odds: list[MarketOdds] = []
            if self._have_cookies:
                html = self._get_via_requests(url)
                if html:
                    page_odds = self._parse_html(html, kind)
            # 2) браузер: решает челлендж и дорисовывает SPA
            if not page_odds:
                page_odds = self._fetch_via_selenium(url, kind)
            odds.extend(page_odds)
        return odds

    # ---------- получение HTML ----------

    def _get_via_requests(self, url: str) -> str | None:
        try:
            html = self.get_html(url, delay=True)
        except Exception as exc:  # noqa: BLE001
            log.info("Liga Stavok: HTTP-запрос не прошёл (%s)", exc)
            return None
        if self._is_challenge(html):
            log.info("Liga Stavok: Qrator-cookie протухли — снова браузер")
            self._have_cookies = False
            return None
        return html

    def _fetch_via_selenium(self, url: str, kind: str) -> list[MarketOdds]:
        session = SeleniumSession()
        # Ждём дольше обычного: помимо рендеринга SPA сюда входит
        # прохождение Qrator-челленджа (PoW + перезагрузка страницы).
        snaps = session.render_snapshots(
            url, wait_seconds=LIGASTAVOK_CHALLENGE_WAIT,
            scroll_seconds=SCROLL_SECONDS)
        if not snaps:
            log.warning("Liga Stavok: браузер не отдал DOM — 0 котировок")
            return []
        if all(self._is_challenge(s) for s in snaps):
            if not self._qrator_warned:
                log.warning(
                    "Liga Stavok: анти-бот Qrator не пропускает этот IP "
                    "(челлендж не проходится даже в браузере). Сайт "
                    "доступен только с российских «жилых» IP — запустите "
                    "сканер на российском IP/прокси или отключите БК.")
                self._qrator_warned = True
            else:
                log.info("Liga Stavok: Qrator всё ещё блокирует — "
                         "0 котировок")
            return []
        # челлендж пройден — переносим cookie в requests для быстрых циклов
        self._steal_cookies(session)
        by_key: dict[str, MarketOdds] = {}
        for html in snaps:
            if self._is_challenge(html):
                continue
            for o in self._parse_html(html, kind):
                by_key[o.match_key] = o
        if not by_key:
            log.warning(
                "Liga Stavok: страница загрузилась, но события не "
                "распознаны (%d снимков DOM) — вероятно, изменилась "
                "вёрстка, нужны новые селекторы", len(snaps))
        return list(by_key.values())

    @staticmethod
    def _is_challenge(html: str | None) -> bool:
        if not html:
            return True
        low = html[:20000].lower()
        return any(m in low for m in _QRATOR_MARKERS)

    def _steal_cookies(self, session: SeleniumSession) -> None:
        """Копирует cookie браузера (включая Qrator) в requests-сессию."""
        try:
            driver = session.driver
            if driver is None:
                return
            for c in driver.get_cookies():
                self.session.cookies.set(
                    c.get("name"), c.get("value"),
                    domain=c.get("domain") or ".ligastavok.ru")
            self._have_cookies = True
        except Exception as exc:  # noqa: BLE001
            log.debug("Liga Stavok: не удалось забрать cookie: %s", exc)

    # ---------- разбор HTML ----------

    def _parse_html(self, html: str, kind: str) -> list[MarketOdds]:
        soup = BeautifulSoup(html, SOUP_PARSER)
        result: list[MarketOdds] = []
        for event in soup.select(_EVENT_SELECTORS):
            teams = [t.get_text(strip=True)
                     for t in event.select(_TEAM_SELECTORS)]
            teams = [t for t in teams if t]
            if len(teams) < 2:
                # обобщённый разбор: две команды из текста «А — Б»
                teams = self._teams_from_text(event.get_text(" ", strip=True))
            if len(teams) < 2 or teams[0] == teams[1]:
                continue
            team1, team2 = teams[0], teams[1]
            sport_el = event.find_parent(attrs={"data-sport-name": True})
            sport = sport_el["data-sport-name"] if sport_el else "Спорт"
            time_el = event.select_one(_TIME_SELECTORS)
            start_time = time_el.get_text(strip=True) if time_el else None
            start_ts = parse_start_ts(start_time)
            base = dict(
                bookmaker=self.name, sport=sport,
                team1=team1, team2=team2, kind=kind,
                start_time=start_time, start_ts=start_ts,
            )
            coefs = [c.get_text(strip=True)
                     for c in event.select(_COEF_SELECTORS)]
            coefs = [c for c in coefs if num(c)]
            # РОВНО два кэфа — «Победитель» без ничьей. Три кэфа — 1X2
            # (не двухисходный), больше — не разобрать без разметки рынков.
            k1, k2 = pair(coefs)
            if k1 and k2:
                result.append(MarketOdds(
                    market="Победитель", market_key="winner",
                    outcome1="П1", outcome2="П2", k1=k1, k2=k2, **base))
            result.extend(parse_totals(event, base, _COEF_SELECTORS))
        return result

    _VS_RE = re.compile(r"^(.{2,60}?)\s+[—–-]\s+(.{2,60}?)$")

    @classmethod
    def _teams_from_text(cls, text: str) -> list[str]:
        m = cls._VS_RE.match(text[:130])
        if m:
            return [m.group(1).strip(), m.group(2).strip()]
        return []
