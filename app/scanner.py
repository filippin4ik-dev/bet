"""Фоновый сканер: раз в SCAN_INTERVAL секунд опрашивает все БК
(параллельно, каждая — со своими случайными задержками), ищет вилки
и сохраняет новые находки в SQLite."""
import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import db
from .arbitrage import find_arbs
from .config import SCAN_INTERVAL, SCANNER_MODE
from .models import Arb
from .parsers import get_parsers

log = logging.getLogger("scanner")


class Scanner:
    def __init__(self) -> None:
        self.parsers = get_parsers()
        self._lock = threading.Lock()
        self._arbs: list[Arb] = []
        self._last_scan: float | None = None
        self._scan_count = 0
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
                "last_scan": self._last_scan,
                "events_checked": self._events_checked,
                "quotes_checked": self._quotes_checked,
                "arbs": [a.to_dict() for a in self._arbs],
            }

    # ---------- цикл ----------

    def _scan_once(self) -> None:
        futures = [self._executor.submit(p.safe_fetch) for p in self.parsers]
        all_odds = [o for f in futures for o in f.result()]
        arbs = find_arbs(all_odds)

        with self._lock:
            new = [a for a in arbs if a.match_key not in self._prev_keys]
            self._prev_keys = {a.match_key for a in arbs}
            self._arbs = arbs
            self._last_scan = time.time()
            self._scan_count += 1
            self._events_checked = len({o.event_key for o in all_odds})
            self._quotes_checked = len(all_odds)

        db.save_arbs(new)
        if arbs:
            log.info("Найдено вилок: %d (лучшая %.2f%%)",
                     len(arbs), arbs[0].profit_pct)

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
