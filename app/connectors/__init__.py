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
коннектор для указанной БК.
"""
from .base import BetLeg, BetResult, BookmakerConnector
from .mock import MockConnector
from .selenium_generic import BOOKMAKER_CONNECTORS


def get_connector(bookmaker: str, login: str, password: str) -> BookmakerConnector:
    cls = BOOKMAKER_CONNECTORS.get(bookmaker)
    if cls is None:
        return MockConnector(bookmaker, login, password)
    return cls(bookmaker, login, password)


__all__ = [
    "BetLeg", "BetResult", "BookmakerConnector", "MockConnector",
    "get_connector",
]
