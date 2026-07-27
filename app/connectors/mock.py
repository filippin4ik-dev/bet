"""Имитационный коннектор: детерминированный «баланс» и «успешная»
ставка без единого сетевого запроса к БК.

Используется:
- как безопасный дефолт для БК без реальной Selenium-автоматизации;
- ВСЕГДА при AUTOBET_DRY_RUN=1 (даже для БК, у которых есть реальный
  коннектор) — так весь конвейер (расчёт лимитов, распределение стейков,
  журнал ставок, фронтенд) можно проверить, не трогая реальные аккаунты.
"""
import hashlib
import random

from .base import BetLeg, BetResult, BookmakerConnector


class MockConnector(BookmakerConnector):
    """Баланс — псевдослучайное, но стабильное для (БК, логин) число.

    Стабильность важна, чтобы UI не «мигал» разными суммами при каждом
    обновлении — баланс меняется только при явном refresh."""

    def get_balance(self) -> float:
        seed = hashlib.sha256(
            f"{self.bookmaker}:{self.login}".encode("utf-8")).hexdigest()
        rnd = random.Random(seed)
        return round(rnd.uniform(3000, 25000), 2)

    def place_bet(self, leg: BetLeg) -> BetResult:
        return BetResult(
            ok=True,
            message=(f"[ИМИТАЦИЯ] ставка {leg.stake:.2f}₽ на "
                     f"«{leg.outcome_label}» по кэфу {leg.odds} "
                     f"в {self.bookmaker} — реальный запрос НЕ отправлялся"),
            placed_odds=leg.odds,
            placed_stake=leg.stake,
        )
