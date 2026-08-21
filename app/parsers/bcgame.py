"""bc.game — прематч- и лайв-линия через публичный REST-фид BetBy.

Спортивный раздел bc.game работает на платформе BetBy (провайдер sptpub).
Линию отдаёт не HTML, а публичный JSON-фид виджета bt-renderer. Российское
«зеркало» фида (cocoesports.com) открыто без авторизации — токен нужен лишь
для персональных данных и приёма ставок, а коэффициенты доступны всем.

Схема фида (снята с бандла bt-renderer виджета):

    /api/v4/prematch/brand/<brand>/<lang>/0   — индекс: список версий чанков
    /api/v4/prematch/brand/<brand>/<lang>/<v> — чанк данных (sports/…/events)
    /api/v4/live/...                          — то же для лайва
    /api/v3/descriptions/brand/<brand>/markets/<lang> — справочник рынков

Индекс /0 возвращает {epoch, version, top_events_versions, rest_events_
versions, ...}. Забираем ВСЕ перечисленные чанки и склеиваем — получаем
полный текущий срез линии (несколько тысяч событий за пару секунд).

Рынки BetBy — стандартные (Betradar-подобные) id с русскими названиями из
справочника. Разбираем ВСЕ двухисходные рынки: победитель, тоталы, форы
(в т.ч. по геймам/сетам), «обе забьют», чет/нечет, индивидуальные тоталы —
и трёхисходный «Исход 1X2» (П1/X/П2). Предмет/период рынка нормализуется
общим `market_scope`, чтобы совпадать с тем же рынком у других БК.

Исход 1X2 — самый частый рынок футбола и хоккея (в живом прематч-фиде он
есть у большинства событий) и единственный рынок, по которому работает
трёхсторонний движок вилок (arbitrage.find_arbs_1x2). Номера исходов для
него НЕ зашиты в код: их отдаёт сам справочник рынков — у трёхисходного
рынка результата в нём ровно три исхода с названиями «{$competitor1}»,
«ничья», «{$competitor2}», и id берутся оттуда (см. _result3_ids). Зашитый
номер здесь опаснее лишнего запроса: перепутанный исход в ставке — это
потерянные деньги, а нумерация рынков у платформы своя на каждый вид
спорта.

«Ставка без ничьей» (draw no bet) идёт ОТДЕЛЬНЫМ рынком (winner_dnb), а не
победителем: у неё те же два исхода и те же id, что у победителя, но
ничья возвращает ставку. Сшить её с обычным двухисходным победителем
другой БК — значит показать вилку, которой нет: на ничьей одна нога
вернётся, а вторая проиграет.

brand_id bc.game периодически меняется — парсер пытается узнать актуальный
с самого сайта, а при неудаче берёт значение по умолчанию из config.
"""
import logging
import re
import time
from urllib.parse import urlencode

from ..config import (BCGAME_API_HOST, BCGAME_BRAND_ID, BCGAME_LANG,
                      BCGAME_PROVIDER_URL, HTTP_TIMEOUT)
from ..models import KIND_LIVE, KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import (fmt_hcap, fmt_total, format_start, market_scope,
                         sane_1x2_margin)

log = logging.getLogger("parsers.bcgame")

# Как часто обновлять справочник рынков (он большой ~700 КБ и меняется редко)
_MARKETS_TTL = 3600.0

# id исходов BetBy (одинаковы во всех видах спорта, из справочника рынков):
OUT_C1, OUT_C2 = "4", "5"          # победитель: команда 1 / команда 2
OUT_OVER, OUT_UNDER = "12", "13"   # тотал: больше / меньше
OUT_HCAP1, OUT_HCAP2 = "1714", "1715"  # фора: команда 1 (+hcp) / команда 2 (-hcp)
OUT_BTS_YES, OUT_BTS_NO = "74", "76"    # обе забьют: да / нет
OUT_ODD, OUT_EVEN = "70", "72"     # чет/нечет: нечётное / чётное

# Названия исходов трёхисходного рынка результата в справочнике. Именно по
# ним (а не по номерам) опознаётся тройка П1/X/П2 — см. _result3_ids.
_C1_NAME, _C2_NAME = "{$competitor1}", "{$competitor2}"
_DRAW_NAME = "ничья"

# Виды рынков справочника, из которых берём тройку П1/X/П2. «Result» —
# обычный результат матча или его части. Родственные «ResultEP» (досрочная
# выплата) и «ResultFast» (результат внутри интервала) выглядят так же, но
# считаются по другим правилам, и сшивать их с исходом матча у другой БК
# нельзя.
_RESULT3_TYPES = ("Result",)

# Плейсхолдеры названия рынка из справочника: {!setnr}/{gamenr} — номер
# периода, {$competitor1}/{%player} — участник, {total}/{+hcp} — линия.
_PLACEHOLDER_RE = re.compile(r"\{[!$%+\-]?([a-zA-Z_][a-zA-Z0-9_]*)\}")
# Слова вида рынка — они не предмет; убираем перед market_scope
_MARKET_WORDS_RE = re.compile(
    r"(?i)победитель|исход|тотал|фора|гандикап|ставка без ничьей|"
    r"двойной шанс|обе команды забьют|чет/нечет|чёт/нечет")


class BCGameParser(BaseParser):
    name = "bc.game"

    def __init__(self) -> None:
        super().__init__()
        self._brand_id: str | None = None
        self._markets: dict[str, dict] = {}
        self._markets_at = 0.0
        # id рынка → тройка исходов П1/X/П2 (или None, если рынок не
        # трёхисходный). Справочник разбирается один раз на рынок, а не на
        # каждое событие: рынков две тысячи, событий — тысячи, и тройка от
        # события не зависит.
        self._result3: dict[str, tuple[str, str, str] | None] = {}

    # ---------- сетевые помощники ----------

    def _headers(self) -> dict:
        h = super()._headers()
        # Фид bt-renderer запрашивается со страницы bc.game — некоторые
        # узлы отдают данные только с этим Origin/Referer.
        h["Origin"] = "https://bc.game"
        h["Referer"] = "https://bc.game/"
        return h

    def _brand(self) -> str:
        """brand_id BetBy: пробуем узнать с bc.game, иначе — из config."""
        if self._brand_id:
            return self._brand_id
        try:
            data = self.session.post(
                BCGAME_PROVIDER_URL, headers=self._headers(),
                json={}, timeout=HTTP_TIMEOUT).json()
            for prov in data.get("data", {}).get("sportProviders", []):
                brand = (prov.get("betByBrand") or {}).get("brandFlag")
                if prov.get("name") == "betby" and brand:
                    self._brand_id = str(brand)
                    log.info("bc.game: brand_id BetBy = %s", self._brand_id)
                    return self._brand_id
        except Exception as exc:  # noqa: BLE001
            log.info("bc.game: не удалось узнать brand_id с сайта (%s) — "
                     "беру значение по умолчанию", exc)
        self._brand_id = BCGAME_BRAND_ID
        return self._brand_id

    def _api(self, path: str, timeout: float | None = None) -> dict | None:
        url = f"{BCGAME_API_HOST}/{path}"
        try:
            resp = self.session.get(url, headers=self._headers(),
                                    timeout=timeout or HTTP_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            log.debug("bc.game: %s не ответил (%s)", url, exc)
            return None

    def _load_markets(self, brand: str) -> None:
        """Справочник рынков id→{name, specifiers} (кэш с TTL)."""
        now = time.monotonic()
        if self._markets and now - self._markets_at < _MARKETS_TTL:
            return
        data = self._api(
            f"api/v3/descriptions/brand/{brand}/markets/{BCGAME_LANG}",
            timeout=max(HTTP_TIMEOUT, 20))
        if isinstance(data, dict) and data:
            self._markets = data
            self._markets_at = now
            self._result3.clear()
            log.info("bc.game: справочник рынков — %d рынков", len(data))

    # ---------- сбор линии ----------

    def fetch_odds(self) -> list[MarketOdds]:
        return self._collect(live=False)

    def fetch_live_odds(self) -> list[MarketOdds]:
        return self._collect(live=True)

    def _collect(self, live: bool) -> list[MarketOdds]:
        self._delay()
        brand = self._brand()
        self._load_markets(brand)
        if not self._markets:
            log.warning("bc.game: нет справочника рынков — 0 котировок")
            return []

        section = "live" if live else "prematch"
        index = self._api(
            f"api/v4/{section}/brand/{brand}/{BCGAME_LANG}/0")
        if not isinstance(index, dict):
            log.warning("bc.game: индекс %s-фида недоступен — 0 котировок",
                        section)
            return []
        versions = list(index.get("top_events_versions") or []) + \
            list(index.get("rest_events_versions") or [])
        if not versions:
            log.info("bc.game: %s-индекс без версий (линия пуста сейчас)",
                     section)
            return []

        sports: dict = {}
        categories: dict = {}
        tournaments: dict = {}
        events: dict = {}
        for v in versions:
            chunk = self._api(
                f"api/v4/{section}/brand/{brand}/{BCGAME_LANG}/{v}",
                timeout=max(HTTP_TIMEOUT, 20))
            if not isinstance(chunk, dict):
                continue
            sports.update(chunk.get("sports") or {})
            categories.update(chunk.get("categories") or {})
            tournaments.update(chunk.get("tournaments") or {})
            events.update(chunk.get("events") or {})

        now = time.time()
        by_key: dict[str, MarketOdds] = {}
        for eid, ev in events.items():
            for o in self._parse_event(eid, ev, sports, categories,
                                       tournaments, live, now):
                by_key[o.match_key] = o
        odds = list(by_key.values())
        three = sum(1 for o in odds if o.market_key.startswith("winner1x2"))
        log.info("bc.game: %s — %d котировок на %d событий, из них исход "
                 "1X2: %d", section, len(odds),
                 len({o.event_key for o in odds}), three)
        return odds

    def _parse_event(self, eid: str, ev: dict, sports: dict,
                     categories: dict, tournaments: dict,
                     live: bool, now: float) -> list[MarketOdds]:
        desc = ev.get("desc") or {}
        comps = desc.get("competitors") or []
        if desc.get("type") != "match" or len(comps) < 2:
            return []  # аутрайты/спецрынки — не двухисходные матчи
        team1 = (comps[0].get("name") or "").strip()
        team2 = (comps[1].get("name") or "").strip()
        if not team1 or not team2 or team1 == team2:
            return []

        scheduled = desc.get("scheduled")
        start_ts = float(scheduled) if scheduled else None
        if live:
            kind = KIND_LIVE
        else:
            if start_ts is None or start_ts <= now:
                return []  # только прематч
            kind = KIND_PREMATCH
        start_time = format_start(start_ts) if start_ts else None

        sport = self._sport_name(desc, sports, categories, tournaments)
        base = dict(
            bookmaker=self.name, sport=sport, team1=team1, team2=team2,
            kind=kind, start_time=start_time, start_ts=start_ts,
            url=self._event_url(eid, desc, sports, categories, tournaments))

        out: list[MarketOdds] = []
        for mid, variants in (ev.get("markets") or {}).items():
            mdesc = self._markets.get(str(mid))
            if not mdesc or not isinstance(variants, dict):
                continue
            for spec_str, outcomes in variants.items():
                if not isinstance(outcomes, dict):
                    continue
                o = self._market(str(mid), mdesc, spec_str, outcomes, base)
                if o is not None:
                    out.append(o)
        return out

    # ---------- классификация одного рынка ----------

    def _market(self, mid: str, mdesc: dict, spec_str: str, outcomes: dict,
                base: dict) -> MarketOdds | None:
        name = mdesc.get("name") or ""
        spec = self._parse_spec(spec_str)
        ids = set(outcomes)

        def k(oid: str) -> float | None:
            try:
                return float(outcomes[oid]["k"])
            except (KeyError, TypeError, ValueError):
                return None

        low = name.lower().replace("ё", "е")
        has_c1 = "{$competitor1}" in name or "{%player}" in name
        has_c2 = "{$competitor2}" in name
        scope = self._scope(name, spec)

        # --- Исход 1X2 (П1 / ничья / П2) ---
        three = self._result3_ids(mid, mdesc)
        if three and ids >= set(three):
            k1, kx, k2 = k(three[0]), k(three[1]), k(three[2])
            if not sane_1x2_margin(k1, kx, k2):
                return None
            return MarketOdds(
                market="Исход (1X2)" + (f" ({scope})" if scope else ""),
                market_key=f"winner1x2:{scope}" if scope else "winner1x2",
                outcome1="П1", outcome2="П2", outcome3="X",
                k1=k1, k2=k2, k3=kx, **base)

        # --- Ставка без ничьей: те же исходы, но ничья возвращает ставку ---
        if ids >= {OUT_C1, OUT_C2} and "без ничьей" in low:
            k1, k2 = k(OUT_C1), k(OUT_C2)
            if not self._ok(k1, k2):
                return None
            return MarketOdds(
                market="Ставка без ничьей" + (f" ({scope})" if scope else ""),
                market_key=f"winner_dnb:{scope}" if scope else "winner_dnb",
                outcome1="П1", outcome2="П2", k1=k1, k2=k2, **base)

        # --- Победитель (двухисходный: ничьей в рынке нет) ---
        if ids >= {OUT_C1, OUT_C2}:
            k1, k2 = k(OUT_C1), k(OUT_C2)
            if not self._ok(k1, k2):
                return None
            return MarketOdds(
                market="Победитель" + (f" ({scope})" if scope else ""),
                market_key=f"winner:{scope}" if scope else "winner",
                outcome1="П1", outcome2="П2", k1=k1, k2=k2, **base)

        # --- Тоталы (в т.ч. индивидуальные) больше/меньше ---
        if ids >= {OUT_OVER, OUT_UNDER} and "total" in spec:
            over, under = k(OUT_OVER), k(OUT_UNDER)
            pt = fmt_total(spec["total"])
            if not self._ok(over, under) or pt is None:
                return None
            if has_c1 ^ has_c2:  # ровно одна команда — индивидуальный тотал
                side = "1" if has_c1 else "2"
                team = base["team1"] if has_c1 else base["team2"]
                return MarketOdds(
                    market=f"Тотал {pt} ({team})",
                    market_key=f"itotal:{side}:{scope}:{pt}",
                    outcome1=f"ИТБ {pt}", outcome2=f"ИТМ {pt}",
                    k1=over, k2=under, **base)
            if has_c1 and has_c2:
                return None  # комбинированный рынок — не наш
            key = f"total:{scope}:{pt}" if scope else f"total:{pt}"
            return MarketOdds(
                market=f"Тотал {pt}" + (f" ({scope})" if scope else ""),
                market_key=key, outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                k1=over, k2=under, **base)

        # --- Форы (команда1 +hcp / команда2 -hcp) ---
        if ids >= {OUT_HCAP1, OUT_HCAP2} and "hcp" in spec:
            k1, k2 = k(OUT_HCAP1), k(OUT_HCAP2)
            if not self._ok(k1, k2) or has_c1 and has_c2:
                return None
            h1 = fmt_hcap(spec["hcp"])          # знаковая линия команды 1
            h2 = fmt_hcap(-self._num(spec["hcp"]))
            return MarketOdds(
                market=f"Фора {h1}" + (f" ({scope})" if scope else ""),
                market_key=f"hcap:{scope}:{h1}",
                outcome1=f"Ф1 {h1}", outcome2=f"Ф2 {h2}",
                k1=k1, k2=k2, **base)

        # --- Обе забьют Да/Нет ---
        if ids >= {OUT_BTS_YES, OUT_BTS_NO} and "обе" in low \
                and "забьют" in low and not (has_c1 or has_c2):
            yes, no = k(OUT_BTS_YES), k(OUT_BTS_NO)
            if not self._ok(yes, no):
                return None
            return MarketOdds(
                market="Обе забьют" + (f" ({scope})" if scope else ""),
                market_key=f"bothscore:{scope}" if scope else "bothscore",
                outcome1="Да", outcome2="Нет", k1=yes, k2=no, **base)

        # --- Чет/Нечет (только по матчу/периоду, без командных) ---
        if ids >= {OUT_ODD, OUT_EVEN} and "чет" in low \
                and not (has_c1 or has_c2):
            even, odd = k(OUT_EVEN), k(OUT_ODD)
            if not self._ok(even, odd):
                return None
            return MarketOdds(
                market="Чет/Нечет" + (f" ({scope})" if scope else ""),
                market_key=f"oddeven:{scope}" if scope else "oddeven",
                outcome1="Чет", outcome2="Нечет", k1=even, k2=odd, **base)

        return None

    # ---------- помощники ----------

    def _result3_ids(self, mid: str,
                     mdesc: dict) -> tuple[str, str, str] | None:
        """id исходов П1/X/П2 этого рынка по справочнику (или None).

        Тройка признаётся, только если у рынка результата ровно три исхода
        и их названия — в точности «{$competitor1}», «ничья» и
        «{$competitor2}». Точность важна: рядом в справочнике лежат похожие
        на вид рынки с теми же тремя исходами, ставить по которым как по
        исходу матча нельзя — двойной шанс («{$competitor1} или ничья»),
        точный счёт и фора («ничья ({hcp})»), а у одного рынка в живом
        справочнике обе крайние подписи и вовсе указывают на одну команду.
        """
        if mid in self._result3:
            return self._result3[mid]
        found = None
        if mdesc.get("market_type") in _RESULT3_TYPES:
            for variants in (mdesc.get("variants") or {}).values():
                for variant in variants or ():
                    found = self._triple(variant.get("outcomes") or [])
                    if found:
                        break
                if found:
                    break
        self._result3[mid] = found
        return found

    @staticmethod
    def _triple(outcomes: list) -> tuple[str, str, str] | None:
        if len(outcomes) != 3:
            return None
        by_name: dict[str, str] = {}
        for out in outcomes:
            name = str(out.get("name") or "").strip().lower()
            name = name.replace("ё", "е")
            oid = str(out.get("id") or "")
            if not oid or name in by_name:
                return None      # повторная подпись — разметка непонятна
            by_name[name] = oid
        want = (_C1_NAME, _DRAW_NAME, _C2_NAME)
        if set(by_name) != set(want):
            return None
        return by_name[want[0]], by_name[want[1]], by_name[want[2]]

    @staticmethod
    def _ok(k1: float | None, k2: float | None) -> bool:
        return bool(k1 and k2 and k1 > 1 and k2 > 1)

    @staticmethod
    def _num(v) -> float:
        try:
            return float(str(v).replace(",", "."))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _parse_spec(spec_str: str) -> dict:
        """«setnr=1|total=21.5» → {'setnr': '1', 'total': '21.5'}."""
        spec: dict[str, str] = {}
        for part in spec_str.split("|"):
            if "=" in part:
                key, val = part.split("=", 1)
                spec[key.strip()] = val.strip()
        return spec

    def _scope(self, name: str, spec: dict) -> str:
        """Канонический предмет/период рынка (или "" для рынка всего матча).

        Подставляем номера периодов из спецификатора в название («{!setnr}
        сет» → «1 сет»), убираем участников, скобочные пояснения (напр.
        «(вкл. овертайм)» — такой рынок сшивается с основным у других БК) и
        слова вида рынка, остаток прогоняем общим market_scope.
        """
        def repl(m: re.Match) -> str:
            val = spec.get(m.group(1))
            return f" {val} " if val is not None else " "

        text = _PLACEHOLDER_RE.sub(repl, name)
        text = re.sub(r"\([^)]*\)", " ", text)   # «(вкл. овертайм)», «(+hcp)»
        text = _MARKET_WORDS_RE.sub(" ", text)
        return market_scope(text)

    @staticmethod
    def _sport_name(desc: dict, sports: dict, categories: dict,
                    tournaments: dict) -> str:
        sp = sports.get(str(desc.get("sport")), {})
        root = sp.get("name") or "Спорт"
        tour = tournaments.get(str(desc.get("tournament")), {})
        tour_name = tour.get("name")
        return f"{root} · {tour_name}" if tour_name else root

    @staticmethod
    def _event_url(eid: str, desc: dict, sports: dict, categories: dict,
                   tournaments: dict) -> str:
        """Deep-link на страницу события (bt-path виджета BetBy)."""
        sp = sports.get(str(desc.get("sport")), {})
        cat = categories.get(str(desc.get("category")), {})
        tour = tournaments.get(str(desc.get("tournament")), {})
        parts = [sp.get("slug"), cat.get("slug"), tour.get("slug")]
        slug = desc.get("slug")
        crumbs = "/".join(p for p in parts if p)
        path = f"/{crumbs}/{slug}-{eid}" if crumbs and slug \
            else f"/{slug}-{eid}" if slug else f"/{eid}"
        return "https://bc.game/sports?" + urlencode({"bt-path": path})
