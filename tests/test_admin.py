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

import threading  # noqa: E402
import time  # noqa: E402

from app import accounts_manager, autobet, config, db, otp  # noqa: E402
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


def test_account_cookies_roundtrip():
    """Cookie — опциональная альтернатива автологину (см. README «Вход по
    cookie»): хранится зашифрованной, никогда не отдаётся по API как
    значение (только признак has_cookies), можно очистить пустой строкой."""
    account_id = db.add_account("BetBoom", "u-cookie", "p-cookie",
                                label="cookie-test",
                                cookies="sid=abc123; other=xyz")
    acc = db.get_account(account_id)
    assert acc["has_cookies"] is True
    assert "cookies" not in acc and "cookies_enc" not in acc
    assert db.get_account_cookies(account_id) == "sid=abc123; other=xyz"

    # обновление на другое значение
    db.update_account(account_id, cookies="new=1")
    assert db.get_account_cookies(account_id) == "new=1"
    assert db.get_account(account_id)["has_cookies"] is True

    # явная очистка пустой строкой
    db.update_account(account_id, cookies="")
    assert db.get_account_cookies(account_id) is None
    assert db.get_account(account_id)["has_cookies"] is False

    # аккаунт без cookies изначально
    account_id2 = db.add_account("Winline", "u2", "p2")
    assert db.get_account_cookies(account_id2) is None
    assert db.get_account(account_id2)["has_cookies"] is False

    db.delete_account(account_id)
    db.delete_account(account_id2)
    print("OK: test_account_cookies_roundtrip")


def test_parse_cookie_string():
    from app.connectors.selenium_generic import _parse_cookie_string
    assert _parse_cookie_string("a=1; b=2") == [
        {"name": "a", "value": "1"}, {"name": "b", "value": "2"}]
    assert _parse_cookie_string("") == []
    assert _parse_cookie_string("garbage;;  ; c=") == [{"name": "c", "value": ""}]
    # значение может содержать "=" (напр. base64 с паддингом)
    assert _parse_cookie_string("t=abc=def==") == [
        {"name": "t", "value": "abc=def=="}]
    # записи "ls:key=value" (localStorage, напр. JWT-токен BetBoom) — не
    # обычные cookie, _parse_cookie_string их пропускает
    assert _parse_cookie_string("a=1; ls:token=eyJ.x.y; b=2") == [
        {"name": "a", "value": "1"}, {"name": "b", "value": "2"}]
    print("OK: test_parse_cookie_string")


def test_parse_local_storage_string():
    """"ls:key=value" — сессия БК типа BetBoom, хранящаяся не в cookie, а
    в window.localStorage (JWT-токен) — см. докстринг selenium_generic.py
    про Qrator/BetBoom и README «Вход по cookie»."""
    from app.connectors.selenium_generic import _parse_local_storage_string
    assert _parse_local_storage_string("ls:token=eyJ.x.y") == {
        "token": "eyJ.x.y"}
    # смешанная строка: обычные cookie игнорируются, ls: — разбираются
    assert _parse_local_storage_string("a=1; ls:token=abc; ls:other=xyz") == {
        "token": "abc", "other": "xyz"}
    assert _parse_local_storage_string("") == {}
    assert _parse_local_storage_string("a=1; b=2") == {}
    # значение с "=" (base64-паддинг) сохраняется целиком
    assert _parse_local_storage_string("ls:t=abc=def==") == {
        "t": "abc=def=="}
    print("OK: test_parse_local_storage_string")


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


def test_live_toggle_settings():
    """Переключатель лайва в админке: применяется на ходу (правит
    config.LIVE_ENABLED, который читает сканер) И сохраняется в базе —
    выключенный лайв остаётся выключенным после перезапуска. Флаги
    авто-ставки, наоборот, сознательно только runtime."""
    from app import admin_api
    before = config.LIVE_ENABLED
    try:
        admin_api.update_settings(
            admin_api.SettingsBody(live_enabled=False), username="admin")
        assert config.LIVE_ENABLED is False
        assert db.get_bool_setting("live_enabled", True) is False
        assert admin_api.get_settings(username="admin")["live_enabled"] is False

        admin_api.update_settings(
            admin_api.SettingsBody(live_enabled=True), username="admin")
        assert config.LIVE_ENABLED is True
        assert db.get_bool_setting("live_enabled", False) is True

        # автобет-флаги в базу не попадают
        admin_api.update_settings(
            admin_api.SettingsBody(autobet_dry_run=True), username="admin")
        assert db.get_setting("autobet_dry_run") is None
    finally:
        config.LIVE_ENABLED = before
    print("OK: test_live_toggle_settings")


def test_settings_report_scanner_state():
    from app import admin_api
    s = admin_api.get_settings(username="admin")
    assert s["scan_interval"] == config.SCAN_INTERVAL
    assert s["live_running"] is False  # сканеры в тестах не запущены
    print("OK: test_settings_report_scanner_state")


def test_parser_endpoints_toggle_and_restart():
    """Админка управляет каждой БК отдельно: список с состоянием, вкл/выкл
    (переживает перезапуск) и перезапуск одной БК."""
    from fastapi import HTTPException

    from app import admin_api, bk_control
    data = admin_api.list_parsers(username="admin")
    names = [p["name"] for p in data["parsers"]]
    assert "Winline" in names and "Fonbet" in names
    assert all(p["enabled"] for p in data["parsers"]), \
        "по умолчанию включены все БК"
    assert data["scan_interval"] == config.SCAN_INTERVAL

    try:
        res = admin_api.update_parser(
            "Winline", admin_api.ParserBody(enabled=False), username="admin")
        assert res["enabled"] is False
        assert bk_control.is_enabled("Winline") is False
        assert db.get_bool_setting("bk_enabled:Winline", True) is False
        state = next(p for p in admin_api.list_parsers(username="admin")["parsers"]
                     if p["name"] == "Winline")
        assert state["enabled"] is False
    finally:
        admin_api.update_parser("Winline", admin_api.ParserBody(enabled=True),
                                username="admin")
    assert bk_control.is_enabled("Winline") is True

    assert admin_api.restart_parser("Fonbet", username="admin")["ok"] is True
    for name in ("НетТакойБК",):
        for call in (lambda: admin_api.update_parser(
                        name, admin_api.ParserBody(enabled=False),
                        username="admin"),
                     lambda: admin_api.restart_parser(name, username="admin")):
            try:
                call()
            except HTTPException as exc:
                assert exc.status_code == 404
            else:
                raise AssertionError("неизвестная БК должна давать 404")
    print("OK: test_parser_endpoints_toggle_and_restart")


def test_scanner_restart_endpoint():
    from fastapi import HTTPException

    from app import admin_api
    from app.runtime import live_scanner, scanner
    assert admin_api.restart_scanner(
        admin_api.ScannerRestartBody(mode="all"), username="admin")["ok"]
    assert scanner._restart_all is True
    assert live_scanner._restart_all is True
    scanner._restart_all = False
    live_scanner._restart_all = False
    try:
        admin_api.restart_scanner(admin_api.ScannerRestartBody(mode="боком"),
                                  username="admin")
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("непонятный режим должен отвергаться")
    print("OK: test_scanner_restart_endpoint")


def test_restart_argv_repeats_original_command():
    """Перезапуск сервера должен повторять исходную команду целиком.

    Проверено на живом сервере: склейка sys.executable + sys.argv ломает
    запуск через `python -m uvicorn` — интерпретатор получает путь к
    uvicorn/__main__.py, кладёт его каталог в sys.path, и `import logging`
    внутри uvicorn находит uvicorn/logging.py вместо стандартного модуля.
    Процесс падает на циклическом импорте, и сайт не поднимается."""
    import sys

    from app import admin_api
    orig = getattr(sys, "orig_argv", None)
    try:
        sys.orig_argv = [sys.executable, "-m", "uvicorn", "app.main:app",
                         "--port", "8000"]
        assert admin_api.restart_argv() == sys.orig_argv
        # интерпретатор без ключей: команда тоже воспроизводится как есть
        sys.orig_argv = [sys.executable, "run.py"]
        assert admin_api.restart_argv() == [sys.executable, "run.py"]
        # очень старый Python без orig_argv — хотя бы не падаем
        del sys.orig_argv
        assert admin_api.restart_argv()[0] == sys.executable
    finally:
        if orig is None:
            if hasattr(sys, "orig_argv"):
                del sys.orig_argv
        else:
            sys.orig_argv = orig
    print("OK: test_restart_argv_repeats_original_command")


def test_otp_relay_roundtrip():
    """Коннектор (фоновый поток) ждёт код, админка вводит его — код
    должен дойти обратно до заблокированного потока."""
    result = {}

    def worker():
        result["code"] = otp.request_otp(999, "Fonbet", timeout=5)

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.3)
    pending = otp.pending_accounts()
    assert any(p["account_id"] == 999 for p in pending)
    assert otp.submit_otp(999, "654321")
    t.join(3)
    assert result.get("code") == "654321"
    assert not any(p["account_id"] == 999 for p in otp.pending_accounts())
    print("OK: test_otp_relay_roundtrip")


def test_otp_relay_timeout():
    """Если оператор не успел ввести код — поток коннектора должен
    получить понятную ошибку, а не зависнуть навечно."""
    raised = {}

    def worker():
        try:
            otp.request_otp(998, "BetBoom", timeout=0.3)
        except TimeoutError as exc:
            raised["msg"] = str(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join(2)
    assert "msg" in raised
    print("OK: test_otp_relay_timeout")


if __name__ == "__main__":
    test_encrypt_roundtrip()
    test_admin_password_check()
    test_session_token_roundtrip()
    test_account_crud_and_balance()
    test_account_cookies_roundtrip()
    test_parse_cookie_string()
    test_parse_local_storage_string()
    test_autobet_plan_without_accounts_is_zero()
    test_autobet_plan_with_balances_caps_stake()
    test_place_on_arb_logs_bet()
    test_live_toggle_settings()
    test_settings_report_scanner_state()
    test_otp_relay_roundtrip()
    test_otp_relay_timeout()
    print("Все тесты прошли.")
