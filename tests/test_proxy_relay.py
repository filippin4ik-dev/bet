"""Тесты локального ретранслятора прокси (app/proxy_relay.py).

Браузер не умеет логин/пароль у прокси; ретранслятор на 127.0.0.1 принимает
CONNECT без авторизации и туннелит в настоящий прокси с логином. Здесь
настоящий прокси изображают крошечные SOCKS5- и HTTP-серверы в потоке,
проверяющие логин/пароль, а «сайт» — эхо-сервер.

Запуск: python3 -m pytest tests/test_proxy_relay.py
"""
import socket
import struct
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import proxy_relay  # noqa: E402
from app.proxy_relay import ProxyRelay, browser_proxy, has_credentials  # noqa: E402


def _serve(handler):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handler, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return srv, srv.getsockname()[1]


def _pipe(a, b):
    def one(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    threading.Thread(target=one, args=(a, b), daemon=True).start()
    one(b, a)


def _recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise OSError("closed")
        buf += chunk
    return buf


@pytest.fixture
def echo():
    def handler(conn):
        with conn:
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                conn.sendall(b"echo:" + data)

    srv, port = _serve(handler)
    yield port
    srv.close()


@pytest.fixture
def socks5():
    """SOCKS5 с авторизацией логином/паролем; записывает, кто заходил."""
    seen = {"auth": [], "targets": []}

    def handler(conn):
        try:
            ver, n = _recv_exact(conn, 2)
            methods = _recv_exact(conn, n)
            if 2 not in methods:
                conn.sendall(b"\x05\xff")
                return
            conn.sendall(b"\x05\x02")
            _recv_exact(conn, 1)                       # subnegotiation ver
            ulen = _recv_exact(conn, 1)[0]
            user = _recv_exact(conn, ulen).decode()
            plen = _recv_exact(conn, 1)[0]
            pwd = _recv_exact(conn, plen).decode()
            seen["auth"].append((user, pwd))
            if (user, pwd) != ("user_country-DE_session-a1", "secret"):
                conn.sendall(b"\x01\x01")
                return
            conn.sendall(b"\x01\x00")
            _, cmd, _, atyp = _recv_exact(conn, 4)
            if atyp == 1:
                host = socket.inet_ntoa(_recv_exact(conn, 4))
            elif atyp == 3:
                host = _recv_exact(conn, _recv_exact(conn, 1)[0]).decode()
            else:
                conn.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)
                return
            port = struct.unpack("!H", _recv_exact(conn, 2))[0]
            seen["targets"].append((host, port))
            up = socket.create_connection((host, port), timeout=5)
            conn.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
            _pipe(conn, up)
        except OSError:
            pass
        finally:
            conn.close()

    srv, port = _serve(handler)
    yield port, seen
    srv.close()


@pytest.fixture
def http_proxy():
    """HTTP-прокси с Proxy-Authorization: Basic."""
    seen = {"auth": []}

    def handler(conn):
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                head += conn.recv(4096)
            lines = head.decode().split("\r\n")
            target = lines[0].split(" ")[1]
            auth = next((l.split(":", 1)[1].strip() for l in lines
                         if l.lower().startswith("proxy-authorization:")), "")
            seen["auth"].append(auth)
            if auth != "Basic dXNlcjpwYXNz":           # user:pass
                conn.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
                return
            host, port = target.rsplit(":", 1)
            up = socket.create_connection((host, int(port)), timeout=5)
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            _pipe(conn, up)
        except OSError:
            pass
        finally:
            conn.close()

    srv, port = _serve(handler)
    yield port, seen
    srv.close()


def _connect_through(relay_addr, target_port):
    host, port = relay_addr.replace("http://", "").split(":")
    c = socket.create_connection((host, int(port)), timeout=5)
    c.sendall(f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\n"
              f"Host: 127.0.0.1:{target_port}\r\n\r\n".encode())
    reply = b""
    while b"\r\n\r\n" not in reply:
        chunk = c.recv(4096)
        if not chunk:
            break
        reply += chunk
    return c, reply.split(b"\r\n", 1)[0]


def test_relay_tunnels_through_socks5_with_credentials(echo, socks5):
    socks_port, seen = socks5
    relay = ProxyRelay(
        f"socks5://user_country-DE_session-a1:secret@127.0.0.1:{socks_port}")
    relay.start()
    try:
        c, status = _connect_through(relay.address, echo)
        assert status == b"HTTP/1.1 200 Connection Established"
        c.sendall(b"ping")
        assert c.recv(100) == b"echo:ping"
        c.close()
    finally:
        relay.close()
    assert seen["auth"] == [("user_country-DE_session-a1", "secret")]
    assert seen["targets"] == [("127.0.0.1", echo)]


def test_relay_reports_upstream_refusal_as_502(echo, socks5):
    socks_port, seen = socks5
    relay = ProxyRelay(f"socks5://wrong:creds@127.0.0.1:{socks_port}")
    relay.start()
    try:
        c, status = _connect_through(relay.address, echo)
        assert status == b"HTTP/1.1 502 Bad Gateway"
        c.close()
    finally:
        relay.close()


def test_relay_tunnels_through_http_proxy_with_basic_auth(echo, http_proxy):
    http_port, seen = http_proxy
    relay = ProxyRelay(f"http://user:pass@127.0.0.1:{http_port}")
    relay.start()
    try:
        c, status = _connect_through(relay.address, echo)
        assert status == b"HTTP/1.1 200 Connection Established"
        c.sendall(b"hi")
        assert c.recv(100) == b"echo:hi"
        c.close()
    finally:
        relay.close()
    assert seen["auth"] == ["Basic dXNlcjpwYXNz"]


def test_relay_rejects_plain_requests():
    relay = ProxyRelay("socks5://u:p@127.0.0.1:1")
    relay.start()
    try:
        host, port = relay.address.replace("http://", "").split(":")
        c = socket.create_connection((host, int(port)), timeout=5)
        c.sendall(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
        assert c.recv(4096).startswith(b"HTTP/1.1 405")
        c.close()
    finally:
        relay.close()


def test_relay_listens_only_on_loopback():
    relay = ProxyRelay("socks5://u:p@127.0.0.1:1")
    try:
        assert relay.address.startswith("http://127.0.0.1:")
    finally:
        relay.close()


def test_browser_proxy_passes_plain_addresses_and_relays_credentials(monkeypatch):
    assert browser_proxy(None) is None
    assert browser_proxy("") is None
    assert browser_proxy("socks5://1.2.3.4:1080") == "socks5://1.2.3.4:1080"
    assert browser_proxy("http://1.2.3.4") is None      # без порта — нечего давать
    assert not has_credentials("socks5://1.2.3.4:1080")
    assert has_credentials("socks5://u:p@1.2.3.4:1080")
    monkeypatch.setattr(proxy_relay, "_relays", {})
    first = browser_proxy("socks5://u:p@127.0.0.1:1")
    second = browser_proxy("socks5://u:p@127.0.0.1:1")
    assert first.startswith("http://127.0.0.1:") and first == second
    other = browser_proxy("socks5://u:p@127.0.0.1:2")
    assert other != first
    for relay in proxy_relay._relays.values():
        relay.close()


def test_bad_upstream_addresses_are_rejected():
    with pytest.raises(ValueError):
        ProxyRelay("socks5://u:p@nohost")
    with pytest.raises(ValueError):
        ProxyRelay("ftp://u:p@127.0.0.1:21")
