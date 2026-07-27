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

    def __init__(self, bookmaker: str, login: str, password: str,
                 account_id: int | None = None,
                 cookies: str | None = None):
        self.bookmaker = bookmaker
        self.login = login
        self.password = password
        # id аккаунта в базе — нужен коннекторам, умеющим запрашивать
        # SMS/OTP-код через app/otp.py (ретрансляция кода из админки).
        self.account_id = account_id
        # Сырая строка сессионной cookie («name1=v1; name2=v2»), вставленная
        # оператором из своего браузера, где вход уже пройден вручную (см.
        # README «Вход по cookie» и selenium_generic.py). None/"" — нет
        # сохранённой сессии, коннектор идёт по обычному сценарию логина.
        self.cookies = cookies or None

    @abstractmethod
    def get_balance(self) -> float:
        """Баланс аккаунта в рублях. Бросает исключение при ошибке."""

    @abstractmethod
    def place_bet(self, leg: BetLeg) -> BetResult:
        """Пытается поставить leg.stake на leg.outcome_label по кэфу
        не хуже leg.odds. Бросает исключение при фатальной ошибке."""

    def close(self) -> None:
        """Освобождает ресурсы (браузер и т.п.). По умолчанию — ничего."""
