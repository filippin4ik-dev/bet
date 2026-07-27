"""Ретрансляция SMS/OTP-кода при входе в аккаунт БК.

Некоторые БК запрашивают код подтверждения (СМС/пуш) при входе с нового
устройства — типичная реакция на вход из дата-центра/нового браузерного
профиля (см. предыдущее обсуждение про VPS). Полностью убрать этот шаг
нельзя (это защита самой БК), но можно ПЕРЕДАТЬ код от человека боту:

1. Коннектор (в фоновом потоке) заходит на сайт, вводит логин/пароль;
2. если сайт показал поле для кода — коннектор вызывает request_otp()
   и БЛОКИРУЕТСЯ (поток фоновый, event loop FastAPI не тормозит);
3. админка видит ожидающий запрос (GET /api/admin/otp_pending) и
   показывает оператору диалог «введите код»;
4. оператор вводит код, админка отправляет его через submit_otp();
5. request_otp() возвращает код, коннектор вписывает его в поле на
   сайте и продолжает вход.

Хранилище — только в памяти процесса (коды не переживают перезапуск
сервера: если сервер перезапустился во время ожидания, попытка входа
просто оборвётся по таймауту).

⚠️ Детект поля кода (см. OTP_SELECTORS в selenium_generic.py) —
эвристика по типовой вёрстке, НЕ проверенная на живом запросе СМС ни
у одной БК (см. общее предупреждение в selenium_generic.py)."""
import threading
import time

_lock = threading.Lock()
_pending: dict[int, dict] = {}


def request_otp(account_id: int, bookmaker: str, timeout: float = 180) -> str:
    """Блокирует вызывающий поток, пока оператор не введёт код в админке
    (или не истечёт timeout секунд). Бросает TimeoutError при таймауте."""
    event = threading.Event()
    with _lock:
        _pending[account_id] = {
            "bookmaker": bookmaker, "event": event, "code": None,
            "requested_at": time.time(),
        }
    try:
        if not event.wait(timeout):
            raise TimeoutError(
                f"Код подтверждения для аккаунта «{bookmaker}» не был "
                f"введён за {timeout:.0f} с — вход отменён.")
        with _lock:
            entry = _pending.get(account_id)
            code = entry["code"] if entry else None
        if not code:
            raise RuntimeError("Код подтверждения не получен")
        return code
    finally:
        with _lock:
            _pending.pop(account_id, None)


def submit_otp(account_id: int, code: str) -> bool:
    """Вызывается из API, когда оператор ввёл код в админке."""
    with _lock:
        entry = _pending.get(account_id)
        if entry is None:
            return False
        entry["code"] = code
        entry["event"].set()
    return True


def pending_accounts() -> list[dict]:
    """Аккаунты, которые СЕЙЧАС ждут код (для показа диалога в админке)."""
    with _lock:
        return [
            {"account_id": aid, "bookmaker": e["bookmaker"],
             "requested_at": e["requested_at"]}
            for aid, e in _pending.items()
        ]
