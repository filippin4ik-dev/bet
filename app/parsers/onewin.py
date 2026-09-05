"""1win — спортивный раздел на платформе top-parser, прематч и лайв.

Сам сайт 1win линию не считает: раздел «Ставки» на нём — виджет
(Betting_Frame) платформы top-parser, и котировки идут прямо с её шлюза,
без авторизации. Понадобился только id партнёра (ONEWIN_PARTNER_ID), он же
externalPartnerId в адресе websocket'а сайта. Проверено с адреса
дата-центра: и REST, и websocket отвечают (сам сайт 1win при этом тоже
открывается — прежняя заметка в README про 403 устарела).

Схема (снята с запросов сайта):

    POST {ONEWIN_API_HOST}/matches/get-many?l=<lang>&p=<partner>
         {"limit": 5000, "service": "prematch"|"live",
          "excludeSportType": ["polybet", "racing"]}
      → result.items: матчи (id, homeTeam/awayTeam, sport, tournament,
        category, startAt) — ВСЯ линия одним ответом (~2.5 тыс. событий).
        Названия здесь ТОЛЬКО английские, какой бы язык ни просили.

    WS {ONEWIN_WS_URL}?Language=ru-RU&externalPartnerId=<partner>&EIO=4
       &transport=websocket   (socket.io v4)
      → 42["subscribe", {"messageType": "subscribe-match-odds",
           "data": {"matchIds": [...], "isBaseOddsGroups": true|false}}]
      ← 42["u", {"messageType": "match-odds-snapshot", "data": {matchId,
           oddsGroups: [{id, name, isBase, renderType, outcomes,
           oddsList: [{id, name, outcome, cf, status, vars}]}]}}]
      ← 42["u", {"messageType": "match-odds", ...}] — дельты (только
           изменившиеся кэфы, по тем же id).
        С Language=ru-RU подписи рынков и исходов приходят ПО-РУССКИ — в
        том числе имена команд в исходах рынка результата. Оттуда парсер и
        берёт названия команд: по ним событие сшивается с остальными БК
        (английские имена из REST — только запасной вариант).

Группа рынков описывает себя сама, id рынков знать не нужно:
- renderType «cols-3» с исходами 1/x/2 → «Исход 1X2» (движок
  arbitrage.find_arbs_1x2, с проверкой маржи sane_1x2_margin);
- «cols-2» с исходами 1/2 без линии → победитель (winner);
- «fora-2»: у каждого исхода vars.v1 — линия форы СВОЕЙ команды; пара
  собирается по противоположным линиям (+1.5 у первой ↔ −1.5 у второй);
- «total-2»: исходы over/under, линия в vars.v1;
- yes/no → только «обе забьют»; odd/even → «чет/нечет» без командных
  вариантов (у прочих да/нет-рынков по одним исходам смысл не отличить).
Предмет/период рынка («1-й тайм», «Угловые», «Карта 1», «Индивидуальный
тотал <команда>») — из русского названия группы через общий market_scope.
Не сшиваем с остальными БК: «Досрочный выигрыш» (другие правила расчёта),
интервалы «с 1 по 10 минуту», двойной шанс, точный счёт, гонки до N,
игроки — у них либо больше двух исходов, либо своя семантика.

Базовые группы (isBaseOddsGroups=true: исход, тотал, фора — ~7 КБ на
событие) берём у всей линии, полную роспись (~30-100 КБ) — у
ONEWIN_FULL_MARKETS_MAX ближайших событий: по ним и ставят, а всю линию
целиком качать незачем. Подписка идёт пачками по ONEWIN_WS_BATCH id (на
пятистах сервер молчит) в одном соединении за обход.
"""
import json
import logging
import re
import time

import websocket

from ..config import (HTTP_TIMEOUT, ONEWIN_API_HOST, ONEWIN_FEED_TIMEOUT,
                      ONEWIN_FULL_MARKETS_MAX, ONEWIN_LANG, ONEWIN_MIN_REFRESH,
                      ONEWIN_PARTNER_ID, ONEWIN_SITE_HOST, ONEWIN_WS_BATCH,
                      ONEWIN_WS_BATCH_WAIT, ONEWIN_WS_URL)
from ..models import KIND_LIVE, KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import (fmt_hcap, fmt_total, format_start, market_scope,
                         sane_1x2_margin, sane_pair_margin)

log = logging.getLogger("parsers.onewin")

# Виды спорта платформы (slug) → подпись, под которой тот же спорт идёт у
# остальных БК. Неизвестный slug превращается в слова («table-tennis» →
# «Table tennis») — такой спорт с русскими подписями не сойдётся, поэтому
# таблица покрывает всё, что есть в живой линии.
_SPORTS = {
    "football": "Футбол", "ice-hockey": "Хоккей", "basketball": "Баскетбол",
    "tennis": "Теннис", "table-tennis": "Настольный теннис",
    "volleyball": "Волейбол", "handball": "Гандбол", "baseball": "Бейсбол",
    "american-football": "Американский футбол", "cricket": "Крикет",
    "darts": "Дартс", "futsal": "Мини-футбол", "boxing": "Бокс",
    "ufc": "Единоборства", "mma": "Единоборства",
    "martial-arts": "Единоборства", "kabaddi": "Кабадди",
    "rugby-union": "Регби", "rugby-league": "Регби", "rugby": "Регби",
    "badminton": "Бадминтон", "aussie-rules-football": "Австралийский футбол",
    "floorball": "Флорбол", "lacrosse": "Лакросс", "chess": "Шахматы",
    "snooker": "Снукер", "water-polo": "Водное поло", "beach-volleyball":
    "Пляжный волейбол", "beach-football": "Пляжный футбол",
    "field-hockey": "Хоккей на траве", "bandy": "Хоккей с мячом",
    "squash": "Сквош", "golf": "Гольф", "cycling": "Велоспорт",
    "formula-1": "Формула-1", "motorsport": "Автоспорт",
    "wrestling": "Борьба", "bowls": "Боулз", "netball": "Нетбол",
    "padel": "Падел-теннис", "pickleball": "Пиклбол",
}
# Киберспортивные дисциплины: подпись «Киберспорт · <дисциплина>. <турнир>»,
# как у остальных БК (корень «киберспорт» общий, дисциплина — в лиге).
_ESPORTS = {
    "cs2": "CS2", "counter-strike": "CS2", "csgo": "CS2", "dota-2": "Dota 2",
    "lol": "League of Legends", "league-of-legends": "League of Legends",
    "valorant": "Valorant", "rainbow-six": "Rainbow Six",
    "mobile-legends": "Mobile Legends", "arena-of-valor": "Arena of Valor",
    "honor-of-kings": "Honor of Kings", "standoff-2": "Standoff 2",
    "heroes-3": "Heroes 3", "starcraft": "StarCraft", "starcraft-2":
    "StarCraft 2", "overwatch": "Overwatch", "rocket-league": "Rocket League",
    "king-of-glory": "King of Glory", "warcraft-3": "Warcraft 3",
    "fifa": "FIFA", "efootball": "eFootball", "nba2k": "NBA 2K",
}
# Типы «спорта» платформы, которые не матчи двух соперников
_SKIP_SPORT_TYPES = {"polybet", "racing"}

# Латиница, неотличимая от кириллицы, прямо в русских подписях платформы:
# «1-й cет» пишется латинской «c». Складываем, иначе market_scope не
# опознает сет.
_HOMOGLYPHS = str.maketrans("acekmopxy", "асекморху")
_CYR_WORD_RE = re.compile(r"\b(?=[a-zA-Zа-яА-ЯёЁ]*[а-яА-ЯёЁ])[a-zA-Zа-яА-ЯёЁ]+\b")

# «Карта 1. Победитель» → «1-я карта. Победитель»: market_scope понимает
# номер периода только перед словом.
_MAP_N_RE = re.compile(r"(?i)\b(карта|сет|период|тайм|четверть|раунд|"
                       r"гейм|иннинг)\s+(\d+)\b")
# Слова вида рынка — не предмет; убираем перед market_scope
_MARKET_WORDS_RE = re.compile(
    r"(?i)победитель|победа в матче|результат основного времени|"
    r"результат матча|результат|исход|индивидуальный тотал|тотал|фора|"
    r"гандикап|обе команды забьют|обе забьют|нечет/чет|чет/нечет|"
    r"чёт/нечет|нечет/чёт|вкл\.\s*от|основное время")
# Рынки, которые НЕ сшиваются с остальными БК (см. докстринг модуля)
_SKIP_MARKET_RE = re.compile(
    r"(?i)досрочн|минут|двойной шанс|точн|гонка|первый до|race|игрок|"
    r"кто |какая команда|самый результативн|волевая|сухая|всухую|"
    r"выиграет оба|один из|в обоих|автогол|пенальти|карточка\?|"
    r"удаление|карточку|время |забьет|забьёт|хотя бы|угловой\b")


class OneWinParser(BaseParser):
    name = "1win"
    min_refresh = ONEWIN_MIN_REFRESH

    def _headers(self) -> dict:
        h = super()._headers()
        h["Origin"] = ONEWIN_SITE_HOST
        h["Referer"] = ONEWIN_SITE_HOST + "/"
        h["Content-Type"] = "application/json"
        return h

    # ---------- сбор линии ----------

    def fetch_odds(self) -> list[MarketOdds]:
        return self._collect(live=False)

    def fetch_live_odds(self) -> list[MarketOdds]:
        return self._collect(live=True)

    def _collect(self, live: bool) -> list[MarketOdds]:
        deadline = time.monotonic() + ONEWIN_FEED_TIMEOUT
        now = time.time()
        matches = self._matches("live" if live else "prematch")
        if not matches:
            return []
        wanted: dict[int, dict] = {}
        for m in matches:
            mid = m.get("id")
            sport = m.get("sport") or {}
            if not mid or sport.get("sportType") in _SKIP_SPORT_TYPES:
                continue
            start = m.get("startAt")
            if not live and (not start or float(start) <= now):
                continue   # только прематч
            wanted[int(mid)] = m
        log.info("1win: %s — %d событий в линии", "лайв" if live else
                 "прематч", len(wanted))
        if not wanted:
            return []

        # полная роспись — у ближайших событий, базовые рынки — у всех
        ordered = sorted(wanted, key=lambda i: wanted[i].get("startAt") or 0)
        full_ids = ordered[:ONEWIN_FULL_MARKETS_MAX]
        base_ids = ordered[len(full_ids):]
        snapshots = self._odds_snapshots(full_ids, base_ids, deadline)
        if not snapshots:
            self.status_note = ("websocket платформы не отдал ни одного "
                                "снимка котировок")
            return []

        kind = KIND_LIVE if live else KIND_PREMATCH
        by_key: dict[str, MarketOdds] = {}
        for mid, groups in snapshots.items():
            m = wanted.get(mid)
            if not m:
                continue
            for o in self._parse_match(m, groups, kind, now):
                by_key[o.match_key] = o
        odds = list(by_key.values())
        three = sum(1 for o in odds if o.market_key.startswith("winner1x2"))
        log.info("1win: %s — %d котировок на %d событий (снимков %d/%d), "
                 "из них исход 1X2: %d", "лайв" if live else "прематч",
                 len(odds), len({o.event_key for o in odds}),
                 len(snapshots), len(wanted), three)
        return odds

    # ---------- REST: список матчей ----------

    def _matches(self, service: str) -> list[dict]:
        url = f"{ONEWIN_API_HOST}/matches/get-many"
        body = {"limit": 5000, "service": service,
                "excludeSportType": sorted(_SKIP_SPORT_TYPES)}
        try:
            resp = self.session.post(
                url, params={"l": ONEWIN_LANG, "p": ONEWIN_PARTNER_ID},
                headers=self._headers(), json=body,
                timeout=max(HTTP_TIMEOUT, 30))
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("1win: список матчей (%s) не получен: %s",
                        service, exc)
            self.status_note = f"шлюз платформы не отдал список матчей: {exc}"
            return []
        items = (data.get("result") or {}).get("items")
        if not isinstance(items, list):
            errors = data.get("errors")
            log.warning("1win: неожиданный ответ списка матчей: %s",
                        errors or str(data)[:200])
            self.status_note = "шлюз платформы ответил без списка матчей"
            return []
        return items

    # ---------- websocket: снимки котировок ----------

    def _ws_url(self) -> str:
        sep = "&" if "?" in ONEWIN_WS_URL else "?"
        return (f"{ONEWIN_WS_URL}{sep}Language={ONEWIN_LANG}"
                f"&externalPartnerId={ONEWIN_PARTNER_ID}"
                f"&EIO=4&transport=websocket")

    def _odds_snapshots(self, full_ids: list[int], base_ids: list[int],
                        deadline: float) -> dict[int, dict[str, dict]]:
        """matchId → {groupId → группа рынков (oddsList — словарь по id)}.

        Одно соединение на обход; подписки уходят пачками, каждая ждётся
        до ONEWIN_WS_BATCH_WAIT секунд. Дельты (match-odds) применяются к
        уже полученным снимкам — за долгий обход кэфы ближайших матчей
        успевают сдвинуться."""
        snaps: dict[int, dict[str, dict]] = {}
        try:
            ws = websocket.create_connection(
                self._ws_url(), timeout=10, origin=ONEWIN_SITE_HOST,
                header=[f"User-Agent: {super()._headers()['User-Agent']}"])
        except Exception as exc:  # noqa: BLE001
            log.warning("1win: websocket платформы недоступен: %s", exc)
            self.status_note = f"websocket платформы недоступен: {exc}"
            return snaps
        try:
            # рукопожатие socket.io v4: 0{...} → 40 → 40{...}
            self._recv_until(ws, lambda m: m.startswith("0"), 10, snaps)
            ws.send("40")
            self._recv_until(ws, lambda m: m.startswith("40"), 10, snaps)

            plan = [(full_ids, False), (base_ids, True)]
            for ids, base in plan:
                for i in range(0, len(ids), ONEWIN_WS_BATCH):
                    if time.monotonic() > deadline:
                        log.info("1win: дедлайн обхода истёк, снимков %d",
                                 len(snaps))
                        return snaps
                    batch = ids[i:i + ONEWIN_WS_BATCH]
                    ws.send('42["subscribe",' + json.dumps({
                        "messageType": "subscribe-match-odds",
                        "data": {"matchIds": batch,
                                 "isBaseOddsGroups": base}}) + "]")
                    want = set(batch)
                    self._recv_until(
                        ws, lambda _m: want <= snaps.keys(),
                        min(ONEWIN_WS_BATCH_WAIT,
                            max(1.0, deadline - time.monotonic())), snaps)
                    missing = len(want - snaps.keys())
                    if missing:
                        log.debug("1win: пачка %d id — без снимка %d",
                                  len(batch), missing)
        except Exception as exc:  # noqa: BLE001
            log.warning("1win: обрыв websocket'а после %d снимков: %s",
                        len(snaps), exc)
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        return snaps

    def _recv_until(self, ws, done, wait: float,
                    snaps: dict[int, dict[str, dict]]) -> None:
        """Читает кадры, пока done(последний кадр) не станет истиной или не
        выйдет время; снимки и дельты складывает в snaps."""
        end = time.monotonic() + wait
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            ws.settimeout(min(left, 5.0))
            try:
                msg = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not isinstance(msg, str):
                continue
            if msg == "2":            # ping → pong
                ws.send("3")
                continue
            if msg.startswith("42"):
                self._apply_frame(msg, snaps)
            if done(msg):
                return

    @staticmethod
    def _apply_frame(msg: str, snaps: dict[int, dict[str, dict]]) -> None:
        try:
            payload = json.loads(msg[2:])
        except ValueError:
            return
        if not isinstance(payload, list) or len(payload) < 2 \
                or not isinstance(payload[1], dict):
            return
        body = payload[1]
        kind = body.get("messageType")
        data = body.get("data")
        if not isinstance(data, dict) or "matchId" not in data:
            return
        mid = data["matchId"]
        groups = data.get("oddsGroups") or []
        if kind == "match-odds-snapshot":
            snaps[mid] = {}
            for g in groups:
                gid = str(g.get("id"))
                odds = {str(o.get("id")): o for o in g.get("oddsList") or []
                        if o.get("id")}
                snaps[mid][gid] = {**g, "oddsList": odds}
        elif kind == "match-odds" and mid in snaps:
            for g in groups:
                cur = snaps[mid].get(str(g.get("id")))
                if not cur:
                    continue
                for o in g.get("oddsList") or []:
                    oid = str(o.get("id") or "")
                    if not oid:
                        continue
                    if oid in cur["oddsList"]:
                        cur["oddsList"][oid].update(o)
                    elif o.get("name"):
                        cur["oddsList"][oid] = o

    # ---------- разбор одного матча ----------

    def _parse_match(self, m: dict, groups: dict[str, dict], kind: str,
                     now: float) -> list[MarketOdds]:
        team1, team2 = self._teams(m, groups)
        if not team1 or not team2 or team1 == team2:
            return []
        start = m.get("startAt")
        start_ts = float(start) if start else None
        if kind == KIND_PREMATCH and (start_ts is None or start_ts <= now):
            return []
        base = dict(
            bookmaker=self.name, sport=self._sport(m), team1=team1,
            team2=team2, kind=kind,
            start_time=format_start(start_ts) if start_ts else None,
            start_ts=start_ts, url=self._event_url(m))

        # Одноимённые рынки «(вкл. ОТ)» дают тот же ключ, что и основные
        # (у остальных БК тотал/фора хоккея — основное время); основной
        # вариант идёт первым и остаётся, вариант с овертаймом отсеивается.
        out: list[MarketOdds] = []
        seen: set[str] = set()
        for g in sorted(groups.values(),
                        key=lambda x: ("вкл" in (x.get("name") or "").lower(),
                                       x.get("order") or 0)):
            for o in self._group(g, base):
                if o.market_key in seen:
                    continue
                seen.add(o.market_key)
                out.append(o)
        return out

    @staticmethod
    def _active(g: dict) -> dict[str, dict]:
        return {oid: o for oid, o in (g.get("oddsList") or {}).items()
                if o.get("status", 1) == 1 and o.get("cf")
                and float(o["cf"]) > 1.0}

    def _teams(self, m: dict, groups: dict[str, dict]) -> tuple[str, str]:
        """Русские имена команд — из исходов рынка результата (запасной
        вариант — английские из REST)."""
        best = None
        for g in groups.values():
            odds = list((g.get("oddsList") or {}).values())
            outs = g.get("outcomes") or {str(o.get("outcome")) for o in odds}
            if g.get("renderType") not in ("cols-2", "cols-3") \
                    or "1" not in outs or "2" not in outs:
                continue
            if _SKIP_MARKET_RE.search(g.get("name") or ""):
                continue
            if any(o.get("vars") for o in odds):
                continue
            rank = (0 if g.get("isBase") else 1, g.get("baseOrder", 99),
                    g.get("order") or 0)
            if best is None or rank < best[0]:
                best = (rank, odds)
        if best:
            t1 = next((o.get("name") for o in best[1]
                       if o.get("outcome") == "1"), None)
            t2 = next((o.get("name") for o in best[1]
                       if o.get("outcome") == "2"), None)
            if t1 and t2:
                return t1.strip(), t2.strip()
        home = (m.get("homeTeam") or {}).get("name") or ""
        away = (m.get("awayTeam") or {}).get("name") or ""
        if not (home and away):
            comps = sorted(m.get("competitors") or [],
                           key=lambda c: c.get("position") or 0)
            if len(comps) >= 2:
                home, away = comps[0].get("name") or "", comps[1].get("name") or ""
        return home.strip(), away.strip()

    @staticmethod
    def _sport(m: dict) -> str:
        sp = m.get("sport") or {}
        slug = (sp.get("slug") or m.get("sportTag") or "").lower()
        tour = (m.get("tournament") or {}).get("slug") or ""
        tour_name = tour.replace("-", " ").strip().title()
        if sp.get("isEsport") or sp.get("sportType") == "esport":
            game = _ESPORTS.get(slug) or slug.replace("-", " ").title()
            return f"Киберспорт · {game}. {tour_name}" if tour_name \
                else f"Киберспорт · {game}"
        root = _SPORTS.get(slug) or slug.replace("-", " ").capitalize() \
            or "Спорт"
        return f"{root} · {tour_name}" if tour_name else root

    @staticmethod
    def _event_url(m: dict) -> str | None:
        slug = m.get("slug")
        mid = m.get("id")
        if not slug or not mid:
            return f"{ONEWIN_SITE_HOST}/betting"
        sp = m.get("sport") or {}
        section = "esport" if sp.get("isEsport") else "sport"
        return f"{ONEWIN_SITE_HOST}/betting/match/{section}/{slug}-{mid}"

    # ---------- классификация группы рынков ----------

    @staticmethod
    def _fold(text: str) -> str:
        return _CYR_WORD_RE.sub(lambda w: w.group(0).translate(_HOMOGLYPHS),
                                text)

    def _scope(self, name: str, base: dict) -> str | None:
        """Предмет/период рынка; None — рынок сшивать нельзя."""
        text = self._fold(name)
        if _SKIP_MARKET_RE.search(text):
            return None
        text = re.sub(r"\([^)]*\)", " ", text)
        text = _MAP_N_RE.sub(lambda mm: f"{mm.group(2)}-й {mm.group(1)}", text)
        for team in (base["team1"], base["team2"]):
            if team:
                text = text.replace(team, " ")
        text = _MARKET_WORDS_RE.sub(" ", text)
        return market_scope(text)

    def _group(self, g: dict, base: dict) -> list[MarketOdds]:
        name = (g.get("name") or "").strip()
        if not name:
            return []
        scope = self._scope(name, base)
        if scope is None:
            return []
        odds = self._active(g)
        if len(odds) < 2:
            return []
        rt = g.get("renderType") or ""
        by_out: dict[str, list[dict]] = {}
        for o in odds.values():
            by_out.setdefault(str(o.get("outcome") or ""), []).append(o)

        plain = not any(o.get("vars") for o in odds.values())
        low = self._fold(name).lower().replace("ё", "е")

        # --- исход 1X2 / победитель ---
        if plain and rt in ("cols-2", "cols-3") and "1" in by_out \
                and "2" in by_out:
            k1 = float(by_out["1"][0]["cf"])
            k2 = float(by_out["2"][0]["cf"])
            if "x" in by_out:
                kx = float(by_out["x"][0]["cf"])
                if not sane_1x2_margin(k1, kx, k2):
                    return []
                return [MarketOdds(
                    market="Исход (1X2)" + (f" ({scope})" if scope else ""),
                    market_key=f"winner1x2:{scope}" if scope else "winner1x2",
                    outcome1="П1", outcome2="П2", outcome3="X",
                    k1=k1, k2=k2, k3=kx, **base)]
            if len(odds) != 2 or not sane_pair_margin(k1, k2):
                return []
            return [MarketOdds(
                market=name, market_key=f"winner:{scope}" if scope else "winner",
                outcome1="П1", outcome2="П2", k1=k1, k2=k2, **base)]

        # --- тотал (в т.ч. индивидуальный) ---
        if rt == "total-2" or ("over" in by_out and "under" in by_out):
            return self._totals(name, low, scope, by_out, base)

        # --- фора ---
        if rt == "fora-2" or (not plain and "1" in by_out and "2" in by_out):
            return self._handicaps(name, scope, by_out, base)

        # --- обе забьют ---
        if "yes" in by_out and "no" in by_out and len(odds) == 2:
            if "обе" not in low or "забьют" not in low:
                return []
            k1, k2 = float(by_out["yes"][0]["cf"]), float(by_out["no"][0]["cf"])
            if not sane_pair_margin(k1, k2):
                return []
            return [MarketOdds(
                market=name,
                market_key=f"bothscore:{scope}" if scope else "bothscore",
                outcome1="Да", outcome2="Нет", k1=k1, k2=k2, **base)]

        # --- чет/нечет (только общий рынок матча/периода) ---
        if "odd" in by_out and "even" in by_out and len(odds) == 2:
            if base["team1"] in name or base["team2"] in name:
                return []
            # порядок исходов — как у остальных парсеров: 1-й исход «Чет»
            k1, k2 = float(by_out["even"][0]["cf"]), float(by_out["odd"][0]["cf"])
            if not sane_pair_margin(k1, k2):
                return []
            return [MarketOdds(
                market=name,
                market_key=f"oddeven:{scope}" if scope else "oddeven",
                outcome1="Чет", outcome2="Нечет", k1=k1, k2=k2, **base)]
        return []

    def _totals(self, name: str, low: str, scope: str,
                by_out: dict[str, list[dict]], base: dict) -> list[MarketOdds]:
        side = None
        if "индивидуальн" in low or base["team1"] in name \
                or base["team2"] in name:
            if base["team1"] and base["team1"] in name:
                side = 1
            elif base["team2"] and base["team2"] in name:
                side = 2
            else:
                return []      # чей тотал — непонятно
        over = {self._line(o): o for o in by_out.get("over", [])}
        under = {self._line(o): o for o in by_out.get("under", [])}
        out: list[MarketOdds] = []
        for line, o in over.items():
            u = under.get(line)
            if line is None or u is None:
                continue
            k1, k2 = float(o["cf"]), float(u["cf"])
            if not sane_pair_margin(k1, k2):
                continue
            pt = fmt_total(line)
            if side:
                team = base["team1"] if side == 1 else base["team2"]
                out.append(MarketOdds(
                    market=f"Тотал {pt} ({team})",
                    market_key=f"itotal:{side}:{scope}:{pt}",
                    outcome1=f"ИТБ {pt}", outcome2=f"ИТМ {pt}",
                    k1=k1, k2=k2, **base))
            else:
                out.append(MarketOdds(
                    market=f"{name} {pt}",
                    market_key=f"total:{scope}:{pt}" if scope
                    else f"total:{pt}",
                    outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                    k1=k1, k2=k2, **base))
        return out

    def _handicaps(self, name: str, scope: str,
                   by_out: dict[str, list[dict]], base: dict) -> list[MarketOdds]:
        side1 = {self._line(o): o for o in by_out.get("1", [])}
        side2 = {self._line(o): o for o in by_out.get("2", [])}
        out: list[MarketOdds] = []
        for h, o in side1.items():
            if h is None:
                continue
            o2 = side2.get(-h) if h != 0 else side2.get(0.0)
            if o2 is None:
                continue
            k1, k2 = float(o["cf"]), float(o2["cf"])
            if not sane_pair_margin(k1, k2):
                continue
            h1, h2 = fmt_hcap(h), fmt_hcap(-h)
            out.append(MarketOdds(
                market=f"{name} {h1}", market_key=f"hcap:{scope}:{h1}",
                outcome1=f"Ф1 {h1}", outcome2=f"Ф2 {h2}",
                k1=k1, k2=k2, **base))
        return out

    @staticmethod
    def _line(o: dict) -> float | None:
        v = (o.get("vars") or {}).get("v1")
        if v is None:
            return None
        try:
            f = float(str(v).replace(",", "."))
        except (TypeError, ValueError):
            return None
        return 0.0 if f == 0 else f
