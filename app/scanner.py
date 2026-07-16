"""Фоновый сканер: раз в SCAN_INTERVAL секунд опрашивает все БК
(параллельно, каждая — со своими случайными задержками), ищет вилки
и сохраняет новые находки в SQLite."""
import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import db
from .arbitrage import find_arbs
from .config import SCAN_INTERVAL, SCANNER_MODE
from .models import Arb, MarketOdds
from .parsers import get_parsers

log = logging.getLogger("scanner")


class Scanner:
    def __init__(self) -> None:
        self.parsers = get_parsers()
        self._lock = threading.Lock()
        self._arbs: list[Arb] = []
        # котировки последнего обхода по каждой БК (для /api/odds)
        self._odds_by_bk: dict[str, list[MarketOdds]] = {}
        self._last_scan: float | None = None
        self._scan_count = 0
        self._scanning = False
        self._events_checked = 0   # уникальных событий за последний цикл
        self._quotes_checked = 0   # всего котировок (событие x БК) за цикл
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
        """Все котировки последнего обхода (все найденные матчи)."""
        with self._lock:
            return [o.to_dict()
                    for odds in self._odds_by_bk.values() for o in odds]

    # ---------- цикл ----------

    def _publish(self, odds_by_bk: dict[str, list[MarketOdds]]) -> list[Arb]:
        """Обновляет состояние по уже собранным БК — результаты видны в UI
        сразу, не дожидаясь конца полного цикла (BetBoom обходит виды спорта
        несколько минут)."""
        all_odds = [o for odds in odds_by_bk.values() for o in odds]
        arbs = find_arbs(all_odds)
        with self._lock:
            self._odds_by_bk = dict(odds_by_bk)
            self._arbs = arbs
            self._last_scan = time.time()
            self._events_checked = len({o.event_key for o in all_odds})
            self._quotes_checked = len(all_odds)
        return arbs

    def _scan_once(self) -> None:
        started = time.monotonic()
        with self._lock:
            self._scanning = True
        odds_by_bk: dict[str, list[MarketOdds]] = {}
        futures = {self._executor.submit(p.safe_fetch): p
                   for p in self.parsers}
        try:
            for fut in as_completed(futures):
                parser = futures[fut]
                odds_by_bk[parser.name] = fut.result()
                arbs = self._publish(odds_by_bk)
                log.info("Готово %d/%d БК (%s: %d котировок), вилок пока: %d",
                         len(odds_by_bk), len(futures), parser.name,
                         len(odds_by_bk[parser.name]), len(arbs))
        finally:
            with self._lock:
                self._scanning = False

        arbs = self._publish(odds_by_bk)
        with self._lock:
            new = [a for a in arbs if a.match_key not in self._prev_keys]
            self._prev_keys = {a.match_key for a in arbs}
            self._scan_count += 1

        db.save_arbs(new)
        log.info("Цикл %d завершён за %.0f c: %d котировок, %d вилок%s",
                 self._scan_count, time.monotonic() - started,
                 sum(len(o) for o in odds_by_bk.values()), len(arbs),
                 f" (лучшая {arbs[0].profit_pct:.2f}%)" if arbs else "")

    async def run(self) -> None:
        log.info("Сканер запущен: режим=%s, период=%s c",
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
