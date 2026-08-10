"""Запрос полной росписи Winline («event.plus»): темп и очередь.

Снапшот прематча содержит только топ-линии (~10 рынков на матч), а вся
роспись — тоталы всех линий, форы, таймы, периоды — приезжает отдельным
запросом на КАЖДОЕ событие. Сервер отвечает медленно (порядка двух росписей
в секунду), и всё, что послано сверх этого темпа, просто стоит в очереди.

Раньше событие считалось спрошенным в момент ОТПРАВКИ: круг проходил по
всем ~1800 событиям за две минуты, помечал все спрошенными, а через
WINLINE_PLUS_REFRESH запускался заново поверх неотвеченной очереди. До
половины событий так и оставались с одними топ-линиями — сравнивать с
другими БК было почти нечего, и вилки по ним не находились.

Тесты гоняются офлайн: вместо вебсокета — записывающая заглушка.
"""
import time

import pytest

from app.parsers import wl_feed
from app.parsers.wl_feed import WinlineFeed


class _WS:
    """Заглушка вебсокета: запоминает id событий из отправленных запросов."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    @property
    def asked(self) -> int:
        # на одно событие уходит два кадра: «event.plus» и тело с id
        return sum(1 for s in self.sent if s == "event.plus")


def _feed(n_events: int = 500) -> WinlineFeed:
    f = WinlineFeed(url="ws://test")
    f.pre_events = {i: {"id": i} for i in range(n_events)}
    f.tiplines = {1: {"src": 0, "text": "", "R": [], "countV": 2}}
    f._prematch_ready.set()
    f._plus_budget = 0.0
    f._plus_tick = 0.0
    return f


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    """Ограничение частоты здесь не проверяется — снимаем, чтобы не мешало."""
    monkeypatch.setattr(wl_feed, "WINLINE_PLUS_RATE", 10_000.0)


def _pump(f: WinlineFeed, ws: _WS) -> int:
    before = ws.asked
    f._plus_tick = time.monotonic() - 10.0   # лимит успел накопиться
    f._pump_plus(ws)
    return ws.asked - before


def test_no_more_requests_in_flight_than_the_window(monkeypatch):
    """За раз в полёте не больше окна: сервер всё равно быстрее не ответит."""
    monkeypatch.setattr(wl_feed, "WINLINE_PLUS_WINDOW", 60)
    f, ws = _feed(), _WS()
    assert _pump(f, ws) == 60
    # второй заход не шлёт НИЧЕГО: ответов не было, окно занято
    assert _pump(f, ws) == 0
    print("OK: test_no_more_requests_in_flight_than_the_window")


def test_an_answer_frees_a_slot(monkeypatch):
    """Пришла роспись — освободилось место для следующего события."""
    monkeypatch.setattr(wl_feed, "WINLINE_PLUS_WINDOW", 10)
    f, ws = _feed(), _WS()
    assert _pump(f, ws) == 10
    for eid in list(f._plus_pending)[:4]:
        f._plus_pending.pop(eid)
        f._plus_got[eid] = 1.0
    assert _pump(f, ws) == 4
    print("OK: test_an_answer_frees_a_slot")


def test_events_without_any_markets_go_first(monkeypatch):
    """Событие без росписи важнее, чем обновление уже полученной.

    Иначе таймер обновления гоняет по кругу те же события, а до тех, у кого
    росписи нет вовсе, очередь не доходит никогда.
    """
    monkeypatch.setattr(wl_feed, "WINLINE_PLUS_WINDOW", 5)
    monkeypatch.setattr(wl_feed, "WINLINE_PLUS_REFRESH", 0.0)
    f, ws = _feed(20), _WS()
    # у половины событий роспись уже есть и по таймеру подлежит обновлению
    for eid in range(10):
        f._plus_got[eid] = 1.0
    _pump(f, ws)
    assert set(f._plus_pending) <= set(range(10, 20)), \
        "спрошены события, у которых роспись уже есть, — а без неё остались"
    print("OK: test_events_without_any_markets_go_first")


def test_a_lost_answer_is_asked_again(monkeypatch):
    """Ответ не пришёл за WINLINE_PLUS_PENDING — запрос считаем потерянным."""
    monkeypatch.setattr(wl_feed, "WINLINE_PLUS_WINDOW", 3)
    monkeypatch.setattr(wl_feed, "WINLINE_PLUS_PENDING", 120.0)
    f, ws = _feed(3), _WS()
    assert _pump(f, ws) == 3
    assert _pump(f, ws) == 0
    for eid in f._plus_pending:                 # запросы «состарились»
        f._plus_pending[eid] -= 121.0
    assert _pump(f, ws) == 3
    print("OK: test_a_lost_answer_is_asked_again")


def test_a_fresh_answer_is_not_asked_again(monkeypatch):
    """Роспись только что пришла — повторно не спрашиваем до обновления."""
    monkeypatch.setattr(wl_feed, "WINLINE_PLUS_WINDOW", 100)
    monkeypatch.setattr(wl_feed, "WINLINE_PLUS_REFRESH", 300.0)
    f, ws = _feed(5), _WS()
    assert _pump(f, ws) == 5
    for eid in list(f._plus_pending):
        f._plus_pending.pop(eid)
        f._plus_got[eid] = time.monotonic()
    assert _pump(f, ws) == 0
    print("OK: test_a_fresh_answer_is_not_asked_again")


def test_stale_markets_are_left_out_of_the_snapshot(monkeypatch):
    """Роспись старше предельного возраста в снапшот не попадает.

    Круг по всем событиям идёт около четверти часа, и залежавшийся кэф дал
    бы вилку, которой на сайте давно нет.
    """
    monkeypatch.setattr(wl_feed, "WINLINE_PLUS_MAX_AGE", 1800.0)
    f = _feed(2)
    f.pre_lines = {}
    f.plus_lines = {0: {10: {"id": 10, "event": 0, "tid": 1}},
                    1: {11: {"id": 11, "event": 1, "tid": 1}}}
    now = time.monotonic()
    f._plus_got = {0: now, 1: now - 1801.0}
    *_, lines = f.prematch_snapshot()
    assert [ln["id"] for ln in lines] == [10], \
        "в снапшот попала роспись старше предельного возраста"
    print("OK: test_stale_markets_are_left_out_of_the_snapshot")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
