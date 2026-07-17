"""Фоновый сканер: раз в SCAN_INTERVAL секунд опрашивает все БК
(параллельно), ищет вилки и сохраняет новые находки в SQLite.

Котировки хранятся ПО КАЖДОЙ БК и живут между циклами:
- пришли свежие данные БК — её котировки заменяются целиком;
- обход БК сорвался (сеть, рендеринг) — старые котировки остаются до ODDS_TTL,
  матчи не «слетают» в начале каждого круга;
- матч начался (по распознанному времени старта) — убирается сразу.
"""
import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import db
from .arbitrage import (_market_group, _neg_hcap, _time_clusters, find_arbs,
                        norm_team)
from .config import (LIVE_ODDS_TTL, LIVE_PER_BK_GAP, LIVE_SCAN_INTERVAL,
                     ODDS_TTL, SCAN_INTERVAL)
from .models import Arb, KIND_LIVE, KIND_PREMATCH, MarketOdds
from .parsers import get_parsers
from .parsers.base import BaseParser
from .parsers.html_utils import display_market

log = logging.getLogger("scanner")


class Scanner:
    """Фоновый сканер одного режима: прематч ИЛИ лайв.

    mode="prematch" — медленный цикл, матчи убираются по времени старта;
    mode="live"     — быстрый цикл, матчи в игре, короткий TTL котировок
                      (в лайве старый кэф опаснее его отсутствия).
    """

    def __init__(self, mode: str = KIND_PREMATCH) -> None:
        self.mode = mode
        self.live = mode == KIND_LIVE
        self.interval = LIVE_SCAN_INTERVAL if self.live else SCAN_INTERVAL
        self.ttl = LIVE_ODDS_TTL if self.live else ODDS_TTL
        self.parsers = get_parsers()
        self._lock = threading.Lock()
        self._arbs: list[Arb] = []
        # котировки по каждой БК (живут между циклами) + время их получения
        self._odds_by_bk: dict[str, list[MarketOdds]] = {}
        self._fetched_at: dict[str, float] = {}
        self._last_scan: float | None = None
        self._scan_count = 0
        self._scanning = False
        self._events_checked = 0   # уникальных событий сейчас в памяти
        self._quotes_checked = 0   # всего котировок (событие x БК)
        # ключи вилок прошлого цикла — чтобы писать в историю только новые
        self._prev_keys: set[str] = set()
        self._executor = ThreadPoolExecutor(max_workers=len(self.parsers),
                                            thread_name_prefix=f"scan-{mode}")
        self._stop = asyncio.Event()

    def _fetch(self, parser):
        return parser.safe_fetch_live() if self.live else parser.safe_fetch()

    # ---------- публичное состояние ----------

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            return {
                "mode": self.mode,
                "scan_interval": self.interval,
                "scan_count": self._scan_count,
                "scanning": self._scanning,
                "last_scan": self._last_scan,
                "events_checked": self._events_checked,
                "quotes_checked": self._quotes_checked,
                # по каждой БК: сколько котировок и сколько секунд назад
                # они получены (для индикатора свежести в UI)
                "bookmakers": {
                    bk: {"count": len(o),
                         "age_sec": round(now - self._fetched_at.get(bk, now))}
                    for bk, o in self._odds_by_bk.items()},
                "arbs": [a.to_dict() for a in self._arbs],
            }

    def odds_snapshot(self) -> list[dict]:
        """Все котировки, находящиеся сейчас в памяти (все найденные матчи)."""
        with self._lock:
            return [o.to_dict()
                    for odds in self._odds_by_bk.values() for o in odds]

    # ---------- матчи: группировка котировок по событиям ----------

    def _all_odds(self) -> list[MarketOdds]:
        with self._lock:
            return [o for odds in self._odds_by_bk.values() for o in odds]

    @staticmethod
    def _event_groups(all_odds: list[MarketOdds]) -> dict[str, list[MarketOdds]]:
        """Группирует котировки всех БК по событию (матч + время старта).

        Ключ события устойчив между опросами: kind | отсортированная пара
        нормализованных команд | номер кластера времени старта (разные
        матчи одной пары команд не сливаются)."""
        clusters = _time_clusters(all_odds)
        groups: dict[str, list[MarketOdds]] = {}
        for o in all_odds:
            t1, t2 = norm_team(o.team1), norm_team(o.team2)
            if not t1 or not t2 or t1 == t2:
                continue
            teams = frozenset((t1, t2))
            cluster = 0
            if o.kind == KIND_PREMATCH and o.start_ts:
                cluster = clusters.get((o.kind, teams), {}).get(o.start_ts, 0)
            key = f"{o.kind}|{'|'.join(sorted(teams))}|{cluster}"
            groups.setdefault(key, []).append(o)
        return groups

    def matches_snapshot(self) -> list[dict]:
        """Список всех найденных матчей (сгруппированных по событию)."""
        groups = self._event_groups(self._all_odds())
        out = []
        for event_id, odds in groups.items():
            # образец с временем старта и самыми длинными именами команд
            sample = max(odds, key=lambda o: (o.start_ts is not None,
                                              len(o.team1) + len(o.team2)))
            books = sorted({o.bookmaker for o in odds})
            markets = {_market_group(o) for o in odds}
            start_ts = min((o.start_ts for o in odds if o.start_ts),
                           default=None)
            out.append({
                "id": event_id,
                "kind": sample.kind,
                "sport": sample.sport,
                "match": f"{sample.team1} — {sample.team2}",
                "team1": sample.team1,
                "team2": sample.team2,
                "start_ts": start_ts,
                "start_time": sample.start_time,
                "bookmakers": books,
                "markets_count": len(markets),
            })
        return out

    def match_detail(self, event_id: str) -> dict | None:
        """Полная роспись одного события: все рынки всех БК бок о бок."""
        groups = self._event_groups(self._all_odds())
        odds = groups.get(event_id)
        if not odds:
            return None
        sample = max(odds, key=lambda o: (o.start_ts is not None,
                                          len(o.team1) + len(o.team2)))
        base_t1 = norm_team(sample.team1)

        markets: dict[str, dict] = {}
        for o in odds:
            # ориентация к team1 события: у БК с перевёрнутым порядком
            # команд исходы меняются местами (тоталы и «обе забьют»
            # от порядка команд не зависят)
            flipped = (norm_team(o.team1) != base_t1
                       and not o.market_key.startswith(("total",
                                                        "bothscore")))
            mg = _market_group(o)
            m = markets.get(mg)
            if m is None:
                m = markets[mg] = {
                    "market": display_market(o.market_key, o.market),
                    "market_key": o.market_key,
                    "outcome1": o.outcome1,
                    "outcome2": o.outcome2,
                    "quotes": {},
                }
                if flipped:
                    m["outcome1"] = self._swap_side(o.outcome2)
                    m["outcome2"] = self._swap_side(o.outcome1)
                    if o.market_key.startswith("hcap"):
                        # в имени рынка — линия team1 СОБЫТИЯ, а не этой БК
                        h1 = o.market_key.rsplit(":", 1)[1]
                        key = o.market_key[:-len(h1)] + _neg_hcap(h1)
                        m["market"] = display_market(key, o.market)
            k1, k2 = (o.k2, o.k1) if flipped else (o.k1, o.k2)
            if o.bookmaker not in m["quotes"]:
                m["quotes"][o.bookmaker] = {"k1": k1, "k2": k2}

        def _sort_key(item):
            key = item[1]["market_key"]
            cat = 0 if key.startswith("winner") else \
                1 if key.startswith("bothscore") else \
                2 if key.startswith("total") else 3
            parts = key.split(":")
            scope = parts[1] if len(parts) > 2 else ""
            try:
                line = abs(float(parts[-1].replace("+", "")))
            except ValueError:
                line = 0.0
            return (cat, scope, line, key)

        market_rows = []
        for _, m in sorted(markets.items(), key=_sort_key):
            m["quotes"] = [
                {"bookmaker": bk, **ks}
                for bk, ks in sorted(m["quotes"].items())]
            market_rows.append(m)

        return {
            "id": event_id,
            "kind": sample.kind,
            "sport": sample.sport,
            "match": f"{sample.team1} — {sample.team2}",
            "team1": sample.team1,
            "team2": sample.team2,
            "start_ts": min((o.start_ts for o in odds if o.start_ts),
                            default=None),
            "start_time": sample.start_time,
            "bookmakers": sorted({o.bookmaker for o in odds}),
            "markets": market_rows,
        }

    @staticmethod
    def _swap_side(label: str) -> str:
        """«Ф2 +1.5» → «Ф1 +1.5», «П2» → «П1» (перестановка стороны
        исхода при перевёрнутом порядке команд у одной из БК)."""
        for a, b in (("1", "2"), ("2", "1")):
            for pref in ("Ф", "П"):
                if label.startswith(pref + a):
                    return pref + b + label[2:]
        return label

    # ---------- обновление состояния ----------

    def _prune_locked(self, now: float) -> None:
        """Убирает протухшие данные БК и (для прематча) начавшиеся матчи."""
        for bk in list(self._odds_by_bk):
            if now - self._fetched_at.get(bk, 0) > self.ttl:
                # БК давно не отвечает — её кэфы больше нельзя считать
                # актуальными, на них нельзя ставить
                log.info("Котировки %s устарели (> %d с) — убраны",
                         bk, int(self.ttl))
                del self._odds_by_bk[bk]
                self._fetched_at.pop(bk, None)
                continue
            if self.live:
                # лайв: матч «в игре», по времени старта не отсеиваем —
                # завершённые матчи БК сама перестаёт отдавать (уйдут по TTL)
                continue
            kept = [o for o in self._odds_by_bk[bk] if not o.started(now)]
            if len(kept) != len(self._odds_by_bk[bk]):
                log.info("%s: %d матчей начались — убраны из прематча",
                         bk, len(self._odds_by_bk[bk]) - len(kept))
                self._odds_by_bk[bk] = kept

    def _update_bk(self, bk: str, odds: list[MarketOdds]) -> list[Arb]:
        """Обновляет котировки одной БК и пересчитывает вилки.

        Пустой результат (сбой обхода) НЕ затирает старые данные —
        они живут до ODDS_TTL, чтобы матчи не пропадали между циклами.
        """
        now = time.time()
        with self._lock:
            if odds:
                self._odds_by_bk[bk] = odds
                self._fetched_at[bk] = now
            self._prune_locked(now)
            all_odds = [o for os_ in self._odds_by_bk.values() for o in os_]

        arbs = find_arbs(all_odds)
        with self._lock:
            self._arbs = arbs
            self._last_scan = now
            self._events_checked = len({o.event_key for o in all_odds})
            self._quotes_checked = len(all_odds)
        return arbs

    # ---------- цикл ----------

    def _scan_once(self) -> None:
        started = time.monotonic()
        with self._lock:
            self._scanning = True
        futures = {self._executor.submit(self._fetch, p): p
                   for p in self.parsers}
        done = 0
        arbs: list[Arb] = []
        try:
            for fut in as_completed(futures):
                parser = futures[fut]
                odds = fut.result()
                done += 1
                arbs = self._update_bk(parser.name, odds)
                log.info("Готово %d/%d БК (%s: %d котировок), вилок: %d",
                         done, len(futures), parser.name, len(odds),
                         len(arbs))
        finally:
            with self._lock:
                self._scanning = False

        with self._lock:
            new = [a for a in arbs if a.match_key not in self._prev_keys]
            self._prev_keys = {a.match_key for a in arbs}
            self._scan_count += 1
            total = sum(len(o) for o in self._odds_by_bk.values())

        # Лайв-вилки не пишем в историю: они меняются ежесекундно и быстро
        # засорили бы БД. История — только по прематчу.
        if not self.live:
            db.save_arbs(new)
        log.info("[%s] Цикл %d завершён за %.0f c: %d котировок в памяти, "
                 "%d вилок%s",
                 self.mode, self._scan_count, time.monotonic() - started,
                 total, len(arbs),
                 f" (лучшая {arbs[0].profit_pct:.2f}%)" if arbs else "")

    async def run(self) -> None:
        if self.live:
            await self._run_live()
            return
        log.info("Сканер запущен: режим=%s, период=%s c",
                 self.mode, self.interval)
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await loop.run_in_executor(None, self._scan_once)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка цикла сканирования")
            elapsed = time.monotonic() - started
            wait = max(0.5, self.interval - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    # ---------- лайв: независимое обновление по каждой БК ----------

    @staticmethod
    def _supports_live(parser: BaseParser) -> bool:
        """БК реально отдаёт лайв (переопределила fetch_live_odds)?"""
        return type(parser).fetch_live_odds is not BaseParser.fetch_live_odds

    async def _run_live(self) -> None:
        """Каждая лайв-БК крутится в своём потоке и обновляется как можно
        чаще — медленная БК не тормозит быструю. Как только БК принесла
        свежие кэфы, вилки сразу пересчитываются по всем БК."""
        workers = [p for p in self.parsers if self._supports_live(p)]
        if not workers:
            log.info("Лайв-сканер: ни одна БК не поддерживает лайв — простой")
            await self._stop.wait()
            return
        log.info("Лайв-сканер запущен: БК=%s, пауза между обновлениями %.1f c",
                 ", ".join(p.name for p in workers), LIVE_PER_BK_GAP)
        with self._lock:
            self._scanning = True
        loop = asyncio.get_running_loop()
        tasks = [loop.run_in_executor(self._executor, self._live_worker, p)
                 for p in workers]
        await self._stop.wait()
        for t in tasks:
            try:
                await t
            except Exception:  # noqa: BLE001
                pass

    def _live_worker(self, parser: BaseParser) -> None:
        """Бесконечный цикл обновления одной лайв-БК (в отдельном потоке)."""
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                odds = self._fetch(parser)
                arbs = self._update_bk(parser.name, odds)
                with self._lock:
                    self._scan_count += 1
                log.info("[live] %s: %d котировок за %.1f c, вилок: %d",
                         parser.name, len(odds), time.monotonic() - started,
                         len(arbs))
            except Exception:  # noqa: BLE001
                log.exception("[live] %s: ошибка обновления", parser.name)
            # небольшая пауза, чтобы не долбить сервер БК вплотную
            slept = 0.0
            while slept < LIVE_PER_BK_GAP and not self._stop.is_set():
                time.sleep(0.3)
                slept += 0.3

    def stop(self) -> None:
        self._stop.set()
        self._executor.shutdown(wait=False)
