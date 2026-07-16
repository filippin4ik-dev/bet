"""Демо-режим: встроенный генератор котировок (Live + прематч).

Симулирует полную линию БК: десятки событий по всем двухисходным видам
спорта, включая дочерние рынки (сеты/карты). События ротируются: старые
завершаются, новые появляются — как в реальной линии. Все 4 «БК» видят
один и тот же рынок, но выставляют кэфы со своей маржой и шумом.
Периодически одна из БК «ошибается» — появляется вилка. Это позволяет
проверить сайт целиком без доступа к реальным БК.
"""
import itertools
import random
import threading
import time
from datetime import datetime, timedelta

from ..models import KIND_LIVE, KIND_PREMATCH, MatchOdds
from .base import BaseParser

_BOOKMAKERS = ["Winline", "BetBoom", "Fonbet", "Liga Stavok"]

# Пулы участников по двухисходным видам спорта (без ничьей в осн. время)
_TEAM_POOLS = {
    "Теннис": [
        "Медведев Д.", "Синнер Я.", "Рублёв А.", "Алькарас К.", "Джокович Н.",
        "Зверев А.", "Хачанов К.", "Фриц Т.", "Соболенко А.", "Швёнтек И.",
        "Гауфф К.", "Рыбакина Е.", "Руне Х.", "Циципас С.",
    ],
    "Баскетбол": [
        "ЦСКА", "Зенит", "УНИКС", "Локомотив-Кубань", "Реал Мадрид",
        "Барселона", "Панатинаикос", "Олимпиакос", "Фенербахче", "Монако",
    ],
    "Волейбол": [
        "Динамо Москва", "Зенит-Казань", "Локомотив Нск", "Белогорье",
        "Факел", "Кузбасс", "Динамо-ЛО", "Урал",
    ],
    "Настольный теннис": [
        "Смирнов А.", "Ковалёв П.", "Орлов Д.", "Сидоров М.",
        "Козлов И.", "Ефимов В.", "Тарасов Н.", "Фомин Г.",
    ],
    "Киберспорт (CS2)": [
        "Team Spirit", "NaVi", "FaZe", "G2", "Vitality", "MOUZ",
        "Cloud9", "Astralis",
    ],
    "Киберспорт (Dota 2)": [
        "Virtus.pro", "BetBoom Team", "Team Falcons", "Tundra",
        "Team Liquid", "Gaimin Gladiators",
    ],
    "Бокс": ["Иванов С.", "Петров Н.", "Кроуфорд Т.", "Канело А.",
             "Бивол Д.", "Бетербиев А."],
    "ММА": ["Волков А.", "Ган С.", "Махачев И.", "Царукян А.",
            "Пантожа А.", "Двалишвили М."],
}

# Дочерние рынки по видам спорта (проверяем и их — это тоже П1/П2)
_SUB_MARKETS = {
    "Теннис": ["2-й сет"],
    "Волейбол": ["3-я партия"],
    "Настольный теннис": ["4-й сет"],
    "Киберспорт (CS2)": ["Карта 2"],
    "Киберспорт (Dota 2)": ["Карта 2"],
}

_LIVE_TARGET = 30       # сколько live-событий держим в линии
_PREMATCH_TARGET = 40   # сколько прематч-событий держим в линии
_MAX_GLITCHES = 3       # максимум «ошибок линии» в каждом рынке за цикл


def _random_start() -> str:
    start = datetime.now() + timedelta(hours=random.uniform(2, 72))
    start = start.replace(minute=random.choice((0, 15, 30, 45)), second=0)
    return start.strftime("%d.%m %H:%M")


class _Event:
    __slots__ = ("sport", "team1", "team2", "p1", "start_time", "sub_market")

    def __init__(self, sport: str, team1: str, team2: str,
                 start_time: str | None = None,
                 sub_market: str | None = None) -> None:
        self.sport = sport
        self.team1 = team1
        self.team2 = team2
        self.p1 = random.uniform(0.25, 0.75)
        self.start_time = start_time
        self.sub_market = sub_market

    @property
    def display_sport(self) -> str:
        if self.sub_market:
            return f"{self.sport} · {self.sub_market}"
        return self.sport


def _make_events(n: int, prematch: bool) -> list[_Event]:
    """Генерирует n событий из пулов команд + дочерние рынки для части из них."""
    pairs = []
    for sport, teams in _TEAM_POOLS.items():
        for t1, t2 in itertools.combinations(teams, 2):
            pairs.append((sport, t1, t2))
    random.shuffle(pairs)

    events: list[_Event] = []
    for sport, t1, t2 in pairs[:n]:
        start = _random_start() if prematch else None
        events.append(_Event(sport, t1, t2, start))
        # Для live-событий добавляем дочерние рынки (сет/карта) — их тоже проверяем
        if not prematch and sport in _SUB_MARKETS and random.random() < 0.5:
            events.append(_Event(sport, t1, t2, None,
                                 random.choice(_SUB_MARKETS[sport])))
    return events


class _MarketSimulator:
    """Общее «истинное» состояние рынка, разделяемое всеми демо-БК."""

    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_tick = 0.0
        self._live = _make_events(_LIVE_TARGET, prematch=False)
        self._prematch = _make_events(_PREMATCH_TARGET, prematch=True)
        # текущие «ошибки линии»: {id(событие): (исход 1|2, имя БК, буст)}
        self._glitches: dict[int, tuple] = {}

    @classmethod
    def instance(cls) -> "_MarketSimulator":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _rotate(self) -> None:
        """Часть событий «завершается», на их место приходят новые."""
        if random.random() < 0.3 and self._live:
            self._live.pop(random.randrange(len(self._live)))
            self._live.extend(_make_events(1, prematch=False))
        if random.random() < 0.2 and self._prematch:
            self._prematch.pop(random.randrange(len(self._prematch)))
            self._prematch.extend(_make_events(1, prematch=True))

    def _tick(self) -> None:
        """Дрейф вероятностей ~раз в цикл + случайные «ошибки линии»."""
        now = time.monotonic()
        if now - self._last_tick < 8:
            return
        self._last_tick = now
        self._rotate()

        for ev in self._live:
            ev.p1 = min(0.9, max(0.1, ev.p1 + random.uniform(-0.04, 0.04)))
        for ev in self._prematch:
            ev.p1 = min(0.9, max(0.1, ev.p1 + random.uniform(-0.01, 0.01)))

        self._glitches.clear()
        for pool in (self._live, self._prematch):
            for ev in random.sample(pool, min(len(pool), _MAX_GLITCHES)):
                if random.random() < 0.35:
                    self._glitches[id(ev)] = (
                        random.choice((1, 2)),
                        random.choice(_BOOKMAKERS),
                        random.uniform(1.08, 1.22),
                    )

    def _quote(self, bookmaker: str, ev: _Event, kind: str) -> MatchOdds:
        margin = random.uniform(1.04, 1.08)  # маржа БК 4–8%
        noise = lambda: random.uniform(0.985, 1.015)  # noqa: E731
        k1 = (1 / (ev.p1 * margin)) * noise()
        k2 = (1 / ((1 - ev.p1) * margin)) * noise()
        glitch = self._glitches.get(id(ev))
        if glitch and glitch[1] == bookmaker:
            if glitch[0] == 1:
                k1 *= glitch[2]
            else:
                k2 *= glitch[2]
        return MatchOdds(
            bookmaker=bookmaker, sport=ev.display_sport,
            team1=ev.team1, team2=ev.team2,
            k1=round(k1, 2), k2=round(k2, 2),
            kind=kind, start_time=ev.start_time,
        )

    def odds_for(self, bookmaker: str) -> list[MatchOdds]:
        with self._lock:
            self._tick()
            live = [self._quote(bookmaker, ev, KIND_LIVE)
                    for ev in self._live]
            prematch = [self._quote(bookmaker, ev, KIND_PREMATCH)
                        for ev in self._prematch]
            return live + prematch


class DemoParser(BaseParser):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name

    def fetch_odds(self) -> list[MatchOdds]:
        return _MarketSimulator.instance().odds_for(self.name)
