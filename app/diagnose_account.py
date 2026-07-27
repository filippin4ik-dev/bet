"""Диагностика аккаунтов БК (баланс) для ручной проверки коннекторов.

Данные аккаунта берутся ТОЛЬКО из переменных окружения — никогда из
аргументов командной строки или файлов в репозитории, чтобы логин/пароль
не осели в истории команд, git или чате с агентом. Задавайте их через
секреты окружения (Cursor Dashboard → Cloud Agents → Secrets, или
.env/systemd на своём сервере).

Поддерживаются ДВА способа задать проверяемые аккаунты:

1. Один аккаунт (быстрый путь):
     TEST_BOOKMAKER=Fonbet
     TEST_BK_LOGIN=...
     TEST_BK_PASSWORD=...

2. Несколько аккаунтов сразу — по паре переменных `<БК>_TEST_LOGIN` /
   `<БК>_TEST_PASSWORD` на каждую БК из списка ниже (префикс — имя БК
   без пробелов/точек, в верхнем регистре):
     FONBET_TEST_LOGIN / FONBET_TEST_PASSWORD
     BETBOOM_TEST_LOGIN / BETBOOM_TEST_PASSWORD
     WINLINE_TEST_LOGIN / WINLINE_TEST_PASSWORD
     LIGA_STAVOK_TEST_LOGIN / LIGA_STAVOK_TEST_PASSWORD
     BCGAME_TEST_LOGIN / BCGAME_TEST_PASSWORD
   Заданы (даже частично) — скрипт проверит баланс по каждой такой БК.

Запуск:  python3 -m app.diagnose_account

Пароли в вывод НЕ печатаются. Скрипт делает только get_balance() —
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

BOOKMAKERS = ["Winline", "BetBoom", "Fonbet", "Liga Stavok", "bc.game"]


def _env_prefix(bookmaker: str) -> str:
    return bookmaker.upper().replace(" ", "_").replace(".", "").replace("-", "_")


def _collect_accounts() -> list[tuple[str, str, str]]:
    accounts: list[tuple[str, str, str]] = []

    single_bk = os.getenv("TEST_BOOKMAKER", "").strip()
    single_login = os.getenv("TEST_BK_LOGIN", "").strip()
    single_pass = os.getenv("TEST_BK_PASSWORD", "").strip()
    if single_bk and single_login and single_pass:
        accounts.append((single_bk, single_login, single_pass))

    for bk in BOOKMAKERS:
        prefix = _env_prefix(bk)
        login = os.getenv(f"{prefix}_TEST_LOGIN", "").strip()
        password = os.getenv(f"{prefix}_TEST_PASSWORD", "").strip()
        if login and password and not any(a[0] == bk for a in accounts):
            accounts.append((bk, login, password))

    return accounts


def check_one(bookmaker: str, login: str, password: str) -> bool:
    print(f"\n=== {bookmaker} ===")
    print(f"логин: {login[:2]}***  (пароль не выводится)")
    connector = get_connector(bookmaker, login, password)
    try:
        balance = connector.get_balance()
        print(f"УСПЕХ: баланс = {balance:.2f}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"ОШИБКА чтения баланса: {exc}")
        print("Это ожидаемо для неверифицированных селекторов — см. "
              "app/connectors/selenium_generic.py::SELECTORS и поправьте "
              "их под актуальную вёрстку личного кабинета этой БК.")
        return False
    finally:
        connector.close()


def main() -> int:
    accounts = _collect_accounts()
    if not accounts:
        print("Не заданы данные ни одного тестового аккаунта в окружении — "
              "см. докстринг этого файла (TEST_BOOKMAKER/TEST_BK_LOGIN/"
              "TEST_BK_PASSWORD или <БК>_TEST_LOGIN/<БК>_TEST_PASSWORD).")
        return 1

    results = [check_one(bk, login, password)
              for bk, login, password in accounts]
    ok = sum(results)
    print(f"\n--- Итого: {ok}/{len(results)} успешно ---")
    return 0 if ok == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
