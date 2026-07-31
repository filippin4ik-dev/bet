"""Вход на сайт: страница /login общается только с этими ручками.

Сама проверка доступа живёт в app/access.py, учётки игроков — в
app/players.py, шлюз (middleware) — в app/main.py. Здесь только три
публичные ручки: состояние защиты, вход и выход. Публичные они по
необходимости — иначе форму входа было бы нечем обслужить, — поэтому
наружу не отдают ничего лишнего: ни белый список, ни даже того, есть ли
на сайте такой логин (ошибка входа одна на все случаи).
"""
import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from . import access, config, db, players
from .security import verify_admin_password

log = logging.getLogger("access")

router = APIRouter(prefix="/api/access")


@router.get("/state")
def state(request: Request):
    """Нужен ли вход этому посетителю (для страницы входа)."""
    allowed, _reason = access.check_request(request)
    return {
        "gate_enabled": access.gate_enabled(),
        "authenticated": allowed,
        "ip_only": access.ip_only() and bool(access.whitelist()),
    }


class LoginBody(BaseModel):
    # Логин игрока или администратора. Пустым он остаётся только при
    # входе по общему паролю (запасной вход, см. ниже).
    username: str = ""
    password: str


def _site_cookie(response: Response, username: str) -> None:
    response.set_cookie(
        access.COOKIE_NAME, access.session_cookie(username),
        max_age=int(config.SITE_SESSION_TTL), httponly=True, samesite="lax")


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    """Вход по логину и паролю.

    Порядок проверок: сначала учётки игроков (основной путь), затем
    реквизиты администратора, затем общий пароль доступа — запасной вход,
    оставшийся с тех пор, когда сайт закрывался одним паролем на всех.
    Ответ во всех неудачах одинаковый: подсказывать, что логин существует,
    а не подошёл пароль, — значит помогать перебору."""
    ip = access.client_ip(request)
    if access.too_many_fails(ip):
        raise HTTPException(
            status_code=429,
            detail="Слишком много неудачных попыток. Подождите "
                   f"{int(config.SITE_LOGIN_FAIL_WINDOW / 60)} мин "
                   "и попробуйте снова.")

    player = players.check_credentials(body.username, body.password)
    if player is not None:
        access.note_success(ip)
        db.touch_player_login(player["id"])
        response.set_cookie(
            players.COOKIE_NAME, players.session_cookie(player),
            max_age=int(config.SITE_SESSION_TTL), httponly=True,
            samesite="lax")
        log.info("Вход игрока %s (IP %s)", player["username"], ip)
        return {"ok": True, "player": players.public_profile(player)}

    if verify_admin_password(body.username, body.password):
        access.note_success(ip)
        _site_cookie(response, config.ADMIN_USERNAME)
        log.info("Вход администратора на сайт (IP %s)", ip)
        return {"ok": True, "admin": True}

    if access.check_password(body.password):
        access.note_success(ip)
        _site_cookie(response, "site")
        log.info("Вход по общему паролю доступа (IP %s)", ip)
        return {"ok": True}

    access.note_fail(ip)
    log.warning("Неверный вход на сайт: логин «%s» (IP %s)",
                body.username or "—", ip)
    raise HTTPException(status_code=401, detail="Неверный логин или пароль")


@router.post("/logout")
def logout(response: Response):
    # Гасим обе cookie сразу: в браузере может лежать и сессия игрока, и
    # старая сессия сайта по общему паролю — иначе «выйти» не выводит.
    response.delete_cookie(players.COOKIE_NAME)
    response.delete_cookie(access.COOKIE_NAME)
    return {"ok": True}
