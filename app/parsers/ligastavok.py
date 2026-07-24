"""Лига Ставок — прематч/лайв через внутренний JSON-API (за анти-ботом Qrator).

www.ligastavok.ru закрыт анти-ботом Qrator: любой запрос без специальной
cookie получает 401 со страницей JS-челленджа (proof-of-work + редирект).
Поэтому парсер работает в два эшелона:

1. БЫСТРЫЙ ПУТЬ — внутренний JSON-API линии
   (`lds-api-sites.ligastavok.ru`, `POST /rest/events/v8/eventsList`): тот
   же API, что дергает мобильный сайт. Отдаёт структурированные события с
   основными рынками (1X2/победитель, тотал, фора) — это надёжнее и
   быстрее, чем парсить HTML. Требует пройденной Qrator-cookie.
2. Cookie добывает общий headless-Chrome (SeleniumSession): браузер сам
   решает Qrator-челлендж, после чего его cookie переносятся в
   requests-сессию, и все следующие циклы ходят прямо в API. Если API
   почему-то не отдаёт линию — парсер откатывается к разбору HTML линии
   из уже отрендеренного браузером DOM.

ВАЖНО: Qrator пропускает только «жилые» российские IP. С IP дата-центров
(в т.ч. зарубежных VPS) и API, и челлендж отклоняются (401/403) даже в
настоящем Chrome — тогда Лига Ставок отдаст 0 котировок, о чём парсер
честно пишет в лог. Запускайте сканер на российском IP/прокси.
"""
import logging
import re
import time
import uuid

from bs4 import BeautifulSoup

from ..config import (LIGASTAVOK_API_HOST, LIGASTAVOK_API_MAX_PAGES,
                      LIGASTAVOK_API_PAGE, LIGASTAVOK_CHALLENGE_WAIT,
                      LIGASTAVOK_PROXY, SCROLL_SECONDS)
from ..models import KIND_LIVE, KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import (SOUP_PARSER, fmt_hcap, fmt_total, format_start,
                         neg_hcap, num, pair, parse_start_ts, parse_totals)
from .selenium_helper import SeleniumSession

log = logging.getLogger("parsers.ligastavok")

# Страница для прогрева Qrator-cookie в браузере (после неё быстрый API).
WARMUP_URL = "https://www.ligastavok.ru/bets"

# Признаки страницы-заглушки Qrator (челлендж не пройден)
_QRATOR_MARKERS = ("__qrator", "qauth", "qrerror", "пройдите проверку")

# ---- JSON-API ----
# Мобильный API отдаёт основные рынки события (proposedTypes=MAINOFFER):
# 1X2/победитель, основной тотал, основная фора. Схема сверена с рабочим
# захватом сетевого трафика мобильного сайта.
_API_UA = ("Mozilla/5.0 (Linux; Android 13; SM-G998B) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.6367.119 Mobile Safari/537.36")
_EVENTS_LIST = "/rest/events/v8/eventsList"

# Подписи исходов в API (title): тотал больше/меньше, фора.
_OVER_TITLES = {"бол", "больше", "тб", "б", "over", "over "}
_UNDER_TITLES = {"мен", "меньше", "тм", "м", "under"}


class LigaStavokParser(BaseParser):
    name = "Liga Stavok"

    def __init__(self) -> None:
        super().__init__()
        self._qrator_warned = False
        self._have_cookies = False
        if LIGASTAVOK_PROXY:
            # Все HTTP-запросы Лиги Ставок — через резидентный прокси
            # (сам сайт и его API; остальных БК это не касается).
            self.session.proxies.update({"http": LIGASTAVOK_PROXY,
                                         "https": LIGASTAVOK_PROXY})
            log.info("Liga Stavok: HTTP-запросы идут через прокси")

    # ---------- публичный интерфейс ----------

    def fetch_odds(self) -> list[MarketOdds]:
        # Только прематч: лайв Лиги Ставок пришлось бы качать циклом
        # с прогревом браузера — слишком тяжело для быстрого лайв-цикла.
        return self._fetch(live=False)

    def _fetch(self, live: bool) -> list[MarketOdds]:
        kind = KIND_LIVE if live else KIND_PREMATCH
        # 1) быстрый путь: JSON-API. Пробуем ДАЖЕ без Qrator-cookie:
        # с «жилого» российского IP (или через резидентный прокси,
        # LIGASTAVOK_PROXY) мобильный API отвечает и так — тогда браузер
        # вообще не нужен.
        odds = self._fetch_via_api(kind)
        if odds:
            return odds
        if self._have_cookies:
            log.info("Liga Stavok: API не отдал линию с текущими cookie — "
                     "обновляю через браузер")
            self._have_cookies = False

        # 2) браузер решает Qrator-челлендж и отдаёт cookie (+ DOM про запас)
        snaps = self._warmup_cookies()
        if self._have_cookies:
            odds = self._fetch_via_api(kind)
            if odds:
                return odds

        # 3) запасной путь: разбор HTML линии из отрендеренного DOM (прематч)
        if not live and snaps:
            odds = self._parse_snaps(snaps, KIND_PREMATCH)
            if odds:
                return odds
            log.warning(
                "Liga Stavok: страница загрузилась, но ни API, ни HTML не "
                "дали событий (%s) — вероятно, IP не «жилой» российский "
                "(Qrator отдаёт заглушку) либо сменились API/вёрстка. "
                "Поможет резидентный прокси: LIGASTAVOK_PROXY (см. README).",
                self._page_hint(snaps[-1]))
        return []

    @staticmethod
    def _page_hint(html: str) -> str:
        """Короткая подсказка, ЧТО за страницу отдал сайт (для логов):
        заголовок + признак заглушки «доступ ограничен»."""
        m = re.search(r"<title[^>]*>(.*?)</title>", html,
                      re.IGNORECASE | re.DOTALL)
        title = " ".join(m.group(1).split())[:80] if m else "без <title>"
        low = html.lower()
        blocked = any(w in low for w in ("error-img", "block-info",
                                         "доступ ограничен", "access denied"))
        return f"страница: «{title}»" + (", похоже на заглушку блокировки"
                                         if blocked else "")

    # ---------- JSON-API ----------

    def _api_headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "x-application-name": "mobile",
            "x-req-id": str(uuid.uuid4()),
            "User-Agent": _API_UA,
            "Origin": "https://www.ligastavok.ru",
            "Referer": "https://www.ligastavok.ru/",
        }

    def _api_post(self, path: str, payload: dict) -> dict | None:
        """POST к внутреннему API. None, если Qrator/сеть не пропустили."""
        url = f"{LIGASTAVOK_API_HOST}{path}"
        try:
            resp = self.session.post(url, headers=self._api_headers(),
                                     json=payload, timeout=15)
        except Exception as exc:  # noqa: BLE001
            log.info("Liga Stavok: запрос %s не прошёл (%s)", path, exc)
            return None
        ctype = resp.headers.get("content-type", "")
        if resp.status_code != 200 or "json" not in ctype.lower():
            # Скорее всего Qrator вернул HTML-челлендж (401/403) — cookie
            # протухли или IP не проходит проверку.
            log.info("Liga Stavok: API %s -> %s (%s) — Qrator/ошибка",
                     path, resp.status_code, ctype.split(";")[0])
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def _fetch_via_api(self, kind: str) -> list[MarketOdds]:
        want_live = kind == KIND_LIVE
        now = time.time()
        by_key: dict[str, MarketOdds] = {}
        for page in range(LIGASTAVOK_API_MAX_PAGES):
            payload = {
                "gameId": [],
                "limit": LIGASTAVOK_API_PAGE,
                "skip": page * LIGASTAVOK_API_PAGE,
                "topEvents": False,
                "ts": int(now * 1000),
                "view": "priority",
                "widgetVideo": False,
                "proposedTypes": ["MAINOFFER"],
            }
            data = self._api_post(_EVENTS_LIST, payload)
            if data is None:
                break
            items = (((data.get("result") or {}).get("data")) or []) \
                if isinstance(data, dict) else []
            if not items:
                break
            for item in items:
                for o in self._parse_api_item(item, want_live, now):
                    by_key[o.match_key] = o
            if len(items) < LIGASTAVOK_API_PAGE:
                break  # последняя страница
        if by_key:
            log.info("Liga Stavok: API отдал %d котировок (%s)",
                     len(by_key), kind)
        return list(by_key.values())

    def _parse_api_item(self, item: dict, want_live: bool,
                        now: float) -> list[MarketOdds]:
        if not isinstance(item, dict):
            return []
        ev = item.get("event") or {}
        team1 = (ev.get("team1") or "").strip()
        team2 = (ev.get("team2") or "").strip()
        if not team1 or not team2 or team1 == team2:
            return []

        ns = str(ev.get("ns") or "").lower()
        is_live = "live" in ns
        if want_live != is_live:
            return []

        start_raw = ev.get("startDate")
        start_ts = None
        if start_raw:
            try:
                start_ts = float(start_raw) / 1000.0  # мс → секунды
            except (TypeError, ValueError):
                start_ts = None
        if not want_live:
            if start_ts is None or start_ts <= now:
                return []  # только прематч (будущее время старта)

        game = ev.get("gameTitle") or "Спорт"
        tour = ev.get("tournamentTitle") or ""
        sport = f"{game} · {tour}" if tour else game
        start_time = format_start(start_ts) if start_ts else None
        eid = item.get("id")
        base = dict(
            bookmaker=self.name, sport=sport, team1=team1, team2=team2,
            kind=KIND_LIVE if want_live else KIND_PREMATCH,
            start_time=start_time, start_ts=start_ts,
            url=f"https://www.ligastavok.ru/line/{eid}" if eid else None)

        return self._markets_from_outcomes(item.get("outcomes") or {}, base)

    def _markets_from_outcomes(self, outcomes, base: dict) -> list[MarketOdds]:
        """Строит двухисходные рынки из плоского списка исходов MAINOFFER.

        Исходы группируем по marketId (у каждого рынка свой id); рынок
        распознаётся по подписям исходов (title): 1/2 без ничьей —
        победитель, Больше/Меньше — тотал, Ф1/Ф2 — фора.
        """
        groups: dict = {}
        seq = outcomes.values() if isinstance(outcomes, dict) else outcomes
        for out in seq:
            if not isinstance(out, dict):
                continue
            mid = out.get("marketId") or out.get("marketType") or "_"
            groups.setdefault(mid, []).append(out)

        result: list[MarketOdds] = []
        for outs in groups.values():
            titles = {str(o.get("title") or "").strip() for o in outs}
            by_title = {str(o.get("title") or "").strip().lower(): o
                        for o in outs}
            # --- Победитель (1 / 2 без ничьей) ---
            if {"1", "2"} <= titles and not (titles & {"X", "Х"}):
                k1 = num(str(by_title["1"].get("value")))
                k2 = num(str(by_title["2"].get("value")))
                if self._ok(k1, k2):
                    result.append(MarketOdds(
                        market="Победитель", market_key="winner",
                        outcome1="П1", outcome2="П2", k1=k1, k2=k2, **base))
                continue
            # --- Тотал (Больше / Меньше на одной линии) ---
            over = self._first(outs, _OVER_TITLES)
            under = self._first(outs, _UNDER_TITLES)
            if over and under:
                line = over.get("adValue") or under.get("adValue")
                k1 = num(str(over.get("value")))
                k2 = num(str(under.get("value")))
                if line is not None and self._ok(k1, k2):
                    pt = fmt_total(line)
                    result.append(MarketOdds(
                        market=f"Тотал {pt}", market_key=f"total:{pt}",
                        outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                        k1=k1, k2=k2, **base))
                continue
            # --- Фора (Ф1 / Ф2 с противоположными линиями) ---
            h1o = self._hcap_side(outs, "1")
            h2o = self._hcap_side(outs, "2")
            if h1o and h2o:
                l1 = self._line(h1o.get("adValue"))
                l2 = self._line(h2o.get("adValue"))
                k1 = num(str(h1o.get("value")))
                k2 = num(str(h2o.get("value")))
                if l1 is not None and self._ok(k1, k2):
                    h1 = fmt_hcap(l1)
                    h2 = fmt_hcap(l2) if l2 is not None else neg_hcap(h1)
                    if h2 == neg_hcap(h1):
                        result.append(MarketOdds(
                            market=f"Фора {h1}", market_key=f"hcap::{h1}",
                            outcome1=f"Ф1 {h1}", outcome2=f"Ф2 {h2}",
                            k1=k1, k2=k2, **base))
        return result

    @staticmethod
    def _ok(k1, k2) -> bool:
        return bool(k1 and k2 and k1 > 1 and k2 > 1)

    @staticmethod
    def _line(v):
        try:
            return float(str(v).replace(",", "."))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _first(outs, titles: set) -> dict | None:
        for o in outs:
            if str(o.get("title") or "").strip().lower() in titles:
                return o
        return None

    @staticmethod
    def _hcap_side(outs, side: str) -> dict | None:
        for o in outs:
            t = str(o.get("title") or "").strip().lower()
            if t.startswith(("ф", "h")) and side in t:
                return o
        return None

    # ---------- прогрев cookie браузером ----------

    def _warmup_cookies(self) -> list[str]:
        """Рендерит сайт браузером (решает Qrator), крадёт cookie в
        requests-сессию и возвращает снимки DOM (для HTML-отката)."""
        session = SeleniumSession()
        snaps = session.render_snapshots(
            WARMUP_URL, wait_seconds=LIGASTAVOK_CHALLENGE_WAIT,
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
        self._steal_cookies(session)
        return [s for s in snaps if not self._is_challenge(s)]

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

    # ---------- запасной разбор HTML ----------

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
    _VS_RE = re.compile(r"^(.{2,60}?)\s+[—–-]\s+(.{2,60}?)$")

    def _parse_snaps(self, snaps: list[str], kind: str) -> list[MarketOdds]:
        by_key: dict[str, MarketOdds] = {}
        for html in snaps:
            for o in self._parse_html(html, kind):
                by_key[o.match_key] = o
        return list(by_key.values())

    def _parse_html(self, html: str, kind: str) -> list[MarketOdds]:
        soup = BeautifulSoup(html, SOUP_PARSER)
        result: list[MarketOdds] = []
        for event in soup.select(self._EVENT_SELECTORS):
            teams = [t.get_text(strip=True)
                     for t in event.select(self._TEAM_SELECTORS)]
            teams = [t for t in teams if t]
            if len(teams) < 2:
                teams = self._teams_from_text(event.get_text(" ", strip=True))
            if len(teams) < 2 or teams[0] == teams[1]:
                continue
            team1, team2 = teams[0], teams[1]
            sport_el = event.find_parent(attrs={"data-sport-name": True})
            sport = sport_el["data-sport-name"] if sport_el else "Спорт"
            time_el = event.select_one(self._TIME_SELECTORS)
            start_time = time_el.get_text(strip=True) if time_el else None
            start_ts = parse_start_ts(start_time)
            base = dict(
                bookmaker=self.name, sport=sport,
                team1=team1, team2=team2, kind=kind,
                start_time=start_time, start_ts=start_ts,
                url=self._event_url(event),
            )
            coefs = [c.get_text(strip=True)
                     for c in event.select(self._COEF_SELECTORS)]
            coefs = [c for c in coefs if num(c)]
            k1, k2 = pair(coefs)
            if k1 and k2:
                result.append(MarketOdds(
                    market="Победитель", market_key="winner",
                    outcome1="П1", outcome2="П2", k1=k1, k2=k2, **base))
            result.extend(parse_totals(event, base, self._COEF_SELECTORS))
        return result

    @staticmethod
    def _event_url(event) -> str | None:
        a = event.find("a", href=True)
        if a is None and event.name == "a" and event.get("href"):
            a = event
        if a is None:
            return None
        href = a["href"]
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            return f"https://www.ligastavok.ru{href}"
        return None

    @classmethod
    def _teams_from_text(cls, text: str) -> list[str]:
        m = cls._VS_RE.match(text[:130])
        if m:
            return [m.group(1).strip(), m.group(2).strip()]
        return []
