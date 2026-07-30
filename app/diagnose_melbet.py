"""Диагностика парсера Melbet — запускать на сервере, где крутится сканер.

    python -m app.diagnose_melbet

Фид Melbet не подписывает рынки словами: у исхода только номер группы и
код («{"G": 17, "T": 9, "C": 1.85, "P": 2.5}»), а словарь кодов БК не
публикует. Парсер поэтому не верит кодам на слово, а проверяет их по
самим данным (см. app/parsers/melbet_layout.py). Этот скрипт показывает,
что именно приехало с сайта и к каким выводам пришла проверка:

- какая база фида ответила (и ответила ли вообще — доступ только с
  российского адреса);
- сколько видов спорта и событий в линии;
- какие пары «группа + код исхода» встречаются и как часто — по этой
  таблице видно, если БК поменяла нумерацию;
- как разложились рынки и с каким счётом голосов;
- пример события с готовыми котировками — их и увидит сканер.
"""
import logging
from collections import Counter

from .parsers.melbet import MelbetParser, _picks, _subgame_events
from .parsers.melbet_layout import FAMILY_OF, RawEvent, detect

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s %(name)s: %(message)s")

# Сколько событий брать на вид спорта: диагностике хватает верхушки
# линии, а сервер БК не стоит грузить полным обходом ради проверки.
SAMPLE_PER_SPORT = 50
# По скольким событиям смотреть полную роспись (по одному запросу на
# событие — больше и не нужно, чтобы увидеть лестницы и подигры).
SAMPLE_FULL = 15


def main() -> None:
    print("=" * 64)
    print("ДИАГНОСТИКА ПАРСЕРА MELBET")
    print("=" * 64)

    parser = MelbetParser()
    base = parser._resolve_base()
    if not base:
        print("Фид не ответил ни на одном зеркале.")
        print("Обычная причина — не российский IP: с адресов дата-центров и "
              "из-за границы сайт отдаёт 406 или страницу «отключите VPN».")
        print("Если домен просто сменился, задайте его вручную: "
              "MELBET_API_HOST=https://<домен>/service-api")
        return
    print(f"База фида: {base}")

    sports = parser._sports(base, "LineFeed")
    print(f"Видов спорта: {len(sports)}")

    games: dict = {}
    for sport_id in sports or [None]:
        try:
            chunk = parser._sport_games(base, "LineFeed", sport_id,
                                        SAMPLE_PER_SPORT)
        except Exception as exc:  # noqa: BLE001
            print(f"  вид спорта {sport_id}: ошибка {exc}")
            continue
        for game in chunk:
            gid = game.get("I")
            if gid:
                games[gid] = game
    print(f"Событий в выборке: {len(games)}")
    if not games:
        print("Линия пуста — смотреть дальше нечего.")
        return

    events = [RawEvent(game=g, picks=_picks(g)) for g in games.values()]
    for ev in events:
        ev.base_picks = ev.picks

    print("-" * 64)
    print(f"Полная роспись: беру {SAMPLE_FULL} событий")
    subgames, parsed_subgames = 0, []
    for ev in events[:SAMPLE_FULL]:
        try:
            full = parser._game_zip(base, "LineFeed", ev.game.get("I"))
        except Exception as exc:  # noqa: BLE001
            print(f"  событие {ev.game.get('I')}: ошибка {exc}")
            continue
        if not full:
            continue
        picks = _picks(full)
        if picks:
            ev.game = {**ev.game, **full}
            ev.picks = picks
        subgames += len(full.get("SG") or [])
        parsed_subgames.extend(_subgame_events(ev, full))
    print(f"Подигр (таймы/периоды/сеты): {subgames}, из них с понятным "
          f"периодом: {len(parsed_subgames)}")
    events += parsed_subgames

    _print_codes(events)

    print("-" * 64)
    layout = detect(events)
    print("Раскладка рынков:", layout.summary())
    print("Перевес хозяев по линии (должен быть > 0):", layout.home_edge)
    for family, rows in sorted(layout.group_stats.items()):
        pretty = ", ".join(f"G{g}: {n} соб., медиана линии {m:g}"
                           for g, n, m in rows[:5])
        print(f"  {family:<9} {pretty}")

    print("-" * 64)
    odds = []
    import time
    now = time.time()
    for ev in events:
        odds.extend(parser._event_odds(ev, layout, now, False))
    by_kind = Counter(o.market_key.split(":")[0] for o in odds)
    print(f"Котировок из выборки: {len(odds)}")
    for kind, count in by_kind.most_common():
        print(f"  {kind:<10} {count}")
    if odds:
        print("-" * 64)
        first = odds[0]
        print(f"Пример события: {first.sport} | {first.team1} — "
              f"{first.team2} | старт {first.start_time}")
        print(f"  ссылка: {first.url}")
        same = [o for o in odds if o.event_key == first.event_key][:15]
        for o in same:
            third = f" / {o.outcome3} {o.k3}" if o.k3 else ""
            print(f"  {o.market:<28} {o.outcome1} {o.k1} / "
                  f"{o.outcome2} {o.k2}{third}")
    else:
        print("Ни одной котировки: разметка рынков не подтвердилась — "
              "смотрите таблицу кодов выше и предупреждения в логе.")


def _print_codes(events: list[RawEvent]) -> None:
    """Таблица «группа + код исхода»: сколько событий и какие линии."""
    per_pair: dict[tuple[int, int], list] = {}
    for ev in events:
        seen = set()
        for group, code, line, _coef in ev.picks:
            key = (group, code)
            entry = per_pair.setdefault(key, [0, 0, set()])
            entry[1] += 1                       # исходов
            if line is not None:
                entry[2].add(line)
            if key not in seen:
                seen.add(key)
                entry[0] += 1                   # событий
    print("-" * 64)
    print("Пары «группа + код исхода» (сверху самые массовые):")
    print(f"  {'G':>5} {'T':>4} {'событий':>8} {'исходов':>8}  "
          f"{'семейство':<9} линии")
    rows = sorted(per_pair.items(), key=lambda kv: kv[1][0], reverse=True)
    for (group, code), (events_n, picks_n, lines) in rows[:30]:
        family = FAMILY_OF.get(code, "—")
        shown = ""
        if lines:
            ordered = sorted(lines)
            shown = f"{ordered[0]:g} … {ordered[-1]:g} ({len(ordered)} шт.)"
        print(f"  {group:>5} {code:>4} {events_n:>8} {picks_n:>8}  "
              f"{family:<9} {shown}")


if __name__ == "__main__":
    main()
