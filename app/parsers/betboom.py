"""BetBoom — прематч-линия через прямой websocket-фид sporthub.

Раньше линию собирали через Selenium: браузер открывал каждый вид спорта,
прокликивал первые несколько лиг и прокручивал ленту. Так набиралось лишь
около сотни матчей, а цикл был долгим и хрупким. Сайт (Next.js/React) вообще
не отдаёт линию в HTML — события приходят по бинарному websocket-фиду.

Теперь мы подключаемся к тому же фиду напрямую (см. bb_feed.py) и забираем
ВСЮ прематч-линию: несколько тысяч матчей за ~10 секунд, без браузера. Так
BetBoom собирает столько же прематч-матчей, сколько Fonbet и Winline.

Разбираем два двухисходных рынка ВСЕГО матча:
- «Исход» (П1/П2) — только если нет ничьей (X): рынки 1X2 пропускаем;
- «Тотал» (Больше/Меньше) по каждой линии (аргумент ставки).
Дочерние росписи (сеты/тайму/карты — отдельные названия рынков) не берём:
их нельзя сопоставлять с рынками всего матча у других БК.
"""
import logging
import time
from datetime import datetime, timezone

from ..config import BETBOOM_FEED_TIMEOUT
from ..models import KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .bb_feed import BBFeedClient, FeedError, _decode, _one, _text
from .html_utils import format_start

log = logging.getLogger("parsers.betboom")

# Короткие подписи исходов в фиде (поле short_name ставки)
_WIN_1, _WIN_2, _WIN_X = "П1", "П2", "X"
_TOTAL_OVER, _TOTAL_UNDER = "Больше", "Меньше"

# Полное имя рынка ВСЕГО матча (дочерние росписи — другие имена, напр.
# «Исход (с ОТ)», «Исход матча», «Тотал карт» — их не берём).
_MARKET_WINNER = "Исход"
_MARKET_TOTAL = "Тотал"

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

# Обычный матч (не аутрайт/спецставка)
_MATCH_TYPE_NORMAL = 2


class BetBoomParser(BaseParser):
    name = "BetBoom"

    def __init__(self) -> None:
        super().__init__()
        self._feed_host: str | None = None

    def fetch_odds(self) -> list[MarketOdds]:
        by_key: dict[str, MarketOdds] = {}
        now = time.time()
        client = BBFeedClient(host=self._feed_host,
                              overall_timeout=BETBOOM_FEED_TIMEOUT)
        try:
            for sport_name, match in client.crawl():
                for o in self._parse_match(sport_name, match, now):
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

    def _parse_match(self, sport: str, match: dict,
                     now: float) -> list[MarketOdds]:
        if 1 not in match:
            return []
        info = _decode(match[_MI_ID][0])
        if _one(info, _MI_TYPE) != _MATCH_TYPE_NORMAL:
            return []  # аутрайты и спецставки — не двухисходные матчи

        team1, team2 = self._teams(info)
        if not team1 or not team2 or team1 == team2:
            return []

        start_ts = self._start_ts(info)
        if start_ts is None or start_ts <= now:
            return []  # только прематч
        start_time = format_start(start_ts)

        base = dict(bookmaker=self.name, sport=sport or "Спорт",
                    team1=team1, team2=team2, kind=KIND_PREMATCH,
                    start_time=start_time, start_ts=start_ts)

        # Группируем ставки нужных рынков всего матча
        winner: dict[str, float] = {}
        totals: dict[float, dict[str, float]] = {}
        for stake_raw in match.get(2, []):
            st = _decode(stake_raw)
            market = _text(st, _ST_MARKET_NAME)
            short = _text(st, _ST_SHORT_NAME)
            factor = _one(st, _ST_FACTOR)
            if not factor or factor <= 1:
                continue
            if market == _MARKET_WINNER and short in (_WIN_1, _WIN_2, _WIN_X):
                winner[short] = float(factor)
            elif market == _MARKET_TOTAL and short in (_TOTAL_OVER,
                                                       _TOTAL_UNDER):
                line = _one(st, _ST_ARGUMENT)
                if line is None:
                    continue
                totals.setdefault(float(line), {})[short] = float(factor)

        result: list[MarketOdds] = []

        # Победитель: только двухисходный (без ничьей X)
        if _WIN_X not in winner and _WIN_1 in winner and _WIN_2 in winner:
            result.append(MarketOdds(
                market="Победитель", market_key="winner",
                outcome1="П1", outcome2="П2",
                k1=winner[_WIN_1], k2=winner[_WIN_2], **base))

        # Тоталы: по каждой линии, где есть и Больше, и Меньше
        for line, sides in totals.items():
            over, under = sides.get(_TOTAL_OVER), sides.get(_TOTAL_UNDER)
            if not over or not under:
                continue
            pt = self._fmt_line(line)
            result.append(MarketOdds(
                market=f"Тотал {pt}", market_key=f"total:{pt}",
                outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                k1=over, k2=under, **base))

        return result

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

    @staticmethod
    def _fmt_line(line: float) -> str:
        # «2.5» / «2» — без хвостовых нулей, чтобы совпадать с ключами
        # тоталов других БК (Fonbet/Winline)
        if line == int(line):
            return str(int(line))
        return ("%f" % line).rstrip("0").rstrip(".")
