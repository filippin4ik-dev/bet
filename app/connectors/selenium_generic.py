"""Best-effort Selenium-коннектор к личному кабинету БК.

ВЫБОР СПОСОБА ВХОДА. В российскую БК входят либо номером телефона, либо
логином, и это НЕ одно и то же поле: у Winline и Melbet форма разведена
на две вкладки, каждая со своим input. Угадать способ по самой строке
нельзя — номер клубной карты Melbet тоже состоит из одних цифр, — поэтому
способ хранится вместе с аккаунтом (`login_type`, см.
app/connectors/base.py) и выбирается оператором в админке. Здесь он
решает, какую вкладку нажать (`tab_phone`/`tab_login`), какое поле
заполнить (`username_phone`/`username_login`) и надо ли отрезать у
номера код страны.

⚠️ ЭКСПЕРИМЕНТАЛЬНО. Статус по каждой БК (реальная проверка живым
браузером из облачной песочницы — см. app/diagnose_account.py):

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
  ошибку вместо тихого зависания на неактивной кнопке. Селектор баланса
  подтверждён по реальному DOM (мобильная вёрстка, кнопка баланса в
  хедере — `[class*='MobileBalanceText']`, см. SELECTORS ниже).
  **Важно:** авторизация у BetBoom — не браузерная cookie, а JWT-токен
  (обычно лежит в `window.localStorage`, а не в cookie), см. «Вход по
  cookie» ниже про формат `ls:key=value`. При этом сайт защищён Qrator
  (заметная JS-фингерпринт-отправка на `/api/fl` при каждой загрузке
  страницы) — проверка `POST /api/auth/check-token` с токеном, снятым в
  ОДНОМ браузере/IP, но отправленным С ДРУГОГО IP (из этой облачной
  песочницы), вернула `{"isNeedLogout": true}`, т.е. сайт считает сессию
  недействительной при смене IP/фингерпринта. Это значит, что просто
  скопировать токен недостаточно — скорее всего, нужен резидентный/
  мобильный российский IP того же региона, что и исходный вход (через
  `BETBOOM_PROXY`, аналогично Fonbet), иначе даже валидный токен не
  примется.
- **Winline** (2026-07-30) — ЕДИНСТВЕННАЯ БК, У КОТОРОЙ ВСЁ ПРОВЕРЕНО
  ЖИВЫМ АККАУНТОМ: вход по логину проходит до конца и баланс читается
  (проверено полным путём админки — аккаунт в базе, `refresh_balance()`,
  баланс в базе). Селектор баланса ищется ПО ПОДПИСИ рядом со значением,
  и это не педантизм: в шапке два одинаковых
  `<div class="user-account__value">` — первый «В игре» (на тестовом
  счёте там было 84 619 при НУЛЕВОМ балансе), второй собственно баланс.
  Селектор по классу взял бы первый, и лимит ставки считался бы от денег,
  которых на счету нет. Знака ₽ Winline не рисует вовсе — баланс приходит
  голой цифрой. СМС-кода при входе с датацентрового адреса не было.
  Сценарий входа пройден до проверки
  реквизитов ОБОИМИ способами. Модалка открывается кнопкой «Войти» в
  хедере; внутри две вкладки — `<span class="ww-tabs__item">Телефон</span>`
  и `…>Логин</span>`. Вкладка «Телефон» рисует
  `input[name='auth-phone-base']` (рядом ОТКЛЮЧЁННЫЙ
  `input[name='auth-phone-code']` со значением «+7» — значит в поле идут
  10 цифр без кода страны), вкладка «Логин» —
  `input[name='auth-username']`. Пароль в обоих случаях
  `input[name='auth-password-base']`, отправка —
  `button.ww-login__btn[type='submit']`, и она приходит С АТРИБУТОМ
  `disabled`: включается, только когда фронтенд принял оба поля (отсюда
  `_wait_submit_enabled`). Отправка заведомо неверных реквизитов даёт
  «Неверный телефон или пароль» и «Неверный логин или пароль»
  соответственно — то есть запрос доходит до проверки на стороне БК.
  Капчи на форме нет. НЕ проверено: вход по ТЕЛЕФОНУ до конца (тестовый
  аккаунт был с логином) и постановка ставки.
- **Melbet** — движок 1xBet, вход тоже двумя способами (телефон и
  «ID/номер счёта»). Селекторы НЕ ПРОВЕРЕНЫ и проверены быть не могут из
  этой песочницы: melbet.ru отдаёт «отключите VPN» адресам дата-центров,
  а международные зеркала — страницу `/ru/block` на всё, кроме фида линии
  (проверено 2026-07-30; у парсера линии та же беда, см.
  app/parsers/melbet.py). Поэтому селекторы ниже заданы намеренно широко,
  а уточняются они НА БОЕВОМ СЕРВЕРЕ и без правки кода: `python -m
  app.diagnose_account --form Melbet` печатает, что реально лежит в
  форме, а переменные `MELBET_TAB_PHONE`, `MELBET_USERNAME_LOGIN_SELECTOR`
  и т.п. заменяют не подошедшее.
- **Liga Stavok**, **bc.game** — форма входа НЕ проверена (нет тестового
  аккаунта в этой сессии); селекторы ниже — типовые заготовки, как и
  было изначально.

Кроме Winline, ни у одной БК успешного end-to-end чтения баланса живым
логином не было: Fonbet блокируется на уровне сети, BetBoom — капчей,
Melbet не пускает на сайт вовсе, для Liga Stavok/bc.game не было
тестовых аккаунтов. Поэтому элемент баланса
(`SELECTORS[...]["balance"]`) у всех остальных БК — НЕПОДТВЕРЖДЁННАЯ
заготовка; прежде чем включать реальный (не dry-run) режим:

1. Откройте личный кабинет БК руками (или через это же окружение с
   прокси/капчей, если применимо), через DevTools найдите актуальный
   селектор элемента с балансом ПОСЛЕ входа.
2. Впишите его в SELECTORS ниже (или переопределите через переменную
   окружения `<BOOKMAKER>_BALANCE_SELECTOR` — см. `_selector()`).
   Если значение баланса неотличимо по классу от соседних чисел в шапке
   (как оказалось у Winline), задайте вместо этого пару
   `balance_label` + `balance_value` — поиск по подписи рядом со
   значением (`<BOOKMAKER>_BALANCE_LABEL` /
   `<BOOKMAKER>_BALANCE_VALUE_SELECTOR`).
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
У BetBoom, чья сессия — JWT в `localStorage`, а не cookie, в ту же
строку можно добавить запись `ls:key=value` (напр. `ls:token=eyJ...`) —
ключ подсмотрите в DevTools → Application → Local Storage у себя в
браузере (присылайте сюда ТОЛЬКО имя ключа, не значение). Но см. выше:
даже с правильным ключом BetBoom, похоже, привязывает токен к
IP/фингерпринту (Qrator), так что без `BETBOOM_PROXY` под тот же регион
сессия может не приняться.

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
from .base import (LOGIN_BY_LOGIN, LOGIN_BY_PHONE, LOGIN_TYPE_NAMES,
                   BetLeg, BetResult, BookmakerConnector, national_phone,
                   normalize_login_type)

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
#
# ВЫБОР СПОСОБА ВХОДА (телефон или логин). У БК это разные вкладки одной
# модалки, и на каждой — СВОЁ поле имени пользователя. Поэтому вместо
# одного `username` здесь пара `username_phone` / `username_login`, а
# переключает вкладки `tab_phone` / `tab_login` — видимый текст вкладки,
# который ищется СТРОГО среди элементов `tab_selector`. Искать вкладку по
# тексту на всей странице нельзя: слово «Логин» встречается и в шапке
# сайта, и в подсказках, и клик ушёл бы не туда. Если у БК вкладок нет
# (одно поле на оба способа), `tab_*` не задаются, а поле берётся из
# общего `username`.
SELECTORS: dict[str, dict[str, str]] = {
    "Winline": {
        "login_url": "https://winline.ru/",
        "login_button_text": "Войти",
        # Всё ниже снято с живой модалки 2026-07-30 (см. докстринг модуля):
        # <span class="ww-tabs__item ww-tabs__item--active"> Телефон </span>
        # <span class="ww-tabs__item"> Логин </span>
        "tab_selector": ".ww-tabs__item",
        "tab_active_class": "ww-tabs__item--active",
        "tab_phone": "Телефон",
        "tab_login": "Логин",
        "username_phone": "input[name='auth-phone-base']",
        "username_login": "input[name='auth-username']",
        "password": "input[name='auth-password-base']",
        "submit": "button.ww-login__btn[type='submit']",
        # Баланс ищется ПО ПОДПИСИ рядом со значением, а не по классу.
        # Снято с шапки после реального входа 2026-07-30:
        #   <div class="user-account__block">
        #     <div class="user-account__label">Баланс:</div>
        #     <div class="user-account__value">0</div></div>
        # Таких user-account__value в шапке ДВА: первый — «В игре»
        # (сумма в незавершённых ставках), второй — баланс. Селектор по
        # классу взял бы первый, и бот считал бы лимит ставки от денег,
        # которых на счету нет. Знака ₽ Winline не рисует вовсе — отсюда
        # и разбор голого числа (см. _parse_money).
        "balance_label": "Баланс",
        "balance_value": ".user-account__value",
    },
    # Melbet работает на движке 1xBet, и вход у неё — те же две вкладки:
    # по номеру телефона и по «ID/логину» (у 1xBet это номер клубной
    # карты). СЕЛЕКТОРЫ НИЖЕ НЕ ПРОВЕРЕНЫ на живом сайте: melbet.ru не
    # пускает адреса дата-центров, а международные зеркала отдают
    # страницу-заглушку `/ru/block` на всё, кроме фида линии (проверено
    # 2026-07-30 — то же ограничение, что и у парсера, см.
    # app/parsers/melbet.py). Поэтому селекторы заданы «широко» (по
    # нескольким вариантам имени поля сразу), а уточнить их на реальном
    # сервере можно, не трогая код: `venv/bin/python -m
    # app.diagnose_account --form Melbet` покажет, что реально лежит в
    # форме, а переменные MELBET_USERNAME_PHONE_SELECTOR /
    # MELBET_USERNAME_LOGIN_SELECTOR / MELBET_TAB_PHONE / … их заменят.
    "Melbet": {
        "login_url": "https://melbet.ru/ru/",
        "login_button_text": "Вход",
        "tab_selector": "[class*='tab' i], [role='tab']",
        "tab_phone": "По номеру телефона",
        "tab_login": "По номеру счета",
        "username_phone": "input[type='tel'], input[name*='phone' i]",
        "username_login": "input[name*='login' i], input[name*='userId' i], "
                          "input[type='text']",
        "username": "input[type='tel'], input[name*='login' i], "
                    "input[type='text']",
        "password": "input[type='password'], input[name*='password' i]",
        "submit": "button[type='submit']",
        "submit_text": "Вход",
        "balance": "[class*='balance' i], [class*='Balance']",
    },
    "BetBoom": {
        "login_url": "https://betboom.ru/",
        "login_button_text": "Вход",
        "username": "input[name='phone'], input[name='login']",
        "username_phone": "input[name='phone']",
        "username_login": "input[name='login']",
        "password": "input[name='password']",
        "submit_text": "Войти",
        # "[class*='MobileBalanceText']" — подтверждено по реальному DOM
        # (мобильная вёрстка: `<button class="MobileBalance...">…<p
        # class="…MobileBalanceText…">0 ₽</p></button>` в хедере). Хеш-суффикс
        # styled-components (после "-sc-...-N") может смениться при редеплое
        # сайта, поэтому селектор — по префиксу класса, не по полному имени.
        "balance": "[class*='MobileBalanceText'], [class*='balance'], "
                  "[class*='Balance']",
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

# Чем в принципе можно войти в каждую БК. Список нужен интерфейсу: не
# предлагать «вход по телефону» там, где БК его не принимает, — оператор
# иначе полчаса разбирался бы, почему форма не отправляется. Первый
# элемент — способ по умолчанию.
LOGIN_TYPES_BY_BOOKMAKER: dict[str, tuple[str, ...]] = {
    "Winline": (LOGIN_BY_PHONE, LOGIN_BY_LOGIN),
    "Melbet": (LOGIN_BY_PHONE, LOGIN_BY_LOGIN),
    "BetBoom": (LOGIN_BY_PHONE, LOGIN_BY_LOGIN),
    "Fonbet": (LOGIN_BY_LOGIN, LOGIN_BY_PHONE),
    "Liga Stavok": (LOGIN_BY_PHONE, LOGIN_BY_LOGIN),
    # bc.game — международный сайт, входа по российскому номеру у него нет
    # вовсе: там e-mail либо никнейм, и то и другое — «логин».
    "bc.game": (LOGIN_BY_LOGIN,),
}


def login_types(bookmaker: str) -> list[str]:
    """Способы входа, которые принимает эта БК (первый — по умолчанию)."""
    return list(LOGIN_TYPES_BY_BOOKMAKER.get(
        bookmaker, (LOGIN_BY_PHONE, LOGIN_BY_LOGIN)))


def default_login_type(bookmaker: str) -> str:
    return login_types(bookmaker)[0]


def resolve_login_type(bookmaker: str, value: str | None) -> str:
    """Способ входа, которым в эту БК можно войти на самом деле.

    От `normalize_login_type` отличается тем, что проверяет не «знаем ли
    мы такое слово», а принимает ли способ сама БК. Разница не
    теоретическая: колонка `login_type` появилась у аккаунтов миграцией,
    и всем записям, заведённым до выбора способа, досталось 'phone' —
    включая bc.game, куда по телефону не входят вовсе. Без этой проверки
    такой аккаунт искал бы на форме вкладку телефона, которой там нет, и
    падал бы с «сайт изменил вёрстку» на ровном месте."""
    allowed = login_types(bookmaker)
    value = normalize_login_type(value, allowed[0])
    return value if value in allowed else allowed[0]


_BALANCE_RE = re.compile(r"([\d\s]{1,9}[.,]?\d{0,2})\s*(?:₽|руб)", re.I)

# Число внутри текста баланса. Знака валюты НЕ требует: Winline рисует
# баланс голой цифрой («0», «1 234,56») без ₽ вообще.
_MONEY_RE = re.compile(r"\d[\d \u00a0]*(?:[.,]\d+)?")


def _parse_money(text: str, bookmaker: str) -> float:
    """«1 234,56 ₽» → 1234.56.

    Отдельной функцией, потому что прежний разбор на живом балансе падал
    с `ValueError: could not convert string to float: '\\n'`: запасная
    регулярка ловила пробельную строку, и Python валился с сообщением, по
    которому невозможно понять, что случилось на самом деле."""
    m = _MONEY_RE.search(text or "")
    if not m:
        raise RuntimeError(
            f"В тексте баланса {bookmaker} нет числа: {text!r}. Похоже, "
            f"селектор указывает не на тот элемент — проверьте "
            f"{env_prefix(bookmaker)}_BALANCE_VALUE_SELECTOR/"
            f"{env_prefix(bookmaker)}_BALANCE_SELECTOR.")
    clean = m.group(0).replace("\u00a0", "").replace(" ", "").replace(",", ".")
    return float(clean)


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

# Переключение вкладки способа входа. Ищем ТОЛЬКО среди элементов
# tab_selector, а не по всей странице: слово «Логин» есть и в шапке
# сайта. Точное совпадение текста предпочтительнее вхождения — «Логин»
# не должен выбрать вкладку «Логин или e-mail», если рядом есть точная.
# Возвращаем и список надписей всех вкладок: когда нужной не нашлось, по
# нему сразу видно, как БК назвала свои вкладки на самом деле.
_CLICK_TAB_JS = """
    const sel = arguments[0], want = arguments[1];
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
    const all = Array.from(document.querySelectorAll(sel)).filter(el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    });
    const options = all.map(el => norm(el.textContent)).filter(t => t);
    const lower = want.toLowerCase();
    let hit = all.find(el => norm(el.textContent) === want);
    if (!hit) {
        hit = all.find(el => norm(el.textContent).toLowerCase() === lower);
    }
    if (!hit) {
        hit = all.find(el => norm(el.textContent).toLowerCase()
                                .includes(lower));
    }
    if (!hit) return {clicked: false, text: null, options: options};
    hit.scrollIntoView({block: 'center'});
    hit.click();
    return {clicked: true, text: norm(hit.textContent), options: options};
"""

# Снимок формы входа для диагностики (inspect_login_form): видимые поля,
# кнопки и всё, что похоже на вкладку. Только чтение — ничего не жмём.
_DUMP_FORM_JS = """
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
    const seen = el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    const inputs = Array.from(document.querySelectorAll('input'))
        .filter(seen)
        .map(el => ({type: el.type, name: el.name, id: el.id,
                     placeholder: el.placeholder,
                     autocomplete: el.getAttribute('autocomplete'),
                     disabled: el.disabled, cls: el.className}));
    const buttons = Array.from(document.querySelectorAll(
        'button, [type="submit"]')).filter(seen)
        .map(el => ({text: norm(el.textContent), type: el.type,
                     disabled: el.disabled, cls: el.className}));
    const tabs = Array.from(document.querySelectorAll(
        '[class*="tab" i], [role="tab"]')).filter(seen)
        .map(el => norm(el.textContent)).filter(t => t && t.length < 40);
    return {inputs: inputs, buttons: buttons, tabs: tabs};
"""

# Признаки того, что БК не пустила на сайт ВООБЩЕ — до всякой формы
# входа. Отличать это от «селектор устарел» обязательно: советовать
# править селекторы человеку, которому сайт показал заглушку, — это
# отправить его чинить не то. Подтверждено: melbet.ru отдаёт «Пожалуйста,
# отключите VPN» адресам дата-центров, международные зеркала — /ru/block,
# fonbet.ru — «Forbidden» от антибота ServicePipe.
_BLOCK_PAGE_MARKERS = (
    "отключите vpn", "отключите впн", "доступ ограничен", "доступ запрещен",
    "доступ запрещён", "access denied", "forbidden", "/ru/block",
)


def _looks_blocked(driver) -> str:
    """Пусто — страница как страница. Иначе — что именно на ней найдено."""
    try:
        title = driver.title or ""
        url = driver.current_url or ""
        body = driver.find_element("tag name", "body").text[:2000]
    except Exception:  # noqa: BLE001
        return ""
    haystack = f"{title}\n{url}\n{body}".lower()
    hits = [m for m in _BLOCK_PAGE_MARKERS if m in haystack]
    return ", ".join(hits)


# Значение баланса рядом с его подписью. Ищем именно так, потому что по
# классу его не отличить от соседей: у Winline «В игре» и «Баланс» — это
# два одинаковых <div class="user-account__value"> подряд, и первый
# попавшийся оказывается суммой в незавершённых ставках. Разница не
# косметическая — от баланса считается лимит ставки.
#
# Подпись ищется по СОБСТВЕННОМУ тексту элемента (без текста потомков),
# иначе подошёл бы любой контейнер, внутри которого встречается слово
# «Баланс». От найденной подписи поднимаемся вверх на несколько уровней и
# берём первое значение — так работает и «подпись рядом со значением», и
# «подпись с значением в одной обёртке».
_BALANCE_BY_LABEL_JS = """
    const valueSel = arguments[0], want = arguments[1].toLowerCase();
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
    const seen = el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    for (const el of document.querySelectorAll('*')) {
        if (!seen(el)) continue;
        const own = norm(Array.from(el.childNodes)
            .filter(n => n.nodeType === 3)
            .map(n => n.textContent).join(' '));
        if (!own.toLowerCase().startsWith(want)) continue;
        let node = el;
        for (let up = 0; up < 4 && node.parentElement; up++) {
            node = node.parentElement;
            for (const v of node.querySelectorAll(valueSel)) {
                if (v === el || !seen(v)) continue;
                const text = norm(v.textContent);
                if (text) return text;
            }
        }
    }
    return null;
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


def env_prefix(bookmaker: str) -> str:
    """«Liga Stavok» → LIGA_STAVOK, «bc.game» → BCGAME: приставка всех
    переменных окружения, которыми настраивают вход в эту БК."""
    return bookmaker.upper().replace(" ", "_").replace(".", "") \
                            .replace("-", "_")


def _selector(bookmaker: str, key: str) -> str:
    env_key = f"{env_prefix(bookmaker)}_{key.upper()}_SELECTOR"
    return os.getenv(env_key, "") or SELECTORS.get(bookmaker, {}).get(key, "")


def _text_setting(bookmaker: str, key: str) -> str:
    """Настройка формы, которая не селектор, а видимый текст (название
    вкладки, надпись на кнопке). Переопределяется переменной окружения
    БЕЗ суффикса _SELECTOR: `WINLINE_TAB_LOGIN=Логин`."""
    env_key = f"{env_prefix(bookmaker)}_{key.upper()}"
    return os.getenv(env_key, "") or SELECTORS.get(bookmaker, {}).get(key, "")


def _username_selector(bookmaker: str, login_type: str) -> str:
    """Поле имени пользователя для выбранного способа входа.

    У БК с вкладками поля разные (Winline: auth-phone-base против
    auth-username), у остальных — одно общее `username` на оба способа."""
    key = ("username_phone" if login_type == LOGIN_BY_PHONE
           else "username_login")
    return _selector(bookmaker, key) or _selector(bookmaker, "username")


def _tab_text(bookmaker: str, login_type: str) -> str:
    """Видимая надпись вкладки нужного способа входа («Телефон»/«Логин»).
    Пусто — у БК вкладок нет, переключать нечего."""
    return _text_setting(bookmaker,
                         "tab_phone" if login_type == LOGIN_BY_PHONE
                         else "tab_login")


def _proxy_for(bookmaker: str) -> str | None:
    """`<BOOKMAKER>_PROXY` — резидентный/мобильный прокси именно для входа
    в эту БК (см. предупреждение о Fonbet/ServicePipe в докстринге модуля
    и LIGASTAVOK_PROXY в app/config.py — та же идея, но per-БК и только
    для standalone-браузера аккаунтов, не для общего браузера сканера)."""
    value = os.getenv(f"{env_prefix(bookmaker)}_PROXY", "").strip()
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
    словарей для driver.add_cookie(). Пустые/битые пары и записи с
    префиксом «ls:» (см. _parse_local_storage_string) пропускаются."""
    cookies: list[dict] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part or part.startswith("ls:"):
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if name:
            cookies.append({"name": name, "value": value})
    return cookies


def _parse_local_storage_string(raw: str) -> dict[str, str]:
    """Записи вида «ls:key=value» в той же строке, что и cookie (через
    «;») — для БК, где сессия хранится не в cookie браузера, а в
    window.localStorage (подтверждено на BetBoom: авторизация — JWT-токен,
    а не cookie, см. докстринг файла выше). Обычные записи без префикса
    «ls:» здесь игнорируются (это cookie, см. _parse_cookie_string)."""
    items: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part.startswith("ls:") or "=" not in part[len("ls:"):]:
            continue
        name, _, value = part[len("ls:"):].partition("=")
        name = name.strip()
        value = value.strip()
        if name:
            items[name] = value
    return items


def _fill_username(user_el, login: str, login_type: str) -> None:
    """Заполняет поле имени пользователя, нормализуя номер телефона.

    Подтверждено на Winline: поле телефона показывает фиксированный
    префикс «+7» ОТДЕЛЬНО от самого input (рядом лежит отключённый
    input[name='auth-phone-code'] со значением «+7») — туда нужно вводить
    10 цифр без кода страны. Если передать полный номер (11 цифр,
    начинается на 7/8), маска сдвигает цифры и получается чужой номер.

    Решает выбранный оператором способ входа, а не догадка по атрибутам
    поля: у 1xBet-движка (Melbet) «логин» — это номер клубной карты, тоже
    одни цифры, и от телефона его не отличить. Атрибуты поля остаются
    страховкой на случай, если БК держит оба способа в одном input и
    переключает у него только маску."""
    value = login
    if login_type == LOGIN_BY_PHONE:
        value = national_phone(value)
    else:
        try:
            attrs = (user_el.get_attribute("type") or "").lower() + " " + \
                (user_el.get_attribute("name") or "").lower()
        except Exception:  # noqa: BLE001
            attrs = ""
        if "tel" in attrs or "phone" in attrs:
            value = national_phone(value)
    user_el.clear()
    user_el.send_keys(value)


def _count_matches(driver, css: str) -> int:
    """Сколько ВИДИМЫХ элементов находит настроенный селектор. Ноль —
    селектор устарел; больше одного — он слишком широкий, и коннектор
    возьмёт первый попавшийся."""
    from selenium.webdriver.common.by import By
    if not css:
        return 0
    try:
        els = driver.find_elements(By.CSS_SELECTOR, css)
    except Exception:  # noqa: BLE001
        return 0
    return sum(1 for e in els if e.is_displayed())


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
                cookies: str | None = None,
                login_type: str = LOGIN_BY_PHONE):
        super().__init__(bookmaker, login, password, account_id=account_id,
                         cookies=cookies,
                         login_type=resolve_login_type(bookmaker, login_type))
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
        login_button_text = _text_setting(self.bookmaker,
                                          "login_button_text")
        if login_button_text and not self._open_login_form(driver):
            raise RuntimeError(
                f"Не удалось найти/нажать кнопку «{login_button_text}» для "
                f"открытия формы входа {self.bookmaker} — сайт изменил "
                f"вёрстку? Что на странице есть на самом деле, покажет "
                f"`python -m app.diagnose_account --form {self.bookmaker}`.")

        self._select_login_tab(driver)

        user_sel = _username_selector(self.bookmaker, self.login_type)
        pass_sel = _selector(self.bookmaker, "password")
        try:
            user_el = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, user_sel)))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Не удалось найти поле входа "
                f"{LOGIN_TYPE_NAMES[self.login_type]} у {self.bookmaker} "
                f"(селектор {user_sel!r}) — сайт изменил "
                f"вёрстку или эта БК так не пускает. Что реально лежит в "
                f"форме, покажет `python -m app.diagnose_account --form "
                f"{self.bookmaker}`: {exc}") from exc

        if _wait_for_captcha(driver):
            raise RuntimeError(
                f"{self.bookmaker} показал капчу (reCAPTCHA/hCaptcha) на "
                f"форме входа — автоматический вход невозможен без её "
                f"решения человеком (см. предупреждение в докстринге "
                f"этого файла). Войдите вручную на тестовом аккаунте, "
                f"прежде чем доверять авто-ставкам для этой БК.")

        try:
            _fill_username(user_el, self.login, self.login_type)
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
        submit_text = _text_setting(self.bookmaker, "submit_text")
        submitted = False
        if submit_sel:
            try:
                button = driver.find_element(By.CSS_SELECTOR, submit_sel)
                self._wait_submit_enabled(button)
                button.click()
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

    def _open_login_form(self, driver, wait_seconds: float = 15.0) -> bool:
        """Открывает модалку входа кнопкой в шапке сайта.

        У некоторых БК (подтверждено на Winline/BetBoom) полей логина и
        пароля просто НЕТ в DOM, пока эту кнопку не нажать. Клик именно с
        ожиданием: шапка у Winline рисуется Angular'ом через несколько
        секунд после загрузки страницы, и одна попытка «сразу» стабильно
        промахивается — выглядит это как «сайт поменял вёрстку», хотя
        вёрстка та же.

        True — кнопки не было в настройках (открывать нечего) либо она
        нажата."""
        text = _text_setting(self.bookmaker, "login_button_text")
        if not text:
            return True
        deadline = time.monotonic() + wait_seconds
        while True:
            if _click_by_text(driver, text):
                time.sleep(1.5)     # модалке нужно дорисоваться
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)

    def _wait_submit_enabled(self, button, wait_seconds: float = 6.0) -> None:
        """Ждёт, пока форма признает введённое и разблокирует кнопку.

        Подтверждено на Winline: «Войти» приходит с `disabled` и
        включается, только когда фронтенд принял и телефон, и пароль.
        Клик по отключённой кнопке Selenium выполняет молча и без
        ошибки — форма не отправляется, а коннектор идёт дальше и падает
        уже на чтении баланса, где причина не видна. Поэтому ждём здесь и
        говорим прямо: чаще всего это неверный ФОРМАТ реквизита (в поле
        телефона — логин или наоборот), а не сломанные селекторы."""
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if button.is_enabled():
                return
            time.sleep(0.3)
        if button.is_enabled():
            return
        raise RuntimeError(
            f"{self.bookmaker}: кнопка входа осталась заблокированной — "
            f"форма не приняла реквизиты, введённые как «"
            f"{LOGIN_TYPE_NAMES[self.login_type]}». Проверьте, тем ли "
            f"способом входа заведён аккаунт (телефон против логина) и "
            f"нет ли в нём лишних символов.")

    def _select_login_tab(self, driver) -> None:
        """Переключает форму на нужный способ входа (телефон/логин).

        У БК без вкладок (`tab_phone`/`tab_login` не заданы) шаг
        пропускается: там одно поле на оба способа."""
        tab_selector = _selector(self.bookmaker, "tab_selector")
        want = _tab_text(self.bookmaker, self.login_type)
        if not tab_selector or not want:
            return
        try:
            res = driver.execute_script(_CLICK_TAB_JS, tab_selector, want)
        except Exception as exc:  # noqa: BLE001
            log.info("%s: не удалось переключить вкладку входа «%s» (%s) — "
                     "пробую форму как есть", self.bookmaker, want, exc)
            return
        res = res or {}
        if res.get("clicked"):
            log.info("%s: вход %s — выбрана вкладка «%s»", self.bookmaker,
                     LOGIN_TYPE_NAMES[self.login_type], res.get("text"))
            time.sleep(1.0)     # вкладка перерисовывает поля формы
            return
        # Не нашли — не молчим: список реальных надписей и есть ответ на
        # вопрос «как эта БК назвала свои вкладки».
        raise RuntimeError(
            f"{self.bookmaker}: на форме входа нет вкладки «{want}» для "
            f"входа {LOGIN_TYPE_NAMES[self.login_type]}. Вкладки, которые "
            f"сайт показывает сейчас: "
            f"{', '.join(res.get('options') or []) or '— ни одной —'}. "
            f"Поправьте без правки кода: "
            f"{env_prefix(self.bookmaker)}_TAB_"
            f"{'PHONE' if self.login_type == LOGIN_BY_PHONE else 'LOGIN'}"
            f"=<надпись>.")

    def _try_cookie_session(self, driver, cfg_url: str) -> bool:
        """Пытается войти по сохранённой cookie вместо сценария логина —
        см. `self.cookies` (докстринг base.BookmakerConnector) и раздел
        README «Вход по cookie». Так обходится: капча BetBoom (она нужна
        только при самом входе, не для уже авторизованной сессии) и
        повторный СМС-код при каждом запуске.

        Возвращает True, если сессия похожа на залогиненную (или БК не
        даёт способа это проверить — см. `login_button_text`) — тогда
        `_login()` дальше не идёт по форме. False — cookie не подошла
        или протухла, откатываемся на обычный сценарий входа.

        Поддержаны два формата в `self.cookies` (можно смешивать через
        «;»): обычные cookie «name=value» и «ls:key=value» — запись в
        window.localStorage (нужна БК типа BetBoom, у которых сессия —
        JWT-токен в localStorage, а не браузерная cookie)."""
        raw = self.cookies or ""
        cookies = _parse_cookie_string(raw)
        local_storage = _parse_local_storage_string(raw)
        if not cookies and not local_storage:
            return False
        # add_cookie()/localStorage требуют, чтобы браузер уже был на нужном
        # домене — иначе Selenium отклонит cookie с несовпадающим доменом,
        # а localStorage окажется недоступен (SecurityError на about:blank).
        driver.get(cfg_url)
        added = 0
        for c in cookies:
            try:
                driver.add_cookie(c)
                added += 1
            except Exception as exc:  # noqa: BLE001
                log.debug("%s: cookie %s не принята браузером: %s",
                         self.bookmaker, c.get("name"), exc)
        for key, value in local_storage.items():
            try:
                driver.execute_script(
                    "window.localStorage.setItem(arguments[0], arguments[1]);",
                    key, value)
                added += 1
            except Exception as exc:  # noqa: BLE001
                log.debug("%s: localStorage[%s] не удалось установить: %s",
                         self.bookmaker, key, exc)
        if not added:
            log.warning("%s: ни одна cookie/localStorage-запись из "
                       "сохранённой сессии не принята браузером (не тот "
                       "домен/формат?) — использую обычный сценарий "
                       "входа.", self.bookmaker)
            return False
        driver.get(cfg_url)  # перезагрузка — чтобы сайт увидел новую сессию
        time.sleep(2.0)
        login_button_text = _text_setting(self.bookmaker,
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
        self._login()
        driver = self._ensure_driver()
        label = _text_setting(self.bookmaker, "balance_label")
        value_sel = _selector(self.bookmaker, "balance_value")
        if label and value_sel:
            return self._balance_by_label(driver, label, value_sel)
        return self._balance_by_selector(driver)

    def _balance_by_label(self, driver, label: str, value_sel: str) -> float:
        """Баланс по подписи рядом с ним («Баланс: 0»).

        Запасных вариантов у этого пути СОЗНАТЕЛЬНО нет. Он настраивается
        только там, где значение баланса неотличимо по классу от соседних
        чисел (Winline: «В игре» и «Баланс» — два одинаковых див подряд),
        и любой откат на «первый подходящий элемент» или на поиск денег по
        всей странице означал бы вернуть чужое число: сумму в игре, кэф
        или «5000 ₽» с рекламного баннера. Лучше честная ошибка и
        неизвестный баланс — при нём лимит ставки просто не показывается
        (см. accounts_manager.max_stake_for_bookmaker)."""
        deadline = time.monotonic() + 20
        text = None
        while time.monotonic() < deadline:
            time.sleep(1.0)
            try:
                text = driver.execute_script(_BALANCE_BY_LABEL_JS,
                                             value_sel, label)
            except Exception:  # noqa: BLE001
                text = None
            if text:
                break
        if not text:
            raise RuntimeError(
                f"Не нашёл баланс {self.bookmaker}: на странице нет подписи "
                f"«{label}» со значением рядом (селектор значения "
                f"{value_sel!r}). Либо вход не прошёл, либо сайт изменил "
                f"шапку — поправьте {env_prefix(self.bookmaker)}_"
                f"BALANCE_LABEL и {env_prefix(self.bookmaker)}_"
                f"BALANCE_VALUE_SELECTOR.")
        return _parse_money(text, self.bookmaker)

    def _balance_by_selector(self, driver) -> float:
        from selenium.webdriver.common.by import By
        balance_sel = _selector(self.bookmaker, "balance")
        deadline = time.monotonic() + 20
        text = ""
        while time.monotonic() < deadline:
            time.sleep(1.0)
            try:
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
            # запасной вариант: ищем денежный паттерн по всему тексту
            # страницы. Он требует ₽/руб именно поэтому — без знака
            # валюты сюда попал бы любой кэф со страницы.
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
            except Exception:  # noqa: BLE001
                body_text = ""
            m = _BALANCE_RE.search(body_text)
            if not m:
                raise RuntimeError(
                    f"Не удалось найти элемент баланса у {self.bookmaker} — "
                    f"проверьте/обновите селектор 'balance' (вход, "
                    f"возможно, тоже не прошёл: сайт мог показать капчу).")
            text = m.group(0)
        return _parse_money(text, self.bookmaker)

    def inspect_login_form(self) -> dict:
        """Что РЕАЛЬНО лежит в форме входа этой БК: вкладки, поля, кнопки.

        Нужно там, где селекторы не проверить из среды разработки:
        Melbet, например, не пускает на сайт никого, кроме российских
        домашних адресов, — DOM её формы можно увидеть только с боевого
        сервера. Ничего не заполняет и не отправляет: только открывает
        модалку и переписывает её содержимое, чтобы оператор мог сверить
        селекторы и при необходимости заменить их переменными окружения
        (см. `_selector`/`_text_setting`), не трогая код."""
        cfg_url = _selector(self.bookmaker, "login_url")
        if not cfg_url:
            raise NotImplementedError(
                f"Для {self.bookmaker} не настроен URL входа.")
        driver = self._ensure_driver()
        driver.get(cfg_url)
        time.sleep(3.0)
        out: dict = {"bookmaker": self.bookmaker, "url": cfg_url,
                     "title": driver.title, "opened_by": None, "tabs": {},
                     "blocked": _looks_blocked(driver)}

        login_button_text = _text_setting(self.bookmaker, "login_button_text")
        if login_button_text:
            out["opened_by"] = login_button_text
            out["opened"] = self._open_login_form(driver)
        out["form"] = driver.execute_script(_DUMP_FORM_JS)
        out["captcha"] = _has_visible_captcha(driver)

        for login_type in login_types(self.bookmaker):
            want = _tab_text(self.bookmaker, login_type)
            tab_selector = _selector(self.bookmaker, "tab_selector")
            entry: dict = {"tab_text": want, "selector":
                           _username_selector(self.bookmaker, login_type)}
            if want and tab_selector:
                res = driver.execute_script(_CLICK_TAB_JS, tab_selector,
                                            want) or {}
                entry["clicked"] = bool(res.get("clicked"))
                entry["matched_tab"] = res.get("text")
                entry["tabs_on_page"] = res.get("options")
                time.sleep(1.5)
            entry["form"] = driver.execute_script(_DUMP_FORM_JS)
            entry["selector_matches"] = _count_matches(driver,
                                                       entry["selector"])
            out["tabs"][login_type] = entry
        return out

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
