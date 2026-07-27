"""API админки: логин, аккаунты БК, настройки авто-ставок, журнал ставок.

Все /api/admin/* (кроме /login) требуют валидную сессионную cookie —
см. require_admin().Cookie подписана HMAC (app/security.py), сам пароль
администратора никогда не хранится, а логины/пароли аккаунтов БК
хранятся в базе только в зашифрованном виде.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from . import accounts_manager, config, db, otp
from .security import create_session_token, verify_admin_password, \
    verify_session_token

log = logging.getLogger("admin_api")

router = APIRouter(prefix="/api/admin")

COOKIE_NAME = "admin_session"


def require_admin(request: Request) -> str:
    token = request.cookies.get(COOKIE_NAME)
    username = verify_session_token(token)
    if username is None:
        raise HTTPException(status_code=401, detail="Требуется вход в админку")
    return username


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginBody, response: Response):
    if not verify_admin_password(body.username, body.password):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    token = create_session_token(body.username)
    response.set_cookie(
        COOKIE_NAME, token, max_age=int(config.ADMIN_SESSION_TTL),
        httponly=True, samesite="lax")
    return {"ok": True, "username": body.username}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
def me(username: str = Depends(require_admin)):
    return {"username": username}


# ---------------------------------------------------------------------------
# Аккаунты БК
# ---------------------------------------------------------------------------

BOOKMAKERS = ["Winline", "BetBoom", "Fonbet", "Liga Stavok", "bc.game"]


@router.get("/bookmakers")
def bookmakers(username: str = Depends(require_admin)):
    return {"bookmakers": BOOKMAKERS}


@router.get("/accounts")
def list_accounts(username: str = Depends(require_admin)):
    return {"accounts": db.list_accounts()}


class AccountBody(BaseModel):
    bookmaker: str
    login: str
    password: str
    label: str = ""


@router.post("/accounts")
def add_account(body: AccountBody, username: str = Depends(require_admin)):
    if body.bookmaker not in BOOKMAKERS:
        raise HTTPException(status_code=400, detail="Неизвестная БК")
    if not body.login or not body.password:
        raise HTTPException(status_code=400, detail="Логин и пароль обязательны")
    account_id = db.add_account(body.bookmaker, body.login, body.password,
                                body.label)
    accounts_manager.refresh_balance_async(account_id)
    return {"ok": True, "id": account_id}


class AccountUpdateBody(BaseModel):
    enabled: bool | None = None
    label: str | None = None
    login: str | None = None
    password: str | None = None


@router.put("/accounts/{account_id}")
def update_account(account_id: int, body: AccountUpdateBody,
                   username: str = Depends(require_admin)):
    if db.get_account(account_id) is None:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    db.update_account(account_id, enabled=body.enabled, label=body.label,
                      login=body.login, password=body.password)
    if body.login is not None or body.password is not None:
        accounts_manager.refresh_balance_async(account_id)
    return {"ok": True}


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, username: str = Depends(require_admin)):
    db.delete_account(account_id)
    return {"ok": True}


@router.post("/accounts/{account_id}/refresh_balance")
def refresh_balance(account_id: int, username: str = Depends(require_admin)):
    if db.get_account(account_id) is None:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    accounts_manager.refresh_balance(account_id)
    acc = db.get_account(account_id)
    return {"ok": acc.get("last_error") is None, "account": acc}


@router.get("/otp_pending")
def otp_pending(username: str = Depends(require_admin)):
    """Аккаунты, у которых сейчас идёт вход и БК запросила код
    подтверждения (СМС/пуш) — фронтенд опрашивает это и показывает
    диалог для ввода кода."""
    return {"pending": otp.pending_accounts()}


class OtpCodeBody(BaseModel):
    code: str


@router.post("/accounts/{account_id}/otp")
def submit_otp_code(account_id: int, body: OtpCodeBody,
                    username: str = Depends(require_admin)):
    if not otp.submit_otp(account_id, body.code):
        raise HTTPException(
            status_code=404,
            detail="Нет ожидающего запроса кода для этого аккаунта "
                   "(возможно, уже истёк таймаут)")
    return {"ok": True}


@router.get("/limits")
def limits(username: str = Depends(require_admin)):
    balances = accounts_manager.available_balance_by_bookmaker()
    return {
        "balances": balances,
        "max_stakes": {
            bk: accounts_manager.max_stake_for_bookmaker(bk, balances)
            for bk in balances
        },
    }


# ---------------------------------------------------------------------------
# Настройки авто-ставок
# ---------------------------------------------------------------------------

@router.get("/settings")
def get_settings(username: str = Depends(require_admin)):
    return {
        "autobet_enabled": config.AUTOBET_ENABLED,
        "autobet_dry_run": config.AUTOBET_DRY_RUN,
        "autobet_max_stake": config.AUTOBET_MAX_STAKE,
        "autobet_max_balance_fraction": config.AUTOBET_MAX_BALANCE_FRACTION,
        "balance_refresh_interval": config.BALANCE_REFRESH_INTERVAL,
    }


class SettingsBody(BaseModel):
    autobet_enabled: bool | None = None
    autobet_dry_run: bool | None = None


@router.post("/settings")
def update_settings(body: SettingsBody, username: str = Depends(require_admin)):
    # Настройки авто-ставки — сознательно НЕ персистентные в базе, а только
    # runtime-флаги процесса: это доп. защита от случайного включения
    # реальных ставок после перезапуска сервера (нужно явно включать
    # каждый раз, либо задавать переменные окружения на проде).
    if body.autobet_enabled is not None:
        config.AUTOBET_ENABLED = body.autobet_enabled
    if body.autobet_dry_run is not None:
        config.AUTOBET_DRY_RUN = body.autobet_dry_run
    return {"ok": True}


@router.get("/bet_log")
def bet_log(limit: int = 200, username: str = Depends(require_admin)):
    return {"log": db.get_bet_log(limit)}
