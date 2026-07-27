"""Best-effort Selenium-коннектор к личному кабинету БК.

⚠️ ЭКСПЕРИМЕНТАЛЬНО И НЕ ПРОВЕРЕНО НА ЖИВЫХ АККАУНТАХ.

У песочницы, в которой писался этот код, нет доступа ни к одному реальному
аккаунту ни одной из БК — соответственно, CSS-селекторы полей логина/пароля
и элемента баланса ниже подобраны по типовой вёрстке личных кабинетов
букмекеров и МОГУТ НЕ СОВПАДАТЬ с текущей версией сайта. Прежде чем включать
реальный (не dry-run) режим:

1. Откройте личный кабинет БК руками, через DevTools найдите актуальные
   селекторы полей логина/пароля/кнопки входа и элемента с балансом.
   2. Впишите их в SELECTORS ниже (или переопределите через переменные
      окружения `<BOOKMAKER>_LOGIN_SELECTOR` и т.д. — см. _selector()).
   3. Проверьте вход и чтение баланса на тестовом аккаунте с BALANCE-ONLY
      операцией (без ставок), прежде чем доверять авто-лимитам.

Автоматическая ПОСТАНОВКА СТАВКИ (place_bet) для всех БК НЕ реализована —
DOM ставочного слипа у каждой БК свой, часто с капчей/доп. подтверждением,
и без живого аккаунта его невозможно ни увидеть, ни протестировать. Вместо
фейковых «рабочих» селекторов, которые тихо ошибутся и поставят не туда,
place_bet честно возвращает ошибку с прямой ссылкой на событие у БК —
поставить вручную. Реализуйте place_bet в подклассе для своей БК, когда
проверите ставочный флоу лично (см. AUTOBET_DRY_RUN в README).
"""
import logging
import os
import re

from ..parsers import selenium_helper
from .base import BetLeg, BetResult, BookmakerConnector

log = logging.getLogger("connectors.selenium")

# Best-effort: типовые селекторы форм входа. НЕ проверены на реальных
# аккаунтах (см. предупреждение в докстринге модуля).
SELECTORS: dict[str, dict[str, str]] = {
    "Winline": {
        "login_url": "https://winline.ru/",
        "username": "input[name='login'], input[type='tel'], input[name='phone']",
        "password": "input[name='password'], input[type='password']",
        "submit": "button[type='submit']",
        "balance": "[class*='balance'], [class*='Balance']",
    },
    "BetBoom": {
        "login_url": "https://betboom.ru/",
        "username": "input[name='login'], input[type='tel']",
        "password": "input[name='password'], input[type='password']",
        "submit": "button[type='submit']",
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


def _selector(bookmaker: str, key: str) -> str:
    env_key = f"{bookmaker.upper().replace(' ', '_').replace('.', '')}_{key.upper()}_SELECTOR"
    return os.getenv(env_key, "") or SELECTORS.get(bookmaker, {}).get(key, "")


class SeleniumGenericConnector(BookmakerConnector):
    """Отдельный (НЕ общий со сканером) headless-браузер на аккаунт.

    Сканер использует общий Chrome для парсинга линии — аккаунтные сессии
    сюда подключать нельзя (перепутаются cookie между аккаунтами разных
    пользователей). Каждый вызов get_balance()/place_bet() поднимает и
    закрывает свой собственный браузер: медленнее, но безопасно."""

    def __init__(self, bookmaker: str, login: str, password: str):
        super().__init__(bookmaker, login, password)
        self._driver = None

    def _ensure_driver(self):
        if self._driver is None:
            self._driver = selenium_helper.new_standalone_driver()
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
        driver.get(cfg_url)
        wait = WebDriverWait(driver, 20)
        user_sel = _selector(self.bookmaker, "username")
        pass_sel = _selector(self.bookmaker, "password")
        submit_sel = _selector(self.bookmaker, "submit")
        try:
            user_el = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, user_sel)))
            user_el.clear()
            user_el.send_keys(self.login)
            pass_el = driver.find_element(By.CSS_SELECTOR, pass_sel)
            pass_el.clear()
            pass_el.send_keys(self.password)
            driver.find_element(By.CSS_SELECTOR, submit_sel).click()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Не удалось найти форму входа {self.bookmaker} по "
                f"настроенным селекторам (сайт изменил вёрстку?): {exc}"
            ) from exc

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
