"""Тесты цикла сканера: по потоку на каждую БК + переключение лайва.

Использует ВРЕМЕННУЮ базу (история вилок пишется в SQLite) и подставные
парсеры вместо настоящих БК — сети здесь нет.
"""
import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmpdir = tempfile.mkdtemp()
os.environ["DB_PATH"] = str(Path(_tmpdir) / "scanner-test.sqlite3")
os.environ["SECRET_KEY"] = "test-secret-key-not-for-prod"

from app import config, db  # noqa: E402
from app.models import KIND_LIVE, KIND_PREMATCH, MarketOdds  # noqa: E402
from app.parsers.base import BaseParser  # noqa: E402
from app.scanner import Scanner  # noqa: E402

db.init_db()


def _odds(bk, k1=2.0, k2=2.0, market_key="winner"):
    return [MarketOdds(
        bookmaker=bk, sport="Футбол", team1="Спартак", team2="Зенит",
        market="Победитель", market_key=market_key,
        outcome1="П1", outcome2="П2", k1=k1, k2=k2,
        start_ts=time.time() + 3600, start_time="01.01 20:00")]


class _Fake(BaseParser):
    """Парсер без сети: считает обходы и может «тормозить»."""

    def __init__(self, name, delay=0.0, odds=None):
        super().__init__()
        self.name = name
        self.delay = delay
        self.calls = 0
        self._odds = odds
        self.busy_seen = threading.Event()

    def fetch_odds(self):
        self.calls += 1
        self.busy_seen.set()
        if self.delay:
            time.sleep(self.delay)
        return self._odds if self._odds is not None else _odds(self.name)


class _FakeLive(_Fake):
    """БК с поддержкой лайва (переопределяет fetch_live_odds)."""

    def fetch_live_odds(self):
        return self.fetch_odds()


async def _run_for(scanner, seconds, on_tick=None):
    task = asyncio.create_task(scanner.run())
    try:
        if on_tick is None:
            await asyncio.sleep(seconds)
        else:
            await on_tick()
    finally:
        scanner.stop()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()


def test_slow_bookmaker_does_not_block_fast_one():
    """Главное свойство: БК не ждут друг друга. Раньше общий цикл ждал
    самую медленную БК, и быстрая всё это время стояла — в интерфейсе
    висело «Fonbet: 7 минут назад» при обходе в полминуты."""
    fast = _Fake("Быстрая")
    slow = _Fake("Медленная", delay=3.0)
    sc = Scanner(mode=KIND_PREMATCH, parsers=[fast, slow])
    sc.interval = 0.1  # период обхода одной БК (не меньше пола в 0.5 с)
    asyncio.run(_run_for(sc, 2.0))
    assert slow.calls == 1, "медленная БК успевает ровно один обход"
    assert fast.calls >= 3, (
        f"быстрая БК должна обновиться несколько раз, а не ждать медленную "
        f"(обходов: {fast.calls})")


def test_parser_min_refresh_respected():
    """У БК со своим ограничением (Betcity: снимок всей линии тяжёлый, и на
    частых обходах её сервер рвёт соединение) период обхода не меньше
    её собственного, даже если общий SCAN_INTERVAL меньше."""
    greedy = _Fake("Обычная")
    gentle = _Fake("Тяжёлая")
    gentle.min_refresh = 3.0
    sc = Scanner(mode=KIND_PREMATCH, parsers=[greedy, gentle])
    sc.interval = 0.1
    asyncio.run(_run_for(sc, 1.5))
    assert gentle.calls == 1, "тяжёлую БК нельзя опрашивать чаще её периода"
    assert greedy.calls >= 2


def test_failing_bookmaker_backs_off():
    """БК отвечает пусто (не отвечает сервер, защита рвёт соединение) —
    сканер отступает, а не долбит её прежним темпом: пользы это не даёт,
    а защита БК от такого только злее. Котировки живут до ODDS_TTL."""
    dead = _Fake("Молчит", odds=[])
    sc = Scanner(mode=KIND_PREMATCH, parsers=[dead])
    sc.interval = 0.5
    asyncio.run(_run_for(sc, 3.0))
    # без отступа было бы ~6 обходов: 0.5 + 1 + 2 + (4) — три-четыре
    assert dead.calls <= 4, f"обходов слишком много: {dead.calls}"
    assert dead.calls >= 2


def test_bookmaker_marked_busy_while_fetching():
    """Пока БК качает линию, в статусе стоит busy — в интерфейсе вместо
    возраста котировок видно «обновляется…», а не «зависла»."""
    slow = _Fake("Медленная", delay=1.5)
    sc = Scanner(mode=KIND_PREMATCH, parsers=[slow])

    async def check():
        # ждать блокирующим Event.wait() нельзя: он остановил бы сам цикл
        # событий, и воркеры не успели бы запуститься
        for _ in range(40):
            if slow.busy_seen.is_set():
                break
            await asyncio.sleep(0.1)
        snap = sc.snapshot()
        assert snap["bookmakers"]["Медленная"]["busy"] is True
        assert snap["scanning"] is True

    asyncio.run(_run_for(sc, 0, on_tick=check))


def test_empty_fetch_keeps_previous_odds():
    """Сбой обхода (пустой результат) не должен затирать котировки —
    матчи не «слетают», пока не истёк ODDS_TTL."""
    p = _Fake("БК")
    sc = Scanner(mode=KIND_PREMATCH, parsers=[p])
    sc._store_odds("БК", _odds("БК"))
    sc._recalc()
    assert sc.snapshot()["quotes_checked"] == 1
    sc._store_odds("БК", [])
    sc._recalc()
    assert sc.snapshot()["quotes_checked"] == 1


def test_stale_odds_dropped_after_ttl():
    p = _Fake("БК")
    sc = Scanner(mode=KIND_PREMATCH, parsers=[p])
    sc.ttl = 0.05
    sc._store_odds("БК", _odds("БК"))
    sc._recalc()
    time.sleep(0.1)
    sc._store_odds("БК", [])
    sc._recalc()
    assert sc.snapshot()["quotes_checked"] == 0


def test_recalc_coalesces_updates():
    """Пересчёт вилок стоит секунды на сотнях тысяч котировок, поэтому он
    один на сканер: пока он идёт, обновления БК копятся и схлопываются в
    один следующий проход (иначе каждая БК платила бы за полный пересчёт и
    обходы растягивались бы в разы)."""
    p1, p2, p3 = (_Fake(f"БК{i}") for i in (1, 2, 3))
    sc = Scanner(mode=KIND_PREMATCH, parsers=[p1, p2, p3])
    sc.interval = 0.1
    calls = []
    real_recalc = sc._recalc

    def slow_recalc():
        calls.append(time.monotonic())
        time.sleep(0.5)
        return real_recalc()

    sc._recalc = slow_recalc
    asyncio.run(_run_for(sc, 0.8))
    fetches = p1.calls + p2.calls + p3.calls
    assert fetches >= 3
    assert len(calls) <= 2, (
        f"пересчётов должно быть меньше, чем обходов БК "
        f"(обходов {fetches}, пересчётов {len(calls)})")
    assert calls, "хоть один пересчёт должен был случиться"


def test_prematch_ignores_live_switch():
    """Переключатель в админке гасит только лайв: прематч — основной
    режим и работает всегда."""
    sc = Scanner(mode=KIND_PREMATCH, parsers=[_Fake("БК")])
    before = config.LIVE_ENABLED
    try:
        config.LIVE_ENABLED = False
        assert sc.enabled() is True
    finally:
        config.LIVE_ENABLED = before


def test_live_scanner_idles_while_disabled():
    """Выключенный лайв не должен трогать БК вообще — ресурсы прематчу."""
    p = _FakeLive("Лайв-БК")
    sc = Scanner(mode=KIND_LIVE, parsers=[p])
    before = config.LIVE_ENABLED
    try:
        config.LIVE_ENABLED = False
        asyncio.run(_run_for(sc, 1.0))
        assert p.calls == 0
        assert sc.is_running() is False
    finally:
        config.LIVE_ENABLED = before


def test_live_scanner_starts_and_stops_at_runtime():
    """Переключатель применяется на ходу: включили — воркеры поднялись,
    выключили — остановились и забыли котировки (показывать кэфы режима,
    который больше не обновляется, нельзя)."""
    p = _FakeLive("Лайв-БК")
    sc = Scanner(mode=KIND_LIVE, parsers=[p])
    before = config.LIVE_ENABLED

    async def scenario():
        config.LIVE_ENABLED = True
        for _ in range(40):
            if p.calls and sc.is_running():
                break
            await asyncio.sleep(0.1)
        assert p.calls >= 1, "включённый лайв должен опрашивать БК"
        assert sc.snapshot()["quotes_checked"] == 1

        config.LIVE_ENABLED = False
        for _ in range(40):
            if not sc.is_running():
                break
            await asyncio.sleep(0.1)
        assert sc.is_running() is False
        snap = sc.snapshot()
        assert snap["quotes_checked"] == 0, "котировки выключенного лайва — забыть"
        assert snap["bookmakers"] == {}
        calls_after_stop = p.calls
        await asyncio.sleep(0.5)
        assert p.calls == calls_after_stop, "остановленный лайв не шлёт запросы"

    try:
        asyncio.run(_run_for(sc, 0, on_tick=scenario))
    finally:
        config.LIVE_ENABLED = before


def test_live_scanner_without_live_capable_bk_idles():
    """Ни одна БК не отдаёт лайв — сканер просто простаивает."""
    sc = Scanner(mode=KIND_LIVE, parsers=[_Fake("Только прематч")])
    asyncio.run(_run_for(sc, 0.3))
    assert sc.is_running() is False


def test_live_switch_persisted_in_db():
    """Оператор выключил лайв — после перезапуска он остаётся выключенным
    (в отличие от флагов авто-ставки, которые сознательно только runtime)."""
    key = "test_live_enabled"  # своё имя: настоящий флаг могли записать
    # нет записи — берётся значение по умолчанию (из переменной окружения)
    assert db.get_bool_setting(key, True) is True
    assert db.get_bool_setting(key, False) is False
    db.set_bool_setting(key, False)
    assert db.get_bool_setting(key, True) is False
    db.set_bool_setting(key, True)
    assert db.get_bool_setting(key, False) is True


def test_new_arbs_saved_to_history_once():
    """Вилка пишется в историю один раз, пока живёт между обходами."""
    p = _Fake("БК")
    sc = Scanner(mode=KIND_PREMATCH, parsers=[p])
    a = [type("A", (), {"match_key": "k", "kind": KIND_PREMATCH,
                        "start_time": None, "sport": "Футбол",
                        "team1": "A", "team2": "B", "market": "Победитель",
                        "outcome1": "П1", "outcome2": "П2", "k1_max": 2.1,
                        "k1_bookmaker": "БК", "k2_max": 2.1,
                        "k2_bookmaker": "БК2", "profit_pct": 3.0,
                        "stakes": {}})()]
    before = len(db.get_history(1000))
    sc._save_new(a, [])
    sc._save_new(a, [])
    assert len(db.get_history(1000)) == before + 1


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"OK: {name}")
    print("Все тесты прошли.")
