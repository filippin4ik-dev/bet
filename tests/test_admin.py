"""Sanity-тесты админки: шифрование, сессии, аккаунты, расчёт лимитов.

Запуск: python3 tests/test_admin.py
Использует ВРЕМЕННУЮ базу (не трогает arbs.sqlite3 из рабочего каталога).
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmpdir = tempfile.mkdtemp()
os.environ["DB_PATH"] = str(Path(_tmpdir) / "test.sqlite3")
os.environ["SECRET_KEY"] = "test-secret-key-not-for-prod"

from app import accounts_manager, autobet, config, db  # noqa: E402
from app.security import (create_session_token, decrypt_str, encrypt_str,  # noqa: E402
                          verify_admin_password, verify_session_token)

db.init_db()


def test_encrypt_roundtrip():
    enc = encrypt_str("s3cr3t-login")
    assert enc != "s3cr3t-login"
    assert decrypt_str(enc) == "s3cr3t-login"
    print("OK: test_encrypt_roundtrip")


def test_admin_password_check():
    assert verify_admin_password(config.ADMIN_USERNAME, config.ADMIN_PASSWORD)
    assert not verify_admin_password(config.ADMIN_USERNAME, "wrong")
    assert not verify_admin_password("wrong", config.ADMIN_PASSWORD)
    print("OK: test_admin_password_check")


def test_session_token_roundtrip():
    token = create_session_token("admin")
    assert verify_session_token(token) == "admin"
    assert verify_session_token(token + "x") is None
    assert verify_session_token(None) is None
    assert verify_session_token("garbage") is None
    print("OK: test_session_token_roundtrip")


def test_account_crud_and_balance():
    account_id = db.add_account("Fonbet", "user@example.com", "hunter2",
                                label="test")
    accounts = db.list_accounts()
    assert any(a["id"] == account_id for a in accounts)
    acc = db.get_account(account_id)
    assert acc["login"].startswith("us")  # маскировано
    creds = db.get_account_credentials(account_id)
    assert creds == ("user@example.com", "hunter2")

    db.set_account_balance(account_id, 12345.67, None)
    acc = db.get_account(account_id)
    assert acc["balance"] == 12345.67

    balances = accounts_manager.available_balance_by_bookmaker()
    assert balances.get("Fonbet") == 12345.67

    cap = accounts_manager.max_stake_for_bookmaker("Fonbet", balances)
    expected = min(12345.67 * config.AUTOBET_MAX_BALANCE_FRACTION,
                   config.AUTOBET_MAX_STAKE)
    assert abs(cap - round(expected, 2)) < 1e-6

    db.delete_account(account_id)
    assert db.get_account(account_id) is None
    print("OK: test_account_crud_and_balance")


def test_autobet_plan_without_accounts_is_zero():
    """Без подключённых аккаунтов план не должен «придумывать» ставку —
    честно возвращает stake=0 и пояснение."""
    arb = {
        "match_key": "test|match", "sport": "Футбол", "match": "A — B",
        "market": "Победитель", "outcome1": "П1", "outcome2": "П2",
        "k1_max": 2.1, "k1_bookmaker": "НетТакойБК1",
        "k2_max": 2.1, "k2_bookmaker": "НетТакойБК2",
        "profit_pct": 5.0, "k1_url": None, "k2_url": None,
    }
    plan = autobet.build_plan_2way(arb)
    assert plan.legs[0].stake == 0.0
    assert plan.legs[1].stake == 0.0
    assert plan.note is not None
    print("OK: test_autobet_plan_without_accounts_is_zero")


def test_autobet_plan_with_balances_caps_stake():
    a1 = db.add_account("Winline", "u1", "p1")
    a2 = db.add_account("BetBoom", "u2", "p2")
    db.set_account_balance(a1, 1000.0, None)
    db.set_account_balance(a2, 1000.0, None)
    arb = {
        "match_key": "test|match2", "sport": "Футбол", "match": "A — B",
        "market": "Победитель", "outcome1": "П1", "outcome2": "П2",
        "k1_max": 2.0, "k1_bookmaker": "Winline",
        "k2_max": 2.0, "k2_bookmaker": "BetBoom",
        "profit_pct": 3.0, "k1_url": None, "k2_url": None,
    }
    plan = autobet.build_plan_2way(arb)
    assert plan.note is None
    assert plan.legs[0].stake > 0
    assert plan.legs[1].stake > 0
    # ставка не должна превышать разрешённую долю баланса
    cap = min(1000.0 * config.AUTOBET_MAX_BALANCE_FRACTION,
              config.AUTOBET_MAX_STAKE)
    assert plan.legs[0].stake <= cap + 1
    assert plan.legs[1].stake <= cap + 1

    # dry-run исполнение — без реальных запросов, но с полным журналом
    results = autobet.execute_plan(plan, dry_run=True)
    assert all(r["ok"] for r in results)
    db.delete_account(a1)
    db.delete_account(a2)
    print("OK: test_autobet_plan_with_balances_caps_stake")


def test_place_on_arb_logs_bet():
    arb = {
        "match_key": "test|match3", "sport": "Футбол", "match": "C — D",
        "market": "Победитель", "outcome1": "П1", "outcome2": "П2",
        "k1_max": 2.0, "k1_bookmaker": "НетБК1",
        "k2_max": 2.0, "k2_bookmaker": "НетБК2",
        "profit_pct": 3.0, "k1_url": None, "k2_url": None,
    }
    before = len(db.get_bet_log(1000))
    res = autobet.place_on_arb(arb, kind3=False, dry_run=True)
    assert res["dry_run"] is True
    after = db.get_bet_log(1000)
    assert len(after) == before + 1
    assert after[0]["match"] == "C — D"
    print("OK: test_place_on_arb_logs_bet")


if __name__ == "__main__":
    test_encrypt_roundtrip()
    test_admin_password_check()
    test_session_token_roundtrip()
    test_account_crud_and_balance()
    test_autobet_plan_without_accounts_is_zero()
    test_autobet_plan_with_balances_caps_stake()
    test_place_on_arb_logs_bet()
    print("Все тесты прошли.")
