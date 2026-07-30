"""Раскладка рынков в фиде Melbet: какой группе верить и не перепутаны ли
стороны исходов.

ЗАЧЕМ ЭТО НУЖНО. Движок 1xBet (на нём работает Melbet) не подписывает
рынки словами: у исхода только числа — группа рынка G, код исхода T,
коэффициент C и линия P:

    {"G": 17, "T": 9, "C": 1.85, "P": 2.5}

Что означает конкретный код, БК нигде не публикует, а открытые описания
движка расходятся между собой: в одном разборе T=10 — это «тотал меньше»,
в другом — «тотал больше, альтернативная линия». Ошибка тут не безобидна.
Перепутав «больше» и «меньше», сканер сшил бы ТБ Melbet с ТМ другой БК и
показал бы «вилку 15 %» там, где её нет, — а на такие ставятся деньги.

ПОЭТОМУ КОДЫ ЗДЕСЬ — НЕ ИСТИНА, А ГИПОТЕЗА, которую каждый обход
проверяет по самим данным. Проверки опираются на то, что в линии любой БК
верно всегда, независимо от нумерации:

1. Лестница тоталов монотонна: чем выше линия, тем ДОРОЖЕ «больше» и
   ДЕШЕВЛЕ «меньше». Если по всей линии выходит наоборот — коды «больше»
   и «меньше» стоят наоборот.
2. Фора фаворита отрицательна: у пары форы с почти равными кэфами
   (кэф ≈ кэф — это «справедливая» линия) знак линии показывает, кто
   фаворит. Кто фаворит, известно из исхода 1X2 того же матча.
3. Индивидуальный тотал фаворита выше, чем у аутсайдера.

Каждое событие снимка отдаёт по такому правилу один голос «за» гипотезу
или «против». Решение принимается по всей линии сразу (тысячи событий),
а не по одному матчу, поэтому шум отдельных матчей ничего не решает:
- голоса «против» уверенно победили → стороны переворачиваются, в лог
  идёт предупреждение (значит, БК сменила нумерацию);
- голоса разделились примерно поровну → это НЕ шум, а признак того, что
  в одну группу попали разные по смыслу рынки (например тотал матча и
  тотал углов). Такое семейство рынков целиком пропускается: лучше не
  показать рынок, чем показать вилку по ошибочной разметке.

Вторая задача модуля — ВЫБОР ГРУППЫ. Один и тот же код исхода (T=9/10 —
«тотал») встречается в фиде у нескольких групп: тотал матча, тотал тайма,
азиатский тотал, тотал углов. По номеру группы их не различить (словаря
имён у фида нет), а смешивать нельзя: «тотал 2.5 первого тайма» и «тотал
2.5 матча» — разные рынки. Берём только ОДНУ группу на семейство — ту,
что встречается в компактном снимке линии чаще всех (там у события лежат
именно основные рынки матча, они есть почти у каждого события). Остальные
группы пропускаются: они попадут в линию, только когда появится
подтверждённый словарь имён рынков (см. app/diagnose_melbet.py).
"""
import logging
from dataclasses import dataclass, field
from statistics import median

log = logging.getLogger("parsers.melbet")

# ---------------------------------------------------------------------------
# Гипотеза о кодах исходов
# ---------------------------------------------------------------------------
# Числа взяты из общедоступных разборов движка 1xBet и сходятся у разных
# источников для основных рынков (1X2, тотал, фора). Всё, что ниже,
# проверяется голосованием по живой линии — см. detect().
T_W1, T_DRAW, T_W2 = 1, 2, 3
T_HCAP1, T_HCAP2 = 7, 8
T_OVER, T_UNDER = 9, 10
T_IT1_OVER, T_IT1_UNDER = 11, 12
T_IT2_OVER, T_IT2_UNDER = 13, 14

WINNER = "winner"
HCAP = "hcap"
TOTAL = "total"
ITOTAL1 = "itotal1"
ITOTAL2 = "itotal2"
# Отдельный «вопрос» калибровки, а не семейство рынков: чей индивидуальный
# тотал лежит в кодах 11/12 — первой команды или второй. Ошибиться тут
# можно независимо от того, верно ли размечены «больше»/«меньше», поэтому
# и голосование за него своё.
ITOTAL_SIDES = "itotal_sides"

# Семейство рынка → коды его исходов. Пара «прямых» сторон (первая —
# «больше»/«фора первой команды») стоит первой: именно её и проверяет
# голосование.
FAMILY_CODES: dict[str, tuple[int, ...]] = {
    WINNER: (T_W1, T_DRAW, T_W2),
    HCAP: (T_HCAP1, T_HCAP2),
    TOTAL: (T_OVER, T_UNDER),
    ITOTAL1: (T_IT1_OVER, T_IT1_UNDER),
    ITOTAL2: (T_IT2_OVER, T_IT2_UNDER),
}
FAMILY_OF: dict[int, str] = {
    code: family for family, codes in FAMILY_CODES.items() for code in codes
}
# Семейства с линией (P): у них при выборе группы учитывается ещё и
# величина линии, а стороны проверяются на монотонность лестницы.
LINE_FAMILIES = (HCAP, TOTAL, ITOTAL1, ITOTAL2)

# Сколько голосов нужно, чтобы вообще принимать решение по семейству.
# Меньше — это уже не «вся линия», а несколько случайных матчей.
MIN_VOTES = 20
# Доля голосов, при которой сторона считается уверенно доказанной.
# Между 0.4 и 0.6 голоса «размазаны» — семейство пропускается целиком.
SURE_SHARE = 0.6
# Насколько кэфы на победу должны различаться, чтобы считать команду
# фаворитом (на равных соперниках знак форы неинформативен).
FAV_GAP = 1.1


@dataclass
class RawEvent:
    """Событие фида, разобранное до чисел: (группа, код, линия, кэф).

    scope — область рынков: "" у рынков всего матча и канонический токен
    периода (см. html_utils.market_scope) у подигры вроде «1-й тайм».

    base_picks — исходы из КОМПАКТНОГО снимка линии (там лежат основные
    рынки матча). По ним выбирается группа семейства: в полной росписи
    (GetGameZip) групп на порядок больше, и «самая частая» там была бы
    случайной.
    """

    game: dict
    scope: str = ""
    picks: list[tuple[int, int, float | None, float]] = field(
        default_factory=list)
    base_picks: list[tuple[int, int, float | None, float]] | None = None

    def lines(self, family: str, group: int | None) -> dict[int, dict]:
        """Исходы семейства в группе: {код исхода: {линия: кэф}}.

        group=None — брать любую группу (для подигр, где своя группа
        выбирается по однозначности, см. sole_group)."""
        codes = FAMILY_CODES[family]
        out: dict[int, dict] = {}
        for g, t, p, c in self.picks:
            if t not in codes or (group is not None and g != group):
                continue
            out.setdefault(t, {})[p] = c
        return out

    def sole_group(self, family: str) -> int | None:
        """Единственная группа семейства в этом событии (или None, если её
        нет или групп несколько — тогда рынок неоднозначен)."""
        codes = FAMILY_CODES[family]
        groups = {g for g, t, _p, _c in self.picks if t in codes}
        return groups.pop() if len(groups) == 1 else None

    def winner_odds(self, group: int | None) -> tuple[float, float] | None:
        """Кэфы на победу первой и второй команды (для проверки фаворита)."""
        win = self.lines(WINNER, group)
        k1 = (win.get(T_W1) or {}).get(None)
        k2 = (win.get(T_W2) or {}).get(None)
        if not k1 or not k2 or k1 <= 1 or k2 <= 1:
            return None
        return k1, k2


@dataclass
class Layout:
    """Итог калибровки одного снимка линии."""

    groups: dict[str, int | None] = field(default_factory=dict)
    # Семейства, у которых стороны исходов оказались перевёрнуты
    # относительно гипотезы (T_OVER на самом деле «меньше» и т.п.).
    inverted: set[str] = field(default_factory=set)
    # Семейства, разметку которых подтвердить не удалось — не отдаём.
    skipped: set[str] = field(default_factory=set)
    votes: dict[str, tuple[int, int]] = field(default_factory=dict)
    # Сколько событий стоит за каждой группой: {семейство: [(группа, событий,
    # медиана линии)]} — нужно только для диагностики.
    group_stats: dict[str, list[tuple[int, int, float]]] = field(
        default_factory=dict)
    # Средний перевес хозяев по всей линии (1/К1 − 1/К2). Ни на что не
    # влияет, но в диагностике сразу видно, не перепутаны ли команды
    # местами: у настоящей линии он положительный (хозяева выигрывают чаще).
    home_edge: float | None = None

    def group(self, family: str) -> int | None:
        return self.groups.get(family)

    def is_inverted(self, family: str) -> bool:
        return family in self.inverted

    def usable(self, family: str) -> bool:
        if family in self.skipped:
            return False
        # Не знаем, чей индивидуальный тотал где, — не отдаём ни один.
        if family in (ITOTAL1, ITOTAL2) and ITOTAL_SIDES in self.skipped:
            return False
        return True

    def itotal_side(self, family: str) -> int:
        """Команда (1 или 2), которой принадлежит семейство инд. тоталов."""
        side = 1 if family == ITOTAL1 else 2
        if ITOTAL_SIDES in self.inverted:
            side = 3 - side
        return side

    def summary(self) -> str:
        parts = []
        for family in list(FAMILY_CODES) + [ITOTAL_SIDES]:
            group = self.groups.get(family)
            good, bad = self.votes.get(family, (0, 0))
            if group is None and not (good or bad):
                continue
            mark = ""
            if family in self.skipped:
                mark = " (пропущено: разметка не подтвердилась)"
            elif family in self.inverted:
                mark = " (стороны перевёрнуты)"
            where = f"=G{group}" if group is not None else ""
            vote = f" голоса {good}/{bad}" if good or bad else ""
            parts.append(f"{family}{where}{vote}{mark}")
        return "; ".join(parts) or "рынков не найдено"


def detect(events: list[RawEvent]) -> Layout:
    """Определяет раскладку рынков по снимку линии."""
    layout = Layout()
    layout.groups, layout.group_stats = _pick_groups(events)
    layout.home_edge = _home_edge(events, layout.groups.get(WINNER))

    for family, (good, bad) in _collect_votes(events, layout.groups).items():
        layout.votes[family] = (good, bad)
        total = good + bad
        if total < MIN_VOTES:
            continue          # мало данных — остаёмся при гипотезе
        share = bad / total
        if share >= SURE_SHARE:
            layout.inverted.add(family)
        elif share > 1 - SURE_SHARE:
            layout.skipped.add(family)
    return layout


# ---------------------------------------------------------------------------
# Выбор группы
# ---------------------------------------------------------------------------


def _pick_groups(events: list[RawEvent]):
    """Для каждого семейства — группа, покрывающая больше всего событий.

    Считаем по компактному снимку и только по рынкам всего матча: у
    подигр («1-й тайм») свои группы, и они бы смешались с основными."""
    counts: dict[str, dict[int, int]] = {}
    lines: dict[tuple[str, int], list[float]] = {}
    for ev in events:
        if ev.scope:
            continue
        picks = ev.base_picks if ev.base_picks is not None else ev.picks
        seen: set[tuple[str, int]] = set()
        for g, t, p, _c in picks:
            family = FAMILY_OF.get(t)
            if family is None:
                continue
            seen.add((family, g))
            if p is not None:
                lines.setdefault((family, g), []).append(abs(p))
        for family, g in seen:
            per_group = counts.setdefault(family, {})
            per_group[g] = per_group.get(g, 0) + 1

    groups: dict[str, int | None] = {}
    stats: dict[str, list[tuple[int, int, float]]] = {}
    for family, per_group in counts.items():
        rows = []
        for g, n in per_group.items():
            ln = lines.get((family, g)) or [0.0]
            rows.append((g, n, float(median(ln))))
        # Больше событий — важнее; при равном покрытии берём группу с
        # большей линией: тотал матча всегда выше тотала тайма.
        rows.sort(key=lambda r: (r[1], r[2]), reverse=True)
        stats[family] = rows
        groups[family] = rows[0][0] if rows else None
    for family in FAMILY_CODES:
        groups.setdefault(family, None)
    return groups, stats


# ---------------------------------------------------------------------------
# Голосование за стороны исходов
# ---------------------------------------------------------------------------


def _collect_votes(events: list[RawEvent],
                   groups: dict[str, int | None]) -> dict[str, tuple[int, int]]:
    votes = {
        TOTAL: [0, 0],
        HCAP: [0, 0],
        ITOTAL1: [0, 0],
        ITOTAL2: [0, 0],
        ITOTAL_SIDES: [0, 0],
    }
    for ev in events:
        if ev.scope:
            continue    # подигры калибровку не решают: их рынки штучные
        _vote_ladder(ev, TOTAL, groups.get(TOTAL), votes[TOTAL])
        _vote_ladder(ev, ITOTAL1, groups.get(ITOTAL1), votes[ITOTAL1])
        _vote_ladder(ev, ITOTAL2, groups.get(ITOTAL2), votes[ITOTAL2])
        _vote_hcap(ev, groups, votes[HCAP])
        _vote_itotal_sides(ev, groups, votes[ITOTAL_SIDES])
    return {family: (v[0], v[1]) for family, v in votes.items()}


def _vote_ladder(ev: RawEvent, family: str, group: int | None,
                 tally: list[int]) -> None:
    """Монотонность лестницы: «больше» дорожает с ростом линии.

    Голос даёт КАЖДАЯ соседняя пара линий: у события с лестницей из
    десяти тоталов сомнений в направлении быть не может, а у события с
    единственной линией голоса нет вовсе."""
    if group is None:
        return
    over_code, under_code = FAMILY_CODES[family]
    sides = ev.lines(family, group)
    over, under = sides.get(over_code) or {}, sides.get(under_code) or {}
    common = sorted(p for p in over
                    if p is not None and p in under
                    and over[p] > 1 and under[p] > 1)
    for prev, nxt in zip(common, common[1:]):
        if over[nxt] > over[prev]:
            tally[0] += 1
        elif over[nxt] < over[prev]:
            tally[1] += 1
        if under[nxt] < under[prev]:
            tally[0] += 1
        elif under[nxt] > under[prev]:
            tally[1] += 1


def _vote_hcap(ev: RawEvent, groups: dict[str, int | None],
               tally: list[int]) -> None:
    """Знак «справедливой» форы: у фаворита она отрицательная.

    Справедливой считаем ту линию лестницы, где кэфы сторон ближе всего
    друг к другу, — именно там БК считает шансы равными."""
    group = groups.get(HCAP)
    if group is None:
        return
    odds = ev.winner_odds(groups.get(WINNER))
    if odds is None:
        return
    k1, k2 = odds
    if 1 / FAV_GAP < k2 / k1 < FAV_GAP:
        return          # соперники равны — знак линии ничего не скажет
    sides = ev.lines(HCAP, group)
    first = sides.get(T_HCAP1) or {}
    second = sides.get(T_HCAP2) or {}
    best: tuple[float, float] | None = None
    for line, ka in first.items():
        if line is None or line == 0 or ka <= 1:
            continue
        kb = second.get(-line)
        if not kb or kb <= 1:
            continue
        gap = abs(ka - kb)
        if best is None or gap < best[0]:
            best = (gap, line)
    if best is None:
        return
    home_is_favourite = k1 < k2
    if (best[1] < 0) == home_is_favourite:
        tally[0] += 1
    else:
        tally[1] += 1


def _vote_itotal_sides(ev: RawEvent, groups: dict[str, int | None],
                       tally: list[int]) -> None:
    """Чей индивидуальный тотал выше: у фаворита забитых больше."""
    g1, g2 = groups.get(ITOTAL1), groups.get(ITOTAL2)
    if g1 is None or g2 is None:
        return
    odds = ev.winner_odds(groups.get(WINNER))
    if odds is None:
        return
    k1, k2 = odds
    if 1 / FAV_GAP < k2 / k1 < FAV_GAP:
        return
    m1 = _median_line(ev, ITOTAL1, g1)
    m2 = _median_line(ev, ITOTAL2, g2)
    if m1 is None or m2 is None or abs(m1 - m2) < 0.25:
        return
    home_is_favourite = k1 < k2
    tally[0 if (m1 > m2) == home_is_favourite else 1] += 1


def _median_line(ev: RawEvent, family: str, group: int) -> float | None:
    values = [p for _t, per_line in ev.lines(family, group).items()
              for p in per_line if p is not None]
    return float(median(values)) if values else None


def _home_edge(events: list[RawEvent], group: int | None) -> float | None:
    """Средний перевес хозяев по линии — контрольная цифра для диагностики."""
    if group is None:
        return None
    diffs = []
    for ev in events:
        if ev.scope:
            continue
        odds = ev.winner_odds(group)
        if odds is None:
            continue
        k1, k2 = odds
        diffs.append(1 / k1 - 1 / k2)
    if len(diffs) < MIN_VOTES:
        return None
    return round(sum(diffs) / len(diffs), 4)
