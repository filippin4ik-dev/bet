"""BetBoom — прематч-линия через прямой websocket-фид sporthub.

Раньше линию собирали через Selenium: браузер открывал каждый вид спорта,
прокликивал первые несколько лиг и прокручивал ленту. Так набиралось лишь
около сотни матчей, а цикл был долгим и хрупким. Сайт (Next.js/React) вообще
не отдаёт линию в HTML — события приходят по бинарному websocket-фиду.

Теперь мы подключаемся к тому же фиду напрямую (см. bb_feed.py) и забираем
ВСЮ прематч-линию: несколько тысяч матчей за ~10 секунд, без браузера. Так
BetBoom собирает столько же прематч-матчей, сколько Fonbet и Winline.

Разбираем ВСЕ двухисходные рынки события:
- «Исход» (П1/П2) — только если нет ничьей (X): рынки 1X2 пропускаем;
- любой «Тотал…» (Больше/Меньше) по каждой линии — голов, карт, углов,
  сетов, геймов и т.п.; предмет тотала берём из имени рынка;
- любая «Фора…» (двухисходная, привязана к команде и знаку линии).
Индивидуальные тоталы/форы пропускаем (это не двухисходный рынок всего
матча в привычном виде). Предмет/период рынка нормализуется общим
`market_scope`, чтобы совпадать с тем же рынком у других БК.
"""
import logging
import time
from datetime import datetime, timezone

from ..config import BETBOOM_FEED_TIMEOUT, BETBOOM_LIVE_FEED_TIMEOUT
from ..models import KIND_LIVE, KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .bb_feed import (TREE_LIVE, TREE_PREMATCH, BBFeedClient, FeedError,
                      _decode, _one, _text)
from .html_utils import fmt_hcap, fmt_total, format_start, market_scope

log = logging.getLogger("parsers.betboom")

# Короткие подписи исходов в фиде (поле short_name ставки)
_WIN_1, _WIN_2, _WIN_X = "П1", "П2", "X"
_TOTAL_OVER, _TOTAL_UNDER = "Больше", "Меньше"

# Имена рынков ВСЕГО матча (по префиксу): «Исход», «Тотал…», «Фора…».
_MARKET_WINNER = "Исход"

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
        client = BBFeedClient(host=self._feed_host, overall_timeout=timeout,
                              tree_type=tree_type)
        try:
            for sport_name, match in client.crawl():
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
        #   winner        — {short: factor}
        #   totals[scope][line] = {Больше/Меньше: factor}
        #   hcaps[scope][line] = {team_side: factor}   (team_side: 1/2)
        winner: dict[str, float] = {}
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
            low = market.lower()

            if market == _MARKET_WINNER and short in (_WIN_1, _WIN_2, _WIN_X):
                winner[short] = factor
            elif low.startswith("тотал") and "индивид" not in low \
                    and short in (_TOTAL_OVER, _TOTAL_UNDER):
                line = _one(st, _ST_ARGUMENT)
                if line is None:
                    continue
                scope = market_scope(market)
                totals.setdefault(scope, {}).setdefault(
                    float(line), {})[short] = factor
            elif low.startswith("фора") and "индивид" not in low:
                line = _one(st, _ST_ARGUMENT)
                if line is None:
                    continue
                side = self._team_side(short, n1, n2)
                if side:
                    scope = market_scope(market)
                    hcaps.setdefault(scope, {}).setdefault(
                        float(line), {})[side] = factor

        result: list[MarketOdds] = []

        # Победитель: только двухисходный (без ничьей X)
        if _WIN_X not in winner and _WIN_1 in winner and _WIN_2 in winner:
            result.append(MarketOdds(
                market="Победитель", market_key="winner",
                outcome1="П1", outcome2="П2",
                k1=winner[_WIN_1], k2=winner[_WIN_2], **base))

        # Тоталы: по каждой линии, где есть и Больше, и Меньше
        for scope, lines in totals.items():
            for line, sides in lines.items():
                over, under = sides.get(_TOTAL_OVER), sides.get(_TOTAL_UNDER)
                if not over or not under:
                    continue
                pt = fmt_total(line)
                pref = f"{scope} " if scope else ""
                key = f"total:{scope}:{pt}" if scope else f"total:{pt}"
                result.append(MarketOdds(
                    market=f"Тотал {pref}{pt}", market_key=key,
                    outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                    k1=over, k2=under, **base))

        # Форы: пара «team1(+L)/team2(-L)» = один двухисходный рынок
        for scope, lines in hcaps.items():
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
                pref = f"{scope} " if scope else ""
                result.append(MarketOdds(
                    market=f"Фора {pref}{h1}", market_key=key,
                    outcome1=f"Ф1 {h1}", outcome2=f"Ф2 {h2}",
                    k1=sides[1], k2=opp[2], **base))

        return result

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
