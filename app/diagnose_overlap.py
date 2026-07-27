"""Диагностика ПЕРЕСЕЧЕНИЯ событий/рынков между БК — запускать на сервере,
где крутится сканер (нужен российский IP, иначе см. app.diagnose).

    python -m app.diagnose_overlap

Мало вилок может объясняться не ошибкой поиска, а тем, что события/рынки
разных БК просто не совпадают: разное покрытие лиг, расхождение в
написании имён команд (норм. не сработала), или у совпавшего события нет
общих рынков (одна БК прислала только «Победитель», без тоталов/фор).

Скрипт показывает:
1. Сколько уникальных событий нашла каждая БК.
2. Матрицу пересечений событий по парам БК (сколько событий видят ОБЕ).
3. Разбивку по видам спорта: сколько событий и какая доля из них
   СОВПАЛА (2+ БК на событие) — если у популярного спорта (футбол/хоккей)
   доля совпадения намного ниже, чем у остальных, это сигнал бага
   сопоставления именно для него, а не общая нехватка вилок.
4. Среди событий с 2+ БК — сколько из них имеют хотя бы 1 общий
   двухисходный рынок (без этого вилка невозможна даже при совпавшем матче).
5. «Почти совпадения»: события, которые НЕ склеились в одно, хотя у них
   близкое время старта и похожие (но не идентичные после нормализации)
   имена команд — кандидаты на баг в norm_team/парсинге имени.

ВАЖНО: низкая доля вилок именно в футболе/хоккее/баскетболе при ВЫСОКОЙ
доле совпадения событий — это, скорее всего, НЕ баг, а реальная
эффективность рынка: это самые ходовые линии, букмекеры следят за ними
внимательнее и держат кэфы ближе друг к другу. Вилки чаще находятся в
нишевых рынках/спорта (тоталы по сетам в волейболе, небольшие турниры
киберспорта) — там маржа между БК расходится сильнее.
"""
import difflib
import logging
from collections import defaultdict
from itertools import combinations

from .arbitrage import _market_group, find_arbs, norm_team
from .models import KIND_PREMATCH
from .parsers import get_parsers
from .scanner import Scanner

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s %(name)s: %(message)s")

# Порог похожести имён (0..1, difflib.SequenceMatcher.ratio) для «почти
# совпадений» и допуск времени старта, сек.
_NAME_SIM_THRESHOLD = 0.6
_NEAR_TS_TOLERANCE = 3 * 3600
_MAX_NEAR_MISSES = 40


def _team_pair_similarity(a: tuple[str, str], b: tuple[str, str]) -> float:
    """Похожесть двух пар имён команд (порядок не важен) — среднее по
    лучшему сопоставлению команда↔команда."""
    ratios = [
        (difflib.SequenceMatcher(None, a[0], b[0]).ratio()
         + difflib.SequenceMatcher(None, a[1], b[1]).ratio()) / 2,
        (difflib.SequenceMatcher(None, a[0], b[1]).ratio()
         + difflib.SequenceMatcher(None, a[1], b[0]).ratio()) / 2,
    ]
    return max(ratios)


def main() -> None:
    print("=" * 72)
    print("ДИАГНОСТИКА ПЕРЕСЕЧЕНИЯ СОБЫТИЙ/РЫНКОВ МЕЖДУ БК")
    print("=" * 72)

    all_odds = []
    for parser in get_parsers():
        odds = parser.safe_fetch()
        print(f"{parser.name:<12}: {len(odds)} котировок")
        all_odds.extend(odds)
    print("-" * 72)

    prematch = [o for o in all_odds if o.kind == KIND_PREMATCH]
    groups = Scanner._event_groups(prematch)
    print(f"Всего событий (прематч, сгруппировано): {len(groups)}")

    # ---------- 1+2. события на БК + матрица пересечений ----------
    books_per_event: dict[str, set[str]] = {}
    sample_per_event: dict[str, tuple] = {}
    for event_id, odds in groups.items():
        books = {o.bookmaker for o in odds}
        books_per_event[event_id] = books
        o0 = odds[0]
        sample_per_event[event_id] = (
            o0.sport, o0.team1, o0.team2, o0.start_ts)

    events_by_bk: dict[str, int] = defaultdict(int)
    for books in books_per_event.values():
        for bk in books:
            events_by_bk[bk] += 1

    print("-" * 72)
    print("Событий на каждую БК (сколько матчей нашла БК всего):")
    for bk, cnt in sorted(events_by_bk.items(), key=lambda x: -x[1]):
        print(f"  {bk:<12}: {cnt}")

    overlap: dict[tuple[str, str], int] = defaultdict(int)
    for books in books_per_event.values():
        for a, b in combinations(sorted(books), 2):
            overlap[(a, b)] += 1

    print("-" * 72)
    print("Пересечение событий по парам БК (видят ОБЕ одно и то же событие):")
    for (a, b), cnt in sorted(overlap.items(), key=lambda x: -x[1]):
        print(f"  {a:<12} x {b:<12}: {cnt}")

    only_one = sum(1 for b in books_per_event.values() if len(b) == 1)
    two_plus = len(books_per_event) - only_one
    print("-" * 72)
    print(f"Событий ТОЛЬКО у одной БК (вилка невозможна): {only_one}")
    print(f"Событий у 2+ БК (потенциально вилка возможна): {two_plus}")

    # ---------- 3. разбивка по видам спорта ----------
    events_by_sport: dict[str, int] = defaultdict(int)
    matched_by_sport: dict[str, int] = defaultdict(int)
    arbs_by_sport: dict[str, int] = defaultdict(int)
    for event_id, books in books_per_event.items():
        root = sample_per_event[event_id][0].split("·")[0].strip()
        events_by_sport[root] += 1
        if len(books) >= 2:
            matched_by_sport[root] += 1
    for a in find_arbs(prematch):
        arbs_by_sport[a.sport.split("·")[0].strip()] += 1

    print("-" * 72)
    print("Разбивка по видам спорта (события / из них совпало у 2+ БК / "
          "вилок):")
    print(f"  {'спорт':<28}{'событий':>9}{'совпало':>9}{'  %':>6}"
          f"{'вилок':>8}")
    for sport, total in sorted(events_by_sport.items(), key=lambda x: -x[1]):
        matched = matched_by_sport.get(sport, 0)
        pct = 100 * matched / total if total else 0.0
        arbs_n = arbs_by_sport.get(sport, 0)
        print(f"  {sport:<28}{total:>9}{matched:>9}{pct:>5.0f}%"
              f"{arbs_n:>8}")

    # ---------- 4. совпавшие события без общих рынков ----------
    no_common_market = 0
    for event_id, odds in groups.items():
        if len(books_per_event[event_id]) < 2:
            continue
        by_market: dict[str, set[str]] = defaultdict(set)
        for o in odds:
            by_market[_market_group(o)].add(o.bookmaker)
        if not any(len(bks) >= 2 for bks in by_market.values()):
            no_common_market += 1
    print(f"Из них БЕЗ общего рынка ни по одному (0 вилок гарантированно): "
          f"{no_common_market}")
    print(f"С хотя бы одним общим рынком (кандидаты на вилку): "
          f"{two_plus - no_common_market}")

    # ---------- 5. «почти совпадения» — вероятные баги нормализации ----------
    print("-" * 72)
    print("Ищу «почти совпадения» (похожие имена, близкое время старта, "
          "НЕ склеились в одно событие)...")
    singles = [(eid, *sample_per_event[eid])
               for eid, books in books_per_event.items() if len(books) == 1]
    near_misses = []
    for i, (eid_a, sport_a, t1a, t2a, tsa) in enumerate(singles):
        if tsa is None:
            continue
        root_a = sport_a.split("·")[0].strip().lower()
        pair_a = (norm_team(t1a), norm_team(t2a))
        for eid_b, sport_b, t1b, t2b, tsb in singles[i + 1:]:
            if tsb is None or eid_a.split("|")[0] != eid_b.split("|")[0]:
                continue
            if abs(tsa - tsb) > _NEAR_TS_TOLERANCE:
                continue
            root_b = sport_b.split("·")[0].strip().lower()
            if root_a != root_b:
                continue
            pair_b = (norm_team(t1b), norm_team(t2b))
            if pair_a == pair_b:
                continue  # не должно случиться (иначе склеились бы)
            sim = _team_pair_similarity(pair_a, pair_b)
            if sim >= _NAME_SIM_THRESHOLD:
                near_misses.append((sim, sport_a, t1a, t2a, t1b, t2b))
    near_misses.sort(key=lambda x: -x[0])
    if not near_misses:
        print("Не найдено — расхождение имён похоже не является причиной "
              "низкого пересечения (скорее разное покрытие лиг/событий).")
    else:
        print(f"Найдено {len(near_misses)} потенциальных пар (топ "
              f"{min(_MAX_NEAR_MISSES, len(near_misses))} по похожести):")
        for sim, sport, t1a, t2a, t1b, t2b in near_misses[:_MAX_NEAR_MISSES]:
            print(f"  [{sim:.2f}] {sport}: «{t1a} — {t2a}»  vs  "
                  f"«{t1b} — {t2b}»")

    print("=" * 72)


if __name__ == "__main__":
    main()
