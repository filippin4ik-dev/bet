"""Fonbet — парсинг публичного JSON-API линии (только прематч).

Домен сервера линии Fonbet периодически меняется (line01w..lineNNw на разных
resource-доменах), поэтому парсер перебирает список кандидатов и запоминает
первый рабочий. Можно жёстко задать хост через переменную окружения
FONBET_LINE_HOST (см. app/config.py и README).

Проверяются ВСЕ прематч-события (включая дочерние росписи) и оба
двухисходных рынка:
- Победитель (П1 id=921 / П2 id=923), если НЕТ ничьей (id=922);
- Тоталы больше/меньше (ТБ id=930 / ТМ id=931) с параметром линии `pt`.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from ..config import BK_TZ_OFFSET, FONBET_LINE_HOST
from ..models import KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import fmt_hcap, fmt_total, market_scope

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
F_TOTAL_OVER, F_TOTAL_UNDER = 930, 931
# Пары форы (Ф1, Ф2): каждая пара — отдельная линия форы. pt у Ф1 — линия
# team1, у Ф2 — линия team2 (противоположная). Это хорошо известные id
# основной азиатской форы (910/912), форы «ноль» (927/928) и форы ±1
# (989/991); экзотические id не трогаем, чтобы не поймать ложную вилку.
F_HANDICAPS = [(910, 912), (927, 928), (989, 991)]


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
        self._delay()
        data = self._fetch_data()
        if not data:
            return []

        # У Fonbet «спорт» события — это лига/сегмент (напр. «США. MLS»),
        # у сегмента есть parentId до корневого вида спорта («Футбол»).
        # Показываем «Вид спорта · Лига», чтобы работал фильтр по спорту.
        raw_sports = {s["id"]: s for s in data.get("sports", [])}

        def sport_name(sport_id) -> str:
            seg = raw_sports.get(sport_id)
            if not seg:
                return "Спорт"
            root, hops = seg, 0
            while root.get("parentId") in raw_sports and hops < 5:
                root = raw_sports[root["parentId"]]
                hops += 1
            root_name = root.get("name", "Спорт")
            seg_name = seg.get("name", "")
            if seg_name and seg_name != root_name:
                return f"{root_name} · {seg_name}"
            return root_name

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

            # Только прематч: place == 'line'. Live и неактивные события
            # ('live' / 'notActive') пропускаем.
            if event.get("place") != "line" or root.get("place") != "line":
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
                        kind=KIND_PREMATCH, start_time=start_time,
                        start_ts=float(start_ts) if start_ts else None)

            factors = ef.get("factors", [])
            result.extend(self._winner(factors, base, scope))
            result.extend(self._totals(factors, base, scope))
            result.extend(self._handicaps(factors, base, scope))

        return result

    def _winner(self, factors: list, base: dict,
                scope: str) -> list[MarketOdds]:
        vals = {f["f"]: f.get("v") for f in factors}
        if F_DRAW in vals:
            return []  # трёхисходный рынок — пропускаем
        k1, k2 = vals.get(F_P1), vals.get(F_P2)
        if not k1 or not k2:
            return []
        return [MarketOdds(
            market="Победитель",
            market_key=f"winner:{scope}" if scope else "winner",
            outcome1="П1", outcome2="П2",
            k1=float(k1), k2=float(k2), **base,
        )]

    def _totals(self, factors: list, base: dict,
                scope: str) -> list[MarketOdds]:
        overs, unders = {}, {}
        for f in factors:
            raw = f.get("pt") or f.get("p")
            if raw is None:
                continue
            pt = fmt_total(raw)
            if f["f"] == F_TOTAL_OVER:
                overs[pt] = f.get("v")
            elif f["f"] == F_TOTAL_UNDER:
                unders[pt] = f.get("v")
        out = []
        for pt, over in overs.items():
            under = unders.get(pt)
            if not over or not under:
                continue
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
            key = f"hcap:{scope}:{h1}"
            out.append(MarketOdds(
                market=f"Фора {h1}", market_key=key,
                outcome1=f"Ф1 {h1}", outcome2=f"Ф2 {h2}",
                k1=float(k1), k2=float(k2), **base,
            ))
        return out
