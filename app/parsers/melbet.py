"""Melbet — публичный JSON-фид линии (движок 1xBet), прематч и лайв.

Melbet работает на том же движке, что 1xBet/Betwinner/Linebet, и раздаёт
линию тем же фидом, которым пользуется сам сайт, — без авторизации:

    GET {база}/LineFeed/GetSportsShortZip?virtualSports=true
        — список видов спорта: [{"I": 1, "N": "Футбол"}, ...]
    GET {база}/LineFeed/GetChampsZip?sport=1
        — чемпионаты вида спорта: {"LI": 118593, "L": "Лига Европы УЕФА",
          "GC": 10} (GC — сколько в нём событий)
    GET {база}/LineFeed/Get1x2_VZip?champs=118593&count=50&mode=4
        — события чемпионата с рынками
    GET {база}/LineFeed/GetChampZip?champ=118593&mode=4
        — чемпионат целиком, БЕЗ рынков: нужен только там, где событий
          больше 50 и Get1x2_VZip отдаёт не всех
    GET {база}/LineFeed/GetGameZip?id=<событие>&isSubGames=true&...
        — полная роспись одного события плюс список его подигр (тайм,
          «угловые», «жёлтые карточки»); рынки самой подигры лежат за
          таким же запросом по её собственному id

ФИД ПРИНИМАЕТ ТОЛЬКО ТЕ СТРОКИ ЗАПРОСА, КОТОРЫЕ ШЛЁТ САМ САЙТ. Всё
остальное — не «лишний параметр проигнорируется», а 406 NotAcceptable на
весь запрос. Проверено на живом фиде:

- «?sports=1&count=50&mode=4» — 200, а «?count=50&mode=4&sports=1» — 406:
  ЗНАЧИМ ДАЖЕ ПОРЯДОК параметров, фильтр идёт первым;
- lng ломает Get1x2_VZip и GetChampsZip при ЛЮБОМ значении, mode и
  getEmpty — GetSportsShortZip;
- count обязан быть кратен пяти: count=8 — это 406;
- зато без mode=4 Get1x2_VZip отвечает 200 и ПУСТЫМ списком.

Поэтому наборы параметров записаны здесь ровно так, как их шлёт сайт, и
пересортировывать их нельзя. Языка в запросах нет вовсе — фид и без него
отвечает по-русски.

Линия собирается ПО ЧЕМПИОНАТАМ: одним запросом фид отдаёт максимум 50
событий, сколько ни проси, поэтому «весь вид спорта» — это лишь верхушка
(50 матчей из ~2700 по футболу). Чемпионатов около 900, событий в них
~7500 — это и есть вся линия.

База — это домен сайта плюс префикс: сейчас это «/service-api», на
зеркалах постарше эндпоинты лежат прямо в корне. Домен и префикс парсер
перебирает сам (как у Fonbet) и запоминает первый рабочий; неудачный
перебор повторяется не чаще раза в PROBE_BACKOFF секунд.

ВАЖНО ПРО ДОСТУП, И ЭТО ГЛАВНАЯ ПРИЧИНА ПУСТОЙ ЛИНИИ. Зеркала пускают
разные адреса. melbet.ru отвечает страницей «Пожалуйста, отключите VPN»
(HTTP 403) на ЛЮБОЙ путь, если адрес клиента ему не нравится, — и IP
дата-центра ему как раз не нравится, хотя сканер именно на VPS и живёт.
Международные зеркала (melbet.com, melbet.org, mel-bet.com) на тот же
запрос с того же адреса отдают линию — проверено на живом фиде
2026-07-30. Поэтому перебирается несколько доменов (MELBET_HOSTS), а не
один; жёстко задать базу можно через MELBET_API_HOST. Что ответило
каждое зеркало ИМЕННО С ВАШЕГО сервера, показывает `venv/bin/python -m
app.diagnose_melbet`.

КАК РАЗБИРАЕТСЯ РЫНОК. В фиде нет ни одного слова о смысле котировки —
только числа: {"G": 17, "T": 9, "C": 1.85, "P": 2.5} (группа рынка, код
исхода, коэффициент, линия). Словаря кодов БК не публикует, а открытые
разборы движка друг другу противоречат. Поэтому коды здесь считаются
гипотезой, а каждая линия проверяет её по самим данным — монотонностью
лестницы тоталов, знаком форы фаворита и т.д. Не подтвердилось —
семейство рынков не отдаётся вовсе (см. app/parsers/melbet_layout.py).

Что попадает в котировки:
- «Исход 1X2» (П1/X/П2) с обычной проверкой маржи (sane_1x2_margin) и
  «Победитель» без ничьей — там, где ничьей в фиде нет;
- тоталы — вся лестница линий, пара «больше/меньше» на каждую линию;
- форы — вся лестница; линии сторон обязаны быть противоположными;
- индивидуальные тоталы обеих команд — вся лестница;
- «обе забьют» (Да/Нет) — рынок без линии, поэтому его сторона
  определяется через результативность матча (см. melbet_layout._decide_bts);
- те же рынки у подигр: у подигры есть предмет (TG: «Угловые», «Жёлтые
  карточки») и период (PN: «1-й тайм»), из них собирается scope, поэтому
  «тотал угловых 1-го тайма» Melbet сшивается с таким же рынком других
  БК, а не с тоталом голов всего матча.

Что НЕ берём: чет/нечет, двойной шанс, точный счёт и прочую экзотику.
Чет/нечет проверить нечем в принципе: у честной линии «чёт» и «нечет»
почти равновероятны при любом тотале, и никакой структурный признак не
скажет, который из двух кодов какой, — а перепутать их значит сложить
свой «чёт» с чужим «нечет» и показать вилку на пустом месте. Остальное
либо не двухисходное, либо не сопоставляется с другими БК. Появится
словарь имён рынков — добавим.
"""
import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from ..config import (MELBET_API_HOST, MELBET_CHAMP_WORKERS, MELBET_CHAMPS,
                      MELBET_FEED_TIMEOUT, MELBET_FULL_MARKETS,
                      MELBET_FULL_MARKETS_MAX, MELBET_FULL_MARKETS_WORKERS,
                      MELBET_GROUPS, MELBET_HOSTS, MELBET_LIVE_COUNT,
                      MELBET_MIN_REFRESH, MELBET_SITE_HOST,
                      MELBET_SPORT_COUNT, MELBET_SPORT_WORKERS,
                      MELBET_SUBGAMES)
from ..models import KIND_LIVE, KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import (fmt_hcap, fmt_total, format_start, market_scope,
                         neg_hcap, sane_1x2_margin, sane_pair_margin)
from .melbet_layout import (BTS, HCAP, ITOTAL1, ITOTAL2, T_DRAW, T_W1, T_W2,
                            TOTAL, WINNER, FAMILY_CODES, Layout, RawEvent,
                            detect)

log = logging.getLogger("parsers.melbet")

# Префиксы, под которыми движок раздаёт фид на разных зеркалах
API_PREFIXES = ["/service-api", ""]
# Если ни один домен не ответил — не перебираем весь список каждый цикл
PROBE_BACKOFF = 300
# «Зеркала ещё ни разу не перебирались». Именно минус бесконечность, а не
# ноль: time.monotonic() на Linux считает секунды с загрузки МАШИНЫ, и с
# нулём первые PROBE_BACKOFF секунд после каждой перезагрузки сервера
# выглядели для парсера как «только что перебирал» — база не искалась
# вовсе, а обход молча возвращал пустую линию.
NEVER_PROBED = float("-inf")
# По чему узнаём заглушку «Пожалуйста, отключите VPN» (см. _is_geo_stub).
# Кроме русского текста здесь разметка самой страницы: она не зависит от
# кодировки и переживёт смену формулировки в заголовке.
_GEO_STUB_MARKERS = (
    "отключите vpn", "disable vpn", "недоступен в", "вашей стране",
    "site-unavailable", "cloudflare_error_1000s_box",
)
# Пауза перед повторной попыткой, когда фид оборвал соединение
RETRY_PAUSE = 0.5
# Если по чемпионатам собралось меньше этой доли линии — фид отвечал не
# всем запросам, и снимок добирается запросами «по виду спорта»
CHAMPS_ENOUGH = 0.5
# Куда лезть за исходами внутри ответа. Ключи перечислены явно, обходить
# ответ целиком нельзя: в нём есть ещё и подигры (SG), и их рынки нельзя
# смешивать с рынками всего матча.
_MARKET_KEYS = ("E", "AE", "ME", "GE")
# Из чего складывается название подигры: предмет счёта (TG: «Угловые») и
# период (PN: «1-й тайм»). Поле N у подигры — числовой идентификатор, а не
# имя, поэтому его брать нельзя. Без названия подигра пропускается:
# неизвестно, к чему относятся её рынки.
_SUBGAME_NAME_KEYS = ("TG", "PN")
_MAX_DEPTH = 6
# Фид отдаёт события пачками не больше этого числа, сколько ни проси.
_MAX_PER_REQUEST = 50


class MelbetParser(BaseParser):
    name = "Melbet"
    min_refresh = MELBET_MIN_REFRESH or None

    def __init__(self) -> None:
        super().__init__()
        self._base: str | None = None
        self._last_probe = NEVER_PROBED
        self._layout_note = ""
        # Что ответило каждое зеркало на последнем переборе — для лога и
        # для app/diagnose_melbet.py.
        self.probe_notes: list[tuple[str, str]] = []
        # Роспись тянется пулом потоков, а пул соединений requests по
        # умолчанию держит десять: без этого лишние соединения к тому же
        # хосту открывались бы и закрывались на каждый запрос.
        adapter = requests.adapters.HTTPAdapter(
            pool_maxsize=max(MELBET_FULL_MARKETS_WORKERS, MELBET_CHAMP_WORKERS,
                             MELBET_SPORT_WORKERS) + 4)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # ------------------------------------------------------------------
    # Поиск рабочей базы фида
    # ------------------------------------------------------------------

    def _candidates(self) -> list[str]:
        bases: list[str] = []
        if MELBET_API_HOST:
            bases.append(MELBET_API_HOST)
        for host in MELBET_HOSTS:
            for prefix in API_PREFIXES:
                bases.append(host + prefix)
        return bases

    def _resolve_base(self, force: bool = False) -> str | None:
        if self._base:
            return self._base
        now = time.monotonic()
        if not force and now - self._last_probe < PROBE_BACKOFF:
            # Без этой строки обход, пропущенный из-за паузы, выглядел в
            # логе как «Melbet: 0 котировок» безо всякой причины.
            log.info("Melbet: рабочей базы фида нет, следующий перебор "
                     "зеркал через %.0f с — в этом обходе линии не будет",
                     PROBE_BACKOFF - (now - self._last_probe))
            return None
        self._last_probe = now
        notes: list[tuple[str, str]] = []
        for base in self._candidates():
            note = self._probe(base)
            if note is None:
                log.info("Melbet: рабочая база фида — %s", base)
                self._base = base
                self.probe_notes = notes + [(base, "линия отдана")]
                return base
            notes.append((base, note))
        self.probe_notes = notes
        # Причину пишем словами: без неё «фид не ответил» одинаково выглядит
        # и когда сменился домен, и когда сайт не пускает адрес.
        log.warning(
            "Melbet: ни одно зеркало не отдало линию (повтор через %d с). "
            "Ответы: %s. Фид отвечает не любому адресу; проверить — "
            "venv/bin/python -m app.diagnose_melbet, задать базу вручную — "
            "MELBET_API_HOST.", PROBE_BACKOFF,
            "; ".join(f"{b} → {n}" for b, n in notes))
        return None

    def _probe(self, base: str) -> str | None:
        """Пробный запрос к базе: None — работает, иначе причина отказа."""
        try:
            # Попыток больше, чем обычно: неудачная проверка гасит Melbet на
            # PROBE_BACKOFF секунд, а фид умеет обрывать соединение и на
            # ровном месте — обидно потерять обход из-за одного обрыва.
            data = self._get(base, "LineFeed", "GetSportsShortZip",
                             self._sports_params(), 8, attempts=3)
        except requests.JSONDecodeError:
            # Ответ есть, но это не фид: обычно так отвечает сам сайт —
            # значит эндпоинты лежат под другим префиксом.
            return "ответ не JSON (это страница сайта, а не фид)"
        except requests.RequestException as exc:
            resp = getattr(exc, "response", None)
            code = getattr(resp, "status_code", None)
            if code in (403, 406):
                if _is_geo_stub(resp):
                    return (f"HTTP {code} — вместо фида страница «отключите "
                            f"VPN»: зеркало не пускает адрес этого сервера")
                return (f"HTTP {code} — фид отклонил запрос (адрес не пускают "
                        f"либо строка запроса не та)")
            if code == 404:
                return f"HTTP {code} — нет такого пути (не тот префикс базы)"
            if code:
                return f"HTTP {code}"
            return f"нет ответа ({type(exc).__name__})"
        except Exception as exc:  # noqa: BLE001 — мало ли что вернут
            return f"ответ не разобрать ({type(exc).__name__})"
        if not (isinstance(data, dict) and data.get("Value")):
            return "ответ без списка видов спорта"
        return None

    # ------------------------------------------------------------------
    # Параметры запросов
    # ------------------------------------------------------------------
    # Наборы ниже переписаны с запросов самого сайта, и ПОРЯДОК КЛЮЧЕЙ В НИХ
    # ЗНАЧИМ: requests сохраняет порядок словаря, а фид отвечает 406, если
    # строка запроса отличается от той, которую шлёт сайт (см. заголовок
    # модуля). Пересортировать ключи — сломать парсер.

    def _get(self, base: str, feed: str, method: str, params: dict,
             timeout: float, attempts: int = 2):
        """Запрос к фиду с повторными попытками.

        Под нагрузкой (а линия — это сотни запросов за обход) фид начинает
        рвать соединение на подключении, причём выборочно: часть запросов
        проходит, часть обрывается. Повтор через небольшую паузу вытягивает
        такие обрывы; отказы с кодом (406, 404) не повторяем — они не про
        нагрузку, а про сам запрос."""
        url = f"{base}/{feed}/{method}"
        for attempt in range(1, attempts + 1):
            try:
                return self.get_json(url, params=params, timeout=timeout)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == attempts:
                    raise
                time.sleep(RETRY_PAUSE * attempt)
        return None

    def _sports_params(self) -> dict:
        return {"virtualSports": "true", "groupChamps": "true"}

    def _champs_params(self, sport_id: int) -> dict:
        return {"sport": sport_id}

    def _games_params(self, key: str, value: int | None, count: int) -> dict:
        # Фильтр (sports или champs) обязан идти ПЕРВЫМ: с «count&mode&sports»
        # тот же запрос — это 406. mode=4 тоже обязателен, без него фид
        # отвечает 200 и пустым списком.
        params: dict = {}
        if value is not None:
            params[key] = value
        params["count"] = _count(count)
        params["mode"] = 4
        return params

    def _champ_params(self, champ_id: int) -> dict:
        return {"champ": champ_id, "mode": 4}

    def _zip_params(self, game_id: int) -> dict:
        return {"id": game_id, "isSubGames": "true", "GroupEvents": "true",
                "countevents": 250, "grMode": 4, "marketType": 1, "cfview": 0}

    # ------------------------------------------------------------------
    # Сбор линии
    # ------------------------------------------------------------------

    def fetch_odds(self) -> list[MarketOdds]:
        return self._collect(live=False)

    def fetch_live_odds(self) -> list[MarketOdds]:
        return self._collect(live=True)

    def _collect(self, live: bool) -> list[MarketOdds]:
        base = self._resolve_base()
        if not base:
            return []
        deadline = time.monotonic() + MELBET_FEED_TIMEOUT
        feed = "LiveFeed" if live else "LineFeed"
        games = self._line(base, feed, live, deadline)
        if not games:
            if not live:
                # Домен мог смениться прямо во время работы — при следующем
                # обходе перебор начнётся заново.
                self._base = None
            return []
        log.info("Melbet: %s — %d событий", "лайв" if live else "линия",
                 len(games))

        events = [RawEvent(game=g, picks=_picks(g)) for g in games.values()]
        for ev in events:
            ev.base_picks = ev.picks
        if MELBET_FULL_MARKETS and not live:
            events = self._enrich(base, feed, events, deadline)

        layout = detect(events, forced=MELBET_GROUPS)
        self._log_layout(layout)

        now = time.time()
        by_key: dict[str, MarketOdds] = {}
        for ev in events:
            for o in self._event_odds(ev, layout, now, live):
                by_key[o.match_key] = o
        return list(by_key.values())

    def _line(self, base: str, feed: str, live: bool,
              deadline: float) -> dict[int, dict]:
        """Снимок линии с рынками: по запросу на чемпионат (или вид спорта).

        Лайв собирается по видам спорта: матчей в игре немного, и в 50
        событий на запрос они укладываются."""
        sports = self._sports(base, feed)
        if not sports:
            log.warning("Melbet: список видов спорта не пришёл — линию "
                        "запросить не по чему")
            return {}
        count = MELBET_LIVE_COUNT if live else MELBET_SPORT_COUNT
        if not live and MELBET_CHAMPS:
            champs = self._champs(base, feed, sports, deadline)
            if champs:
                games = self._games(base, feed, "champs",
                                    [li for li, _gc in champs], count,
                                    deadline)
                # Часть запросов фид мог оборвать (он это делает под
                # нагрузкой) — тогда добираем верхушку каждого вида спорта:
                # это всего десятки запросов, зато линия не окажется пустой.
                expected = sum(gc for _li, gc in champs)
                if len(games) < expected * CHAMPS_ENOUGH:
                    log.warning("Melbet: по чемпионатам собрано %d событий из "
                                "%d — фид ответил не на все запросы, добираю "
                                "линию по видам спорта", len(games), expected)
                    games.update(self._games(base, feed, "sports", sports,
                                             count, deadline))
                self._tails(base, feed, champs, count, games, deadline)
                return games
            log.info("Melbet: чемпионаты не пришли — беру верхушку линии по "
                     "видам спорта")
        return self._games(base, feed, "sports", sports, count, deadline)

    def _sports(self, base: str, feed: str) -> list[int]:
        try:
            data = self._get(base, feed, "GetSportsShortZip",
                             self._sports_params(), 15)
        except Exception as exc:  # noqa: BLE001
            log.info("Melbet: список видов спорта недоступен (%s)", exc)
            return []
        out = []
        for sport in data.get("Value") or []:
            sid = sport.get("I") or sport.get("Id")
            if isinstance(sid, int) and sid not in out:
                out.append(sid)
        return out

    def _champs(self, base: str, feed: str, sports: list[int],
                deadline: float) -> list[tuple[int, int]]:
        """Все чемпионаты линии: [(id чемпионата, сколько в нём событий)].

        Число событий (GC) нужно, чтобы знать, где линия чемпионата не
        влезет в один запрос, — таким событиям потом добираются хвосты."""
        out: list[tuple[int, int]] = []
        fails: Counter = Counter()
        pool = ThreadPoolExecutor(max_workers=MELBET_CHAMP_WORKERS)
        futures = [pool.submit(self._sport_champs, base, feed, sid)
                   for sid in sports]
        try:
            for fut in as_completed(futures):
                try:
                    out.extend(fut.result())
                except Exception as exc:  # noqa: BLE001 — вид не критичен
                    fails[_reason(exc)] += 1
                    continue
                if time.monotonic() > deadline:
                    break
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        _log_fails("списка чемпионатов", fails)
        if out:
            log.info("Melbet: чемпионатов %d, событий в них по счётчику фида "
                     "%d", len(out), sum(gc for _li, gc in out))
        return out

    def _sport_champs(self, base: str, feed: str,
                      sport_id: int) -> list[tuple[int, int]]:
        data = self._get(base, feed, "GetChampsZip",
                         self._champs_params(sport_id), 20)
        value = data.get("Value") if isinstance(data, dict) else None
        out = []
        for champ in value or []:
            if not isinstance(champ, dict):
                continue
            li, gc = champ.get("LI"), champ.get("GC")
            if isinstance(li, int) and li and (gc or 0) > 0:
                out.append((li, int(gc)))
        return out

    def _games(self, base: str, feed: str, key: str, values: list[int],
               count: int, deadline: float) -> dict[int, dict]:
        """События с рынками по списку чемпионатов или видов спорта."""
        games: dict[int, dict] = {}
        fails: Counter = Counter()
        pool = ThreadPoolExecutor(
            max_workers=MELBET_CHAMP_WORKERS if key == "champs"
            else MELBET_SPORT_WORKERS)
        futures = [pool.submit(self._chunk_games, base, feed, key, v, count)
                   for v in values]
        try:
            for fut in as_completed(futures):
                try:
                    chunk = fut.result()
                except Exception as exc:  # noqa: BLE001 — пачка не критична
                    fails[_reason(exc)] += 1
                    continue
                for g in chunk:
                    gid = _game_id(g)
                    if gid:
                        games[gid] = g
                if time.monotonic() > deadline:
                    log.info("Melbet: дедлайн снимка линии истёк, собрано "
                             "%d событий", len(games))
                    break
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        _log_fails("линии", fails)
        return games

    def _chunk_games(self, base: str, feed: str, key: str, value: int | None,
                     count: int) -> list[dict]:
        data = self._get(base, feed, "Get1x2_VZip",
                         self._games_params(key, value, count), 20)
        value_ = data.get("Value") if isinstance(data, dict) else None
        return [g for g in (value_ or []) if isinstance(g, dict)]

    def _tails(self, base: str, feed: str, champs: list[tuple[int, int]],
               count: int, games: dict[int, dict], deadline: float) -> None:
        """Хвосты чемпионатов, которые не влезли в один запрос.

        Get1x2_VZip отдаёт максимум 50 событий, а бывают чемпионаты и на
        сотню. Остальные события берём из индекса чемпионата
        (GetChampZip) — там их полный список, но БЕЗ рынков: рынки к ним
        приедут с полной росписью. Поэтому без росписи хвосты и не нужны."""
        big = [(li, gc) for li, gc in champs if gc > _count(count)]
        if not big or not MELBET_FULL_MARKETS:
            return
        added = 0
        for li, _gc in big:
            if time.monotonic() > deadline:
                break
            try:
                data = self._get(base, feed, "GetChampZip",
                                 self._champ_params(li), 20)
            except Exception:  # noqa: BLE001
                continue
            champ = data.get("Value") if isinstance(data, dict) else None
            if not isinstance(champ, dict):
                continue
            # У события внутри индекса нет ни вида спорта, ни названия
            # чемпионата — они стоят на самом чемпионате.
            head = {k: champ.get(k) for k in ("SN", "L", "SI", "LI")
                    if champ.get(k) is not None}
            for g in champ.get("G") or []:
                gid = _game_id(g) if isinstance(g, dict) else None
                if gid and gid not in games:
                    games[gid] = {**head, **g}
                    added += 1
        if added:
            log.info("Melbet: у %d крупных чемпионатов добрано %d событий из "
                     "индекса (рынки к ним придут с росписью)", len(big),
                     added)

    # ------------------------------------------------------------------
    # Полная роспись события
    # ------------------------------------------------------------------

    def _enrich(self, base: str, feed: str, events: list[RawEvent],
                deadline: float) -> list[RawEvent]:
        """Догружает роспись событий: лестницы линий целиком и подигры.

        Роспись — это запрос НА КАЖДОЕ событие, а их в линии тысячи, и за
        отведённое время всех не обойти. Поэтому запросов не больше
        MELBET_FULL_MARKETS_MAX и начинаем с тех событий, которые начнутся
        раньше: по ним и ставят, а до дальних дело дойдёт на следующих
        обходах. Событие, не успевшее до дедлайна или упавшее с ошибкой,
        остаётся с рынками из снимка линии — это не хуже, чем без росписи."""
        queue = sorted(events,
                       key=lambda ev: _start_ts(ev.game) or float("inf"))
        if MELBET_FULL_MARKETS_MAX > 0:
            queue = queue[:MELBET_FULL_MARKETS_MAX]
        pool = ThreadPoolExecutor(max_workers=MELBET_FULL_MARKETS_WORKERS)
        futures = {pool.submit(self._game_zip, base, feed, _game_id(ev.game)):
                   ev for ev in queue}
        extra: list[RawEvent] = []
        subgames: list[tuple[RawEvent, dict]] = []
        got = 0
        try:
            for fut in as_completed(futures):
                ev = futures[fut]
                try:
                    full = fut.result()
                except Exception:  # noqa: BLE001
                    full = None
                if full:
                    picks = _picks(full)
                    if picks:
                        ev.game = {**ev.game, **full}
                        ev.picks = picks
                        got += 1
                    subgames.extend((ev, sub) for sub in _subgames(full))
                if time.monotonic() > deadline:
                    log.info("Melbet: дедлайн росписи истёк, %d/%d событий "
                             "успели обогатиться", got, len(queue))
                    break
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if subgames:
            extra = self._subgame_odds(base, feed, subgames, deadline)
        log.info("Melbet: роспись получена по %d из %d событий (в линии %d), "
                 "рынков подигр: %d", got, len(queue), len(events), len(extra))
        return events + extra

    def _subgame_odds(self, base: str, feed: str,
                      subgames: list[tuple[RawEvent, dict]],
                      deadline: float) -> list[RawEvent]:
        """Рынки подигр: у каждой подигры свой id и свой запрос росписи.

        Внутри GetGameZip подигры приходят только списком (id, предмет,
        период) — без котировок. Поэтому берём их отдельно и ровно столько,
        сколько разрешает MELBET_SUBGAMES: подигр у одного футбольного
        матча бывает под три десятка (угловые, карточки, удары по каждому
        тайму), и тянуть их все — это в тридцать раз больше запросов, чем
        на саму линию."""
        out: list[RawEvent] = []
        pool = ThreadPoolExecutor(max_workers=MELBET_FULL_MARKETS_WORKERS)
        futures = {pool.submit(self._game_zip, base, feed, _game_id(sub)):
                   (parent, sub) for parent, sub in subgames}
        try:
            for fut in as_completed(futures):
                parent, sub = futures[fut]
                try:
                    full = fut.result()
                except Exception:  # noqa: BLE001
                    full = None
                if full:
                    ev = _subgame_event(parent, {**sub, **full})
                    if ev is not None:
                        out.append(ev)
                if time.monotonic() > deadline:
                    break
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return out

    def _game_zip(self, base: str, feed: str, game_id: int | None):
        if not game_id:
            return None
        data = self._get(base, feed, "GetGameZip", self._zip_params(game_id),
                         10)
        value = data.get("Value") if isinstance(data, dict) else None
        return value if isinstance(value, dict) else None

    # ------------------------------------------------------------------
    # Разбор события в котировки
    # ------------------------------------------------------------------

    def _log_layout(self, layout: Layout) -> None:
        note = layout.summary()
        if note == self._layout_note:
            return
        self._layout_note = note
        log.info("Melbet: раскладка рынков — %s", note)
        if layout.inverted:
            log.warning("Melbet: стороны исходов не совпали с ожидаемыми "
                        "(%s) — коды в фиде поменялись, разметка "
                        "перевёрнута по данным линии",
                        ", ".join(sorted(layout.inverted)))
        if layout.skipped:
            log.warning("Melbet: рынки %s не отдаются: по линии не удалось "
                        "подтвердить, где какая сторона (скорее всего в одну "
                        "группу попали разные рынки)",
                        ", ".join(sorted(layout.skipped)))

    def _event_odds(self, ev: RawEvent, layout: Layout, now: float,
                    live: bool) -> list[MarketOdds]:
        game = ev.game
        team1, team2 = _team(game, "O1"), _team(game, "O2")
        if not team1 or not team2 or team1 == team2:
            return []
        start_ts = _start_ts(game)
        if not live and (start_ts is None or start_ts <= now):
            return []       # прематч — только то, что ещё не началось

        sport_name = (game.get("SN") or "Спорт").strip()
        league = (game.get("L") or "").strip()
        sport = f"{sport_name} · {league}" if league else sport_name
        base = dict(bookmaker=self.name, sport=sport, team1=team1,
                    team2=team2, kind=KIND_LIVE if live else KIND_PREMATCH,
                    start_time=format_start(start_ts) if start_ts else None,
                    start_ts=start_ts, url=_event_url(game))

        out: list[MarketOdds] = []
        out.extend(self._winner(ev, layout, base))
        out.extend(self._totals(ev, layout, base))
        out.extend(self._handicaps(ev, layout, base))
        for family in (ITOTAL1, ITOTAL2):
            out.extend(self._ind_totals(ev, layout, base, family))
        out.extend(self._both_score(ev, layout, base))
        return out

    def _group(self, ev: RawEvent, family: str,
               layout: Layout) -> int | None:
        """Группа, из которой берём семейство рынков у этого события.

        У рынков всего матча группа выбрана по всей линии сразу. У подигры
        своя нумерация групп, и общий выбор к ней неприменим: берём группу,
        только если она в подигре одна — иначе непонятно, какой из рынков
        основной, а угадывать нельзя."""
        if not layout.usable(family):
            return None
        return layout.group(family) if not ev.scope else ev.sole_group(family)

    def _sides(self, ev: RawEvent, family: str, group: int | None,
               layout: Layout) -> tuple[dict, dict]:
        """Две стороны рынка с учётом того, что калибровка их перевернула."""
        first, second = FAMILY_CODES[family][0], FAMILY_CODES[family][1]
        if layout.is_inverted(family):
            first, second = second, first
        lines = ev.lines(family, group)
        return lines.get(first) or {}, lines.get(second) or {}

    def _winner(self, ev: RawEvent, layout: Layout,
                base: dict) -> list[MarketOdds]:
        group = self._group(ev, WINNER, layout)
        if group is None:
            return []
        win = ev.lines(WINNER, group)
        k1 = (win.get(T_W1) or {}).get(None)
        k2 = (win.get(T_W2) or {}).get(None)
        kx = (win.get(T_DRAW) or {}).get(None)
        if not k1 or not k2:
            return []
        scope = ev.scope
        if kx:
            if not sane_1x2_margin(k1, kx, k2):
                return []
            return [MarketOdds(
                market="Исход (1X2)",
                market_key=f"winner1x2:{scope}" if scope else "winner1x2",
                outcome1="П1", outcome2="П2", outcome3="X",
                k1=k1, k2=k2, k3=kx, **base)]
        if not sane_pair_margin(k1, k2):
            return []
        return [MarketOdds(
            market="Победитель",
            market_key=f"winner:{scope}" if scope else "winner",
            outcome1="П1", outcome2="П2", k1=k1, k2=k2, **base)]

    def _totals(self, ev: RawEvent, layout: Layout,
                base: dict) -> list[MarketOdds]:
        group = self._group(ev, TOTAL, layout)
        if group is None:
            return []
        over, under = self._sides(ev, TOTAL, group, layout)
        scope = ev.scope
        out = []
        for line, k_over in sorted(over.items(), key=_line_sort):
            k_under = under.get(line)
            if line is None or not k_under \
                    or not sane_pair_margin(k_over, k_under):
                continue
            pt = fmt_total(line)
            out.append(MarketOdds(
                market=f"Тотал {pt}",
                market_key=f"total:{scope}:{pt}" if scope else f"total:{pt}",
                outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                k1=k_over, k2=k_under, **base))
        return out

    def _handicaps(self, ev: RawEvent, layout: Layout,
                   base: dict) -> list[MarketOdds]:
        group = self._group(ev, HCAP, layout)
        if group is None:
            return []
        first, second = self._sides(ev, HCAP, group, layout)
        scope = ev.scope
        out = []
        for line, k1 in sorted(first.items(), key=_line_sort):
            if line is None:
                continue
            k2 = second.get(-line)
            if not k2 or not sane_pair_margin(k1, k2):
                continue
            h1 = fmt_hcap(line)
            h2 = neg_hcap(h1)
            out.append(MarketOdds(
                market=f"Фора {h1}", market_key=f"hcap:{scope}:{h1}",
                outcome1=f"Ф1 {h1}", outcome2=f"Ф2 {h2}",
                k1=k1, k2=k2, **base))
        return out

    def _ind_totals(self, ev: RawEvent, layout: Layout, base: dict,
                    family: str) -> list[MarketOdds]:
        group = self._group(ev, family, layout)
        if group is None:
            return []
        over, under = self._sides(ev, family, group, layout)
        side = layout.itotal_side(family)
        team = base["team1"] if side == 1 else base["team2"]
        scope = ev.scope
        out = []
        for line, k_over in sorted(over.items(), key=_line_sort):
            k_under = under.get(line)
            if line is None or not k_under \
                    or not sane_pair_margin(k_over, k_under):
                continue
            pt = fmt_total(line)
            out.append(MarketOdds(
                market=f"Тотал {pt} ({team})",
                market_key=f"itotal:{side}:{scope}:{pt}",
                outcome1=f"ИТБ {pt}", outcome2=f"ИТМ {pt}",
                k1=k_over, k2=k_under, **base))
        return out

    def _both_score(self, ev: RawEvent, layout: Layout,
                    base: dict) -> list[MarketOdds]:
        group = self._group(ev, BTS, layout)
        if group is None:
            return []
        yes, no = ev.flat(BTS, group)
        if layout.is_inverted(BTS):
            yes, no = no, yes
        if not yes or not no or not sane_pair_margin(yes, no):
            return []
        scope = ev.scope
        return [MarketOdds(
            market="Обе забьют",
            market_key=f"bothscore:{scope}" if scope else "bothscore",
            outcome1="Да", outcome2="Нет", k1=yes, k2=no, **base)]


# ---------------------------------------------------------------------------
# Разбор ответа фида
# ---------------------------------------------------------------------------


def _picks(game: dict) -> list[tuple[int, int, float | None, float]]:
    """Все исходы события: (группа, код исхода, линия, коэффициент).

    Исходы разбросаны по нескольким полям и вложены по-разному: в
    компактном снимке это плоский список E, в полной росписи —
    сгруппированные GE/AE, где группа стоит на самой группе, а не на
    исходе. Собираем всё в один плоский список."""
    out: list[tuple[int, int, float | None, float]] = []
    # Идём только по полям с рынками, а не по событию целиком: у самого
    # события тоже может оказаться поле «G», и тогда исходы без своей
    # группы унаследовали бы номер, к рынкам отношения не имеющий.
    for key in _MARKET_KEYS:
        if key in game:
            _walk(game[key], out, None, 0)
    return out


def _walk(node, out: list, group: int | None, depth: int) -> None:
    if depth > _MAX_DEPTH:
        return
    if isinstance(node, list):
        for item in node:
            _walk(item, out, group, depth + 1)
        return
    if not isinstance(node, dict):
        return
    own = node.get("G")
    if isinstance(own, int):
        group = own
    code, coef = node.get("T"), node.get("C")
    if isinstance(code, int) and coef is not None:
        value = _num(coef)
        if value and value > 1 and group is not None:
            out.append((group, code, _num(node.get("P")), value))
        return
    for key in _MARKET_KEYS:
        if key in node:
            _walk(node[key], out, group, depth + 1)


def _subgames(full: dict) -> list[dict]:
    """Подигры события, у которых понятно, к чему относятся их рынки.

    Периоды («1-й тайм») идут раньше предметных подигр («угловые»,
    «жёлтые карточки»): запросов на подигры выделено немного, и тратить их
    в первую очередь стоит на периоды — они есть у всех БК, а значит и
    сопоставляются чаще. Подигра без названия пропускается: её рынок
    склеился бы с рынком всего матча в ложную вилку."""
    named = []
    for sub in full.get("SG") or []:
        if not isinstance(sub, dict) or not _game_id(sub):
            continue
        if not _subgame_scope(sub):
            continue
        # предмет счёта пуст — это чистый период
        named.append((bool(str(sub.get("TG") or "").strip()), sub))
    named.sort(key=lambda item: item[0])
    return [sub for _subject, sub in named[:max(MELBET_SUBGAMES, 0)]]


def _subgame_scope(sub: dict) -> str:
    """Область рынков подигры: «Угловые» + «1-й тайм» → corners+half1."""
    name = " ".join(str(sub.get(k) or "").strip()
                    for k in _SUBGAME_NAME_KEYS).strip()
    return market_scope(name)


def _subgame_event(parent: RawEvent, sub: dict) -> RawEvent | None:
    """Подигра как отдельное событие: те же команды, свой набор рынков."""
    scope = _subgame_scope(sub)
    picks = _picks(sub)
    if not scope or not picks:
        return None
    return RawEvent(game={**parent.game, **_without_markets(sub)},
                    scope=scope, picks=picks, base_picks=[])


def _without_markets(sub: dict) -> dict:
    """Поля подигры без её рынков: имена команд и время берём у родителя,
    а вот id и название подигры пригодятся для ссылки."""
    return {k: v for k, v in sub.items()
            if k not in _MARKET_KEYS and k not in ("SG", "O1", "O2")}


def _game_id(game: dict) -> int | None:
    for key in ("I", "CI", "Id", "ID"):
        value = game.get(key)
        if isinstance(value, int) and value:
            return value
    return None


def _team(game: dict, key: str) -> str:
    """Имя команды: строкой, списком (пара в теннисе) или объектом."""
    value = game.get(key)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("N") or "").strip()
    if isinstance(value, list):
        names = [str(v.get("N") if isinstance(v, dict) else v).strip()
                 for v in value]
        return " / ".join(n for n in names if n)
    ext = game.get(key + "E")
    if isinstance(ext, list) and ext:
        names = [str(v.get("N") if isinstance(v, dict) else v).strip()
                 for v in ext]
        return " / ".join(n for n in names if n)
    return ""


def _start_ts(game: dict) -> float | None:
    value = game.get("S")
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _event_url(game: dict) -> str:
    """Ссылка на событие. Роутер сайта ориентируется на числовые id вида
    спорта, чемпионата и события; не сойдётся формат — откроется линия."""
    sport, champ, gid = game.get("SI"), game.get("LI"), _game_id(game)
    if sport and champ and gid:
        return f"{MELBET_SITE_HOST}/ru/line/{sport}/{champ}/{gid}"
    return f"{MELBET_SITE_HOST}/ru/line"


def _is_geo_stub(resp) -> bool:
    """Ответ — это заглушка «Пожалуйста, отключите VPN», а не отказ фида.

    Зеркало отдаёт её с кодом 403 на ЛЮБОЙ путь, когда адрес клиента ему
    не нравится (проверено: melbet.ru так отвечает и на корень сайта, и
    на эндпоинты фида). Отличить её от «строка запроса не та» важно:
    первое лечится другим зеркалом или прокси, второе — правкой парсера.

    Тело берётся байтами и раскодируется вручную, а не через resp.text:
    заглушка приходит БЕЗ charset в Content-Type, и requests по стандарту
    разбирает такой ответ как ISO-8859-1 — русские слова в resp.text
    превращаются в «Ð¾Ñ\x82ÐºÐ»Ñ\x8eÑ\x87Ð¸Ñ\x82Ðµ», и поиск по ним ничего
    не находил."""
    try:
        head = (resp.content or b"")[:4000].decode("utf-8", "ignore").lower()
    except Exception:  # noqa: BLE001 — тело могло не приехать
        return False
    return any(m in head for m in _GEO_STUB_MARKERS)


def _reason(exc: BaseException) -> str:
    """Короткая причина отказа: «HTTP 406» или «ConnectionError»."""
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return f"HTTP {code}" if code else type(exc).__name__


def _log_fails(what: str, fails: Counter) -> None:
    """Сводка отказов. Без неё неполная линия выглядит просто короткой, и не
    видно, что фид половину запросов оборвал."""
    if not fails:
        return
    log.warning("Melbet: %d запросов %s не прошли (%s)", sum(fails.values()),
                what, ", ".join(f"{r}×{n}" for r, n in fails.most_common(4)))


def _count(value: int) -> int:
    """Допустимое для фида значение count.

    Проверено на живом фиде: count обязан быть кратен пяти (count=8 — это
    406 на весь запрос), а больше 50 событий за раз он всё равно не
    отдаёт, сколько ни проси."""
    value = min(int(value or 0), _MAX_PER_REQUEST)
    value -= value % 5
    return max(value, 5)


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _line_sort(item) -> float:
    line = item[0]
    return line if line is not None else float("-inf")
