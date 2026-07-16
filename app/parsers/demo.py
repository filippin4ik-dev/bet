"""Демо-режим: встроенный генератор котировок.

Все 4 «БК» видят один и тот же набор матчей (общий симулятор рынка),
но выставляют кэфы со своей маржой и шумом. Периодически одна из БК
«ошибается» — завышает кэф на один из исходов, и появляется вилка.
Это позволяет проверить сайт целиком без доступа к реальным БК.
"""
import random
import threading
import time

from ..models import MatchOdds
from .base import BaseParser

# Матчи только двухисходных видов спорта (без ничьей в основное время)
_MATCHES = [
    ("Теннис", "Медведев Д.", "Синнер Я."),
    ("Теннис", "Рублёв А.", "Алькарас К."),
    ("Теннис", "Соболенко А.", "Швёнтек И."),
    ("Баскетбол", "ЦСКА", "Зенит"),
    ("Баскетбол", "УНИКС", "Локомотив-Кубань"),
    ("Волейбол", "Динамо Москва", "Зенит-Казань"),
    ("Волейбол", "Локомотив Нск", "Белогорье"),
    ("Настольный теннис", "Смирнов А.", "Ковалёв П."),
    ("Киберспорт (CS2)", "Team Spirit", "NaVi"),
    ("Киберспорт (Dota 2)", "Virtus.pro", "BetBoom Team"),
    ("Бокс", "Иванов С.", "Петров Н."),
    ("ММА", "Волков А.", "Ган С."),
]


class _MarketSimulator:
    """Общее «истинное» состояние рынка, разделяемое всеми демо-БК."""

    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_tick = 0.0
        # p1 — «истинная» вероятность победы первой команды
        self._probs = {m: random.uniform(0.30, 0.70) for m in _MATCHES}
        # текущая «ошибка линии»: (матч, исход 1|2, имя БК, буст кэфа)
        self._glitch: tuple | None = None

    @classmethod
    def instance(cls) -> "_MarketSimulator":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _tick(self) -> None:
        """Дрейф вероятностей ~раз в цикл + случайные «ошибки линии»."""
        now = time.monotonic()
        if now - self._last_tick < 8:
            return
        self._last_tick = now
        for m in _MATCHES:
            p = self._probs[m] + random.uniform(-0.03, 0.03)
            self._probs[m] = min(0.85, max(0.15, p))
        # С вероятностью ~60% в цикле существует хотя бы одна вилка
        if random.random() < 0.6:
            match = random.choice(_MATCHES)
            outcome = random.choice((1, 2))
            bookmaker = random.choice(
                ["Winline", "BetBoom", "Fonbet", "Liga Stavok"])
            boost = random.uniform(1.08, 1.22)
            self._glitch = (match, outcome, bookmaker, boost)
        else:
            self._glitch = None

    def odds_for(self, bookmaker: str) -> list[MatchOdds]:
        with self._lock:
            self._tick()
            result = []
            for match in _MATCHES:
                sport, team1, team2 = match
                p1 = self._probs[match]
                margin = random.uniform(1.04, 1.08)  # маржа БК 4–8%
                noise = lambda: random.uniform(0.985, 1.015)  # noqa: E731
                k1 = (1 / (p1 * margin)) * noise()
                k2 = (1 / ((1 - p1) * margin)) * noise()
                if self._glitch and self._glitch[0] == match \
                        and self._glitch[2] == bookmaker:
                    if self._glitch[1] == 1:
                        k1 *= self._glitch[3]
                    else:
                        k2 *= self._glitch[3]
                result.append(MatchOdds(
                    bookmaker=bookmaker, sport=sport,
                    team1=team1, team2=team2,
                    k1=round(k1, 2), k2=round(k2, 2),
                ))
            return result


class DemoParser(BaseParser):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name

    def fetch_odds(self) -> list[MatchOdds]:
        return _MarketSimulator.instance().odds_for(self.name)
