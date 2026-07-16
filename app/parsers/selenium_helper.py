"""Опциональный помощник для динамических страниц (JS-рендеринг).

Используется парсерами как запасной вариант, если обычный requests
не получает данные. Требует установленных selenium и webdriver-manager
(см. requirements.txt) и наличия Chrome/Chromium в системе.
"""
import logging
import os
import random
import shutil

from ..config import USE_SELENIUM

log = logging.getLogger("parsers.selenium")

_warned = False

# Типичные расположения браузера и chromedriver, включая snap-версию
# Chromium на Ubuntu (пакет chromium-browser -> snap).
BROWSER_CANDIDATES = [
    os.getenv("CHROME_BINARY", ""),
    shutil.which("chromium-browser") or "",
    shutil.which("chromium") or "",
    shutil.which("google-chrome") or "",
    "/snap/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
]
DRIVER_CANDIDATES = [
    os.getenv("CHROMEDRIVER_PATH", ""),
    shutil.which("chromedriver") or "",
    "/snap/bin/chromium.chromedriver",   # драйвер в комплекте snap-хрома
    "/usr/bin/chromedriver",
    "/usr/lib/chromium-browser/chromedriver",
]


def _first_existing(paths: list[str]) -> str | None:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def get_html_via_selenium(url: str, wait_seconds: float = 5.0) -> str | None:
    """Возвращает HTML страницы после исполнения JS, либо None,
    если Selenium отключён/недоступен или произошла ошибка."""
    global _warned
    if not USE_SELENIUM:
        return None
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        if not _warned:
            log.warning(
                "Selenium не установлен — Winline/BetBoom/Лига Ставок "
                "(динамические сайты) отдадут 0 котировок. Установите его: "
                "раскомментируйте selenium в requirements.txt, поставьте "
                "Chromium и переустановите зависимости (см. README).")
            _warned = True
        return None

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(f"--window-size={random.choice(['1920,1080', '1366,768'])}")

    # Указываем найденный браузер (Selenium Manager не видит snap-хром)
    browser = _first_existing(BROWSER_CANDIDATES)
    if browser:
        options.binary_location = browser

    service = None
    driver_path = _first_existing(DRIVER_CANDIDATES)
    if driver_path:
        from selenium.webdriver.chrome.service import Service
        service = Service(executable_path=driver_path)

    driver = None
    try:
        if service:
            driver = webdriver.Chrome(options=options, service=service)
        else:
            # Пусть Selenium Manager сам подберёт браузер и драйвер
            driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(30)
        driver.get(url)
        driver.implicitly_wait(wait_seconds)
        import time
        time.sleep(wait_seconds)  # даём SPA дорисовать линию
        return driver.page_source
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Selenium: ошибка загрузки %s: %s (браузер=%s, драйвер=%s)",
            url, exc, browser or "не найден", driver_path or "авто")
        return None
    finally:
        if driver is not None:
            driver.quit()
