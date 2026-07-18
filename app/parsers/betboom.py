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
- любая «…Фора…» (двухисходная, привязана к команде и знаку линии);
- «Обе забьют» Да/Нет (без комбинированных вариантов «…и тотал/исход»).
Индивидуальные (командные) тоталы/форы пропускаем (это не двухисходный
рынок всего матча в привычном виде). Предмет/период рынка нормализуется
общим `market_scope`, чтобы совпадать с тем же рынком у других БК.
"""
import logging
import re
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
_BTS_YES, _BTS_NO = "Да", "Нет"

# Варианты ОСНОВНОГО рынка исхода (весь матч): сопоставляются с рынком
# «Победитель» других БК (у них победитель обычно учитывает ОТ).
_WINNER_BASE = {
    "исход", "исход матча", "исход (2 исхода)", "исход (с от)",
    "исход (с от и буллитами)", "исход (включая доп. иннинги)",
}

# Номера полей ModelsMatch.MatchInfo (bb.sport_ws.v1.models)
_MI_ID = 1
_MI_TYPE = 3          # 2 = обычный матч
_MI_CATEGORY = 9      # id категории (страны/раздела) — для ссылки на матч
_MI_TOURNAMENT = 10   # id турнира — для ссылки на матч
_MI_START_DTTM = 13   # ISO-строка «2026-07-17T13:00:00.000Z» (UTC)
_MI_TEAMS = 16
# ModelsStake
_ST_SHORT_NAME = 6
_ST_ARGUMENT = 9      # линия тотала (double)
_ST_FACTOR = 10       # коэффициент (double)
_ST_MARKET_NAME = 14
_ST_GROUP = 18        # группа рынка: «Тотал», «Экспресс», «Интервалы»…
_ST_PERIOD = 20       # период рынка: «Основное время», «1-й тайм», «Карта 1»

# Группы рынков, которые НЕЛЬЗЯ разбирать как обычные тоталы/форы/исходы:
# «Экспресс» — комбинированные ставки («1Х и Тотал больше»), «Интервалы» —
# отрезки матча («Тотал с 1-15 мин.»), «Игроки» — статистика игроков,
# «Счет» — точные счета, «Турнир» — долгосрочные ставки.
_SKIP_GROUPS = {"экспресс", "интервалы", "игроки", "счет", "турнир", "итоги"}

# Периоды, означающие ВЕСЬ матч (не добавляют scope рынку)
_MAIN_PERIODS = {"", "основное время", "матч", "бой", "игра", "игроки"}

# «Карта 1» → «1 карта»: market_scope ждёт номер ПЕРЕД словом периода
_PERIOD_NUM_LAST = re.compile(r"^(карта|сет|период|тайм)\s+(\d+)$")

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
            matches: dict[int, tuple[str, str, dict]] = {}
            for sport_name, sport_slug, match in client.crawl():
                if 1 not in match:
                    continue
                mid = _one(_decode(match[1][0]), 1)
                if mid:
                    matches[mid] = (sport_name, sport_slug, match)

            # 2) полная роспись каждого матча (все рынки, а не топ-7).
            # Если ответ по матчу не пришёл — остаются топ-ставки из дерева.
            if BETBOOM_FULL_MARKETS and matches:
                full_got = 0
                for mid, full in client.subscribe_matches(
                        list(matches), deadline):
                    sport_name, sport_slug, _ = matches[mid]
                    matches[mid] = (sport_name, sport_slug, full)
                    full_got += 1
                log.info("BetBoom feed: полная роспись по %d/%d матчам",
                         full_got, len(matches))

            for sport_name, sport_slug, match in matches.values():
                for o in self._parse_match(sport_name, match, now, live,
                                           sport_slug):
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
                     live: bool = False,
                     sport_slug: str = "") -> list[MarketOdds]:
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
                    start_time=start_time, start_ts=start_ts,
                    url=self._event_url(sport_slug, info, live))

        # Группируем ставки по рынкам. Ключ mk = (период, имя рынка):
        # у BetBoom период часто НЕ входит в имя («Исход» с периодом
        # «Карта 1» в киберспорте), без него рынки разных карт слились бы.
        #   winners[mk] = {short: factor}                 («Исход…»)
        #   totals[mk][line] = {Больше/Меньше: factor}
        #   hcaps[mk][line] = {team_side: factor}         (team_side: 1/2)
        winners: dict[tuple, dict[str, float]] = {}
        winner_names: dict[tuple, str] = {}
        totals: dict[tuple, dict[float, dict[str, float]]] = {}
        hcaps: dict[tuple, dict[float, dict[int, float]]] = {}
        both_score: dict[tuple, dict[str, float]] = {}
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
            group = _text(st, _ST_GROUP).lower().replace("ё", "е").strip()
            if group in _SKIP_GROUPS:
                continue  # экспрессы, интервалы, игроки… — не наши рынки
            if "ком." in low or "мин." in low or "результативн" in low:
                continue  # командные рынки и отрезки без явной группы
            if self._mentions_team(low, n1, n2):
                continue  # командный (индивидуальный) рынок — пропускаем
            period = self._period(st)
            mk = (period, low)

            if "исход" in low and (short in (_WIN_1, _WIN_2)
                                   or short in _WIN_X):
                key = "X" if short in _WIN_X else short
                winners.setdefault(mk, {})[key] = factor
                winner_names[mk] = market
            elif "тотал" in low and "индивид" not in low \
                    and short in (_TOTAL_OVER, _TOTAL_UNDER):
                line = _one(st, _ST_ARGUMENT)
                if line is None:
                    continue
                totals.setdefault(mk, {}).setdefault(
                    float(line), {})[short] = factor
            elif "фора" in low and "индивид" not in low:
                line = _one(st, _ST_ARGUMENT)
                if line is None:
                    continue
                side = self._team_side(short, n1, n2)
                if side:
                    hcaps.setdefault(mk, {}).setdefault(
                        float(line), {})[side] = factor
            elif "обе" in low and "забьют" in low \
                    and short in (_BTS_YES, _BTS_NO) \
                    and "тотал" not in low and "исход" not in low \
                    and " и " not in low:
                # чистый рынок «Обе забьют» Да/Нет (комбинированные
                # варианты «Обе забьют и тотал/исход» — не он)
                both_score.setdefault(mk, {})[short] = factor

        result: list[MarketOdds] = []
        seen_keys: set[str] = set()

        # Победители: рынки «Исход…» без ничьей (X) — двухисходные.
        # Базовые варианты («Исход», «Исход (с ОТ)»…) → ключ winner;
        # росписи («1-й тайм: Исход», «Карта 1» + «Исход») →
        # winner:<scope>. При коллизии приоритет у варианта, ближнего
        # к «Победителю» других БК.
        for mk in sorted(winners, key=self._winner_rank):
            period, low = mk
            sides = winners[mk]
            if "X" in sides or _WIN_1 not in sides or _WIN_2 not in sides:
                continue
            if not period and low in _WINNER_BASE:
                scope = ""
            else:
                scope = market_scope(f"{period} {low}".strip())
            key = f"winner:{scope}" if scope else "winner"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if not scope:
                name = "Победитель"
            else:
                pref = f"{period}: " if period else ""
                name = f"Победитель ({pref}{winner_names[mk]})"
            result.append(MarketOdds(
                market=name, market_key=key,
                outcome1="П1", outcome2="П2",
                k1=sides[_WIN_1], k2=sides[_WIN_2], **base))

        # Тоталы: по каждой линии, где есть и Больше, и Меньше
        for (period, low), lines in totals.items():
            scope = market_scope(f"{period} {low}".strip())
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

        # «Обе забьют»: Да/Нет — двухисходный рынок. Основной вариант
        # (весь матч) → ключ bothscore, периодные — bothscore:<scope>.
        for (period, low), sides in both_score.items():
            yes, no = sides.get(_BTS_YES), sides.get(_BTS_NO)
            if not yes or not no:
                continue
            scope = market_scope(f"{period} {low}".strip())
            key = f"bothscore:{scope}" if scope else "bothscore"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            name = f"Обе забьют ({period})" if period else "Обе забьют"
            result.append(MarketOdds(
                market=name, market_key=key,
                outcome1="Да", outcome2="Нет",
                k1=yes, k2=no, **base))

        # Форы: пара «team1(+L)/team2(-L)» = один двухисходный рынок
        for (period, low), lines in hcaps.items():
            scope = market_scope(f"{period} {low}".strip())
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
    def _event_url(slug: str, info: dict, live: bool) -> str | None:
        """Ссылка на страницу матча: /sport/<slug>/<категория>/<турнир>/<id>
        (роут снят с бандла sport/[[...all]]: DETAILED_EVENT). Для
        киберспорта дисциплинарного slug'а в фиде нет — ведём в раздел."""
        mid = _one(info, _MI_ID)
        if not mid:
            return None
        if slug == "esports":
            return "https://betboom.ru/esport"
        if not slug:
            return "https://betboom.ru/sport/live" if live \
                else "https://betboom.ru/sport"
        cat = _one(info, _MI_CATEGORY)
        tour = _one(info, _MI_TOURNAMENT)
        if not cat or not tour:
            return f"https://betboom.ru/sport/{slug}"
        return f"https://betboom.ru/sport/{slug}/{cat}/{tour}/{mid}"

    @staticmethod
    def _period(st: dict) -> str:
        """Нормализованный период ставки («1 карта», «1-й тайм») или ""
        для всего матча («Основное время», «Матч», «Бой»…)."""
        period = _text(st, _ST_PERIOD).lower().replace("ё", "е").strip()
        period = " ".join(period.split())  # встречается хвостовой \t
        if period in _MAIN_PERIODS:
            return ""
        # «Карта 1» → «1 карта»: market_scope ждёт номер перед словом
        m = _PERIOD_NUM_LAST.match(period)
        if m:
            period = f"{m.group(2)} {m.group(1)}"
        return period

    @staticmethod
    def _winner_rank(mk: tuple) -> tuple[int, str, str]:
        """Порядок разбора рынков исхода: при коллизии ключей выигрывает
        вариант с ОТ/доп. иннингами (так считают победителя другие БК),
        затем простой «Исход», затем остальные."""
        period, low = mk
        if "(с от" in low or "иннинг" in low:
            rank = 0
        elif low in _WINNER_BASE:
            rank = 1
        else:
            rank = 2
        return (rank, period, low)

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
