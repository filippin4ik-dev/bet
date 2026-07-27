"""Клиент бинарного websocket-фида Winline (data_ng).

Winline (Angular SPA) получает ВСЮ линию не HTML'ом, а через собственный
бинарный websocket `wss://wss.winline.ru/data_ng`. Раньше парсер рендерил
страницы браузером (Selenium) и собирал лишь верхушку ленты; теперь мы
подключаемся к тому же фиду напрямую и получаем:

- шаг 16 (меню)     — справочник видов спорта и «типов линий» (tipLines:
                      каким рынкам соответствует id, подписи исходов);
- шаг 3  (прематч)  — полный снапшот прематч-линии (страны, чемпионаты,
                      события, линии) + дельты (обновления/удаления);
- шаг 4  (лайв)     — снапшот и непрерывные обновления лайв-линии.

Снапшот прематча содержит только ТОП-линии каждого события (~10 штук:
исход, пара тоталов, пара фор). ПОЛНУЮ роспись события (все линии тоталов,
фор, таймы/периоды — на порядок больше рынков) сайт запрашивает командой
«event.plus» (текст «event.plus», затем base64 от [id события:4][0:1]);
ответ приходит шагом 1118 (GET_EVENT_FILL_NEW). Мы делаем то же самое для
ВСЕХ прематч-событий по кругу с ограничением частоты (WINLINE_PLUS_RATE
запросов/с, повтор каждые WINLINE_PLUS_REFRESH с) — так Winline отдаёт
в несколько раз больше котировок на матч.

Протокол снят с бандла main.*.js сайта (DataListener.addMessage и парсеры
шагов). Формат кадра: [step:2 байта LE][payload]; кадр может быть сжат
gzip (магия 1f 8b — распаковываем raw deflate с 10-го байта); step 20000 —
контейнер вложенных кадров ([len:4 байта LE][кадр])*.

Числа — little-endian; строки — [len:2][utf-8], байт 0x1b обрезает строку.
Коэффициенты — unsigned short / 100; к прематчевым применяется маржа
(second short / 10000) по формуле сайта. Время — «московский epoch»
(секунды от 1970 в поясе МСК): вычитаем 3 часа.

Соединение ОДНО на процесс (как у сайта): фоновый поток держит его,
шлёт keepalive («getdate» раз в 5 c), применяет дельты и переподключается
с бэкоффом. Парсеры (прематч/лайв-сканеры) просто читают текущее
состояние из памяти. При смене протокола правьте номера шагов/поля здесь.
"""
import base64
import logging
import re
import struct
import threading
import time
import zlib

from ..config import (WINLINE_FEED_URL, WINLINE_PLUS_ENABLED,
                      WINLINE_PLUS_RATE, WINLINE_PLUS_REFRESH,
                      WINLINE_SNAPSHOT_WAIT, WINLINE_STALE_AFTER)

log = logging.getLogger("parsers.wl_feed")

ORIGIN = "https://winline.ru"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# Команды инициализации сайта: локаль, подписка на данные, справочник, дата
INIT_COMMANDS = ("lang", "AA==", "data", "WINLINE", "getdate")

STEP_PACK = 20000
STEP_MENU = 16       # виды спорта + tipLines (справочник рынков)
STEP_PREMATCH = 3    # снапшот/дельты прематч-линии
STEP_LIVE = 4        # снапшот/дельты лайв-линии
STEP_FILL = 18       # полная роспись события (ответ на «event.plus»)
STEP_FILL_NEW = 1118  # то же + id команд (новый клиент шлёт этот шаг)

# Типы линий (idTipEventSrc), которые парсер умеет превращать в
# двухисходные рынки, — только их и храним из полной росписи (остальное —
# 1X2 и прочая экзотика, хранить её значит зря жечь память на слабом VPS).
DECODABLE_SRC = frozenset({1, 3, 4, 6, 7, 15, 61, 71, 151})

# Разбираемая «экзотика» src=16: у неё нет отдельного src, рынок опознаётся
# СЕМАНТИЧЕСКИ — по ключевому слову вида рынка в тексте типа линии
# («тотал»/«фора»/«чет/нечет») И подписям исходов из справочника (R):
# «Больше»/«Меньше» — тотал, «1»/«2» — фора, «Нечет...»/«Чет...» — чет/нечет.
# Конкретный ПРЕДМЕТ и ПЕРИОД рынка (угловые, карточки, 2-й тайм, N-й сет...)
# при этом не важен — его разбирает market_scope() из ОСТАВШЕГОСЯ текста
# (см. winline.py:_scope) для ЛЮБОЙ формулировки, а не только заранее
# перечисленных: у Winline сотни типов линий src=16 («Тотал угловых»,
# «Фора [a] (ауты)», «Чет/Нечет карточек» и т.п.), и специфичных шаблонов
# слишком много, чтобы держать их полным белым списком текстов (раньше
# так и было — ловилось около 2% типов, остальные тихо терялись).
# Рынки с ДРУГОЙ семантикой исходов («Точный счёт», «Мультисчёт», «Двойной
# шанс» — три и более исхода) сюда не проходят: их подписи не совпадут ни
# с одним из трёх шаблонов R ниже, а «Двойной шанс» и «Точный счёт» к тому
# же почти всегда имеют больше 2 значений в v (отсекается отдельно, countV).
_EXOTIC_KIND_RE = re.compile(r"(?i)тотал|фора|чет/нечет|чет\b")


def is_decodable_exotic(tl: dict) -> bool:
    """Экзотика src=16 с однозначной семантикой (тотал/фора/чет-нечет) —
    winline.py._exotic() умеет её разобрать независимо от конкретной
    формулировки предмета/периода."""
    if tl.get("src") != 16:
        return False
    if tl.get("countV") not in (None, 2):
        return False  # не двухисходный тип линии — не наш случай
    text = tl.get("text") or ""
    if not _EXOTIC_KIND_RE.search(text):
        return False
    r = tl.get("R") or ["", ""]
    r0, r1 = (r[0] or "").strip(), (r[1] or "").strip()
    if r0 == "Больше" and r1 == "Меньше":
        return True
    if r0 == "1" and r1 == "2":
        return True
    if r0.startswith("Нечет") and r1.startswith("Чет"):
        return True
    return False


# Вложенные шаги прематч-кадра (PREMATCH_STEPS из бандла)
P_COUNTRY, P_CHAMP, P_EVENT, P_LINE = 1, 2, 3, 4
P_EVENT_TIME, P_EVENT_DEL, P_EVENT_ADD, P_EVENT_UPD = 31, 32, 33, 34
P_LINE_DEL, P_LINE_UPD = 42, 43
# Вложенные шаги лайв-кадра (STEPS.LIVE)
L_CHAMP, L_EVENT, L_EVENT_UPD, L_LINE, L_COUNT = 2, 3, 4, 5, 6

# Время фида — «epoch в поясе МСК»: для UTC вычитаем 3 часа.
MSK_OFFSET = 3 * 3600

KEEPALIVE_EVERY = 5.0     # период «getdate» (как у сайта)
SILENCE_RECONNECT = 40.0  # нет кадров дольше — переподключаемся
RECONNECT_MIN, RECONNECT_MAX = 5.0, 60.0


class _Reader:
    """Курсорное чтение little-endian значений из кадра."""

    __slots__ = ("b", "i")

    def __init__(self, buf: bytes, seek: int = 0) -> None:
        self.b = buf
        self.i = seek

    def u8(self) -> int:
        v = self.b[self.i]
        self.i += 1
        return v

    def u16(self) -> int:
        v = self.b[self.i] | (self.b[self.i + 1] << 8)
        self.i += 2
        return v

    def u32(self) -> int:
        v = struct.unpack_from("<I", self.b, self.i)[0]
        self.i += 4
        return v

    def i32(self) -> int:
        v = struct.unpack_from("<i", self.b, self.i)[0]
        self.i += 4
        return v

    def utf(self) -> str:
        n = self.u16()
        raw = self.b[self.i:self.i + n]
        self.i += n
        cut = raw.find(b"\x1b")   # 0x1b — «конец строки» в протоколе
        if cut >= 0:
            raw = raw[:cut]
        return raw.decode("utf-8", "replace")

    def skip_block16(self) -> None:
        """Пропускает блок [len:2][данные] (виджеты/ТВ события)."""
        n = self.u16()
        self.i += n


def _coef(v: float, margin: float) -> float:
    """Повторяет setCoeff сайта: применяет маржу к прематч-кэфу."""
    if margin != 1 and v > 1:
        v = int((1 + (v - 1) * margin) * 100 + 0.5) / 100
    if v >= 10:
        v = float(int(v))
    if 0 < v < 1.01:
        v = 1.0
    return v


class WinlineFeed:
    """Одно подключение к фиду + состояние линии в памяти."""

    def __init__(self, url: str = WINLINE_FEED_URL) -> None:
        self.url = url
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._started = False
        # Справочники
        self.sports: dict[int, dict] = {}     # id -> {name, strings[9]}
        self.tiplines: dict[int, dict] = {}   # id -> {src, countV, text, R}
        self.champs: dict[int, tuple] = {}    # id -> (sport_id, name)
        # Прематч
        self.pre_events: dict[int, dict] = {}
        self.pre_lines: dict[int, dict] = {}
        # Полная роспись прематч-событий («event.plus», шаг 1118):
        # event_id -> {line_id: line}. Обновляется по кругу с ограничением
        # частоты; при удалении события/переподключении — очищается.
        self.plus_lines: dict[int, dict[int, dict]] = {}
        # Кэш «разбираемая ли экзотика» по id типа линии (регэкспы по
        # тексту дорогие, а типов всего сотни)
        self._exotic_ok: dict[int, bool] = {}
        # event_id -> monotonic-время последнего ЗАПРОСА полной росписи
        self._plus_asked: dict[int, float] = {}
        self._plus_budget = 0.0        # накопленный лимит запросов (rate)
        self._plus_tick = 0.0          # время последнего пополнения лимита
        # Лайв
        self.live_events: dict[int, dict] = {}
        self.live_lines: dict[int, dict] = {}
        # Статус
        self._prematch_ready = threading.Event()
        self._last_frame = 0.0

    # ---------- публичное API ----------

    def start(self) -> None:
        """Запускает фоновый поток фида (повторные вызовы — no-op)."""
        with self._lock:
            if self._started:
                return
            self._started = True
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="winline-feed")
            self._thread.start()

    def wait_prematch(self, timeout: float = WINLINE_SNAPSHOT_WAIT) -> bool:
        """Ждёт первый снапшот прематча после (пере)подключения."""
        return self._prematch_ready.wait(timeout)

    def healthy(self) -> bool:
        """Кадры приходили недавно — состоянию можно верить."""
        return (self._prematch_ready.is_set()
                and time.time() - self._last_frame < WINLINE_STALE_AFTER)

    def prematch_snapshot(self) -> tuple[dict, dict, dict, list, list]:
        """(sports, tiplines, champs, events, lines) — копии под локом.

        Линии — объединение полной росписи («event.plus») и топ-линий
        снапшота. Топ-линии кладутся ПОВЕРХ: они обновляются дельтами
        непрерывно, а роспись — по кругу раз в WINLINE_PLUS_REFRESH с.
        """
        with self._lock:
            merged: dict[int, dict] = {}
            for lines in self.plus_lines.values():
                merged.update(lines)
            merged.update(self.pre_lines)
            return (dict(self.sports), dict(self.tiplines),
                    dict(self.champs),
                    [dict(e) for e in self.pre_events.values()],
                    [dict(ln) for ln in merged.values()])

    def live_snapshot(self) -> tuple[dict, dict, dict, list, list]:
        with self._lock:
            return (dict(self.sports), dict(self.tiplines),
                    dict(self.champs),
                    [dict(e) for e in self.live_events.values()],
                    [dict(ln) for ln in self.live_lines.values()])

    # ---------- цикл подключения ----------

    def _run(self) -> None:
        backoff = RECONNECT_MIN
        while True:
            try:
                self._session()
                backoff = RECONNECT_MIN     # сессия жила — сбросить бэкофф
            except Exception as exc:  # noqa: BLE001
                log.warning("Winline feed: %s — переподключение через %.0f c",
                            exc, backoff)
            self._prematch_ready.clear()
            with self._lock:
                self.pre_events.clear()
                self.pre_lines.clear()
                self.plus_lines.clear()
                self._plus_asked.clear()
                self.live_events.clear()
                self.live_lines.clear()
            time.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)

    def _session(self) -> None:
        import websocket
        ws = websocket.create_connection(
            self.url, timeout=15, origin=ORIGIN,
            header={"User-Agent": USER_AGENT})
        log.info("Winline feed: подключено к %s", self.url)
        try:
            for cmd in INIT_COMMANDS:
                ws.send(cmd)
            self._plus_budget = 0.0
            self._plus_tick = time.monotonic()
            last_send = time.monotonic()
            last_recv = time.monotonic()
            while True:
                if time.monotonic() - last_send > KEEPALIVE_EVERY:
                    ws.send("getdate")
                    last_send = time.monotonic()
                self._pump_plus(ws)
                try:
                    op, frame = ws.recv_data()
                except websocket.WebSocketTimeoutException:
                    if time.monotonic() - last_recv > SILENCE_RECONNECT:
                        raise RuntimeError("нет кадров от сервера") from None
                    continue
                last_recv = time.monotonic()
                if isinstance(frame, str):
                    frame = frame.encode("utf-8", "replace")
                if frame:
                    self._on_frame(bytes(frame))
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------- разбор кадров ----------

    def _on_frame(self, data: bytes) -> None:
        if len(data) < 2:
            return
        if data[0] == 0x1F and data[1] == 0x8B:       # gzip
            data = zlib.decompress(data[10:], -15)
        step = data[0] | (data[1] << 8)
        if step == STEP_PACK:
            i = 2
            while i + 4 <= len(data):
                ln = struct.unpack_from("<I", data, i)[0]
                self._on_frame(data[i + 4:i + 4 + ln])
                i += ln + 4
            return
        self._last_frame = time.time()
        payload = data[2:]
        try:
            if step == STEP_MENU:
                self._parse_menu(payload)
            elif step == STEP_PREMATCH:
                self._parse_prematch(payload)
                self._prematch_ready.set()
            elif step == STEP_LIVE:
                self._parse_live(payload)
            elif step in (STEP_FILL, STEP_FILL_NEW):
                self._parse_event_fill(payload, step)
        except Exception as exc:  # noqa: BLE001 — не роняем поток фида
            log.warning("Winline feed: ошибка разбора шага %d (%d байт): %s",
                        step, len(payload), exc)

    def _parse_menu(self, p: bytes) -> None:
        r = _Reader(p)
        sports: dict[int, dict] = {}
        for _ in range(r.u32()):
            sid = r.i32()
            r.i32()                      # sort
            name = r.utf()
            # 9 строк периодов: NP, RT, FT, HT, P, OT, PEN, Z, W
            strings = [r.utf() for _ in range(9)]
            sports[sid] = {"name": name, "strings": strings}
        tiplines: dict[int, dict] = {}
        for _ in range(r.u32()):
            tl = {"id": r.u32(), "sports": r.utf(), "favorite": r.u32(),
                  "countV": r.u32(), "src": r.u32(), "text": r.utf()}
            tl["R"] = [r.utf() for _ in range(30)]
            tiplines[tl["id"]] = tl
        with self._lock:
            self.sports = sports
            self.tiplines = tiplines
        log.info("Winline feed: справочник — %d видов спорта, %d типов рынков",
                 len(sports), len(tiplines))

    # -- прематч --

    def _read_line_body(self, r: _Reader, src: int) -> tuple[int, str, list]:
        """Общая часть линии: favorite/параметр + массив кэфов V."""
        fav, koef = 0, ""
        if src in (3, 6):                 # фора: сторона + линия
            fav = r.u8()
            koef = str(r.u16() / 100)
        elif src in (4, 7):               # тотал: линия
            koef = str(r.u16() / 100)
        elif src in (51, 151):            # рынок 1-го периода без параметра
            koef = "1"
        elif src == 61:                   # фора периода
            fav = r.u8()
            koef = "1/" + str(r.u16() / 100)
        elif src == 71:                   # тотал периода
            koef = "1/" + str(r.u16() / 100)
        v = [r.u16() / 100, r.u16() / 100]
        if src in (2, 5, 51):
            v.append(r.u16() / 100)
        elif src == 9:
            v.append(r.u16() / 100)
            v.append(r.u16() / 100)
        return fav, koef, v

    def _read_pre_line(self, r: _Reader, event_id: int | None,
                       explicit_event: bool) -> dict:
        lid = r.u32()
        if explicit_event:
            event_id = r.u32()
        tid = r.u16()
        r.u16()                            # маржа для касс — не наша
        margin = r.u16() / 1e4             # маржа сайта (CUPIS)
        tl = self.tiplines.get(tid)
        if tl is None:
            # Без справочника не знаем длину записи — кадр дальше не читается
            raise ValueError(f"неизвестный tipline {tid}")
        fav, koef, v = self._read_line_body(r, tl["src"])
        v = [_coef(x, margin) for x in v]
        return {"id": lid, "event": event_id, "tid": tid,
                "fav": fav, "koef": koef, "v": v}

    def _parse_prematch(self, p: bytes) -> None:
        # Заголовок кадра — 16 байт, последний байт — контрольный
        r = _Reader(p, 16)
        end = len(p) - 1
        cur_champ = -1
        cur_event: int | None = None
        new_champs: dict[int, tuple] = {}
        upd_events: list[dict] = []
        del_events: list[int] = []
        upd_lines: list[dict] = []
        del_lines: list[int] = []
        time_upd: list[tuple] = []
        while r.i < end:
            st = r.u8()
            if st == P_COUNTRY:
                r.u32(); r.u32(); r.utf(); r.u32()
                r.u8(); r.u16(); r.u16(); r.u8()
            elif st == P_CHAMP:
                cid = r.u32()
                sport_id = r.u32()
                r.u32()                    # страна
                name = r.utf()
                r.i32(); r.u32(); r.u32(); r.u8(); r.u32(); r.u32()
                new_champs[cid] = (sport_id, name)
                cur_champ = cid
            elif st in (P_EVENT, P_EVENT_UPD):
                ev = {"id": r.u32()}
                r.u32(); r.u32()           # idRadar, idNative
                r.u8(); r.u8(); r.u8(); r.u8(); r.u8(); r.u8()
                r.skip_block16()           # блок виджетов/ТВ
                ev["champ"] = r.u32() if st == P_EVENT_UPD else cur_champ
                # секунды в «поясе МСК»; сайт добавляет ~30 c и зануляет
                # секунды — округляем к ближайшей минуте
                raw = r.u32()
                ev["ts"] = (raw + 30 - MSK_OFFSET) // 60 * 60
                r.u8(); r.u8()             # count_add_line, isOD
                ev["team1"] = r.utf()
                ev["team2"] = r.utf()
                upd_events.append(ev)
                cur_event = ev["id"]
            elif st == P_LINE:
                upd_lines.append(self._read_pre_line(r, cur_event, False))
            elif st == P_LINE_UPD:
                upd_lines.append(self._read_pre_line(r, None, True))
            elif st == P_EVENT_TIME:
                eid = r.u32()
                raw = r.u32()
                time_upd.append((eid, (raw + 30 - MSK_OFFSET) // 60 * 60))
            elif st == P_EVENT_DEL:
                del_events.append(r.u32())
            elif st == P_EVENT_ADD:
                r.u32(); r.u8(); r.u8()
            elif st == P_LINE_DEL:
                del_lines.append(r.u32())
            else:
                raise ValueError(f"неизвестный шаг прематча {st} @{r.i}")

        with self._lock:
            self.champs.update(new_champs)
            for ev in upd_events:
                self.pre_events[ev["id"]] = ev
            for eid, ts in time_upd:
                if eid in self.pre_events:
                    self.pre_events[eid]["ts"] = ts
            for eid in del_events:
                self.pre_events.pop(eid, None)
            for ln in upd_lines:
                if ln["event"] is not None:
                    self.pre_lines[ln["id"]] = ln
            for lid in del_lines:
                self.pre_lines.pop(lid, None)
                # удаление линии касается и полной росписи события
                for lines in self.plus_lines.values():
                    if lid in lines:
                        lines.pop(lid, None)
                        break
            if del_events:
                gone = set(del_events)
                self.pre_lines = {k: v for k, v in self.pre_lines.items()
                                  if v["event"] not in gone}
                for eid in gone:
                    self.plus_lines.pop(eid, None)
                    self._plus_asked.pop(eid, None)

    def _is_exotic(self, tl: dict) -> bool:
        """Разбираемая экзотика src=16 (тотал/фора/чет-нечет любого предмета
        и периода — см. is_decodable_exotic)? Ответ кэшируется по id типа
        линии."""
        if tl["src"] != 16:
            return False
        cached = self._exotic_ok.get(tl["id"])
        if cached is None:
            cached = is_decodable_exotic(tl)
            self._exotic_ok[tl["id"]] = cached
        return cached

    # -- полная роспись события («event.plus» -> шаг 18/1118) --

    def _pump_plus(self, ws) -> None:
        """Запрашивает полную роспись прематч-событий по кругу.

        Частота ограничена WINLINE_PLUS_RATE запросов/с (сайт шлёт такие
        запросы при каждом открытии события — умеренный поток нормален);
        каждое событие повторно опрашивается раз в WINLINE_PLUS_REFRESH с.
        """
        if not WINLINE_PLUS_ENABLED or not self._prematch_ready.is_set():
            return
        now = time.monotonic()
        self._plus_budget = min(
            WINLINE_PLUS_RATE,
            self._plus_budget + (now - self._plus_tick) * WINLINE_PLUS_RATE)
        self._plus_tick = now
        if self._plus_budget < 1 or not self.tiplines:
            return
        with self._lock:
            due = [eid for eid in self.pre_events
                   if now - self._plus_asked.get(eid, 0.0)
                   > WINLINE_PLUS_REFRESH]
        if not due:
            return
        # сперва события, которых ещё не спрашивали, затем самые давние
        due.sort(key=lambda eid: self._plus_asked.get(eid, 0.0))
        for eid in due[:int(self._plus_budget)]:
            ws.send("event.plus")
            ws.send(base64.b64encode(
                struct.pack("<i", eid) + b"\x00").decode())
            self._plus_asked[eid] = now
            self._plus_budget -= 1

    def _parse_event_fill(self, p: bytes, step: int) -> None:
        """Разбирает полную роспись события (формат класса Me из бандла).

        Кадр: [id события:4] (+ [id команды 1:4][id команды 2:4] для шага
        1118), затем линии до конца кадра: [id:4][tip:2][маржа касс:2]
        [маржа сайта:2][favorite:1][koef:строка][кол-во кэфов:4]
        [кэфы: int32/1e4]. Кэфы уже без маржи — применяем её как сайт.
        """
        r = _Reader(p)
        eid = r.i32()
        if step == STEP_FILL_NEW:
            r.i32()
            r.i32()                        # id команд — не нужны
        lines: dict[int, dict] = {}
        end = len(p)
        while r.i < end:
            lid = r.u32()
            tid = r.u16()
            r.u16()                        # маржа для касс — не наша
            margin = r.u16() / 1e4         # маржа сайта (CUPIS)
            fav = r.u8()
            koef = r.utf()
            count = r.u32()
            if count > 31:
                raise ValueError(f"подозрительный countV {count} @{r.i}")
            v = [_coef(struct.unpack_from("<i", p, r.i + 4 * k)[0] / 1e4,
                       margin) for k in range(count)]
            r.i += 4 * count
            tl = self.tiplines.get(tid)
            if tl is not None and (tl["src"] in DECODABLE_SRC
                                   or self._is_exotic(tl)):
                lines[lid] = {"id": lid, "event": eid, "tid": tid,
                              "fav": fav, "koef": koef, "v": v}
        with self._lock:
            if eid in self.pre_events:
                self.plus_lines[eid] = lines

    # -- лайв --

    def _parse_live(self, p: bytes) -> None:
        r = _Reader(p, 12)                 # 12-байтовый заголовок
        new_champs: dict[int, tuple] = {}
        upd_events: list[dict] = []
        del_events: list[int] = []
        upd_lines: list[dict] = []
        del_lines: list[int] = []
        partial: list[dict] = []           # обновления без полного состава
        while r.i < len(p):
            st = r.u8()
            if st == L_CHAMP:
                cid = r.u32()
                sport_id = r.u32()
                r.i32(); r.u32(); r.u8()
                name = r.utf()
                r.u8(); r.u32(); r.u32()
                new_champs[cid] = (sport_id, name)
            elif st == L_EVENT:
                eid = r.i32()
                r.u32(); r.u32(); r.u8(); r.u8()
                r.skip_block16()
                r.u8(); r.u8(); r.u8()     # isOD, ?, duration
                state = r.i32()
                if state > 3:              # > DELETE — событие убрано
                    del_events.append(eid)
                    continue
                ev = {"id": eid, "state": state}
                ev["champ"] = r.u32()
                ev["team1"] = r.utf()
                ev["team2"] = r.utf()
                ev["ts"] = r.u32() - MSK_OFFSET
                ev["time"] = r.utf()
                r.u8(); r.u8(); r.u8(); r.u8()  # карточки
                r.u8()                     # feed
                ev["score"] = r.utf()
                r.utf()                    # счёт по сетам
                r.utf()                    # доп. информация
                r.u8()                     # число линий
                upd_events.append(ev)
            elif st == L_EVENT_UPD:
                eid = r.i32()
                r.skip_block16()
                r.u8(); r.u8(); r.u8()
                state = r.i32()
                if state > 3:
                    del_events.append(eid)
                    continue
                upd = {"id": eid, "state": state}
                upd["time"] = r.utf()
                r.u8(); r.u8(); r.u8(); r.u8()
                r.u8()
                upd["score"] = r.utf()
                r.utf(); r.utf()
                r.u8()
                partial.append(upd)
            elif st == L_LINE:
                lid = r.u32()
                state = r.u8()
                if state == 5:             # удаление линии
                    del_lines.append(lid)
                    continue
                eid = r.u32()
                count_v = r.u8()
                v = [r.u16() / 100 for _ in range(min(count_v, 31))]
                tid = r.u16()
                koef = r.utf()
                fav = r.u8()
                upd_lines.append({"id": lid, "event": eid, "tid": tid,
                                  "fav": fav, "koef": koef, "v": v,
                                  "state": state})
            elif st == L_COUNT:
                r.u32(); r.u8()
            else:
                raise ValueError(f"неизвестный шаг лайва {st} @{r.i}")

        with self._lock:
            self.champs.update(new_champs)
            for ev in upd_events:
                self.live_events[ev["id"]] = ev
            for upd in partial:
                cur = self.live_events.get(upd["id"])
                if cur:
                    cur.update(upd)
            for eid in del_events:
                self.live_events.pop(eid, None)
            for ln in upd_lines:
                self.live_lines[ln["id"]] = ln
            for lid in del_lines:
                self.live_lines.pop(lid, None)
            if del_events:
                gone = set(del_events)
                self.live_lines = {k: v for k, v in self.live_lines.items()
                                   if v["event"] not in gone}


_feed: WinlineFeed | None = None
_feed_lock = threading.Lock()


def get_feed() -> WinlineFeed:
    """Общий фид Winline на процесс (прематч- и лайв-сканеры делят его)."""
    global _feed
    with _feed_lock:
        if _feed is None:
            _feed = WinlineFeed()
        return _feed
