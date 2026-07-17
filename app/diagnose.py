"""Диагностика доступности БК — запускать на сервере, где крутится сканер.

    python -m app.diagnose

Скрипт проверяет, отвечают ли сайты/API букмекеров с текущего IP, и что
именно возвращает каждый парсер. Помогает понять, почему котировок 0:
нет российского IP, сменился домен Fonbet, не установлен Selenium и т.п.
"""
import logging

from .parsers import get_parsers
from .parsers.selenium_helper import get_html_via_selenium

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s %(name)s: %(message)s")


def main() -> None:
    print("=" * 64)
    print("ДИАГНОСТИКА СКАНЕРА ВИЛОК")
    print("=" * 64)

    # 1. Внешний IP и его страна (важно: БК доступны только из РФ)
    try:
        import requests
        info = requests.get("https://ipinfo.io/json", timeout=8).json()
        print(f"Внешний IP: {info.get('ip')}  страна: {info.get('country')}  "
              f"провайдер: {info.get('org')}")
        if info.get("country") != "RU":
            print("  ВНИМАНИЕ: IP не российский — сайты БК, скорее всего, "
                  "не отдадут данные. Нужен VPS с IP в РФ.")
    except Exception as exc:  # noqa: BLE001
        print(f"Не удалось определить внешний IP: {exc}")

    # 2. Доступность Selenium (нужен только для Лиги Ставок; Winline и
    # BetBoom работают через прямые websocket-фиды без браузера)
    print("-" * 64)
    html = get_html_via_selenium("https://example.com", wait_seconds=2)
    print("Selenium:", "работает" if html else "недоступен (Лига Ставок "
          "вернёт 0 — установите selenium + Chromium)")

    # 3. Что реально возвращает каждый парсер
    print("-" * 64)
    total = 0
    for parser in get_parsers():
        odds = parser.safe_fetch()
        total += len(odds)
        sample = ""
        if odds:
            o = odds[0]
            sample = (f"  пример: {o.sport} | {o.team1} — {o.team2} | "
                      f"{o.market}: {o.k1}/{o.k2}")
        print(f"{parser.name:<12}: {len(odds)} котировок{sample}")
    print("-" * 64)
    print(f"ИТОГО котировок со всех БК: {total}")
    if total == 0:
        print("Ни одной котировки. Вероятные причины: не российский IP; "
              "сменился домен Fonbet (задайте FONBET_LINE_HOST); "
              "не установлен Selenium для динамических БК.")


if __name__ == "__main__":
    main()
