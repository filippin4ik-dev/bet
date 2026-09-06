"""Stake (stake.com/ru/sports) — собственный спортбук на данных Betradar.

Как устроен доступ
------------------
Линия отдаётся GraphQL-ручкой `{site}/_api/graphql` без авторизации и с
русскими названиями (заголовок `x-language: ru`), но сайт стоит за
Cloudflare в самом строгом режиме: адресам дата-центров он показывает
JS-челлендж с галочкой Turnstile, обычному headless-Chrome и chromedriver —
тоже (галочка не засчитывается). Проверено, что проходит: настоящий Chrome
без следов автоматизации (undetected-chromedriver) с окном на экране/Xvfb,
через резидентный прокси НЕ из России (в России stake.com блокирует РКН —
TLS-рукопожатие рвётся на DPI; из Германии/Финляндии/Бразилии сайт
открыт), и клик по галочке синтетическим событием мыши через CDP. После
этого браузер получает cookie `cf_clearance`, и дальше ВСЁ берётся
обычными HTTP-запросами: cookie + тот же User-Agent + тот же IP (липкая
сессия прокси) — GraphQL отвечает 200 (проверено 2026-09-06).

Челлендж проходится лениво: при первом обходе, когда нет сохранённой
сессии, и после каждого 403 от Cloudflare (cf_clearance живёт около часа и
протухает, если сменился IP выхода). Между проваленными попытками —
пауза STAKE_SOLVE_COOLDOWN: пока прокси лежит, гонять браузер каждые 30 с
бессмысленно. Пройденная сессия пишется в STAKE_SESSION_FILE и после
перезапуска сканера подхватывается без браузера.

Что берём
---------
Один GraphQL-запрос на страницу турниров вида спорта: турниры с матчами,
у каждого матча — ВСЕ группы рынков со всеми рынками и исходами (полная
роспись сразу, отдельных запросов на событие не нужно). Рынки — Betradar:
`templateExtId` — id рынка, `specifiers` — линия («total=2.5»,
«hcp=-1.5», «setnr=1|total=9.5»), у исходов `extId` — стандартные номера
Betradar, одинаковые во всех видах спорта: 1/2/3 — П1/X/П2, 4/5 — П1/П2
двухисходного победителя, 12/13 — больше/меньше, 1714/1715 — фора,
74/76 — обе забьют да/нет, 70/72 — нечет/чет. Разбираем по ним, а
предмет/период рынка (тайм, сет, угловые…) — из русского названия через
общий `market_scope`, как у остальных БК.
"""
import json
import logging
import os
import random
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

from ..config import (STAKE_FEED_TIMEOUT, STAKE_FIXTURES_PER_TOURNAMENT,
                      STAKE_FULL_MARKETS_MAX, STAKE_FULL_MARKETS_REFRESH,
                      STAKE_LANG, STAKE_MAX_PAGES, STAKE_MIN_REFRESH,
                      STAKE_PROXY, STAKE_SESSION_FILE, STAKE_SITE_URL,
                      STAKE_SOLVE_COOLDOWN, STAKE_SOLVE_TIMEOUT, STAKE_SPORTS,
                      STAKE_TOURNAMENTS_PER_PAGE, HTTP_TIMEOUT)
from ..models import KIND_LIVE, KIND_PREMATCH, MarketOdds
from ..proxy_relay import browser_proxy
from .base import BaseParser
from .html_utils import (fmt_hcap, fmt_total, format_start, market_scope,
                         sane_1x2_margin)

log = logging.getLogger("parsers.stake")

# Номера исходов Betradar (одинаковы во всех видах спорта)
OUT_1, OUT_X, OUT_2 = "1", "2", "3"        # исход 1X2
OUT_C1, OUT_C2 = "4", "5"                  # победитель (двухисходный)
OUT_OVER, OUT_UNDER = "12", "13"           # тотал: больше / меньше
OUT_HCAP1, OUT_HCAP2 = "1714", "1715"      # фора: команда 1 / команда 2
OUT_BTS_YES, OUT_BTS_NO = "74", "76"       # обе забьют: да / нет
OUT_ODD, OUT_EVEN = "70", "72"             # нечет / чет

# Виды спорта Stake, которые не матчи (спецставки на политику и т.п.)
_SKIP_SPORTS = {"politics-entertainment", "specials"}

# Рынки с теми же номерами исходов, но другой семантикой — в вилку с
# обычным исходом/тоталом другой БК их ставить нельзя.
_SKIP_MARKET_RE = re.compile(
    r"(?i)оставш|остаток|точн|гонк|следующ|первый гол|первого гола|"
    r"последн|двойной шанс|мультигол|интервал|минут|с разницей|"
    r"3 исхода|три исхода|3-way|досрочн|выигра[её]т хотя бы|"
    r"буллит|пенальти|серия|заб[ьи]т.*оба тайма|в обоих таймах|"
    r"1up|2up|\bи\b|&|монет|лучш")
# Betradar market id «ставки без ничьей» (draw no bet): у Stake рынок так и
# называется «Ничья ставок нет», исходы те же 4/5, что у победителя, — по
# id надёжнее, чем по формулировке.
_DNB_IDS = {"11", "64", "86"}     # матч, 1-я и 2-я половина
_DNB_NAME_RE = re.compile(r"без ничьей|ничья\s+став\w*\s+нет|draw no bet")
# Слова вида рынка — не предмет; убираем перед market_scope
_MARKET_WORDS_RE = re.compile(
    r"(?i)победитель|исход|тотал|фора|гандикап|ставка без ничьей|"
    r"ничья\s+став\w*\s+нет|без ничьей|обе команды забьют|обе забьют|"
    r"чет/нечет|чёт/нечет|нечет/чет|больше/меньше|1x2|1х2|вкл\.?|включая|"
    r"овертайм")

# Спецификаторы периода Betradar → слово периода для market_scope
_SPEC_PERIODS = {
    "setnr": "сет", "periodnr": "период", "quarternr": "четверть",
    "mapnr": "карта", "inningnr": "иннинг", "gamenr": "гейм",
    "framenr": "партия", "roundnr": "раунд",
}

# Плейсхолдер id липкой сессии в STAKE_PROXY (см. config)
SESSION_TOKEN = "{session}"

# Сколько ждать между кликами по галочке Turnstile
_CLICK_EVERY = 6.0
# SportSearchEnum: «upcoming» — прематч (проверено: только матчи, без
# аутрайтов), «live» — лайв. Есть ещё «all» (с аутрайтами) и «popular».
_PREMATCH_TYPE = "upcoming"

# Группы рынков Stake (slugSport.allGroups) — свои у каждого вида спорта.
# «Лёгкий» обход берёт у всех событий только основные группы: исход,
# тоталы, форы, обе забьют — по названию группы без учёта регистра.
_LIGHT_GROUPS = {"winner", "total", "handicap", "both teams to score"}
# Группы, которые не нужны и в полной росписи: игроки, авторы голов,
# спецставки, интервалы, комбинации, аутрайты.
_SKIP_GROUPS_RE = re.compile(
    r"(?i)player|goalscorer|special|minute|interval|sure sub|1up|outright|"
    r"qualify|combo|fast bets|on the mound|other|strikes|point|competitor|"
    r"penalty|игрок|автор|специальн|интервал|комбинац|быстр|очки|удар|"
    r"прочее|буллит")
# Как часто перечитывать список групп вида спорта
_GROUPS_TTL = 3600.0

_FIXTURE_FIELDS = """
        id slug status
        data {
          __typename
          ... on SportFixtureDataMatch {
            startTime isOutright
            teams { name qualifier }
            competitors { name }
          }
          ... on SportFixtureDataOutright { isOutright }
        }"""
_GROUP_FIELDS = """
        groups(groups: $groups, status: [active]) {
          name
          templates(includeEmpty: false) {
            markets {
              name status specifiers templateExtId
              outcomes { active odds extId }
            }
          }
        }"""

_FIXTURES_QUERY = """
query ArbLine($sport: String!, $type: SportSearchEnum!, $tl: Int!,
              $to: Int!, $fl: Int!, $groups: [String!]!) {
  slugSport(sport: $sport) {
    name slug
    tournamentList(type: $type, limit: $tl, offset: $to) {
      name slug
      category { name slug sport { name slug } }
      fixtureList(type: $type, limit: $fl) {""" + _FIXTURE_FIELDS + _GROUP_FIELDS + """
      }
    }
  }
}
"""
_TOURNAMENT_QUERY = """
query ArbTournament($sport: String!, $category: String!, $tournament: String!,
                    $type: SportSearchEnum!, $fl: Int!, $groups: [String!]!) {
  slugTournament(sport: $sport, category: $category, tournament: $tournament) {
    fixtureList(type: $type, limit: $fl) {
      id""" + _GROUP_FIELDS + """
    }
  }
}
"""
_GROUPS_QUERY = """
query ArbGroups($sport: String!) {
  slugSport(sport: $sport) { allGroups { name translation } }
}
"""
_SPORTS_QUERY = "{ sportList { id name slug } }"


class _Challenge(Exception):
    """Cloudflare показал челлендж вместо данных — cf_clearance протухла."""


class StakeParser(BaseParser):
    name = "Stake"
    min_refresh = STAKE_MIN_REFRESH

    def __init__(self) -> None:
        super().__init__()
        self.site = STAKE_SITE_URL
        self._lock = threading.Lock()
        self._ua = ""
        self._cookies: dict[str, str] = {}
        self._solve_after = 0.0       # раньше этого момента челлендж не гоняем
        self._xvfb: subprocess.Popen | None = None
        # группы рынков вида спорта: slug → (момент, [имена])
        self._groups: dict[str, tuple[float, list[str]]] = {}
        # кэш полной росписи турнира: путь турнира → (отпечаток основной
        # линии его событий, момент запроса, {id события: группы})
        self._full: dict[tuple[str, str, str], tuple[str, float, dict]] = {}
        # id липкой сессии прокси (подставляется вместо {session} в
        # STAKE_PROXY). Хранится вместе с cf_clearance: cookie привязана к IP
        # выхода, а выход — к этому id.
        self._session_id = _new_session_id()
        self._load_session()
        self._apply_proxy()

    # ---------- прокси ----------

    @property
    def proxy(self) -> str:
        """Адрес прокси с подставленным id липкой сессии ("" — без прокси)."""
        return STAKE_PROXY.replace(SESSION_TOKEN, self._session_id)

    def _apply_proxy(self) -> None:
        proxy = self.proxy
        self.session.proxies = ({"http": proxy, "https": proxy} if proxy
                                else {})

    def _rotate_exit(self, why: str) -> None:
        """Новый id липкой сессии = новый IP выхода у прокси. Есть смысл,
        только если в STAKE_PROXY стоит {session}."""
        if SESSION_TOKEN not in STAKE_PROXY:
            return
        self._session_id = _new_session_id()
        self._apply_proxy()
        log.info("%s: %s — меняю выход прокси (сессия %s)", self.name, why,
                 self._session_id)

    # ---------- HTTP ----------

    def _headers(self) -> dict:
        return {
            "User-Agent": self._ua or super()._headers()["User-Agent"],
            "Accept": "application/json",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
            "Content-Type": "application/json",
            "Origin": self.site,
            "Referer": f"{self.site}/{STAKE_LANG}/sports",
            "x-language": STAKE_LANG,
            "x-operation-type": "query",
        }

    def _gql(self, query: str, variables: dict | None = None,
             timeout: float | None = None) -> dict:
        """POST на GraphQL. _Challenge — если вместо данных челлендж."""
        resp = self.session.post(
            f"{self.site}/_api/graphql",
            json={"query": query, "variables": variables or {}},
            headers=self._headers(), cookies=self._cookies,
            timeout=timeout or max(HTTP_TIMEOUT, 30))
        if resp.status_code in (403, 429, 503) and (
                "cf-mitigated" in resp.headers or "cloudflare" in
                (resp.headers.get("server") or "").lower() or
                "Just a moment" in resp.text or "__cf_chl" in resp.text):
            raise _Challenge(f"HTTP {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors") and not data.get("data"):
            raise ValueError("GraphQL: " + "; ".join(
                str(e.get("message")) for e in data["errors"])[:300])
        return data.get("data") or {}

    def _gql_retry(self, query: str, variables: dict | None = None,
                   timeout: float | None = None) -> dict:
        """То же, но один раз перепроходит челлендж, если он вылез."""
        try:
            return self._gql(query, variables, timeout)
        except _Challenge as exc:
            log.info("%s: Cloudflare снова показал челлендж (%s) — прохожу "
                     "заново", self.name, exc)
            if not self._solve(force=True):
                raise
            return self._gql(query, variables, timeout)

    # ---------- сессия Cloudflare ----------

    def _load_session(self) -> None:
        try:
            with open(STAKE_SESSION_FILE, encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            return
        cookies = saved.get("cookies") or {}
        if saved.get("ua") and cookies.get("cf_clearance"):
            self._ua = saved["ua"]
            self._cookies = {str(k): str(v) for k, v in cookies.items()}
            if saved.get("proxy_session"):
                self._session_id = str(saved["proxy_session"])
            log.info("%s: подхватил сохранённую сессию Cloudflare (%s)",
                     self.name, STAKE_SESSION_FILE)

    def _save_session(self) -> None:
        try:
            tmp = STAKE_SESSION_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"ua": self._ua, "cookies": self._cookies,
                           "proxy_session": self._session_id,
                           "saved_at": time.time()}, fh)
            os.replace(tmp, STAKE_SESSION_FILE)
        except OSError as exc:
            log.debug("%s: не удалось сохранить сессию: %s", self.name, exc)

    def _have_clearance(self) -> bool:
        return bool(self._ua and self._cookies.get("cf_clearance"))

    def _solve(self, force: bool = False) -> bool:
        """Пройти челлендж Cloudflare браузером. True — cookie получена."""
        with self._lock:
            now = time.monotonic()
            if not force and self._have_clearance():
                return True
            if now < self._solve_after:
                left = int(self._solve_after - now)
                self.status_note = (f"Cloudflare не пройден, следующая "
                                    f"попытка через {left} с")
                return False
            # пауза ставится сразу: даже удачный проход не нужно повторять
            # чаще — если cookie слетает мгновенно, дело в прокси
            self._solve_after = now + STAKE_SOLVE_COOLDOWN
            try:
                ok = self._solve_in_browser()
            except Exception as exc:  # noqa: BLE001 — любой сбой браузера
                log.warning("%s: браузер для челленджа не отработал: %s",
                            self.name, exc)
                self.status_note = f"челлендж Cloudflare: {exc}"
                return False
            if ok:
                self.status_note = ""
                self._save_session()
            else:
                # Cloudflare крутит спиннер бесконечно на выходах с плохой
                # репутацией — следующий раз пробуем с другого IP
                self._rotate_exit("челлендж не пройден")
            return ok

    def _solve_in_browser(self) -> bool:
        try:
            import undetected_chromedriver as uc
        except ImportError as exc:
            raise RuntimeError(
                "нет пакета undetected-chromedriver (pip install "
                "undetected-chromedriver) — челлендж Cloudflare обычный "
                "chromedriver не проходит") from exc
        self._ensure_display()
        options = uc.ChromeOptions()
        server = browser_proxy(self.proxy) if self.proxy else None
        if server:
            options.add_argument(f"--proxy-server={server}")
        for arg in ("--no-sandbox", "--disable-dev-shm-usage",
                    "--lang=ru-RU", "--window-size=1366,768"):
            options.add_argument(arg)
        binary = _chrome_binary()
        if binary:
            options.binary_location = binary
        major = _chrome_major(binary)
        driver_path = _driver_for(major)
        kwargs: dict = {"options": options, "headless": False,
                        "use_subprocess": True}
        if major:
            kwargs["version_main"] = major
        if driver_path:
            kwargs["driver_executable_path"] = driver_path
        log.info("%s: прохожу челлендж Cloudflare браузером (Chrome %s, "
                 "прокси %s)", self.name, major or "?",
                 _mask(self.proxy) if self.proxy else "нет")
        driver = uc.Chrome(**kwargs)
        try:
            driver.set_page_load_timeout(STAKE_SOLVE_TIMEOUT)
            try:
                driver.get(f"{self.site}/{STAKE_LANG}/sports")
            except Exception as exc:  # noqa: BLE001 — SPA грузится долго
                log.debug("%s: get: %s", self.name, exc.__class__.__name__)
            deadline = time.monotonic() + STAKE_SOLVE_TIMEOUT
            last_click = 0.0
            while time.monotonic() < deadline:
                time.sleep(2)
                cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
                title = (driver.title or "").lower()
                if cookies.get("cf_clearance") and "moment" not in title:
                    self._ua = driver.execute_script(
                        "return navigator.userAgent")
                    self._cookies = cookies
                    log.info("%s: челлендж Cloudflare пройден, cf_clearance "
                             "получена", self.name)
                    return True
                if time.monotonic() - last_click >= _CLICK_EVERY:
                    if _click_turnstile(driver):
                        last_click = time.monotonic()
            self.status_note = "челлендж Cloudflare не пройден за отведённое время"
            shot = STAKE_SESSION_FILE + ".fail.png"
            try:
                driver.save_screenshot(shot)
            except Exception:  # noqa: BLE001
                shot = "снимок не сохранился"
            log.warning("%s: челлендж Cloudflare не пройден за %.0f с "
                        "(заголовок: %r, снимок: %s)", self.name,
                        STAKE_SOLVE_TIMEOUT, driver.title, shot)
            return False
        finally:
            try:
                driver.quit()
            except Exception:  # noqa: BLE001
                pass

    def _ensure_display(self) -> None:
        """Turnstile в headless не проходит — нужен экран. Нет DISPLAY —
        поднимаем Xvfb (если установлен) и живём на нём до конца процесса."""
        if os.environ.get("DISPLAY"):
            return
        xvfb = shutil.which("Xvfb")
        if not xvfb:
            raise RuntimeError(
                "нет DISPLAY и не установлен Xvfb (apt install xvfb) — "
                "челлендж Cloudflare в headless-режиме не проходит")
        display = ":97"
        if self._xvfb is None or self._xvfb.poll() is not None:
            self._xvfb = subprocess.Popen(
                [xvfb, display, "-screen", "0", "1366x768x24",
                 "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.0)
        os.environ["DISPLAY"] = display

    # ---------- обход ----------

    def fetch_odds(self) -> list[MarketOdds]:
        return self._collect(live=False)

    def fetch_live_odds(self) -> list[MarketOdds]:
        return self._collect(live=True)

    def _collect(self, live: bool) -> list[MarketOdds]:
        if not self._have_clearance() and not self._solve():
            return []
        deadline = time.monotonic() + STAKE_FEED_TIMEOUT
        sports = self._sports()
        now = time.time()
        out: list[MarketOdds] = []
        events = full = 0
        for slug in sports:
            if time.monotonic() > deadline:
                log.warning("%s: обход не уложился в %.0f с — остальные виды "
                            "спорта в следующий раз", self.name,
                            STAKE_FEED_TIMEOUT)
                break
            all_groups = self._sport_groups(slug)
            light = [g for g in all_groups if g.lower() in _LIGHT_GROUPS] \
                or ["main"]
            fixtures = self._fixtures(slug, live, light, deadline)
            if not live and STAKE_FULL_MARKETS_MAX > 0:
                deep = [g for g in all_groups if not _SKIP_GROUPS_RE.search(g)]
                full += self._merge_full(slug, fixtures, deep, now, deadline)
            for fixture in fixtures:
                events += 1
                out.extend(self._parse_fixture(fixture, live, now))
        n1x2 = sum(1 for o in out if o.market_key.startswith("winner1x2"))
        log.info("%s: %s — %d котировок на %d событий, из них исход 1X2: %d"
                 "%s", self.name, "лайв" if live else "прематч", len(out),
                 events, n1x2,
                 f"; полная роспись у {full} событий" if not live else "")
        return out

    def _sports(self) -> list[str]:
        data = self._gql_retry(_SPORTS_QUERY)
        slugs = [str(s.get("slug") or "") for s in data.get("sportList") or []]
        slugs = [s for s in slugs if s and s not in _SKIP_SPORTS]
        if STAKE_SPORTS:
            slugs = [s for s in slugs if s in STAKE_SPORTS]
        return slugs

    def _sport_groups(self, sport: str) -> list[str]:
        """Имена групп рынков вида спорта (кэш на час)."""
        cached = self._groups.get(sport)
        if cached and time.monotonic() - cached[0] < _GROUPS_TTL:
            return cached[1]
        data = self._gql_retry(_GROUPS_QUERY, {"sport": sport})
        groups = [str(g.get("name") or "") for g in
                  ((data.get("slugSport") or {}).get("allGroups") or [])]
        groups = [g for g in groups if g]
        self._groups[sport] = (time.monotonic(), groups)
        return groups

    def _fixtures(self, sport: str, live: bool, groups: list[str],
                  deadline: float) -> list[dict]:
        found: list[dict] = []
        for page in range(STAKE_MAX_PAGES):
            if time.monotonic() > deadline:
                break
            variables = {"sport": sport,
                         "type": "live" if live else _PREMATCH_TYPE,
                         "tl": STAKE_TOURNAMENTS_PER_PAGE,
                         "to": page * STAKE_TOURNAMENTS_PER_PAGE,
                         "fl": STAKE_FIXTURES_PER_TOURNAMENT,
                         "groups": groups}
            data = self._gql_retry(_FIXTURES_QUERY, variables)
            tournaments = ((data.get("slugSport") or {})
                           .get("tournamentList") or [])
            for tour in tournaments:
                for fx in tour.get("fixtureList") or []:
                    fx["_tournament"] = tour
                    found.append(fx)
            if len(tournaments) < STAKE_TOURNAMENTS_PER_PAGE:
                break
        return found

    # ---------- полная роспись ближайших событий ----------

    def _merge_full(self, sport: str, fixtures: list[dict], groups: list[str],
                    now: float, deadline: float) -> int:
        """Дописать событиям, что начнутся раньше всех, остальные группы
        рынков. Запрос идёт по турниру (он отдаёт все свои события разом)
        и кэшируется: пока основная линия событий турнира не сдвинулась и
        кэш моложе STAKE_FULL_MARKETS_REFRESH, повторно не спрашиваем.
        Возвращает число событий, получивших полную роспись."""
        if not groups:
            return 0
        soon = sorted(
            (fx for fx in fixtures if self._starts(fx) and self._starts(fx) > now),
            key=self._starts)[:STAKE_FULL_MARKETS_MAX]
        by_tour: dict[tuple[str, str, str], list[dict]] = {}
        for fx in soon:
            tour = fx.get("_tournament") or {}
            cat = tour.get("category") or {}
            path = (sport, str(cat.get("slug") or ""), str(tour.get("slug") or ""))
            if all(path):
                by_tour.setdefault(path, []).append(fx)
        merged = 0
        for path, group_fx in by_tour.items():
            if time.monotonic() > deadline:
                break
            fingerprint = "|".join(sorted(self._fingerprint(fx) for fx in group_fx))
            cached = self._full.get(path)
            if cached and cached[0] == fingerprint and \
                    now - cached[1] < STAKE_FULL_MARKETS_REFRESH:
                full = cached[2]
            else:
                try:
                    data = self._gql_retry(_TOURNAMENT_QUERY, {
                        "sport": path[0], "category": path[1],
                        "tournament": path[2], "type": _PREMATCH_TYPE,
                        "fl": STAKE_FIXTURES_PER_TOURNAMENT, "groups": groups})
                except _Challenge:
                    raise
                except Exception as exc:  # noqa: BLE001 — турнир пропускаем
                    log.debug("%s: роспись турнира %s не пришла: %s",
                              self.name, "/".join(path), exc)
                    continue
                full = {str(fx.get("id")): fx.get("groups") or []
                        for fx in ((data.get("slugTournament") or {})
                                   .get("fixtureList") or [])}
                self._full[path] = (fingerprint, now, full)
            for fx in group_fx:
                extra = full.get(str(fx.get("id")))
                if extra:
                    # свежие основные группы — первыми: при совпадении
                    # ключа рынка побеждает первый (см. _parse_fixture)
                    fx["groups"] = list(fx.get("groups") or []) + list(extra)
                    merged += 1
        return merged

    @staticmethod
    def _starts(fx: dict) -> float | None:
        cached = fx.get("_start_ts")
        if cached is None:
            cached = _parse_time((fx.get("data") or {}).get("startTime")) or 0.0
            fx["_start_ts"] = cached
        return cached or None

    @staticmethod
    def _fingerprint(fx: dict) -> str:
        """Отпечаток основной линии события: все кэфы лёгких групп."""
        parts = [str(fx.get("id"))]
        for group in fx.get("groups") or []:
            for template in group.get("templates") or []:
                for market in template.get("markets") or []:
                    parts.append(str(market.get("specifiers") or ""))
                    parts.extend(str(o.get("odds")) for o in
                                 market.get("outcomes") or [])
        return str(hash("|".join(parts)))

    # ---------- разбор ----------

    def _parse_fixture(self, fx: dict, live: bool, now: float) -> list[MarketOdds]:
        data = fx.get("data") or {}
        if data.get("__typename") != "SportFixtureDataMatch" or \
                data.get("isOutright"):
            return []
        team1, team2 = self._teams(data)
        if not team1 or not team2 or team1 == team2:
            return []
        start_ts = self._starts(fx)
        if live:
            kind = KIND_LIVE
        else:
            if start_ts is None or start_ts <= now:
                return []
            kind = KIND_PREMATCH
        tour = fx.get("_tournament") or fx.get("tournament") or {}
        cat = tour.get("category") or {}
        sport = cat.get("sport") or {}
        sport_name = sport.get("name") or "Спорт"
        tour_name = tour.get("name")
        base = dict(
            bookmaker=self.name,
            sport=f"{sport_name} · {tour_name}" if tour_name else sport_name,
            team1=team1, team2=team2, kind=kind,
            start_time=format_start(start_ts) if start_ts else None,
            start_ts=start_ts,
            url=self._event_url(fx, tour, cat, sport))
        out: list[MarketOdds] = []
        seen: set[str] = set()
        for group in fx.get("groups") or []:
            for template in group.get("templates") or []:
                for market in template.get("markets") or []:
                    if (market.get("status") or "active") != "active":
                        continue
                    odds = self._market(market, base)
                    if odds is not None and odds.market_key not in seen:
                        seen.add(odds.market_key)
                        out.append(odds)
        return out

    @staticmethod
    def _teams(data: dict) -> tuple[str, str]:
        home = away = ""
        for team in data.get("teams") or []:
            q = str(team.get("qualifier") or "").lower()
            if q == "home":
                home = str(team.get("name") or "").strip()
            elif q == "away":
                away = str(team.get("name") or "").strip()
        comps = data.get("competitors") or []
        if (not home or not away) and len(comps) >= 2:
            home = home or str(comps[0].get("name") or "").strip()
            away = away or str(comps[1].get("name") or "").strip()
        return home, away

    def _market(self, market: dict, base: dict) -> MarketOdds | None:
        name = str(market.get("name") or "")
        if _SKIP_MARKET_RE.search(name):
            return None
        spec = _parse_spec(market.get("specifiers"))
        ks: dict[str, float] = {}
        for o in market.get("outcomes") or []:
            if o.get("active") is False:
                continue
            ext = str(o.get("extId") or "")
            try:
                k = float(o.get("odds"))
            except (TypeError, ValueError):
                continue
            if ext and k > 1:
                ks[ext] = k
        ids = set(ks)
        low = name.lower().replace("ё", "е")
        t1, t2 = base["team1"].lower(), base["team2"].lower()
        has_c1 = bool(t1) and t1 in low
        has_c2 = bool(t2) and t2 in low
        scope = self._scope(name, base, spec)

        def tag(label: str) -> str:
            return label + (f" ({scope})" if scope else "")

        def key(root: str) -> str:
            return f"{root}:{scope}" if scope else root

        # --- Исход 1X2 ---
        if ids >= {OUT_1, OUT_X, OUT_2}:
            k1, kx, k2 = ks[OUT_1], ks[OUT_X], ks[OUT_2]
            if not sane_1x2_margin(k1, kx, k2):
                return None
            return MarketOdds(market=tag("Исход (1X2)"), market_key=key("winner1x2"),
                              outcome1="П1", outcome2="П2", outcome3="X",
                              k1=k1, k2=k2, k3=kx, **base)
        # --- Ставка без ничьей / победитель ---
        if ids >= {OUT_C1, OUT_C2}:
            if str(market.get("templateExtId")) in _DNB_IDS \
                    or _DNB_NAME_RE.search(low):
                return MarketOdds(market=tag("Ставка без ничьей"),
                                  market_key=key("winner_dnb"),
                                  outcome1="П1", outcome2="П2",
                                  k1=ks[OUT_C1], k2=ks[OUT_C2], **base)
            return MarketOdds(market=tag("Победитель"), market_key=key("winner"),
                              outcome1="П1", outcome2="П2",
                              k1=ks[OUT_C1], k2=ks[OUT_C2], **base)
        # --- Тоталы (в т.ч. индивидуальные) ---
        if ids >= {OUT_OVER, OUT_UNDER} and "total" in spec:
            pt = fmt_total(spec["total"])
            over, under = ks[OUT_OVER], ks[OUT_UNDER]
            if has_c1 ^ has_c2:
                side = "1" if has_c1 else "2"
                team = base["team1"] if has_c1 else base["team2"]
                return MarketOdds(market=f"Тотал {pt} ({team})",
                                  market_key=f"itotal:{side}:{scope}:{pt}",
                                  outcome1=f"ИТБ {pt}", outcome2=f"ИТМ {pt}",
                                  k1=over, k2=under, **base)
            if has_c1 and has_c2:
                return None
            return MarketOdds(market=tag(f"Тотал {pt}"),
                              market_key=(f"total:{scope}:{pt}" if scope
                                          else f"total:{pt}"),
                              outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                              k1=over, k2=under, **base)
        # --- Форы ---
        if ids >= {OUT_HCAP1, OUT_HCAP2} and "hcp" in spec:
            if has_c1 and has_c2:
                return None
            h = _num(spec["hcp"])
            if h is None:
                return None
            h1, h2 = fmt_hcap(h), fmt_hcap(-h)
            return MarketOdds(market=tag(f"Фора {h1}"),
                              market_key=f"hcap:{scope}:{h1}",
                              outcome1=f"Ф1 {h1}", outcome2=f"Ф2 {h2}",
                              k1=ks[OUT_HCAP1], k2=ks[OUT_HCAP2], **base)
        # --- Обе забьют ---
        if ids >= {OUT_BTS_YES, OUT_BTS_NO} and "заб" in low \
                and not (has_c1 or has_c2):
            return MarketOdds(market=tag("Обе забьют"), market_key=key("bothscore"),
                              outcome1="Да", outcome2="Нет",
                              k1=ks[OUT_BTS_YES], k2=ks[OUT_BTS_NO], **base)
        # --- Чет/Нечет ---
        if ids >= {OUT_ODD, OUT_EVEN} and "чет" in low \
                and not (has_c1 or has_c2):
            return MarketOdds(market=tag("Чет/Нечет"), market_key=key("oddeven"),
                              outcome1="Чет", outcome2="Нечет",
                              k1=ks[OUT_EVEN], k2=ks[OUT_ODD], **base)
        return None

    @staticmethod
    def _scope(name: str, base: dict, spec: dict | None = None) -> str:
        """Предмет/период рынка из русского названия («Тотал — 1-й тайм»
        → «half1»); команды, линию и слова вида рынка убираем. Если период
        в названии не назван, но есть в спецификаторе Betradar
        («setnr=2»), добавляем его словами — market_scope поймёт."""
        text = name
        for team in (base["team1"], base["team2"]):
            if team:
                text = text.replace(team, " ")
        text = re.sub(r"\([^)]*\)", " ", text)
        text = _MARKET_WORDS_RE.sub(" ", text)
        text = re.sub(r"[\u2014\u2013:]+", " ", text)
        # линия тотала/форы («2.5», «+1,5») — не предмет; целые числа
        # оставляем: «1-й тайм», «2 сет» — это период
        text = re.sub(r"(?<![\d.])[+-]?\d+[.,]\d+(?![\d.])", " ", text)
        scope = market_scope(text)
        if spec and not re.search(r"\d", text):
            for key, word in _SPEC_PERIODS.items():
                if key in spec and str(spec[key]).isdigit():
                    scope = market_scope(f"{text} {spec[key]}-й {word}")
                    break
        return scope

    def _event_url(self, fx: dict, tour: dict, cat: dict,
                   sport: dict) -> str | None:
        parts = [sport.get("slug"), cat.get("slug"), tour.get("slug"),
                 fx.get("slug")]
        if not all(parts):
            return f"{self.site}/{STAKE_LANG}/sports"
        return f"{self.site}/{STAKE_LANG}/sports/" + "/".join(parts)


# ---------- помощники ----------

def _parse_spec(spec: str | None) -> dict:
    """«setnr=1|total=21.5» → {'setnr': '1', 'total': '21.5'}."""
    out: dict[str, str] = {}
    for part in str(spec or "").split("|"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _parse_time(value) -> float | None:
    """Время начала → unix-время. Stake отдаёт RFC 2822 («Sun, 06 Sep 2026
    13:00:00 GMT»); на всякий случай понимаем и ISO-8601, и unix-время
    (секунды/миллисекунды)."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 1e11 else 1)
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return _parse_time(float(text))
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _new_session_id() -> str:
    return "arb" + "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789",
                                          k=8))


def _mask(proxy_url: str) -> str:
    u = urlsplit(proxy_url)
    if not u.hostname:
        return proxy_url
    user = f"{u.username}:***@" if u.username else ""
    return f"{u.scheme}://{user}{u.hostname}:{u.port}"


def _click_turnstile(driver) -> bool:
    """Клик по галочке Turnstile синтетическим событием мыши через CDP.

    Виджет живёт в закрытом shadow DOM, поэтому обычным find_element до
    него не добраться; но его контейнер (сосед скрытого поля
    cf-turnstile-response) в обычном DOM, и координаты галочки от него
    постоянны: ~28px от левого края, по центру высоты.
    """
    try:
        rect = driver.execute_script("""
          const inp = document.querySelector('input[name=cf-turnstile-response]');
          if (!inp) return null;
          const host = inp.parentElement.firstElementChild;
          const r = host.getBoundingClientRect();
          return [r.left, r.top, r.width, r.height];""")
    except Exception:  # noqa: BLE001
        return False
    if not rect or rect[2] <= 0 or rect[3] <= 0:
        return False
    x, y = rect[0] + 28, rect[1] + rect[3] / 2
    try:
        for kind, count in (("mouseMoved", 0), ("mousePressed", 1),
                            ("mouseReleased", 1)):
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": kind, "x": x, "y": y, "button": "left",
                "clickCount": count})
            time.sleep(0.15)
    except Exception:  # noqa: BLE001
        return False
    return True


def _chrome_binary() -> str | None:
    from .selenium_helper import BROWSER_CANDIDATES, _first_existing
    return _first_existing(BROWSER_CANDIDATES)


def _chrome_major(binary: str | None) -> int | None:
    """Мажорная версия установленного Chrome: undetected-chromedriver
    иначе скачает драйвер под последний Chrome, и он не подойдёт."""
    if not binary:
        return None
    try:
        out = subprocess.run([binary, "--version"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.\d+\.\d+", out or "")
    return int(m.group(1)) if m else None


def _driver_for(major: int | None) -> str | None:
    """Готовый chromedriver нужной версии: явный CHROMEDRIVER_PATH или
    кэш Selenium Manager (~/.cache/selenium/chromedriver/...). None —
    undetected-chromedriver скачает сам."""
    explicit = os.getenv("CHROMEDRIVER_PATH", "").strip()
    if explicit and os.path.exists(explicit):
        return explicit
    if not major:
        return None
    root = os.path.expanduser("~/.cache/selenium/chromedriver")
    best: tuple[tuple[int, ...], str] | None = None
    for platform in ("linux64", "mac-x64", "mac-arm64", "win64", "win32"):
        base = os.path.join(root, platform)
        if not os.path.isdir(base):
            continue
        for version in os.listdir(base):
            if not version.startswith(f"{major}."):
                continue
            path = os.path.join(base, version, "chromedriver")
            if not os.path.exists(path):
                path += ".exe"
                if not os.path.exists(path):
                    continue
            try:
                ver = tuple(int(p) for p in version.split("."))
            except ValueError:
                continue
            if best is None or ver > best[0]:
                best = (ver, path)
    return best[1] if best else None
