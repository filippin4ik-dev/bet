"""Опциональный помощник для динамических страниц (JS-рендеринг).

Используется парсерами как запасной вариант, если обычный requests
не получает данные. Требует установленных selenium и webdriver-manager
(см. requirements.txt) и наличия Chrome/Chromium в системе.
"""
import logging
import os
import random
import shutil
import threading

from ..config import USE_SELENIUM

log = logging.getLogger("parsers.selenium")

_warned = False

# Парсеры разных БК работают в параллельных потоках, но рендерим страницы
# СТРОГО по одной: несколько Chrome, рендерящих одновременно на слабом VPS
# (1–2 vCPU), душат друг друга и всё встаёт на десятки минут.
_render_lock = threading.Lock()

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
                "--disable-gpu", "--lang=ru-RU", "--mute-audio",
                # Картинки/шрифты не нужны для кэфов, а на слабом VPS они
                # съедают CPU так, что рендерер перестаёт отвечать.
                "--blink-settings=imagesEnabled=false",
                "--disable-background-networking",
                f"--window-size={random.choice(['1920,1080', '1366,768'])}"):
        options.add_argument(arg)
    options.add_experimental_option(
        "prefs", {"profile.managed_default_content_settings.images": 2})
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
        with _render_lock:
            return self._render_locked(url, wait_seconds)

    def _render_locked(self, url: str, wait_seconds: float) -> str | None:
        import time
        t0 = time.monotonic()
        try:
            self.driver.set_page_load_timeout(60)
            self.driver.set_script_timeout(25)
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
            # Ждём, пока SPA дорисует линию. Готовность проверяем дёшево —
            # по числу DOM-узлов. Выкачивать page_source в цикле нельзя:
            # на слабом VPS сериализация огромного DOM подвешивает рендерер
            # (Read timed out на /source).
            deadline = time.monotonic() + max(wait_seconds, 6.0)
            prev, stable = -1, 0
            while time.monotonic() < deadline:
                time.sleep(2.0)
                try:
                    n = int(self.driver.execute_script(
                        "return document.getElementsByTagName('*').length"))
                except Exception:  # noqa: BLE001
                    continue
                if n == prev and n > 200:
                    stable += 1
                    if stable >= 2:      # ~4 секунды без изменений — готово
                        break
                else:
                    stable, prev = 0, n
            # Останавливаем фоновую загрузку/анимации перед чтением DOM,
            # иначе занятый рендерер может не ответить.
            try:
                self.driver.execute_script("window.stop();")
            except Exception:  # noqa: BLE001
                pass
            try:
                html = self.driver.execute_script(
                    "return document.documentElement.outerHTML;")
            except Exception:  # noqa: BLE001
                html = self.driver.page_source
            log.info("Selenium: %s отрендерен за %.0f c (%d байт)",
                     url, time.monotonic() - t0, len(html or ""))
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
