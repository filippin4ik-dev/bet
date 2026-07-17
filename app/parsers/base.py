"""Базовый класс парсера: сессия requests, смена User-Agent,
случайные задержки между запросами к одной БК."""
import logging
import random
import time

import requests

from ..config import HTTP_TIMEOUT, REQUEST_DELAY_MIN, REQUEST_DELAY_MAX
from ..models import MarketOdds

log = logging.getLogger("parsers")

USER_AGENTS = [
    # Chrome / Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # Chrome / macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Firefox / Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) "
    "Gecko/20100101 Firefox/127.0",
    # Safari / macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    # Chrome / Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]


class BaseParser:
    """Каждый наследник реализует fetch_odds() -> list[MarketOdds]."""

    name: str = "base"

    def __init__(self) -> None:
        self.session = requests.Session()

    # ---------- защита от блокировок ----------

    def _headers(self) -> dict:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
        }

    def _delay(self) -> None:
        """Случайная пауза 2–5 сек между запросами к одной БК."""
        time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))

    # ---------- сетевые помощники ----------

    def get_json(self, url: str, *, delay: bool = False,
                 timeout: float | None = None, **kwargs):
        if delay:
            self._delay()
        resp = self.session.get(url, headers=self._headers(),
                                timeout=timeout or HTTP_TIMEOUT, **kwargs)
        log.debug("%s GET %s -> %s (%d байт)",
                  self.name, url, resp.status_code, len(resp.content))
        resp.raise_for_status()
        return resp.json()

    def get_html(self, url: str, *, delay: bool = False, **kwargs) -> str:
        if delay:
            self._delay()
        resp = self.session.get(url, headers=self._headers(),
                                timeout=HTTP_TIMEOUT, **kwargs)
        log.info("%s GET %s -> %s (%d байт)",
                 self.name, url, resp.status_code, len(resp.text))
        resp.raise_for_status()
        return resp.text

    # ---------- интерфейс ----------

    def fetch_odds(self) -> list[MarketOdds]:
        raise NotImplementedError

    def fetch_live_odds(self) -> list[MarketOdds]:
        """Лайв-линия (матчи в игре). БК без поддержки лайва — пустой список."""
        return []

    def safe_fetch(self) -> list[MarketOdds]:
        """Обёртка: ошибки одной БК не должны ронять весь цикл сканера."""
        try:
            odds = self.fetch_odds()
            log.info("%s: получено %d котировок", self.name, len(odds))
            return odds
        except Exception as exc:  # noqa: BLE001 — любые сбои сети/разметки
            log.warning("%s: ошибка парсинга: %s", self.name, exc)
            return []

    def safe_fetch_live(self) -> list[MarketOdds]:
        """То же для лайва: сбой одной БК не должен ронять лайв-цикл."""
        try:
            odds = self.fetch_live_odds()
            if odds:
                log.info("%s: получено %d лайв-котировок", self.name, len(odds))
            return odds
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: ошибка лайв-парсинга: %s", self.name, exc)
            return []
