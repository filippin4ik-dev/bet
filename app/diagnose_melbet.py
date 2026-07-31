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

Если ни одно зеркало не отдало линию, адрес фида ищется по самому сайту:

    cd /opt/arb-scanner && venv/bin/python -m app.diagnose_melbet --api

Этот режим показывает, что площадка отвечает вашему серверу на самом деле
(код, куда увела переадресация, заголовок страницы, защиту от ботов), а
потом достаёт адрес фида из скриптов сайта — там он записан ровно тот,
которым пользуется сама страница, — и проверяет найденное запросом.

Отдельный режим — подбор ссылки на страницу матча:

    cd /opt/arb-scanner && venv/bin/python -m app.diagnose_melbet --links

Формат адреса у площадок движка разный (у melbet.ru — внутренний маршрут
приложения, у международных зеркал — SEO-адрес), да и номера событий у них
свои. Угадать снаружи нельзя: melbet.ru отвечает только российским
адресам. Поэтому режим строит варианты ссылки на живых событиях, дёргает
каждый и показывает, какой из них действительно открыл матч.
"""
import logging
import re
import sys
import time
from collections import Counter
from urllib.parse import urlsplit

import requests

from .config import (MELBET_EVENT_URL, MELBET_GROUPS, MELBET_SITE_HOST_SET)
from .parsers.melbet import (EVENT_URL_DEFAULT, EVENT_URL_SEO, MelbetParser,
                             _ANTIBOT_MARKERS, _page_title, _picks,
                             _subgame_event, _subgame_scope, _subgames, _team,
                             event_url_parts, feed_bases, script_urls,
                             site_host)
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
    if "--api" in sys.argv[1:]:
        find_api()
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


# Скриптов смотрим больше, чем сам парсер: здесь мы разбираемся руками, и
# лишняя минута дешевле, чем ещё один заход с вопросами.
MAX_SCRIPTS = 12
MAX_SCRIPT_BYTES = 12 * 1024 * 1024


def find_api() -> None:
    """Ищет живой адрес фида: спрашивает сам сайт и читает его скрипты.

    Запускать на сервере сканера. Список зеркал в настройках стареет: домены
    движка переезжают, а с российского адреса международные площадки ещё и
    недоступны — тогда единственный источник правды это сама страница."""
    print("=" * 64)
    print("ПОИСК АДРЕСА ФИДА MELBET")
    print("=" * 64)

    parser = MelbetParser()
    session = requests.Session()
    session.headers["User-Agent"] = UA

    print("Что отвечает каждая площадка на запрос фида:")
    for base in parser._candidates():
        print(f"  {base}\n      {parser._probe(base) or 'ЛИНИЯ ОТДАНА'}")

    found: list[str] = []
    alive: list[str] = []
    for host in _api_hosts(parser):
        print("-" * 64)
        page = _fetch_site(session, host)
        if page is None:
            continue
        alive.append(host)
        for base in _bases_from_scripts(session, host, page):
            if base not in found:
                found.append(base)

    print("-" * 64)
    if not found:
        print("Фида движка 1xBet на площадке нет. Возможно, она переехала на "
              "другую платформу — смотрю, что там.")
        for host in alive:
            probe_platform(session, host)
        print("-" * 64)
        print("Пришлите распечатку целиком: по адресам и ответам выше видно, "
              "какой платформой сайт пользуется и откуда берёт линию.")
        return
    print("Адреса фида, зашитые в скриптах сайта (их зовёт сама страница):")
    working = []
    for base in found:
        note = parser._probe(base)
        print(f"  {base}\n      {note or 'ЛИНИЯ ОТДАНА'}")
        if note is None:
            working.append(base)
    print("-" * 64)
    if working:
        print("Пропишите рабочий адрес в переменную окружения и "
              "перезапустите сканер:")
        print(f"  MELBET_API_HOST={working[0]}")
    else:
        print("Ни один найденный адрес линию не отдал — пришлите распечатку, "
              "разберём по ней.")


# Что спрашиваем у площадки, если фида движка 1xBet на ней нет. Настройки
# сайта отдают адреса, которыми пользуется сама страница (в загрузчике
# melbet.ru этот путь так и остался в комментарии), а спортбук на таких
# сборках живёт отдельным приложением на поддомене sport.<домен>.
PLATFORM_PATHS = ("/siteapi/ProjectSettings/GetSettings",
                  "/siteapi2/ProjectSettings/GetSettings")
# Загрузчик виджета, настройки партнёра (в них адреса, которыми пользуется
# сам спортбук) и страница приложения — с неё тянутся бандлы из /sportcdn.
SPORTSBOOK_PATHS = ("/js/partner/bootstrapper.min.js",
                    "/StaticContent/GlobalSettings.js", "/")
_URL_RE = re.compile(r"https?://[\w.-]+(?:/[\w./-]*)?")
_API_PATH_RE = re.compile(r"[\"'](/[\w./-]*[Aa][Pp][Ii][\w./-]*)[\"']")
_SPORT_PATH_RE = re.compile(
    r"[\"'](/[\w/-]*(?:[Ss]port|[Cc]hamp|[Ee]vent|[Oo]dds|[Pp]rematch"
    r"|[Ll]ive|[Tt]ournament)[\w/-]*)[\"']")


def probe_platform(session: requests.Session, host: str) -> None:
    """Разведка «не 1xBet» площадки: настройки сайта и приложение спортбука."""
    print("-" * 64)
    print(f"{host}: настройки платформы")
    for path in PLATFORM_PATHS:
        _dump(session, host + path)

    parts = urlsplit(host)
    sport = f"{parts.scheme}://sport.{parts.netloc}"
    print(f"{sport}: спортбук (обычно он отдельным приложением во фрейме)")
    for path in SPORTSBOOK_PATHS:
        body = _dump(session, sport + path)
        if not body:
            continue
        _print_set("ссылки", _URL_RE.findall(body), 20)
        _print_set("пути с api", _API_PATH_RE.findall(body), 20)
        for url in script_urls(body, sport, 6):
            script = _dump(session, url, quiet=True)
            if not script:
                continue
            print(f"    скрипт {url.rsplit('/', 1)[-1][:50]}")
            _print_set("пути с api", _API_PATH_RE.findall(script), 15)
            _print_set("пути про спорт", _SPORT_PATH_RE.findall(script), 15)


def _dump(session: requests.Session, url: str,
          quiet: bool = False) -> str | None:
    """Запрашивает адрес и коротко показывает, что пришло."""
    try:
        resp = session.get(url, timeout=25, allow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  {url}\n      не открылось: {type(exc).__name__}")
        return None
    body = resp.content[:MAX_SCRIPT_BYTES].decode(
        resp.apparent_encoding or "utf-8", "replace")
    if not quiet:
        kind = resp.headers.get("Content-Type", "?").split(";")[0]
        title = _page_title(body)
        head = "" if title else " ".join(body[:600].split())
        print(f"  {url}\n      HTTP {resp.status_code}, {kind}, "
              f"{len(resp.content)} байт"
              + (f", «{title}»" if title else "")
              + (f"\n      начало: {head}" if head else ""))
    return body


def _print_set(label: str, values: list[str], limit: int) -> None:
    uniq = sorted(set(values))
    if not uniq:
        return
    print(f"      {label} ({len(uniq)}): " + ", ".join(uniq[:limit])
          + (" …" if len(uniq) > limit else ""))


def _api_hosts(parser: MelbetParser) -> list[str]:
    """Хосты площадок без префикса пути — по ним и ходим за страницей."""
    hosts: list[str] = []
    for base in parser._candidates():
        parts = urlsplit(base)
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in hosts:
            hosts.append(root)
    return hosts


def _fetch_site(session: requests.Session, host: str) -> str | None:
    """Забирает главную страницу и рассказывает, что это за ответ."""
    try:
        resp = session.get(host + "/", timeout=20, allow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        print(f"{host} — не открылась: {type(exc).__name__}")
        return None
    body = resp.content[:200_000].decode(resp.apparent_encoding or "utf-8",
                                         "replace")
    title = _page_title(body)
    print(f"{host} — HTTP {resp.status_code}, {len(resp.content)} байт"
          + (f", заголовок «{title}»" if title else ""))
    if resp.url.rstrip("/") != host:
        print(f"    переадресация на {resp.url}")
    low = body.lower()
    for marker, guard in _ANTIBOT_MARKERS:
        if marker in low:
            print(f"    похоже на страницу защиты {guard}")
            break
    if resp.cookies:
        print(f"    куки: {', '.join(sorted(resp.cookies.keys()))[:120]}")
    return body


def _bases_from_scripts(session: requests.Session, host: str,
                        page: str) -> list[str]:
    """Скачивает скрипты страницы и вынимает из них базу фида."""
    urls = script_urls(page, host, MAX_SCRIPTS)
    print(f"    скриптов смотрю: {len(urls)}")
    bases: list[str] = []
    budget = MAX_SCRIPT_BYTES
    for url in urls:
        try:
            resp = session.get(url, timeout=20)
        except Exception:  # noqa: BLE001
            continue
        text = resp.content[:budget].decode("utf-8", "replace")
        budget -= len(resp.content)
        name = url.rsplit("/", 1)[-1][:40]
        for base in feed_bases(text, host):
            if base not in bases:
                bases.append(base)
                print(f"    нашёл в {name}: {base}")
        # Адрес может собираться из переменной — тогда регулярка его не
        # достанет. Печатаем сырые куски вокруг вызова фида: по ним видно,
        # из чего он складывается.
        for snippet in _feed_snippets(text):
            print(f"    в {name}: …{snippet}…")
        if budget <= 0:
            break
    return bases


def _feed_snippets(text: str, limit: int = 3) -> list[str]:
    """Куски скрипта вокруг обращения к фиду — по одному на место."""
    out: list[str] = []
    for m in re.finditer(r"(?:Line|Live)Feed", text):
        piece = text[max(0, m.start() - 90):m.end() + 40]
        piece = " ".join(piece.split())
        if piece not in out:
            out.append(piece)
        if len(out) >= limit:
            break
    return out


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
