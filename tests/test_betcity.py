"""Тесты разбора рынков Betcity (app/parsers/betcity.py) — офлайн, на
синтетических event/market словарях в формате реального фида
(POST /d/off/events), без сети.
"""
import time

from app.parsers import betcity as betcity_mod
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
          slug="soccer", ext=None, **event_kwargs):
    p = BetcityParser()
    ev = _event(markets, **event_kwargs)
    if ext is not None:
        # полная роспись (ext=1) живёт в кэше парсера, а не в событии
        p._ext[ev["id_ev"]] = {str(i): m for i, m in enumerate(ext)}
    return p._parse_event(ev, sport_name, league, slug, NOW)


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


def test_unknown_yes_no_block_ignored():
    """Блок YNm (не YN) в общем снимке — турнирный проп, не разбираем."""
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


# ---------- полная роспись рынков (ext=1) ----------

def test_ext_markets_add_full_ladder():
    """В общем снимке у события лишь ОДНА линия тотала и форы; полная
    роспись добавляет всю лестницу (блоки называются T/F1 без суффикса)."""
    main = _market("Тотал", {
        "T1m": {"Tot": 2.5, "Tb": _kf(1.95), "Tm": _kf(1.86)},
    })
    ext = [
        _market("Тотал", {"T": {"Tot": 3.5, "Tb": _kf(2.9), "Tm": _kf(1.42)}}),
        _market("Азиатский тотал",
                {"T": {"Tot": 2.75, "Tb": _kf(2.1), "Tm": _kf(1.75)}}),
        _market("Фора 1-й тайм",
                {"F1": {"F1": -0.5, "F2": 0.5, "Kf_F1": _kf(2.2),
                        "Kf_F2": _kf(1.7)}}),
    ]
    odds = _by_key(_parse([main], ext=ext))
    assert "total:2.5" in odds and "total:3.5" in odds
    # азиатский тотал — та же линия тотала, дробный шаг: scope не меняется
    assert "total:2.75" in odds
    assert "hcap:half1:-0.5" in odds


def test_main_market_wins_over_ext_duplicate():
    """Один и тот же рынок есть и в снимке (свежий каждый цикл), и в
    росписи (обновляется по кругу) — берём снимок."""
    main = _market("Тотал", {
        "T1m": {"Tot": 2.5, "Tb": _kf(1.95), "Tm": _kf(1.86)},
    })
    ext = [_market("Тотал", {"T": {"Tot": 2.5, "Tb": _kf(1.5), "Tm": _kf(2.5)}})]
    odds = _by_key(_parse([main], ext=ext))
    assert (odds["total:2.5"].k1, odds["total:2.5"].k2) == (1.95, 1.86)


def test_individual_totals_both_sides():
    ext = [
        _market("Индивидуальный тотал",
                {"IT_T1": {"Tot": 2.5, "Tb": _kf(1.67), "Tm": _kf(2.16),
                           "id_bet": 1}}),
        _market("Индивидуальный тотал",
                {"IT_T2": {"Tot": 1, "Tb": _kf(1.79), "Tm": _kf(2.0),
                           "id_bet": 2}}),
        _market("Инд. тотал 1-й тайм",
                {"IT_T1": {"Tot": 1, "Tb": _kf(1.63), "Tm": _kf(2.16)}}),
    ]
    odds = _by_key(_parse([], ext=ext))
    o1 = odds["itotal:1::2.5"]
    assert o1.market == "Тотал 2.5 (Спартак)"      # IT_T1 — хозяева
    assert (o1.outcome1, o1.outcome2) == ("ИТБ 2.5", "ИТМ 2.5")
    assert (o1.k1, o1.k2) == (1.67, 2.16)
    assert odds["itotal:2::1"].market == "Тотал 1 (Зенит)"
    assert "itotal:1:half1:1" in odds


def test_both_score_market():
    ext = [_market("Обе забьют", {"YN": {"Y": _kf(1.54), "N": _kf(2.4)}})]
    odds = _by_key(_parse([], ext=ext))
    o = odds["bothscore"]
    assert (o.outcome1, o.outcome2) == ("Да", "Нет")
    assert (o.k1, o.k2) == (1.54, 2.4)


def test_both_score_period_scoped():
    ext = [
        _market("Обе забьют в первом тайме",
                {"YN": {"Y": _kf(3.6), "N": _kf(1.26)}}),
        _market("Обе забьют в 1-м периоде",
                {"YN": {"Y": _kf(2.8), "N": _kf(1.4)}}),
    ]
    odds = _by_key(_parse([], ext=ext))
    assert "bothscore:half1" in odds
    assert "bothscore:period1" in odds
    assert "bothscore" not in odds  # рынок тайма ≠ рынок всего матча


def test_combined_markets_with_same_words_rejected():
    """У Betcity десятки комбинированных Да/Нет-рынков, содержащих слова
    «обе забьют». Служебные «П1»/«ТМ» в scope не попадают, поэтому без
    строгой проверки имени они склеились бы с обычным «Обе забьют» другой
    БК в ложную вилку."""
    ext = [
        _market("П1 и обе забьют", {"YN": {"Y": _kf(3.1), "N": _kf(1.32)}}),
        _market("Обе забьют и не ничья",
                {"YN": {"Y": _kf(2.4), "N": _kf(1.5)}}),
        _market("Обе забьют хотя бы в одном тайме",
                {"YN": {"Y": _kf(1.9), "N": _kf(1.9)}}),
        _market("Волевая победа", {"YN": {"Y": _kf(4.0), "N": _kf(1.2)}}),
    ]
    assert _parse([], ext=ext) == []


def test_odd_even_market():
    """Y=Чет, N=Нечет (по шаблону разметки рынка сайта). Порядок исходов
    как у остальных парсеров: 1-й исход всегда «Чет»."""
    ext = [_market("Чет/Нечет тотала",
                   {"YN": {"Y": _kf(1.9), "N": _kf(1.95)}})]
    odds = _by_key(_parse([], ext=ext))
    o = odds["oddeven"]
    assert (o.outcome1, o.outcome2) == ("Чет", "Нечет")
    assert (o.k1, o.k2) == (1.9, 1.95)


def test_odd_even_with_subject_prefix():
    ext = [_market("УГЛ. Чет/Нечет тотала",
                   {"YN": {"Y": _kf(1.85), "N": _kf(1.95)}})]
    assert "oddeven:corners" in _by_key(_parse([], ext=ext))


def test_team_yes_no_blocks_ignored():
    """Да/Нет КОНКРЕТНОЙ команды (YNT1/YNT2) — рынок другого смысла."""
    ext = [_market("Голы", {"YNT1": {"Y": _kf(1.5), "N": _kf(2.4),
                                     "num_team": 1, "id_bet": 7}})]
    assert _parse([], ext=ext) == []


# ---------- зависимые события (статистика отдельным событием) ----------

def test_dependent_event_team_prefix_stripped():
    """Статистику Betcity дублирует зависимым событием, приписывая предмет
    к именам ОБЕИХ команд («УГЛ Спартак»). Предмет уже есть в имени рынка,
    а искажённое имя команды не даёт сшить событие с другими БК."""
    m = _market("УГЛ. Тотал", {
        "T1m": {"Tot": 9.5, "Tb": _kf(1.9), "Tm": _kf(1.9)},
    })
    odds = _parse([m], team1="УГЛ Спартак", team2="УГЛ Зенит")
    assert len(odds) == 1
    assert (odds[0].team1, odds[0].team2) == ("Спартак", "Зенит")
    assert odds[0].market_key == "total:corners:9.5"


def test_real_team_name_prefix_kept():
    """Убираем префикс ТОЛЬКО когда он у обеих команд: «КС Университатя» —
    настоящее имя клуба, а не пометка предмета."""
    m = _market("Тотал", {
        "T1m": {"Tot": 2.5, "Tb": _kf(1.9), "Tm": _kf(1.9)},
    })
    odds = _parse([m], team1="КС Университатя Крайова", team2="Левски София")
    assert odds[0].team1 == "КС Университатя Крайова"


# ---------- обновление росписи по кругу ----------

class _ExtSpy(BetcityParser):
    """Парсер с подменённой сетью: помнит, какие id запрашивались."""

    def __init__(self, without_ext=()):
        super().__init__()
        self.asked = []
        self._without_ext = set(without_ext)

    def _fetch_ext(self, ids):
        self.asked.append(list(ids))
        return {i: {"1": {"name": "Тотал", "data": {}}}
                for i in ids if i not in self._without_ext}


def _events(n, start=1):
    return [{"id_ev": i, "date_ev": FUTURE_TS + i}
            for i in range(start, start + n)]


def test_ext_refresh_limited_per_cycle(monkeypatch):
    """За цикл — не больше BETCITY_EXT_MAX_REQUESTS пачек: полная роспись
    всей линии весит десятки мегабайт, её незачем качать каждый цикл."""
    monkeypatch.setattr(betcity_mod, "BETCITY_EXT_BATCH", 10)
    monkeypatch.setattr(betcity_mod, "BETCITY_EXT_MAX_REQUESTS", 2)
    p = _ExtSpy()
    events = _events(100)
    p._refresh_ext(events, deadline=time.monotonic() + 60, now=time.time())
    assert sum(len(b) for b in p.asked) == 20
    assert len(p._ext) == 20


def test_ext_refresh_round_robin(monkeypatch):
    """Событие со свежей росписью не перезапрашивается — бюджет цикла
    уходит на те, у которых росписи ещё нет."""
    monkeypatch.setattr(betcity_mod, "BETCITY_EXT_BATCH", 10)
    monkeypatch.setattr(betcity_mod, "BETCITY_EXT_MAX_REQUESTS", 1)
    monkeypatch.setattr(betcity_mod, "BETCITY_EXT_REFRESH", 180)
    p = _ExtSpy()
    events = _events(20)
    now = time.time()
    p._refresh_ext(events, deadline=time.monotonic() + 60, now=now)
    p._refresh_ext(events, deadline=time.monotonic() + 60, now=now + 1)
    first, second = p.asked
    assert not set(first) & set(second)
    assert set(first) | set(second) == {e["id_ev"] for e in events}
    # пока роспись свежая, запросов нет вовсе
    p.asked.clear()
    p._refresh_ext(events, deadline=time.monotonic() + 60, now=now + 2)
    assert p.asked == []
    # спустя BETCITY_EXT_REFRESH первой снова обновляется самая старая пачка
    p._refresh_ext(events, deadline=time.monotonic() + 60, now=now + 200)
    assert set(p.asked[0]) == set(first)


def test_ext_refresh_marks_events_without_extra_markets(monkeypatch):
    """У части событий доп. рынков нет вовсе — они всё равно помечаются
    запрошенными, иначе съедали бы бюджет каждый цикл."""
    monkeypatch.setattr(betcity_mod, "BETCITY_EXT_BATCH", 10)
    monkeypatch.setattr(betcity_mod, "BETCITY_EXT_MAX_REQUESTS", 1)
    events = _events(10)
    p = _ExtSpy(without_ext=[e["id_ev"] for e in events])
    now = time.time()
    p._refresh_ext(events, deadline=time.monotonic() + 60, now=now)
    p.asked.clear()
    p._refresh_ext(events, deadline=time.monotonic() + 60, now=now + 1)
    assert p.asked == []


def test_ext_dropped_when_stale_or_event_gone(monkeypatch):
    monkeypatch.setattr(betcity_mod, "BETCITY_EXT_BATCH", 10)
    monkeypatch.setattr(betcity_mod, "BETCITY_EXT_MAX_REQUESTS", 1)
    monkeypatch.setattr(betcity_mod, "BETCITY_EXT_MAX_AGE", 600)
    p = _ExtSpy()
    events = _events(10)
    now = time.time()
    p._refresh_ext(events, deadline=time.monotonic() + 60, now=now)
    assert len(p._ext) == 10
    # событие ушло из линии — роспись не должна оставаться в памяти
    p._refresh_ext(events[:5], deadline=time.monotonic() + 60, now=now + 1)
    assert set(p._ext) == {e["id_ev"] for e in events[:5]}
    # роспись старше BETCITY_EXT_MAX_AGE выбрасывается
    p._refresh_ext(events[:5], deadline=time.monotonic() + 60, now=now + 1000)
    assert len(p._ext) == 5  # перезапрошена в этом же цикле
    p._fetch_ext = lambda ids: {}
    p._refresh_ext(events[:5], deadline=time.monotonic() + 60, now=now + 3000)
    assert p._ext == {}
