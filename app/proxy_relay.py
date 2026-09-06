"""Локальный ретранслятор прокси: браузер → 127.0.0.1:порт → прокси с логином.

Chrome не умеет логин/пароль у `--proxy-server` — ни через расширение
(branded google-chrome отказывается грузить `--load-extension`), ни через
CDP (Fetch.authRequired на авторизацию у ПРОКСИ не срабатывает; оба пути
проверены). А у резидентных прокси-провайдеров авторизация почти всегда
именно логином/паролем, и в логин часто зашиты параметры вроде страны и
липкой сессии (`user_country-DE_session-abc`), которые авторизацией по IP
не передать.

Поэтому поднимаем в процессе сканера крошечный HTTP-прокси без авторизации
на loopback, который каждое CONNECT-соединение браузера прокидывает в
настоящий прокси уже с логином и паролем (SOCKS5 — через PySocks, HTTP —
заголовком Proxy-Authorization). Браузеру отдаётся адрес
`http://127.0.0.1:<порт>`. Слушаем ТОЛЬКО 127.0.0.1: снаружи ретранслятор
не виден, иначе через него любой ходил бы в интернет за наш счёт.

Один ретранслятор на один upstream-адрес: повторный `relay_for` с тем же
адресом отдаёт уже поднятый. Поток-приёмник — демон, вместе с процессом
умирает и он.
"""
import logging
import select
import socket
import threading
from urllib.parse import urlsplit

log = logging.getLogger("proxy_relay")

_lock = threading.Lock()
_relays: dict[str, "ProxyRelay"] = {}

# Пауза без трафика в обе стороны, после которой туннель закрывается:
# у браузера соединения к сайту живут минутами, но не вечно.
IDLE_TIMEOUT = 300.0
CONNECT_TIMEOUT = 40.0


def has_credentials(proxy_url: str | None) -> bool:
    """Есть ли в адресе прокси логин (`scheme://user:pass@host:port`)."""
    if not proxy_url:
        return False
    try:
        return bool(urlsplit(proxy_url).username)
    except ValueError:
        return False


def relay_for(upstream: str) -> str:
    """Адрес локального ретранслятора для этого upstream (поднимет, если
    ещё нет). Бросает исключение, если upstream не разобрать."""
    with _lock:
        relay = _relays.get(upstream)
        if relay is None or not relay.alive:
            relay = ProxyRelay(upstream)
            relay.start()
            _relays[upstream] = relay
        return relay.address


def browser_proxy(proxy_url: str | None) -> str | None:
    """Что отдать браузеру в `--proxy-server`.

    Без логина — сам адрес (scheme://host:port), с логином — адрес
    локального ретранслятора. None — прокси нет.
    """
    if not proxy_url:
        return None
    u = urlsplit(proxy_url)
    if not u.hostname or not u.port:
        return None
    if not u.username:
        return f"{u.scheme or 'http'}://{u.hostname}:{u.port}"
    return relay_for(proxy_url)


class ProxyRelay:
    """HTTP CONNECT-прокси на loopback, туннелящий в upstream с логином."""

    def __init__(self, upstream: str, host: str = "127.0.0.1",
                 port: int = 0) -> None:
        u = urlsplit(upstream)
        if not u.hostname or not u.port:
            raise ValueError(f"прокси без host:port: {upstream!r}")
        self.scheme = (u.scheme or "http").lower()
        if self.scheme not in ("socks5", "socks5h", "socks4", "http",
                               "https"):
            raise ValueError(f"неизвестная схема прокси: {self.scheme}")
        self.host, self.port_up = u.hostname, u.port
        self.username, self.password = u.username, u.password
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((host, port))
        self._srv.listen(64)
        self.port = self._srv.getsockname()[1]
        self.address = f"http://{host}:{self.port}"
        self.alive = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.alive = True
        self._thread = threading.Thread(
            target=self._serve, name=f"proxy-relay:{self.port}", daemon=True)
        self._thread.start()
        log.info("Прокси-ретранслятор %s → %s://%s:%s (логин %s)",
                 self.address, self.scheme, self.host, self.port_up,
                 self.username or "нет")

    def close(self) -> None:
        self.alive = False
        try:
            self._srv.close()
        except OSError:
            pass

    # ---------- приём ----------

    def _serve(self) -> None:
        while self.alive:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        upstream = None
        try:
            client.settimeout(CONNECT_TIMEOUT)
            request = self._read_head(client)
            if request is None:
                return
            method, target = request
            if method != "CONNECT":
                # Обычные (не CONNECT) запросы браузер шлёт только для
                # http:// — сайты БК все на https, поддерживать не будем.
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n"
                               b"Connection: close\r\n\r\n")
                return
            host, _, port = target.rpartition(":")
            upstream = self._open_upstream(host, int(port or 443))
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            client.settimeout(None)
            upstream.settimeout(None)
            self._pipe(client, upstream)
        except Exception as exc:  # noqa: BLE001 — любой сбой = 502 клиенту
            log.debug("ретранслятор %s: %s", self.address, exc)
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n"
                               b"Connection: close\r\n\r\n")
            except OSError:
                pass
        finally:
            for s in (client, upstream):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    @staticmethod
    def _read_head(client: socket.socket) -> tuple[str, str] | None:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = client.recv(4096)
            if not chunk:
                return None
            buf += chunk
            if len(buf) > 65536:
                return None
        line = buf.split(b"\r\n", 1)[0].decode("latin-1")
        parts = line.split(" ")
        if len(parts) < 2:
            return None
        return parts[0].upper(), parts[1]

    def _open_upstream(self, host: str, port: int) -> socket.socket:
        if self.scheme.startswith("socks"):
            import socks  # PySocks — зависимость requests[socks]
            s = socks.socksocket()
            kind = socks.SOCKS4 if self.scheme == "socks4" else socks.SOCKS5
            # rdns=True: имя резолвит сам прокси, как и браузер за ним, —
            # иначе резидентный выход и DNS-ответ разойдутся по регионам.
            s.set_proxy(kind, self.host, self.port_up, True,
                        self.username, self.password)
            s.settimeout(CONNECT_TIMEOUT)
            s.connect((host, port))
            return s
        # HTTP-прокси: CONNECT с Proxy-Authorization
        s = socket.create_connection((self.host, self.port_up),
                                     timeout=CONNECT_TIMEOUT)
        if self.scheme == "https":
            import ssl
            s = ssl.create_default_context().wrap_socket(
                s, server_hostname=self.host)
        head = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        if self.username:
            import base64
            token = base64.b64encode(
                f"{self.username}:{self.password or ''}".encode()).decode()
            head += f"Proxy-Authorization: Basic {token}\r\n"
        s.sendall((head + "\r\n").encode())
        reply = b""
        while b"\r\n\r\n" not in reply:
            chunk = s.recv(4096)
            if not chunk:
                break
            reply += chunk
        status = reply.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if " 200" not in status:
            s.close()
            raise OSError(f"upstream-прокси ответил: {status or 'пусто'}")
        return s

    @staticmethod
    def _pipe(a: socket.socket, b: socket.socket) -> None:
        pair = {a: b, b: a}
        while True:
            ready, _, _ = select.select([a, b], [], [], IDLE_TIMEOUT)
            if not ready:
                return
            for s in ready:
                try:
                    data = s.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                pair[s].sendall(data)
