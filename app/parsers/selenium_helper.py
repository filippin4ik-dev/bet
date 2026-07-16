"""Опциональный помощник для динамических страниц (JS-рендеринг).

Используется парсерами как запасной вариант, если обычный requests
не получает данные. Требует установленных selenium и webdriver-manager
(см. requirements.txt) и наличия Chrome/Chromium в системе.
"""
import logging
import random

from ..config import USE_SELENIUM

log = logging.getLogger("parsers.selenium")

_warned = False


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

    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(30)
        driver.get(url)
        driver.implicitly_wait(wait_seconds)
        return driver.page_source
    except Exception as exc:  # noqa: BLE001
        log.warning("Selenium: ошибка загрузки %s: %s", url, exc)
        return None
    finally:
        if driver is not None:
            driver.quit()
