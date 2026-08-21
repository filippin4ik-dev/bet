"""Управление аккаунтами БК: обновление баланса и агрегация лимитов
ставки по каждой БК (используется и админкой, и расчётом max_stake
в выдаче вилок)."""
import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from . import config, currency, db
from .connectors import get_connector

log = logging.getLogger("accounts")

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="balance")


def refresh_balance(account_id: int) -> None:
    """Синхронно обновляет баланс одного аккаунта (для вызова из пула)."""
    acc = db.get_account(account_id)
    if acc is None:
        return
    creds = db.get_account_credentials(account_id)
    if creds is None:
        return
    login, password = creds
    cookies = db.get_account_cookies(account_id)
    connector = get_connector(acc["bookmaker"], login, password,
                              account_id=account_id, cookies=cookies,
                              login_type=acc.get("login_type"))
    try:
        balance = connector.get_balance()
        db.set_account_balance(account_id, balance, None)
        # Баланс хранится и логируется в валюте САМОЙ БК: у крипто-БК это
        # доллары, и «превращать» их в рубли при чтении нельзя — курс
        # меняется, а число в базе осталось бы старым.
        log.info("Баланс %s (%s) обновлён: %.2f %s",
                 acc["bookmaker"], acc["label"] or account_id, balance,
                 currency.info(
                     currency.bookmaker_currency(acc["bookmaker"]))["symbol"])
    except Exception as exc:  # noqa: BLE001
        db.set_account_balance(account_id, None, str(exc))
        log.warning("Не удалось обновить баланс %s (%s): %s",
                    acc["bookmaker"], acc["label"] or account_id, exc)
    finally:
        connector.close()


def refresh_balance_async(account_id: int) -> None:
    _executor.submit(refresh_balance, account_id)


def refresh_all_async() -> None:
    for acc in db.list_accounts():
        if acc["enabled"]:
            refresh_balance_async(acc["id"])


def available_balance_by_bookmaker() -> dict[str, float]:
    """Суммарный известный баланс по каждой БК, в РУБЛЯХ.

    Аккаунты без проверенного баланса (ещё не обновлялись/ошибка) не
    учитываются — так что после первого добавления аккаунта лимит
    появится не раньше первого успешного refresh.

    Баланс крипто-БК приходит в долларах и пересчитывается курсом из
    админки. Курс не задан — баланс такой БК считается НЕизвестным: лимит
    ставки без пересчёта был бы завышен почти в сто раз, а по нему
    ставятся настоящие деньги."""
    totals: dict[str, float] = {}
    for acc in db.list_accounts():
        if not acc["enabled"] or acc.get("balance") is None:
            continue
        bk = acc["bookmaker"]
        in_rub = currency.to_rub(acc["balance"],
                                 currency.bookmaker_currency(bk))
        if in_rub is None:
            log.warning("Баланс %s не учтён в лимитах: курс доллара к рублю "
                        "не задан (задайте его в админке, раздел «Валюта»)",
                        bk)
            continue
        totals[bk] = totals.get(bk, 0.0) + in_rub
    return totals


def max_stake_for_bookmaker(bookmaker: str,
                            balances: dict[str, float] | None = None
                            ) -> float | None:
    """Максимально безопасная разовая ставка в этой БК: доля от баланса,
    не выше жёсткого потолка AUTOBET_MAX_STAKE. None — баланс неизвестен
    (аккаунт не подключён/не обновлялся)."""
    balances = balances if balances is not None else \
        available_balance_by_bookmaker()
    bal = balances.get(bookmaker)
    if bal is None:
        return None
    cap = min(bal * config.AUTOBET_MAX_BALANCE_FRACTION,
              config.AUTOBET_MAX_STAKE)
    return round(max(cap, 0.0), 2)


class BalanceRefreshLoop:
    """Фоновый цикл периодического обновления балансов подключённых
    аккаунтов (запускается из lifespan приложения)."""

    def __init__(self) -> None:
        self._stop = threading.Event()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                refresh_all_async()
            except Exception as exc:  # noqa: BLE001
                log.warning("Ошибка авто-обновления балансов: %s", exc)
            await asyncio.sleep(config.BALANCE_REFRESH_INTERVAL)

    def stop(self) -> None:
        self._stop.set()
