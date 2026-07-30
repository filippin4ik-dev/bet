"""Тесты разбора линии Melbet (app/parsers/melbet.py) — офлайн, на
синтетических ответах в формате реального фида движка 1xBet, без сети.

Главное, что здесь проверяется, — не «парсер что-то вернул», а то, что он
НЕ верит кодам исходов на слово: при перевёрнутой нумерации он переворачивает
разметку сам, а при неразличимой — молчит вместо того, чтобы отдать рынок с
перепутанными сторонами (это была бы ложная вилка).
"""
import time

import requests

from app.parsers.melbet import (MelbetParser, _count, _is_geo_stub, _picks,
                                _subgame_event, _subgames)
from app.parsers.melbet_layout import (BTS, HCAP, ITOTAL_SIDES, TOTAL,
                                       RawEvent, detect)

NOW = time.time()
FUTURE = NOW + 7200

G_WINNER, G_TOTAL, G_HCAP, G_IT1, G_IT2 = 1, 17, 2, 15, 62
G_HALF_TOTAL = 63          # «чужая» группа тоталов (тайм) — не должна мешать
G_BTS = 19

# Лестница тотала: линия → (больше, меньше). Кэф «больше» растёт с линией.
TOTAL_LADDER = {2.0: (1.60, 2.30), 2.5: (1.90, 1.90), 3.0: (2.35, 1.60)}
# Те же лестницы, сдвинутые в низовой и в результативный матч: «центр»
# лестницы (там, где кэфы сторон равны) — это и есть ожидаемые голы.
LOW_TOTALS = {1.5: (1.60, 2.30), 2.0: (1.90, 1.90), 2.5: (2.35, 1.60)}
HIGH_TOTALS = {3.0: (1.60, 2.30), 3.5: (1.90, 1.90), 4.0: (2.35, 1.60)}
# «Обе забьют» (Да/Нет) — рынок без линии. В низовом матче «да» дороже,
# в результативном — дешевле; на этом и держится калибровка.
BTS_LOW = (2.30, 1.60)
BTS_HIGH = (1.55, 2.40)
# Фора первой команды: линия → (Ф1, Ф2). Равные кэфы на −1.0 — там БК
# считает шансы равными, значит фаворит первая команда.
HCAP_LADDER = {-1.5: (2.10, 1.70), -1.0: (1.90, 1.90), -0.5: (1.60, 2.30)}
IT1_LADDER = {1.5: (1.75, 2.05), 2.5: (2.40, 1.55)}
IT2_LADDER = {0.5: (1.70, 2.10), 1.5: (2.45, 1.52)}
# Первая команда — фаворит (кэф ниже), маржа рынка нормальная.
WINNER_ODDS = (1.60, 4.00, 5.00)


def _outcome(group, code, line, coef):
    out = {"G": group, "T": code, "C": coef}
    if line is not None:
        out["P"] = line
    return out


def _ladder_outcomes(group, codes, ladder, mirror_second=False):
    """Пара кодов на каждую линию лестницы.

    mirror_second — у форы линия второй стороны противоположна первой."""
    first, second = codes
    out = []
    for line, (k1, k2) in ladder.items():
        out.append(_outcome(group, first, line, k1))
        out.append(_outcome(group, second, -line if mirror_second else line,
                            k2))
    return out


def _game(gid=1, *, team1="Спартак", team2="Зенит", sport="Футбол",
          league="РПЛ", start=None, totals=TOTAL_LADDER,
          hcaps=HCAP_LADDER, it1=IT1_LADDER, it2=IT2_LADDER,
          winner=WINNER_ODDS, total_codes=(9, 10), total_group=G_TOTAL,
          bts=None, bts_line=None, extra=()):
    k1, kx, k2 = winner
    outcomes = [_outcome(G_WINNER, 1, None, k1),
                _outcome(G_WINNER, 2, None, kx),
                _outcome(G_WINNER, 3, None, k2)]
    if totals:
        outcomes += _ladder_outcomes(total_group, total_codes, totals)
    if hcaps:
        outcomes += _ladder_outcomes(G_HCAP, (7, 8), hcaps,
                                     mirror_second=True)
    if it1:
        outcomes += _ladder_outcomes(G_IT1, (11, 12), it1)
    if it2:
        outcomes += _ladder_outcomes(G_IT2, (13, 14), it2)
    if bts:
        outcomes += [_outcome(G_BTS, 180, bts_line, bts[0]),
                     _outcome(G_BTS, 181, bts_line, bts[1])]
    outcomes += list(extra)
    return {"I": gid, "O1": team1, "O2": team2, "SN": sport, "L": league,
            "SI": 1, "LI": 10, "S": int(start or FUTURE), "E": outcomes}


def _line(count=15, tag="", **kwargs):
    """Линия из нескольких одинаковых по разметке событий: калибровка
    смотрит на всю линию сразу, одного матча ей мало."""
    return [_game(gid=i + 1, team1=f"Хозяева {tag}{i}",
                  team2=f"Гости {tag}{i}", **kwargs) for i in range(count)]


def _bts_line(low=BTS_LOW, high=BTS_HIGH, count=10, **kwargs):
    """Линия из низовых и результативных матчей: «обе забьют» проверяется
    по обеим половинам сразу, поэтому в снимке нужны и те и другие."""
    return (_line(count=count, tag="н", totals=LOW_TOTALS, bts=low, **kwargs)
            + _line(count=count, tag="в", totals=HIGH_TOTALS, bts=high,
                    **kwargs))


def _bothscore(odds, tag):
    """Рынок «обе забьют» одного из событий линии _bts_line."""
    return next(o for o in odds if o.market_key == "bothscore"
                and o.team1 == f"Хозяева {tag}0")


def _run(games, live=False):
    parser = MelbetParser()
    events = []
    for game in games:
        ev = RawEvent(game=game, picks=_picks(game))
        ev.base_picks = ev.picks
        events.append(ev)
    layout = detect(events)
    odds = []
    for ev in events:
        odds.extend(parser._event_odds(ev, layout, NOW, live))
    return layout, odds


def _keys(odds):
    return {o.market_key: o for o in odds}


# ---------- сбор исходов из ответа ----------

def test_picks_collects_flat_grouped_and_nested_outcomes():
    game = {"E": [_outcome(1, 1, None, 2.0)],
            "AE": [{"G": 17, "ME": [{"T": 9, "P": 2.5, "C": 1.9},
                                    {"T": 10, "P": 2.5, "C": 1.9}]}],
            "GE": [{"G": 2, "E": [[_outcome(2, 7, -1.5, 2.1)],
                                  [_outcome(2, 8, 1.5, 1.7)]]}]}
    assert sorted(_picks(game)) == sorted([
        (1, 1, None, 2.0), (17, 9, 2.5, 1.9), (17, 10, 2.5, 1.9),
        (2, 7, -1.5, 2.1), (2, 8, 1.5, 1.7)])


def test_picks_ignores_subgames_and_broken_values():
    """Рынки подигры не должны попасть в рынки всего матча, а кэф ≤ 1 —
    это заглушка «ставка снята», а не котировка."""
    game = {"E": [_outcome(1, 1, None, 2.0), _outcome(1, 3, None, 1.0)],
            "SG": [{"E": [_outcome(1, 1, None, 3.3)]}]}
    assert _picks(game) == [(1, 1, None, 2.0)]


def test_string_numbers_are_parsed():
    game = {"E": [{"G": 17, "T": 9, "P": "2,5", "C": "1,90"}]}
    assert _picks(game) == [(17, 9, 2.5, 1.9)]


# ---------- основные рынки ----------

def test_main_markets_are_parsed():
    layout, odds = _run(_line())
    by_key = _keys(odds)

    assert layout.groups[TOTAL] == G_TOTAL
    assert not layout.inverted and not layout.skipped

    o = by_key["winner1x2"]
    assert (o.k1, o.k3, o.k2) == WINNER_ODDS
    assert (o.outcome1, o.outcome3, o.outcome2) == ("П1", "X", "П2")

    total = by_key["total:2.5"]
    assert (total.k1, total.k2) == TOTAL_LADDER[2.5]
    assert total.outcome1 == "ТБ 2.5" and total.outcome2 == "ТМ 2.5"
    assert {"total:2", "total:2.5", "total:3"} <= set(by_key)

    hcap = by_key["hcap::-1.5"]
    assert (hcap.k1, hcap.k2) == HCAP_LADDER[-1.5]
    assert hcap.outcome1 == "Ф1 -1.5" and hcap.outcome2 == "Ф2 +1.5"

    it1 = by_key["itotal:1::2.5"]
    assert (it1.k1, it1.k2) == IT1_LADDER[2.5]
    assert it1.market.startswith("Тотал 2.5 (Хозяева")
    assert by_key["itotal:2::1.5"].k1 == IT2_LADDER[1.5][0]


def test_event_fields_and_link():
    _layout, odds = _run(_line(count=15))
    o = odds[0]
    assert o.bookmaker == "Melbet"
    assert o.sport == "Футбол · РПЛ"
    assert o.start_ts == int(FUTURE)
    # Ссылка ведёт на страницу матча: чемпионат, постоянный номер события
    # и флаг «не лайв» — подробнее в tests/test_event_urls.py
    assert o.url.startswith("https://melbet.ru/ru/sport/event-details/10-1-")
    assert o.url.endswith("-0-0-0-hozyaeva-0-gosti-0")


def test_started_matches_are_skipped_in_prematch():
    _layout, odds = _run(_line(start=NOW - 60))
    assert odds == []


# ---------- калибровка: перевёрнутые стороны ----------

def test_inverted_totals_are_detected_and_fixed():
    """Если БК поменяет коды местами, кэф «больше» станет падать с ростом
    линии. Парсер обязан это заметить и переставить стороны сам."""
    swapped = {line: (under, over)
               for line, (over, under) in TOTAL_LADDER.items()}
    layout, odds = _run(_line(totals=swapped))
    assert TOTAL in layout.inverted
    total = _keys(odds)["total:2.5"]
    assert (total.k1, total.k2) == TOTAL_LADDER[2.5]


def test_mixed_totals_are_skipped_entirely():
    """Половина линии говорит одно, половина — другое: значит в одну
    группу попали разные рынки. Тотал не отдаём вовсе."""
    swapped = {line: (under, over)
               for line, (over, under) in TOTAL_LADDER.items()}
    layout, odds = _run(_line(count=12) + _line(count=12, totals=swapped))
    assert TOTAL in layout.skipped
    assert not [o for o in odds if o.market_key.startswith("total:")]
    # остальные рынки при этом продолжают работать
    assert [o for o in odds if o.market_key.startswith("hcap:")]


def test_inverted_handicap_sides_are_detected():
    """Фора фаворита обязана быть отрицательной: если у первой команды
    (по кэфу — фаворита) справедливая линия положительная, стороны
    перепутаны."""
    mirrored = {-line: (k2, k1) for line, (k1, k2) in HCAP_LADDER.items()}
    # у форы голос даёт одно событие целиком (знак справедливой линии),
    # поэтому событий нужно больше, чем для лестницы тоталов
    layout, odds = _run(_line(count=25, hcaps=mirrored))
    assert HCAP in layout.inverted
    hcap = _keys(odds)["hcap::-1.5"]
    assert (hcap.k1, hcap.k2) == HCAP_LADDER[-1.5]


def test_swapped_individual_total_sides_are_detected():
    """Индивидуальный тотал фаворита выше — по этому и видно, что стороны
    инд. тоталов поменяли местами."""
    layout, odds = _run(_line(count=25, it1=IT2_LADDER, it2=IT1_LADDER))
    assert ITOTAL_SIDES in layout.inverted
    by_key = _keys(odds)
    # линии «более забивной» команды снова оказались у первой
    assert by_key["itotal:1::2.5"].k1 == IT1_LADDER[2.5][0]
    assert by_key["itotal:2::1.5"].k1 == IT2_LADDER[1.5][0]


def test_group_with_wider_coverage_wins():
    """Тотал тайма лежит в своей группе и не должен смешиваться с тоталом
    матча: берём группу, покрывающую больше событий."""
    half = _ladder_outcomes(G_HALF_TOTAL, (9, 10), {0.5: (1.7, 2.1)})
    games = _line(count=15, extra=half)
    layout, odds = _run(games)
    assert layout.groups[TOTAL] == G_TOTAL
    assert "total:0.5" not in _keys(odds)


# ---------- «обе забьют»: рынок без линии ----------

def test_both_score_sides_are_learned_from_scoring_level():
    """У «обе забьют» нет ни линии, ни лестницы — где «да», видно только
    по результативности матчей: в низовых «да» дороже, в результативных
    дешевле."""
    layout, odds = _run(_bts_line())
    assert BTS not in layout.skipped and BTS not in layout.inverted

    low = _bothscore(odds, "н")
    assert (low.k1, low.k2) == BTS_LOW
    assert (low.outcome1, low.outcome2) == ("Да", "Нет")
    assert low.market == "Обе забьют"
    assert _bothscore(odds, "в").k1 == BTS_HIGH[0]


def test_both_score_without_line_field_accepts_zero_line():
    """Часть зеркал кладёт в рынок без линии P=0 вместо пустого поля."""
    _layout, odds = _run(_bts_line(bts_line=0))
    assert (_bothscore(odds, "н").k1, _bothscore(odds, "н").k2) == BTS_LOW


def test_swapped_both_score_codes_are_detected():
    """Коды «да» и «нет» стоят наоборот: обе половины линии говорят об
    этом одинаково, значит стороны надо переставить."""
    layout, odds = _run(_bts_line(low=BTS_LOW[::-1], high=BTS_HIGH[::-1]))
    assert BTS in layout.inverted
    assert (_bothscore(odds, "н").k1, _bothscore(odds, "н").k2) == BTS_LOW


def test_both_score_is_skipped_when_halves_disagree():
    """«Да» дороже и в низовых, и в результативных матчах — так ведёт себя
    не «обе забьют» матча, а какой-то другой рынок (например той же пары
    кодов, но за 1-й тайм). Разошлись половины — рынок не отдаём."""
    layout, odds = _run(_bts_line(low=BTS_LOW, high=BTS_LOW))
    assert BTS in layout.skipped
    assert not [o for o in odds if o.market_key.startswith("bothscore")]


def test_both_score_is_skipped_without_low_scoring_half():
    """Только результативные матчи: рынок вёл бы себя так же, даже будь
    коды перепутаны. Одной половины линии для вывода мало."""
    layout, odds = _run(_line(count=20, totals=HIGH_TOTALS, bts=BTS_HIGH))
    assert BTS in layout.skipped
    assert not [o for o in odds if o.market_key.startswith("bothscore")]


def test_both_score_is_skipped_when_totals_are_unusable():
    """Опора у «обе забьют» — тотал той же БК. Не подтвердился он —
    проверять рынок нечем."""
    swapped = {line: (under, over) for line, (over, under) in
               LOW_TOTALS.items()}
    layout, _odds = _run(_bts_line() + _line(count=20, tag="x",
                                             totals=swapped, bts=BTS_LOW))
    assert TOTAL in layout.skipped and BTS in layout.skipped


def test_both_score_from_full_markets_only():
    """«Обе забьют» нет в компактном снимке — он приходит только с полной
    росписью. Группу для такого рынка берём из росписи, иначе он не попал
    бы в линию вовсе."""
    events = []
    for game in _bts_line():
        bts = [o for o in game["E"] if o["G"] == G_BTS]
        base = {**game, "E": [o for o in game["E"] if o["G"] != G_BTS]}
        ev = RawEvent(game=game, picks=_picks(game))
        ev.base_picks = _picks(base)      # в снимке линии рынка ещё нет
        assert bts and len(ev.picks) > len(ev.base_picks)
        events.append(ev)
    layout = detect(events)
    assert layout.groups[BTS] == G_BTS and BTS not in layout.skipped


# ---------- защита от мусора ----------

def test_impossible_pair_is_dropped():
    """1/К1 + 1/К2 < 1 внутри ОДНОЙ БК не бывает — это склеенные разные
    рынки, а не подарок."""
    broken = dict(TOTAL_LADDER)
    broken[2.5] = (2.60, 2.60)
    game = _game(gid=99, team1="Битые", team2="Кэфы", totals=broken)
    _layout, odds = _run(_line() + [game])
    keys = {o.market_key for o in odds if o.team1 == "Битые"}
    assert "total:2.5" not in keys
    assert {"total:2", "total:3"} <= keys


def test_1x2_with_absurd_margin_is_dropped():
    _layout, odds = _run(_line(winner=(1.05, 1.05, 1.05)))
    assert not [o for o in odds if o.market_key.startswith("winner")]


def test_handicap_without_opposite_line_is_dropped():
    """Стороны форы обязаны стоять на противоположных линиях."""
    game = _game()
    game["E"] = [o for o in game["E"]
                 if not (o["T"] == 8 and o.get("P") == 1.5)]
    game["E"].append(_outcome(G_HCAP, 8, 2.5, 1.70))
    _layout, odds = _run(_line(count=14) + [game])
    keys = [o.market_key for o in odds
            if o.team1 == "Спартак" and o.market_key.startswith("hcap:")]
    assert "hcap::-1.5" not in keys
    assert "hcap::-1" in keys


def test_two_way_market_without_draw():
    _layout, odds = _run(_line(winner=(1.80, 0, 2.05)))
    o = _keys(odds)["winner"]
    assert (o.k1, o.k2, o.k3) == (1.80, 2.05, None)


# ---------- подигры (таймы, периоды, предметные рынки) ----------

def _parsed_subgame(parent_game, sub, outcomes):
    """Подигра как отдельное событие: сначала её берут из росписи матча,
    потом её рынки приезжают отдельным запросом по её же id."""
    parent = RawEvent(game=parent_game, picks=_picks(parent_game))
    picked = _subgames({"SG": [{"I": 555, **sub}]})
    if not picked:
        return None
    return _subgame_event(parent, {**picked[0], "E": outcomes})


def test_subgame_markets_get_period_scope():
    """Период подигры лежит в PN, а не в N: там числовой идентификатор."""
    game = _game()
    outcomes = _ladder_outcomes(G_HALF_TOTAL, (9, 10), {1.5: (1.85, 1.95)})
    sub = _parsed_subgame(game, {"N": "159625", "PN": "1-й тайм", "TG": ""},
                          outcomes)
    assert sub is not None and sub.scope == "half1"

    parser = MelbetParser()
    events = [RawEvent(game=g, picks=_picks(g)) for g in _line()]
    for ev in events:
        ev.base_picks = ev.picks
    layout = detect(events)
    odds = parser._event_odds(sub, layout, NOW, False)
    key = _keys(odds)["total:half1:1.5"]
    assert (key.k1, key.k2) == (1.85, 1.95)


def test_subgame_subject_and_period_make_one_scope():
    """«Угловые» + «1-й тайм» — это тотал УГЛОВЫХ тайма, и складывать его с
    тоталом голов нельзя."""
    outcomes = _ladder_outcomes(G_HALF_TOTAL, (9, 10), {8.5: (1.85, 1.95)})
    sub = _parsed_subgame(_game(), {"TG": "Угловые", "PN": "1-й тайм"},
                          outcomes)
    assert sub is not None and sub.scope == "corners+half1"


def test_nameless_subgame_is_skipped():
    """Без названия непонятно, к чему относятся рынки, — а смешать их с
    рынками всего матча значит выдумать вилку."""
    outcomes = _ladder_outcomes(G_HALF_TOTAL, (9, 10), {1.5: (1.85, 1.95)})
    assert _parsed_subgame(_game(), {"N": "159625"}, outcomes) is None


def test_periods_are_taken_before_statistics(monkeypatch):
    """Запросов на подигры выделено немного, и уходить они должны на
    периоды: их подписывают все БК, а «удары в створ» — единицы."""
    monkeypatch.setattr("app.parsers.melbet.MELBET_SUBGAMES", 2)
    picked = _subgames({"SG": [
        {"I": 1, "TG": "Угловые", "PN": ""},
        {"I": 2, "TG": "Жёлтые карточки", "PN": "1-й тайм"},
        {"I": 3, "TG": "", "PN": "1-й тайм"},
        {"I": 4, "TG": "", "PN": "2-й тайм"},
    ]})
    assert [sub["I"] for sub in picked] == [3, 4]


def test_subgame_limit_is_respected(monkeypatch):
    monkeypatch.setattr("app.parsers.melbet.MELBET_SUBGAMES", 0)
    assert _subgames({"SG": [{"I": 1, "TG": "", "PN": "1-й тайм"}]}) == []


def test_forced_group_overrides_automatic_choice():
    """Оператор может закрепить группу руками, если автовыбор промахнулся."""
    half = _ladder_outcomes(G_HALF_TOTAL, (9, 10), {0.5: (1.7, 2.1)})
    events = []
    for game in _line(count=15, extra=half):
        ev = RawEvent(game=game, picks=_picks(game))
        ev.base_picks = ev.picks
        events.append(ev)
    layout = detect(events, forced={TOTAL: G_HALF_TOTAL})
    odds = []
    for ev in events:
        odds.extend(MelbetParser()._event_odds(ev, layout, NOW, False))
    keys = {o.market_key for o in odds}
    assert "total:0.5" in keys and "total:2.5" not in keys


def _fake_feed(line, *, champs=((70, 2),), subgame=True):
    """Подменённый фид: отвечает так же, как настоящий, и так же придирчив
    к строке запроса. Возвращает (функция get_json, журнал вызовов)."""
    ladder_extra = _ladder_outcomes(G_TOTAL, (9, 10), {3.5: (2.90, 1.42)})
    half = _ladder_outcomes(G_HALF_TOTAL, (9, 10), {1.5: (1.85, 1.95)})
    calls = []

    def fake_get_json(url, *, params=None, timeout=None, **_kw):
        params = dict(params or {})
        calls.append((url, params))
        # Живой фид отвечает 406 на любую строку запроса, отличную от той,
        # которую шлёт сайт: и на лишний параметр, и на другой их порядок.
        assert "lng" not in params, "фид не принимает lng ни с каким значением"
        if url.endswith("/GetSportsShortZip"):
            assert "mode" not in params and "getEmpty" not in params
            return {"Value": [{"I": 1, "N": "Футбол"}]}
        if url.endswith("/GetChampsZip"):
            return {"Value": [{"LI": li, "GC": gc, "L": "РПЛ", "SI": 1}
                              for li, gc in champs]}
        if url.endswith("/Get1x2_VZip"):
            keys = list(params)
            assert keys[0] in ("champs", "sports"), "фильтр идёт первым"
            assert keys[1:] == ["count", "mode"], "порядок как у сайта"
            assert params["count"] % 5 == 0 and params["count"] <= 50
            return {"Value": line[:params["count"]]}
        if url.endswith("/GetChampZip"):
            return {"Value": {"L": "РПЛ", "SI": 1, "SN": "Футбол",
                              "G": [{"I": 900 + n} for n in range(3)]}}
        if url.endswith("/GetGameZip"):
            game = next((g for g in line if g["I"] == params["id"]), None)
            if game is None:
                return {"Value": {"I": params["id"], "E": half}}
            full = {**game, "E": game["E"] + ladder_extra}
            if subgame:
                full["SG"] = [{"I": 5000 + game["I"], "PN": "1-й тайм",
                               "TG": ""}]
            return {"Value": full}
        raise AssertionError(f"неожиданный запрос: {url}")

    return fake_get_json, calls


def test_full_cycle_over_fake_feed():
    """Полный обход: виды спорта → чемпионаты → события чемпионата →
    роспись события → роспись подигры. Сеть подменена, но маршруты и
    строки запросов проверяются настоящие."""
    line = _line(count=15)
    fake, calls = _fake_feed(line)
    parser = MelbetParser()
    parser.get_json = fake
    odds = parser.fetch_odds()

    keys = {o.market_key for o in odds}
    assert "total:3.5" in keys          # линия из полной росписи
    assert "total:half1:1.5" in keys    # рынок подигры со своим scope
    assert "winner1x2" in keys
    assert all(o.bookmaker == "Melbet" for o in odds)
    methods = {url.rsplit("/", 1)[-1] for url, _ in calls}
    assert {"GetSportsShortZip", "GetChampsZip", "Get1x2_VZip",
            "GetGameZip"} <= methods
    assert all("/LineFeed/" in url for url, _ in calls)
    # события спрашиваем по чемпионату, а не по виду спорта
    line_calls = [p for u, p in calls if u.endswith("/Get1x2_VZip")]
    assert line_calls[0]["champs"] == 70


def test_line_is_topped_up_when_champs_requests_fail():
    """Фид умеет оборвать часть запросов — тогда линия добирается по видам
    спорта, чтобы обход не остался вообще без событий."""
    line = _line(count=15)
    fake, calls = _fake_feed(line, champs=((70, 900),), subgame=False)

    def flaky(url, *, params=None, timeout=None, **kw):
        if url.endswith("/Get1x2_VZip") and "champs" in dict(params or {}):
            raise requests.ConnectionError("сброс соединения")
        return fake(url, params=params, timeout=timeout, **kw)

    parser = MelbetParser()
    parser.get_json = flaky
    odds = parser.fetch_odds()
    assert odds, "линия должна собраться запросами по виду спорта"
    assert any("sports" in p for u, p in calls if u.endswith("/Get1x2_VZip"))


def test_big_champ_tail_comes_from_index():
    """В чемпионате больше событий, чем отдаёт один запрос: остальные берём
    из индекса чемпионата, рынки к ним приедут с росписью."""
    line = _line(count=15)
    fake, calls = _fake_feed(line, champs=((70, 200),), subgame=False)
    parser = MelbetParser()
    parser.get_json = fake
    parser.fetch_odds()
    assert any(u.endswith("/GetChampZip") for u, _ in calls)
    tail_ids = {p["id"] for u, p in calls
                if u.endswith("/GetGameZip") and p["id"] >= 900}
    assert tail_ids, "у событий из индекса должна спрашиваться роспись"


def test_count_is_clamped_to_feed_limits():
    """count обязан быть кратен пяти (иначе 406), а больше 50 фид всё равно
    не отдаёт."""
    assert _count(500) == 50
    assert _count(8) == 5
    assert _count(0) == 5
    assert _count(47) == 45


def _refusing_parser(status=406, body="", content_type="text/html"):
    parser = MelbetParser()

    def refuse(url, *, params=None, timeout=None, **_kw):
        resp = requests.Response()
        resp.status_code = status
        resp._content = body.encode("utf-8")
        # Заголовок как у живого сайта: «text/html» БЕЗ charset — и
        # кодировка выводится из него ровно так же, как это делает сам
        # requests при настоящем запросе. Без этих двух строк ответ
        # раскодировался бы по содержимому, русский текст читался бы, и
        # тест был бы зелёным там, где боевой ответ приезжает
        # кракозябрами (ISO-8859-1 по умолчанию для text/html).
        resp.headers["Content-Type"] = content_type
        resp.encoding = requests.utils.get_encoding_from_headers(resp.headers)
        raise requests.HTTPError("не принято", response=resp)

    parser.get_json = refuse
    return parser


# Заглушка melbet.ru в том виде, в каком она приходит на боевой сервер
# (снято живьём 2026-07-30): русский текст в UTF-8, а Content-Type — без
# charset, см. _refusing_parser выше.
VPN_STUB_HTML = (
    '<!DOCTYPE html>\n<html lang="ru">\n<head>\n'
    '    <meta charset="UTF-8">\n'
    '    <title>Пожалуйста, отключите VPN</title>\n</head>\n<body>\n'
    '    <section class="site-unavailable">\n'
    '        <div class="site-unavailable__title">Данный сайт <br> '
    'недоступен в <br> вашей стране</div>\n'
    '    </section>\n</body>\n</html>\n'
    '<p style="display: none;">::CLOUDFLARE_ERROR_1000S_BOX:: </p>'
)


def test_probe_reports_why_mirror_refused():
    """Причина отказа зеркала попадает и в лог, и в диагностику: 406 и 404
    лечатся по-разному."""
    parser = _refusing_parser()
    assert parser._resolve_base() is None
    assert parser.probe_notes and all("406" in note
                                      for _base, note in parser.probe_notes)


def test_probe_names_the_disable_vpn_stub():
    """Заглушка «отключите VPN» — это про адрес сервера, а не про строку
    запроса, и чинится она другим зеркалом.

    Страница подставляется целиком и с боевым заголовком: сайт отдаёт её
    как «text/html» без charset, requests по стандарту разбирает такой
    ответ как ISO-8859-1, и поиск русских слов в resp.text ничего не
    находил — на живом сервере заглушка так и отчитывалась обычным
    «фид отклонил запрос», отправляя чинить строку запроса вместо
    адреса."""
    parser = _refusing_parser(403, VPN_STUB_HTML)
    assert parser._resolve_base() is None
    assert all("VPN" in note for _base, note in parser.probe_notes)


def test_geo_stub_survives_a_page_without_russian_text():
    """Опознание не должно держаться на одной формулировке заголовка:
    вёрстку заглушки (`site-unavailable`, cloudflare-маркер) видно в любой
    кодировке и при любой смене текста."""
    resp = requests.Response()
    resp.status_code = 403
    resp._content = (b'<html><head><title>Nope</title></head><body>'
                     b'<section class="site-unavailable"></section>'
                     b'</body></html>')
    resp.headers["Content-Type"] = "text/html"
    assert _is_geo_stub(resp)

    # обычный отказ фида заглушкой считаться не должен
    ok = requests.Response()
    ok.status_code = 406
    ok._content = b'{"Error":"bad params","Success":false}'
    assert not _is_geo_stub(ok)

    # тело не приехало вовсе — не падаем
    empty = requests.Response()
    empty.status_code = 403
    assert not _is_geo_stub(empty)


def test_first_probe_runs_right_after_machine_boot(monkeypatch):
    """time.monotonic() считает секунды с загрузки машины, и у свежего
    парсера «прошлая попытка» обязана быть бесконечно давно: иначе первые
    PROBE_BACKOFF секунд после перезагрузки сервера база не искалась бы
    вовсе и Melbet молча отдавала пустую линию."""
    monkeypatch.setattr("app.parsers.melbet.time.monotonic", lambda: 1.0)
    parser = _refusing_parser()
    assert parser._resolve_base() is None
    assert parser.probe_notes, "зеркала должны быть перебраны сразу"


def test_failed_probe_is_not_repeated_every_cycle(monkeypatch):
    """Неудачный перебор повторяется не чаще PROBE_BACKOFF: он стоит
    таймаута на каждое зеркало."""
    clock = {"now": 10_000.0}
    monkeypatch.setattr("app.parsers.melbet.time.monotonic",
                        lambda: clock["now"])
    parser = _refusing_parser()
    assert parser._resolve_base() is None
    tried = len(parser.probe_notes)

    parser.probe_notes = []
    clock["now"] += 10
    assert parser._resolve_base() is None
    assert parser.probe_notes == []

    clock["now"] += 300
    assert parser._resolve_base() is None
    assert len(parser.probe_notes) == tried


def test_diagnostics_probe_ignores_the_backoff(monkeypatch):
    """Диагностику запускают именно тогда, когда линия пуста, — и она
    обязана сходить к зеркалам, а не пересказать пустой список."""
    monkeypatch.setattr("app.parsers.melbet.time.monotonic", lambda: 10_000.0)
    parser = _refusing_parser()
    parser._last_probe = 10_000.0
    assert parser._resolve_base() is None and not parser.probe_notes
    assert parser._resolve_base(force=True) is None
    assert parser.probe_notes


def test_ambiguous_subgame_group_is_skipped():
    """В подигре два разных набора тоталов: какой из них основной —
    неизвестно, поэтому не берём ни один."""
    outcomes = (_ladder_outcomes(G_HALF_TOTAL, (9, 10), {1.5: (1.85, 1.95)})
                + _ladder_outcomes(G_HALF_TOTAL + 1, (9, 10),
                                   {4.5: (1.75, 2.05)}))
    sub = _parsed_subgame(_game(), {"PN": "1-й тайм"}, outcomes)
    events = [RawEvent(game=g, picks=_picks(g)) for g in _line()]
    for ev in events:
        ev.base_picks = ev.picks
    layout = detect(events)
    odds = MelbetParser()._event_odds(sub, layout, NOW, False)
    assert not [o for o in odds if o.market_key.startswith("total:")]
