"""BetBoom — прематч-линия через прямой websocket-фид sporthub.

Раньше линию собирали через Selenium: браузер открывал каждый вид спорта,
прокликивал первые несколько лиг и прокручивал ленту. Так набиралось лишь
около сотни матчей, а цикл был долгим и хрупким. Сайт (Next.js/React) вообще
не отдаёт линию в HTML — события приходят по бинарному websocket-фиду.

Теперь мы подключаемся к тому же фиду напрямую (см. bb_feed.py) и забираем
ВСЮ прематч-линию: несколько тысяч матчей за ~10 секунд, без браузера. Так
BetBoom собирает столько же прематч-матчей, сколько Fonbet и Winline.

Дерево турниров отдаёт по матчу только топ-ставки (~7 штук: исход,
основная фора, основной тотал). Поэтому после обхода дерева мы дополнительно
подписываемся на КАЖДЫЙ матч (state_subscribe_matches) и получаем полную
роспись: сотни ставок — таймы/периоды, карты, тоталы и форы по всем линиям
и т.д. Это быстро: ~2700 матчей за несколько секунд, ~10 МБ на цикл.

Разбираем ВСЕ двухисходные рынки события:
- «Исход…» (П1/П2) — только если нет ничьей (X): рынки 1X2 пропускаем;
  варианты «Исход (с ОТ)», «1-й тайм: Исход» и т.п. тоже разбираем;
- любой «…Тотал…» (Больше/Меньше) по каждой линии — голов, карт, углов,
  сетов, геймов, таймов и т.п.; предмет/период берём из имени рынка;
- любая «…Фора…» (двухисходная, привязана к команде и знаку линии).
Индивидуальные (командные) тоталы/форы пропускаем (это не двухисходный
рынок всего матча в привычном виде). Предмет/период рынка нормализуется
общим `market_scope`, чтобы совпадать с тем же рынком у других БК.
"""
import logging
import time
from datetime import datetime, timezone

from ..config import (BETBOOM_FEED_TIMEOUT, BETBOOM_FULL_MARKETS,
                      BETBOOM_LIVE_FEED_TIMEOUT)
from ..models import KIND_LIVE, KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .bb_feed import (TREE_LIVE, TREE_PREMATCH, BBFeedClient, FeedError,
                      _decode, _one, _text)
from .html_utils import fmt_hcap, fmt_total, format_start, market_scope

log = logging.getLogger("parsers.betboom")

# Короткие подписи исходов в фиде (поле short_name ставки).
# «Х» встречается и латиницей, и кириллицей — принимаем обе.
_WIN_1, _WIN_2 = "П1", "П2"
_WIN_X = ("X", "Х")
_TOTAL_OVER, _TOTAL_UNDER = "Больше", "Меньше"

# Варианты ОСНОВНОГО рынка исхода (весь матч): сопоставляются с рынком
# «Победитель» других БК (у них победитель обычно учитывает ОТ).
_WINNER_BASE = {
    "исход", "исход матча", "исход (2 исхода)", "исход (с от)",
    "исход (с от и буллитами)", "исход (включая доп. иннинги)",
}

# Номера полей ModelsMatch.MatchInfo (bb.sport_ws.v1.models)
_MI_ID = 1
_MI_TYPE = 3          # 2 = обычный матч
_MI_START_DTTM = 13   # ISO-строка «2026-07-17T13:00:00.000Z» (UTC)
_MI_TEAMS = 16
# ModelsStake
_ST_SHORT_NAME = 6
_ST_ARGUMENT = 9      # линия тотала (double)
_ST_FACTOR = 10       # коэффициент (double)
_ST_MARKET_NAME = 14
_ST_GROUP = 18        # группа рынка: «Тотал», «Экспресс», «Интервалы»…

# Группы рынков, которые НЕЛЬЗЯ разбирать как обычные тоталы/форы/исходы:
# «Экспресс» — комбинированные ставки («1Х и Тотал больше»), «Интервалы» —
# отрезки матча («Тотал с 1-15 мин.», все сливались бы в один ключ).
_SKIP_GROUPS = {"экспресс", "интервалы"}

# Тип матча: 2 — прематч-матч, 1 — лайв-матч (в игре). Прочие типы
# (аутрайты/спецставки) отсеиваем. Двухисходность рынка и наличие двух
# команд дополнительно фильтруют неигровые события.
_MATCH_TYPES_NORMAL = (1, 2)


class BetBoomParser(BaseParser):
    name = "BetBoom"

    def __init__(self) -> None:
        super().__init__()
        self._feed_host: str | None = None

    def fetch_odds(self) -> list[MarketOdds]:
        return self._crawl(tree_type=TREE_PREMATCH, live=False,
                           timeout=BETBOOM_FEED_TIMEOUT)

    def fetch_live_odds(self) -> list[MarketOdds]:
        return self._crawl(tree_type=TREE_LIVE, live=True,
                           timeout=BETBOOM_LIVE_FEED_TIMEOUT)

    def _crawl(self, tree_type: int, live: bool,
               timeout: float) -> list[MarketOdds]:
        by_key: dict[str, MarketOdds] = {}
        now = time.time()
        deadline = time.monotonic() + timeout
        client = BBFeedClient(host=self._feed_host, overall_timeout=timeout,
                              tree_type=tree_type)
        try:
            # 1) обход дерева: все матчи, но только топ-ставки
            matches: dict[int, tuple[str, dict]] = {}
            for sport_name, match in client.crawl():
                if 1 not in match:
                    continue
                mid = _one(_decode(match[1][0]), 1)
                if mid:
                    matches[mid] = (sport_name, match)

            # 2) полная роспись каждого матча (все рынки, а не топ-7).
            # Если ответ по матчу не пришёл — остаются топ-ставки из дерева.
            if BETBOOM_FULL_MARKETS and matches:
                full_got = 0
                for mid, full in client.subscribe_matches(
                        list(matches), deadline):
                    sport_name = matches[mid][0]
                    matches[mid] = (sport_name, full)
                    full_got += 1
                log.info("BetBoom feed: полная роспись по %d/%d матчам",
                         full_got, len(matches))

            for sport_name, match in matches.values():
                for o in self._parse_match(sport_name, match, now, live):
                    by_key[o.match_key] = o
        except FeedError as exc:
            log.warning("BetBoom feed недоступен: %s", exc)
            return []
        except ImportError:
            log.warning("BetBoom: не установлен websocket-client "
                        "(pip install websocket-client) — 0 котировок")
            return []
        finally:
            self._feed_host = client.host
            client.close()
        return list(by_key.values())

    # ---- разбор одного матча ----

    def _parse_match(self, sport: str, match: dict, now: float,
                     live: bool = False) -> list[MarketOdds]:
        if 1 not in match:
            return []
        info = _decode(match[_MI_ID][0])
        if _one(info, _MI_TYPE) not in _MATCH_TYPES_NORMAL:
            return []  # аутрайты и спецставки — не двухисходные матчи

        team1, team2 = self._teams(info)
        if not team1 or not team2 or team1 == team2:
            return []

        start_ts = self._start_ts(info)
        if live:
            # лайв: матч уже идёт — не отбрасываем по времени старта
            kind = KIND_LIVE
            start_time = format_start(start_ts) if start_ts else None
        else:
            if start_ts is None or start_ts <= now:
                return []  # только прематч
            kind = KIND_PREMATCH
            start_time = format_start(start_ts)

        base = dict(bookmaker=self.name, sport=sport or "Спорт",
                    team1=team1, team2=team2, kind=kind,
                    start_time=start_time, start_ts=start_ts)

        # Группируем ставки по рынкам:
        #   winners[имя рынка] = {short: factor}          («Исход…»)
        #   totals[имя рынка][line] = {Больше/Меньше: factor}
        #   hcaps[имя рынка][line] = {team_side: factor}  (team_side: 1/2)
        winners: dict[str, dict[str, float]] = {}
        winner_names: dict[str, str] = {}
        totals: dict[str, dict[float, dict[str, float]]] = {}
        hcaps: dict[str, dict[float, dict[int, float]]] = {}
        n1, n2 = team1.strip().lower(), team2.strip().lower()

        for stake_raw in match.get(2, []):
            st = _decode(stake_raw)
            market = _text(st, _ST_MARKET_NAME)
            short = _text(st, _ST_SHORT_NAME)
            factor = _one(st, _ST_FACTOR)
            if not factor or factor <= 1:
                continue
            factor = float(factor)
            low = market.lower().replace("ё", "е")
            group = _text(st, _ST_GROUP).lower().replace("ё", "е")
            if group in _SKIP_GROUPS:
                continue  # экспрессы и интервалы — не двухисходные рынки
            if "ком." in low or "мин." in low or "результативн" in low:
                continue  # командные рынки и отрезки без явной группы
            if self._mentions_team(low, n1, n2):
                continue  # командный (индивидуальный) рынок — пропускаем

            if "исход" in low and (short in (_WIN_1, _WIN_2)
                                   or short in _WIN_X):
                key = "X" if short in _WIN_X else short
                winners.setdefault(low, {})[key] = factor
                winner_names[low] = market
            elif "тотал" in low and "индивид" not in low \
                    and short in (_TOTAL_OVER, _TOTAL_UNDER):
                line = _one(st, _ST_ARGUMENT)
                if line is None:
                    continue
                totals.setdefault(low, {}).setdefault(
                    float(line), {})[short] = factor
            elif "фора" in low and "индивид" not in low:
                line = _one(st, _ST_ARGUMENT)
                if line is None:
                    continue
                side = self._team_side(short, n1, n2)
                if side:
                    hcaps.setdefault(low, {}).setdefault(
                        float(line), {})[side] = factor

        result: list[MarketOdds] = []
        seen_keys: set[str] = set()

        # Победители: рынки «Исход…» без ничьей (X) — двухисходные.
        # Базовые варианты («Исход», «Исход (с ОТ)»…) → ключ winner;
        # росписи («1-й тайм: Исход») → winner:<scope>. При коллизии
        # приоритет у варианта, ближнего к «Победителю» других БК.
        for low in sorted(winners, key=self._winner_rank):
            sides = winners[low]
            if "X" in sides or _WIN_1 not in sides or _WIN_2 not in sides:
                continue
            scope = "" if low in _WINNER_BASE else market_scope(low)
            key = f"winner:{scope}" if scope else "winner"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            name = ("Победитель" if low in _WINNER_BASE
                    else f"Победитель ({winner_names[low]})")
            result.append(MarketOdds(
                market=name, market_key=key,
                outcome1="П1", outcome2="П2",
                k1=sides[_WIN_1], k2=sides[_WIN_2], **base))

        # Тоталы: по каждой линии, где есть и Больше, и Меньше
        for low, lines in totals.items():
            scope = market_scope(low)
            for line, sides in lines.items():
                over, under = sides.get(_TOTAL_OVER), sides.get(_TOTAL_UNDER)
                if not over or not under:
                    continue
                pt = fmt_total(line)
                pref = f"{scope} " if scope else ""
                key = f"total:{scope}:{pt}" if scope else f"total:{pt}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                result.append(MarketOdds(
                    market=f"Тотал {pref}{pt}", market_key=key,
                    outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                    k1=over, k2=under, **base))

        # Форы: пара «team1(+L)/team2(-L)» = один двухисходный рынок
        for low, lines in hcaps.items():
            scope = market_scope(low)
            for line, sides in lines.items():
                if 1 not in sides:
                    continue
                # линия team2 должна быть противоположной (-line)
                opp = lines.get(-line)
                if not opp or 2 not in opp:
                    continue
                h1 = fmt_hcap(line)
                h2 = fmt_hcap(-line)
                key = f"hcap:{scope}:{h1}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                pref = f"{scope} " if scope else ""
                result.append(MarketOdds(
                    market=f"Фора {pref}{h1}", market_key=key,
                    outcome1=f"Ф1 {h1}", outcome2=f"Ф2 {h2}",
                    k1=sides[1], k2=opp[2], **base))

        return result

    @staticmethod
    def _winner_rank(low: str) -> tuple[int, str]:
        """Порядок разбора рынков исхода: при коллизии ключей выигрывает
        вариант с ОТ/доп. иннингами (так считают победителя другие БК),
        затем простой «Исход», затем остальные."""
        if "(с от" in low or "иннинг" in low:
            rank = 0
        elif low in _WINNER_BASE:
            rank = 1
        else:
            rank = 2
        return (rank, low)

    @staticmethod
    def _mentions_team(low: str, n1: str, n2: str) -> bool:
        """Имя рынка содержит название команды? («Тотал Коритиба» —
        индивидуальный тотал, это не двухисходный рынок всего матча)."""
        for name in (n1, n2):
            frag = name.replace("ё", "е")[:8].strip()
            if len(frag) >= 4 and frag in low:
                return True
        return False

    @staticmethod
    def _team_side(short: str, n1: str, n2: str) -> int | None:
        """Определяет, чья это фора: 1 (team1), 2 (team2) или None."""
        s = short.strip().lower()
        if not s:
            return None
        if s == n1:
            return 1
        if s == n2:
            return 2
        # имя в ставке бывает усечено — сверяем по вхождению
        if s and (s in n1 or n1 in s) and not (s in n2 or n2 in s):
            return 1
        if s and (s in n2 or n2 in s) and not (s in n1 or n1 in s):
            return 2
        return None

    # ---- вспомогательное ----

    @staticmethod
    def _teams(info: dict) -> tuple[str, str]:
        if _MI_TEAMS not in info:
            return "", ""
        teams = _decode(info[_MI_TEAMS][0])
        home = _decode(teams[1][0]) if 1 in teams else {}
        away = _decode(teams[3][0]) if 3 in teams else {}
        return _text(home, 3), _text(away, 3)  # поле 3 = name

    @staticmethod
    def _start_ts(info: dict) -> float | None:
        iso = _text(info, _MI_START_DTTM)
        if not iso:
            return None
        try:
            # «2026-07-17T13:00:00.000Z» — время в UTC
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
