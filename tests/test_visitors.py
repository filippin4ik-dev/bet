"""Тесты списка посетителей и бана по устройству.

Проверяем то, ради чего это делалось: что в админке видно устройство и его
адрес, что бан реально закрывает сайт, что он переживает чистку cookie и
перезапуск — и, главное, что оператор не может запереть сам себя.

Использует ВРЕМЕННУЮ базу (не трогает arbs.sqlite3 из рабочего каталога).
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmpdir = tempfile.mkdtemp()
os.environ["DB_PATH"] = str(Path(_tmpdir) / "visitors-test.sqlite3")
os.environ["SECRET_KEY"] = "test-secret-key-not-for-prod"

from app import access, db, visitors  # noqa: E402
from app.security import sign_value, unsign_value  # noqa: E402

db.init_db()

UA_ANDROID = ("Mozilla/5.0 (Linux; Android 14; SM-A536E) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/138.0.0.0 Mobile Safari/537.36")
UA_WINDOWS = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36")
UA_IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
             "Mobile/15E148 Safari/604.1")


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    def __init__(self, host="203.0.113.9", ua=UA_ANDROID, cookies=None,
                 headers=None, path="/"):
        self.client = _FakeClient(host)
        head = {"user-agent": ua, "accept-language": "ru-RU,ru"}
        head.update({k.lower(): v for k, v in (headers or {}).items()})
        self.headers = head
        self.cookies = cookies or {}
        self.url = type("U", (), {"path": path, "query": ""})()


def _fresh():
    visitors.reset()
    for row in db.list_visitors():
        db.delete_visitor(row["device_id"])
    access.set_ip_blacklist("")
    access.invalidate_cache()


def _visit(request, ip=None):
    return visitors.observe(request, ip=ip or request.client.host,
                            path=request.url.path)


def test_user_agent_becomes_readable_device():
    """В админке нужно «Chrome на Android, телефон», а не строка UA."""
    d = visitors.describe(UA_ANDROID)
    assert d["browser"] == "Chrome 138" and d["os"] == "Android 14"
    assert d["kind"] == "телефон"

    d = visitors.describe(UA_WINDOWS)
    assert d["browser"] == "Chrome 137" and d["os"] == "Windows 10/11"
    assert d["kind"] == "компьютер"

    d = visitors.describe(UA_IPHONE)
    assert d["browser"] == "Safari 17" and d["os"] == "iOS 17.5"
    assert d["kind"] == "телефон"

    # Яндекс.Браузер и Edge притворяются Chrome — их правила идут раньше
    ya = visitors.describe(UA_WINDOWS + " YaBrowser/24.7.0.0")
    assert ya["browser"] == "Яндекс.Браузер 24"
    edge = visitors.describe(UA_WINDOWS + " Edg/126.0.0.0")
    assert edge["browser"] == "Edge 126"

    bot = visitors.describe("Mozilla/5.0 (compatible; Googlebot/2.1)")
    assert bot["kind"] == "бот"
    assert visitors.describe("")["kind"] == "неизвестно"
    print("OK: test_user_agent_becomes_readable_device")


def test_first_visit_gets_device_cookie_and_row():
    _fresh()
    first = _visit(_FakeRequest())
    assert first["set_cookie"] is True
    assert first["blocked"] is False
    assert unsign_value(first["cookie"]) == first["device_id"]

    # тот же браузер со своей cookie — то же устройство, а не новое
    again = _visit(_FakeRequest(
        cookies={visitors.COOKIE_NAME: first["cookie"]}))
    assert again["device_id"] == first["device_id"]
    assert again["set_cookie"] is False

    rows = visitors.snapshot()
    assert len(rows) == 1
    v = rows[0]
    assert v["hits"] == 2 and v["ip"] == "203.0.113.9"
    assert v["browser"] == "Chrome 138" and v["online"] is True
    print("OK: test_first_visit_gets_device_cookie_and_row")


def test_forged_cookie_is_ignored():
    """Подпись не даёт насочинять устройств: подделанная cookie считается
    отсутствующей, и посетителю выдаётся своя."""
    _fresh()
    forged = _visit(_FakeRequest(cookies={visitors.COOKIE_NAME: "чужое.id"}))
    assert forged["set_cookie"] is True
    assert forged["device_id"] != "чужое"
    assert unsign_value("чужое.id") is None
    assert unsign_value(sign_value("своё")) == "своё"
    print("OK: test_forged_cookie_is_ignored")


def test_block_device_closes_site_for_it_only():
    _fresh()
    guest = _visit(_FakeRequest(host="8.8.8.8"))
    mine = _visit(_FakeRequest(host="203.0.113.9", ua=UA_WINDOWS))

    visitors.set_blocked(guest["device_id"], True)
    assert visitors.is_blocked(guest["device_id"]) is True
    assert visitors.is_blocked(mine["device_id"]) is False

    blocked_again = _visit(_FakeRequest(
        host="8.8.8.8", cookies={visitors.COOKIE_NAME: guest["cookie"]}))
    assert blocked_again["blocked"] is True
    # свой заход по-прежнему свободен
    assert _visit(_FakeRequest(
        host="203.0.113.9", ua=UA_WINDOWS,
        cookies={visitors.COOKIE_NAME: mine["cookie"]}))["blocked"] is False
    print("OK: test_block_device_closes_site_for_it_only")


def test_ban_survives_cookie_wipe_from_same_ip_and_browser():
    """Cookie чистится в два клика, поэтому бан помнит ещё и отпечаток
    браузера: тот же браузер с того же адреса — то же устройство."""
    _fresh()
    guest = _visit(_FakeRequest(host="8.8.8.8"))
    visitors.set_blocked(guest["device_id"], True)

    # тот же браузер и адрес, но cookie удалена
    wiped = _visit(_FakeRequest(host="8.8.8.8"))
    assert wiped["device_id"] != guest["device_id"]
    assert wiped["blocked"] is True

    # тот же браузер, но ДРУГОЙ адрес — не блокируем: за одним адресом
    # может сидеть кто угодно ещё, и наоборот
    other_ip = _visit(_FakeRequest(host="1.1.1.1"))
    assert other_ip["blocked"] is False
    # тот же адрес, но другой браузер — тоже не блокируем (общий роутер)
    other_ua = _visit(_FakeRequest(host="8.8.8.8", ua=UA_WINDOWS))
    assert other_ua["blocked"] is False
    print("OK: test_ban_survives_cookie_wipe_from_same_ip_and_browser")


def test_unblock_also_frees_auto_blocked_twins():
    _fresh()
    guest = _visit(_FakeRequest(host="8.8.8.8"))
    visitors.set_blocked(guest["device_id"], True)
    twin = _visit(_FakeRequest(host="8.8.8.8"))
    assert twin["blocked"] is True

    visitors.set_blocked(guest["device_id"], False)
    assert visitors.is_blocked(twin["device_id"]) is False
    assert _visit(_FakeRequest(
        host="8.8.8.8",
        cookies={visitors.COOKIE_NAME: twin["cookie"]}))["blocked"] is False
    print("OK: test_unblock_also_frees_auto_blocked_twins")


def test_ban_survives_restart():
    """Бан пишется в базу сразу же: перезапуск сервера его не снимает."""
    _fresh()
    guest = _visit(_FakeRequest(host="8.8.8.8"))
    visitors.set_blocked(guest["device_id"], True)

    visitors.reset()          # как будто процесс перезапустили
    assert visitors.is_blocked(guest["device_id"]) is True
    rows = {v["device_id"]: v for v in visitors.snapshot()}
    assert rows[guest["device_id"]]["blocked"] is True
    print("OK: test_ban_survives_restart")


def test_visits_are_persisted_on_flush():
    _fresh()
    v = _visit(_FakeRequest(host="203.0.113.9"))
    visitors.flush()
    saved = {r["device_id"]: r for r in db.list_visitors()}
    assert v["device_id"] in saved
    assert saved[v["device_id"]]["browser"] == "Chrome 138"
    assert saved[v["device_id"]]["ip"] == "203.0.113.9"
    print("OK: test_visits_are_persisted_on_flush")


def test_forgotten_device_is_unbanned_and_gone():
    _fresh()
    guest = _visit(_FakeRequest(host="8.8.8.8"))
    visitors.set_blocked(guest["device_id"], True)
    assert visitors.forget(guest["device_id"]) is True
    assert visitors.is_blocked(guest["device_id"]) is False
    assert visitors.snapshot() == []
    assert db.list_visitors() == []
    assert visitors.forget("нет-такого") is False
    print("OK: test_forgotten_device_is_unbanned_and_gone")


def test_ip_ban_names_its_own_reason():
    """Забаненный адрес получает «banned», а не общее «войдите»: по этой
    причине шлюз показывает страницу блокировки, и с формой входа она
    ничего общего не имеет — вводить логин такому гостю бессмысленно."""
    _fresh()
    access.clear_password()
    access.set_whitelist("")

    access.set_ip_blacklist("8.8.8.8, 198.51.100.0/24")
    assert access.ip_banned("8.8.8.8") is True
    assert access.ip_banned("198.51.100.77") is True
    assert access.ip_banned("203.0.113.9") is False

    allowed, reason = access.check_request(_FakeRequest(host="8.8.8.8"))
    assert allowed is False and reason == "banned"
    # незабаненному адресу без сессии предлагают войти
    assert access.check_request(_FakeRequest(host="203.0.113.9")) == \
        (False, "login")
    access.set_ip_blacklist("")
    print("OK: test_ip_ban_names_its_own_reason")


def test_admin_session_is_never_banned():
    """Сессия админки обходит любой бан: иначе оператор, забанивший свой
    же адрес, остался бы снаружи без ssh."""
    from app.admin_api import COOKIE_NAME as ADMIN_COOKIE
    from app.security import create_session_token
    _fresh()
    access.set_ip_blacklist("8.8.8.8")
    req = _FakeRequest(host="8.8.8.8",
                       cookies={ADMIN_COOKIE: create_session_token("admin")})
    assert access.check_request(req)[0] is True
    access.set_ip_blacklist("")
    print("OK: test_admin_session_is_never_banned")


def test_admin_api_lists_and_blocks():
    from app import admin_api
    _fresh()
    guest = _visit(_FakeRequest(host="8.8.8.8"))
    me = _visit(_FakeRequest(host="203.0.113.9", ua=UA_WINDOWS))
    my_req = _FakeRequest(host="203.0.113.9", ua=UA_WINDOWS,
                          cookies={visitors.COOKIE_NAME: me["cookie"]})

    data = admin_api.list_visitors(request=my_req, username="admin")
    assert data["my_device"] == me["device_id"]
    assert data["my_ip"] == "203.0.113.9"
    assert {v["device_id"] for v in data["visitors"]} == \
        {guest["device_id"], me["device_id"]}

    res = admin_api.block_visitor(
        guest["device_id"], admin_api.VisitorBlockBody(blocked=True,
                                                       with_ip=True),
        request=my_req, username="admin")
    assert res["banned_ip"] == "8.8.8.8"
    assert access.ip_banned("8.8.8.8") is True
    assert visitors.is_blocked(guest["device_id"]) is True

    admin_api.block_visitor(
        guest["device_id"], admin_api.VisitorBlockBody(blocked=False),
        request=my_req, username="admin")
    assert visitors.is_blocked(guest["device_id"]) is False
    # адрес из чёрного списка сам не уходит — это отдельное решение
    assert access.ip_banned("8.8.8.8") is True
    access.set_ip_blacklist("")
    print("OK: test_admin_api_lists_and_blocks")


def test_admin_api_guards_against_banning_yourself():
    from fastapi import HTTPException

    from app import admin_api
    _fresh()
    me = _visit(_FakeRequest(host="203.0.113.9", ua=UA_WINDOWS))
    my_req = _FakeRequest(host="203.0.113.9", ua=UA_WINDOWS,
                          cookies={visitors.COOKIE_NAME: me["cookie"]})

    try:
        admin_api.block_visitor(
            me["device_id"], admin_api.VisitorBlockBody(blocked=True),
            request=my_req, username="admin")
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("бан собственного устройства должен падать")

    # свой адрес нельзя утащить в чёрный список и через «Забанить с IP»
    same_ip_guest = _visit(_FakeRequest(host="203.0.113.9", ua=UA_IPHONE))
    try:
        admin_api.block_visitor(
            same_ip_guest["device_id"],
            admin_api.VisitorBlockBody(blocked=True, with_ip=True),
            request=my_req, username="admin")
    except HTTPException as exc:
        assert exc.status_code == 400 and "203.0.113.9" in exc.detail
    else:
        raise AssertionError("бан собственного адреса должен падать")
    assert access.ip_banned("203.0.113.9") is False

    # и через раздел «Доступ к сайту» тоже
    try:
        admin_api.update_access(
            admin_api.AccessBody(ip_blacklist="203.0.113.9"),
            request=my_req, username="admin")
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("свой адрес в чёрном списке должен падать")
    print("OK: test_admin_api_guards_against_banning_yourself")


def test_registry_does_not_grow_forever():
    """Список — «кто ходит», а не архив: старые устройства вымываются,
    забаненные остаются (иначе бан тихо снимался бы сам)."""
    from app import config
    _fresh()
    guest = _visit(_FakeRequest(host="8.8.8.8"))
    visitors.set_blocked(guest["device_id"], True)
    for i in range(config.VISITORS_MAX + 20):
        _visit(_FakeRequest(host=f"10.0.{i // 250}.{i % 250}", ua=UA_WINDOWS))
    visitors.flush()
    rows = visitors.snapshot()
    assert len(rows) == config.VISITORS_MAX + 1
    assert visitors.is_blocked(guest["device_id"]) is True
    print("OK: test_registry_does_not_grow_forever")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("Все тесты прошли.")
