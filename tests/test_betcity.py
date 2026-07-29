"""Тесты разбора рынков Betcity (app/parsers/betcity.py) — офлайн, на
синтетических event/market словарях в формате реального фида
(POST /d/off/events), без сети.
"""
import time

from app.parsers.betcity import BetcityParser

NOW = time.time()
FUTURE_TS = int(NOW + 3600)


def _kf(v):
    return {"kf": v, "ps": 1, "md": 0}


def _market(name, blocks, data_id="1"):
    return {"name": name, "data": {data_id: {"blocks": blocks, "order": 1}}}


def _event(markets, team1="Спартак", team2="Зенит", date_ev=FUTURE_TS,
          cn_ch=100, id_ev=1):
    ev = {
        "id_ev": id_ev, "st_ev": 2, "del_ev": 0, "del": 0, "hd_bl": 0,
        "date_ev": date_ev, "date_ev_str": "01.01 20:00", "cn_ch": cn_ch,
        "name_ht": team1,
    }
    if team2 is not None:
        ev["name_at"] = team2
    ev["main"] = {str(i): m for i, m in enumerate(markets)}
    return ev


def _parse(markets, sport_name="Футбол", league="Премьер-лига",
          slug="soccer", **event_kwargs):
    p = BetcityParser()
    return p._parse_event(_event(markets, **event_kwargs), sport_name,
                          league, slug, NOW)


def _by_key(odds):
    return {o.market_key: o for o in odds}


# ---------- 1X2 / winner ----------

def test_1x2_market():
    m = _market("Фактический исход", {
        "Wm": {"P1": _kf(2.65), "X": _kf(3.59), "P2": _kf(2.64)},
    })
    odds = _by_key(_parse([m]))
    o = odds["winner1x2"]
    assert (o.k1, o.k2, o.k3) == (2.65, 2.64, 3.59)
    assert o.outcome1 == "П1" and o.outcome2 == "П2" and o.outcome3 == "X"


def test_1x2_insane_margin_rejected():
    m = _market("Фактический исход", {
        "Wm": {"P1": _kf(1.01), "X": _kf(1.01), "P2": _kf(1.01)},
    })
    odds = _by_key(_parse([m]))
    assert "winner1x2" not in odds


def test_1x2_scoped_by_subject_prefix():
    """«УГЛ. Фактический исход» — тот же тип рынка (1X2), но по угловым:
    предмет разворачивается («УГЛ.»→«угловые.») до канонизации scope."""
    m = _market("УГЛ. Фактический исход", {
        "Wm": {"P1": _kf(2.1), "X": _kf(3.2), "P2": _kf(3.4)},
    })
    odds = _by_key(_parse([m]))
    assert "winner1x2:corners" in odds


def test_card_market_scoped():
    m = _market("ЖК. Фактический исход", {
        "Wm": {"P1": _kf(1.9), "X": _kf(3.5), "P2": _kf(3.9)},
    })
    odds = _by_key(_parse([m]))
    assert "winner1x2:cards" in odds


def test_winner_no_draw():
    m = _market("Победитель", {
        "Wm": {"P1": _kf(1.92), "P2": _kf(1.92)},
    })
    odds = _by_key(_parse([m]))
    o = odds["winner"]
    assert (o.k1, o.k2) == (1.92, 1.92)


def test_partial_wm_block_ignored():
    """Неполный набор исходов (сторона приостановлена) — пропускаем."""
    m = _market("Фактический исход", {
        "Wm": {"P2": _kf(2.0)},
    })
    assert _parse([m]) == []


# ---------- тотал ----------

def test_total_market():
    m = _market("Тотал", {
        "T1m": {"Tot": 2.5, "Tb": _kf(1.95), "Tm": _kf(1.86)},
    })
    odds = _by_key(_parse([m]))
    o = odds["total:2.5"]
    assert (o.k1, o.k2) == (1.95, 1.86)
    assert o.outcome1 == "ТБ 2.5" and o.outcome2 == "ТМ 2.5"


def test_total_scoped_by_subject():
    m = _market("Офсайды. Тотал", {
        "T1m": {"Tot": 3.5, "Tb": _kf(1.8), "Tm": _kf(1.95)},
    })
    odds = _by_key(_parse([m]))
    assert "total:offsides:3.5" in odds


def test_total_missing_line_ignored():
    m = _market("Тотал", {
        "T1m": {"Tot": None, "Tb": _kf(1.8), "Tm": _kf(1.95)},
    })
    assert _parse([m]) == []


# ---------- фора ----------

def test_handicap_market():
    m = _market("Фора", {
        "F1m": {"F1": 0, "F2": 0, "Kf_F1": _kf(1.86), "Kf_F2": _kf(1.96)},
    })
    odds = _by_key(_parse([m]))
    o = odds["hcap:0"]
    assert (o.k1, o.k2) == (1.86, 1.96)


def test_handicap_signed_lines():
    m = _market("Фора", {
        "F1m": {"F1": -1, "F2": 1, "Kf_F1": _kf(2.1), "Kf_F2": _kf(1.69)},
    })
    odds = _by_key(_parse([m]))
    assert "hcap:-1" in odds
    assert odds["hcap:-1"].outcome2 == "Ф2 +1"


def test_handicap_inconsistent_lines_rejected():
    m = _market("Фора", {
        "F1m": {"F1": -1, "F2": 0.5, "Kf_F1": _kf(2.0), "Kf_F2": _kf(2.0)},
    })
    assert _parse([m]) == []


def test_handicap_scoped_by_subject():
    m = _market("Фолы. Фора", {
        "F1m": {"F1": -2, "F2": 2, "Kf_F1": _kf(1.7), "Kf_F2": _kf(2.05)},
    })
    odds = _by_key(_parse([m]))
    assert "hcap:fouls:-2" in odds


# ---------- пропускаемые типы блоков ----------

def test_double_chance_ignored():
    m = _market("Двойной исход", {
        "WXm": {"1X": _kf(1.51), "12": _kf(1.26), "X2": _kf(1.57)},
    })
    assert _parse([m]) == []


def test_outright_tms_block_ignored():
    """Турнирные пропы («Обладатель Суперкубка» и т.п.) используют блок
    TMS — не привязаны к исходу конкретного матча, пропускаются."""
    m = _market("Обладатель Суперкубка", {
        "TMS": {"T1": _kf(1.87), "T2": _kf(1.95)},
    })
    assert _parse([m]) == []


def test_yes_no_block_ignored():
    """YNm не привязан однозначно к «Обе забьют» (нет отдельных тегов
    исходов, как у LeonBet) — Betcity-парсер его не разбирает вовсе."""
    m = _market("Победитель", {
        "YNm": {"Y": _kf(1.5), "N": _kf(2.5)},
    })
    assert _parse([m]) == []


def test_correct_score_ignored():
    m = _market("Счет", {
        "SCR4": {"KF1": _kf(5.0), "KF2": _kf(6.0), "KF3": _kf(7.0),
                 "KF4": _kf(8.0)},
    })
    assert _parse([m]) == []


# ---------- фильтрация событий ----------

def test_outright_without_second_team_ignored():
    p = BetcityParser()
    ev = _event([_market("Тотал", {
        "T1m": {"Tot": 2.5, "Tb": _kf(1.9), "Tm": _kf(1.9)},
    })], team2=None)
    assert p._parse_event(ev, "Футбол", "Лига", "soccer", NOW) == []


def test_started_event_ignored():
    m = _market("Тотал", {
        "T1m": {"Tot": 2.5, "Tb": _kf(1.9), "Tm": _kf(1.9)},
    })
    odds = _parse([m], date_ev=int(NOW - 3600))
    assert odds == []


def test_hidden_event_ignored():
    p = BetcityParser()
    ev = _event([_market("Тотал", {
        "T1m": {"Tot": 2.5, "Tb": _kf(1.9), "Tm": _kf(1.9)},
    })])
    ev["hd_bl"] = 1
    assert p._parse_event(ev, "Футбол", "Лига", "soccer", NOW) == []


def test_event_url_built_from_slug_and_cn_ch():
    m = _market("Тотал", {
        "T1m": {"Tot": 2.5, "Tb": _kf(1.9), "Tm": _kf(1.9)},
    })
    odds = _parse([m], cn_ch=1420, id_ev=23114966)
    assert odds[0].url == "https://betcity.ru/ru/line/soccer/1420/23114966"


def test_event_url_falls_back_without_slug():
    m = _market("Тотал", {
        "T1m": {"Tot": 2.5, "Tb": _kf(1.9), "Tm": _kf(1.9)},
    })
    odds = _parse([m], slug=None)
    assert odds[0].url == "https://betcity.ru/ru/line"
