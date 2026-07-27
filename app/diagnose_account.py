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

Опционально (для обеих схем) — сессионная cookie вместо/в дополнение к
сценарию логина (см. README «Вход по cookie» и app/connectors/
selenium_generic.py): `TEST_BK_COOKIES` (схема 1) или `<БК>_TEST_COOKIES`
(схема 2), формат — как заголовок Cookie из DevTools: "name1=v1; name2=v2".
Полезно, когда автологин у БК спотыкается о капчу (BetBoom) или анти-бот
блокирует IP этой машины (Fonbet) — залогиньтесь в СВОЁМ браузере руками
и вставьте её cookie сюда, минуя форму входа.

Запуск:  python3 -m app.diagnose_account

Пароли и cookie в вывод НЕ печатаются. Скрипт делает только get_balance()
— это экспериментальный функционал (см. предупреждения в
app/connectors/selenium_generic.py), проверенный не для всех БК.
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


def _collect_accounts() -> list[tuple[str, str, str, str]]:
    accounts: list[tuple[str, str, str, str]] = []

    single_bk = os.getenv("TEST_BOOKMAKER", "").strip()
    single_login = os.getenv("TEST_BK_LOGIN", "").strip()
    single_pass = os.getenv("TEST_BK_PASSWORD", "").strip()
    single_cookies = os.getenv("TEST_BK_COOKIES", "").strip()
    if single_bk and single_login and single_pass:
        accounts.append((single_bk, single_login, single_pass, single_cookies))

    for bk in BOOKMAKERS:
        prefix = _env_prefix(bk)
        login = os.getenv(f"{prefix}_TEST_LOGIN", "").strip()
        password = os.getenv(f"{prefix}_TEST_PASSWORD", "").strip()
        cookies = os.getenv(f"{prefix}_TEST_COOKIES", "").strip()
        if login and password and not any(a[0] == bk for a in accounts):
            accounts.append((bk, login, password, cookies))

    return accounts


def check_one(bookmaker: str, login: str, password: str,
              cookies: str = "") -> bool:
    print(f"\n=== {bookmaker} ===")
    print(f"логин: {login[:2]}***  (пароль не выводится)"
          + (", cookie: задана (не выводится)" if cookies else ""))
    connector = get_connector(bookmaker, login, password,
                              cookies=cookies or None)
    try:
        balance = connector.get_balance()
        print(f"УСПЕХ: баланс = {balance:.2f}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"ОШИБКА чтения баланса: {exc}")
        print("Смотрите текст ошибки выше — это может быть: неверный "
              "селектор (см. app/connectors/selenium_generic.py::SELECTORS "
              "и докстринг файла — там статус по каждой БК), капча на "
              "форме входа (автоматически не проходится) или анти-бот-"
              "блок IP этой машины (подтверждено на Fonbet — см. "
              "докстринг selenium_generic.py; попробуйте резидентный/"
              "мобильный российский прокси через `<БК>_PROXY`, напр. "
              "FONBET_PROXY=socks5://host:port).")
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

    results = [check_one(bk, login, password, cookies)
              for bk, login, password, cookies in accounts]
    ok = sum(results)
    print(f"\n--- Итого: {ok}/{len(results)} успешно ---")
    return 0 if ok == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
