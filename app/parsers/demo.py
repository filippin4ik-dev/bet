"""Демо-режим: встроенный генератор котировок (Live + прематч).

Все 4 «БК» видят один и тот же набор матчей (общий симулятор рынка),
но выставляют кэфы со своей маржой и шумом. Периодически одна из БК
«ошибается» — завышает кэф на один из исходов, и появляется вилка.
Это позволяет проверить сайт целиком без доступа к реальным БК.
"""
import random
import threading
import time
from datetime import datetime, timedelta

from ..models import KIND_LIVE, KIND_PREMATCH, MatchOdds
from .base import BaseParser

# Матчи только двухисходных видов спорта (без ничьей в основное время)
_LIVE_MATCHES = [
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

_PREMATCH_MATCHES = [
    ("Теннис", "Джокович Н.", "Зверев А."),
    ("Теннис", "Хачанов К.", "Фриц Т."),
    ("Теннис", "Гауфф К.", "Рыбакина Е."),
    ("Баскетбол", "Реал Мадрид", "Барселона"),
    ("Баскетбол", "Панатинаикос", "Олимпиакос"),
    ("Волейбол", "Факел", "Кузбасс"),
    ("Настольный теннис", "Орлов Д.", "Сидоров М."),
    ("Киберспорт (CS2)", "FaZe", "G2"),
    ("Киберспорт (Dota 2)", "Team Falcons", "Tundra"),
    ("Бокс", "Кроуфорд Т.", "Канело А."),
    ("ММА", "Махачев И.", "Царукян А."),
    ("Хоккей (осн. время искл.)", "Ак Барс", "Металлург"),
]

_BOOKMAKERS = ["Winline", "BetBoom", "Fonbet", "Liga Stavok"]


def _gen_start_times() -> dict:
    """Прематч: случайное время начала от +2 до +48 часов."""
    now = datetime.now()
    times = {}
    for m in _PREMATCH_MATCHES:
        start = now + timedelta(hours=random.uniform(2, 48))
        start = start.replace(minute=random.choice((0, 15, 30, 45)), second=0)
        times[m] = start.strftime("%d.%m %H:%M")
    return times


class _MarketSimulator:
    """Общее «истинное» состояние рынка, разделяемое всеми демо-БК."""

    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_tick = 0.0
        all_matches = _LIVE_MATCHES + _PREMATCH_MATCHES
        # p1 — «истинная» вероятность победы первой команды
        self._probs = {m: random.uniform(0.30, 0.70) for m in all_matches}
        self._start_times = _gen_start_times()
        # текущие «ошибки линии»: {матч: (исход 1|2, имя БК, буст кэфа)}
        self._glitches: dict[tuple, tuple] = {}

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
        for m in self._probs:
            # Live-кэфы «дышат» сильнее, прематч — спокойнее
            drift = 0.03 if m in _LIVE_MATCHES else 0.01
            p = self._probs[m] + random.uniform(-drift, drift)
            self._probs[m] = min(0.85, max(0.15, p))

        self._glitches.clear()
        # Независимые «ошибки линии» в Live и в прематче
        for pool, chance in ((_LIVE_MATCHES, 0.6), (_PREMATCH_MATCHES, 0.5)):
            if random.random() < chance:
                match = random.choice(pool)
                self._glitches[match] = (
                    random.choice((1, 2)),
                    random.choice(_BOOKMAKERS),
                    random.uniform(1.08, 1.22),
                )

    def _quote(self, bookmaker: str, match: tuple, kind: str) -> MatchOdds:
        sport, team1, team2 = match
        p1 = self._probs[match]
        margin = random.uniform(1.04, 1.08)  # маржа БК 4–8%
        noise = lambda: random.uniform(0.985, 1.015)  # noqa: E731
        k1 = (1 / (p1 * margin)) * noise()
        k2 = (1 / ((1 - p1) * margin)) * noise()
        glitch = self._glitches.get(match)
        if glitch and glitch[1] == bookmaker:
            if glitch[0] == 1:
                k1 *= glitch[2]
            else:
                k2 *= glitch[2]
        return MatchOdds(
            bookmaker=bookmaker, sport=sport,
            team1=team1, team2=team2,
            k1=round(k1, 2), k2=round(k2, 2),
            kind=kind, start_time=self._start_times.get(match),
        )

    def odds_for(self, bookmaker: str) -> list[MatchOdds]:
        with self._lock:
            self._tick()
            live = [self._quote(bookmaker, m, KIND_LIVE)
                    for m in _LIVE_MATCHES]
            prematch = [self._quote(bookmaker, m, KIND_PREMATCH)
                        for m in _PREMATCH_MATCHES]
            return live + prematch


class DemoParser(BaseParser):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name

    def fetch_odds(self) -> list[MatchOdds]:
        return _MarketSimulator.instance().odds_for(self.name)
