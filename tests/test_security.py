"""Тесты защиты соединения и шлюза сайта (app/security.py, app/main.py).

Проверяем ровно то, ради чего защита и сделана: неавторизованного не
пускают ни на страницу, ни к API; cookie не уезжает по открытому HTTP;
запрос с чужого сайта нашей cookie ничего не меняет; сайт не отвечает по
чужому домену; ответы API не остаются в кэше.

Использует ВРЕМЕННУЮ базу (не трогает arbs.sqlite3 из рабочего каталога).
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmpdir = tempfile.mkdtemp()
os.environ["DB_PATH"] = str(Path(_tmpdir) / "security-test.sqlite3")
os.environ["SECRET_KEY"] = "test-secret-key-not-for-prod"

from fastapi.responses import JSONResponse  # noqa: E402

from app import access, config, db, main, security  # noqa: E402

db.init_db()


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeUrl:
    def __init__(self, path="/", query="", scheme="http"):
        self.path, self.query, self.scheme = path, query, scheme

    def replace(self, scheme):
        return f"{scheme}://arb.example.ru{self.path}"

    def __str__(self):
        return f"{self.scheme}://arb.example.ru{self.path}"


class _FakeRequest:
    def __init__(self, path="/", method="GET", host="203.0.113.9",
                 headers=None, cookies=None, scheme="http", query=""):
        self.client = _FakeClient(host)
        self.method = method
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.cookies = cookies or {}
        self.url = _FakeUrl(path, query, scheme)


async def _ok(_request):
    return JSONResponse({"ok": True})


def _gate(request):
    """Прогоняет запрос через шлюз и отдаёт ответ (как это делает сервер)."""
    api = request.url.path.startswith("/api/")
    resp = asyncio.run(main._guarded_response(request, _ok, api=api))
    for name, value in security.security_headers(request, api=api).items():
        resp.headers[name] = value
    return resp


def _session_cookies():
    return {access.COOKIE_NAME: access.session_cookie()}


def _reset():
    access.clear_password()
    access.set_whitelist("")
    access.set_ip_only(False)
    access.set_ip_blacklist("")
    access.invalidate_cache()


# ---------- шлюз: неавторизованных откидывает ----------

def test_page_without_session_goes_to_login():
    _reset()
    resp = _gate(_FakeRequest("/"))
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/login?next=%2F"


def test_login_redirect_remembers_where_the_visitor_was_going():
    _reset()
    resp = _gate(_FakeRequest("/profile", query="tab=bets"))
    assert "next=%2Fprofile%3Ftab%3Dbets" in resp.headers["location"]


def test_api_without_session_answers_401_and_not_a_redirect():
    """Страницу можно увести на форму входа, а вот опрос вилок должен
    получить именно 401: по нему открытая вкладка понимает, что сессия
    кончилась, и сама уходит на /login."""
    _reset()
    resp = _gate(_FakeRequest("/api/arbs"))
    assert resp.status_code == 401


def test_static_of_the_site_is_closed_too():
    """Закрыт весь сайт, а не только страницы: app.js посторонним не
    отдаём — иначе по нему видно устройство API."""
    _reset()
    assert _gate(_FakeRequest("/static/app.js")).status_code in (302, 307)
    # страница входа и её файлы — открыты, иначе войти было бы нечем
    assert _gate(_FakeRequest("/login")).status_code == 200
    assert _gate(_FakeRequest("/static/login.js")).status_code == 200


def test_session_opens_the_site():
    _reset()
    resp = _gate(_FakeRequest("/api/arbs", cookies=_session_cookies()))
    assert resp.status_code == 200


def test_banned_address_gets_the_door_even_with_a_session():
    _reset()
    access.set_ip_blacklist("203.0.113.9")
    resp = _gate(_FakeRequest("/api/arbs", cookies=_session_cookies()))
    assert resp.status_code == 403
    _reset()


# ---------- запрос с чужого сайта ----------

def test_request_from_another_site_changes_nothing():
    _reset()
    foreign = _FakeRequest(
        "/api/admin/players", method="POST", cookies=_session_cookies(),
        headers={"host": "arb.example.ru", "origin": "https://evil.example"})
    assert _gate(foreign).status_code == 403


def test_request_from_our_own_page_passes():
    _reset()
    own = _FakeRequest(
        "/api/admin/players", method="POST", cookies=_session_cookies(),
        headers={"host": "arb.example.ru",
                 "origin": "https://arb.example.ru"})
    assert _gate(own).status_code == 200


def test_a_client_without_a_page_is_not_a_csrf_risk():
    """curl и systemd не присылают Origin — подделывать там нечего: чужой
    страницы, которая отправила бы запрос вашей cookie, не существует."""
    _reset()
    cli = _FakeRequest("/api/admin/players", method="POST",
                       cookies=_session_cookies())
    assert _gate(cli).status_code == 200


def test_same_origin_ignores_the_port_of_the_proxy():
    req = _FakeRequest(headers={"host": "arb.example.ru",
                                "origin": "https://arb.example.ru:443"})
    assert security.same_origin(req) is True


# ---------- чужой домен ----------

def test_foreign_domain_is_not_served(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_HOSTS", ("arb.example.ru",))
    _reset()
    ours = _FakeRequest("/api/arbs", cookies=_session_cookies(),
                        headers={"host": "arb.example.ru"})
    assert _gate(ours).status_code == 200
    theirs = _FakeRequest("/api/arbs", cookies=_session_cookies(),
                          headers={"host": "phish.example"})
    assert _gate(theirs).status_code == 421


def test_domain_check_does_not_break_localhost(monkeypatch):
    """С самого сервера по localhost проверяют, жив ли сайт, и им же идёт
    диагностика на VPS — заданный домен ломать это не должен."""
    monkeypatch.setattr(config, "TRUSTED_HOSTS", ("arb.example.ru",))
    _reset()
    loopback = _FakeRequest("/api/arbs", host="127.0.0.1",
                            cookies=_session_cookies(),
                            headers={"host": "localhost:8000"})
    assert _gate(loopback).status_code == 200


def test_localhost_in_the_header_is_not_a_way_around_the_domain(monkeypatch):
    """Снаружи в заголовок можно написать что угодно: «localhost» от
    постороннего адреса домен не открывает."""
    monkeypatch.setattr(config, "TRUSTED_HOSTS", ("arb.example.ru",))
    _reset()
    spoof = _FakeRequest("/api/arbs", host="203.0.113.9",
                         cookies=_session_cookies(),
                         headers={"host": "localhost"})
    assert _gate(spoof).status_code == 421


# ---------- заголовки ----------

def test_security_headers_are_on_every_answer():
    _reset()
    resp = _gate(_FakeRequest("/login"))
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


def test_api_answers_are_never_cached():
    """Вилки, балансы и списки игроков не должны оставаться ни в кэше
    браузера, ни у прокси по пути."""
    _reset()
    resp = _gate(_FakeRequest("/api/arbs", cookies=_session_cookies()))
    assert resp.headers["Cache-Control"] == "no-store"


def test_hsts_only_when_the_connection_is_already_secure():
    plain = security.security_headers(_FakeRequest(), api=False)
    assert "Strict-Transport-Security" not in plain
    secure = security.security_headers(_FakeRequest(scheme="https"),
                                       api=False)
    assert "max-age" in secure["Strict-Transport-Security"]


# ---------- cookie ----------

def test_cookie_secure_flag_follows_the_scheme(monkeypatch):
    monkeypatch.setattr(config, "COOKIE_SECURE", "auto")
    assert security.cookie_secure(_FakeRequest()) is False
    assert security.cookie_secure(_FakeRequest(scheme="https")) is True
    monkeypatch.setattr(config, "COOKIE_SECURE", "1")
    assert security.cookie_secure(_FakeRequest()) is True
    monkeypatch.setattr(config, "COOKIE_SECURE", "0")
    assert security.cookie_secure(_FakeRequest(scheme="https")) is False


def test_https_behind_the_proxy_is_recognised_only_from_the_proxy(monkeypatch):
    """X-Forwarded-Proto верим только своему nginx: иначе посетитель одним
    заголовком объявил бы соединение защищённым и получил бы cookie без
    флага Secure — то есть уходящую и по открытому HTTP."""
    monkeypatch.setattr(config, "SITE_TRUST_PROXY", True)
    spoof = _FakeRequest(host="8.8.8.8",
                         headers={"x-forwarded-proto": "https"})
    assert security.is_https(spoof) is False
    proxied = _FakeRequest(host="127.0.0.1",
                           headers={"x-forwarded-proto": "https"})
    assert security.is_https(proxied) is True


def test_session_cookie_is_httponly_and_scoped(monkeypatch):
    monkeypatch.setattr(config, "COOKIE_SECURE", "1")

    class _Resp:
        def __init__(self):
            self.kwargs = None

        def set_cookie(self, name, value, **kwargs):
            self.kwargs = dict(kwargs, name=name)

    resp = _Resp()
    security.set_session_cookie(resp, _FakeRequest(), "site_session", "t",
                                ttl=3600, samesite="strict")
    assert resp.kwargs["httponly"] is True
    assert resp.kwargs["secure"] is True
    assert resp.kwargs["samesite"] == "strict"
    assert resp.kwargs["path"] == "/"
