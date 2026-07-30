"""Коннекторы к личным кабинетам БК: баланс аккаунта и (опционально)
размещение ставки.

Реальная автоматизация браузера для входа/чтения баланса/постановки
ставки у каждой БК — это отдельная, легко ломающаяся под изменения
вёрстки задача, которую невозможно надёжно реализовать и проверить без
живого залогиненного аккаунта (см. предупреждения в selenium_generic.py
и README). Поэтому здесь два вида коннекторов:

- MockConnector — имитация (детерминированный «баланс» и «успешная»
  ставка), включена по умолчанию для аккаунтов без реальной
  автоматизации и всегда используется в режиме dry-run;
- SeleniumGenericConnector (и наследники в selenium_generic.py) —
  best-effort реальная автоматизация через Selenium, ЭКСПЕРИМЕНТАЛЬНАЯ,
  требует проверки CSS-селекторов оператором перед использованием.

get_connector(bookmaker, login, password) возвращает подходящий
коннектор для указанной БК. Опциональный `cookies` — сырая строка
сессионной cookie («name1=v1; name2=v2»), вставленная оператором из
СВОЕГО браузера (где вход уже пройден вручную, в т.ч. капча/СМС) — см.
раздел «Вход по cookie» README и docstring selenium_generic.py.

`login_type` — чем оператор входит в этот аккаунт: номером телефона
(`phone`) или логином (`login`). Это не косметика: у БК это разные
вкладки формы входа с разными полями, а телефон ещё и вводится без кода
страны. Какие способы принимает конкретная БК, говорит `login_types()`.
"""
from .base import (LOGIN_BY_LOGIN, LOGIN_BY_PHONE, LOGIN_TYPE_NAMES,
                   LOGIN_TYPES, BetLeg, BetResult, BookmakerConnector,
                   national_phone, normalize_login_type)
from .mock import MockConnector
from .selenium_generic import (BOOKMAKER_CONNECTORS, default_login_type,
                               env_prefix, login_types, resolve_login_type)


def get_connector(bookmaker: str, login: str, password: str,
                  account_id: int | None = None,
                  cookies: str | None = None,
                  login_type: str | None = None) -> BookmakerConnector:
    login_type = resolve_login_type(bookmaker, login_type)
    cls = BOOKMAKER_CONNECTORS.get(bookmaker, MockConnector)
    return cls(bookmaker, login, password, account_id=account_id,
               cookies=cookies, login_type=login_type)


__all__ = [
    "LOGIN_BY_LOGIN", "LOGIN_BY_PHONE", "LOGIN_TYPES", "LOGIN_TYPE_NAMES",
    "BetLeg", "BetResult", "BookmakerConnector", "MockConnector",
    "default_login_type", "env_prefix", "get_connector", "login_types",
    "national_phone", "normalize_login_type", "resolve_login_type",
]
