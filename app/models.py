"""Модели данных."""
from dataclasses import dataclass, field

# Тип рынка: live — матч идёт, prematch — матч ещё не начался
KIND_LIVE = "live"
KIND_PREMATCH = "prematch"


@dataclass
class MatchOdds:
    """Коэффициенты одной БК на один матч (только П1/П2, без ничьей)."""

    bookmaker: str      # название БК: Winline / BetBoom / Fonbet / Liga Stavok
    sport: str          # вид спорта (теннис, баскетбол и т.п.)
    team1: str
    team2: str
    k1: float           # коэффициент на победу team1
    k2: float           # коэффициент на победу team2
    kind: str = KIND_LIVE          # live | prematch
    start_time: str | None = None  # время начала матча (для прематча)

    @property
    def match_key(self) -> str:
        """Ключ для сопоставления одного матча между разными БК."""
        return f"{self.kind}|{self.sport}|{_norm(self.team1)}|{_norm(self.team2)}"


def _norm(name: str) -> str:
    return " ".join(name.lower().replace("ё", "е").split())


@dataclass
class Arb:
    """Найденная двухисходная вилка."""

    match_key: str
    sport: str
    team1: str
    team2: str
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
            "k1_max": self.k1_max,
            "k1_bookmaker": self.k1_bookmaker,
            "k2_max": self.k2_max,
            "k2_bookmaker": self.k2_bookmaker,
            "margin": round(self.margin, 4),
            "profit_pct": round(self.profit_pct, 2),
            "stakes": self.stakes,
        }
