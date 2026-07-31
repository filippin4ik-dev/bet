"""Тесты закрытого доступа к сайту: вход, белый список IP, строгий режим.

Проверяем именно то, из-за чего доступ обычно ломают: чтобы посторонний не
прошёл, чтобы оператор не запер себя сам и чтобы X-Forwarded-For нельзя было
подделать в обход белого списка. Учётки игроков — в tests/test_players.py.

Использует ВРЕМЕННУЮ базу (не трогает arbs.sqlite3 из рабочего каталога).
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmpdir = tempfile.mkdtemp()
os.environ["DB_PATH"] = str(Path(_tmpdir) / "access-test.sqlite3")
os.environ["SECRET_KEY"] = "test-secret-key-not-for-prod"

from app import access, config, db  # noqa: E402
from app.security import hash_password, verify_password_hash  # noqa: E402

db.init_db()


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    """Минимальный request: шлюзу нужны только адрес, cookie и заголовки."""

    def __init__(self, host="203.0.113.9", headers=None, cookies=None,
                 path="/"):
        self.client = _FakeClient(host)
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.cookies = cookies or {}
        self.url = type("U", (), {"path": path, "query": ""})()


def _reset():
    access.clear_password()
    access.set_whitelist("")
    access.set_ip_only(False)
    access.invalidate_cache()


def test_site_is_closed_even_when_nothing_configured():
    """Сайт закрыт всегда: без входа посетитель не видит ни вилок, ни
    линии. Раньше пустые настройки означали «открыт всем» — теперь вход
    по логину и паролю, и попасть внутрь без сессии нельзя (реквизиты
    администратора при этом работают, запереть себя нечем)."""
    _reset()
    assert access.gate_enabled() is True
    allowed, reason = access.check_request(_FakeRequest())
    assert allowed is False and reason == "login"
    print("OK: test_site_is_closed_even_when_nothing_configured")


def test_password_hash_roundtrip():
    """Пароль доступа хранится хэшем, а не текстом (в т.ч. в базе)."""
    stored = hash_password("s3cret-pass")
    assert "s3cret-pass" not in stored
    assert verify_password_hash(stored, "s3cret-pass")
    assert not verify_password_hash(stored, "s3cret-Pass")
    assert not verify_password_hash("мусор", "s3cret-pass")
    print("OK: test_password_hash_roundtrip")


def test_shared_password_still_opens_the_site():
    """Общий пароль остался запасным входом: учётки игроков — основной
    путь, но старые установки с SITE_PASSWORD не должны остаться снаружи."""
    _reset()
    access.set_password("tajna123")
    assert access.password_source() == "db"
    assert db.get_setting("site_password_hash") != "tajna123"

    allowed, reason = access.check_request(_FakeRequest())
    assert allowed is False and reason == "login"

    assert access.check_password("tajna123") is True
    assert access.check_password("другой") is False
    assert access.check_password("") is False
    # пароль админки тоже подходит: два пароля помнить незачем
    assert access.check_password(config.ADMIN_PASSWORD) is True

    # с сессионной cookie — пускаем
    req = _FakeRequest(cookies={access.COOKIE_NAME: access.session_cookie()})
    assert access.check_request(req)[0] is True
    # с испорченной — нет
    bad = _FakeRequest(cookies={access.COOKIE_NAME: "мусор.подпись"})
    assert access.check_request(bad)[0] is False
    print("OK: test_shared_password_still_opens_the_site")


def test_whitelist_alone_no_longer_lets_anyone_in():
    """Белый список больше не пропуск: у каждого игрока свой журнал
    ставок, и «свой адрес» не отвечает на вопрос, кто именно пришёл.
    Списку осталась роль ограничения (строгий режим)."""
    _reset()
    access.set_password("tajna123")
    access.set_whitelist("203.0.113.9\n198.51.100.0/24")
    assert access.whitelist() == ["203.0.113.9/32", "198.51.100.0/24"]

    for ip in ("203.0.113.9", "198.51.100.77", "8.8.8.8"):
        assert access.check_request(_FakeRequest(ip)) == (False, "login")
    # с сессией — пускаем, откуда бы ни пришли
    req = _FakeRequest("8.8.8.8",
                       cookies={access.COOKIE_NAME: access.session_cookie()})
    assert access.check_request(req)[0] is True
    print("OK: test_whitelist_alone_no_longer_lets_anyone_in")


def test_ip_only_blocks_everyone_else():
    """Строгий режим: посторонним не показываем даже форму входа, и
    действующая cookie их тоже не спасает."""
    _reset()
    access.set_password("tajna123")
    access.set_whitelist("203.0.113.9")
    access.set_ip_only(True)

    allowed, reason = access.check_request(_FakeRequest("8.8.8.8"))
    assert allowed is False and reason == "ip"
    with_cookie = _FakeRequest(
        "8.8.8.8", cookies={access.COOKIE_NAME: access.session_cookie()})
    assert access.check_request(with_cookie)[0] is False
    # свой адрес проходит ограничение, но войти всё равно надо
    assert access.check_request(_FakeRequest("203.0.113.9")) == (False, "login")
    ok = _FakeRequest("203.0.113.9",
                      cookies={access.COOKIE_NAME: access.session_cookie()})
    assert access.check_request(ok)[0] is True
    print("OK: test_ip_only_blocks_everyone_else")


def test_ip_only_without_list_does_not_lock_out():
    """Строгий режим с пустым списком закрыл бы сайт для всех, включая
    оператора — считаем его невключённым (а API админки такое и не даст
    сохранить)."""
    _reset()
    access.set_ip_only(True)
    assert access.whitelist() == []
    assert access.state()["ip_only"] is False
    access.set_password("tajna123")
    allowed, reason = access.check_request(_FakeRequest("8.8.8.8"))
    assert allowed is False and reason == "login"   # вход, а не отказ
    print("OK: test_ip_only_without_list_does_not_lock_out")


def test_forwarded_for_only_trusted_from_local_proxy():
    """Главная ловушка белого списка: клиент из интернета не должен
    подставлять себе чужой адрес заголовком X-Forwarded-For. Верим
    заголовку только от локального nginx."""
    _reset()
    access.set_password("tajna123")
    access.set_whitelist("203.0.113.9")

    access.set_ip_only(True)

    spoof = _FakeRequest("8.8.8.8",
                         headers={"X-Forwarded-For": "203.0.113.9"},
                         cookies={access.COOKIE_NAME: access.session_cookie()})
    assert access.client_ip(spoof) == "8.8.8.8"
    assert access.check_request(spoof) == (False, "ip")

    # тот же заголовок от своего nginx — учитываем, причём ПОСЛЕДНИЙ адрес
    # цепочки (его дописал nginx, всё раньше прислал клиент)
    proxied = _FakeRequest(
        "127.0.0.1",
        headers={"X-Forwarded-For": "9.9.9.9, 203.0.113.9"},
        cookies={access.COOKIE_NAME: access.session_cookie()})
    assert access.client_ip(proxied) == "203.0.113.9"
    assert access.check_request(proxied)[0] is True

    # X-Real-IP от nginx, когда цепочки нет
    real = _FakeRequest("127.0.0.1", headers={"X-Real-IP": "203.0.113.9"})
    assert access.client_ip(real) == "203.0.113.9"
    print("OK: test_forwarded_for_only_trusted_from_local_proxy")


def test_whitelist_parsing_rejects_garbage():
    assert access.parse_whitelist("") == []
    assert access.parse_whitelist("203.0.113.9, 203.0.113.9") == \
        ["203.0.113.9/32"]
    assert access.parse_whitelist("2001:db8::1") == ["2001:db8::1/128"]
    try:
        access.parse_whitelist("не-адрес")
    except ValueError as exc:
        assert "не похоже на IP" in str(exc)
    else:
        raise AssertionError("мусор в списке должен отвергаться с пояснением")
    print("OK: test_whitelist_parsing_rejects_garbage")


def test_login_bruteforce_is_limited():
    _reset()
    ip = "8.8.4.4"
    for _ in range(config.SITE_LOGIN_MAX_FAILS):
        assert access.too_many_fails(ip) is False
        access.note_fail(ip)
    assert access.too_many_fails(ip) is True
    access.note_success(ip)
    assert access.too_many_fails(ip) is False
    print("OK: test_login_bruteforce_is_limited")


def test_admin_api_guards_against_self_lockout():
    """Админка не даёт включить строгий режим, если своего адреса в списке
    нет — иначе оператор отрежет себя от сайта одним кликом."""
    from fastapi import HTTPException

    from app import admin_api
    _reset()
    req = _FakeRequest("203.0.113.9")
    try:
        admin_api.update_access(
            admin_api.AccessBody(whitelist="198.51.100.5", ip_only=True),
            request=req, username="admin")
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "203.0.113.9" in exc.detail
    else:
        raise AssertionError("строгий режим без своего адреса должен падать")

    # со своим адресом — сохраняется
    state = admin_api.update_access(
        admin_api.AccessBody(whitelist="203.0.113.9", ip_only=True),
        request=req, username="admin")
    assert state["ip_only"] is True
    assert state["whitelist"] == ["203.0.113.9/32"]

    # пустой список при включённом строгом режиме тоже не пройдёт
    try:
        admin_api.update_access(admin_api.AccessBody(whitelist=""),
                                request=req, username="admin")
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("очистка списка в строгом режиме должна падать")

    # слишком короткий пароль бессмысленен
    _reset()
    try:
        admin_api.update_access(admin_api.AccessBody(password="123"),
                                request=req, username="admin")
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("короткий пароль должен отвергаться")
    print("OK: test_admin_api_guards_against_self_lockout")


def test_password_can_be_removed():
    _reset()
    access.set_password("tajna123")
    assert access.password_set() is True
    access.clear_password()
    assert access.password_set() is False
    assert access.check_password("tajna123") is False
    print("OK: test_password_can_be_removed")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("Все тесты прошли.")
