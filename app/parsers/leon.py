"""LeonBet — прематч-линия через публичный JSON-фид betline.

leon.ru отдаёт всю линию сайта (прематч + лайв) одним JSON-снимком БЕЗ
авторизации (используется самим фронтендом сайта):

    GET {LEON_API_HOST}/api-2/betline/changes/all?ctag=ru-RU&vtag=&flags=...

Ответ — плоский список событий, у каждого «топ»-рынки (~9 на событие:
исход/1X2, основной тотал/фора, тайм-маркеты, обе забьют, тоталы хозяев/
гостей). Полную роспись рынков события (лестница доп. линий тоталов/фор,
рынки по сетам в теннисе и т.п.) отдаёт отдельный запрос НА КАЖДОЕ событие:

    GET {LEON_API_HOST}/api-2/betline/event/all?ctag=ru-RU&eventId=<id>&flags=...

В отличие от Winline/Fonbet (где рынок нужно узнавать по числовому id —
предметная область не задокументирована) у Leon рынки размечены СЕМАНТИЧЕСКИ:
у каждого рынка typeTag (REGULAR/TOTAL/HANDICAP) и человекочитаемое русское
имя, у каждого исхода — набор tags (HOME/AWAY/DRAW/OVER/UNDER/YES/NO/ODD/
EVEN), а у тоталов/фор коэффициент каждой стороны несёт уже ЗНАКОВУЮ линию
(runner["handicap"]). Поэтому рынок разбирается по набору тегов исходов, а
период/предмет — из русского имени через общий market_scope() (у Leon эти
слова уже человекочитаемые: «1-й тайм: Тотал 1», «Тотал хозяев 0.5»).

Разбираем:
- «Исход 1Х2» (HOME/DRAW/AWAY) → market_key=winner1x2 (движок
  arbitrage.find_arbs_1x2), с защитной проверкой маржи (sane_1x2_margin);
- «…Победитель»/«Итоговая победа» (HOME/AWAY, без ничьей) → winner;
- «…Фора…» (HANDICAP, HOME/AWAY) — знак линии берём из runner["handicap"];
- «…Тотал…» (TOTAL, OVER/UNDER) — включая «Тотал хозяев/гостей»
  (индивидуальный тотал команды, market_key=itotal:<сторона>);
- «Обе забьют» Да/Нет (только явное «обе… забьют», не любые Да/Нет-рынки —
  у Leon есть и другие пропы вида «Хозяева забьют», «Пенальти будет» и
  т.п., их семантику по одним tags=YES/NO не отличить, поэтому пропускаем,
  чтобы не склеить в вилку разные по смыслу рынки);
- «Чет/Нечет» (ODD/EVEN) — только основной рынок матча (без team-специфик,
  та же причина, что и для Да/Нет).
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import (LEON_API_HOST, LEON_FEED_TIMEOUT, LEON_FULL_MARKETS,
                      LEON_FULL_MARKETS_WORKERS)
from ..models import KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import (fmt_hcap, fmt_total, format_start, market_scope,
                         sane_1x2_margin)

log = logging.getLogger("parsers.leon")

_CHANGES_URL = f"{LEON_API_HOST}/api-2/betline/changes/all"
_EVENT_URL = f"{LEON_API_HOST}/api-2/betline/event/all"
# flags сняты с реального запроса фронтенда сайта (см. докстринг модуля).
_FLAGS = "reg,urlv2,mm2,rrc,fac"

_HOME_AWAY = frozenset({"HOME", "AWAY"})
_HOME_DRAW_AWAY = frozenset({"HOME", "DRAW", "AWAY"})
_OVER_UNDER = frozenset({"OVER", "UNDER"})
_YES_NO = frozenset({"YES", "NO"})
_ODD_EVEN = frozenset({"ODD", "EVEN"})


class LeonParser(BaseParser):
    name = "LeonBet"

    def fetch_odds(self) -> list[MarketOdds]:
        now = time.time()
        deadline = time.monotonic() + LEON_FEED_TIMEOUT
        try:
            snap = self.get_json(_CHANGES_URL, params={
                "ctag": "ru-RU", "vtag": "", "flags": _FLAGS}, timeout=30)
        except Exception as exc:  # noqa: BLE001
            log.warning("LeonBet: не удалось получить снимок линии: %s", exc)
            return []

        events = snap.get("data") or []
        by_id: dict[int, dict] = {
            e["id"]: e for e in events
            if e.get("betline") == "prematch" and e.get("open")
            and e.get("markets") and e.get("id")
        }
        log.info("LeonBet: снимок линии — %d прематч-событий", len(by_id))
        if not by_id:
            return []

        if LEON_FULL_MARKETS:
            self._enrich_full_markets(by_id, deadline)

        by_key: dict[str, MarketOdds] = {}
        for event in by_id.values():
            for o in self._parse_event(event, now):
                by_key[o.match_key] = o
        return list(by_key.values())

    # ---- полная роспись рынков (отдельный запрос на событие) ----

    def _enrich_full_markets(self, by_id: dict, deadline: float) -> None:
        """Заменяет «топ»-рынки события (из общего снимка) полной
        росписью. Событие, не успевшее до дедлайна/упавшее с ошибкой,
        остаётся с базовыми рынками — не хуже, чем совсем без обогащения."""
        ids = list(by_id)
        pool = ThreadPoolExecutor(max_workers=LEON_FULL_MARKETS_WORKERS)
        futures = {pool.submit(self._fetch_event, eid): eid for eid in ids}
        got = 0
        try:
            for fut in as_completed(futures):
                eid = futures[fut]
                try:
                    full = fut.result()
                except Exception:  # noqa: BLE001
                    full = None
                if full and full.get("markets"):
                    by_id[eid] = full
                    got += 1
                if time.monotonic() > deadline:
                    log.info("LeonBet: дедлайн полной росписи истёк, "
                             "%d/%d событий успели обогатиться",
                             got, len(ids))
                    break
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        log.info("LeonBet: полная роспись получена по %d/%d событиям",
                 got, len(ids))

    def _fetch_event(self, event_id) -> dict | None:
        try:
            return self.get_json(_EVENT_URL, params={
                "ctag": "ru-RU", "eventId": event_id, "flags": _FLAGS},
                timeout=8)
        except Exception:  # noqa: BLE001
            return None

    # ---- разбор одного события ----

    def _parse_event(self, event: dict, now: float) -> list[MarketOdds]:
        team1, team2 = self._teams(event)
        if not team1 or not team2 or team1 == team2:
            return []
        kickoff = event.get("kickoff")
        if not kickoff:
            return []
        start_ts = kickoff / 1000.0
        if start_ts <= now:
            return []  # только прематч

        league = event.get("league") or {}
        sport_info = league.get("sport") or {}
        sport_name = sport_info.get("name") or "Спорт"
        league_name = league.get("name") or ""
        sport = f"{sport_name} · {league_name}" if league_name else sport_name

        base = dict(bookmaker=self.name, sport=sport, team1=team1,
                    team2=team2, kind=KIND_PREMATCH,
                    start_time=format_start(start_ts), start_ts=start_ts,
                    url=self._event_url(event, league, sport_info))

        result: list[MarketOdds] = []
        seen_keys: set[str] = set()
        for m in event.get("markets") or []:
            o = self._parse_market(m, base)
            if o is None or o.market_key in seen_keys:
                continue
            seen_keys.add(o.market_key)
            result.append(o)
        return result

    def _parse_market(self, m: dict, base: dict) -> MarketOdds | None:
        if not m.get("open", True):
            return None
        runners = self._runner_map(m)
        tagset = frozenset(runners)
        name = m.get("name") or ""
        type_tag = m.get("typeTag")
        low = name.lower().replace("ё", "е")
        scope = market_scope(name)

        if tagset == _HOME_DRAW_AWAY and type_tag == "REGULAR":
            k1, kx = runners["HOME"]["price"], runners["DRAW"]["price"]
            k2 = runners["AWAY"]["price"]
            if not sane_1x2_margin(k1, kx, k2):
                return None
            key = f"winner1x2:{scope}" if scope else "winner1x2"
            return MarketOdds(
                market=name, market_key=key,
                outcome1="П1", outcome2="П2", outcome3="X",
                k1=k1, k2=k2, k3=kx, **base)

        if tagset == _HOME_AWAY and type_tag == "REGULAR":
            key = f"winner:{scope}" if scope else "winner"
            return MarketOdds(
                market=name, market_key=key,
                outcome1="П1", outcome2="П2",
                k1=runners["HOME"]["price"], k2=runners["AWAY"]["price"],
                **base)

        if tagset == _HOME_AWAY and type_tag == "HANDICAP":
            h1_raw = runners["HOME"].get("handicap")
            h2_raw = runners["AWAY"].get("handicap")
            try:
                if h1_raw is None or h2_raw is None \
                        or float(h1_raw) != -float(h2_raw):
                    return None
            except (TypeError, ValueError):
                return None
            h1, h2 = fmt_hcap(h1_raw), fmt_hcap(h2_raw)
            # scope пустой (фора всего матча) — сегмент всё равно на месте:
            # «hcap::-1.5», как у остальных БК (см. canon_market_key)
            key = f"hcap:{scope}:{h1}"
            return MarketOdds(
                market=name, market_key=key,
                outcome1=f"Ф1 {h1}", outcome2=f"Ф2 {h2}",
                k1=runners["HOME"]["price"], k2=runners["AWAY"]["price"],
                **base)

        if tagset == _OVER_UNDER and type_tag == "TOTAL":
            line = m.get("handicap")
            if line is None:
                line = runners["OVER"].get("handicap")
            if line is None:
                return None
            pt = fmt_total(line)
            over, under = runners["OVER"]["price"], runners["UNDER"]["price"]
            # «Тотал хозяев/гостей» — индивидуальный тотал команды, а не
            # общий рынок матча: сторону несёт market_key (itotal:<side>),
            # само слово «хозяев»/«гостей» убираем из текста ДО market_scope
            # — иначе scope разойдётся с «Тотал <имя команды>» других БК.
            if "хозя" in low:
                side, rest = 1, low.replace("хозяев", "").replace(
                    "хозяева", "")
            elif "гост" in low:
                side, rest = 2, low.replace("гостей", "").replace(
                    "гости", "")
            else:
                side, rest = None, None
            if side:
                iscope = market_scope(rest)
                team = base["team1"] if side == 1 else base["team2"]
                key = f"itotal:{side}:{iscope}:{pt}"
                return MarketOdds(
                    market=f"Тотал {pt} ({team})", market_key=key,
                    outcome1=f"ИТБ {pt}", outcome2=f"ИТМ {pt}",
                    k1=over, k2=under, **base)
            key = f"total:{scope}:{pt}" if scope else f"total:{pt}"
            return MarketOdds(
                market=name, market_key=key,
                outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                k1=over, k2=under, **base)

        if tagset == _YES_NO and "обе" in low and "забьют" in low:
            # Только «Обе забьют»: у Leon есть и другие Да/Нет-пропы
            # («Хозяева забьют», «Пенальти будет» и т.п.) с теми же tags —
            # по одним тегам их не отличить, поэтому берём точечно.
            key = f"bothscore:{scope}" if scope else "bothscore"
            return MarketOdds(
                market=name, market_key=key,
                outcome1="Да", outcome2="Нет",
                k1=runners["YES"]["price"], k2=runners["NO"]["price"],
                **base)

        if tagset == _ODD_EVEN and "хозя" not in low and "гост" not in low:
            # Только общий «Чет/Нечет» матча/периода — командные варианты
            # («Чет/Нечет хозяев/гостей») тоже дают tags ODD/EVEN, но по
            # market_scope не отличаются от общего рынка (оба слова не
            # несут «предметного» токена) — пропускаем, чтобы не смешать.
            # Порядок исходов ОБЯЗАН совпадать с остальными парсерами
            # (Winline/BetBoom/Fonbet/Betcity): 1-й исход — «Чет». Движок
            # сшивает чет/нечет по позиции исхода, а не по подписи, так что
            # перевёрнутый порядок дал бы ложную вилку «чет против чета».
            key = f"oddeven:{scope}" if scope else "oddeven"
            return MarketOdds(
                market=name, market_key=key,
                outcome1="Чет", outcome2="Нечет",
                k1=runners["EVEN"]["price"], k2=runners["ODD"]["price"],
                **base)

        return None

    @staticmethod
    def _runner_map(m: dict) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in m.get("runners") or []:
            if not r.get("open", True):
                continue
            price = r.get("price")
            if not price or price <= 1.0:
                continue
            tags = r.get("tags") or []
            if len(tags) == 1:
                out[tags[0]] = r
        return out

    @staticmethod
    def _teams(event: dict) -> tuple[str, str]:
        comps = event.get("competitors") or []
        home = next((c.get("name") for c in comps
                    if c.get("homeAway") == "HOME"), None)
        away = next((c.get("name") for c in comps
                    if c.get("homeAway") == "AWAY"), None)
        if home and away:
            return home.strip(), away.strip()
        name = (event.get("name") or "").strip()
        if " - " in name:
            a, b = name.split(" - ", 1)
            return a.strip(), b.strip()
        return "", ""

    @staticmethod
    def _event_url(event: dict, league: dict, sport_info: dict) -> str | None:
        """Ссылка на страницу события: /{спорт}/{турнир}/{событие} (слаги
        сайта; формат не задокументирован, собран по образцу структуры
        фида — если разойдётся, страница всё равно откроет SPA-роутер)."""
        sp, lg, ev = (sport_info.get("url"), league.get("url"),
                     event.get("url"))
        if sp and lg and ev:
            return f"{LEON_API_HOST}/{sp}/{lg}/{ev}"
        if sp and ev:
            return f"{LEON_API_HOST}/{sp}/{ev}"
        return f"{LEON_API_HOST}/results"
