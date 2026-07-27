"""Точечная диагностика конкретного матча/рынка — печатает СЫРЫЕ market/
market_key всех БК для события, чтобы разобрать конкретную подозрительную
вилку (например, если одна БК на самом деле даёт 1-й тайм, а другая —
весь матч, но они почему-то попали в один market_key).

Запускать на сервере с российским IP:

    python -m app.debug_market София Трнава
    python -m app.debug_market KBO

Ищет события, где ВСЕ переданные подстроки (без учёта регистра) находятся
в строке "<sport> <team1> <team2>". Печатает по каждой найденной БК все
строки кэфов этого события: market (человекочитаемое имя), market_key
(ключ сопоставления вилок) и k1/k2. Совпадающий market_key у РАЗНЫХ БК —
это то, что движок вилок считает «одним рынком»; если по смыслу это
разные рынки (разные периоды/предметы) — баг в market_scope/парсере.
"""
import sys
from collections import defaultdict

from .models import KIND_LIVE
from .parsers import get_parsers
from .parsers.html_utils import format_start


def main() -> None:
    needles = [a.lower() for a in sys.argv[1:]]
    if not needles:
        print("Использование: python -m app.debug_market <подстрока1> "
              "[подстрока2] ...")
        return

    all_odds = []
    for parser in get_parsers():
        all_odds.extend(parser.safe_fetch())
        all_odds.extend(parser.safe_fetch_live())

    by_event: dict[tuple, list] = defaultdict(list)
    for o in all_odds:
        hay = f"{o.sport} {o.team1} {o.team2}".lower()
        if all(n in hay for n in needles):
            key = (o.kind, o.bookmaker, o.team1, o.team2, o.start_ts)
            by_event[key].append(o)

    if not by_event:
        print("Ничего не найдено — проверьте подстроки/что матч ещё в линии.")
        return

    for (kind, bk, t1, t2, ts), odds in sorted(by_event.items()):
        kind_lbl = "LIVE" if kind == KIND_LIVE else "прематч"
        when = format_start(ts) if ts else "?"
        print("=" * 100)
        print(f"{bk} [{kind_lbl}] {odds[0].sport}: {t1} — {t2}  ({when})")
        print("-" * 100)
        for o in sorted(odds, key=lambda x: x.market_key):
            print(f"  market_key={o.market_key:<28} market={o.market!r:<40} "
                  f"k1={o.k1} k2={o.k2} k3={o.k3}")


if __name__ == "__main__":
    main()
