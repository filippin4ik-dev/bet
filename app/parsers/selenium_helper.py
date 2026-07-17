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
            driver = webdriver.Chrome(
                options=options,
                service=Service(executable_path=driver_path))
        else:
            driver = webdriver.Chrome(options=options)
        _tune_executor(driver)
        # Часовой пояс браузера — московский: БК рендерят время начала
        # матчей в поясе браузера, а парсер разбирает его как МСК
        # (BK_TZ_OFFSET). Иначе на сервере с UTC время «уедет» на 3 часа.
        try:
            driver.execute_cdp_cmd("Emulation.setTimezoneOverride",
                                   {"timezoneId": "Europe/Moscow"})
        except Exception as exc:  # noqa: BLE001
            log.debug("Selenium: не удалось задать таймзону: %s", exc)
        return driver
    except Exception as exc:  # noqa: BLE001
        log.warning("Selenium: не удалось запустить браузер: %s "
                    "(браузер=%s, драйвер=%s)",
                    exc, browser or "не найден", driver_path or "авто")
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

    # Пункты левого меню BetBoom: текст «Имя лиги/группы<счётчик событий>»
    # (например «Setka Cup359»). Берём самый глубокий элемент с таким
    # текстом — по нему и кликаем.
    _SIDEBAR_ITEMS_JS = """
        const out = [], seen = new Set();
        for (const el of document.querySelectorAll('a, button, [role=button], div, li')) {
            if (el.offsetParent === null) continue;
            const r = el.getBoundingClientRect();
            if (r.left > 420 || r.width === 0) continue;   // только левое меню
            const t = el.textContent.trim();
            if (!t || t.length > 80 || !/\\d$/.test(t)) continue;
            if (seen.has(t)) continue;
            let deepest = true;
            for (const c of el.querySelectorAll('*'))
                if (c.textContent.trim() === t) { deepest = false; break; }
            if (!deepest) continue;
            seen.add(t);
            out.push(t);
        }
        return out;
    """
    _CLICK_SIDEBAR_JS = """
        const want = arguments[0];
        let best = null;
        for (const el of document.querySelectorAll('a, button, [role=button], div, li')) {
            if (el.offsetParent === null) continue;
            const r = el.getBoundingClientRect();
            if (r.left > 420 || r.width === 0) continue;
            if (el.textContent.trim() !== want) continue;
            if (!best || best.contains(el)) best = el;   // самый глубокий
        }
        if (!best) return false;
        best.scrollIntoView({block: 'center'});
        best.click();
        return true;
    """

    def render_league_pages(self, url: str, click_text: str,
                            wait_seconds: float = 12.0,
                            scroll_seconds: float = 10.0,
                            max_leagues: int = 20) -> list[str]:
        """BetBoom: обходит все лиги одного вида спорта внутри SPA.

        Линия не рисуется, пока вид спорта не выбран в левом меню. После
        клика в меню появляются его дочерние пункты: закреплённые лиги
        и группы (страны/туры). Клик по лиге открывает её страницу (лента
        показывает только ближайшие матчи первой лиги), клик по группе
        разворачивает подсписок — обходим и то и другое, до max_leagues
        страниц. Сайдбар остаётся на страницах лиг, поэтому переходим
        между лигами без возврата назад. Возвращает снимки DOM со всех
        посещённых страниц.
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
            return self._render_leagues_locked(
                driver, url, click_text, wait_seconds, scroll_seconds,
                max_leagues)

    def _sidebar_items(self, driver) -> list[str]:
        try:
            return driver.execute_script(self._SIDEBAR_ITEMS_JS) or []
        except Exception:  # noqa: BLE001
            return []

    def _render_leagues_locked(self, driver, url: str, click_text: str,
                               wait_seconds: float, scroll_seconds: float,
                               max_leagues: int) -> list[str]:
        import time
        t0 = time.monotonic()
        # Страница вида спорта — пока БЕЗ клика по меню: сначала запоминаем
        # пункты сайдбара, чтобы после клика вычислить ДОБАВИВШИЕСЯ (это
        # и есть лиги/группы выбранного вида спорта).
        snaps = self._render_locked(driver, url, wait_seconds, 0.0, None)
        if not snaps:
            return []
        before = set(self._sidebar_items(driver))
        try:
            clicked = driver.execute_script(self._CLICK_TEXT_JS, click_text)
        except Exception:  # noqa: BLE001
            clicked = False
        if not clicked:
            log.info("Selenium: %s — вид спорта «%s» в меню не найден",
                     url, click_text)
            return snaps
        self._wait_cards(driver, wait_seconds)
        html = self._dom(driver)
        if html:
            snaps.append(html)
        snaps.extend(self._scroll_snapshots(driver, scroll_seconds))

        # Очередь дочерних пунктов меню: лиги открываются (URL меняется),
        # группы разворачиваются (URL прежний, но появляются новые пункты).
        # Пункт заканчивается числом событий («Setka Cup359») — идём от
        # крупных лиг к мелким, чтобы за лимит страниц собрать максимум
        # матчей. Страницы лиг короткие, длинная прокрутка им не нужна.
        import re as _re

        def _count(t: str) -> int:
            m = _re.search(r"(\d+)$", t)
            return int(m.group(1)) if m else 0

        league_scroll = min(scroll_seconds, 12.0)
        queue = [t for t in self._sidebar_items(driver) if t not in before]
        seen_items = set(queue) | before
        visited_urls = {driver.current_url}
        pages = 0
        while queue and pages < max_leagues:
            queue.sort(key=_count, reverse=True)
            name = queue.pop(0)
            try:
                prev_url = driver.current_url
                if not driver.execute_script(self._CLICK_SIDEBAR_JS, name):
                    continue
                self._wait_cards(driver, wait_seconds)
                cur = driver.current_url
                if cur != prev_url and cur not in visited_urls:
                    # открылась страница лиги — снимаем её
                    visited_urls.add(cur)
                    pages += 1
                    html = self._dom(driver)
                    if html:
                        snaps.append(html)
                    snaps.extend(
                        self._scroll_snapshots(driver, league_scroll))
                # в любом случае подбираем новые пункты (развернулась
                # группа или подсписок остался открытым)
                for t in self._sidebar_items(driver):
                    if t not in seen_items:
                        seen_items.add(t)
                        queue.append(t)
            except Exception as exc:  # noqa: BLE001
                log.warning("Selenium: пункт «%s» на %s не открылся: %s",
                            name, url, exc)
                break
        log.info("Selenium: %s обойдено страниц лиг: %d за %.0f c "
                 "(%d снимков)",
                 url, pages, time.monotonic() - t0, len(snaps))
        return snaps

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

    # Число карточек событий на странице (кнопок «П1») — дешёвый признак
    # готовности страницы лиги.
    _CARD_COUNT_JS = """
        let n = 0;
        for (const el of document.querySelectorAll('*'))
            if (el.childElementCount === 0 && el.textContent.trim() === 'П1')
                n++;
        return n;
    """

    def _wait_cards(self, driver, wait_seconds: float) -> None:
        """Быстрое ожидание страницы лиги: карточки появились и их число
        перестало меняться. Вдвое быстрее _wait_dom_stable на готовых
        страницах — важно, когда лиг десятки."""
        import time
        deadline = time.monotonic() + max(wait_seconds, 4.0)
        prev, stable = -1, 0
        while time.monotonic() < deadline:
            time.sleep(1.0)
            try:
                n = int(driver.execute_script(self._CARD_COUNT_JS))
            except Exception:  # noqa: BLE001
                continue
            if n == prev and n > 0:
                stable += 1
                if stable >= 2:
                    return
            else:
                stable, prev = 0, n

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


def get_html_via_selenium(url: str, wait_seconds: float = 5.0) -> str | None:
    """Разовый рендер одной страницы (открывает и закрывает браузер).
    Для нескольких страниц используйте SeleniumSession (быстрее)."""
    with SeleniumSession() as s:
        return s.render(url, wait_seconds)
