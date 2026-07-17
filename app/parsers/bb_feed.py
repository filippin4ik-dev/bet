"""Клиент фида BetBoom (sporthub) поверх websocket + protobuf.

BetBoom больше не отдаёт прематч-линию в HTML: React-приложение получает
события по бинарному websocket-фиду (`wss://<partner>-ws2.sporthub.bet/
api/tree_ws/v1`, протокол protobuf `bb.sport_ws.v1`). Прежний парсер через
Selenium прокликивал по 8 лиг на вид спорта и собирал лишь ~100 матчей.

Здесь мы подключаемся к тому же фиду напрямую и забираем ВСЮ прематч-линию
(несколько тысяч матчей за ~10 секунд, без браузера):

    сервер линии
      → state_subscribe_by_sports  (список видов спорта + счётчики)
      → state_subscribe_sports      (id турниров внутри вида спорта)
      → state_subscribe_tournaments (матчи турнира вместе со ставками)

Схему сообщений (номера полей) сняли с бандла виджета sportbook. Нам нужен
лишь небольшой её кусок, поэтому protobuf реализован вручную (varint +
length-delimited + double), без сгенерированного кода и лишних зависимостей.
При смене схемы правьте номера полей ниже — логика поиска вилок не меняется.
"""
import logging
import secrets
import struct
import time

log = logging.getLogger("parsers.bb_feed")

# Кандидаты хостов сервера линии (partner name = "ru"). Первый рабочий
# запоминается между циклами. Домены/номера меняются редко, но fallback
# на всякий случай оставлен.
FEED_HOSTS = [
    "wss://ru-ws2.sporthub.bet/api/tree_ws/v1",
    "wss://ru-ws.sporthub.bet:444/api/tree_ws/v1",
    "wss://ru-ws1.sporthub.bet/api/tree_ws/v1",
    "wss://ru-ws.sporthub.bet/api/tree_ws/v1",
]
ORIGIN = "https://betboom.ru"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Значения enum из бандла (bb.sport_ws.v1.common.TreeTypes / LANGUAGES)
TREE_LIVE = 1
TREE_PREMATCH = 2
LANG_RU = 8

# Номера полей oneof `type` в bb.sport_ws.v1.MainRequest / MainResponse
REQ_UNSUBSCRIBE = 2
REQ_SETTINGS_SET = 3
REQ_STATE_BY_SPORTS = 4
REQ_STATE_SPORTS = 6
REQ_STATE_TOURNAMENTS = 8
REQ_STATE_MATCHES = 16
RESP_STATE_BY_SPORTS = 5
RESP_STATE_SPORTS = 7
RESP_STATE_TOURNAMENTS = 9
RESP_STATE_MATCHES = 17

# Сколько турниров подписывать одним запросом (сервер отдаёт их пачкой).
TOURNAMENTS_PER_REQUEST = 10
# Серверный лимит одновременных подписок full_match на одно соединение.
# Получив роспись матча, сразу отписываемся (REQ_UNSUBSCRIBE) и подписываем
# следующий — так «окно» из 10 подписок прокатывается по всем матчам.
MATCH_SUBS_LIMIT = 10


# ---------------------------------------------------------------------------
# Минимальный protobuf (только то, что нужно этому фиду)
# ---------------------------------------------------------------------------

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint(field << 3 | wire)


def _f_len(field: int, payload: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(payload)) + payload


def _f_str(field: int, s: str) -> bytes:
    return _f_len(field, s.encode("utf-8"))


def _f_varint(field: int, v: int) -> bytes:
    return _tag(field, 0) + _varint(v)


def _decode(buf: bytes) -> dict:
    """Разбирает protobuf-сообщение в dict: номер поля → список значений.

    Значения: int (varint), bytes (length-delimited — вложенное сообщение
    или строка), float (fixed32/fixed64). Разбор ленивый — вложенные
    сообщения разбираем по требованию повторным вызовом _decode(bytes).
    """
    out: dict[int, list] = {}
    i, n = 0, len(buf)
    while i < n:
        tag = 0
        shift = 0
        while True:
            b = buf[i]
            i += 1
            tag |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
        field, wire = tag >> 3, tag & 7
        if wire == 0:  # varint
            v = 0
            shift = 0
            while True:
                b = buf[i]
                i += 1
                v |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
        elif wire == 2:  # length-delimited
            ln = 0
            shift = 0
            while True:
                b = buf[i]
                i += 1
                ln |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
            v = buf[i:i + ln]
            i += ln
        elif wire == 1:  # 64-bit (double)
            v = struct.unpack_from("<d", buf, i)[0]
            i += 8
        elif wire == 5:  # 32-bit (float)
            v = struct.unpack_from("<f", buf, i)[0]
            i += 4
        else:
            raise ValueError(f"protobuf wire type {wire} не поддерживается")
        out.setdefault(field, []).append(v)
    return out


def _one(d: dict, field: int, default=None):
    vals = d.get(field)
    return vals[0] if vals else default


def _text(d: dict, field: int) -> str:
    v = _one(d, field)
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else ""


def _uid() -> str:
    return secrets.token_hex(3)


# ---------------------------------------------------------------------------
# Клиент фида
# ---------------------------------------------------------------------------

class FeedError(Exception):
    pass


class BBFeedClient:
    """Одно websocket-подключение к серверу линии BetBoom.

    Хранит первый удачный хост между вызовами (передаётся парсером).
    Использование:
        client = BBFeedClient()
        for match in client.crawl():
            ...
    """

    def __init__(self, host: str | None = None,
                 overall_timeout: float = 60.0,
                 tree_type: int = TREE_PREMATCH) -> None:
        self.host = host
        self.overall_timeout = overall_timeout
        # тип дерева: TREE_PREMATCH (линия) или TREE_LIVE (лайв)
        self.tree_type = tree_type
        self._ws = None

    # ---- соединение ----

    def _connect(self):
        import websocket  # ленивый импорт: зависимость нужна только тут

        hosts = ([self.host] if self.host else []) + \
            [h for h in FEED_HOSTS if h != self.host]
        last_exc = None
        for host in hosts:
            try:
                ws = websocket.create_connection(
                    host, timeout=15, origin=ORIGIN,
                    header={"User-Agent": USER_AGENT})
                self.host = host
                self._ws = ws
                log.info("BetBoom feed: подключено к %s", host)
                return ws
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                log.debug("BetBoom feed: %s не подошёл (%s)", host, exc)
        raise FeedError(f"ни один хост фида не ответил: {last_exc}")

    def _send(self, main_field: int, payload: bytes) -> None:
        import websocket
        self._ws.send(_f_len(main_field, payload),
                      opcode=websocket.ABNF.OPCODE_BINARY)

    def _recv(self) -> dict:
        frame = self._ws.recv()
        if isinstance(frame, str):
            frame = frame.encode("utf-8", "replace")
        return _decode(frame)

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    # ---- запросы ----

    def _settings_set(self) -> None:
        # time_filter="all" — показать и сегодняшние, и будущие матчи
        # (без этого сервер по умолчанию отдаёт урезанный список).
        body = (_f_str(1, _uid()) + _f_str(2, "all") +
                _f_varint(3, LANG_RU))
        self._send(REQ_SETTINGS_SET, body)

    def _list_sports(self, deadline: float) -> list[int]:
        packed_types = _varint(self.tree_type)
        self._send(REQ_STATE_BY_SPORTS,
                   _f_str(1, _uid()) + _f_len(2, packed_types))
        while time.monotonic() < deadline:
            m = self._recv()
            if RESP_STATE_BY_SPORTS not in m:
                continue
            resp = _decode(m[RESP_STATE_BY_SPORTS][0])
            sport_ids: list[int] = []
            for state_raw in resp.get(5, []):
                state = _decode(state_raw)
                for sp_raw in state.get(2, []):
                    sp = _decode(sp_raw)
                    info = _decode(sp[1][0]) if 1 in sp else {}
                    sid = _one(info, 1)
                    count = _one(info, 5, 0)
                    if sid and count:
                        sport_ids.append(sid)
            return sport_ids
        raise FeedError("нет ответа state_subscribe_by_sports")

    def _list_tournaments(self, sport_id: int,
                          deadline: float) -> list[int]:
        sub = _f_str(1, _uid()) + _f_varint(2, self.tree_type) + \
            _f_varint(3, sport_id)
        self._send(REQ_STATE_SPORTS, _f_str(1, _uid()) + _f_len(2, sub))
        while time.monotonic() < deadline:
            m = self._recv()
            if RESP_STATE_SPORTS not in m:
                continue
            resp = _decode(m[RESP_STATE_SPORTS][0])
            ids: list[int] = []
            for sp_sub_raw in resp.get(5, []):
                sp_sub = _decode(sp_sub_raw)
                if 6 not in sp_sub:
                    continue
                sport = _decode(sp_sub[6][0])
                for t_raw in sport.get(2, []):
                    tinfo = _decode(_decode(t_raw)[1][0])
                    tid = _one(tinfo, 1)
                    if tid:
                        ids.append(tid)
            return ids
        return []

    def _subscribe_tournaments(self, tournament_ids: list[int],
                               deadline: float):
        subs = b"".join(
            _f_len(2, _f_str(1, _uid()) + _f_varint(2, self.tree_type) +
                   _f_varint(3, tid))
            for tid in tournament_ids)
        self._send(REQ_STATE_TOURNAMENTS, _f_str(1, _uid()) + subs)
        need = len(tournament_ids)
        got = 0
        while got < need and time.monotonic() < deadline:
            m = self._recv()
            if RESP_STATE_TOURNAMENTS not in m:
                continue
            resp = _decode(m[RESP_STATE_TOURNAMENTS][0])
            for tsub_raw in resp.get(5, []):
                got += 1
                tsub = _decode(tsub_raw)
                # field 6 = sport, field 8 = tournament (с матчами)
                sport_name = ""
                if 6 in tsub:
                    sport_info = _decode(_decode(tsub[6][0]).get(1, [b""])[0])
                    sport_name = _text(sport_info, 2)
                if 8 not in tsub:
                    continue
                tour = _decode(tsub[8][0])
                for match_raw in tour.get(3, []):
                    yield sport_name, _decode(match_raw)

    def subscribe_matches(self, match_ids: list[int], deadline: float):
        """Полная роспись матчей: генератор (match_id, match_dict).

        В дереве турниров у матча только топ-ставки (~7 штук: исход,
        основная фора и тотал). Подписка state_subscribe_matches отдаёт
        ВСЕ рынки матча (сотни ставок: таймы, карты, угловые, ЖК и т.д.).

        Сервер разрешает максимум MATCH_SUBS_LIMIT одновременных подписок
        full_match на соединение, поэтому получив снапшот матча мы сразу
        отписываемся и подписываем следующий матч.
        """
        got = 0
        i = 0
        inflight = 0
        n = len(match_ids)
        while got < n and time.monotonic() < deadline:
            while i < n and inflight < MATCH_SUBS_LIMIT:
                sub = _f_str(1, _uid()) + _f_varint(2, match_ids[i])
                self._send(REQ_STATE_MATCHES,
                           _f_str(1, _uid()) + _f_len(2, sub))
                i += 1
                inflight += 1
            try:
                m = self._recv()
            except Exception:  # noqa: BLE001  (таймаут сокета и т.п.)
                break
            for raw in m.get(RESP_STATE_MATCHES, []):
                resp = _decode(raw)
                for sub_raw in resp.get(5, []):
                    got += 1
                    inflight -= 1
                    sub = _decode(sub_raw)
                    # освобождаем слот подписки (uid эхом в поле 4)
                    sub_uid = _text(sub, 4)
                    if sub_uid:
                        self._send(REQ_UNSUBSCRIBE,
                                   _f_str(1, _uid()) + _f_str(2, sub_uid))
                    if 8 not in sub:
                        continue  # матч не найден / ошибка — пропускаем
                    match = _decode(sub[8][0])
                    if 1 not in match:
                        continue
                    info = _decode(match[1][0])
                    mid = _one(info, 1)
                    if mid:
                        yield mid, match
        if got < n:
            log.warning("BetBoom feed: полная роспись получена для %d из %d "
                        "матчей (таймаут)", got, n)

    # ---- высокоуровневый обход ----

    def crawl(self):
        """Генератор (sport_name, match_dict) по всей прематч-линии.

        match_dict — разобранный ModelsMatch: {1: [info_bytes...],
        2: [stake_bytes...]}. Разбирает вызывающий (см. betboom.py).
        """
        deadline = time.monotonic() + self.overall_timeout
        self._connect()
        self._settings_set()
        sport_ids = self._list_sports(deadline)
        log.info("BetBoom feed: видов спорта с прематчем: %d", len(sport_ids))

        tournament_ids: list[int] = []
        for sid in sport_ids:
            if time.monotonic() >= deadline:
                break
            tournament_ids.extend(
                self._list_tournaments(sid, min(deadline,
                                                time.monotonic() + 10)))
        log.info("BetBoom feed: турниров: %d", len(tournament_ids))

        for i in range(0, len(tournament_ids), TOURNAMENTS_PER_REQUEST):
            if time.monotonic() >= deadline:
                log.warning("BetBoom feed: превышен общий таймаут (%ds) — "
                            "отдаю, что успели", int(self.overall_timeout))
                break
            batch = tournament_ids[i:i + TOURNAMENTS_PER_REQUEST]
            yield from self._subscribe_tournaments(
                batch, min(deadline, time.monotonic() + 15))
