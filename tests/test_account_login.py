"""Тесты выбора способа входа в аккаунт БК: по телефону или по логину.

Офлайн: браузер не поднимается, сеть не трогается. Проверяется всё, что
коннектор решает ДО того, как открыть сайт, — какую вкладку формы нажать,
какое поле заполнить и что именно в него вписать.

Почему это вообще тесты, а не «и так видно»: угадать способ входа по самой
строке нельзя. Номер клубной карты Melbet — тоже одни цифры, и от телефона
его не отличить, поэтому способ выбирает оператор, а код обязан этот выбор
донести до формы без «умных» догадок по дороге.

Запуск: python3 tests/test_account_login.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.connectors import (LOGIN_BY_LOGIN, LOGIN_BY_PHONE,  # noqa: E402
                            LOGIN_TYPE_NAMES, MockConnector,
                            default_login_type, env_prefix, get_connector,
                            login_types, national_phone, normalize_login_type)
from app.connectors.selenium_generic import (  # noqa: E402
    SELECTORS, _fill_username, _looks_blocked, _parse_money, _selector,
    _tab_text, _username_selector)


class FakeInput:
    """Заглушка поля ввода: запоминает, что в него напечатали."""

    def __init__(self, **attrs):
        self.attrs = attrs
        self.typed = ""
        self.cleared = False

    def get_attribute(self, name):
        return self.attrs.get(name)

    def clear(self):
        self.cleared = True
        self.typed = ""

    def send_keys(self, value):
        self.typed += value


class FakeBody:
    def __init__(self, text):
        self.text = text


class FakePage:
    """Заглушка браузера: отдаёт заголовок, адрес и текст страницы."""

    def __init__(self, title="", url="https://example.com/", body=""):
        self.title = title
        self.current_url = url
        self._body = body

    def find_element(self, *_args):
        return FakeBody(self._body)


def test_unknown_login_type_falls_back_instead_of_crashing():
    """Способ входа приходит из базы и из переменных окружения — то есть
    из мест, где может оказаться что угодно (аккаунт, заведённый до
    появления выбора; опечатка в .env). Падать на этом нельзя: аккаунт
    просто входит способом по умолчанию."""
    assert normalize_login_type(None) == LOGIN_BY_PHONE
    assert normalize_login_type("") == LOGIN_BY_PHONE
    assert normalize_login_type("телефон") == LOGIN_BY_PHONE
    assert normalize_login_type("phone") == LOGIN_BY_PHONE
    assert normalize_login_type("  LOGIN  ") == LOGIN_BY_LOGIN
    # default — не константа: у bc.game телефона нет вовсе
    assert normalize_login_type("мусор", LOGIN_BY_LOGIN) == LOGIN_BY_LOGIN
    print("OK: test_unknown_login_type_falls_back_instead_of_crashing")


def test_phone_is_typed_without_country_code():
    """Подтверждено на Winline: рядом с полем телефона лежит отдельный
    ОТКЛЮЧЁННЫЙ input со значением «+7» — значит в само поле идут 10 цифр.
    Впишешь полный номер — маска сдвинет цифры, и БК получит чужой."""
    assert national_phone("+7 (999) 123-45-67") == "9991234567"
    assert national_phone("89991234567") == "9991234567"
    assert national_phone("79991234567") == "9991234567"
    assert national_phone("9991234567") == "9991234567"
    # не телефон (логин, случайно помеченный как телефон) — не калечим:
    # пусть БК сама скажет «неверный формат», это понятнее молчаливой
    # подмены реквизита
    assert national_phone("ivan_petrov") == "ivan_petrov"
    # 12 цифр — это уже не российский номер, обрезать первую цифру нельзя
    assert national_phone("712345678901") == "712345678901"
    print("OK: test_phone_is_typed_without_country_code")


def test_username_value_depends_on_chosen_type():
    """Одна и та же строка «+7 999 123-45-67» уходит в форму по-разному:
    как телефон — 10 цифрами, как логин — ровно как её ввёл оператор."""
    raw = "+7 (999) 123-45-67"
    as_phone = MockConnector("Winline", raw, "pw", login_type=LOGIN_BY_PHONE)
    as_login = MockConnector("Winline", raw, "pw", login_type=LOGIN_BY_LOGIN)
    assert as_phone.username_value == "9991234567"
    assert as_login.username_value == raw
    print("OK: test_username_value_depends_on_chosen_type")


def test_bookmaker_login_types_are_offered_honestly():
    """Список способов нужен интерфейсу: предложить «вход по телефону»
    там, где БК его не принимает, — обречь оператора разбираться, почему
    форма молча не отправляется."""
    assert login_types("Winline") == [LOGIN_BY_PHONE, LOGIN_BY_LOGIN]
    assert login_types("Melbet") == [LOGIN_BY_PHONE, LOGIN_BY_LOGIN]
    # bc.game — международный сайт: там e-mail или никнейм, телефона нет
    assert login_types("bc.game") == [LOGIN_BY_LOGIN]
    assert default_login_type("bc.game") == LOGIN_BY_LOGIN
    # Fonbet исторически логин-первая: первым идёт способ по умолчанию
    assert default_login_type("Fonbet") == LOGIN_BY_LOGIN
    # незнакомая БК — оба способа, чтобы не запрещать лишнего
    assert login_types("Новая БК") == [LOGIN_BY_PHONE, LOGIN_BY_LOGIN]
    assert set(LOGIN_TYPE_NAMES) == {LOGIN_BY_PHONE, LOGIN_BY_LOGIN}
    print("OK: test_bookmaker_login_types_are_offered_honestly")


def test_connector_never_gets_type_the_bookmaker_rejects():
    """Даже если в базе у аккаунта bc.game лежит «phone» (сохранился с
    тех пор, когда способ был один), коннектор должен пойти по логину:
    иначе он ищет вкладку телефона, которой на сайте нет."""
    conn = get_connector("bc.game", "u@example.com", "pw",
                         login_type=LOGIN_BY_PHONE)
    assert conn.login_type == LOGIN_BY_LOGIN
    # у Winline телефон принимается — выбор оператора остаётся в силе
    conn = get_connector("Winline", "89991234567", "pw",
                         login_type=LOGIN_BY_PHONE)
    assert conn.login_type == LOGIN_BY_PHONE
    conn = get_connector("Winline", "ivan", "pw", login_type=LOGIN_BY_LOGIN)
    assert conn.login_type == LOGIN_BY_LOGIN
    # способ не указан вовсе — берём дефолт этой БК, а не общий
    assert get_connector("bc.game", "u", "p").login_type == LOGIN_BY_LOGIN
    assert get_connector("Winline", "u", "p").login_type == LOGIN_BY_PHONE
    print("OK: test_connector_never_gets_type_the_bookmaker_rejects")


def test_each_login_type_has_its_own_field():
    """Главное, ради чего всё затевалось: вкладки формы — это РАЗНЫЕ
    поля. Селекторы Winline сняты с живой модалки (см. докстринг
    app/connectors/selenium_generic.py)."""
    assert _username_selector("Winline", LOGIN_BY_PHONE) == \
        "input[name='auth-phone-base']"
    assert _username_selector("Winline", LOGIN_BY_LOGIN) == \
        "input[name='auth-username']"
    assert _tab_text("Winline", LOGIN_BY_PHONE) == "Телефон"
    assert _tab_text("Winline", LOGIN_BY_LOGIN) == "Логин"
    print("OK: test_each_login_type_has_its_own_field")


def test_bookmaker_without_tabs_uses_one_field_for_both():
    """У Fonbet/Liga Stavok вкладок в настройках нет — там одно поле на
    оба способа, и переключать нечего. Проверка на регресс: попытка
    «умно» потребовать username_phone сломала бы вход в такие БК."""
    for bk in ("Fonbet", "Liga Stavok"):
        common = _selector(bk, "username")
        assert common
        assert _username_selector(bk, LOGIN_BY_PHONE) == common
        assert _username_selector(bk, LOGIN_BY_LOGIN) == common
        assert _tab_text(bk, LOGIN_BY_PHONE) == ""
        assert _tab_text(bk, LOGIN_BY_LOGIN) == ""
    print("OK: test_bookmaker_without_tabs_uses_one_field_for_both")


def test_selectors_are_overridable_without_touching_code():
    """Форму Melbet не видно из среды разработки (сайт не пускает адреса
    дата-центров), поэтому её селекторы обязаны настраиваться на боевом
    сервере переменными окружения — иначе правка вёрстки у БК требует
    релиза."""
    assert env_prefix("Liga Stavok") == "LIGA_STAVOK"
    assert env_prefix("bc.game") == "BCGAME"
    assert env_prefix("Melbet") == "MELBET"
    keys = ("MELBET_USERNAME_LOGIN_SELECTOR", "MELBET_TAB_PHONE")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ["MELBET_USERNAME_LOGIN_SELECTOR"] = "input#account-id"
        os.environ["MELBET_TAB_PHONE"] = "По телефону"
        assert _username_selector("Melbet", LOGIN_BY_LOGIN) == \
            "input#account-id"
        assert _tab_text("Melbet", LOGIN_BY_PHONE) == "По телефону"
        # соседний способ переменной не задет
        assert _username_selector("Melbet", LOGIN_BY_PHONE) == \
            SELECTORS["Melbet"]["username_phone"]
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert _tab_text("Melbet", LOGIN_BY_PHONE) == \
        SELECTORS["Melbet"]["tab_phone"]
    print("OK: test_selectors_are_overridable_without_touching_code")


def test_filled_field_gets_the_right_string():
    """Что в итоге напечатано в поле. Отдельно от username_value: здесь
    ещё и страховка по атрибутам поля — на случай БК, которая держит оба
    способа в одном input и переключает у него только маску."""
    el = FakeInput(type="tel", name="auth-phone-base")
    _fill_username(el, "+7 (999) 123-45-67", LOGIN_BY_PHONE)
    assert el.cleared and el.typed == "9991234567"

    el = FakeInput(type="text", name="auth-username")
    _fill_username(el, "ivan_petrov", LOGIN_BY_LOGIN)
    assert el.typed == "ivan_petrov"

    # вход «по логину», но поле у БК — телефонное: значит это тот самый
    # общий input, и код страны всё равно лишний
    el = FakeInput(type="tel", name="login")
    _fill_username(el, "89991234567", LOGIN_BY_LOGIN)
    assert el.typed == "9991234567"

    # поле без атрибутов не должно ронять заполнение
    el = FakeInput()
    _fill_username(el, "ivan", LOGIN_BY_LOGIN)
    assert el.typed == "ivan"
    print("OK: test_filled_field_gets_the_right_string")


def test_block_page_is_not_mistaken_for_broken_selectors():
    """Когда БК показала заглушку вместо сайта, полей на странице нет —
    и разведка формы обязана сказать именно это. Совет «поправьте
    селекторы» отправил бы оператора чинить не то: чинить надо адрес, с
    которого он ходит."""
    assert _looks_blocked(FakePage(title="Пожалуйста, отключите VPN")) \
        == "отключите vpn"
    assert _looks_blocked(FakePage(url="https://melbet.com/ru/block")) \
        == "/ru/block"
    assert _looks_blocked(FakePage(body="Forbidden")) == "forbidden"
    # обычная страница БК заглушкой считаться не должна
    assert _looks_blocked(FakePage(
        title="Официальный сайт Букмекерской компании Winline",
        url="https://winline.ru/",
        body="Ставки на спорт Войти Регистрация Телефон Логин")) == ""

    # браузер, который не отвечает, — не повод объявить сайт закрытым
    class Dead:
        @property
        def title(self):
            raise RuntimeError("no session")
    assert _looks_blocked(Dead()) == ""
    print("OK: test_block_page_is_not_mistaken_for_broken_selectors")


def test_balance_number_is_read_without_a_currency_sign():
    """Winline рисует баланс голой цифрой, без ₽ (снято с живого счёта
    2026-07-30: `<div class="user-account__value">0</div>`), и разделяет
    тысячи неразрывным пробелом."""
    assert _parse_money("0", "Winline") == 0.0
    assert _parse_money("1 234,56", "Winline") == 1234.56
    assert _parse_money("1\u00a0234.56", "Winline") == 1234.56
    assert _parse_money("12 345,67 ₽", "BetBoom") == 12345.67
    assert _parse_money("Баланс: 750 руб", "BetBoom") == 750.0
    print("OK: test_balance_number_is_read_without_a_currency_sign")


def test_balance_without_a_number_says_what_is_wrong():
    """На этом прежний разбор падал живьём: пустой элемент баланса давал
    `ValueError: could not convert string to float: '\\n'` — сообщение, по
    которому невозможно понять, что случилось."""
    for junk in ("\n", "", "   ", "—", None):
        try:
            _parse_money(junk, "Winline")
        except RuntimeError as exc:
            assert "WINLINE_BALANCE" in str(exc)
        else:
            raise AssertionError(f"{junk!r} не число, нужна понятная ошибка")
    print("OK: test_balance_without_a_number_says_what_is_wrong")


def test_winline_balance_is_found_by_label_not_by_class():
    """Защита от подмены баланса суммой в игре. В шапке Winline два
    одинаковых `.user-account__value`: первый — «В игре» (на живом счёте
    было 84 619 при нулевом балансе), второй — сам баланс. Поиск по
    классу взял бы первый, и лимит ставки считался бы от денег, которых
    на счету нет, — поэтому у Winline настроен поиск по подписи, а
    неоднозначного `balance` быть не должно."""
    winline = SELECTORS["Winline"]
    assert winline["balance_label"] == "Баланс"
    assert winline["balance_value"] == ".user-account__value"
    assert "balance" not in winline, \
        "запасной селектор по классу вернул бы «В игре» вместо баланса"
    print("OK: test_winline_balance_is_found_by_label_not_by_class")


if __name__ == "__main__":
    test_unknown_login_type_falls_back_instead_of_crashing()
    test_phone_is_typed_without_country_code()
    test_username_value_depends_on_chosen_type()
    test_bookmaker_login_types_are_offered_honestly()
    test_connector_never_gets_type_the_bookmaker_rejects()
    test_each_login_type_has_its_own_field()
    test_bookmaker_without_tabs_uses_one_field_for_both()
    test_selectors_are_overridable_without_touching_code()
    test_filled_field_gets_the_right_string()
    test_block_page_is_not_mistaken_for_broken_selectors()
    test_balance_number_is_read_without_a_currency_sign()
    test_balance_without_a_number_says_what_is_wrong()
    test_winline_balance_is_found_by_label_not_by_class()
    print("Все тесты прошли.")
