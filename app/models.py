"""Модели данных.

Работаем с ЛЮБЫМ двухисходным рынком, а не только с победителем матча:
- Победитель (П1 / П2, без ничьей);
- Тотал больше/меньше на пол-линии (напр. «больше 2.5» / «меньше 2.5» —
  сколько будет убийств, карт, геймов, очков и т.п.);
- любой другой рынок ровно с двумя взаимоисключающими исходами.

Вилка всегда считается по одной формуле: 1/К1_max + 1/К2_max < 1,
где К1 — макс. кэф на исход 1, К2 — макс. кэф на исход 2 (у разных БК).
"""
from dataclasses import dataclass, field

# Тип рынка по времени: live — матч идёт, prematch — матч ещё не начался
KIND_LIVE = "live"
KIND_PREMATCH = "prematch"


@dataclass
class MarketOdds:
    """Коэффициенты одной БК на один двухисходный рынок одного события."""

    bookmaker: str          # Winline / BetBoom / Fonbet / Liga Stavok
    sport: str              # вид спорта (+ дочерняя роспись, если есть)
    team1: str
    team2: str
    market: str             # человекочитаемое имя рынка: «Победитель», «Тотал 2.5»
    market_key: str         # ключ рынка для сопоставления между БК
    outcome1: str           # метка исхода 1: «П1», «ТБ 2.5»
    outcome2: str           # метка исхода 2: «П2», «ТМ 2.5»
    k1: float               # коэффициент на исход 1
    k2: float               # коэффициент на исход 2
    kind: str = KIND_LIVE          # live | prematch
    start_time: str | None = None  # время начала (для прематча)

    @property
    def event_key(self) -> str:
        """Ключ события (без рынка) — для подсчёта числа событий."""
        return f"{self.kind}|{self.sport}|{_norm(self.team1)}|{_norm(self.team2)}"

    @property
    def match_key(self) -> str:
        """Ключ конкретного рынка конкретного события: по нему кэфы разных
        БК сопоставляются между собой (одинаковый матч И одинаковый рынок)."""
        return f"{self.event_key}|{self.market_key}"

    def to_dict(self) -> dict:
        return {
            "bookmaker": self.bookmaker,
            "sport": self.sport,
            "match": f"{self.team1} — {self.team2}",
            "market": self.market,
            "outcome1": self.outcome1,
            "outcome2": self.outcome2,
            "k1": self.k1,
            "k2": self.k2,
            "kind": self.kind,
            "start_time": self.start_time,
        }


def _norm(name: str) -> str:
    return " ".join(name.lower().replace("ё", "е").split())


@dataclass
class Arb:
    """Найденная двухисходная вилка."""

    match_key: str
    sport: str
    team1: str
    team2: str
    market: str
    outcome1: str
    outcome2: str
    k1_max: float
    k1_bookmaker: str
    k2_max: float
    k2_bookmaker: str
    margin: float               # 1/К1 + 1/К2 (< 1 — вилка)
    profit_pct: float           # доходность, %
    kind: str = KIND_LIVE       # live | prematch
    start_time: str | None = None
    stakes: dict = field(default_factory=dict)  # {банк: {...}}

    def to_dict(self) -> dict:
        return {
            "match_key": self.match_key,
            "kind": self.kind,
            "start_time": self.start_time,
            "sport": self.sport,
            "match": f"{self.team1} — {self.team2}",
            "team1": self.team1,
            "team2": self.team2,
            "market": self.market,
            "outcome1": self.outcome1,
            "outcome2": self.outcome2,
            "k1_max": self.k1_max,
            "k1_bookmaker": self.k1_bookmaker,
            "k2_max": self.k2_max,
            "k2_bookmaker": self.k2_bookmaker,
            "margin": round(self.margin, 4),
            "profit_pct": round(self.profit_pct, 2),
            "stakes": self.stakes,
        }
