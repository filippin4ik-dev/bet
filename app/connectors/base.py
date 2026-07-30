"""Базовые типы коннекторов к аккаунтам БК."""
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

# Способ входа в личный кабинет. У российских БК логин и телефон — это
# РАЗНЫЕ реквизиты и, как правило, разные вкладки формы входа с разными
# полями (подтверждено на Winline: вкладка «Телефон» рисует
# input[name='auth-phone-base'], вкладка «Логин» — input[name='auth-
# username']). Угадать способ по самой строке нельзя: клубная карта у
# Melbet — это тоже одни цифры, и от номера телефона её не отличить.
# Поэтому способ выбирает оператор при добавлении аккаунта.
LOGIN_BY_PHONE = "phone"
LOGIN_BY_LOGIN = "login"
LOGIN_TYPES = (LOGIN_BY_PHONE, LOGIN_BY_LOGIN)

LOGIN_TYPE_NAMES = {
    LOGIN_BY_PHONE: "по телефону",
    LOGIN_BY_LOGIN: "по логину",
}


def normalize_login_type(value: str | None,
                         default: str = LOGIN_BY_PHONE) -> str:
    """Приводит способ входа к одному из известных.

    Неизвестное значение — это либо аккаунт из базы, заведённый до
    появления выбора, либо опечатка в переменной окружения: и в том, и в
    другом случае берём способ по умолчанию, а не падаем."""
    value = (value or "").strip().lower()
    return value if value in LOGIN_TYPES else default


def national_phone(value: str) -> str:
    """Российский номер в том виде, в каком его ждёт форма входа: 10 цифр
    без кода страны.

    У полей телефона префикс «+7» стоит ОТДЕЛЬНО от самого input
    (подтверждено на Winline: рядом с input[name='auth-phone-base'] лежит
    отключённый input[name='auth-phone-code'] со значением «+7»). Введёшь
    полный номер — маска сдвинет цифры, и БК получит чужой номер."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits[0] in "78":
        return digits[1:]
    return digits or (value or "").strip()


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
                 cookies: str | None = None,
                 login_type: str = LOGIN_BY_PHONE):
        self.bookmaker = bookmaker
        self.login = login
        self.password = password
        # Чем оператор входит в этот аккаунт: номером телефона или
        # логином (см. LOGIN_TYPES выше). От этого зависит и вкладка
        # формы входа, и поле, и нужно ли отрезать код страны.
        self.login_type = normalize_login_type(login_type)
        # id аккаунта в базе — нужен коннекторам, умеющим запрашивать
        # SMS/OTP-код через app/otp.py (ретрансляция кода из админки).
        self.account_id = account_id
        # Сырая строка сессионной cookie («name1=v1; name2=v2»), вставленная
        # оператором из своего браузера, где вход уже пройден вручную (см.
        # README «Вход по cookie» и selenium_generic.py). None/"" — нет
        # сохранённой сессии, коннектор идёт по обычному сценарию логина.
        self.cookies = cookies or None

    @property
    def username_value(self) -> str:
        """Что именно вписывать в поле имени пользователя на форме."""
        if self.login_type == LOGIN_BY_PHONE:
            return national_phone(self.login)
        return (self.login or "").strip()

    @abstractmethod
    def get_balance(self) -> float:
        """Баланс аккаунта в рублях. Бросает исключение при ошибке."""

    @abstractmethod
    def place_bet(self, leg: BetLeg) -> BetResult:
        """Пытается поставить leg.stake на leg.outcome_label по кэфу
        не хуже leg.odds. Бросает исключение при фатальной ошибке."""

    def close(self) -> None:
        """Освобождает ресурсы (браузер и т.п.). По умолчанию — ничего."""
