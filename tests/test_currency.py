"""Тесты валюты отображения и курса доллара (app/currency.py).

Главное здесь — не дать показать деньги по выдуманному курсу: доллар без
заданного курса не включается, курс вне разумных границ не сохраняется, а
баланс крипто-БК без курса не идёт в лимит ставки (иначе он был бы завышен
почти в сто раз).

Использует ВРЕМЕННУЮ базу (не трогает arbs.sqlite3 из рабочего каталога).
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmpdir = tempfile.mkdtemp()
os.environ["DB_PATH"] = str(Path(_tmpdir) / "currency-test.sqlite3")
os.environ["SECRET_KEY"] = "test-secret-key-not-for-prod"

import pytest  # noqa: E402

from app import accounts_manager, config, currency, db  # noqa: E402

db.init_db()


def _reset():
    db.set_setting("usd_rub_rate", "")
    db.set_setting("display_currency", "RUB")
    currency.invalidate_cache()


def test_rubles_until_the_rate_is_known():
    _reset()
    assert currency.currency() == "RUB"
    assert currency.usd_rub() is None
    state = currency.state()
    assert state["symbol"] == "₽"
    assert state["banks"] == [1000, 5000, 10000]
    assert state["rate_source"] == "none"


def test_dollars_cannot_be_switched_on_without_a_rate():
    _reset()
    with pytest.raises(ValueError) as exc:
        currency.set_currency("USD")
    assert "курс" in str(exc.value).lower()


def test_rate_and_currency_from_the_admin_panel():
    _reset()
    assert currency.set_usd_rub("92,5") == 92.5      # запятая тоже число
    assert currency.set_currency("USD") == "USD"
    state = currency.state()
    assert state["currency"] == "USD"
    assert state["symbol"] == "$"
    assert state["usd_rub"] == 92.5
    assert state["rate_source"] == "manual"
    # 9 250 ₽ по курсу 92.5 — это 100 долларов
    assert currency.from_rub(9250) == 100.0
    assert currency.to_rub(100) == 9250.0
    # банки для доллара — свои круглые, а не «$54.05»
    assert state["banks"] == [10, 50, 100]
    _reset()


def test_absurd_rate_is_rejected():
    _reset()
    for bad in ("0", "-5", "1000000", "девяносто"):
        with pytest.raises(ValueError):
            currency.set_usd_rub(bad)
    assert currency.usd_rub() is None


def test_usdt_counts_by_the_dollar_rate():
    _reset()
    currency.set_usd_rub(80)
    assert currency.rub_per_unit("USDT") == 80
    assert currency.to_rub(2.5, "USDT") == 200.0
    _reset()


def test_crypto_bookmaker_holds_dollars():
    assert currency.bookmaker_currency("bc.game") == "USDT"
    assert currency.bookmaker_currency("Winline") == "RUB"


def test_losing_the_rate_returns_the_site_to_rubles():
    """Курс убрали, а валюта в базе осталась долларом: показываем рубли,
    а не суммы, поделённые на неизвестно что."""
    _reset()
    currency.set_usd_rub(90)
    currency.set_currency("USD")
    db.set_setting("usd_rub_rate", "")
    currency.invalidate_cache()
    assert currency.currency() == "RUB"
    _reset()


def test_crypto_balance_goes_into_limits_only_with_a_rate():
    _reset()
    account = db.add_account("bc.game", "player", "secret", label="крипто")
    db.set_account_balance(account, 100.0, None)     # 100 долларов на счету
    try:
        # курса нет — баланс не учитываем вовсе: лимит ставки по нему был
        # бы посчитан как «100 ₽», а с рублёвым знаком в интерфейсе это
        # выглядело бы правдоподобно
        assert accounts_manager.available_balance_by_bookmaker() == {}

        currency.set_usd_rub(90)
        balances = accounts_manager.available_balance_by_bookmaker()
        assert balances["bc.game"] == 9000.0
        cap = accounts_manager.max_stake_for_bookmaker("bc.game", balances)
        assert cap == round(min(9000 * config.AUTOBET_MAX_BALANCE_FRACTION,
                                config.AUTOBET_MAX_STAKE), 2)
    finally:
        db.delete_account(account)
        _reset()
