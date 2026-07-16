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
from .arbitrage import find_arbs
from .config import ODDS_TTL, SCAN_INTERVAL, SCANNER_MODE
from .models import Arb, MarketOdds
from .parsers import get_parsers

log = logging.getLogger("scanner")


class Scanner:
    def __init__(self) -> None:
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
        self._executor = ThreadPoolExecutor(max_workers=len(self.parsers))
        self._stop = asyncio.Event()

    # ---------- публичное состояние ----------

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "mode": SCANNER_MODE,
                "scan_interval": SCAN_INTERVAL,
                "scan_count": self._scan_count,
                "scanning": self._scanning,
                "last_scan": self._last_scan,
                "events_checked": self._events_checked,
                "quotes_checked": self._quotes_checked,
                "bookmakers": {bk: len(o)
                               for bk, o in self._odds_by_bk.items()},
                "arbs": [a.to_dict() for a in self._arbs],
            }

    def odds_snapshot(self) -> list[dict]:
        """Все котировки, находящиеся сейчас в памяти (все найденные матчи)."""
        with self._lock:
            return [o.to_dict()
                    for odds in self._odds_by_bk.values() for o in odds]

    # ---------- обновление состояния ----------

    def _prune_locked(self, now: float) -> None:
        """Убирает начавшиеся матчи и протухшие данные БК. Под _lock."""
        for bk in list(self._odds_by_bk):
            if now - self._fetched_at.get(bk, 0) > ODDS_TTL:
                # БК давно не отвечает — её кэфы больше нельзя считать
                # актуальными, на них нельзя ставить
                log.info("Котировки %s устарели (> %d с) — убраны",
                         bk, int(ODDS_TTL))
                del self._odds_by_bk[bk]
                self._fetched_at.pop(bk, None)
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
        futures = {self._executor.submit(p.safe_fetch): p
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

        db.save_arbs(new)
        log.info("Цикл %d завершён за %.0f c: %d котировок в памяти, "
                 "%d вилок%s",
                 self._scan_count, time.monotonic() - started, total,
                 len(arbs),
                 f" (лучшая {arbs[0].profit_pct:.2f}%)" if arbs else "")

    async def run(self) -> None:
        log.info("Сканер запущен: режим=%s (только прематч), период=%s c",
                 SCANNER_MODE, SCAN_INTERVAL)
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await loop.run_in_executor(None, self._scan_once)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка цикла сканирования")
            elapsed = time.monotonic() - started
            wait = max(0.5, SCAN_INTERVAL - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
        self._executor.shutdown(wait=False)
