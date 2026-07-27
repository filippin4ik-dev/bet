"""Диагностика ОДНОГО аккаунта БК (баланс) для ручной проверки коннектора.

Данные аккаунта берутся ТОЛЬКО из переменных окружения — никогда из
аргументов командной строки или файлов в репозитории, чтобы логин/пароль
не осели в истории команд или git:

  TEST_BOOKMAKER   — «Winline» / «BetBoom» / «Fonbet» / «Liga Stavok» / «bc.game»
  TEST_BK_LOGIN    — логин/телефон/email
  TEST_BK_PASSWORD — пароль

Задавайте их через секреты окружения (Cursor Dashboard → Cloud Agents →
Secrets, или .env/systemd на своём сервере) — не через аргументы скрипта.

Запуск:  python3 -m app.diagnose_account

Пароль в вывод НЕ печатается. Скрипт делает попытку get_balance() —
это экспериментальный функционал (см. предупреждения в
app/connectors/selenium_generic.py), НЕ проверенный на живых аккаунтах.
Постановка ставки здесь НЕ вызывается.
"""
import logging
import os
import sys

from .connectors import get_connector

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> int:
    bookmaker = os.getenv("TEST_BOOKMAKER", "").strip()
    login = os.getenv("TEST_BK_LOGIN", "").strip()
    password = os.getenv("TEST_BK_PASSWORD", "").strip()

    if not bookmaker or not login or not password:
        print("Не заданы TEST_BOOKMAKER / TEST_BK_LOGIN / TEST_BK_PASSWORD "
              "в окружении — см. докстринг этого файла.")
        return 1

    print(f"БК: {bookmaker}  логин: {login[:2]}***  "
          f"(пароль не выводится)")
    connector = get_connector(bookmaker, login, password)
    try:
        balance = connector.get_balance()
        print(f"УСПЕХ: баланс = {balance:.2f}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"ОШИБКА чтения баланса: {exc}")
        print("Это ожидаемо для неверифицированных селекторов — см. "
              "app/connectors/selenium_generic.py::SELECTORS и поправьте "
              "их под актуальную вёрстку личного кабинета этой БК.")
        return 2
    finally:
        connector.close()


if __name__ == "__main__":
    sys.exit(main())
