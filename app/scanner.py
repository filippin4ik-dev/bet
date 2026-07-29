"""Фоновый сканер: опрашивает БК, ищет вилки и сохраняет новые в SQLite.

Каждая БК обновляется в СВОЁМ потоке и независимо от остальных. Раньше
прематч шёл общим кругом и ждал самую медленную БК — быстрая всё это время
стояла, и в интерфейсе висело «Fonbet: 7 минут назад», хотя её собственный
обход занимает полминуты. Пауза между обходами ОДНОЙ БК — SCAN_INTERVAL
(в лайве LIVE_PER_BK_GAP), но не чаще, чем позволяет сама БК
(BaseParser.min_refresh). Вилки считает отдельный поток по флагу
«данные изменились»: пересчёт по всей линии стоит секунды, и делать его
после каждой БК значит отдать процессор пересчёту вместо обходов.

Котировки хранятся ПО КАЖДОЙ БК и живут между обходами:
- пришли свежие данные БК — её котировки заменяются целиком;
- обход БК сорвался (сеть, рендеринг) — старые котировки остаются до ODDS_TTL,
  матчи не «слетают» после каждого обхода;
- матч начался (по распознанному времени старта) — убирается сразу.

Лайв-сканер можно включать и выключать НА ХОДУ из админки (флаг
config.LIVE_ENABLED): выключенный лайв освобождает CPU и сеть прематчу.
Оттуда же гасится и перезапускается ОТДЕЛЬНАЯ БК (см. bk_control и
Scanner.request_restart): мешающая БК убирается из вилок, а зависшая
получает чистую сессию — без перезапуска сервера.
"""
import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import bk_control, config, db
from .arbitrage import (_canon, _market_group, _neg_hcap, _time_clusters,
                        build_name_canon_map, find_arbs, find_arbs_1x2,
                        norm_team)
from .config import (ARB_RECALC_MIN_GAP, BK_FAIL_BACKOFF_MAX,
                     FUZZY_NAME_MAP_REFRESH, LIVE_ODDS_TTL, LIVE_PER_BK_GAP,
                     LIVE_SCAN_INTERVAL, ODDS_TTL, SCAN_INTERVAL)
from .models import Arb, Arb3, KIND_LIVE, KIND_PREMATCH, MarketOdds
from .parsers import get_parsers
from .parsers.base import BaseParser
from .parsers.html_utils import display_market

log = logging.getLogger("scanner")


class Scanner:
    """Фоновый сканер одного режима: прематч ИЛИ лайв.

    mode="prematch" — редкие обходы, матчи убираются по времени старта;
    mode="live"     — частые обходы, матчи в игре, короткий TTL котировок
                      (в лайве старый кэф опаснее его отсутствия).
    """

    def __init__(self, mode: str = KIND_PREMATCH,
                 parsers: list[BaseParser] | None = None) -> None:
        self.mode = mode
        self.live = mode == KIND_LIVE
        self.interval = LIVE_SCAN_INTERVAL if self.live else SCAN_INTERVAL
        self.ttl = LIVE_ODDS_TTL if self.live else ODDS_TTL
        # parsers подставляются в тестах; в бою — все включённые БК
        self.parsers = get_parsers() if parsers is None else list(parsers)
        # откуда взяты парсеры: при перезапуске набор из реестра пересобирается
        # заново (сессии, cookie, websocket-состояние — с чистого листа),
        # подставленные в тестах остаются теми же объектами
        self._parsers_from_registry = parsers is None
        self._lock = threading.Lock()
        self._arbs: list[Arb] = []
        # трёхисходные вилки (рынок «Исход 1X2»: П1/X/П2) — отдельный список,
        # т.к. у Arb3 другая форма (3 плеча вместо 2)
        self._arbs3: list[Arb3] = []
        # котировки по каждой БК (живут между циклами) + время их получения
        self._odds_by_bk: dict[str, list[MarketOdds]] = {}
        self._fetched_at: dict[str, float] = {}
        self._last_scan: float | None = None
        self._scan_count = 0
        # какие БК опрашиваются ПРЯМО СЕЙЧАС — чтобы в интерфейсе было
        # видно, что БК не «зависла», а как раз обновляется
        self._busy: set[str] = set()
        # БК, выключенные в админке: их воркеры простаивают, а котировки
        # забыты (см. bk_control и _worker)
        self._disabled: set[str] = set()
        # запрошенные из админки перезапуски: имена отдельных БК и флаг
        # «перезапустить весь сканер»
        self._restart_bks: set[str] = set()
        self._restart_all = False
        self._events_checked = 0   # уникальных событий сейчас в памяти
        self._quotes_checked = 0   # всего котировок (событие x БК)
        # ключи вилок прошлого цикла — чтобы писать в историю только новые
        self._prev_keys: set[str] = set()
        self._prev_keys3: set[str] = set()
        # когда каждая живая вилка появилась впервые (match_key → unix-время);
        # пока вилка держится между обновлениями, её таймер не сбрасывается
        self._first_seen: dict[str, float] = {}
        self._first_seen3: dict[str, float] = {}
        # кэш фаззи-слияния имён команд (build_name_canon_map) — дорогая
        # операция, пересчитываем не чаще FUZZY_NAME_MAP_REFRESH (см.
        # _name_map) и переиспользуем на вилки, матчи и роспись.
        self._name_map_cache: dict[str, str] = {}
        self._name_map_at: float = 0.0
        self._name_map_lock = threading.Lock()
        # Разбор котировок по событиям (список матчей и роспись одного
        # матча) стоит секунды на сотни тысяч котировок, а интерфейс тянет
        # его каждые несколько секунд. Держим готовый разбор до прихода
        # свежих котировок — тогда он же и обнуляется (_store_odds), чтобы
        # кэш не держал в памяти прошлое поколение линии.
        self._odds_rev = 0          # растёт при каждом изменении котировок
        self._groups: dict[str, list[MarketOdds]] = {}
        self._groups_name_map: dict[str, str] = {}
        self._groups_rev = -1
        self._matches: list[dict] = []
        self._matches_rev = -1
        # Пока один запрос считает разбор, второй ждёт его результат, а не
        # считает то же самое второй раз (полсотни секунд CPU на двоих).
        self._groups_lock = threading.Lock()
        # пришли новые котировки — вилки надо пересчитать (флаг снимает
        # поток пересчёта, см. _recalc_worker)
        self._dirty = False
        # по потоку на каждую БК + поток пересчёта вилок
        self._pool_size = len(self.parsers) + 1
        self._executor = ThreadPoolExecutor(max_workers=self._pool_size,
                                            thread_name_prefix=f"scan-{mode}")
        self._stop = asyncio.Event()
        # Пока флаг установлен, воркеры БК работают. Лайв снимает его на
        # ходу (выключение из админки), прематч держит установленным всегда.
        self._workers_on = threading.Event()
        # Воркеры ещё живы? Снятый _workers_on — только просьба
        # остановиться: БК сначала докачивают начатый обход.
        self._running = False

    def _fetch(self, parser):
        return parser.safe_fetch_live() if self.live else parser.safe_fetch()

    # ---------- публичное состояние ----------

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            return {
                "mode": self.mode,
                "scan_interval": self.interval,
                "scan_count": self._scan_count,
                "scanning": bool(self._busy),
                "running": self._workers_on.is_set(),
                "last_scan": self._last_scan,
                "events_checked": self._events_checked,
                "quotes_checked": self._quotes_checked,
                # по каждой БК: сколько котировок, сколько секунд назад они
                # получены, не идёт ли обход прямо сейчас (индикатор
                # свежести в UI) и не выключена ли она в админке
                "bookmakers": {
                    bk: {"count": len(self._odds_by_bk.get(bk) or ()),
                         "age_sec": round(now - self._fetched_at.get(bk, now)),
                         "busy": bk in self._busy,
                         "off": bk in self._disabled}
                    # БК, которую опрашивают впервые, котировок ещё не
                    # принесла, но показать её уже надо
                    for bk in sorted(set(self._odds_by_bk) | self._busy
                                     | self._disabled)},
                "arbs": [a.to_dict() for a in self._arbs],
                "arbs_1x2": [a.to_dict() for a in self._arbs3],
            }

    def odds_snapshot(self) -> list[dict]:
        """Все котировки, находящиеся сейчас в памяти (все найденные матчи)."""
        with self._lock:
            return [o.to_dict()
                    for odds in self._odds_by_bk.values() for o in odds]

    # ---------- матчи: группировка котировок по событиям ----------

    def _all_odds(self) -> list[MarketOdds]:
        with self._lock:
            return [o for odds in self._odds_by_bk.values() for o in odds]

    def _name_map(self, all_odds: list[MarketOdds]) -> dict[str, str]:
        """Каноничные имена команд — один кэш на весь сканер.

        build_name_canon_map (фаззи-слияние разных написаний одного имени по
        всей линии) — самая дорогая операция сканера: секунды на сотни тысяч
        котировок. Её результат нужен и пересчёту вилок, и группировке
        матчей, поэтому считаем не чаще FUZZY_NAME_MAP_REFRESH и делим на
        всех: раньше карту строил КАЖДЫЙ запрос списка матчей, и вкладка
        «Матчи» отвечала двадцать секунд, отбирая процессор у обходов БК."""
        now = time.time()
        with self._name_map_lock:
            if now - self._name_map_at <= FUZZY_NAME_MAP_REFRESH:
                return self._name_map_cache
            self._name_map_cache = build_name_canon_map(all_odds)
            self._name_map_at = now
            return self._name_map_cache

    @staticmethod
    def _event_groups(all_odds: list[MarketOdds],
                      name_map: dict[str, str] | None = None
                      ) -> dict[str, list[MarketOdds]]:
        """Группирует котировки всех БК по событию (матч + время старта).

        Ключ события устойчив между опросами: kind | отсортированная пара
        нормализованных (и фаззи-каноничных — см. build_name_canon_map)
        команд | номер кластера времени старта (разные матчи одной пары
        команд не сливаются)."""
        if name_map is None:
            name_map = build_name_canon_map(all_odds)
        clusters = _time_clusters(all_odds, name_map)
        groups: dict[str, list[MarketOdds]] = {}
        for o in all_odds:
            t1 = _canon(name_map, norm_team(o.team1))
            t2 = _canon(name_map, norm_team(o.team2))
            if not t1 or not t2 or t1 == t2:
                continue
            teams = frozenset((t1, t2))
            cluster = 0
            if o.kind == KIND_PREMATCH and o.start_ts:
                cluster = clusters.get((o.kind, teams), {}).get(o.start_ts, 0)
            key = f"{o.kind}|{'|'.join(sorted(teams))}|{cluster}"
            groups.setdefault(key, []).append(o)
        return groups

    def _grouped(self) -> tuple[int, dict[str, list[MarketOdds]],
                                dict[str, str]]:
        """Разбор котировок по событиям — готовый или посчитанный сейчас.

        Вместе с разбором отдаёт номер поколения котировок, по которому он
        сделан: по нему видно, не устарел ли он уже."""
        with self._lock:
            if self._groups_rev == self._odds_rev:
                return self._groups_rev, self._groups, self._groups_name_map
        with self._groups_lock:
            with self._lock:
                # пока ждали очереди, разбор мог посчитать сосед
                if self._groups_rev == self._odds_rev:
                    return (self._groups_rev, self._groups,
                            self._groups_name_map)
                rev = self._odds_rev
                all_odds = [o for odds in self._odds_by_bk.values()
                            for o in odds]
            name_map = self._name_map(all_odds)
            groups = self._event_groups(all_odds, name_map)
            with self._lock:
                # за время разбора пришли новые котировки — этот разбор уже
                # не соответствует памяти сканера, в кэш его не кладём
                if rev == self._odds_rev:
                    self._groups = groups
                    self._groups_name_map = name_map
                    self._groups_rev = rev
            return rev, groups, name_map

    def matches_snapshot(self) -> list[dict]:
        """Список всех найденных матчей, сгруппированных по событию.

        Уже отсортирован по времени начала — в этом порядке его и показывает
        интерфейс, а готовый список кэшируется до свежих котировок."""
        rev, groups, name_map = self._grouped()
        with self._lock:
            if self._matches_rev == rev:
                # копия: вызывающий может отсортировать список по-своему
                return list(self._matches)
        out = []
        for event_id, odds in groups.items():
            # образец с временем старта и самыми длинными именами команд
            sample = max(odds, key=lambda o: (o.start_ts is not None,
                                              len(o.team1) + len(o.team2)))
            books = sorted({o.bookmaker for o in odds})
            markets = {_market_group(o, name_map) for o in odds}
            start_ts = min((o.start_ts for o in odds if o.start_ts),
                           default=None)
            out.append({
                "id": event_id,
                "kind": sample.kind,
                "sport": sample.sport,
                "match": f"{sample.team1} — {sample.team2}",
                "team1": sample.team1,
                "team2": sample.team2,
                "start_ts": start_ts,
                "start_time": sample.start_time,
                "bookmakers": books,
                "markets_count": len(markets),
            })
        out.sort(key=lambda m: (m["start_ts"] or float("inf"),
                                m["sport"], m["match"]))
        with self._lock:
            if rev == self._odds_rev:
                self._matches = out
                self._matches_rev = rev
        return list(out)

    def match_detail(self, event_id: str) -> dict | None:
        """Полная роспись одного события: все рынки всех БК бок о бок."""
        _, groups, name_map = self._grouped()
        odds = groups.get(event_id)
        if not odds:
            return None
        sample = max(odds, key=lambda o: (o.start_ts is not None,
                                          len(o.team1) + len(o.team2)))
        base_t1 = _canon(name_map, norm_team(sample.team1))

        markets: dict[str, dict] = {}
        for o in odds:
            # ориентация к team1 события: у БК с перевёрнутым порядком
            # команд исходы меняются местами (тоталы, «обе забьют»,
            # чет/нечет и инд. тоталы от порядка команд не зависят —
            # инд. тотал привязан к имени команды, а не к позиции). Через
            # name_map, а не «сырой» norm_team — иначе БК с чуть другим
            # написанием имени той же команды ложно посчитались бы
            # «перевёрнутыми» и исходы поменялись бы местами неверно.
            flipped = (_canon(name_map, norm_team(o.team1)) != base_t1
                       and not o.market_key.startswith(("total", "bothscore",
                                                        "itotal", "oddeven")))
            mg = _market_group(o, name_map)
            m = markets.get(mg)
            if m is None:
                m = markets[mg] = {
                    "market": display_market(o.market_key, o.market),
                    "market_key": o.market_key,
                    "outcome1": o.outcome1,
                    "outcome2": o.outcome2,
                    "outcome3": o.outcome3,  # «X» у рынка «Исход 1X2»
                    "quotes": {},
                }
                if flipped:
                    m["outcome1"] = self._swap_side(o.outcome2)
                    m["outcome2"] = self._swap_side(o.outcome1)
                    if o.market_key.startswith("hcap"):
                        # в имени рынка — линия team1 СОБЫТИЯ, а не этой БК
                        h1 = o.market_key.rsplit(":", 1)[1]
                        key = o.market_key[:-len(h1)] + _neg_hcap(h1)
                        m["market"] = display_market(key, o.market)
            k1, k2 = (o.k2, o.k1) if flipped else (o.k1, o.k2)
            if o.bookmaker not in m["quotes"]:
                # ничья (k3) симметрична — не зависит от порядка команд
                m["quotes"][o.bookmaker] = {"k1": k1, "k2": k2, "k3": o.k3,
                                            "url": o.url}

        def _sort_key(item):
            key = item[1]["market_key"]
            cat = 0 if key.startswith("winner") else \
                1 if key.startswith("bothscore") else \
                1 if key.startswith("oddeven") else \
                2 if key.startswith("total") else \
                4 if key.startswith("itotal") else 3
            parts = key.split(":")
            if key.startswith("itotal"):
                # itotal:<сторона>:<scope>:<линия>
                scope = parts[2] if len(parts) >= 4 else ""
            else:
                scope = parts[1] if len(parts) > 2 else ""
            try:
                line = abs(float(parts[-1].replace("+", "")))
            except ValueError:
                line = 0.0
            return (cat, scope, line, key)

        market_rows = []
        for _, m in sorted(markets.items(), key=_sort_key):
            m["quotes"] = [
                {"bookmaker": bk, **ks}
                for bk, ks in sorted(m["quotes"].items())]
            market_rows.append(m)

        return {
            "id": event_id,
            "kind": sample.kind,
            "sport": sample.sport,
            "match": f"{sample.team1} — {sample.team2}",
            "team1": sample.team1,
            "team2": sample.team2,
            "start_ts": min((o.start_ts for o in odds if o.start_ts),
                            default=None),
            "start_time": sample.start_time,
            "bookmakers": sorted({o.bookmaker for o in odds}),
            "markets": market_rows,
        }

    @staticmethod
    def _swap_side(label: str) -> str:
        """«Ф2 +1.5» → «Ф1 +1.5», «П2» → «П1» (перестановка стороны
        исхода при перевёрнутом порядке команд у одной из БК)."""
        for a, b in (("1", "2"), ("2", "1")):
            for pref in ("Ф", "П"):
                if label.startswith(pref + a):
                    return pref + b + label[2:]
        return label

    # ---------- обновление состояния ----------

    def _prune_locked(self, now: float) -> None:
        """Убирает протухшие данные БК и (для прематча) начавшиеся матчи."""
        for bk in list(self._odds_by_bk):
            if now - self._fetched_at.get(bk, 0) > self.ttl:
                # БК давно не отвечает — её кэфы больше нельзя считать
                # актуальными, на них нельзя ставить
                log.info("Котировки %s устарели (> %d с) — убраны",
                         bk, int(self.ttl))
                del self._odds_by_bk[bk]
                self._fetched_at.pop(bk, None)
                continue
            if self.live:
                # лайв: матч «в игре», по времени старта не отсеиваем —
                # завершённые матчи БК сама перестаёт отдавать (уйдут по TTL)
                continue
            kept = [o for o in self._odds_by_bk[bk] if not o.started(now)]
            if len(kept) != len(self._odds_by_bk[bk]):
                log.info("%s: %d матчей начались — убраны из прематча",
                         bk, len(self._odds_by_bk[bk]) - len(kept))
                self._odds_by_bk[bk] = kept

    def _store_odds(self, bk: str, odds: list[MarketOdds]) -> None:
        """Складывает свежие котировки одной БК и помечает вилки к пересчёту.

        Пустой результат (сбой обхода) НЕ затирает старые данные —
        они живут до ODDS_TTL, чтобы матчи не пропадали между обходами.
        """
        now = time.time()
        with self._lock:
            if odds:
                self._odds_by_bk[bk] = odds
                self._fetched_at[bk] = now
            self._prune_locked(now)
            self._mark_changed_locked()

    def _mark_changed_locked(self) -> None:
        """Котировки изменились: вилки к пересчёту, разбор по событиям — в
        утиль. Кэш разбора чистим сразу, а не по требованию: он держит ссылки
        на прежние котировки, и в памяти лежало бы два поколения линии
        (сотни мегабайт на слабом VPS). Вызывать под self._lock."""
        self._dirty = True
        self._odds_rev += 1
        self._groups = {}
        self._groups_name_map = {}
        self._groups_rev = -1
        self._matches = []
        self._matches_rev = -1

    def _recalc(self) -> tuple[list[Arb], list[Arb3]]:
        """Пересчитывает вилки по котировкам ВСЕХ БК, лежащим в памяти.

        Дорогая операция (сотни тысяч котировок), поэтому её выполняет один
        отдельный поток и не чаще, чем успевает: обновления нескольких БК,
        пришедшие подряд, схлопываются в один пересчёт (см. _recalc_worker).
        """
        now = time.time()
        with self._lock:
            self._dirty = False
            all_odds = [o for os_ in self._odds_by_bk.values() for o in os_]

        name_map = self._name_map(all_odds)
        arbs = find_arbs(all_odds, name_map)
        arbs3 = find_arbs_1x2(all_odds, name_map)
        with self._lock:
            # таймер жизни вилки: сохраняем момент первого обнаружения,
            # исчезнувшие вилки забываем (появятся снова — таймер с нуля)
            prev_seen = self._first_seen
            self._first_seen = {}
            for a in arbs:
                a.first_seen = prev_seen.get(a.match_key, now)
                self._first_seen[a.match_key] = a.first_seen
            prev_seen3 = self._first_seen3
            self._first_seen3 = {}
            for a in arbs3:
                a.first_seen = prev_seen3.get(a.match_key, now)
                self._first_seen3[a.match_key] = a.first_seen
            self._arbs = arbs
            self._arbs3 = arbs3
            self._last_scan = now
            self._events_checked = len({o.event_key for o in all_odds})
            self._quotes_checked = len(all_odds)
        return arbs, arbs3

    def _save_new(self, arbs: list[Arb], arbs3: list[Arb3]) -> None:
        """Пишет в историю только вилки, которых не было в прошлый раз.

        Лайв-вилки не пишем: они меняются ежесекундно и быстро засорили бы
        базу. История — только по прематчу."""
        if self.live:
            return
        with self._lock:
            new = [a for a in arbs if a.match_key not in self._prev_keys]
            self._prev_keys = {a.match_key for a in arbs}
            new3 = [a for a in arbs3 if a.match_key not in self._prev_keys3]
            self._prev_keys3 = {a.match_key for a in arbs3}
        db.save_arbs(new)
        db.save_arbs3(new3)

    # ---------- цикл: по потоку на каждую БК ----------

    @staticmethod
    def _supports_live(parser: BaseParser) -> bool:
        """БК реально отдаёт лайв (переопределила fetch_live_odds)?"""
        return type(parser).fetch_live_odds is not BaseParser.fetch_live_odds

    def _worker_parsers(self) -> list[BaseParser]:
        if not self.live:
            return list(self.parsers)
        return [p for p in self.parsers if self._supports_live(p)]

    def enabled(self) -> bool:
        """Должен ли сканер сейчас работать. Лайв включается и выключается
        на ходу (админка правит config.LIVE_ENABLED), прематч — основной
        режим и работает всегда."""
        return bool(config.LIVE_ENABLED) if self.live else True

    def is_running(self) -> bool:
        """Работают ли сейчас воркеры БК. Остаётся True и после выключения,
        пока БК не докачали начатые обходы."""
        with self._lock:
            return self._running

    def parser_names(self) -> list[str]:
        """Имена БК этого режима (для админки)."""
        return [p.name for p in self._worker_parsers()]

    def request_restart(self, bookmaker: str | None = None) -> bool:
        """Перезапуск из админки: одной БК или всего сканера.

        Перезапуск — это не «пнуть посильнее», а начать с чистого листа:
        парсер создаётся заново (новая сессия requests, новые cookie и
        websocket-состояние), его котировки забываются. Нужен, когда БК
        отвечает, но линию отдаёт мусорную или перестала обновляться —
        раньше это лечилось только перезапуском всего сервера.

        Возвращает False, если такой БК в этом режиме нет."""
        if bookmaker is None:
            with self._lock:
                self._restart_all = True
            return True
        if bookmaker not in self.parser_names():
            return False
        with self._lock:
            self._restart_bks.add(bookmaker)
        log.info("[%s] запрошен перезапуск БК %s", self.mode, bookmaker)
        return True

    def _take_restart(self, bookmaker: str) -> bool:
        """Забирает запрос на перезапуск этой БК (и снимает его)."""
        with self._lock:
            if bookmaker not in self._restart_bks:
                return False
            self._restart_bks.discard(bookmaker)
            return True

    async def run(self) -> None:
        """Держит по одному воркеру на каждую БК, пока режим включён.

        Медленная БК не тормозит быструю: каждая обновляется в своём темпе,
        а вилки пересчитывает отдельный воркер по свежим данным всех БК."""
        if not self._worker_parsers():
            log.info("[%s] ни одна БК не поддерживает этот режим — простой",
                     self.mode)
            await self._stop.wait()
            return
        loop = asyncio.get_running_loop()
        workers: list = []
        while not self._stop.is_set():
            if self.enabled() and not workers:
                parsers = self._worker_parsers()
                self._workers_on.set()
                with self._lock:
                    self._running = True
                    self._restart_all = False
                workers = [loop.run_in_executor(self._executor, self._worker, p)
                           for p in parsers]
                workers.append(loop.run_in_executor(self._executor,
                                                    self._recalc_worker))
                log.info("[%s] сканер запущен, период обхода: %s", self.mode,
                         ", ".join(f"{p.name} — {self._period(p):.0f} c"
                                   for p in parsers))
            elif not self.enabled() and workers:
                await self._stop_workers(workers, "выключен — ресурсы "
                                                  "отданы прематчу")
                workers = []
            elif workers and self._restart_requested():
                await self._stop_workers(workers, "перезапуск из админки")
                self._recreate_parsers()
                workers = []
                continue     # поднять воркеры заново, не ожидая секунду
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
        self._workers_on.clear()
        with self._lock:
            self._running = False

    def _restart_requested(self) -> bool:
        with self._lock:
            return self._restart_all

    def _recreate_parsers(self) -> None:
        """Пересобирает набор парсеров (перезапуск сканера).

        Набор из реестра берётся заново — это и подхватывает изменения
        окружения, и даёт БК чистые сессии. Подставленные в тестах парсеры
        пересоздаются по классу, а если у него другой конструктор —
        остаются как есть (терять их состояние тесту незачем)."""
        if self._parsers_from_registry:
            self.parsers = get_parsers()
        else:
            self.parsers = [self._recreate(p) for p in self.parsers]
        self._ensure_executor()

    @staticmethod
    def _recreate(parser: BaseParser) -> BaseParser:
        try:
            return type(parser)()
        except Exception:  # noqa: BLE001 — конструктор с аргументами (тесты)
            return parser

    def _ensure_executor(self) -> None:
        """Держит в пуле поток на каждую БК + поток пересчёта.

        После перезапуска БК в наборе может стать больше (включили новую
        переменной окружения), а пул потоков не растягивается — тогда
        последние БК ждали бы свободного потока вместо обхода."""
        need = len(self.parsers) + 1
        if self._pool_size >= need:
            return
        old, self._executor = self._executor, ThreadPoolExecutor(
            max_workers=need, thread_name_prefix=f"scan-{self.mode}")
        self._pool_size = need
        old.shutdown(wait=False)

    async def _stop_workers(self, workers: list, reason: str) -> None:
        """Гасит воркеры и забывает котировки: показывать кэфы режима,
        который больше не обновляется, нельзя."""
        log.info("[%s] сканер %s (ждём завершения текущих обходов)",
                 self.mode, reason)
        self._workers_on.clear()
        for w in workers:
            try:
                await w
            except Exception:  # noqa: BLE001
                log.exception("[%s] воркер завершился с ошибкой", self.mode)
        with self._lock:
            self._odds_by_bk.clear()
            self._fetched_at.clear()
            self._mark_changed_locked()
            self._busy.clear()
            self._disabled.clear()
            self._restart_bks.clear()
            self._arbs = []
            self._arbs3 = []
            self._events_checked = 0
            self._quotes_checked = 0
            self._running = False
        log.info("[%s] сканер остановлен", self.mode)

    def _replace_parser(self, parser: BaseParser) -> BaseParser:
        """Меняет парсер БК на свежий экземпляр (перезапуск одной БК)."""
        fresh = self._recreate(parser)
        if fresh is not parser:
            self.parsers = [fresh if p is parser else p for p in self.parsers]
        return fresh

    def _forget_bk(self, bk: str) -> None:
        """Убирает котировки одной БК (выключили или перезапустили).

        Держать в вилках кэфы БК, которая больше не обновляется, нельзя:
        по ним нельзя ставить, а в интерфейсе они выглядят как живые."""
        with self._lock:
            had = self._odds_by_bk.pop(bk, None)
            self._fetched_at.pop(bk, None)
            if had:
                self._mark_changed_locked()

    def _mark_disabled(self, bk: str) -> None:
        with self._lock:
            first = bk not in self._disabled
            self._disabled.add(bk)
        if first:
            log.info("[%s] %s: выключена в админке — обходы остановлены",
                     self.mode, bk)
            self._forget_bk(bk)

    def _mark_enabled(self, bk: str) -> None:
        with self._lock:
            was_off = bk in self._disabled
            self._disabled.discard(bk)
        if was_off:
            log.info("[%s] %s: включена в админке — обходы возобновлены",
                     self.mode, bk)

    def _period(self, parser: BaseParser) -> float:
        """Минимальный период между обходами ОДНОЙ БК.

        В лайве все БК ходят с коротким шагом, в прематче — раз в
        SCAN_INTERVAL, но не чаще собственного ограничения БК
        (BaseParser.min_refresh)."""
        if self.live:
            return LIVE_PER_BK_GAP
        return max(self.interval, parser.min_refresh or 0)

    def _worker(self, parser: BaseParser) -> None:
        """Цикл обновления одной БК (в отдельном потоке).

        Здесь же применяются решения оператора из админки: выключенная БК
        только простаивает (и её котировки забыты), а запрошенный перезапуск
        пересоздаёт парсер — с чистой сессией и без прежних котировок."""
        failures = 0
        while self._workers_on.is_set() and not self._stop.is_set():
            if self._take_restart(parser.name):
                log.info("[%s] %s: перезапуск — новая сессия, котировки "
                         "сброшены", self.mode, parser.name)
                parser = self._replace_parser(parser)
                self._forget_bk(parser.name)
                failures = 0
            if not bk_control.is_enabled(parser.name):
                self._mark_disabled(parser.name)
                failures = 0
                self._sleep_while_running(1.0)
                continue
            self._mark_enabled(parser.name)
            started = time.monotonic()
            try:
                with self._lock:
                    self._busy.add(parser.name)
                try:
                    odds = self._fetch(parser)
                finally:
                    with self._lock:
                        self._busy.discard(parser.name)
                # только складываем котировки: вилки посчитает отдельный
                # поток, иначе каждая БК платила бы за полный пересчёт и
                # обходы растягивались бы в разы
                self._store_odds(parser.name, odds)
                failures = 0 if odds else failures + 1
                with self._lock:
                    self._scan_count += 1
                    total = sum(len(o) for o in self._odds_by_bk.values())
                log.info("[%s] %s: %d котировок за %.0f c — в памяти %d",
                         self.mode, parser.name, len(odds),
                         time.monotonic() - started, total)
            except Exception:  # noqa: BLE001
                failures += 1
                log.exception("[%s] %s: ошибка обновления",
                              self.mode, parser.name)
            self._sleep_before_next(started, parser, failures)

    def _recalc_worker(self) -> None:
        """Единственный поток пересчёта вилок.

        Пересчёт по всем БК стоит секунды (сотни тысяч котировок), поэтому
        считаем не после каждой БК, а по флагу: пока идёт пересчёт,
        обновления копятся и схлопываются в один следующий проход. Один
        поток на сканер — значит нагрузка на CPU ограничена и предсказуема,
        а вилки всегда считаются по САМЫМ свежим данным."""
        while self._workers_on.is_set() and not self._stop.is_set():
            with self._lock:
                dirty = self._dirty
            if not dirty:
                time.sleep(0.2)
                continue
            started = time.monotonic()
            try:
                arbs, arbs3 = self._recalc()
                self._save_new(arbs, arbs3)
                log.log(logging.DEBUG if self.live else logging.INFO,
                        "[%s] пересчёт вилок за %.1f c: %d вилок "
                        "(+%d на 1X2)%s", self.mode,
                        time.monotonic() - started, len(arbs), len(arbs3),
                        f", лучшая {arbs[0].profit_pct:.2f}%" if arbs else "")
            except Exception:  # noqa: BLE001
                log.exception("[%s] ошибка пересчёта вилок", self.mode)
            # передышка: без неё пересчёт идёт непрерывно (котировки
            # приходят чаще, чем он успевает) и отбирает процессор у самих
            # обходов БК. В лайве котировок мало и пересчёт дешёвый.
            self._sleep_while_running(0.5 if self.live else ARB_RECALC_MIN_GAP)

    def _sleep_before_next(self, started: float, parser: BaseParser,
                           failures: int = 0) -> None:
        """Ждёт до следующего обхода этой БК: в прематче обходы идут не чаще
        раза в SCAN_INTERVAL (обход дольше периода — следующий сразу), в
        лайве — короткая пауза, чтобы не долбить сервер БК вплотную.

        У БК со своим ограничением (BaseParser.min_refresh) период не меньше
        её собственного: слишком частые обходы такая БК начинает срывать.

        После неудачных обходов подряд пауза удваивается (до потолка): если
        БК не отвечает или её защита рвёт соединение, прежний темп запросов
        ничего не даст, а котировки всё равно живут до ODDS_TTL."""
        period = self._period(parser)
        cap = LIVE_ODDS_TTL / 3 if self.live else BK_FAIL_BACKOFF_MAX
        if failures:
            period = min(period * 2 ** min(failures, 6), cap)
        deadline = max(started + period, time.monotonic() + 0.5)
        # Пауза между обходами прематча — минуты, и досыпать её, когда БК
        # уже выключили из админки, значит «применить» через полчаса.
        self._sleep_while_running(deadline - time.monotonic(),
                                  wake=lambda: self._interrupted(parser.name))

    def _interrupted(self, bk: str) -> bool:
        """Ждать дальше незачем: БК выключили или попросили перезапустить."""
        with self._lock:
            if bk in self._restart_bks:
                return True
        return not bk_control.is_enabled(bk)

    def _sleep_while_running(self, seconds: float, wake=None) -> None:
        """Пауза, которая прерывается остановкой сканера.

        wake — необязательная проверка «просыпаться прямо сейчас»; её
        спрашивают не чаще раза в секунду, чтобы длинная пауза не
        превратилась в опрос базы по кругу."""
        deadline = time.monotonic() + seconds
        next_check = 0.0
        while self._workers_on.is_set() and not self._stop.is_set():
            now = time.monotonic()
            if now >= deadline:
                return
            if wake is not None and now >= next_check:
                if wake():
                    return
                next_check = now + 1.0
            time.sleep(min(0.3, deadline - now))

    def stop(self) -> None:
        self._stop.set()
        self._workers_on.clear()
        self._executor.shutdown(wait=False)
