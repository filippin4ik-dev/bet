"""Диагностика аккаунтов БК: проверка входа/баланса и разведка формы входа.

Данные аккаунта берутся ТОЛЬКО из переменных окружения — никогда из
аргументов командной строки или файлов в репозитории, чтобы логин/пароль
не осели в истории команд, git или чате с агентом. Задавайте их через
секреты окружения (Cursor Dashboard → Cloud Agents → Secrets, или
.env/systemd на своём сервере).

    python3 -m app.diagnose_account              # проверить баланс
    python3 -m app.diagnose_account --form Melbet  # разведка формы входа

РЕЖИМ ПРОВЕРКИ БАЛАНСА. Поддерживаются ДВА способа задать аккаунты:

1. Один аккаунт (быстрый путь):
     TEST_BOOKMAKER=Melbet
     TEST_BK_LOGIN=...
     TEST_BK_PASSWORD=...
     TEST_BK_LOGIN_TYPE=phone     # или login

2. Несколько аккаунтов сразу — по набору переменных на каждую БК из
   списка ниже (префикс — имя БК без пробелов/точек, в верхнем регистре):
     WINLINE_TEST_LOGIN / WINLINE_TEST_PASSWORD / WINLINE_TEST_LOGIN_TYPE
     MELBET_TEST_LOGIN / MELBET_TEST_PASSWORD / MELBET_TEST_LOGIN_TYPE
     BETBOOM_TEST_LOGIN / BETBOOM_TEST_PASSWORD / …
     FONBET_TEST_LOGIN / FONBET_TEST_PASSWORD / …
     LIGA_STAVOK_TEST_LOGIN / LIGA_STAVOK_TEST_PASSWORD / …
     BCGAME_TEST_LOGIN / BCGAME_TEST_PASSWORD
   Заданы (даже частично) — скрипт проверит баланс по каждой такой БК.

`<БК>_TEST_LOGIN_TYPE` — чем входим: `phone` (по номеру телефона) или
`login`. Это не косметика: у БК это разные вкладки формы входа с разными
полями, и по самой строке способ не угадать — номер клубной карты Melbet
тоже состоит из одних цифр. Не задан — берётся способ по умолчанию для
этой БК. Телефон можно писать в любом виде, код страны отрезается сам.

Опционально — сессионная cookie вместо/в дополнение к сценарию логина
(см. README «Вход по cookie» и app/connectors/selenium_generic.py):
`TEST_BK_COOKIES` (схема 1) или `<БК>_TEST_COOKIES` (схема 2), формат —
как заголовок Cookie из DevTools: "name1=v1; name2=v2". Полезно, когда
автологин спотыкается о капчу (BetBoom) или анти-бот блокирует IP этой
машины (Fonbet).

РЕЖИМ РАЗВЕДКИ ФОРМЫ (`--form <БК>`) паролей не требует вообще: он
открывает форму входа и печатает, что в ней реально лежит, — вкладки,
поля, кнопки — и сходятся ли с этим настроенные селекторы. Нужен там,
где форму не увидеть из среды разработки: Melbet пускает на сайт только
российские домашние адреса, поэтому её селекторы заданы «широко» и
уточняются на боевом сервере — переменными окружения
`MELBET_USERNAME_PHONE_SELECTOR`, `MELBET_TAB_LOGIN` и т.п., без правки
кода.

Пароли и cookie в вывод НЕ печатаются. Скрипт делает только
get_balance() — это экспериментальный функционал (см. предупреждения в
app/connectors/selenium_generic.py), проверенный не для всех БК.
Постановка ставки здесь НЕ вызывается.
"""
import json
import logging
import os
import sys

from .connectors import (LOGIN_TYPE_NAMES, env_prefix, get_connector,
                         login_types, national_phone, normalize_login_type,
                         resolve_login_type)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

BOOKMAKERS = ["Winline", "Melbet", "BetBoom", "Fonbet", "Liga Stavok",
              "bc.game"]


def _collect_accounts() -> list[dict]:
    accounts: list[dict] = []

    single_bk = os.getenv("TEST_BOOKMAKER", "").strip()
    single_login = os.getenv("TEST_BK_LOGIN", "").strip()
    single_pass = os.getenv("TEST_BK_PASSWORD", "").strip()
    if single_bk and single_login and single_pass:
        accounts.append({
            "bookmaker": single_bk, "login": single_login,
            "password": single_pass,
            "cookies": os.getenv("TEST_BK_COOKIES", "").strip(),
            "login_type": os.getenv("TEST_BK_LOGIN_TYPE", "").strip(),
        })

    for bk in BOOKMAKERS:
        prefix = env_prefix(bk)
        login = os.getenv(f"{prefix}_TEST_LOGIN", "").strip()
        password = os.getenv(f"{prefix}_TEST_PASSWORD", "").strip()
        if login and password and not any(a["bookmaker"] == bk
                                          for a in accounts):
            accounts.append({
                "bookmaker": bk, "login": login, "password": password,
                "cookies": os.getenv(f"{prefix}_TEST_COOKIES", "").strip(),
                "login_type": os.getenv(f"{prefix}_TEST_LOGIN_TYPE",
                                        "").strip(),
            })

    return accounts


def check_one(bookmaker: str, login: str, password: str, cookies: str = "",
              login_type: str = "") -> bool:
    allowed = login_types(bookmaker)
    requested = normalize_login_type(login_type, allowed[0])
    # То же приведение, что и внутри коннектора, — иначе напечатали бы
    # один способ, а вошли бы другим.
    login_type = resolve_login_type(bookmaker, login_type)
    print(f"\n=== {bookmaker} ===")
    if requested != login_type:
        print(f"ВНИМАНИЕ: {bookmaker} принимает только "
              f"{', '.join(LOGIN_TYPE_NAMES[t] for t in allowed)} — "
              f"вхожу {LOGIN_TYPE_NAMES[login_type]}")
    # Логин обрезаем, пароль и cookie не печатаем вовсе. Телефон
    # показываем в том виде, в каком его получит форма: половина
    # неудачных входов — это как раз лишний код страны.
    shown = national_phone(login) if login_type == "phone" else login
    print(f"вход {LOGIN_TYPE_NAMES[login_type]}: {shown[:3]}*** "
          f"(пароль не выводится)"
          + (", cookie: задана (не выводится)" if cookies else ""))
    connector = get_connector(bookmaker, login, password,
                              cookies=cookies or None,
                              login_type=login_type)
    try:
        balance = connector.get_balance()
        print(f"УСПЕХ: баланс = {balance:.2f}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"ОШИБКА чтения баланса: {exc}")
        print("Смотрите текст ошибки выше — это может быть: не тот способ "
              "входа (телефон против логина — проверьте, как вы входите "
              "руками), неверный селектор (что реально лежит в форме, "
              f"покажет `python -m app.diagnose_account --form "
              f"{bookmaker}`), капча на форме входа (автоматически не "
              "проходится) или анти-бот-блок IP этой машины (подтверждено "
              "на Fonbet — см. докстринг selenium_generic.py; попробуйте "
              "резидентный/мобильный российский прокси через `<БК>_PROXY`, "
              "напр. FONBET_PROXY=socks5://host:port).")
        return False
    finally:
        connector.close()


def inspect_form(bookmaker: str) -> int:
    """Печатает содержимое формы входа БК: вкладки, поля, кнопки."""
    if bookmaker not in BOOKMAKERS:
        print(f"Неизвестная БК «{bookmaker}». Доступны: "
              + ", ".join(BOOKMAKERS))
        return 1
    connector = get_connector(bookmaker, "", "")
    inspect = getattr(connector, "inspect_login_form", None)
    if inspect is None:
        print(f"У {bookmaker} нет браузерного коннектора — разведывать "
              f"нечего.")
        return 1
    try:
        info = inspect()
    except Exception as exc:  # noqa: BLE001
        print(f"Не удалось открыть форму входа {bookmaker}: {exc}")
        return 2
    finally:
        connector.close()

    print("=" * 64)
    print(f"ФОРМА ВХОДА {bookmaker}")
    print("=" * 64)
    print(f"страница: {info['url']}")
    print(f"заголовок: {info['title']}")
    if info.get("blocked"):
        print(f"\nБК НЕ ПУСТИЛА НА САЙТ: на странице «{info['blocked']}» — "
              f"это заглушка вместо сайта, формы входа на ней нет и быть "
              f"не может.")
        print("Селекторы тут ни при чём, править их бессмысленно. Нужен "
              "адрес, который БК считает своим: домашний российский IP "
              "(запустите разведку с боевого сервера) либо резидентный/"
              f"мобильный прокси — {env_prefix(bookmaker)}_PROXY="
              "socks5://host:port.\n")
    opened = True
    if info.get("opened_by"):
        opened = bool(info.get("opened"))
        print(f"кнопка открытия модалки «{info['opened_by']}»: "
              + ("нажата" if opened else "НЕ НАЙДЕНА"))
    if info.get("captcha"):
        print("на форме видна КАПЧА — автоматический вход не пройдёт")
    _print_form(info.get("form") or {})

    broken = not opened
    for login_type, entry in (info.get("tabs") or {}).items():
        if entry.get("tab_text") and not entry.get("clicked"):
            broken = True
        if entry.get("selector_matches") != 1:
            broken = True

    for login_type, entry in (info.get("tabs") or {}).items():
        print("-" * 64)
        print(f"способ входа: {LOGIN_TYPE_NAMES.get(login_type, login_type)}")
        if entry.get("tab_text"):
            print(f"  вкладка «{entry['tab_text']}»: "
                  + ("нажата → «%s»" % entry.get("matched_tab")
                     if entry.get("clicked") else "НЕ НАЙДЕНА"))
            print("  вкладки на странице: "
                  + (", ".join(entry.get("tabs_on_page") or []) or "—"))
        matches = entry.get("selector_matches", 0)
        verdict = ("ок" if matches == 1 else
                   "НИЧЕГО НЕ НАХОДИТ" if matches == 0 else
                   f"находит {matches} полей — возьмётся первое")
        print(f"  селектор поля {entry['selector']!r}: {verdict}")
        _print_form(entry.get("form") or {}, indent="  ")

    print("-" * 64)
    if info.get("blocked"):
        print("Ничего не найдено, потому что сайта не было — см. про "
              "заглушку выше. Повторите разведку оттуда, откуда БК пускает.")
        return 2
    if not broken:
        print("Настроенные селекторы сошлись с формой — вход можно "
              "проверять реальным аккаунтом (<БК>_TEST_LOGIN/"
              "_TEST_PASSWORD/_TEST_LOGIN_TYPE).")
        return 0
    prefix = env_prefix(bookmaker)
    print("Что-то не сошлось. Поправьте, не трогая код:")
    print(f"  {prefix}_TAB_PHONE=<надпись вкладки>")
    print(f"  {prefix}_TAB_LOGIN=<надпись вкладки>")
    print(f"  {prefix}_USERNAME_PHONE_SELECTOR=<css>")
    print(f"  {prefix}_USERNAME_LOGIN_SELECTOR=<css>")
    print(f"  {prefix}_PASSWORD_SELECTOR=<css>")
    print(f"  {prefix}_SUBMIT_SELECTOR=<css>")
    print(f"  {prefix}_BALANCE_SELECTOR=<css>")
    print(f"  {prefix}_LOGIN_BUTTON_TEXT=<надпись кнопки в шапке сайта>")
    return 0


def _print_form(form: dict, indent: str = "") -> None:
    for field in form.get("inputs") or []:
        print(f"{indent}  поле: " + json.dumps(field, ensure_ascii=False))
    for button in form.get("buttons") or []:
        print(f"{indent}  кнопка: " + json.dumps(button, ensure_ascii=False))
    if form.get("tabs"):
        print(f"{indent}  похоже на вкладки: " + ", ".join(form["tabs"]))


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "--form":
        if len(argv) < 2:
            print("Укажите БК: --form Melbet (доступны: "
                  + ", ".join(BOOKMAKERS) + ")")
            return 1
        return inspect_form(" ".join(argv[1:]))

    accounts = _collect_accounts()
    if not accounts:
        print("Не заданы данные ни одного тестового аккаунта в окружении — "
              "см. докстринг этого файла (TEST_BOOKMAKER/TEST_BK_LOGIN/"
              "TEST_BK_PASSWORD или <БК>_TEST_LOGIN/<БК>_TEST_PASSWORD).")
        print("Пароль не нужен, если надо только посмотреть форму входа: "
              "python3 -m app.diagnose_account --form Winline")
        return 1

    results = [check_one(**acc) for acc in accounts]
    ok = sum(results)
    print(f"\n--- Итого: {ok}/{len(results)} успешно ---")
    return 0 if ok == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
