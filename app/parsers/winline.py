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
- разбираемая «экзотика» (src=16, рынок опознаётся СЕМАНТИЧЕСКИ — по слову
  тотал/фора/чет-нечет в тексте и подписям исходов, см. is_decodable_exotic
  в wl_feed.py): тоталы/форы/чет-нечет ЛЮБОГО предмета и периода (угловые,
  карточки, удары, офсайды, 2-й тайм, N-й сет/период...) и индивидуальные
  тоталы @1/@2 (весь матч, таймы, периоды/сеты/карты). Раньше эти рынки
  ловились жёстким белым списком конкретных текстов (типов линий у Winline
  — сотни, шаблонов не напишешь на все) — терялось ~98% типов src=16, в том
  числе популярные для вилок тоталы угловых/карточек;
- «Исход 1X2» (src=2 — весь матч, src=51 — период) — трёхисходный рынок
  (П1/X/П2), вилки по нему ищет отдельный движок arbitrage.find_arbs_1x2
  (см. предупреждение о порядке кэфов у SRC_WINNER_1X2 в коде: НЕ
  верифицирован на живом фиде, защищён sane_1x2_margin).
Прочую экзотику (src=5/9 — неясная семантика) и прочие спец-рынки пропускаем.

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
from .html_utils import (fmt_hcap, fmt_total, format_start, market_scope,
                         sane_1x2_margin, slugify)
from .wl_feed import get_feed, is_decodable_exotic

log = logging.getLogger("parsers.winline")

# Кэф закрытой стороны Winline выдаёт как 1.0, а «невозможный» исход —
# заглушкой вида 50. Всё вне разумного коридора отбрасываем.
MIN_K, MAX_K = 1.01, 45.0

# Типы линий (idTipEventSrc), которые умеем превращать в рынки
SRC_WINNER = 1          # исход 12 (без ничьей)
SRC_WINNER_1X2 = 2      # исход 1X2 (с ничьей), весь матч
SRC_HCAP = 3            # фора матча
SRC_TOTAL = 4           # тотал матча
SRC_HCAP_HT = 6         # фора 1-го тайма
SRC_TOTAL_HT = 7        # тотал 1-го тайма
SRC_BOTH_SCORE = 15     # обе забьют (Да/Нет)
SRC_WINNER_1X2_P = 51   # исход 1X2 периода (только 1-й, koef = «1»)
SRC_HCAP_P = 61         # фора N-го периода (koef = «N/линия»)
SRC_TOTAL_P = 71        # тотал N-го периода (koef = «N/линия»)
SRC_WINNER_P = 151      # исход 12 периода (только 1-й, koef = «1»)
SRC_EXOTIC = 16         # «экзотика»: рынок опознаётся по ТЕКСТУ типа линии
                        # и подписям исходов (см. is_decodable_exotic в
                        # wl_feed: любой тотал/фора/чет-нечет — по углам,
                        # карточкам, 2-му тайму, N-му сету и т.п.)

# ВНИМАНИЕ: порядок кэфов для SRC_WINNER_1X2/_P (v[0]=П1, v[1]=X, v[2]=П2)
# принят ПО АНАЛОГИИ с порядком id у Fonbet (921=П1, 922=X, 923=П2) и
# порядком отображения «1 X 2» на сайтах БК, но НЕ подтверждён на живом
# фиде Winline (в песочнице разработки нет доступа к российскому IP).
# Каждая котировка проходит sane_1x2_margin() — эвристическую проверку
# собственной маржи БК; в норме она отбросит котировку с перепутанным
# порядком исходов, но перед боевым использованием (особенно с
# автоматической ставкой) сверьте пример из /api/odds с сайтом winline.ru.

# сторона индивидуального рынка команды — токен «@1»/«@2» в тексте типа
# линии (в любом месте, не только в конце: «Тотал [a] @1 (ауты) (@NP@)»).
# НЕ путать с периодным плейсхолдером «@1HT@»/«@1P@»/«@1OT@» — у него сразу
# после цифры идёт буква, а не граница слова/конец строки.
_IT_SIDE_RE = re.compile(r"@([12])(?!\w)")
# кэш «текст типа линии разбираем как экзотику» (is_decodable_exotic дороже
# одного вызова re.search, а тип линии повторяется на каждой линии/событии)
_EXOTIC_OK: dict[int, bool] = {}

# Плейсхолдеры в тексте рынка: @NP@/@FT@/@RT@ — период по умолчанию
# (буквы «NP»/«FT»/«RT» здесь НЕ параметры — это фиксированные токены «весь
# матч»), @1HT@/@2HT@ — 1-й/2-й тайм/половина, @[a]P@ — N-й период (номер в
# koef), @1P@/@2P@/... — N-й период С НОМЕРОМ ПРЯМО В ТЕКСТЕ (не через koef),
# @1OT@/@2OT@ — N-й овертайм/доп. период, [a]/[b] — параметры линии.
# Вырезаем их и скармливаем остаток market_scope.
_PLACEHOLDER_RE = re.compile(r"@[^@]*@|\[[a-z]\]|[()]")
# Численные периодные плейсхолдеры (номер прямо в тексте, не в koef) — ЛЕГКО
# спутать с @NP@ (это буквы «N»+«P», не число+«P»): без \d+ они не совпадут.
_NUM_PERIOD_RE = re.compile(r"@(\d+)P@")
_NUM_OT_RE = re.compile(r"@(\d+)OT@")


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
                url=_event_url(ev["id"], sport_name, champ, live),
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

        if src in (SRC_WINNER_1X2, SRC_WINNER_1X2_P):
            if len(v) != 3:
                return None  # страховка: 1X2 всегда трёхисходный
            k1x, kx, k2x = v[0], v[1], v[2]
            if not (MIN_K < k1x < MAX_K and MIN_K < kx < MAX_K
                    and MIN_K < k2x < MAX_K):
                return None
            if not sane_1x2_margin(k1x, kx, k2x):
                return None  # см. предупреждение у SRC_WINNER_1X2 выше
            return MarketOdds(
                market=f"Исход (1X2) {label}".strip(),
                market_key=f"winner1x2:{scope}" if scope else "winner1x2",
                outcome1="П1", outcome2="П2", outcome3="X",
                k1=k1x, k2=k2x, k3=kx, **base)

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

        return None  # прочие спец-рынки не поддерживаем

    def _exotic(self, ln: dict, tl: dict, base: dict, scope: str,
                label: str, k1: float, k2: float) -> MarketOdds | None:
        """Разбираемая «экзотика» src=16 — рынок опознаётся СЕМАНТИЧЕСКИ
        (is_decodable_exotic: ключевое слово тотал/фора/чет-нечет + подписи
        исходов из справочника), а не по жёсткому списку конкретных текстов —
        так подхватывается любой предмет/период (углы, карточки, 2-й тайм,
        N-й сет...), а не только заранее перечисленные формулировки. Предмет/
        период уже разобран в scope (см. _scope). Индивидуальные тоталы
        @1/@2 (текст рынка КОНКРЕТНОЙ команды) обрабатываются отдельно ниже;
        подписи исходов ещё раз сверяются со справочником (R) — при смене
        семантики типа линия просто не разберётся, ложных рынков не будет.
        """
        text = tl["text"]
        ok = _EXOTIC_OK.get(tl["id"])
        if ok is None:
            ok = is_decodable_exotic(tl)
            _EXOTIC_OK[tl["id"]] = ok
        if not ok or len(ln["v"]) != 2:
            return None
        r = tl.get("R") or ["", ""]
        low = text.lower()

        # чет/нечет: порядок кэфов в фиде — [Нечет, Чет] (по справочнику)
        if "чет" in low:
            if not r[0].startswith("Нечет") or not r[1].startswith("Чет"):
                return None
            if _IT_SIDE_RE.search(text):
                return None  # чет/нечет КОНКРЕТНОЙ команды — модель не
                             # поддерживает индивидуальный чет/нечет
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

        if "фора" in low:                    # фора любого предмета/периода
            if r[0] != "1" or r[1] != "2":
                return None
            if _IT_SIDE_RE.search(text):
                return None  # фора КОНКРЕТНОЙ команды текстом не встречена,
                             # но на всякий случай не поддерживаем и её
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

        Период берём из текста tipline: @1HT@/@2HT@ — 1-й/2-й тайм (слово из
        строк вида спорта: тайм/половина/сет), @[a]P@ — N-й период (номер —
        в первой части koef, слово — период/сет/четверть/карта/иннинг),
        @1P@/@2P@/... — N-й период, но номер ЗАШИТ В САМ ПЛЕЙСХОЛДЕР (не в
        koef) — раньше не распознавался вообще, из-за чего, например, фора
        по угловым 1-го периода теряла метку периода и ложно сшивалась с
        форой по угловым всего матча (см. _NUM_PERIOD_RE), @1OT@/@2OT@ —
        N-й овертайм/доп. период (см. _NUM_OT_RE). Остальной текст рынка
        («Тотал карт», «тотал раундов») даёт предметные токены. Всё
        прогоняется через общий market_scope.
        """
        text = tl["text"]
        strings = sport_info.get("strings") or [""] * 9
        period_txt = ""
        m_np = _NUM_PERIOD_RE.search(text)
        m_ot = _NUM_OT_RE.search(text)
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
        elif m_np:
            word = strings[4] or "период"
            period_txt = f"{m_np.group(1)}-й {word}"
        elif m_ot:
            period_txt = f"{m_ot.group(1)}-й овертайм"
        # Индивидуальный маркер «@1»/«@2» убираем ДО _PLACEHOLDER_RE: у него
        # нет закрывающего «@», и «@[^@]*@» иначе жадно захватит всё до
        # СЛЕДУЮЩЕГО плейсхолдера («Тотал [a] @1 (ауты) (@NP@)» без этого
        # превратится в «Тотал NP@», предмет «ауты» потеряется).
        residual = _IT_SIDE_RE.sub(" ", text)
        residual = _PLACEHOLDER_RE.sub(" ", residual)
        # «исход»/«тотал»/«фора» — вид рынка, не предмет; убираем, чтобы
        # market_scope видел только предмет (карты, раунды, углы...)
        residual = re.sub(r"(?i)исход|тотал|фора|победитель", " ", residual)
        scope = market_scope(f"{period_txt} {residual}".strip())
        label = f"({period_txt})" if period_txt else ""
        return scope, label


def _event_url(event_id: int, sport_name: str, champ: tuple | None,
               live: bool) -> str:
    """Страница матча на сайте.

    Раньше ссылка вела на «/stavki/event/<id>» — этот путь есть в разметке
    самого сайта, но работает только при переходе ВНУТРИ приложения: при
    открытии по ссылке роутер не успевает достать событие и сбрасывает на
    главную. Настоящий адрес страницы содержит четыре сегмента —
    «/{раздел}/sport/<спорт>/<страна>/<чемпионат>/<id>» (проверено в
    браузере: по нему матч открывается с холодного захода, а с меньшим
    числом сегментов роутер снова уходит на главную).

    Роутер разбирает только id, слаги ему безразличны (адрес с «x/y/z»
    открывает ту же страницу) — поэтому неизвестная страна не ломает
    ссылку, а лишь делает её менее говорящей.
    """
    section = "live" if live else "stavki"
    country = champ[2] if champ and len(champ) > 2 else ""
    champ_name = champ[1] if champ else ""
    return (f"https://winline.ru/{section}/sport/"
            f"{slugify(sport_name, 'sport')}/"
            f"{slugify(country, 'mir')}/"
            f"{slugify(champ_name, 'liga')}/{event_id}")
