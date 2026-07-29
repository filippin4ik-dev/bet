"""Betcity — прематч-линия через публичный JSON API линии.

Betcity — Angular SPA (`betcity.ru`), защищённая антифрод-скриптом «gib»
(Group-IB): страница `/ru/line` открывается в headless/headed Chrome (даже
со стелс-флагами и Xvfb) и рендерит DOM, но JS-приложение так и не
раскрывает реальные котировки — сайт держит их за отдельными XHR/websocket
запросами, инициализация которых в автоматизированном браузере не
проходит. ОДНАКО сам REST API линии, который дергает этот JS, оказался
ДОСТУПЕН напрямую, без браузера и БЕЗ авторизации — достаточно повторить
запрос обычным HTTP-клиентом (никакого анти-бота на самом API нет, в
отличие от рендера страницы):

    POST {BETCITY_API_HOST}/d/off/events?rev=6&template=1
    Content-Type: application/x-www-form-urlencoded
    Body: ids=0

Без параметра `ids_sp` (вид спорта) отдаётся ОДНИМ снимком вся прематч-линия
сайта — все ~33 вида спорта, ~5000 событий, среди них ~1500 футбольных,
~500 теннисных и т.д. Эндпоинт вычислен и параметры сверены по маршрутам
Angular-роутера из `main.<hash>.js` (карта путей API: `betsLine`→
`/off/events`, `betsSports`→`/off/sports`, `betsCompetions`→`/off/champs`).

Гранулярный per-событийный эндпоинт `off/ext` (полная роспись каждого
события — судя по всему, там жили бы «Обе забьют», индивидуальные тоталы,
чёт/нечет) закрыт файрволом на уровне пути: запрос рвётся (`net::ERR_FAILED`
даже из настоящего браузера с реальной cookie-сессией) — похоже, эта ручка
защищена отдельно именно из-за риска поштучного перебора id. Общий снимок
`off/events` фильтру не поддался и отдаёт «топ»-рынки каждого события —
этого достаточно для П1/П2/X, форы и тотала, включая широкий набор
экзотики (угловые, карточки, офсайды, удары, фолы, вбросы и т.п. — Betcity
считает их ОТДЕЛЬНЫМИ рынками с тем же типом «Фактический исход/Фора/
Тотал», просто с текстовым префиксом-предметом в имени).

Формат рынка — по «блокам» (не по числовому id рынка: один и тот же id
переиспользуется для разных по смыслу рынков в разных событиях):
- блок `Wm` с исходами {P1,P2,X} → «Исход 1X2» (winner1x2);
  {P1,P2} без ничьей → «Победитель» (winner);
- блок `F1m` {F1,F2,Kf_F1,Kf_F2} → фора: F1/F2 — знаковая линия (F1=-F2),
  Kf_F1/Kf_F2 — коэффициенты;
- блок `T1m` {Tot,Tm,Tb} → тотал: Tot — линия, Tb/Tm — коэффициенты
  больше/меньше;
- блоки `WXm` (двойной исход), `TMS`/`YNm`/`WINm`/`SCR4` (турнирные
  пропы/точный счёт) — пропускаем: либо не арбитражный 2-исходный рынок
  (двойной исход — три отдельные ставки, не пара), либо не привязаны к
  конкретному матчу (аутрайты турнира — тот же числовой id рынка, что и
  «Фактический исход» настоящего матча, но блок другой формы).

Предмет/период рынка приводится к канону общей `market_scope()` — с
предварительным разворачиванием собственных сокращений Betcity («УГЛ.» →
«угловые.», «ЖК.» → «желтые карточки.»), иначе они не распознаются как
известный предмет и рынок не сошьётся с Winline/Fonbet/BetBoom.
"""
import logging
import time

from ..config import BETCITY_API_HOST, BETCITY_FEED_TIMEOUT, BETCITY_SITE_HOST
from ..models import KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import fmt_hcap, fmt_total, format_start, market_scope, sane_1x2_margin

log = logging.getLogger("parsers.betcity")

_EVENTS_URL = f"{BETCITY_API_HOST}/d/off/events"

_WM_1X2 = frozenset({"P1", "P2", "X"})
_WM_2WAY = frozenset({"P1", "P2"})
_F1M_KEYS = frozenset({"F1", "F2", "Kf_F1", "Kf_F2"})
_T1M_KEYS = frozenset({"Tot", "Tm", "Tb"})

# Слаг вида спорта для страницы события (`/ru/line/<slug>/<cn_ch>/<id_ev>`) —
# снят с карты `SportNameService.list` в main.<hash>.js сайта (числовой
# id_sp сайт нигде в фиде текстом не раскрывает).
_SPORT_URL_SLUG = {
    1: "soccer", 2: "tennis", 3: "basketball", 4: "formula-1",
    5: "baseball", 6: "american-football", 7: "ice-hockey", 8: "handball",
    9: "golf", 10: "chess", 11: "motorcycling", 12: "volleyball",
    13: "rugby", 14: "athletics", 15: "biathlon",
    16: "cross-country-skiing", 17: "bandy", 18: "auto-racing",
    19: "futsal", 20: "boxing", 21: "cycling", 23: "snooker",
    24: "water-polo", 26: "curling", 36: "field-hockey", 39: "ufc",
    40: "fencing", 41: "rowing", 42: "gymnastics", 43: "beach-soccer",
    46: "table-tennis", 47: "darts", 48: "lacrosse", 49: "badminton",
    52: "sailing", 58: "beach-volleyball", 70: "beach-handball",
    71: "australian-football", 72: "paddle-tennis", 73: "cybersport",
    74: "cricket", 75: "bowls", 76: "wrestling", 85: "gaelicsport",
}

# Betcity сокращает предмет рынка ДО названия («УГЛ. Фора», «ЖК. Тотал») —
# market_scope() ищет полные слова, поэтому разворачиваем префикс перед
# канонизацией (сам текст в MarketOdds.market остаётся как на сайте).
_SCOPE_PREFIX_EXPAND = (
    ("угл.", "угловые."),
    ("жк.", "желтые карточки."),
)


def _expand_scope_prefix(name: str) -> str:
    low = name.lower()
    for prefix, expansion in _SCOPE_PREFIX_EXPAND:
        if low.startswith(prefix):
            return expansion + name[len(prefix):]
    return name


def _price(v) -> float | None:
    """Коэффициент лежит во вложенном {"kf": ...}; голое число — это линия
    тотала/форы (Tot/F1/F2), а не цена — им сюда попадать не следует."""
    if isinstance(v, dict):
        kf = v.get("kf")
        if isinstance(kf, (int, float)) and kf > 1.0:
            return float(kf)
    return None


class BetcityParser(BaseParser):
    name = "Betcity"

    def fetch_odds(self) -> list[MarketOdds]:
        now = time.time()
        try:
            payload = self._fetch_snapshot()
        except Exception as exc:  # noqa: BLE001
            log.warning("Betcity: не удалось получить линию: %s", exc)
            return []

        sports = (payload.get("reply") or {}).get("sports") or {}
        log.info("Betcity: снимок линии — %d видов спорта", len(sports))
        if not sports:
            return []

        by_key: dict[str, MarketOdds] = {}
        for sp in sports.values():
            sport_name = sp.get("name_sp") or "Спорт"
            slug = _SPORT_URL_SLUG.get(sp.get("id_sp"))
            for ch in (sp.get("chmps") or {}).values():
                league = self._league_name(ch.get("name_ch"), sport_name)
                for ev in (ch.get("evts") or {}).values():
                    for o in self._parse_event(ev, sport_name, league, slug, now):
                        by_key[o.match_key] = o
        return list(by_key.values())

    def _fetch_snapshot(self) -> dict:
        resp = self.session.post(
            _EVENTS_URL, params={"rev": "6", "template": "1"},
            data={"ids": "0"}, headers=self._headers(),
            timeout=BETCITY_FEED_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _league_name(name_ch: str | None, sport_name: str) -> str:
        name_ch = (name_ch or "").strip()
        prefix = f"{sport_name}. "
        if name_ch.startswith(prefix):
            return name_ch[len(prefix):]
        return name_ch

    def _parse_event(self, ev: dict, sport_name: str, league: str,
                      slug: str | None, now: float) -> list[MarketOdds]:
        team1, team2 = ev.get("name_ht"), ev.get("name_at")
        if not team1 or not team2 or team1 == team2:
            return []  # без второй команды — турнирный/пропозиционный аутрайт
        if ev.get("del_ev") or ev.get("del") or ev.get("hd_bl"):
            return []
        date_ev = ev.get("date_ev")
        if not date_ev or date_ev <= now:
            return []  # только прематч

        sport = f"{sport_name} · {league}" if league else sport_name
        base = dict(
            bookmaker=self.name, sport=sport, team1=team1.strip(),
            team2=team2.strip(), kind=KIND_PREMATCH,
            start_time=format_start(date_ev), start_ts=date_ev,
            url=self._event_url(ev, slug))

        result: list[MarketOdds] = []
        seen: set[str] = set()
        for m in (ev.get("main") or {}).values():
            for o in self._parse_market(m, base):
                if o.market_key in seen:
                    continue
                seen.add(o.market_key)
                result.append(o)
        return result

    def _parse_market(self, m: dict, base: dict) -> list[MarketOdds]:
        name = m.get("name") or ""
        scope = market_scope(_expand_scope_prefix(name))
        result: list[MarketOdds] = []
        for d in (m.get("data") or {}).values():
            for bname, block in (d.get("blocks") or {}).items():
                if not isinstance(block, dict):
                    continue
                o = self._parse_block(bname, block, name, scope, base)
                if o is not None:
                    result.append(o)
        return result

    @staticmethod
    def _parse_block(bname: str, block: dict, name: str, scope: str,
                      base: dict) -> MarketOdds | None:
        keys = frozenset(block.keys())

        if bname == "Wm" and keys == _WM_1X2:
            k1, kx, k2 = _price(block["P1"]), _price(block["X"]), _price(block["P2"])
            if k1 is None or kx is None or k2 is None:
                return None
            if not sane_1x2_margin(k1, kx, k2):
                return None
            key = f"winner1x2:{scope}" if scope else "winner1x2"
            return MarketOdds(
                market=name, market_key=key,
                outcome1="П1", outcome2="П2", outcome3="X",
                k1=k1, k2=k2, k3=kx, **base)

        if bname == "Wm" and keys == _WM_2WAY:
            k1, k2 = _price(block["P1"]), _price(block["P2"])
            if k1 is None or k2 is None:
                return None
            key = f"winner:{scope}" if scope else "winner"
            return MarketOdds(
                market=name, market_key=key,
                outcome1="П1", outcome2="П2", k1=k1, k2=k2, **base)

        if bname == "F1m" and keys == _F1M_KEYS:
            f1_raw, f2_raw = block.get("F1"), block.get("F2")
            try:
                if float(f1_raw) != -float(f2_raw):
                    return None
            except (TypeError, ValueError):
                return None
            k1, k2 = _price(block["Kf_F1"]), _price(block["Kf_F2"])
            if k1 is None or k2 is None:
                return None
            h1, h2 = fmt_hcap(f1_raw), fmt_hcap(f2_raw)
            key = f"hcap:{scope}:{h1}" if scope else f"hcap:{h1}"
            return MarketOdds(
                market=name, market_key=key,
                outcome1=f"Ф1 {h1}", outcome2=f"Ф2 {h2}",
                k1=k1, k2=k2, **base)

        if bname == "T1m" and keys == _T1M_KEYS:
            line = block.get("Tot")
            if line is None:
                return None
            k1, k2 = _price(block["Tb"]), _price(block["Tm"])
            if k1 is None or k2 is None:
                return None
            pt = fmt_total(line)
            key = f"total:{scope}:{pt}" if scope else f"total:{pt}"
            return MarketOdds(
                market=name, market_key=key,
                outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                k1=k1, k2=k2, **base)

        return None  # WXm/TMS/YNm/WINm/SCR4 — см. докстринг модуля

    @staticmethod
    def _event_url(ev: dict, slug: str | None) -> str | None:
        cn_ch, id_ev = ev.get("cn_ch"), ev.get("id_ev")
        if slug and cn_ch and id_ev:
            return f"{BETCITY_SITE_HOST}/ru/line/{slug}/{cn_ch}/{id_ev}"
        return f"{BETCITY_SITE_HOST}/ru/line"
