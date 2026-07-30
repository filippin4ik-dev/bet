"""Melbet — публичный JSON-фид линии (движок 1xBet), прематч и лайв.

Melbet работает на том же движке, что 1xBet/Betwinner/Linebet, и раздаёт
линию тем же фидом, которым пользуется сам сайт, — без авторизации:

    GET {база}/LineFeed/GetSportsShortZip?lng=ru&country=1&partner=8
        — список видов спорта: [{"I": 1, "N": "Футбол"}, ...]
    GET {база}/LineFeed/Get1x2_VZip?sports=1&count=500&lng=ru&mode=4
        — линия одного вида спорта: события с ОСНОВНЫМИ рынками
    GET {база}/LineFeed/GetGameZip?id=<событие>&isSubGames=true&...
        — полная роспись одного события: лестницы тоталов и фор,
          индивидуальные тоталы, подигры (таймы, периоды, сеты)

База — это домен сайта плюс префикс: сейчас это «/service-api», на
зеркалах постарше эндпоинты лежат прямо в корне. Домен и префикс парсер
перебирает сам (как у Fonbet) и запоминает первый рабочий; жёстко задать
можно через MELBET_API_HOST.

ВАЖНО ПРО ДОСТУП. Сайт пускает только российские адреса: с зарубежного IP
и с IP дата-центров edge отвечает 406 ещё до фида, а melbet.ru отдаёт
страницу «отключите VPN». То есть парсер рассчитан на тот же российский
VPS, с которого работают Winline и Fonbet; проверить, что фид отвечает
именно с вашего сервера, можно командой `python -m app.diagnose_melbet`.

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
- те же рынки у подигр (тайм, период, сет): подигра приходит со своим
  названием, из него берётся scope, поэтому «тотал 1-го тайма» Melbet
  сшивается с таким же рынком других БК, а не с тоталом всего матча.

Что НЕ берём: двойной шанс, «обе забьют», чет/нечет, точный счёт и
прочую экзотику. Их коды исхода проверить теми же структурными
признаками нельзя (у рынка нет ни лестницы, ни линии), а ошибка в
стороне «да/нет» — это ложная вилка с чужим «нет». Появится словарь
имён рынков — добавим.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from ..config import (MELBET_API_HOST, MELBET_COUNTRY, MELBET_FEED_TIMEOUT,
                      MELBET_FULL_MARKETS, MELBET_FULL_MARKETS_WORKERS,
                      MELBET_GROUPS, MELBET_HOSTS, MELBET_LANG,
                      MELBET_LIVE_COUNT, MELBET_MIN_REFRESH, MELBET_PARTNER,
                      MELBET_SITE_HOST, MELBET_SPORT_COUNT,
                      MELBET_SPORT_WORKERS)
from ..models import KIND_LIVE, KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import (fmt_hcap, fmt_total, format_start, market_scope,
                         neg_hcap, sane_1x2_margin, sane_pair_margin)
from .melbet_layout import (HCAP, ITOTAL1, ITOTAL2, T_DRAW, T_W1, T_W2, TOTAL,
                            WINNER, FAMILY_CODES, Layout, RawEvent, detect)

log = logging.getLogger("parsers.melbet")

# Префиксы, под которыми движок раздаёт фид на разных зеркалах
API_PREFIXES = ["/service-api", ""]
# Если ни один домен не ответил — не перебираем весь список каждый цикл
PROBE_BACKOFF = 300
# Куда лезть за исходами внутри ответа. Ключи перечислены явно, обходить
# ответ целиком нельзя: в нём есть ещё и подигры (SG), и их рынки нельзя
# смешивать с рынками всего матча.
_MARKET_KEYS = ("E", "AE", "ME", "GE")
# Где у подигры лежит её название («1-й тайм», «2-й сет»). Разные версии
# движка кладут его в разные поля; без названия подигра пропускается —
# неизвестно, к какому периоду относятся её рынки.
_SUBGAME_NAME_KEYS = ("SN", "N", "TN", "NA", "GN")
_MAX_DEPTH = 6


class MelbetParser(BaseParser):
    name = "Melbet"
    min_refresh = MELBET_MIN_REFRESH or None

    def __init__(self) -> None:
        super().__init__()
        self._base: str | None = None
        self._last_probe = 0.0
        self._layout_note = ""
        # Роспись тянется пулом потоков, а пул соединений requests по
        # умолчанию держит десять: без этого лишние соединения к тому же
        # хосту открывались бы и закрывались на каждый запрос.
        adapter = requests.adapters.HTTPAdapter(
            pool_maxsize=max(MELBET_FULL_MARKETS_WORKERS,
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

    def _resolve_base(self) -> str | None:
        if self._base:
            return self._base
        now = time.monotonic()
        if now - self._last_probe < PROBE_BACKOFF:
            return None
        self._last_probe = now
        for base in self._candidates():
            try:
                data = self.get_json(f"{base}/LineFeed/GetSportsShortZip",
                                     params=self._params(), timeout=8)
            except Exception as exc:  # noqa: BLE001 — пробуем следующий
                log.debug("Melbet: %s не подошёл (%s)", base, exc)
                continue
            if isinstance(data, dict) and data.get("Value"):
                log.info("Melbet: рабочая база фида — %s", base)
                self._base = base
                return base
        log.warning(
            "Melbet: ни одно зеркало не отдало линию (повтор через %d с). "
            "Фид доступен только с российского адреса; проверить — "
            "python -m app.diagnose_melbet, задать домен вручную — "
            "MELBET_API_HOST.", PROBE_BACKOFF)
        return None

    def _params(self, **extra) -> dict:
        params = {"lng": MELBET_LANG, "country": MELBET_COUNTRY,
                  "partner": MELBET_PARTNER, "gr": 70, "mode": 4,
                  "getEmpty": "true"}
        params.update(extra)
        return params

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
        """Компактный снимок линии: по запросу на каждый вид спорта."""
        sports = self._sports(base, feed)
        count = MELBET_LIVE_COUNT if live else MELBET_SPORT_COUNT
        games: dict[int, dict] = {}
        if not sports:
            # Списка видов спорта нет — просим всё разом (меньше событий,
            # но лучше, чем ничего).
            for g in self._sport_games(base, feed, None, count):
                games[_game_id(g)] = g
            return {k: v for k, v in games.items() if k}

        pool = ThreadPoolExecutor(max_workers=MELBET_SPORT_WORKERS)
        futures = {pool.submit(self._sport_games, base, feed, sid, count): sid
                   for sid in sports}
        try:
            for fut in as_completed(futures):
                try:
                    chunk = fut.result()
                except Exception:  # noqa: BLE001 — вид спорта не критичен
                    continue
                if len(chunk) >= count:
                    # Фид отдал ровно столько, сколько попросили, — значит
                    # линия вида спорта на этом обрезана.
                    log.info("Melbet: вид спорта %s упёрся в лимит %d "
                             "событий — часть линии не видна, поднимите "
                             "MELBET_SPORT_COUNT", futures[fut], count)
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
        return games

    def _sports(self, base: str, feed: str) -> list[int]:
        try:
            data = self.get_json(f"{base}/{feed}/GetSportsShortZip",
                                 params=self._params(virtualSports="true",
                                                     groupChamps="true"),
                                 timeout=15)
        except Exception as exc:  # noqa: BLE001
            log.info("Melbet: список видов спорта недоступен (%s) — "
                     "беру линию одним запросом", exc)
            return []
        out = []
        for sport in data.get("Value") or []:
            sid = sport.get("I") or sport.get("Id")
            if isinstance(sid, int):
                out.append(sid)
        return out

    def _sport_games(self, base: str, feed: str, sport_id: int | None,
                     count: int) -> list[dict]:
        params = self._params(count=count)
        if sport_id is not None:
            params["sports"] = sport_id
        data = self.get_json(f"{base}/{feed}/Get1x2_VZip", params=params,
                             timeout=20)
        value = data.get("Value") if isinstance(data, dict) else None
        return [g for g in (value or []) if isinstance(g, dict)]

    # ------------------------------------------------------------------
    # Полная роспись события
    # ------------------------------------------------------------------

    def _enrich(self, base: str, feed: str, events: list[RawEvent],
                deadline: float) -> list[RawEvent]:
        """Догружает роспись каждого события: лестницы линий и подигры.

        Событие, не успевшее до дедлайна или упавшее с ошибкой, остаётся с
        основными рынками из общего снимка — это не хуже, чем совсем без
        росписи."""
        pool = ThreadPoolExecutor(max_workers=MELBET_FULL_MARKETS_WORKERS)
        futures = {pool.submit(self._game_zip, base, feed, _game_id(ev.game)):
                   ev for ev in events}
        extra: list[RawEvent] = []
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
                    extra.extend(_subgame_events(ev, full))
                if time.monotonic() > deadline:
                    log.info("Melbet: дедлайн росписи истёк, %d/%d событий "
                             "успели обогатиться", got, len(events))
                    break
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        log.info("Melbet: полная роспись получена по %d/%d событиям "
                 "(+%d подигр)", got, len(events), len(extra))
        return events + extra

    def _game_zip(self, base: str, feed: str, game_id: int | None):
        if not game_id:
            return None
        data = self.get_json(
            f"{base}/{feed}/GetGameZip",
            params=self._params(id=game_id, cfview=0, isSubGames="true",
                                GroupEvents="true", countevents=250,
                                grMode=4, marketType=1),
            timeout=10)
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


def _subgame_events(parent: RawEvent, full: dict) -> list[RawEvent]:
    """Подигры события (тайм, период, сет) — как отдельные наборы рынков.

    Берём только подигры с названием: из него получается scope, без
    которого рынок тайма склеился бы с рынком всего матча в ложную вилку."""
    out: list[RawEvent] = []
    for sub in full.get("SG") or []:
        if not isinstance(sub, dict):
            continue
        name = next((str(sub[k]) for k in _SUBGAME_NAME_KEYS
                     if isinstance(sub.get(k), str) and sub[k].strip()), "")
        scope = market_scope(name)
        if not scope:
            continue
        picks = _picks(sub)
        if not picks:
            continue
        out.append(RawEvent(game={**parent.game, **_without_markets(sub)},
                            scope=scope, picks=picks, base_picks=[]))
    return out


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
