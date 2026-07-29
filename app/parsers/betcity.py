"""Betcity — прематч-линия через публичный JSON API линии.

Betcity — Angular SPA (`betcity.ru`), защищённая антифрод-скриптом «gib»
(Group-IB): страница `/ru/line` открывается в headless/headed Chrome (даже
со стелс-флагами и Xvfb) и рендерит DOM, но JS-приложение так и не
раскрывает реальные котировки — сайт держит их за отдельными XHR/websocket
запросами, инициализация которых в автоматизированном браузере не
проходит. ОДНАКО сам REST API линии, который дергает этот JS, оказался
ДОСТУПЕН напрямую, без браузера и БЕЗ авторизации — достаточно повторить
запрос обычным HTTP-клиентом (никакого анти-бота на самом API нет, в
отличие от рендера страницы). Эндпоинт один, отличаются только параметры
(маршруты сняты с Angular-роутера из `main.<hash>.js`: `betsLine` →
`/off/events`):

1. Общий снимок линии — все виды спорта и события одним запросом (~7 МБ):

       POST {BETCITY_API_HOST}/d/off/events?rev=6&template=1
       Body: ids=0

   Отдаёт ~5000 событий, но у каждого лишь «топ»-рынки (`main`, ~3 на
   событие: исход, ОДНА линия форы и ОДНА линия тотала).

2. Полная роспись рынков (`ext`, ~160 рынков на событие: вся лестница
   тоталов и фор, азиатские линии, тоталы/форы таймов, индивидуальные
   тоталы, «обе забьют», чет/нечет) — ТОТ ЖЕ эндпоинт с `ext=1` и списком
   id событий в теле (не более ~200 id за раз, дальше сервер отвечает 500):

       POST {BETCITY_API_HOST}/d/off/events?rev=6&ext=1
       Body: id_ev=<id>,<id>,...

   Сколько именно рынков спрятано за `ext`, событие сообщает заранее в
   поле `cnt_ext_add`; у событий с пустым `cnt_ext_add` росписи нет вовсе
   (на живой линии — примерно 700 из 2900). Роспись весит ~9.5 КБ на
   событие в сжатом виде, ~20 МБ за полный обход линии, поэтому она
   обновляется ПО КРУГУ (см. BETCITY_EXT_REFRESH): каждый цикл сканера
   догоняется часть событий, остальные берутся из кэша парсера. Основные
   рынки при этом всегда свежие — они приходят в общем снимке.

   (Отдельный per-событийный эндпоинт `/d/off/ext` из того же роутера
   закрыт файрволом на уровне пути — соединение рвётся даже из настоящего
   браузера с валидной cookie-сессией. `ext=1` у `/off/events` даёт те же
   данные и фильтру не поддался.)

Формат рынка — по «блокам» (не по числовому id рынка: один и тот же id
переиспользуется для разных по смыслу рынков в разных событиях). В общем
снимке блоки называются `Wm`/`F1m`/`T1m`, в полной росписи — `F1`/`T` и
т.д., набор полей внутри одинаковый:
- `Wm` с исходами {P1,P2,X} → «Исход 1X2» (winner1x2); {P1,P2} без
  ничьей → «Победитель» (winner);
- `F1m`/`F1` {F1,F2,Kf_F1,Kf_F2} → фора: F1/F2 — знаковая линия (F1=-F2),
  Kf_F1/Kf_F2 — коэффициенты;
- `T1m`/`T` {Tot,Tm,Tb} → тотал: Tot — линия, Tb/Tm — больше/меньше;
- `IT_T1`/`IT_T2` {Tot,Tm,Tb} → индивидуальный тотал 1-й/2-й команды;
- `YN` {Y,N} → рынок «да/нет». Разбираем ТОЛЬКО «Обе забьют» и
  «Чет/Нечет тотала», причём по ПОЛНОМУ совпадению имени: у Betcity
  десятки комбинированных Да/Нет-рынков, содержащих те же слова («П1 и
  обе забьют», «Обе забьют и ТМ»), а служебные «П1»/«ТМ» в scope не
  попадают — без строгой проверки имени такой рынок склеился бы с обычным
  «Обе забьют» другой БК в ложную вилку;
- блоки `WXm` (двойной исход), `SCR*` (точный счёт), `X3*` (три исхода),
  `YNT1`/`YNT2` (да/нет КОНКРЕТНОЙ команды), `TMS*`, `*Num` — пропускаем:
  либо не двухисходный рынок, либо не привязаны к конкретному матчу
  (аутрайты турнира), либо их семантику не отличить от общерыночной.

Предмет/период рынка приводится к канону общей `market_scope()` — с
предварительным разворачиванием собственных сокращений Betcity («УГЛ.» →
«угловые.», «ЖК.» → «желтые карточки.»), иначе они не распознаются как
известный предмет и рынок не сошьётся с Winline/Fonbet/BetBoom.
"""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import (BETCITY_API_HOST, BETCITY_EXT_BATCH,
                      BETCITY_EXT_MAX_AGE, BETCITY_EXT_MAX_REQUESTS,
                      BETCITY_EXT_REFRESH, BETCITY_EXT_WORKERS,
                      BETCITY_FEED_TIMEOUT, BETCITY_FULL_MARKETS,
                      BETCITY_MIN_REFRESH, BETCITY_SITE_HOST)
from ..models import KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import fmt_hcap, fmt_total, format_start, market_scope, sane_1x2_margin

log = logging.getLogger("parsers.betcity")

_EVENTS_URL = f"{BETCITY_API_HOST}/d/off/events"

# Паузы перед повторами POST: сколько пауз — столько и повторов. Betcity
# рвёт заметную долю соединений (на живой линии — до трети попыток),
# поэтому повторов несколько: снимок линии — единственный источник её
# событий, и терять его на весь цикл из-за одного RST нельзя.
_POST_RETRY_PAUSES = (1.0, 3.0, 8.0)

_WM_1X2 = frozenset({"P1", "P2", "X"})
_WM_2WAY = frozenset({"P1", "P2"})
_HCAP_KEYS = frozenset({"F1", "F2", "Kf_F1", "Kf_F2"})
_TOTAL_KEYS = frozenset({"Tot", "Tm", "Tb"})
_YN_KEYS = frozenset({"Y", "N"})

# Имя блока → вид рынка. `*m` — блоки общего снимка, без суффикса — те же
# рынки в полной росписи (`ext`).
_BLOCK_KIND = {
    "Wm": "winner", "W": "winner",
    "F1m": "hcap", "F1": "hcap",
    "T1m": "total", "T": "total",
    "IT_T1": "itotal1", "IT_T2": "itotal2",
    "YN": "yesno",
}

# Слаг вида спорта для страницы события (`/ru/line/<slug>/<cn_ch>/<id_ev>`) —
# снят с карты `SportNameService.list` в main.<hash>.js сайта (числовой
# id_sp сайт нигде в фиде текстом не раскрывает).
_SPORT_URL_SLUG = {
    1: "soccer", 2: "tennis", 3: "basketball", 4: "formula-1",
    5: "baseball", 6: "american-football", 7: "ice-hockey", 8: "handball",
    9: "golf", 10: "chess", 11: "motorcycling", 12: "volleyball",
    13: "rugby", 14: "athletics", 15: "biathlon",
    16: "cross-country-skiing", 17: "bandy", 18: "auto-racing",
    19: "futsal", 20: "boxing", 21: "cycling", 23: "snooker",
    24: "water-polo", 26: "curling", 36: "field-hockey", 39: "ufc",
    40: "fencing", 41: "rowing", 42: "gymnastics", 43: "beach-soccer",
    46: "table-tennis", 47: "darts", 48: "lacrosse", 49: "badminton",
    52: "sailing", 58: "beach-volleyball", 70: "beach-handball",
    71: "australian-football", 72: "paddle-tennis", 73: "cybersport",
    74: "cricket", 75: "bowls", 76: "wrestling", 85: "gaelicsport",
}

# Betcity сокращает предмет рынка ДО названия («УГЛ. Фора», «ЖК. Тотал») —
# market_scope() ищет полные слова, поэтому разворачиваем префикс перед
# канонизацией (сам текст в MarketOdds.market остаётся как на сайте).
_SCOPE_PREFIX_EXPAND = (
    ("угл.", "угловые."),
    ("жк.", "желтые карточки."),
    ("кк.", "красные карточки."),
)

# Статистику матча Betcity выносит ещё и в ЗАВИСИМОЕ событие (is_dep) того
# же времени в чемпионат «… Статистика», приписывая предмет к именам ОБЕИХ
# команд: «УГЛ Кайрат-Алматы» — «УГЛ Омония Никосия». Предмет уже есть в
# имени рынка («УГЛ. Тотал»), а искажённое имя команды мешает сшить событие
# с той же статистикой у других БК — префикс убираем.
_DEP_TEAM_PREFIX_RE = re.compile(r"^(?:УГЛ|ЖК|КК)\s+")

# «Обе забьют» — только сам рынок (возможно, с уточнением периода), но не
# комбинированные ставки, где эти слова лишь часть условия.
_BOTH_SCORE_RE = re.compile(
    r"^обе\s+забьют"
    r"(?:\s+(?:в|во)\s+\S+\s+(?:тайме|периоде|сете|партии|карте|иннинге))?$")
# «Чет/Нечет тотала» (в т.ч. с предметом-префиксом: «угловые. чет/нечет…»)
_ODD_EVEN_RE = re.compile(r"^(?:[^.]{1,40}\.\s*)?чет/нечет(?:\s+тотала)?$")


def _expand_scope_prefix(name: str) -> str:
    low = name.lower()
    for prefix, expansion in _SCOPE_PREFIX_EXPAND:
        if low.startswith(prefix):
            return expansion + name[len(prefix):]
    return name


def _price(v) -> float | None:
    """Коэффициент лежит во вложенном {"kf": ...}; голое число — это линия
    тотала/форы (Tot/F1/F2), а не цена — им сюда попадать не следует."""
    if isinstance(v, dict):
        kf = v.get("kf")
        if isinstance(kf, (int, float)) and kf > 1.0:
            return float(kf)
    return None


class BetcityParser(BaseParser):
    name = "Betcity"
    # Запрос всей линии у Betcity тяжёлый (~5-7 МБ), и её сервер не терпит
    # частых обходов: при периоде 30 c половина попыток заканчивалась
    # обрывом соединения (Connection reset by peer) даже с повторами, а
    # успешные запросы замедлялись с 15 до 60 c. Поэтому у неё свой период.
    min_refresh = BETCITY_MIN_REFRESH

    def __init__(self) -> None:
        super().__init__()
        # Полная роспись рынков по событию: id_ev → {id рынка: рынок} и
        # время её получения. Живёт МЕЖДУ циклами сканера: роспись всей
        # линии слишком тяжёлая, чтобы качать её каждые SCAN_INTERVAL.
        self._ext: dict[int, dict] = {}
        self._ext_at: dict[int, float] = {}

    def fetch_odds(self) -> list[MarketOdds]:
        now = time.time()
        deadline = time.monotonic() + BETCITY_FEED_TIMEOUT
        try:
            payload = self._fetch_snapshot()
        except Exception as exc:  # noqa: BLE001
            log.warning("Betcity: не удалось получить линию: %s", exc)
            return []

        events = self._collect_events(payload, now)
        log.info("Betcity: снимок линии — %d прематч-событий", len(events))
        if not events:
            return []

        if BETCITY_FULL_MARKETS:
            self._refresh_ext([ev for ev, *_ in events], deadline, now)

        by_key: dict[str, MarketOdds] = {}
        for ev, sport_name, league, slug in events:
            for o in self._parse_event(ev, sport_name, league, slug, now):
                by_key[o.match_key] = o
        return list(by_key.values())

    # ---- общий снимок линии ----

    def _post(self, params: dict, data: dict) -> dict:
        """POST к API линии с повторными попытками.

        Betcity рвёт часть соединений (RST/ошибка TLS-рукопожатия), иногда
        посередине многомегабайтного ответа. Перед повтором закрываем пул
        соединений: сервер мог закрыть keep-alive со своей стороны, и на
        новом соединении запрос проходит."""
        # первая попытка + по одной на каждую паузу
        for pause in (*_POST_RETRY_PAUSES, None):
            try:
                resp = self.session.post(
                    _EVENTS_URL, params=params, data=data,
                    headers=self._headers(), timeout=BETCITY_FEED_TIMEOUT)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                if pause is None:
                    raise
                log.debug("Betcity: повтор запроса через %.1f с (%s)",
                          pause, exc)
                self.session.close()
                time.sleep(pause)
        raise AssertionError("недостижимо")  # pragma: no cover

    def _fetch_snapshot(self) -> dict:
        return self._post({"rev": "6", "template": "1"}, {"ids": "0"})

    def _collect_events(self, payload: dict,
                        now: float) -> list[tuple[dict, str, str, str | None]]:
        """Прематч-события с двумя командами (+ их спорт/турнир/слаг)."""
        sports = (payload.get("reply") or {}).get("sports") or {}
        out: list[tuple[dict, str, str, str | None]] = []
        for sp in sports.values():
            sport_name = sp.get("name_sp") or "Спорт"
            slug = _SPORT_URL_SLUG.get(sp.get("id_sp"))
            for ch in (sp.get("chmps") or {}).values():
                league = self._league_name(ch.get("name_ch"), sport_name)
                for ev in (ch.get("evts") or {}).values():
                    if self._live_prematch(ev, now):
                        out.append((ev, sport_name, league, slug))
        return out

    @staticmethod
    def _live_prematch(ev: dict, now: float) -> bool:
        if not ev.get("name_ht") or not ev.get("name_at"):
            return False  # без второй команды — турнирный аутрайт
        if ev.get("del_ev") or ev.get("del") or ev.get("hd_bl"):
            return False
        if not ev.get("id_ev"):
            return False
        date_ev = ev.get("date_ev")
        return bool(date_ev) and date_ev > now  # только прематч

    @staticmethod
    def _league_name(name_ch: str | None, sport_name: str) -> str:
        name_ch = (name_ch or "").strip()
        prefix = f"{sport_name}. "
        if name_ch.startswith(prefix):
            return name_ch[len(prefix):]
        return name_ch

    # ---- полная роспись рынков (ext=1, пачками, по кругу) ----

    def _refresh_ext(self, events: list[dict], deadline: float,
                     now: float) -> None:
        """Догоняет полную роспись событий, у которых она устарела.

        Роспись ВСЕЙ линии — десятки мегабайт, качать её каждый цикл
        незачем: прематч-кэфы так быстро не двигаются. За цикл делаем не
        больше BETCITY_EXT_MAX_REQUESTS запросов, начиная с событий, у
        которых росписи ещё нет (а среди них — с ближайших по времени);
        остальные события берут роспись из кэша прошлых циклов."""
        live_ids = {ev["id_ev"] for ev in events}
        for eid in list(self._ext_at):
            if eid not in live_ids or now - self._ext_at[eid] > BETCITY_EXT_MAX_AGE:
                self._ext.pop(eid, None)
                self._ext_at.pop(eid, None)

        stale = [ev for ev in events
                 if now - self._ext_at.get(ev["id_ev"], 0.0) >= BETCITY_EXT_REFRESH]
        stale.sort(key=lambda ev: (self._ext_at.get(ev["id_ev"], 0.0),
                                   ev.get("date_ev") or 0))
        ids = [ev["id_ev"] for ev in stale]
        batches = [ids[i:i + BETCITY_EXT_BATCH]
                   for i in range(0, len(ids), BETCITY_EXT_BATCH)]
        batches = batches[:max(0, BETCITY_EXT_MAX_REQUESTS)]
        if not batches:
            log.info("Betcity: роспись рынков свежая у всех %d событий",
                     len(events))
            return

        asked = got = 0
        pool = ThreadPoolExecutor(max_workers=max(1, BETCITY_EXT_WORKERS))
        futures = {pool.submit(self._fetch_ext, b): b for b in batches}
        try:
            for fut in as_completed(futures):
                try:
                    ext_by_id = fut.result()
                except Exception:  # noqa: BLE001
                    continue  # пачка не дошла — попробуем в следующем цикле
                fetched = time.time()
                # Отметку времени ставим ВСЕЙ пачке, а не только событиям с
                # росписью: у части событий доп. рынков нет вовсе, и без
                # отметки они запрашивались бы каждый цикл, съедая бюджет.
                for eid in futures[fut]:
                    self._ext_at[eid] = fetched
                asked += len(futures[fut])
                for eid, ext in ext_by_id.items():
                    self._ext[eid] = ext
                    got += 1
                if time.monotonic() > deadline:
                    log.info("Betcity: дедлайн росписи истёк")
                    break
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        log.info("Betcity: роспись запрошена у %d событий (с рынками — %d, "
                 "устарела у %d, всего в кэше %d)",
                 asked, got, len(ids), len(self._ext))

    def _fetch_ext(self, ids: list[int]) -> dict[int, dict]:
        """Полная роспись пачки событий: id_ev → {id рынка: рынок}."""
        payload = self._post({"rev": "6", "ext": "1"},
                             {"id_ev": ",".join(str(i) for i in ids)})
        out: dict[int, dict] = {}
        sports = (payload.get("reply") or {}).get("sports") or {}
        for sp in sports.values():
            for ch in (sp.get("chmps") or {}).values():
                for ev in (ch.get("evts") or {}).values():
                    ext = ev.get("ext")
                    if ext and ev.get("id_ev"):
                        out[ev["id_ev"]] = ext
        return out

    # ---- разбор события ----

    def _parse_event(self, ev: dict, sport_name: str, league: str,
                     slug: str | None, now: float) -> list[MarketOdds]:
        if not self._live_prematch(ev, now):
            return []
        team1, team2 = self._teams(ev)
        if not team1 or not team2 or team1 == team2:
            return []
        date_ev = ev["date_ev"]

        sport = f"{sport_name} · {league}" if league else sport_name
        base = dict(
            bookmaker=self.name, sport=sport, team1=team1, team2=team2,
            kind=KIND_PREMATCH, start_time=format_start(date_ev),
            start_ts=date_ev, url=self._event_url(ev, slug))

        # Сначала основные рынки (они всегда свежие — из общего снимка),
        # потом роспись: при совпадении ключа берётся первый, т.е. более
        # свежая котировка основной линии.
        markets = list((ev.get("main") or {}).values())
        markets += list((self._ext.get(ev["id_ev"]) or {}).values())

        result: list[MarketOdds] = []
        seen: set[str] = set()
        for m in markets:
            for o in self._parse_market(m, base):
                if o.market_key in seen:
                    continue
                seen.add(o.market_key)
                result.append(o)
        return result

    @staticmethod
    def _teams(ev: dict) -> tuple[str, str]:
        team1 = (ev.get("name_ht") or "").strip()
        team2 = (ev.get("name_at") or "").strip()
        if _DEP_TEAM_PREFIX_RE.match(team1) and _DEP_TEAM_PREFIX_RE.match(team2):
            team1 = _DEP_TEAM_PREFIX_RE.sub("", team1).strip()
            team2 = _DEP_TEAM_PREFIX_RE.sub("", team2).strip()
        return team1, team2

    def _parse_market(self, m: dict, base: dict) -> list[MarketOdds]:
        name = m.get("name") or ""
        expanded = _expand_scope_prefix(name)
        scope = market_scope(expanded)
        low = expanded.lower().replace("ё", "е").strip()
        result: list[MarketOdds] = []
        for d in (m.get("data") or {}).values():
            for bname, block in (d.get("blocks") or {}).items():
                if not isinstance(block, dict):
                    continue
                kind = _BLOCK_KIND.get(bname)
                if kind is None:
                    continue  # см. докстринг модуля
                o = self._parse_block(kind, block, name, low, scope, base)
                if o is not None:
                    result.append(o)
        return result

    @staticmethod
    def _parse_block(kind: str, block: dict, name: str, low: str, scope: str,
                     base: dict) -> MarketOdds | None:
        keys = frozenset(block.keys())

        if kind == "winner" and keys == _WM_1X2:
            k1, kx, k2 = _price(block["P1"]), _price(block["X"]), _price(block["P2"])
            if not sane_1x2_margin(k1, kx, k2):
                return None
            key = f"winner1x2:{scope}" if scope else "winner1x2"
            return MarketOdds(
                market=name, market_key=key,
                outcome1="П1", outcome2="П2", outcome3="X",
                k1=k1, k2=k2, k3=kx, **base)

        if kind == "winner" and keys == _WM_2WAY:
            k1, k2 = _price(block["P1"]), _price(block["P2"])
            if k1 is None or k2 is None:
                return None
            key = f"winner:{scope}" if scope else "winner"
            return MarketOdds(
                market=name, market_key=key,
                outcome1="П1", outcome2="П2", k1=k1, k2=k2, **base)

        if kind == "hcap" and keys >= _HCAP_KEYS:
            f1_raw, f2_raw = block.get("F1"), block.get("F2")
            try:
                if float(f1_raw) != -float(f2_raw):
                    return None
            except (TypeError, ValueError):
                return None
            k1, k2 = _price(block["Kf_F1"]), _price(block["Kf_F2"])
            if k1 is None or k2 is None:
                return None
            h1, h2 = fmt_hcap(f1_raw), fmt_hcap(f2_raw)
            key = f"hcap:{scope}:{h1}" if scope else f"hcap:{h1}"
            return MarketOdds(
                market=name, market_key=key,
                outcome1=f"Ф1 {h1}", outcome2=f"Ф2 {h2}",
                k1=k1, k2=k2, **base)

        if kind in ("total", "itotal1", "itotal2") and keys >= _TOTAL_KEYS:
            line = block.get("Tot")
            if line is None:
                return None
            k1, k2 = _price(block["Tb"]), _price(block["Tm"])
            if k1 is None or k2 is None:
                return None
            pt = fmt_total(line)
            if kind == "total":
                key = f"total:{scope}:{pt}" if scope else f"total:{pt}"
                return MarketOdds(
                    market=name, market_key=key,
                    outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                    k1=k1, k2=k2, **base)
            # индивидуальный тотал: сторону несёт ключ рынка (движок
            # сопоставляет его по ИМЕНИ команды — порядок команд у разных
            # БК может отличаться)
            side = 1 if kind == "itotal1" else 2
            team = base["team1"] if side == 1 else base["team2"]
            return MarketOdds(
                market=f"Тотал {pt} ({team})",
                market_key=f"itotal:{side}:{scope}:{pt}",
                outcome1=f"ИТБ {pt}", outcome2=f"ИТМ {pt}",
                k1=k1, k2=k2, **base)

        if kind == "yesno" and keys == _YN_KEYS:
            yes, no = _price(block["Y"]), _price(block["N"])
            if yes is None or no is None:
                return None
            if _BOTH_SCORE_RE.match(low):
                key = f"bothscore:{scope}" if scope else "bothscore"
                return MarketOdds(
                    market=name, market_key=key,
                    outcome1="Да", outcome2="Нет", k1=yes, k2=no, **base)
            if _ODD_EVEN_RE.match(low):
                # Y=Чет, N=Нечет (сверено с шаблоном разметки рынка сайта:
                # ITEMS[0].PRE="Чет" → RIGHT="YN.Y"). Порядок исходов —
                # как у остальных парсеров: 1-й исход всегда «Чет».
                key = f"oddeven:{scope}" if scope else "oddeven"
                return MarketOdds(
                    market=name, market_key=key,
                    outcome1="Чет", outcome2="Нечет", k1=yes, k2=no, **base)
            return None  # прочие Да/Нет-рынки — см. докстринг модуля

        return None

    @staticmethod
    def _event_url(ev: dict, slug: str | None) -> str | None:
        cn_ch, id_ev = ev.get("cn_ch"), ev.get("id_ev")
        if slug and cn_ch and id_ev:
            return f"{BETCITY_SITE_HOST}/ru/line/{slug}/{cn_ch}/{id_ev}"
        return f"{BETCITY_SITE_HOST}/ru/line"
