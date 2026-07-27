"""Best-effort Selenium-коннектор к личному кабинету БК.

⚠️ ЭКСПЕРИМЕНТАЛЬНО. Статус по каждой БК на 2026-07-27 (реальная проверка
живым тестовым аккаунтом/браузером из облачной песочницы — см.
app/diagnose_account.py):

- **Fonbet** — форма входа НЕДОСТУПНА из этой песочницы: fonbet.ru и
  fon.bet отдают анти-бот-заглушку «Forbidden» (антибот ServicePipe) на
  ЛЮБОЙ запрос с датацентрового IP — даже в headed-режиме (реальный X-
  дисплей, не headless), ДО какой-либо формы. Это не проблема селекторов:
  селекторы логина/баланса ниже НЕ подтверждены и, скорее всего, не будут
  подтверждены без резидентного/мобильного российского IP — та же природа
  проблемы, что у Qrator на Лиге Ставок (см. LIGASTAVOK_PROXY в
  app/config.py). Поддержан аналогичный `FONBET_PROXY` (см. `_proxy_for`
  ниже) — запустите диагностику ещё раз с прокси на реальном сервере,
  прежде чем чинить SELECTORS["Fonbet"] вручную.
- **BetBoom** — форма входа ОТКРЫВАЕТСЯ и поля логина/пароля найдены и
  подтверждены (`input[name='phone']` / `input[name='password']`,
  открывается кнопкой «Вход» в хедере), НО кнопка отправки формы
  заблокирована, пока не пройдена **Google reCAPTCHA** (чекбокс «I'm not
  a robot»). Автоматически решать капчу коннектор не пытается (это не
  просто «другой селектор» — это отдельная защита от ботов, которую
  умышленно не обходим): при обнаружении капчи `_login()` поднимает явную
  ошибку вместо тихого зависания на неактивной кнопке.
- **Winline** — форма входа ОТКРЫВАЕТСЯ и поля логина/пароля/кнопка входа
  найдены и подтверждены (`input[name='auth-phone-base']` /
  `input[name='auth-password-base']` / `button.ww-login__btn[type=
  'submit']`, открывается кнопкой «Войти» в хедере), капчи на этом шаге
  не обнаружено. Дальше вход НЕ проверен целиком (в этой песочнице не было
  тестового аккаунта Winline с известным паролем) — вероятен запрос
  СМС/пуш-кода (см. `_maybe_handle_otp`), это не проверено.
- **Liga Stavok**, **bc.game** — форма входа НЕ проверена (нет тестового
  аккаунта в этой сессии); селекторы ниже — типовые заготовки, как и
  было изначально.

Ни для одной БК не было ни одного успешного end-to-end чтения баланса
(get_balance()) живым логином — Fonbet блокируется на уровне сети,
BetBoom — капчей, для Winline/Liga Stavok/bc.game не было тестовых
паролей. Поэтому элемент баланса (`SELECTORS[...]["balance"]`) ниже
ПО-ПРЕЖНЕМУ не подтверждён ни для одной БК — прежде чем включать реальный
(не dry-run) режим:

1. Откройте личный кабинет БК руками (или через это же окружение с
   прокси/капчей, если применимо), через DevTools найдите актуальный
   селектор элемента с балансом ПОСЛЕ входа.
2. Впишите его в SELECTORS ниже (или переопределите через переменную
   окружения `<BOOKMAKER>_BALANCE_SELECTOR` — см. `_selector()`).
3. Проверьте вход и чтение баланса на тестовом аккаунте с BALANCE-ONLY
   операцией (без ставок), прежде чем доверять авто-лимитам.

💡 **Вход по cookie** (обходит и капчу BetBoom, и упростит повтор входа
у остальных БК): капча/анти-бот/СМС-код нужны только В МОМЕНТ входа, а не
для уже авторизованной сессии. Если задать `self.cookies` (сырая строка
«name1=v1; name2=v2», как заголовок `Cookie` из DevTools — см. README
«Вход по cookie») — `_login()` СНАЧАЛА подставляет её в браузер, и только
если это не похоже на успешный вход (см. `_try_cookie_session()`),
откатывается на обычный сценарий формы. Cookie нужно один раз получить
из реального браузера, где вход выполнил человек (пройдя капчу/2FA сам),
и она не решает проблему Fonbet — анти-бот там блокирует ЛЮБОЙ запрос
с этого IP, включая уже авторизованные (сначала нужен `FONBET_PROXY`).

Автоматическая ПОСТАНОВКА СТАВКИ (place_bet) для всех БК НЕ реализована —
DOM ставочного слипа у каждой БК свой, часто с капчей/доп. подтверждением,
и без ЗАВЕРШЁННОГО живого входа (см. статусы выше — ни для одной БК вход
не пройден до конца) его невозможно ни увидеть, ни протестировать. Вместо
фейковых «рабочих» селекторов, которые тихо ошибутся и поставят не туда,
place_bet честно возвращает ошибку с прямой ссылкой на событие у БК —
поставить вручную. Реализуйте place_bet в подклассе для своей БК, когда
пройдёте вход и увидите купон ставки лично (см. AUTOBET_DRY_RUN в README).
"""
import logging
import os
import re
import time

from .. import otp
from ..parsers import selenium_helper
from .base import BetLeg, BetResult, BookmakerConnector

log = logging.getLogger("connectors.selenium")

# Эвристика поля СМС/OTP-кода — типовые атрибуты формы подтверждения
# входа. НЕ проверено на живом запросе кода ни у одной БК (см. общее
# предупреждение в докстринге модуля выше и в app/otp.py).
OTP_SELECTORS = (
    "input[autocomplete='one-time-code'], input[name*='otp' i], "
    "input[name*='code' i], input[id*='otp' i], input[id*='code' i], "
    "input[placeholder*='код' i]"
)
OTP_WAIT_SECONDS = 8.0        # сколько ждать появления поля кода после логина
OTP_RELAY_TIMEOUT = 180.0     # сколько ждать, пока оператор введёт код в админке

# Селекторы форм входа. Поля username/password/submit у Winline и BetBoom
# ПРОВЕРЕНЫ на живом сайте 2026-07-27 (без входа под паролем — см. статусы
# выше в докстринге модуля); у Fonbet/Liga Stavok/bc.game — НЕ проверены
# (типовые заготовки, как и было изначально).
#
# `login_button_text` — точный видимый текст кнопки в хедере, которую
# нужно нажать, чтобы ОТКРЫТЬ модалку логина (у Winline/BetBoom поля
# логина/пароля не существуют в DOM, пока эту кнопку не нажать — раньше
# коннектор пытался найти их сразу после открытия сайта и просто не
# находил ничего, что выглядело как «сайт поменял вёрстку», хотя реальная
# причина была в отсутствующем клике).
# `submit_text` — запасной вариант, если у кнопки отправки формы нет
# надёжного CSS-селектора (BetBoom: `type="button"`, хешированные React-
# классы вида `sc-zdin7l-0` — искать их по CSS бессмысленно, они меняются
# при каждой пересборке фронтенда БК). Используется, если `submit` не
# задан (или не найден).
SELECTORS: dict[str, dict[str, str]] = {
    "Winline": {
        "login_url": "https://winline.ru/",
        "login_button_text": "Войти",
        "username": "input[name='auth-phone-base'], input[name='login']",
        "password": "input[name='auth-password-base'], input[name='password']",
        "submit": "button.ww-login__btn[type='submit'], button[type='submit']",
        "balance": "[class*='balance'], [class*='Balance']",
    },
    "BetBoom": {
        "login_url": "https://betboom.ru/",
        "login_button_text": "Вход",
        "username": "input[name='phone'], input[name='login']",
        "password": "input[name='password']",
        "submit_text": "Войти",
        "balance": "[class*='balance'], [class*='Balance']",
    },
    "Fonbet": {
        "login_url": "https://fonbet.ru/",
        "username": "input[name='login'], input[type='text']",
        "password": "input[name='password'], input[type='password']",
        "submit": "button[type='submit']",
        "balance": "[class*='balance'], [class*='Balance']",
    },
    "Liga Stavok": {
        "login_url": "https://www.ligastavok.ru/",
        "username": "input[name='login'], input[type='tel']",
        "password": "input[name='password'], input[type='password']",
        "submit": "button[type='submit']",
        "balance": "[class*='balance'], [class*='Balance']",
    },
    "bc.game": {
        "login_url": "https://bc.game/",
        "username": "input[name='email'], input[type='email']",
        "password": "input[name='password'], input[type='password']",
        "submit": "button[type='submit']",
        "balance": "[class*='balance'], [class*='Balance']",
    },
}

_BALANCE_RE = re.compile(r"([\d\s]{1,9}[.,]?\d{0,2})\s*(?:₽|руб)", re.I)

# Общий признак виджета капчи (reCAPTCHA/hCaptcha/собственный виджет БК) —
# если это видно рядом с формой логина, автоматический вход дальше не
# пойдёт: решать капчу за пользователя мы не пытаемся (см. BetBoom выше).
_CAPTCHA_SELECTOR = (
    "iframe[src*='recaptcha' i], iframe[src*='hcaptcha' i], "
    ".g-recaptcha, [class*='captcha' i], [id*='captcha' i]"
)

# Клик по элементу с ТОЧНО таким видимым текстом (используется, когда у
# кнопки нет надёжного CSS-селектора — см. submit_text/login_button_text).
_CLICK_BY_TEXT_JS = """
    const want = arguments[0];
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
    const els = Array.from(document.querySelectorAll(
        'button, a, [role="button"], div, span'))
        .filter(el => norm(el.textContent) === want);
    const visible = els.filter(el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    });
    const target = visible[0] || els[0];
    if (!target) return false;
    target.scrollIntoView({block: 'center'});
    target.click();
    return true;
"""

# Проверка «виден ли на странице элемент с таким текстом» — без клика,
# используется, чтобы понять, вернул ли сайт форму логина (значит, cookie
# не подошла) или нет (значит, похоже, что уже залогинены).
_FIND_TEXT_VISIBLE_JS = """
    const want = arguments[0];
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
    return Array.from(document.querySelectorAll('*')).some(el => {
        if (norm(el.textContent) !== want) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    });
"""


def _selector(bookmaker: str, key: str) -> str:
    env_key = f"{bookmaker.upper().replace(' ', '_').replace('.', '')}_{key.upper()}_SELECTOR"
    return os.getenv(env_key, "") or SELECTORS.get(bookmaker, {}).get(key, "")


def _proxy_for(bookmaker: str) -> str | None:
    """`<BOOKMAKER>_PROXY` — резидентный/мобильный прокси именно для входа
    в эту БК (см. предупреждение о Fonbet/ServicePipe в докстринге модуля
    и LIGASTAVOK_PROXY в app/config.py — та же идея, но per-БК и только
    для standalone-браузера аккаунтов, не для общего браузера сканера)."""
    prefix = bookmaker.upper().replace(" ", "_").replace(".", "").replace("-", "_")
    value = os.getenv(f"{prefix}_PROXY", "").strip()
    return value or None


def _click_by_text(driver, text: str) -> bool:
    try:
        return bool(driver.execute_script(_CLICK_BY_TEXT_JS, text))
    except Exception:  # noqa: BLE001
        return False


def _text_visible(driver, text: str) -> bool:
    try:
        return bool(driver.execute_script(_FIND_TEXT_VISIBLE_JS, text))
    except Exception:  # noqa: BLE001
        return False


def _parse_cookie_string(raw: str) -> list[dict]:
    """«name1=v1; name2=v2» (как в заголовке Cookie/DevTools) → список
    словарей для driver.add_cookie(). Пустые/битые пары пропускаются."""
    cookies: list[dict] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if name:
            cookies.append({"name": name, "value": value})
    return cookies


def _fill_username(user_el, login: str) -> None:
    """Заполняет поле логина, при необходимости нормализуя номер телефона.

    Подтверждено на Winline: поле телефона показывает фиксированный
    префикс «+7» ОТДЕЛЬНО от самого input — туда нужно вводить 10 цифр
    без кода страны. Если передать полный номер (11 цифр, начинается на
    7/8), маска сдвигает цифры и получается неверный номер. Срабатывает
    только для полей типа tel/с именем, содержащим «phone» — обычные
    текстовые логины (email, никнейм) не трогаем."""
    value = login
    try:
        is_phone = (user_el.get_attribute("type") or "").lower() == "tel" or \
            "phone" in (user_el.get_attribute("name") or "").lower()
    except Exception:  # noqa: BLE001
        is_phone = False
    if is_phone:
        digits = re.sub(r"\D", "", value)
        if len(digits) == 11 and digits[0] in "78":
            value = digits[1:]
        elif len(digits) == 10:
            value = digits
    user_el.clear()
    user_el.send_keys(value)


def _has_visible_captcha(driver) -> bool:
    from selenium.webdriver.common.by import By
    try:
        els = driver.find_elements(By.CSS_SELECTOR, _CAPTCHA_SELECTOR)
    except Exception:  # noqa: BLE001
        return False
    return any(e.is_displayed() for e in els)


def _wait_for_captcha(driver, wait_seconds: float = 4.0) -> bool:
    """Капча (напр. Google reCAPTCHA у BetBoom) — сторонний iframe, он
    подгружается не мгновенно вместе с формой. Проверка сразу после
    появления полей логина/пароля (без ожидания) в среднем НЕ успевает
    увидеть капчу (проверено на BetBoom: капча появляется через ~2 с) —
    поэтому здесь короткий поллинг вместо разового find_elements()."""
    deadline = time.monotonic() + wait_seconds
    while True:
        if _has_visible_captcha(driver):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.4)


class SeleniumGenericConnector(BookmakerConnector):
    """Отдельный (НЕ общий со сканером) headless-браузер на аккаунт.

    Сканер использует общий Chrome для парсинга линии — аккаунтные сессии
    сюда подключать нельзя (перепутаются cookie между аккаунтами разных
    пользователей). Каждый вызов get_balance()/place_bet() поднимает и
    закрывает свой собственный браузер: медленнее, но безопасно."""

    def __init__(self, bookmaker: str, login: str, password: str,
                account_id: int | None = None,
                cookies: str | None = None):
        super().__init__(bookmaker, login, password, account_id=account_id,
                         cookies=cookies)
        self._driver = None

    def _ensure_driver(self):
        if self._driver is None:
            self._driver = selenium_helper.new_standalone_driver(
                proxy=_proxy_for(self.bookmaker))
        if self._driver is None:
            raise RuntimeError(
                "Selenium/Chrome недоступен в этой среде — баланс/ставка "
                "через браузер невозможны (см. README про установку "
                "Chromium).")
        return self._driver

    def _login(self) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        cfg_url = _selector(self.bookmaker, "login_url")
        if not cfg_url:
            raise NotImplementedError(
                f"Для {self.bookmaker} не настроен URL входа — заполните "
                f"app/connectors/selenium_generic.py::SELECTORS.")
        driver = self._ensure_driver()

        if self.cookies and self._try_cookie_session(driver, cfg_url):
            return  # вошли по сохранённой cookie — форма логина не нужна

        driver.get(cfg_url)
        wait = WebDriverWait(driver, 20)

        # У некоторых БК (подтверждено на Winline/BetBoom) полей логина/
        # пароля просто НЕТ в DOM, пока не нажать кнопку в хедере — форма
        # рисуется в модалке только по клику. Без этого шага коннектор
        # раньше ждал 20 с и падал с «сайт изменил вёрстку», хотя вёрстка
        # была той же самой — просто форма скрыта до клика.
        login_button_text = SELECTORS.get(self.bookmaker, {}).get(
            "login_button_text")
        if login_button_text:
            try:
                WebDriverWait(driver, 15).until(
                    lambda d: _click_by_text(d, login_button_text))
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"Не удалось найти/нажать кнопку «{login_button_text}» "
                    f"для открытия формы входа {self.bookmaker} (сайт "
                    f"изменил вёрстку?): {exc}") from exc

        user_sel = _selector(self.bookmaker, "username")
        pass_sel = _selector(self.bookmaker, "password")
        try:
            user_el = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, user_sel)))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Не удалось найти форму входа {self.bookmaker} по "
                f"настроенным селекторам (сайт изменил вёрстку?): {exc}"
            ) from exc

        if _wait_for_captcha(driver):
            raise RuntimeError(
                f"{self.bookmaker} показал капчу (reCAPTCHA/hCaptcha) на "
                f"форме входа — автоматический вход невозможен без её "
                f"решения человеком (см. предупреждение в докстринге "
                f"этого файла). Войдите вручную на тестовом аккаунте, "
                f"прежде чем доверять авто-ставкам для этой БК.")

        try:
            _fill_username(user_el, self.login)
            pass_el = driver.find_element(By.CSS_SELECTOR, pass_sel)
            pass_el.clear()
            pass_el.send_keys(self.password)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Форма входа {self.bookmaker} найдена, но не удалось "
                f"заполнить поле логина/пароля (сайт изменил вёрстку?): "
                f"{exc}") from exc

        if _has_visible_captcha(driver):
            raise RuntimeError(
                f"{self.bookmaker} показал капчу (reCAPTCHA/hCaptcha) "
                f"после заполнения формы входа — автоматический вход "
                f"невозможен без её решения человеком.")

        submit_sel = _selector(self.bookmaker, "submit")
        submit_text = SELECTORS.get(self.bookmaker, {}).get("submit_text")
        submitted = False
        if submit_sel:
            try:
                driver.find_element(By.CSS_SELECTOR, submit_sel).click()
                submitted = True
            except Exception:  # noqa: BLE001
                submitted = False
        if not submitted and submit_text:
            submitted = _click_by_text(driver, submit_text)
        if not submitted:
            raise RuntimeError(
                f"Не удалось нажать кнопку отправки формы входа "
                f"{self.bookmaker} (селектор {submit_sel!r}, текст "
                f"{submit_text!r}) — сайт изменил вёрстку?")
        self._maybe_handle_otp(driver)

    def _try_cookie_session(self, driver, cfg_url: str) -> bool:
        """Пытается войти по сохранённой cookie вместо сценария логина —
        см. `self.cookies` (докстринг base.BookmakerConnector) и раздел
        README «Вход по cookie». Так обходится: капча BetBoom (она нужна
        только при самом входе, не для уже авторизованной сессии) и
        повторный СМС-код при каждом запуске.

        Возвращает True, если сессия похожа на залогиненную (или БК не
        даёт способа это проверить — см. `login_button_text`) — тогда
        `_login()` дальше не идёт по форме. False — cookie не подошла
        или протухла, откатываемся на обычный сценарий входа."""
        cookies = _parse_cookie_string(self.cookies or "")
        if not cookies:
            return False
        # add_cookie() требует, чтобы браузер уже был на нужном домене —
        # иначе Selenium отклонит cookie с несовпадающим доменом.
        driver.get(cfg_url)
        added = 0
        for c in cookies:
            try:
                driver.add_cookie(c)
                added += 1
            except Exception as exc:  # noqa: BLE001
                log.debug("%s: cookie %s не принята браузером: %s",
                         self.bookmaker, c.get("name"), exc)
        if not added:
            log.warning("%s: ни одна cookie из сохранённой сессии не "
                       "принята браузером (не тот домен/формат?) — "
                       "использую обычный сценарий входа.", self.bookmaker)
            return False
        driver.get(cfg_url)  # перезагрузка — чтобы сайт увидел новые cookie
        time.sleep(2.0)
        login_button_text = SELECTORS.get(self.bookmaker, {}).get(
            "login_button_text")
        if login_button_text and _text_visible(driver, login_button_text):
            log.info("%s: сохранённая cookie не даёт вход (кнопка «%s» "
                     "всё ещё видна на странице) — сессия протухла, "
                     "переоформите её в браузере и вставьте заново.",
                     self.bookmaker, login_button_text)
            return False
        log.info("%s: вход по сохранённой cookie — форма логина/капча "
                 "не потребовались.", self.bookmaker)
        return True

    def _maybe_handle_otp(self, driver) -> None:
        """Если после входа сайт показал поле кода подтверждения (СМС/пуш)
        — просит код у оператора через админку (app/otp.py) и вписывает
        его в форму. Если аккаунт не привязан к записи в базе
        (account_id=None) или поле не появилось за OTP_WAIT_SECONDS —
        считаем, что код не запрошен, и продолжаем как обычно."""
        if self.account_id is None:
            return
        from selenium.webdriver.common.by import By

        deadline = time.monotonic() + OTP_WAIT_SECONDS
        otp_input = None
        while time.monotonic() < deadline:
            time.sleep(1.0)
            try:
                els = driver.find_elements(By.CSS_SELECTOR, OTP_SELECTORS)
            except Exception:  # noqa: BLE001
                els = []
            visible = [e for e in els if e.is_displayed()]
            if visible:
                otp_input = visible[0]
                break
        if otp_input is None:
            return  # обычный вход без доп. подтверждения

        log.info("%s запросил код подтверждения — жду ввода в админке "
                 "(до %.0f с)", self.bookmaker, OTP_RELAY_TIMEOUT)
        code = otp.request_otp(self.account_id, self.bookmaker,
                               timeout=OTP_RELAY_TIMEOUT)
        otp_input.clear()
        otp_input.send_keys(code)
        # кнопка подтверждения кода часто та же форма — пробуем тот же
        # submit-селектор БК, а если не сработает, форма может уходить
        # по Enter (send_keys уже это не делает, поэтому пробуем явно)
        try:
            submit_sel = _selector(self.bookmaker, "submit")
            driver.find_element(By.CSS_SELECTOR, submit_sel).click()
        except Exception as exc:  # noqa: BLE001
            log.info("%s: не удалось нажать подтверждение кода "
                     "автоматически (%s) — если это критично, "
                     "донастройте selenium_generic.py", self.bookmaker, exc)

    def get_balance(self) -> float:
        import time
        self._login()
        driver = self._ensure_driver()
        balance_sel = _selector(self.bookmaker, "balance")
        deadline = time.monotonic() + 20
        text = ""
        while time.monotonic() < deadline:
            time.sleep(1.0)
            try:
                from selenium.webdriver.common.by import By
                els = driver.find_elements(By.CSS_SELECTOR, balance_sel)
                for el in els:
                    if el.text.strip():
                        text = el.text
                        break
                if text:
                    break
            except Exception:  # noqa: BLE001
                continue
        if not text:
            # запасной вариант: ищем денежный паттерн по всему тексту страницы
            try:
                body_text = driver.find_element(
                    "tag name", "body").text
            except Exception:  # noqa: BLE001
                body_text = ""
            m = _BALANCE_RE.search(body_text)
            if not m:
                raise RuntimeError(
                    f"Не удалось найти элемент баланса у {self.bookmaker} — "
                    f"проверьте/обновите селектор 'balance' (вход, "
                    f"возможно, тоже не прошёл: сайт мог показать капчу).")
            text = m.group(0)
        m = _BALANCE_RE.search(text) or re.search(r"[\d\s]+[.,]?\d*", text)
        if not m:
            raise RuntimeError(
                f"Не удалось распознать число в тексте баланса "
                f"{self.bookmaker}: {text!r}")
        num = m.group(1) if m.lastindex else m.group(0)
        num = num.replace(" ", "").replace("\xa0", "").replace(",", ".")
        return float(num)

    def place_bet(self, leg: BetLeg) -> BetResult:
        raise NotImplementedError(
            f"Автоматическая постановка ставки для {self.bookmaker} не "
            f"реализована (DOM ставочного слипа не проверен на живом "
            f"аккаунте — см. предупреждение в selenium_generic.py). "
            f"Поставьте вручную: {leg.stake:.2f}₽ на «{leg.outcome_label}» "
            f"по кэфу не хуже {leg.odds} — {leg.url or 'ссылка недоступна'}")

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:  # noqa: BLE001
                pass
            self._driver = None


BOOKMAKER_CONNECTORS: dict[str, type] = {
    name: SeleniumGenericConnector for name in SELECTORS
}
