"""Winline — прямой бинарный websocket-фид линии (без браузера).

Раньше парсер рендерил страницы Selenium'ом и собирал лишь верхушку ленты
(гонки рендеринга давали 0 матчей). Теперь подключаемся к тому же фиду
`wss://wss.winline.ru/data_ng`, что и сайт (см. wl_feed.py), и получаем
ВСЮ линию: пару тысяч прематч-матчей и весь лайв за секунды.

Снапшот прематча содержит только топ-линии; фид дополнительно выкачивает
ПОЛНУЮ роспись каждого события командой «event.plus» (см. wl_feed.py) —
все линии тоталов и фор, таймы и периоды. Здесь оба источника разбираются
одинаково.

Из фида берём все двухисходные рынки:
- «Исход 12» / «Победитель» (src=1) — победитель без ничьей;
- «Тотал» (src=4), «тотал 1-го тайма» (src=7), «тотал N-го периода/сета/
  карты» (src=71) — каждая линия Больше/Меньше отдельным рынком;
- «Фора» (src=3), «фора 1-го тайма» (src=6), «фора периода» (src=61) —
  сторона фаворита в поле favorite, величина линии в поле koef;
- «исход 12 периода» (src=151) — победитель 1-го периода/сета;
- «Обе забьют» (src=15) — Да/Нет (проверено: V[0]=Да, V[1]=Нет, подписи
  в справочнике R=['Да','Нет']; кэфы сходятся с рынком Fonbet);
- разбираемая «экзотика» (src=16, рынок опознаётся по тексту типа линии):
  индивидуальные тоталы @1/@2 (весь матч, таймы, периоды/сеты/карты),
  чет/нечет, тотал/фора 2-го тайма, тотал/фора по сетам (теннис).
Трёхисходные рынки (1X2: src=2/5/51/9) и прочую экзотику пропускаем.

Порядок кэфов проверен на живых данных: V[0] — исход «1»/«Больше»,
V[1] — «2»/«Меньше» (по возрастанию линии тотала кэф V[0] растёт).
Период/предмет рынка нормализуется общим market_scope, чтобы рынок
совпадал с тем же рынком других БК.
"""
import logging
import re
import time

from ..config import WINLINE_SNAPSHOT_WAIT
from ..models import KIND_LIVE, KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import fmt_hcap, fmt_total, format_start, market_scope
from .wl_feed import DECODABLE_EXOTIC, get_feed

log = logging.getLogger("parsers.winline")

# Кэф закрытой стороны Winline выдаёт как 1.0, а «невозможный» исход —
# заглушкой вида 50. Всё вне разумного коридора отбрасываем.
MIN_K, MAX_K = 1.01, 45.0

# Типы линий (idTipEventSrc), которые умеем превращать в двухисходные рынки
SRC_WINNER = 1          # исход 12 (без ничьей)
SRC_HCAP = 3            # фора матча
SRC_TOTAL = 4           # тотал матча
SRC_HCAP_HT = 6         # фора 1-го тайма
SRC_TOTAL_HT = 7        # тотал 1-го тайма
SRC_BOTH_SCORE = 15     # обе забьют (Да/Нет)
SRC_HCAP_P = 61         # фора N-го периода (koef = «N/линия»)
SRC_TOTAL_P = 71        # тотал N-го периода (koef = «N/линия»)
SRC_WINNER_P = 151      # исход 12 периода (только 1-й, koef = «1»)
SRC_EXOTIC = 16         # «экзотика»: рынок опознаётся по ТЕКСТУ типа линии
                        # (см. DECODABLE_EXOTIC в wl_feed: инд. тоталы,
                        # чет/нечет, 2-й тайм, сет-рынки)

# сторона индивидуального рынка: текст типа линии кончается на «@1»/«@2»
_IT_SIDE_RE = re.compile(r"@([12])$")
# кэш «текст типа линии разбираем как экзотику» (регэкспы дорогие)
_EXOTIC_OK: dict[int, bool] = {}

# Плейсхолдеры в тексте рынка: @NP@/@FT@/@RT@ — период по умолчанию,
# @1HT@ — 1-й тайм/половина, @[a]P@ — N-й период (номер в koef), [a]/[b] —
# параметры линии. Вырезаем их и скармливаем остаток market_scope.
_PLACEHOLDER_RE = re.compile(r"@[^@]*@|\[[a-z]\]|[()]")


class WinlineParser(BaseParser):
    name = "Winline"

    def fetch_odds(self) -> list[MarketOdds]:
        feed = get_feed()
        feed.start()
        if not feed.healthy():
            feed.wait_prematch(WINLINE_SNAPSHOT_WAIT)
        if not feed.healthy():
            log.warning("Winline: фид ещё не отдал снапшот прематча — "
                        "0 котировок в этом цикле")
            return []
        sports, tiplines, champs, events, lines = feed.prematch_snapshot()
        return self._build(sports, tiplines, champs, events, lines,
                           live=False)

    def fetch_live_odds(self) -> list[MarketOdds]:
        feed = get_feed()
        feed.start()
        if not feed.healthy():
            return []
        sports, tiplines, champs, events, lines = feed.live_snapshot()
        return self._build(sports, tiplines, champs, events, lines,
                           live=True)

    # ---------- сборка MarketOdds ----------

    def _build(self, sports: dict, tiplines: dict, champs: dict,
               events: list, lines: list, live: bool) -> list[MarketOdds]:
        now = time.time()
        ev_by_id: dict[int, dict] = {}
        for ev in events:
            if not ev.get("team1") or not ev.get("team2") \
                    or ev["team1"] == ev["team2"]:
                continue
            if live:
                # state 1 — приём ставок открыт; 2 — событие приостановлено
                # (опасный момент), его кэфы для вилок использовать нельзя
                if ev.get("state") != 1:
                    continue
            elif not ev.get("ts") or ev["ts"] <= now:
                continue  # прематч: матч уже начался
            ev_by_id[ev["id"]] = ev

        by_key: dict[str, MarketOdds] = {}
        for ln in lines:
            if live and ln.get("state", 1) != 1:
                continue  # линия приостановлена — ставить нельзя
            ev = ev_by_id.get(ln["event"])
            if ev is None:
                continue
            tl = tiplines.get(ln["tid"])
            if tl is None:
                continue
            champ = champs.get(ev["champ"])
            sport_id = champ[0] if champ else 0
            sport_info = sports.get(sport_id, {})
            sport_name = sport_info.get("name", "Спорт")
            if champ and champ[1]:
                sport = f"{sport_name} · {champ[1]}"
            else:
                sport = sport_name
            base = dict(
                bookmaker=self.name, sport=sport,
                team1=ev["team1"], team2=ev["team2"],
                kind=KIND_LIVE if live else KIND_PREMATCH,
                start_time=format_start(ev["ts"]) if ev.get("ts") else None,
                start_ts=float(ev["ts"]) if ev.get("ts") else None,
                # страница события на сайте (роут снят с бандла main.js:
                # isLive ? '/live/event/' : '/stavki/event/' + event.id)
                url=("https://winline.ru/live/event/" if live
                     else "https://winline.ru/stavki/event/") + str(ev["id"]),
            )
            o = self._market(ln, tl, sport_info, base)
            if o is not None:
                by_key[o.match_key] = o
        return list(by_key.values())

    def _market(self, ln: dict, tl: dict, sport_info: dict,
                base: dict) -> MarketOdds | None:
        src = tl["src"]
        v = ln["v"]
        if len(v) < 2:
            return None
        k1, k2 = v[0], v[1]
        if not (MIN_K < k1 < MAX_K and MIN_K < k2 < MAX_K):
            return None

        scope, label = self._scope(tl, ln["koef"], sport_info)

        if src == SRC_EXOTIC:
            return self._exotic(ln, tl, base, scope, label, k1, k2)

        if src in (SRC_WINNER, SRC_WINNER_P):
            if len(v) != 2:
                return None  # страховка: победитель всегда двухисходный
            return MarketOdds(
                market=f"Победитель {label}".strip(),
                market_key=f"winner:{scope}" if scope else "winner",
                outcome1="П1", outcome2="П2", k1=k1, k2=k2, **base)

        if src == SRC_BOTH_SCORE:
            if len(v) != 2:
                return None
            return MarketOdds(
                market=f"Обе забьют {label}".strip(),
                market_key=f"bothscore:{scope}" if scope else "bothscore",
                outcome1="Да", outcome2="Нет", k1=k1, k2=k2, **base)

        if src in (SRC_TOTAL, SRC_TOTAL_HT, SRC_TOTAL_P):
            pt = self._line_value(src, ln["koef"])
            if pt is None:
                return None
            pt = fmt_total(pt)
            key = f"total:{scope}:{pt}" if scope else f"total:{pt}"
            return MarketOdds(
                market=f"Тотал {label} {pt}".replace("  ", " "),
                market_key=key,
                outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                k1=k1, k2=k2, **base)   # V[0]=Больше, V[1]=Меньше

        if src in (SRC_HCAP, SRC_HCAP_HT, SRC_HCAP_P):
            val = self._line_value(src, ln["koef"])
            if val is None:
                return None
            h1 = self._h1_from(val, ln.get("fav", 0))
            if h1 is None:
                return None
            h1s, h2s = fmt_hcap(h1), fmt_hcap(-h1)
            key = f"hcap:{scope}:{h1s}"
            return MarketOdds(
                market=f"Фора {label} {h1s}".replace("  ", " "),
                market_key=key,
                outcome1=f"Ф1 {h1s}", outcome2=f"Ф2 {h2s}",
                k1=k1, k2=k2, **base)   # V[0]=team1, V[1]=team2

        return None  # 1X2 и спец-рынки не поддерживаем

    def _exotic(self, ln: dict, tl: dict, base: dict, scope: str,
                label: str, k1: float, k2: float) -> MarketOdds | None:
        """Разбираемая «экзотика» src=16 — рынок опознаётся по тексту типа
        линии (белый список DECODABLE_EXOTIC): индивидуальные тоталы
        @1/@2, чет/нечет, тотал/фора 2-го тайма, тотал/фора по сетам.
        Подписи исходов сверяются со справочником (R) — при смене
        семантики типа линия просто не разберётся, ложных рынков не будет.
        """
        text = tl["text"]
        ok = _EXOTIC_OK.get(tl["id"])
        if ok is None:
            ok = any(p.match(text) for p in DECODABLE_EXOTIC)
            _EXOTIC_OK[tl["id"]] = ok
        if not ok or len(ln["v"]) != 2:
            return None
        r = tl.get("R") or ["", ""]
        low = text.lower()

        # чет/нечет: порядок кэфов в фиде — [Нечет, Чет] (по справочнику)
        if "чет" in low:
            if not r[0].startswith("Нечет") or not r[1].startswith("Чет"):
                return None
            return MarketOdds(
                market=f"Чет/Нечет {label}".strip(),
                market_key=f"oddeven:{scope}" if scope else "oddeven",
                outcome1="Чет", outcome2="Нечет",
                k1=k2, k2=k1, **base)

        if "тотал" in low:
            if r[0] != "Больше" or r[1] != "Меньше":
                return None
            val = self._koef_line(ln["koef"])
            if val is None:
                return None
            pt = fmt_total(val)
            side_m = _IT_SIDE_RE.search(text)
            if side_m:                       # индивидуальный тотал @1/@2
                side = side_m.group(1)
                team = base["team1"] if side == "1" else base["team2"]
                return MarketOdds(
                    market=f"Тотал {pt} ({team}) {label}".strip(),
                    market_key=f"itotal:{side}:{scope}:{pt}",
                    outcome1=f"ИТБ {pt}", outcome2=f"ИТМ {pt}",
                    k1=k1, k2=k2, **base)
            key = f"total:{scope}:{pt}" if scope else f"total:{pt}"
            return MarketOdds(               # тотал 2-го тайма / по сетам
                market=f"Тотал {label} {pt}".replace("  ", " "),
                market_key=key,
                outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                k1=k1, k2=k2, **base)

        if "фора" in low:                    # фора 2-го тайма / по сетам
            if r[0] != "1" or r[1] != "2":
                return None
            val = self._koef_line(ln["koef"])
            if val is None:
                return None
            h1 = self._h1_from(val, ln.get("fav", 0))
            if h1 is None:
                return None
            h1s, h2s = fmt_hcap(h1), fmt_hcap(-h1)
            return MarketOdds(
                market=f"Фора {label} {h1s}".replace("  ", " "),
                market_key=f"hcap:{scope}:{h1s}",
                outcome1=f"Ф1 {h1s}", outcome2=f"Ф2 {h2s}",
                k1=k1, k2=k2, **base)

        return None

    # ---------- помощники ----------

    @staticmethod
    def _h1_from(val: float, fav: int) -> float | None:
        """Знаковая линия форы team1 из величины линии и стороны фаворита.

        Снапшот кодирует линию форы БЕЗ знака (сторона — в fav), а
        полная роспись («event.plus») шлёт строку СО знаком: koef
        «1/-1.5» fav=1 — это линия team1 как есть (Ф1 -1.5).
        Отрицательное значение уже готовая линия team1; с fav=2 оно
        противоречиво — такую линию безопаснее пропустить."""
        if val < 0:
            return None if fav == 2 else val
        if fav == 1:
            return -val
        if fav == 2:
            return val
        if val == 0:
            return 0.0
        return None  # линия без стороны — не разобрать

    @staticmethod
    def _koef_line(koef: str) -> float | None:
        """Числовая линия из koef экзотики: «11.5» или «N/11.5»."""
        try:
            return float(koef.split("/")[-1])
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _line_value(src: int, koef: str) -> float | None:
        """Числовая линия рынка. Для периодных (src 61/71) koef = «N/лин»."""
        try:
            if src in (SRC_HCAP_P, SRC_TOTAL_P):
                return float(koef.split("/", 1)[1])
            return float(koef)
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _scope(tl: dict, koef: str, sport_info: dict) -> tuple[str, str]:
        """(канонический scope рынка, человекочитаемая метка периода).

        Период берём из текста tipline: @1HT@ — 1-й тайм (слово из строк
        вида спорта: тайм/половина/сет), @[a]P@ — N-й период (номер — в
        первой части koef, слово — период/сет/четверть/карта/иннинг).
        Остальной текст рынка («Тотал карт», «тотал раундов») даёт
        предметные токены. Всё прогоняется через общий market_scope.
        """
        text = tl["text"]
        strings = sport_info.get("strings") or [""] * 9
        period_txt = ""
        if "@1HT@" in text:
            word = strings[3] or strings[4] or "тайм"
            period_txt = f"1-й {word}"
        elif "@2HT@" in text:
            word = strings[3] or strings[4] or "тайм"
            period_txt = f"2-й {word}"
        elif "@[a]P@" in text:
            num = koef.split("/", 1)[0] or "1"
            word = strings[4] or "период"
            period_txt = f"{num}-й {word}"
        residual = _PLACEHOLDER_RE.sub(" ", text)
        # «исход»/«тотал»/«фора» — вид рынка, не предмет; убираем, чтобы
        # market_scope видел только предмет (карты, раунды, углы...)
        residual = re.sub(r"(?i)исход|тотал|фора|победитель", " ", residual)
        scope = market_scope(f"{period_txt} {residual}".strip())
        label = f"({period_txt})" if period_txt else ""
        return scope, label
