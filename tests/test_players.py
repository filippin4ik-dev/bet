"""Тесты учётных записей игроков: вход, профиль и журнал ставок.

Проверяем то, ради чего всё затевалось: на сайт пускают по логину и
паролю, заводит их только администратор, а сохранённая с вилки ставка
увеличивает баланс ровно на свою гарантированную прибыль.

Запуск: python3 tests/test_players.py
Использует ВРЕМЕННУЮ базу (не трогает arbs.sqlite3 из рабочего каталога).
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmpdir = tempfile.mkdtemp()
os.environ["DB_PATH"] = str(Path(_tmpdir) / "players-test.sqlite3")
os.environ["SECRET_KEY"] = "test-secret-key-not-for-prod"

from fastapi import HTTPException  # noqa: E402

from app import access, access_api, admin_api, config, db, players  # noqa: E402
from app import profile_api  # noqa: E402

db.init_db()


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    def __init__(self, host="203.0.113.9", cookies=None, headers=None):
        self.client = _FakeClient(host)
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.cookies = cookies or {}
        self.url = type("U", (), {"path": "/", "query": ""})()


class _FakeResponse:
    """Ответ, у которого нас интересуют только выставленные cookie."""

    def __init__(self):
        self.cookies = {}

    def set_cookie(self, name, value, **kwargs):
        self.cookies[name] = value

    def delete_cookie(self, name, **kwargs):
        self.cookies.pop(name, None)


def _fresh() -> None:
    for player in db.list_players():
        db.delete_player(player["id"])
    access.clear_password()
    access.set_whitelist("")
    access.set_ip_only(False)
    access.invalidate_cache()


def _make_player(username="vanya", password="parol123", **kwargs) -> dict:
    admin_api.add_player(
        admin_api.PlayerBody(username=username, password=password, **kwargs),
        username="admin")
    return db.get_player_by_username(username)


def _login(username, password, host="203.0.113.9"):
    """Возвращает (ответ ручки, cookie) или поднимает HTTPException."""
    response = _FakeResponse()
    body = access_api.LoginBody(username=username, password=password)
    result = access_api.login(body, request=_FakeRequest(host), response=response)
    return result, response.cookies


def _as_player(player_cookie: str) -> _FakeRequest:
    return _FakeRequest(cookies={players.COOKIE_NAME: player_cookie})


# ---------------------------------------------------------------------------
# Учётки заводит администратор
# ---------------------------------------------------------------------------

def test_only_admin_creates_accounts_and_password_is_hashed():
    _fresh()
    player = _make_player("Vanya", "parol123", display_name="Ваня",
                          start_balance=50000)
    # логин приводится к нижнему регистру: его диктуют голосом
    assert player["username"] == "vanya"
    assert player["display_name"] == "Ваня"
    assert player["start_balance"] == 50000
    # пароля в базе нет ни в каком виде, только хэш
    stored = db.get_player_by_username("vanya", with_hash=True)
    assert "parol123" not in stored["password_hash"]
    assert "password" not in db.list_players()[0]
    print("OK: test_only_admin_creates_accounts_and_password_is_hashed")


def test_bad_logins_and_passwords_are_rejected():
    _fresh()
    _make_player("vanya", "parol123")
    for username, password, why in (
            ("ва", "parol123", "слишком короткий логин"),
            ("ваня иванов", "parol123", "пробел в логине"),
            ("petya", "123", "слишком короткий пароль"),
            ("VANYA", "parol123", "логин занят (регистр не спасает)")):
        try:
            _make_player(username, password)
        except HTTPException as exc:
            assert exc.status_code == 400, why
        else:
            raise AssertionError(f"должно отвергаться: {why}")
    print("OK: test_bad_logins_and_passwords_are_rejected")


# ---------------------------------------------------------------------------
# Вход
# ---------------------------------------------------------------------------

def test_player_logs_in_and_gets_through_the_gate():
    _fresh()
    _make_player("vanya", "parol123")
    result, cookies = _login("vanya", "parol123")
    assert result["player"]["username"] == "vanya"
    cookie = cookies[players.COOKIE_NAME]

    request = _as_player(cookie)
    assert players.current_player(request)["username"] == "vanya"
    assert access.check_request(request) == (True, "")
    # регистр логина при входе тоже не важен
    assert _login("Vanya", "parol123")[0]["player"]["username"] == "vanya"
    print("OK: test_player_logs_in_and_gets_through_the_gate")


def test_wrong_password_is_refused_without_hints():
    _fresh()
    _make_player("vanya", "parol123")
    for username, password in (("vanya", "parol124"), ("petya", "parol123"),
                               ("vanya", "")):
        try:
            _login(username, password)
        except HTTPException as exc:
            assert exc.status_code == 401
            # ответ одинаковый: подсказывать, что логин существует, значит
            # помогать перебору
            assert exc.detail == "Неверный логин или пароль"
        else:
            raise AssertionError("неверные реквизиты должны отвергаться")
    print("OK: test_wrong_password_is_refused_without_hints")


def test_disabled_player_loses_access_immediately():
    """Выключение — это способ закрыть доступ, не удаляя журнал ставок.
    Действовать оно должно сразу, а не через месяц, когда истечёт cookie."""
    _fresh()
    player = _make_player("vanya", "parol123")
    _, cookies = _login("vanya", "parol123")
    request = _as_player(cookies[players.COOKIE_NAME])
    assert access.check_request(request)[0] is True

    admin_api.update_player(player["id"],
                            admin_api.PlayerUpdateBody(enabled=False),
                            username="admin")
    assert players.current_player(request) is None
    assert access.check_request(request) == (False, "login")
    try:
        _login("vanya", "parol123")
    except HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("выключенного игрока пускать нельзя")

    admin_api.update_player(player["id"],
                            admin_api.PlayerUpdateBody(enabled=True),
                            username="admin")
    assert players.current_player(request)["username"] == "vanya"
    print("OK: test_disabled_player_loses_access_immediately")


def test_admin_and_shared_password_still_open_the_site():
    """Запасные входы: реквизиты администратора и общий пароль. Без них
    первый же запуск (учёток ещё нет) запирал бы оператора снаружи."""
    _fresh()
    result, cookies = _login(config.ADMIN_USERNAME, config.ADMIN_PASSWORD)
    assert result.get("admin") is True
    admin_request = _FakeRequest(cookies={access.COOKIE_NAME:
                                          cookies[access.COOKIE_NAME]})
    assert access.check_request(admin_request)[0] is True
    # профиля у такого входа нет — сохранять ставки некуда
    assert profile_api.me(admin_request)["player"] is None

    access.set_password("obshiy-parol")
    _, cookies = _login("кто-угодно", "obshiy-parol")
    assert access.COOKIE_NAME in cookies
    access.clear_password()
    print("OK: test_admin_and_shared_password_still_open_the_site")


def test_logout_clears_both_cookies():
    _fresh()
    response = _FakeResponse()
    response.cookies[players.COOKIE_NAME] = "x"
    response.cookies[access.COOKIE_NAME] = "y"
    access_api.logout(response)
    assert response.cookies == {}
    print("OK: test_logout_clears_both_cookies")


# ---------------------------------------------------------------------------
# Профиль и журнал ставок
# ---------------------------------------------------------------------------

def test_player_sets_his_own_starting_balance():
    _fresh()
    _make_player("vanya", "parol123", start_balance=1000)
    _, cookies = _login("vanya", "parol123")
    request = _as_player(cookies[players.COOKIE_NAME])

    assert profile_api.me(request)["player"]["balance"] == 1000
    res = profile_api.update_profile(
        profile_api.ProfileBody(start_balance=75000, display_name="Ваня"),
        request)
    assert res["player"]["start_balance"] == 75000
    assert res["player"]["name"] == "Ваня"
    assert profile_api.me(request)["player"]["balance"] == 75000

    # смена собственного пароля работает, короткий — нет
    profile_api.update_profile(profile_api.ProfileBody(password="novyj-parol"),
                               request)
    assert players.check_credentials("vanya", "novyj-parol") is not None
    assert players.check_credentials("vanya", "parol123") is None
    try:
        profile_api.update_profile(profile_api.ProfileBody(password="123"),
                                   request)
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("короткий пароль должен отвергаться")
    print("OK: test_player_sets_his_own_starting_balance")


def _bet_body(**kwargs):
    body = {
        "match_key": "prematch|a-b|x", "kind": "prematch", "kind3": False,
        "sport": "Футбол", "match": "A — B", "market": "Тотал 2.5",
        "profit_pct": 4.0,
        "legs": [
            {"bookmaker": "Winline", "outcome": "ТБ 2.5", "odds": 2.1,
             "stake": 5000},
            {"bookmaker": "BetBoom", "outcome": "ТМ 2.5", "odds": 2.1,
             "stake": 5000},
        ],
    }
    body.update(kwargs)
    return profile_api.SaveBetBody(**body)


def test_saved_bet_raises_the_balance_by_its_profit():
    """Главный сценарий: поставил вилку руками, нажал «Сохранить ставку» —
    запись легла в журнал, баланс вырос на гарантированную прибыль."""
    _fresh()
    _make_player("vanya", "parol123", start_balance=100000)
    _, cookies = _login("vanya", "parol123")
    request = _as_player(cookies[players.COOKIE_NAME])

    res = profile_api.save_bet(_bet_body(), request)
    # 5000 × 2.1 = 10 500 при любом исходе, поставлено 10 000
    assert res["profit"] == 500
    assert res["player"]["balance"] == 100500
    assert res["player"]["profit"] == 500
    assert res["player"]["staked"] == 10000
    assert res["player"]["bets_count"] == 1

    saved = profile_api.list_bets(request)["bets"][0]
    assert saved["match"] == "A — B"
    assert saved["payout"] == 10500 and saved["stake_total"] == 10000
    assert [leg["bookmaker"] for leg in saved["legs"]] == ["Winline", "BetBoom"]

    # вторая ставка складывается с первой
    res = profile_api.save_bet(_bet_body(), request)
    assert res["player"]["balance"] == 101000
    print("OK: test_saved_bet_raises_the_balance_by_its_profit")


def test_profit_is_recounted_on_the_server():
    """Суммы приходят из браузера, а прибыль считаем сами: иначе журнал
    рассинхронизировался бы с плечами при первой же правке округления."""
    _fresh()
    _make_player("vanya", "parol123")
    _, cookies = _login("vanya", "parol123")
    request = _as_player(cookies[players.COOKIE_NAME])

    # неравные плечи: выигрыш определяется ХУДШИМ из них
    res = profile_api.save_bet(_bet_body(legs=[
        {"bookmaker": "Winline", "outcome": "П1", "odds": 3.0, "stake": 4000},
        {"bookmaker": "Fonbet", "outcome": "П2", "odds": 2.0, "stake": 6000},
    ]), request)
    assert res["profit"] == 2000       # min(12000, 12000) − 10000

    res = profile_api.save_bet(_bet_body(legs=[
        {"bookmaker": "Winline", "outcome": "П1", "odds": 3.0, "stake": 3000},
        {"bookmaker": "Fonbet", "outcome": "П2", "odds": 2.0, "stake": 7000},
    ]), request)
    assert res["profit"] == -1000      # min(9000, 14000) − 10000: перекос
    print("OK: test_profit_is_recounted_on_the_server")


def test_broken_bets_are_refused():
    _fresh()
    _make_player("vanya", "parol123")
    _, cookies = _login("vanya", "parol123")
    request = _as_player(cookies[players.COOKIE_NAME])

    one_leg = _bet_body()
    one_leg.legs = one_leg.legs[:1]
    for body, why in (
            (one_leg, "одно плечо — это не вилка"),
            (_bet_body(legs=[
                {"bookmaker": "Winline", "outcome": "П1", "odds": 2.0,
                 "stake": 0},
                {"bookmaker": "Fonbet", "outcome": "П2", "odds": 2.0,
                 "stake": 0}]), "сумма не задана")):
        try:
            profile_api.save_bet(body, request)
        except HTTPException as exc:
            assert exc.status_code == 400, why
        else:
            raise AssertionError(f"должно отвергаться: {why}")
    assert profile_api.list_bets(request)["bets"] == []
    print("OK: test_broken_bets_are_refused")


def test_deleted_bet_takes_its_profit_back():
    _fresh()
    _make_player("vanya", "parol123", start_balance=10000)
    _, cookies = _login("vanya", "parol123")
    request = _as_player(cookies[players.COOKIE_NAME])
    bet_id = profile_api.save_bet(_bet_body(), request)["bet_id"]
    assert profile_api.me(request)["player"]["balance"] == 10500

    res = profile_api.delete_bet(bet_id, request)
    assert res["player"]["balance"] == 10000
    assert res["player"]["bets_count"] == 0
    try:
        profile_api.delete_bet(bet_id, request)
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("удалять несуществующую запись нечего")
    print("OK: test_deleted_bet_takes_its_profit_back")


def test_bets_of_other_players_are_out_of_reach():
    """Журнал у каждого свой: id ставки приходит из браузера, и чужую
    запись по нему достать (или удалить) быть не должно."""
    _fresh()
    _make_player("vanya", "parol123")
    _make_player("petya", "parol456")
    _, vanya_cookies = _login("vanya", "parol123")
    _, petya_cookies = _login("petya", "parol456")
    vanya = _as_player(vanya_cookies[players.COOKIE_NAME])
    petya = _as_player(petya_cookies[players.COOKIE_NAME])

    bet_id = profile_api.save_bet(_bet_body(), vanya)["bet_id"]
    assert profile_api.list_bets(petya)["bets"] == []
    assert profile_api.me(petya)["player"]["balance"] == 0
    try:
        profile_api.delete_bet(bet_id, petya)
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("чужую ставку удалять нельзя")
    assert len(profile_api.list_bets(vanya)["bets"]) == 1
    print("OK: test_bets_of_other_players_are_out_of_reach")


def test_profile_needs_a_player_session():
    """Вошедшему администратором (или по общему паролю) профиль не
    полагается: денег и журнала у такого входа нет."""
    _fresh()
    anonymous = _FakeRequest()
    assert profile_api.me(anonymous)["player"] is None
    for call in (lambda: profile_api.list_bets(anonymous),
                 lambda: profile_api.save_bet(_bet_body(), anonymous),
                 lambda: profile_api.update_profile(
                     profile_api.ProfileBody(start_balance=1), anonymous)):
        try:
            call()
        except HTTPException as exc:
            assert exc.status_code == 401
        else:
            raise AssertionError("без учётки профиля нет")
    print("OK: test_profile_needs_a_player_session")


# ---------------------------------------------------------------------------
# Админка
# ---------------------------------------------------------------------------

def test_admin_sees_money_and_resets_passwords():
    _fresh()
    player = _make_player("vanya", "parol123", start_balance=20000)
    _, cookies = _login("vanya", "parol123")
    request = _as_player(cookies[players.COOKIE_NAME])
    profile_api.save_bet(_bet_body(), request)

    row = next(p for p in admin_api.list_players(username="admin")["players"]
               if p["username"] == "vanya")
    assert row["start_balance"] == 20000
    assert row["profit"] == 500 and row["balance"] == 20500
    assert row["bets_count"] == 1
    assert "password_hash" not in row

    admin_api.update_player(player["id"],
                            admin_api.PlayerUpdateBody(password="drugoj-parol"),
                            username="admin")
    assert players.check_credentials("vanya", "drugoj-parol") is not None
    assert players.check_credentials("vanya", "parol123") is None

    # журнал игрока виден администратору — разобрать спорную запись
    bets = admin_api.player_bets(player["id"], username="admin")["bets"]
    assert len(bets) == 1 and bets[0]["profit"] == 500

    admin_api.delete_player(player["id"], username="admin")
    assert db.get_player_by_username("vanya") is None
    # журнал уходит вместе с игроком
    assert db.list_player_bets(player["id"]) == []
    print("OK: test_admin_sees_money_and_resets_passwords")


def test_access_state_counts_players():
    _fresh()
    assert access.state()["players_count"] == 0
    _make_player("vanya", "parol123")
    assert access.state()["players_count"] == 1
    print("OK: test_access_state_counts_players")


def test_autobet_is_hidden_from_everyone_by_default():
    """Авто-ставка спрятана, а не выключена: интерфейс её не показывает
    (флаг уходит и на сайт, и в админку), но ручка и настройки на месте."""
    assert config.AUTOBET_UI is False
    assert admin_api.get_settings(username="admin")["autobet_ui"] is False
    print("OK: test_autobet_is_hidden_from_everyone_by_default")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("Все тесты прошли.")
