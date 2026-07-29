"""Тесты разбора рынков LeonBet (app/parsers/leon.py) — офлайн, на
синтетических event/market словарях в формате реального фида
(api-2/betline/{changes,event}/all), без сети.
"""
import time

from app.parsers.leon import LeonParser

NOW = time.time()
FUTURE_MS = int((NOW + 3600) * 1000)


def _runner(name, tags, price, handicap=None):
    r = {"name": name, "open": True, "tags": tags, "price": price}
    if handicap is not None:
        r["handicap"] = handicap
    return r


def _market(name, type_tag, runners, handicap=None, open_=True):
    m = {"name": name, "typeTag": type_tag, "open": open_,
        "runners": runners}
    if handicap is not None:
        m["handicap"] = handicap
    return m


def _event(markets, team1="Спартак", team2="Зенит", sport="Футбол",
          league="Российская Премьер-лига", kickoff=FUTURE_MS):
    return {
        "id": 1,
        "name": f"{team1} - {team2}",
        "competitors": [
            {"name": team1, "homeAway": "HOME"},
            {"name": team2, "homeAway": "AWAY"},
        ],
        "kickoff": kickoff,
        "betline": "prematch",
        "open": True,
        "league": {
            "name": league, "url": "rpl",
            "sport": {"name": sport, "url": "soccer"},
        },
        "url": "spartak-zenit",
        "markets": markets,
    }


def _parse(markets, **event_kwargs):
    p = LeonParser()
    return p._parse_event(_event(markets, **event_kwargs), NOW)


def _by_key(odds):
    return {o.market_key: o for o in odds}


# ---------- 1X2 / winner ----------

def test_1x2_market():
    m = _market("Исход 1Х2 (основное время)", "REGULAR", [
        _runner("1", ["HOME"], 2.65),
        _runner("X", ["DRAW"], 3.59),
        _runner("2", ["AWAY"], 2.64),
    ])
    odds = _by_key(_parse([m]))
    o = odds["winner1x2"]
    assert (o.k1, o.k2, o.k3) == (2.65, 2.64, 3.59)
    assert o.outcome1 == "П1" and o.outcome2 == "П2" and o.outcome3 == "X"


def test_1x2_insane_margin_rejected():
    """Явно перепутанные исходы (маржа вне разумных пределов) отбрасываются
    защитной проверкой sane_1x2_margin — как и у остальных БК."""
    m = _market("Исход 1Х2", "REGULAR", [
        _runner("1", ["HOME"], 1.01),
        _runner("X", ["DRAW"], 1.01),
        _runner("2", ["AWAY"], 1.01),
    ])
    odds = _by_key(_parse([m]))
    assert "winner1x2" not in odds


def test_winner_no_draw():
    """«Итоговая победа» (футбол, без ничьей) → market_key=winner — тот же
    ключ, что «Победитель» тенниса/др. видов без ничьей у других БК."""
    m = _market("Итоговая победа", "REGULAR", [
        _runner("1", ["HOME"], 1.92),
        _runner("2", ["AWAY"], 1.92),
    ])
    odds = _by_key(_parse([m]))
    o = odds["winner"]
    assert (o.k1, o.k2) == (1.92, 1.92)


def test_period_winner_scoped_separately():
    m1 = _market("1-й тайм: Победитель", "REGULAR", [
        _runner("1", ["HOME"], 3.32),
        _runner("X", ["DRAW"], 2.18),
        _runner("2", ["AWAY"], 3.29),
    ])
    m2 = _market("Исход 1Х2 (основное время)", "REGULAR", [
        _runner("1", ["HOME"], 2.65),
        _runner("X", ["DRAW"], 3.59),
        _runner("2", ["AWAY"], 2.64),
    ])
    odds = _by_key(_parse([m1, m2]))
    assert "winner1x2:half1" in odds
    assert "winner1x2" in odds
    assert odds["winner1x2:half1"].k1 == 3.32


# ---------- тотал ----------

def test_total_market():
    m = _market("Тотал", "TOTAL", [
        _runner("Больше (2.5)", ["OVER"], 1.95, handicap="2.5"),
        _runner("Меньше (2.5)", ["UNDER"], 1.86, handicap="2.5"),
    ], handicap="2.5")
    odds = _by_key(_parse([m]))
    o = odds["total:2.5"]
    assert (o.k1, o.k2) == (1.95, 1.86)
    assert o.outcome1 == "ТБ 2.5" and o.outcome2 == "ТМ 2.5"


def test_asian_total_same_scope_as_regular():
    """«Азиатский тотал» — та же линия/предмет, что обычный «Тотал»: не
    должен давать свой scope (иначе не сойдётся с «Тотал X.X» других БК на
    линиях, где обе формулировки пересекаются)."""
    m = _market("Азиатский тотал", "TOTAL", [
        _runner("Больше (1.75)", ["OVER"], 1.38, handicap="1.75"),
        _runner("Меньше (1.75)", ["UNDER"], 3.08, handicap="1.75"),
    ], handicap="1.75")
    odds = _by_key(_parse([m]))
    assert "total:1.75" in odds


def test_period_total_scoped():
    m = _market("1-й тайм: Тотал", "TOTAL", [
        _runner("Больше (1)", ["OVER"], 1.88, handicap="1"),
        _runner("Меньше (1)", ["UNDER"], 1.93, handicap="1"),
    ], handicap="1")
    odds = _by_key(_parse([m]))
    assert "total:half1:1" in odds


def test_home_away_individual_total():
    home = _market("Тотал хозяев", "TOTAL", [
        _runner("Больше (1)", ["OVER"], 1.64, handicap="1"),
        _runner("Меньше (1)", ["UNDER"], 2.22, handicap="1"),
    ], handicap="1")
    away = _market("Тотал гостей", "TOTAL", [
        _runner("Больше (1)", ["OVER"], 1.63, handicap="1"),
        _runner("Меньше (1)", ["UNDER"], 2.23, handicap="1"),
    ], handicap="1")
    odds = _by_key(_parse([home, away]))
    assert "itotal:1::1" in odds
    assert "itotal:2::1" in odds
    assert odds["itotal:1::1"].k1 == 1.64
    assert odds["itotal:2::1"].k1 == 1.63
    assert "(Спартак)" in odds["itotal:1::1"].market
    assert "(Зенит)" in odds["itotal:2::1"].market


def test_home_away_total_period_scope_kept():
    """Индивидуальный тотал тайма («1-й тайм: Тотал хозяев») должен нести
    и сторону (itotal:1), и период (half1) в ключе."""
    m = _market("1-й тайм: Тотал хозяев", "TOTAL", [
        _runner("Больше (0.5)", ["OVER"], 1.5, handicap="0.5"),
        _runner("Меньше (0.5)", ["UNDER"], 2.4, handicap="0.5"),
    ], handicap="0.5")
    odds = _by_key(_parse([m]))
    assert "itotal:1:half1:0.5" in odds


# ---------- фора ----------

def test_handicap_market():
    m = _market("Фора", "HANDICAP", [
        _runner("1 (0)", ["HOME"], 1.91, handicap="0"),
        _runner("2 (0)", ["AWAY"], 1.9, handicap="-0"),
    ])
    odds = _by_key(_parse([m]))
    o = odds["hcap:0"]
    assert (o.k1, o.k2) == (1.91, 1.9)


def test_handicap_signed_lines():
    m = _market("Азиатская фора", "HANDICAP", [
        _runner("1 (-0.75)", ["HOME"], 3.13, handicap="-0.75"),
        _runner("2 (+0.75)", ["AWAY"], 1.37, handicap="0.75"),
    ])
    odds = _by_key(_parse([m]))
    assert "hcap:-0.75" in odds
    assert odds["hcap:-0.75"].outcome2 == "Ф2 +0.75"


def test_handicap_inconsistent_lines_rejected():
    """Линии сторон не противоположны (баг фида/несогласованные данные) —
    рынок безопаснее отбросить, чем сшить неверную вилку."""
    m = _market("Фора", "HANDICAP", [
        _runner("1 (-1)", ["HOME"], 2.0, handicap="-1"),
        _runner("2 (+0.5)", ["AWAY"], 2.0, handicap="0.5"),
    ])
    odds = _by_key(_parse([m]))
    assert not odds


def test_period_handicap_scoped():
    m = _market("2-й тайм: Фора", "HANDICAP", [
        _runner("1 (0)", ["HOME"], 1.89, handicap="0"),
        _runner("2 (0)", ["AWAY"], 1.89, handicap="-0"),
    ])
    odds = _by_key(_parse([m]))
    assert "hcap:half2:0" in odds


# ---------- обе забьют / чёт-нечет ----------

def test_both_teams_to_score():
    m = _market("Обе команды забьют", "REGULAR", [
        _runner("Да", ["YES"], 1.77),
        _runner("Нет", ["NO"], 2.06),
    ])
    odds = _by_key(_parse([m]))
    o = odds["bothscore"]
    assert (o.k1, o.k2) == (1.77, 2.06)


def test_other_yes_no_market_ignored():
    """У Leon есть и другие Да/Нет-пропы («Хозяева забьют» и т.п.) с теми
    же tags YES/NO — по одним тегам их не отличить от «Обе забьют», поэтому
    парсер их сознательно пропускает (не путать разные по смыслу рынки)."""
    m = _market("Хозяева забьют", "REGULAR", [
        _runner("Да", ["YES"], 1.4),
        _runner("Нет", ["NO"], 2.9),
    ])
    odds = _parse([m])
    assert odds == []


def test_odd_even_main_market():
    """1-й исход — «Чет», как у всех остальных парсеров: движок сшивает
    чет/нечет по ПОЗИЦИИ исхода, а не по подписи."""
    m = _market("Чет/Нечет", "REGULAR", [
        _runner("Нечет", ["ODD"], 1.89),
        _runner("Чет", ["EVEN"], 1.92),
    ])
    odds = _by_key(_parse([m]))
    o = odds["oddeven"]
    assert (o.outcome1, o.outcome2) == ("Чет", "Нечет")
    assert (o.k1, o.k2) == (1.92, 1.89)


def test_team_odd_even_ignored():
    """«Чет/Нечет хозяев» даёт те же tags ODD/EVEN, что общий рынок — по
    market_scope не отличается от него (оба служебных слова), поэтому
    пропускается, чтобы не смешать с рынком всего матча."""
    m = _market("Чет/Нечет хозяев", "REGULAR", [
        _runner("Нечет", ["ODD"], 1.98),
        _runner("Чет", ["EVEN"], 1.82),
    ])
    assert _parse([m]) == []


# ---------- прочее ----------

def test_double_chance_ignored():
    """«Двойной исход» (1X/12/X2) — три пересекающихся исхода, не наш
    двухисходный/трёхисходный формат — должен быть проигнорирован."""
    m = _market("Двойной исход", "REGULAR", [
        _runner("1X", ["HOMEDRAW"], 1.48),
        _runner("12", ["HOMEAWAY"], 1.29),
        _runner("X2", ["DRAWAWAY"], 1.49),
    ])
    assert _parse([m]) == []


def test_multi_outcome_ignored():
    m = _market("Кто забьет первый гол", "REGULAR", [
        _runner("1", ["HOME"], 2.01),
        _runner("2", ["AWAY"], 2.0),
        _runner("Никто", ["OTHER"], 12.4),
    ])
    assert _parse([m]) == []


def test_closed_market_ignored():
    m = _market("Тотал", "TOTAL", [
        _runner("Больше (2.5)", ["OVER"], 1.95, handicap="2.5"),
        _runner("Меньше (2.5)", ["UNDER"], 1.86, handicap="2.5"),
    ], handicap="2.5", open_=False)
    assert _parse([m]) == []


def test_closed_runner_ignored():
    r_over = _runner("Больше (2.5)", ["OVER"], 1.95, handicap="2.5")
    r_over["open"] = False
    m = _market("Тотал", "TOTAL", [
        r_over,
        _runner("Меньше (2.5)", ["UNDER"], 1.86, handicap="2.5"),
    ], handicap="2.5")
    assert _parse([m]) == []


def test_started_event_ignored():
    """Событие с kickoff в прошлом — не прематч, пропускается."""
    m = _market("Тотал", "TOTAL", [
        _runner("Больше (2.5)", ["OVER"], 1.95, handicap="2.5"),
        _runner("Меньше (2.5)", ["UNDER"], 1.86, handicap="2.5"),
    ], handicap="2.5")
    past_ms = int((NOW - 3600) * 1000)
    odds = _parse([m], kickoff=past_ms)
    assert odds == []


def test_event_url_built_from_slugs():
    m = _market("Тотал", "TOTAL", [
        _runner("Больше (2.5)", ["OVER"], 1.95, handicap="2.5"),
        _runner("Меньше (2.5)", ["UNDER"], 1.86, handicap="2.5"),
    ], handicap="2.5")
    odds = _parse([m])
    assert odds[0].url == "https://leon.ru/soccer/rpl/spartak-zenit"
