"""Опциональный помощник для динамических страниц (JS-рендеринг).

Используется парсерами как запасной вариант, если обычный requests
не получает данные. Требует установленных selenium и webdriver-manager
(см. requirements.txt) и наличия Chrome/Chromium в системе.
"""
import atexit
import logging
import os
import random
import shutil
import threading

from ..config import USE_SELENIUM

log = logging.getLogger("parsers.selenium")

_warned = False

# Все парсеры делят ОДИН браузер, страницы рендерятся строго по одной.
# Несколько Chrome на слабом VPS (1–2 vCPU) не могут даже стартовать
# одновременно («chrome not reachable») и душат друг друга при рендеринге.
_render_lock = threading.Lock()
_shared_driver = None
_render_count = 0
# Профилактический перезапуск браузера (утечки памяти долгих SPA-сессий)
_MAX_RENDERS_PER_BROWSER = 100


def _get_shared_driver():
    """Возвращает общий браузер (лениво создаёт). Вызывать под _render_lock."""
    global _shared_driver
    if _shared_driver is None:
        _shared_driver = _make_driver()
        if _shared_driver is not None:
            log.info("Selenium: запущен общий браузер (один на все БК)")
    return _shared_driver


def _drop_shared_driver() -> None:
    """Закрывает общий браузер. Вызывать под _render_lock."""
    global _shared_driver
    if _shared_driver is not None:
        try:
            _shared_driver.quit()
        except Exception:  # noqa: BLE001
            pass
        _shared_driver = None


@atexit.register
def _shutdown() -> None:
    """Не оставляем chromedriver/Chrome после завершения процесса."""
    with _render_lock:
        _drop_shared_driver()

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
    """Доступ к ОБЩЕМУ браузеру (один Chrome на все БК и все циклы).

    Отдельный браузер на каждую БК не запускаем: на слабом VPS несколько
    Chrome не могут даже стартовать одновременно («chrome not reachable»).
    Общий браузер живёт между циклами — экономим и время старта."""

    @property
    def driver(self):
        """Для проверок вида `if s.driver is None` в парсерах."""
        with _render_lock:
            return _get_shared_driver()

    def render(self, url: str, wait_seconds: float = 8.0) -> str | None:
        global _render_count
        with _render_lock:
            driver = _get_shared_driver()
            if driver is None:
                return None
            _render_count += 1
            if _render_count > _MAX_RENDERS_PER_BROWSER:
                log.info("Selenium: профилактический перезапуск браузера")
                _drop_shared_driver()
                _render_count = 1
                driver = _get_shared_driver()
                if driver is None:
                    return None
            return self._render_locked(driver, url, wait_seconds)

    def _render_locked(self, driver, url: str,
                       wait_seconds: float) -> str | None:
        import time
        t0 = time.monotonic()
        try:
            driver.set_page_load_timeout(60)
            driver.set_script_timeout(25)
            try:
                driver.get(url)
            except Exception as exc:  # noqa: BLE001
                # Даже если загрузка «не завершилась» (у live-страниц она не
                # завершается никогда) — DOM уже отрисован, работаем с ним.
                log.info("Selenium: %s грузится дольше таймаута (%s) — "
                         "останавливаю и беру текущий DOM",
                         url, exc.__class__.__name__)
                try:
                    driver.execute_script("window.stop();")
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
                    n = int(driver.execute_script(
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
                driver.execute_script("window.stop();")
            except Exception:  # noqa: BLE001
                pass
            try:
                html = driver.execute_script(
                    "return document.documentElement.outerHTML;")
            except Exception:  # noqa: BLE001
                html = driver.page_source
            log.info("Selenium: %s отрендерен за %.0f c (%d байт)",
                     url, time.monotonic() - t0, len(html or ""))
            return html or None
        except Exception as exc:  # noqa: BLE001
            log.warning("Selenium: ошибка загрузки %s: %s — "
                        "перезапускаю браузер", url, exc)
            _drop_shared_driver()
            return None

    def close(self) -> None:
        """Общий браузер живёт между циклами — здесь ничего не закрываем."""

    def __enter__(self) -> "SeleniumSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def get_html_via_selenium(url: str, wait_seconds: float = 5.0) -> str | None:
    """Разовый рендер одной страницы (открывает и закрывает браузер).
    Для нескольких страниц используйте SeleniumSession (быстрее)."""
    with SeleniumSession() as s:
        return s.render(url, wait_seconds)
