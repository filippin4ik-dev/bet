"""Базовые типы коннекторов к аккаунтам БК."""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class BetLeg:
    """Одна нога вилки — то, что нужно поставить в конкретной БК."""

    bookmaker: str
    outcome_label: str     # «П1», «X», «ТБ 2.5» — что показываем пользователю
    market_key: str
    team1: str
    team2: str
    odds: float             # минимальный кэф, ниже которого ставку не делать
    stake: float
    url: str | None = None  # deep-ссылка на страницу события у БК


@dataclass
class BetResult:
    ok: bool
    message: str
    placed_odds: float | None = None
    placed_stake: float | None = None


class BookmakerConnector(ABC):
    """Интерфейс коннектора к личному кабинету одной БК."""

    def __init__(self, bookmaker: str, login: str, password: str):
        self.bookmaker = bookmaker
        self.login = login
        self.password = password

    @abstractmethod
    def get_balance(self) -> float:
        """Баланс аккаунта в рублях. Бросает исключение при ошибке."""

    @abstractmethod
    def place_bet(self, leg: BetLeg) -> BetResult:
        """Пытается поставить leg.stake на leg.outcome_label по кэфу
        не хуже leg.odds. Бросает исключение при фатальной ошибке."""

    def close(self) -> None:
        """Освобождает ресурсы (браузер и т.п.). По умолчанию — ничего."""
