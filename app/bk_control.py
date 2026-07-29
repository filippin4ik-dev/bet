"""Включение и выключение отдельных БК из админки.

Зачем: у каждой БК свои беды. Betcity рвёт соединение с IP, который слишком
часто просит всю линию; Winline и BetBoom тянут многомегабайтные
websocket-фиды и едят память; у зарубежной bc.game расходятся написания
имён команд. Когда какая-то БК мешает, её нужно уметь погасить, не трогая
остальные и не перезапуская сервер.

Флаг каждой БК хранится в базе (таблица settings), поэтому выключенная БК
остаётся выключенной и после перезапуска. Воркеры сканера читают его на
каждом витке цикла (см. Scanner._worker), так что выключение действует
сразу же, а котировки погашенной БК забываются — держать в вилках кэфы,
которые никто не обновляет, нельзя.
"""
import threading
import time

from . import db

_PREFIX = "bk_enabled:"

# Флаг читается воркером каждой БК на каждом витке (и на каждый запрос
# админки), поэтому значения из базы кэшируются на секунду.
_CACHE_TTL = 1.0
_cache: dict[str, tuple[float, bool]] = {}
_lock = threading.Lock()


def is_enabled(name: str) -> bool:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(name)
        if hit is not None and now - hit[0] < _CACHE_TTL:
            return hit[1]
    value = db.get_bool_setting(_PREFIX + name, True)
    with _lock:
        _cache[name] = (now, value)
    return value


def set_enabled(name: str, value: bool) -> None:
    db.set_bool_setting(_PREFIX + name, value)
    with _lock:
        _cache[name] = (time.monotonic(), value)
