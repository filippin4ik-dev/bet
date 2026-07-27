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
from urllib.parse import urlsplit

from ..config import LIGASTAVOK_PROXY, USE_SELENIUM

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
    """Закрывает общий браузер (жёстко, с таймаутом). Вызывать под _render_lock."""
    global _shared_driver
    if _shared_driver is not None:
        _force_quit(_shared_driver)
        _shared_driver = None


@atexit.register
def _shutdown() -> None:
    """Не оставляем chromedriver/Chrome после завершения процесса."""
    with _render_lock:
        _drop_shared_driver()

# Расположения браузера и chromedriver. ВАЖНО: snap-версия Chromium
# (пакет chromium-browser -> snap) под systemd часто не запускается
# («chromium.chromedriver unexpectedly exited»), а скачанный драйвер не
# может управлять snap-хромом из-за конфайнмента. Поэтому НЕ-snap варианты
# (google-chrome, /usr/bin/chromium) идут первыми, а snap — последним.
BROWSER_CANDIDATES = [
    os.getenv("CHROME_BINARY", ""),
    shutil.which("google-chrome") or "",
    shutil.which("google-chrome-stable") or "",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    shutil.which("chromium") or "",
    shutil.which("chromium-browser") or "",
    "/snap/bin/chromium",                # snap — в последнюю очередь
]
DRIVER_CANDIDATES = [
    os.getenv("CHROMEDRIVER_PATH", ""),
    "/usr/bin/chromedriver",
    "/usr/local/bin/chromedriver",
    "/usr/lib/chromium-browser/chromedriver",
    shutil.which("chromedriver") or "",
    "/snap/bin/chromium.chromedriver",   # snap-драйвер — в последнюю очередь
]


def _is_snap(path: str | None) -> bool:
    return bool(path) and "/snap/" in path


def _first_existing(paths: list[str]) -> str | None:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def _existing(paths: list[str]) -> list[str]:
    seen, out = set(), []
    for p in paths:
        if p and p not in seen and os.path.exists(p):
            seen.add(p)
            out.append(p)
    return out


def _make_driver(proxy: str | None = None):
    """Создаёт headless-Chrome (с учётом snap-хрома). None при неудаче.

    proxy — явный `scheme://host:port` для ЭТОГО браузера (используется
    коннекторами аккаунтов БК, см. app/connectors/selenium_generic.py).
    Если не передан (None) — используется общий LIGASTAVOK_PROXY, как и
    раньше (общий браузер сканера ходит только через него)."""
    global _warned
    if not USE_SELENIUM:
        return None
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        if not _warned:
            log.warning(
                "Selenium не установлен — Лига Ставок (динамический сайт) "
                "отдаст 0 котировок. Установите его: "
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
                # Экономия памяти на VPS с 1–2 ГБ (без этого систему
                # добивает OOM-killer: в логе это выглядит как «Killed»)
                "--renderer-process-limit=2",
                "--disable-site-isolation-trials",
                "--disable-extensions",
                "--disk-cache-size=1048576",
                f"--window-size={random.choice(['1920,1080', '1366,768'])}"):
        options.add_argument(arg)
    options.add_experimental_option(
        "prefs", {"profile.managed_default_content_settings.images": 2})
    # Резидентный прокси. Общий браузер сканера (proxy=None здесь) сейчас
    # проксируется только для Лиги Ставок (LIGASTAVOK_PROXY) — остальные БК
    # линии ходят без браузера. Отдельные standalone-браузеры аккаунтов БК
    # (app/connectors/selenium_generic.py) передают свой proxy явно: анти-бот
    # многих БК (напр. Fonbet — ServicePipe) блокирует датацентровые IP ещё
    # до формы входа, точно как Qrator у Лиги Ставок — без резидентного/
    # мобильного российского прокси личный кабинет с VPS не открыть.
    # Chrome не умеет логин/пароль в --proxy-server — работает только
    # авторизация по IP.
    effective_proxy = LIGASTAVOK_PROXY if proxy is None else proxy
    if effective_proxy:
        u = urlsplit(effective_proxy)
        if u.hostname and u.port:
            scheme = u.scheme or "http"
            options.add_argument(
                f"--proxy-server={scheme}://{u.hostname}:{u.port}")
            if u.username:
                log.warning(
                    "Selenium: у прокси задан логин/пароль — браузер их "
                    "НЕ передаст (ограничение Chrome). Включите у "
                    "прокси-провайдера авторизацию по IP сервера, иначе "
                    "анти-бот-челлендж не пройдёт (HTTP-API парсера при "
                    "этом работает с логином/паролем как обычно).")
    # Живые страницы БК грузятся «бесконечно» (websocket, лента ставок) —
    # не ждём полной загрузки, забираем DOM после паузы. Иначе на слабом
    # VPS driver.get() падает с «Timed out receiving message from renderer».
    options.page_load_strategy = "none"
    browser = _first_existing(BROWSER_CANDIDATES)
    if browser:
        options.binary_location = browser
    snap_browser = _is_snap(browser)

    # Порядок попыток запуска драйвера:
    #   1) явные не-snap chromedriver'ы;
    #   2) авто-подбор Selenium Manager (сам скачает подходящий драйвер) —
    #      работает с обычным Chrome, но НЕ со snap-хромом (конфайнмент);
    #   3) snap-драйвер — только если браузер тоже snap (последний шанс).
    nonsnap_drivers = [d for d in _existing(DRIVER_CANDIDATES)
                       if not _is_snap(d)]
    snap_drivers = [d for d in _existing(DRIVER_CANDIDATES) if _is_snap(d)]
    attempts: list[str | None] = list(nonsnap_drivers)
    if not snap_browser:
        attempts.append(None)            # Selenium Manager (авто)
    attempts.extend(snap_drivers)
    if snap_browser:
        attempts.append(None)            # для snap — авто в самом конце
    # уберём дубли, сохранив порядок
    seen: set = set()
    attempts = [a for a in attempts if not (a in seen or seen.add(a))]

    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    last_exc = None
    for driver_path in attempts:
        try:
            if driver_path:
                driver = webdriver.Chrome(
                    options=options,
                    service=Service(executable_path=driver_path))
            else:
                driver = webdriver.Chrome(options=options)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.info("Selenium: драйвер %s не стартовал (%s) — пробую дальше",
                     driver_path or "авто (Selenium Manager)",
                     exc.__class__.__name__)
            continue
        _tune_executor(driver)
        # Часовой пояс браузера — московский: БК рендерят время начала
        # матчей в поясе браузера, а парсер разбирает его как МСК
        # (BK_TZ_OFFSET). Иначе на сервере с UTC время «уедет» на 3 часа.
        try:
            driver.execute_cdp_cmd("Emulation.setTimezoneOverride",
                                   {"timezoneId": "Europe/Moscow"})
        except Exception as exc:  # noqa: BLE001
            log.debug("Selenium: не удалось задать таймзону: %s", exc)
        log.info("Selenium: браузер запущен (бинарь=%s, драйвер=%s)",
                 browser or "по умолчанию", driver_path or "авто")
        return driver

    log.warning(
        "Selenium: не удалось запустить браузер (браузер=%s): %s. "
        "Если Chromium из snap — он часто не работает под systemd; "
        "поставьте google-chrome (не snap) и задайте CHROME_BINARY "
        "(см. README).", browser or "не найден", last_exc)
    return None


def _tune_executor(driver) -> None:
    """Жёстко ограничивает время ЛЮБОЙ команды браузеру.

    По умолчанию selenium ждёт ответа 120 секунд и повторяет 3 раза —
    если рендерер на слабом VPS перегружен, каждая команда (page_source,
    quit и т.д.) висит до 12 минут. Ставим 60 секунд без повторов."""
    try:
        ce = driver.command_executor
        ce._client_config.timeout = 60          # noqa: SLF001
        ce._conn.connection_pool_kw["retries"] = 0  # noqa: SLF001
        ce._conn.clear()                        # noqa: SLF001 — пересоздать пулы
    except Exception as exc:  # noqa: BLE001
        log.debug("Selenium: не удалось настроить таймауты клиента: %s", exc)


def _force_quit(driver) -> None:
    """quit() с таймаутом: зависший браузер добиваем через kill процессов."""
    done = threading.Event()

    def _quit() -> None:
        try:
            driver.quit()
        except Exception:  # noqa: BLE001
            pass
        done.set()

    t = threading.Thread(target=_quit, daemon=True)
    t.start()
    t.join(20)
    if not done.is_set():
        log.warning("Selenium: quit завис — убиваю процессы браузера")
        try:
            proc = getattr(driver.service, "process", None)
            if proc is not None:
                import subprocess
                subprocess.run(["pkill", "-9", "-P", str(proc.pid)],
                               check=False)
                proc.kill()
        except Exception:  # noqa: BLE001
            pass


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
        """Рендер страницы без прокрутки — вернёт итоговый DOM."""
        snaps = self.render_snapshots(url, wait_seconds, scroll_seconds=0.0)
        return snaps[-1] if snaps else None

    def render_snapshots(self, url: str, wait_seconds: float = 8.0,
                         scroll_seconds: float = 0.0,
                         click_text: str | None = None) -> list[str]:
        """Рендер страницы с прокруткой до конца списка.

        SPA букмекеров рисуют линию лениво (виртуальный список): без
        прокрутки в DOM есть только верхние ~50 событий. Прокручиваем вниз
        до дна (не дольше scroll_seconds) и снимаем DOM на каждом шаге —
        виртуальные списки выгружают уехавшие вверх строки, поэтому одного
        финального снимка недостаточно. Парсер разбирает все снимки и
        убирает дубли.

        click_text — кликнуть по элементу с таким текстом после загрузки
        (BetBoom не рисует прематч-линию, пока не выбран вид спорта в меню).
        """
        global _render_count
        with _render_lock:
            driver = _get_shared_driver()
            if driver is None:
                return []
            _render_count += 1
            if _render_count > _MAX_RENDERS_PER_BROWSER:
                log.info("Selenium: профилактический перезапуск браузера")
                _drop_shared_driver()
                _render_count = 1
                driver = _get_shared_driver()
                if driver is None:
                    return []
            return self._render_locked(driver, url, wait_seconds,
                                       scroll_seconds, click_text)

    # Клик по элементу, чей СОБСТВЕННЫЙ текст равен искомому.
    # Предпочитаем заголовки h1-h6: в левом меню BetBoom вид спорта — это
    # <h3> внутри кнопки, а такой же текст в live-карусели — <span>.
    _CLICK_TEXT_JS = """
        const want = arguments[0];
        const own = el => Array.from(el.childNodes)
            .filter(n => n.nodeType === 3)
            .map(n => n.textContent.trim()).join('');
        const els = Array.from(document.querySelectorAll('*'))
            .filter(el => own(el) === want);
        if (!els.length) return false;
        els.sort((a, b) =>
            (/^H[1-6]$/.test(a.tagName) ? 0 : 1) -
            (/^H[1-6]$/.test(b.tagName) ? 0 : 1));
        els[0].scrollIntoView({block: 'center'});
        els[0].click();
        return true;
    """

    def _dom(self, driver) -> str | None:
        """Текущий DOM (без page_source — он висит на слабом VPS)."""
        html = driver.execute_script(
            "return document.documentElement.outerHTML;")
        return html or None

    # Скроллим не window, а контейнер, в котором живёт лента матчей:
    # у Winline/BetBoom это внутренний div с overflow:auto, и window.scrollBy
    # для него ничего не делает. Ищем прокручиваемого ПРЕДКА кнопки «П1»
    # (не боковое меню!), иначе — самый «длинный» прокручиваемый контейнер.
    _SCROLL_STEP_JS = """
        const scrollable = el => {
            const oy = getComputedStyle(el).overflowY;
            return (oy === 'auto' || oy === 'scroll') &&
                   el.scrollHeight > el.clientHeight + 50;
        };
        let target = null;
        const p1 = Array.from(document.querySelectorAll('*')).find(
            el => el.childElementCount === 0 &&
                  el.textContent.trim() === 'П1');
        if (p1) {
            for (let el = p1.parentElement; el; el = el.parentElement) {
                if (scrollable(el)) { target = el; break; }
            }
        }
        if (!target) {
            let bestD = 0;
            for (const el of document.querySelectorAll('div, main, section')) {
                const d = el.scrollHeight - el.clientHeight;
                if (d > bestD && el.clientHeight > 200 && scrollable(el)) {
                    target = el; bestD = d;
                }
            }
        }
        if (!target) target = document.scrollingElement || document.documentElement;
        target.scrollTop += Math.max(800, target.clientHeight * 1.2);
        return target.scrollTop;
    """

    def _scroll_snapshots(self, driver, scroll_seconds: float) -> list[str]:
        """Прокручивает список до дна, снимая DOM после каждого шага."""
        import time
        snaps: list[str] = []
        deadline = time.monotonic() + scroll_seconds
        last_y, stuck = -1.0, 0
        while time.monotonic() < deadline:
            try:
                y = float(driver.execute_script(self._SCROLL_STEP_JS) or 0)
            except Exception:  # noqa: BLE001
                break
            time.sleep(1.2)
            try:
                html = self._dom(driver)
            except Exception:  # noqa: BLE001
                break
            # одинаковые подряд снимки не копим — их незачем парсить дважды
            if html and (not snaps or html != snaps[-1]):
                snaps.append(html)
            if abs(y - last_y) < 2:
                stuck += 1
                if stuck >= 2:
                    break        # дно списка — дальше не прокручивается
            else:
                stuck = 0
            last_y = y
        return snaps

    def _wait_dom_stable(self, driver, wait_seconds: float) -> None:
        """Ждёт, пока число DOM-узлов перестанет расти (~4 c без изменений)."""
        import time
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
                if stable >= 2:
                    break
            else:
                stable, prev = 0, n

    def _render_locked(self, driver, url: str, wait_seconds: float,
                       scroll_seconds: float = 0.0,
                       click_text: str | None = None) -> list[str]:
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
            self._wait_dom_stable(driver, wait_seconds)
            # BetBoom не рисует линию, пока не кликнешь по виду спорта в
            # меню — данные приходят по websocket уже после клика.
            if click_text:
                try:
                    clicked = driver.execute_script(
                        self._CLICK_TEXT_JS, click_text)
                except Exception:  # noqa: BLE001
                    clicked = False
                if clicked:
                    self._wait_dom_stable(driver, max(wait_seconds, 12.0))
                else:
                    log.info("Selenium: %s — элемент «%s» для клика "
                             "не найден", url, click_text)
            # Останавливаем фоновую загрузку/анимации перед чтением DOM,
            # иначе занятый рендерер может не ответить.
            try:
                driver.execute_script("window.stop();")
            except Exception:  # noqa: BLE001
                pass
            # ВАЖНО: никаких откатов на page_source — на перегруженном
            # рендерере он висит минутами. Не отдал DOM — перезапускаем
            # браузер и пропускаем страницу.
            snaps: list[str] = []
            html = self._dom(driver)
            if html:
                snaps.append(html)
            if scroll_seconds > 0:
                snaps.extend(self._scroll_snapshots(driver, scroll_seconds))
            log.info("Selenium: %s отрендерен за %.0f c "
                     "(%d снимков, %d байт)",
                     url, time.monotonic() - t0, len(snaps),
                     sum(len(s) for s in snaps))
            return snaps
        except Exception as exc:  # noqa: BLE001
            log.warning("Selenium: ошибка загрузки %s: %s — "
                        "перезапускаю браузер", url, exc)
            _drop_shared_driver()
            return []

    def close(self) -> None:
        """Общий браузер живёт между циклами — здесь ничего не закрываем."""

    def __enter__(self) -> "SeleniumSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def new_standalone_driver(proxy: str | None = None):
    """Отдельный (НЕ общий) headless-браузер — для операций с аккаунтами
    БК (баланс/ставки), где нельзя делить cookie-сессию с парсером линии.

    proxy — опциональный `scheme://host:port` для входа именно в эту БК
    (см. `<BOOKMAKER>_PROXY` в app/connectors/selenium_generic.py) — не
    путать с общим LIGASTAVOK_PROXY у браузера сканера.

    Вызывающий код обязан сам закрыть драйвер (driver.quit())."""
    return _make_driver(proxy=proxy)


def get_html_via_selenium(url: str, wait_seconds: float = 5.0) -> str | None:
    """Разовый рендер одной страницы (открывает и закрывает браузер).
    Для нескольких страниц используйте SeleniumSession (быстрее)."""
    with SeleniumSession() as s:
        return s.render(url, wait_seconds)
