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


def _make_driver():
    """Создаёт headless-Chrome (с учётом snap-хрома). None при неудаче."""
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
                "pip install selenium и поставьте Chromium (см. README).")
            _warned = True
        return None

    options = Options()
    for arg in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu", "--lang=ru-RU",
                f"--window-size={random.choice(['1920,1080', '1366,768'])}"):
        options.add_argument(arg)
    # Живые страницы БК грузятся «бесконечно» (websocket, лента ставок) —
    # не ждём полной загрузки, забираем DOM после паузы. Иначе на слабом
    # VPS driver.get() падает с «Timed out receiving message from renderer».
    options.page_load_strategy = "none"
    browser = _first_existing(BROWSER_CANDIDATES)
    if browser:
        options.binary_location = browser

    driver_path = _first_existing(DRIVER_CANDIDATES)
    try:
        from selenium import webdriver
        if driver_path:
            from selenium.webdriver.chrome.service import Service
            return webdriver.Chrome(options=options,
                                    service=Service(executable_path=driver_path))
        return webdriver.Chrome(options=options)
    except Exception as exc:  # noqa: BLE001
        log.warning("Selenium: не удалось запустить браузер: %s "
                    "(браузер=%s, драйвер=%s)",
                    exc, browser or "не найден", driver_path or "авто")
        return None


class SeleniumSession:
    """Один браузер на несколько страниц — избегаем перезапуска Chrome
    (запуск браузера дорогой, а страниц у одной БК много)."""

    def __init__(self) -> None:
        self.driver = _make_driver()

    def render(self, url: str, wait_seconds: float = 8.0) -> str | None:
        if self.driver is None:
            return None
        import time
        try:
            self.driver.set_page_load_timeout(60)
            try:
                self.driver.get(url)
            except Exception as exc:  # noqa: BLE001
                # Даже если загрузка «не завершилась» (у live-страниц она не
                # завершается никогда) — DOM уже отрисован, работаем с ним.
                log.info("Selenium: %s грузится дольше таймаута (%s) — "
                         "останавливаю и беру текущий DOM",
                         url, exc.__class__.__name__)
                try:
                    self.driver.execute_script("window.stop();")
                except Exception:  # noqa: BLE001
                    pass
            # Ждём, пока SPA дорисует линию: опрашиваем размер DOM и выходим,
            # когда он перестал расти (или вышло время).
            deadline = time.monotonic() + max(wait_seconds, 6.0)
            html, prev_len, stable = "", -1, 0
            while time.monotonic() < deadline:
                time.sleep(2.0)
                try:
                    html = self.driver.page_source
                except Exception:  # noqa: BLE001
                    continue
                if len(html) == prev_len and len(html) > 50_000:
                    stable += 1
                    if stable >= 2:      # ~4 секунды без изменений — готово
                        break
                else:
                    stable, prev_len = 0, len(html)
            return html or None
        except Exception as exc:  # noqa: BLE001
            log.warning("Selenium: ошибка загрузки %s: %s", url, exc)
            self._restart()
            return None

    def _restart(self) -> None:
        """После краха рендерера сессия может быть неработоспособна —
        пересоздаём браузер, чтобы следующие страницы не пропали."""
        try:
            if self.driver is not None:
                self.driver.quit()
        except Exception:  # noqa: BLE001
            pass
        self.driver = _make_driver()

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:  # noqa: BLE001
                pass
            self.driver = None

    def __enter__(self) -> "SeleniumSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def get_html_via_selenium(url: str, wait_seconds: float = 5.0) -> str | None:
    """Разовый рендер одной страницы (открывает и закрывает браузер).
    Для нескольких страниц используйте SeleniumSession (быстрее)."""
    with SeleniumSession() as s:
        return s.render(url, wait_seconds)
