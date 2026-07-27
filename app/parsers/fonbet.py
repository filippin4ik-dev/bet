"""Fonbet — парсинг публичного JSON-API линии (только прематч).

Домен сервера линии Fonbet периодически меняется (line01w..lineNNw на разных
resource-доменах), поэтому парсер перебирает список кандидатов и запоминает
первый рабочий. Можно жёстко задать хост через переменную окружения
FONBET_LINE_HOST (см. app/config.py и README).

Проверяются ВСЕ прематч-события (включая дочерние росписи) и все известные
двухисходные рынки, а также рынок «Исход 1X2» (см. ниже):
- Победитель (П1 id=921 / П2 id=923), если НЕТ ничьей (id=922);
- Исход 1X2 (П1/X/П2, id=921/922/923) — если ничья ЕСТЬ: это самый частый
  рынок футбола/хоккея, трёхисходные вилки по нему ищутся отдельным
  движком (arbitrage.find_arbs_1x2), см. README;
- Тоталы больше/меньше: основная линия (930/931) и вся лестница
  дополнительных линий (см. F_TOTAL_PAIRS), включая тотал очков в
  настольном теннисе/теннисе/волейболе (1848/1849);
- Форы: основная (927/928), азиатская (910/912) и лестница доп. линий
  (см. F_HANDICAPS), включая фору по очкам ракеточных видов (1845/1846);
- «Обе забьют» Да/Нет (4241/4242);
- Индивидуальные тоталы обеих команд — вся лестница линий (см. F_ITOTAL_1 /
  F_ITOTAL_2, сверено с рынком «Тотал <команда>» BetBoom);
- Рынки по сетам в теннисе и других сет-видах (тотал сетов 917/918, форы
  по сетам 2421/2422 и 2424/2425) — scope «sets», отдельно от геймов.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from .html_utils import neg_hcap as _neg_hcap
from ..config import BK_TZ_OFFSET, FONBET_LINE_HOST
from ..models import KIND_LIVE, KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import fmt_hcap, fmt_total, market_scope, sane_1x2_margin

log = logging.getLogger("parsers.fonbet")

# Resource-домены, на которых Fonbet раздаёт JSON линии (меняются со временем)
RESOURCE_DOMAINS = [
    "bk6bba-resources.com",
    "bkfon-resources.com",
    "ccf4ab51771cacd46d.com",
]
# Номера серверов линии, которые пробуем на каждом домене
LINE_NUMBERS = ["01", "02", "03", "04", "05", "08", "20", "32", "44", "52"]
# Шаблоны имени хоста: сейчас рабочие хосты вида line01w.<домен>,
# старый формат line01.<домен> оставлен как запасной.
HOST_TEMPLATES = ["line{num}w.{domain}", "line{num}.{domain}"]
# Пути JSON-API (list — компактный, listBase — расширенный)
PATHS = ["events/list", "events/listBase"]

# Если ни один хост не ответил — не долбим все кандидаты каждый цикл,
# а повторяем полный перебор не чаще, чем раз в N секунд.
PROBE_BACKOFF = 300

F_P1, F_DRAW, F_P2 = 921, 922, 923
# Пары тотала (ТБ, ТМ): основной тотал (930/931) и лестница дополнительных
# линий. Все пары проверены на живой линии: pt пары совпадает, кэфы
# зеркальны, лестница монотонна, а вероятности сходятся с тем же рынком
# Winline на той же линии (расхождение в среднем ~2%). 1848/1849 — тотал
# очков в настольном теннисе/теннисе/волейболе/бадминтоне (там основной
# тотал 930/931 не отдаётся вовсе). Каждая линия — отдельный рынок.
F_TOTAL_PAIRS = [(930, 931), (1696, 1697), (1727, 1728),
                 (1730, 1731), (1733, 1734), (1736, 1737),
                 (1739, 1791), (1793, 1794), (1796, 1797),
                 (1799, 1800), (1802, 1803), (1805, 1806),
                 (1848, 1849)]
# Пары форы (Ф1, Ф2): каждая пара — отдельная линия форы. pt у Ф1 — линия
# team1, у Ф2 — линия team2 (противоположная). Основная азиатская фора
# (910/912), фора «ноль» (927/928), лестница доп. фор (989/991, 1569/1572,
# 1672/1675, 1677/1678, 1680/1681, 1683/1684, 1689/1690) и фора по очкам
# в настольном теннисе/теннисе/волейболе (1845/1846). Все пары сверены
# с рынком Winline на тех же линиях; валидность дополнительно проверяется
# по противоположности линий (см. _handicaps).
F_HANDICAPS = [(910, 912), (927, 928), (989, 991), (1569, 1572),
               (1672, 1675), (1677, 1678), (1680, 1681), (1683, 1684),
               (1689, 1690), (1845, 1846)]
# «Обе забьют»: Да (4241) / Нет (4242) — двухисходный рынок (футбол).
# Порядок проверен сверкой кэфов с тем же рынком Winline.
F_BTS_YES, F_BTS_NO = 4241, 4242
# Индивидуальные тоталы (тотал ОДНОЙ команды, ТБ/ТМ) — лестницы линий.
# Сторона проверена сверкой с рынком «Тотал <команда>» BetBoom на живых
# матчах: F_ITOTAL_1 — команда 1 (хозяева), F_ITOTAL_2 — команда 2 (гости);
# первый id пары — «Больше». Лестницы монотонны, маржа пар 1.02–1.25 на
# всей линии (проверено по ~2000 лестниц).
F_ITOTAL_1 = [(1809, 1810), (1812, 1813), (1815, 1816), (1818, 1819),
              (1821, 1822), (1824, 1825), (1827, 1828)]
F_ITOTAL_2 = [(1854, 1871), (1873, 1874), (1880, 1881), (1883, 1884),
              (1886, 1887)]

# ---- Рынки ПО СЕТАМ (ракеточные виды: теннис/наст. теннис/волейбол и т.п.) ----
# В теннисе Fonbet отдаёт ОТДЕЛЬНО рынки «по геймам» (основные тоталы/форы
# выше — счёт в геймах, линии ~20 и форы ~2.5-6.5) и рынки «по сетам»
# (счёт в выигранных сетах). Их нельзя смешивать: «фора −1.5 по сетам» и
# «фора −1.5 по геймам» — разные рынки. Сет-рынки помечаем scope="sets",
# чтобы они сшивались с «Фора/Тотал по сетам» BetBoom, а не с геймами.
# Тотал сетов (917 ТБ / 918 ТМ, линия 2.5/3.5) и форы по сетам, линия ±1.5.
# ВАЖНО: пары фор ориентированы ПО-РАЗНОМУ (сверено с ориентированным
# рынком «Фора по сетам» BetBoom на живых матчах):
#   (2424, 2425) — 2424 это Ф1 (pt — линия team1), 2425 — Ф2;
#   (2421, 2422) — наоборот: 2421 это Ф2 (pt — линия team2), 2422 — Ф1.
# В списке каждая пара записана как (фактор Ф1, фактор Ф2).
F_SET_TOTALS = [(917, 918)]
F_SET_HANDICAPS = [(2422, 2421), (2424, 2425)]
# Виды спорта, где счёт идёт по сетам/партиям (для них включаем сет-рынки).
_SET_SPORTS = {"теннис", "настольный теннис", "волейбол", "бадминтон",
               "пляжный волейбол", "падел", "сквош", "падел-теннис"}


def _candidate_urls() -> list[str]:
    query = "?lang=ru&scopeMarket=1600"
    urls: list[str] = []
    if FONBET_LINE_HOST:
        # Явно заданный хост — пробуем в первую очередь
        for path in PATHS:
            urls.append(f"https://{FONBET_LINE_HOST}/{path}{query}")
    for tmpl in HOST_TEMPLATES:
        for domain in RESOURCE_DOMAINS:
            for num in LINE_NUMBERS:
                host = tmpl.format(num=num, domain=domain)
                for path in PATHS:
                    urls.append(f"https://{host}/{path}{query}")
    return urls


def _discover_hosts(session) -> list[str]:
    """Пытается вытащить актуальный домен сервера линии прямо со страниц
    fon.bet (домены ротируются, поэтому список кандидатов быстро устаревает).
    Работает только с доступом к сайту (российский IP)."""
    import re
    hosts: list[str] = []
    pattern = re.compile(r"https?://(line[\w.-]*\.[\w.-]+)")
    for page in ("https://www.fon.bet/", "https://fon.bet/",
                 "https://www.fon.bet/live/", "https://www.fon.bet/sports/"):
        try:
            html = session.get(page, timeout=8).text
        except Exception:  # noqa: BLE001
            continue
        for host in pattern.findall(html):
            if host not in hosts:
                hosts.append(host)
    return hosts


class FonbetParser(BaseParser):
    name = "Fonbet"

    def __init__(self) -> None:
        super().__init__()
        self._working_url: str | None = None
        self._last_full_probe = 0.0

    def _fetch_data(self) -> dict | None:
        # Рабочий URL известен — используем его, при сбое перебираем заново.
        if self._working_url:
            try:
                data = self.get_json(self._working_url)
                if isinstance(data, dict) and data.get("events"):
                    return data
            except Exception as exc:  # noqa: BLE001
                log.info("Fonbet: рабочий домен %s перестал отвечать (%s) — "
                         "ищу новый", self._working_url, exc)
            self._working_url = None

        # Полный перебор кандидатов — не чаще, чем раз в PROBE_BACKOFF сек
        now = time.monotonic()
        if now - self._last_full_probe < PROBE_BACKOFF:
            return None
        self._last_full_probe = now

        urls = _candidate_urls()
        for host in _discover_hosts(self.session):
            for path in PATHS:
                u = f"https://{host}/{path}?lang=ru&scopeMarket=1600"
                if u not in urls:
                    urls.append(u)
        for url in urls:
            try:
                data = self.get_json(url, timeout=6)
                if isinstance(data, dict) and data.get("events"):
                    log.info("Fonbet: рабочий домен линии — %s", url)
                    self._working_url = url
                    return data
            except Exception as exc:  # noqa: BLE001 — пробуем следующий
                log.debug("Fonbet: %s не подошёл (%s)", url, exc)
                continue
        log.warning("Fonbet: ни один домен линии не ответил валидным JSON "
                    "(повторный поиск через %d с). Можно указать актуальный "
                    "хост в FONBET_LINE_HOST (см. README).", PROBE_BACKOFF)
        return None

    def fetch_odds(self) -> list[MarketOdds]:
        return self._collect(live=False)

    def fetch_live_odds(self) -> list[MarketOdds]:
        return self._collect(live=True)

    def _collect(self, live: bool) -> list[MarketOdds]:
        self._delay()
        data = self._fetch_data()
        if not data:
            return []
        want_place = "live" if live else "line"
        kind = KIND_LIVE if live else KIND_PREMATCH

        # У Fonbet «спорт» события — это лига/сегмент (напр. «США. MLS»),
        # у сегмента есть parentId до корневого вида спорта («Футбол»).
        # Показываем «Вид спорта · Лига», чтобы работал фильтр по спорту.
        raw_sports = {s["id"]: s for s in data.get("sports", [])}

        def sport_root(sport_id) -> dict | None:
            seg = raw_sports.get(sport_id)
            if not seg:
                return None
            root, hops = seg, 0
            while root.get("parentId") in raw_sports and hops < 5:
                root = raw_sports[root["parentId"]]
                hops += 1
            return root

        def sport_name(sport_id) -> str:
            seg = raw_sports.get(sport_id)
            if not seg:
                return "Спорт"
            root = sport_root(sport_id) or seg
            root_name = root.get("name", "Спорт")
            seg_name = seg.get("name", "")
            if seg_name and seg_name != root_name:
                return f"{root_name} · {seg_name}"
            return root_name

        def event_url(sport_id, event_id) -> str:
            # Страница события: fon.bet/sports/<alias>/<segmentId>/<eventId>
            # (роутер SPA ориентируется на числовой id события в конце)
            root = sport_root(sport_id)
            alias = (root or {}).get("alias") or "football"
            return f"https://fon.bet/sports/{alias}/{sport_id}/{event_id}"

        events = {e["id"]: e for e in data.get("events", [])}

        result: list[MarketOdds] = []
        for ef in data.get("customFactors", []):
            event = events.get(ef.get("e"))
            if not event:
                continue

            root = event
            hops = 0
            while (not root.get("team1") or not root.get("team2")) \
                    and root.get("parentId") in events and hops < 5:
                root = events[root["parentId"]]
                hops += 1
            team1, team2 = root.get("team1"), root.get("team2")
            if not team1 or not team2:
                continue

            # Берём только запрошенный тип: прематч (place=='line') или
            # лайв (place=='live'); неактивные ('notActive') всегда мимо.
            if event.get("place") != want_place \
                    or root.get("place") != want_place:
                continue

            sport = sport_name(root.get("sportId"))
            market_name = event.get("name") if event is not root else None
            if market_name:
                sport = f"{sport} · {market_name}"
            # Дочерняя роспись (сет/период/угловые/карты) — отдельный рынок:
            # приводим предмет/период к канону, чтобы совпадал с тем же
            # рынком других БК и НЕ смешивался с рынком всего матча.
            scope = market_scope(market_name)

            start_ts = root.get("startTime")
            start_time = None
            if start_ts:
                # Показываем время в часовом поясе БК (МСК), а не сервера
                tz = timezone(timedelta(hours=BK_TZ_OFFSET))
                start_time = datetime.fromtimestamp(
                    start_ts, tz).strftime("%d.%m %H:%M")

            base = dict(bookmaker=self.name, sport=sport,
                        team1=team1, team2=team2,
                        kind=kind, start_time=start_time,
                        start_ts=float(start_ts) if start_ts else None,
                        url=event_url(root.get("sportId"), root["id"]))

            factors = ef.get("factors", [])
            result.extend(self._winner(factors, base, scope))
            result.extend(self._totals(factors, base, scope))
            result.extend(self._handicaps(factors, base, scope))
            result.extend(self._both_score(factors, base, scope))
            result.extend(self._ind_totals(factors, base, scope))
            # Рынки «по сетам» — только для ракеточных видов и только у
            # основного события (не у дочерних росписей типа «1-й сет»,
            # где scope уже занят периодом).
            root_sport = (sport_root(root.get("sportId")) or {}).get("name", "")
            if not scope and root_sport.lower() in _SET_SPORTS:
                result.extend(self._set_markets(factors, base))

        return result

    def _set_markets(self, factors: list, base: dict) -> list[MarketOdds]:
        """Тоталы и форы ПО СЕТАМ (scope='sets') — теннис и др. сет-виды."""
        vals = {f["f"]: f for f in factors}
        out: list[MarketOdds] = []
        for over_id, under_id in F_SET_TOTALS:
            fo, fu = vals.get(over_id), vals.get(under_id)
            if not fo or not fu:
                continue
            over, under = fo.get("v"), fu.get("v")
            raw_o = fo.get("pt") or fo.get("p")
            raw_u = fu.get("pt") or fu.get("p")
            if not over or not under or raw_o is None:
                continue
            pt = fmt_total(raw_o)
            if pt != fmt_total(raw_u):
                continue
            out.append(MarketOdds(
                market=f"Тотал {pt} по сетам", market_key=f"total:sets:{pt}",
                outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                k1=float(over), k2=float(under), **base))
        seen: set[str] = set()
        for f1_id, f2_id in F_SET_HANDICAPS:
            f1, f2 = vals.get(f1_id), vals.get(f2_id)
            if not f1 or not f2:
                continue
            k1, k2 = f1.get("v"), f2.get("v")
            if not k1 or not k2 or f1.get("pt") is None or f2.get("pt") is None:
                continue
            h1 = fmt_hcap(f1["pt"])
            h2 = fmt_hcap(f2["pt"])
            if h2 != _neg_hcap(h1) or h1 in seen:
                continue
            seen.add(h1)
            out.append(MarketOdds(
                market=f"Фора {h1} по сетам", market_key=f"hcap:sets:{h1}",
                outcome1=f"Ф1 {h1}", outcome2=f"Ф2 {h2}",
                k1=float(k1), k2=float(k2), **base))
        return out

    def _ind_totals(self, factors: list, base: dict,
                    scope: str) -> list[MarketOdds]:
        """Индивидуальные тоталы: вся лестница линий обеих команд."""
        vals = {f["f"]: f for f in factors}
        out: list[MarketOdds] = []
        for side, ladder in (("1", F_ITOTAL_1), ("2", F_ITOTAL_2)):
            team = base["team1"] if side == "1" else base["team2"]
            seen: set[str] = set()
            for over_id, under_id in ladder:
                fo, fu = vals.get(over_id), vals.get(under_id)
                if not fo or not fu:
                    continue
                over, under = fo.get("v"), fu.get("v")
                raw_o = fo.get("pt") or fo.get("p")
                raw_u = fu.get("pt") or fu.get("p")
                if not over or not under or raw_o is None:
                    continue
                pt = fmt_total(raw_o)
                if pt != fmt_total(raw_u) or pt in seen:
                    continue
                seen.add(pt)
                out.append(MarketOdds(
                    market=f"Тотал {pt} ({team})",
                    market_key=f"itotal:{side}:{scope}:{pt}",
                    outcome1=f"ИТБ {pt}", outcome2=f"ИТМ {pt}",
                    k1=float(over), k2=float(under), **base))
        return out

    def _both_score(self, factors: list, base: dict,
                    scope: str) -> list[MarketOdds]:
        vals = {f["f"]: f.get("v") for f in factors}
        yes, no = vals.get(F_BTS_YES), vals.get(F_BTS_NO)
        if not yes or not no:
            return []
        return [MarketOdds(
            market="Обе забьют",
            market_key=f"bothscore:{scope}" if scope else "bothscore",
            outcome1="Да", outcome2="Нет",
            k1=float(yes), k2=float(no), **base,
        )]

    def _winner(self, factors: list, base: dict,
                scope: str) -> list[MarketOdds]:
        vals = {f["f"]: f.get("v") for f in factors}
        k1, k2 = vals.get(F_P1), vals.get(F_P2)
        if not k1 or not k2:
            return []
        kx = vals.get(F_DRAW)
        if kx:
            # Рынок «Исход 1X2» (с ничьей) — самый частый рынок футбола/
            # хоккея. Раньше пропускался целиком (искали только рынок без
            # ничьей), из-за чего вилки по нему не находились вообще.
            k1f, k2f, kxf = float(k1), float(k2), float(kx)
            if not sane_1x2_margin(k1f, kxf, k2f):
                return []
            return [MarketOdds(
                market="Исход (1X2)",
                market_key=f"winner1x2:{scope}" if scope else "winner1x2",
                outcome1="П1", outcome2="П2", outcome3="X",
                k1=k1f, k2=k2f, k3=kxf, **base,
            )]
        return [MarketOdds(
            market="Победитель",
            market_key=f"winner:{scope}" if scope else "winner",
            outcome1="П1", outcome2="П2",
            k1=float(k1), k2=float(k2), **base,
        )]

    def _totals(self, factors: list, base: dict,
                scope: str) -> list[MarketOdds]:
        """Тоталы: основная линия + дополнительные (все пары ТБ/ТМ)."""
        vals = {f["f"]: f for f in factors}
        out = []
        seen: set[str] = set()
        for over_id, under_id in F_TOTAL_PAIRS:
            fo, fu = vals.get(over_id), vals.get(under_id)
            if not fo or not fu:
                continue
            over, under = fo.get("v"), fu.get("v")
            raw_o = fo.get("pt") or fo.get("p")
            raw_u = fu.get("pt") or fu.get("p")
            if not over or not under or raw_o is None:
                continue
            pt = fmt_total(raw_o)
            # линии пары обязаны совпадать, дубли линий не плодим
            if pt != fmt_total(raw_u) or pt in seen:
                continue
            seen.add(pt)
            key = f"total:{scope}:{pt}" if scope else f"total:{pt}"
            out.append(MarketOdds(
                market=f"Тотал {pt}", market_key=key,
                outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                k1=float(over), k2=float(under), **base,
            ))
        return out

    def _handicaps(self, factors: list, base: dict,
                   scope: str) -> list[MarketOdds]:
        """Форы: каждая пара (Ф1, Ф2) — двухисходный рынок.

        pt у Ф1 — знаковая линия team1; линия team2 должна быть строго
        противоположной (иначе это не одна и та же фора — пропускаем).
        """
        vals = {f["f"]: f for f in factors}
        out = []
        for f1_id, f2_id in F_HANDICAPS:
            f1, f2 = vals.get(f1_id), vals.get(f2_id)
            if not f1 or not f2:
                continue
            k1, k2 = f1.get("v"), f2.get("v")
            if not k1 or not k2:
                continue
            # у форы всегда есть строковый pt со знаком («-1.5», «+1», «0»)
            if f1.get("pt") is None or f2.get("pt") is None:
                continue
            h1 = fmt_hcap(f1["pt"])
            h2 = fmt_hcap(f2["pt"])
            # стороны одной форы обязаны быть противоположными — иначе это
            # не пара (защита от смены семантики id у Fonbet)
            if h2 != _neg_hcap(h1):
                continue
            key = f"hcap:{scope}:{h1}"
            out.append(MarketOdds(
                market=f"Фора {h1}", market_key=key,
                outcome1=f"Ф1 {h1}", outcome2=f"Ф2 {h2}",
                k1=float(k1), k2=float(k2), **base,
            ))
        return out
