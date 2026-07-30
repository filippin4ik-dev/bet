"""Вход на сайт по паролю: страница /login общается только с этими ручками.

Сама проверка доступа живёт в app/access.py, шлюз (middleware) — в
app/main.py. Здесь только три публичные ручки: состояние защиты, вход и
выход. Публичные они по необходимости — иначе форму входа было бы нечем
обслужить, — поэтому наружу не отдают ничего лишнего: ни белый список, ни
даже того, задан ли пароль в базе или в переменной окружения.
"""
import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from . import access, config

log = logging.getLogger("access")

router = APIRouter(prefix="/api/access")


@router.get("/state")
def state(request: Request):
    """Нужен ли пароль этому посетителю (для страницы входа)."""
    allowed, _reason = access.check_request(request)
    return {
        "gate_enabled": access.gate_enabled(),
        "authenticated": allowed,
        "ip_only": access.ip_only() and bool(access.whitelist()),
    }


class LoginBody(BaseModel):
    password: str


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    ip = access.client_ip(request)
    if access.too_many_fails(ip):
        raise HTTPException(
            status_code=429,
            detail="Слишком много неудачных попыток. Подождите "
                   f"{int(config.SITE_LOGIN_FAIL_WINDOW / 60)} мин "
                   "и попробуйте снова.")
    if not access.check_password(body.password):
        access.note_fail(ip)
        log.warning("Неверный пароль доступа к сайту (IP %s)", ip)
        raise HTTPException(status_code=401, detail="Неверный пароль")
    access.note_success(ip)
    response.set_cookie(
        access.COOKIE_NAME, access.session_cookie(),
        max_age=int(config.SITE_SESSION_TTL), httponly=True, samesite="lax")
    log.info("Вход на сайт по паролю (IP %s)", ip)
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(access.COOKIE_NAME)
    return {"ok": True}
