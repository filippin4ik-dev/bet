"""Диагностика парсера Melbet — запускать на сервере, где крутится сканер.

    cd /opt/arb-scanner && venv/bin/python -m app.diagnose_melbet

Запускать нужно именно интерпретатором из venv: зависимости сканера
ставятся туда, и системный python свалится на первом же импорте.

Фид Melbet не подписывает рынки словами: у исхода только номер группы и
код («{"G": 17, "T": 9, "C": 1.85, "P": 2.5}»), а словарь кодов БК не
публикует. Парсер поэтому не верит кодам на слово, а проверяет их по
самим данным (см. app/parsers/melbet_layout.py). Этот скрипт показывает,
что именно приехало с сайта и к каким выводам пришла проверка:

- какая база фида ответила, а если ни одна — что именно ответило каждое
  зеркало (403/406 «адрес не пускают» и 404 «не тот путь» лечатся
  по-разному);
- сколько видов спорта, чемпионатов и событий в линии;
- какие пары «группа + код исхода» встречаются и как часто — по этой
  таблице видно, если БК поменяла нумерацию;
- как разложились рынки и с каким счётом голосов;
- пример события с готовыми котировками — их и увидит сканер.

Отдельный режим — подбор ссылки на страницу матча:

    cd /opt/arb-scanner && venv/bin/python -m app.diagnose_melbet --links

Формат адреса у площадок движка разный (у melbet.ru — внутренний маршрут
приложения, у международных зеркал — SEO-адрес), да и номера событий у них
свои. Угадать снаружи нельзя: melbet.ru отвечает только российским
адресам. Поэтому режим строит варианты ссылки на живых событиях, дёргает
каждый и показывает, какой из них действительно открыл матч.
"""
import logging
import sys
import time
from collections import Counter

import requests

from .config import (MELBET_EVENT_URL, MELBET_GROUPS, MELBET_SITE_HOST_SET)
from .parsers.melbet import (EVENT_URL_DEFAULT, EVENT_URL_SEO, MelbetParser,
                             _picks, _subgame_event, _subgame_scope,
                             _subgames, _team, event_url_parts, site_host)
from .parsers.melbet_layout import FAMILY_OF, RawEvent, detect

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s %(name)s: %(message)s")

# Сколько чемпионатов первого вида спорта (это футбол) брать в выборку.
# Выборка нарочно «узкая и густая», а не по верхушке всех видов спорта:
# разметка рынков проверяется по повторяющимся закономерностям линии, и на
# сотне матчей одного вида она подтверждается так же, как на полной линии,
# а на пёстрой выборке из всех видов — нет.
SAMPLE_CHAMPS = 80
# Столько событий берём с вида спорта, если чемпионаты не пришли.
SAMPLE_PER_SPORT = 50
# По скольким событиям смотреть полную роспись (по одному запросу на
# событие — больше и не нужно, чтобы увидеть лестницы и подигры).
SAMPLE_FULL = 15


def main() -> None:
    if "--links" in sys.argv[1:]:
        check_links()
        return
    print("=" * 64)
    print("ДИАГНОСТИКА ПАРСЕРА MELBET")
    print("=" * 64)

    parser = MelbetParser()
    # force: перебор зеркал нужен здесь всегда, даже если сканер в этом же
    # процессе только что его провалил и досиживает паузу.
    base = parser._resolve_base(force=True)
    if not base:
        print("Фид не ответил ни на одном зеркале:")
        for host, note in parser.probe_notes:
            print(f"  {host} → {note}")
        print("«отключите VPN» — зеркало не пускает адрес этого сервера "
              "(melbet.ru так отвечает и IP дата-центров). Лечится другим "
              "зеркалом: международные .com/.org отдают линию и с "
              "датацентрового адреса — допишите их в MELBET_HOSTS.")
        print("403/406 без этой страницы — запрос отклонил сам фид: либо "
              "адрес, либо строка запроса не та.")
        print("404 — путь не тот: домен живой, но эндпоинты лежат по другому "
              "префиксу.")
        print("В любом случае базу можно задать вручную: "
              "MELBET_API_HOST=https://<домен>/service-api")
        return
    print(f"База фида: {base}")
    # Перебор останавливается на первом рабочем зеркале, поэтому в
    # probe_notes лежат только те, что до него не дошли. Остальные
    # опрашиваем отдельно: когда линии нет, первый вопрос — какие
    # площадки вообще пускают этот сервер.
    print("Зеркала:")
    for host, note in parser.probe_notes:
        print(f"  {host} → {note}")
    for host in parser._candidates():
        if host not in {h for h, _n in parser.probe_notes}:
            print(f"  {host} → {parser._probe(host) or 'линия отдана'}")

    sports = parser._sports(base, "LineFeed")
    print(f"Видов спорта: {len(sports)}")

    champs = parser._champs(base, "LineFeed", sports[:1],
                            time.monotonic() + 30)
    if champs:
        print(f"Чемпионатов у первого вида спорта: {len(champs)}, событий в "
              f"них: {sum(gc for _li, gc in champs)}")
        sample = [("champs", li) for li, _gc in champs[:SAMPLE_CHAMPS]]
    else:
        print("Чемпионаты не пришли — линия будет собираться только по "
              "видам спорта, то есть верхушкой (до 50 событий на вид).")
        sample = [("sports", sid) for sid in sports]

    games: dict = {}
    for key, value in sample:
        try:
            chunk = parser._chunk_games(base, "LineFeed", key, value,
                                        SAMPLE_PER_SPORT)
        except Exception as exc:  # noqa: BLE001
            print(f"  {key}={value}: ошибка {exc}")
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
    subgames, parsed_subgames, names = 0, [], []
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
        # Рынки подигры лежат за отдельным запросом — её собственным
        # GetGameZip; сколько подигр берёт сканер, задаёт MELBET_SUBGAMES.
        for sub in _subgames(full):
            try:
                sub_full = parser._game_zip(base, "LineFeed", sub.get("I"))
            except Exception as exc:  # noqa: BLE001
                print(f"  подигра {sub.get('I')}: ошибка {exc}")
                continue
            parsed = _subgame_event(ev, {**sub, **(sub_full or {})})
            if parsed is not None:
                parsed_subgames.append(parsed)
                names.append(_subgame_scope(sub))
    print(f"Подигр в росписи: {subgames}, забрано с рынками: "
          f"{len(parsed_subgames)}")
    if names:
        print("  области рынков подигр:", ", ".join(sorted(set(names))[:12]))
    events += parsed_subgames

    _print_codes(events)

    print("-" * 64)
    layout = detect(events, forced=MELBET_GROUPS)
    if MELBET_GROUPS:
        print("Группы закреплены вручную (MELBET_GROUPS):", MELBET_GROUPS)
    print("Раскладка рынков:", layout.summary())
    print("Перевес хозяев по линии (должен быть > 0):", layout.home_edge)
    for family, rows in sorted(layout.group_stats.items()):
        pretty = ", ".join(f"G{g}: {n} соб., медиана линии {m:g}"
                           for g, n, m in rows[:5])
        print(f"  {family:<9} {pretty}")
    if len(layout.group_stats.get("total", [])) > 1:
        print("  (если выбрана не та группа — закрепите нужную вручную: "
              "MELBET_GROUPS=total=17,hcap=2)")

    print("-" * 64)
    odds = []
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


# Варианты формата ссылки на матч. Точный формат снаружи не угадать: у
# melbet.ru и международных зеркал разные и маршруты, и пространства
# номеров, а сама melbet.ru отвечает только с российского адреса — поэтому
# варианты проверяются здесь, на живом сайте, с вашего сервера.
LINK_CANDIDATES = [
    ("внутренний маршрут сайта (формат melbet.ru)", EVENT_URL_DEFAULT),
    ("то же, но номер события из фида (I вместо CI)",
     "/ru/sport/event-details/"
     "{champ}-{game_feed}-{country}-{sport}-0-{live}-0-{teams}"),
    ("то же, но чемпионат и страна местами",
     "/ru/sport/event-details/"
     "{country}-{game}-{champ}-{sport}-0-{live}-0-{teams}"),
    ("SEO-адрес (формат melbet.com / melbet.mobi)", EVENT_URL_SEO),
    ("SEO-адрес с номером события из фида (I)",
     "/ru/{section}/{sport_slug}/{champ}-{champ_slug}/{game_feed}-{teams}"),
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def check_links() -> None:
    """Подбирает формат ссылки на страницу матча прямо на живом сайте.

    Запускать на сервере сканера: melbet.ru пускает только российские
    адреса, из песочницы разработки её страницы не открываются вовсе."""
    print("=" * 64)
    print("ПОДБОР ССЫЛКИ НА СТРАНИЦУ МАТЧА (MELBET)")
    print("=" * 64)

    parser = MelbetParser()
    base = parser._resolve_base(force=True)
    if not base:
        print("Фид не ответил ни на одном зеркале — сначала разберитесь с "
              "этим: python -m app.diagnose_melbet")
        return
    host = site_host(base)
    print(f"База фида: {base}")
    print(f"Хост ссылок: {host}"
          + ("  (задан в MELBET_SITE_HOST)" if MELBET_SITE_HOST_SET
             else "  (взят от базы фида)"))
    if not base.startswith(host):
        print("\n!!! ВНИМАНИЕ: линия приходит с одной площадки, а ссылки "
              "ведут на другую. Номера событий, чемпионатов и стран у них "
              "не совпадают, и ссылка не откроется НИ В КАКОМ формате: "
              "сайт просто не знает таких номеров. Уберите "
              "MELBET_SITE_HOST — тогда ссылки пойдут туда же, откуда "
              "линия, — либо задайте MELBET_API_HOST под нужный сайт.\n")

    games = _link_sample(parser, base)
    if not games:
        print("В линии не нашлось событий — смотреть нечего.")
        return

    session = requests.Session()
    session.headers["User-Agent"] = UA
    for game, live in games:
        title = f"{_team(game, 'O1')} — {_team(game, 'O2')}"
        print("-" * 64)
        print(f"{title} ({'лайв' if live else 'прематч'}, "
              f"{game.get('L') or '—'})")
        parts = event_url_parts(game, live)
        print("  номера из фида: " + ", ".join(
            f"{k}={parts[k]}" for k in ("champ", "country", "sport", "game",
                                        "game_feed")))
        for name, template in LINK_CANDIDATES:
            url = host + template.format(**parts)
            print(f"  · {name}")
            print(f"    {url}")
            print(f"    {_probe_link(session, url, game)}")

    print("-" * 64)
    print("Что делать с результатом: если один из вариантов открыл матч, "
          "пропишите его шаблон в переменную окружения, например")
    print(f'  MELBET_EVENT_URL="{EVENT_URL_DEFAULT}"')
    current = MELBET_EVENT_URL or (
        (EVENT_URL_DEFAULT if host.endswith(".ru") else EVENT_URL_SEO)
        + "  (по умолчанию, по хосту ссылок)")
    print(f"и перезапустите сканер. Сейчас используется: {current}")
    print("Если ни один не открыл — откройте любой матч на сайте руками и "
          "пришлите адрес из строки браузера вместе с этой распечаткой: по "
          "номерам выше видно, из каких полей он собирается.")


def _link_sample(parser: MelbetParser, base: str) -> list[tuple[dict, bool]]:
    """По одному событию из прематча и из лайва: маршруты у них разные."""
    out: list[tuple[dict, bool]] = []
    for feed, live in (("LineFeed", False), ("LiveFeed", True)):
        try:
            games = parser._games(base, feed, "sports", parser._sports(
                base, feed)[:1], 50, time.monotonic() + 30)
        except Exception as exc:  # noqa: BLE001
            print(f"  {feed}: не получилось взять события ({exc})")
            continue
        for game in games.values():
            if _team(game, "O1") and _team(game, "O2"):
                out.append((game, live))
                break
    return out


def _probe_link(session: requests.Session, url: str, game: dict) -> str:
    """Что отвечает сайт: открылась страница матча или что-то другое."""
    try:
        resp = session.get(url, timeout=20, allow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        return f"не открылось: {type(exc).__name__}"
    text = resp.content.decode("utf-8", "replace").lower()
    if "отключите vpn" in text or "cloudflare_error_1000s_box" in text:
        return ("адрес сервера сайт не пускает («отключите VPN») — проверку "
                "надо гонять с российского IP")
    if resp.url.rstrip("/").endswith("/block"):
        return (f"сайт увёл на заглушку {resp.url} — этот адрес он не "
                "обслуживает для вашей страны, проверить формат по нему "
                "нельзя")
    names = [n.lower() for n in (_team(game, "O1"), _team(game, "O2"),
                                 game.get("O1E") or "", game.get("O2E") or "")
             if n]
    found = [n for n in names if n in text]
    where = "" if resp.url == url else f", увёл на {resp.url}"
    if found:
        return (f"HTTP {resp.status_code}, {len(resp.content)} байт{where} — "
                f"МАТЧ НА СТРАНИЦЕ (нашлось: {', '.join(found[:2])})")
    return (f"HTTP {resp.status_code}, {len(resp.content)} байт{where} — "
            "команд на странице нет (либо не тот адрес, либо страница "
            "рисуется скриптом: откройте её в браузере)")


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
