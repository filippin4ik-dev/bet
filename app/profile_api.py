"""Профиль игрока: стартовый баланс и журнал сохранённых ставок.

Зачем это нужно. Ставки игрок делает руками на сайтах БК, и сканер о них
ничего не знает. Чтобы считать прибыль, он нажимает в карточке вилки
«Сохранить ставку»: вилка со всеми плечами и суммами ложится в его личный
журнал, а баланс растёт на гарантированную прибыль этой вилки.

Баланс НЕ хранится отдельным числом: он всегда считается как стартовый
баланс (его игрок задаёт в профиле) плюс прибыль всех сохранённых ставок.
Иначе удаление ошибочной записи оставляло бы баланс завышенным навсегда.

Суммы приходят с фронтенда (человек мог округлить плечи по-своему —
в купоне БК всё равно вводит он), но арифметика перепроверяется здесь:
выигрыш при любом исходе — минимальное из плеч, прибыль — выигрыш минус
сумма всех плеч. Так журнал остаётся согласованным сам с собой.
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import db, players

log = logging.getLogger("profile")

router = APIRouter(prefix="/api/profile")

# Разумные пределы: журнал ведёт человек, а не биржа. Всё, что выходит за
# них, — это опечатка (или чьё-то любопытство насчёт границ поля ввода).
MAX_MONEY = 1e9
MAX_ODDS = 1000.0
MAX_TEXT = 300


def require_player(request: Request) -> dict:
    player = players.current_player(request)
    if player is None:
        raise HTTPException(
            status_code=401,
            detail="Профиль есть только у игроков. Войдите под своей "
                   "учётной записью — её выдаёт администратор.")
    return player


def _clip(text: str | None, limit: int = MAX_TEXT) -> str:
    return " ".join(str(text or "").split())[:limit]


@router.get("/me")
def me(request: Request):
    """Кто вошёл и сколько у него денег.

    Отвечает и тем, кто вошёл не игроком (администратор, общий пароль):
    шапка сайта по этому ответу решает, показывать ли профиль, — и делать
    из-за неё 401 в консоли браузера незачем."""
    player = players.current_player(request)
    return {"player": players.public_profile(player) if player else None}


class ProfileBody(BaseModel):
    display_name: str | None = None
    start_balance: float | None = None
    # Смена собственного пароля: пусто — не менять.
    password: str | None = None


@router.post("")
def update_profile(body: ProfileBody, request: Request):
    """Игрок правит свой профиль: имя, стартовый баланс, пароль."""
    player = require_player(request)
    if body.start_balance is not None:
        if not -MAX_MONEY <= body.start_balance <= MAX_MONEY:
            raise HTTPException(status_code=400,
                                detail="Стартовый баланс вне разумных границ")
    if body.password is not None and body.password != "":
        if len(body.password) < players.MIN_PASSWORD_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"Пароль короче {players.MIN_PASSWORD_LEN} символов "
                       "не защищает")
    db.update_player(
        player["id"],
        display_name=None if body.display_name is None
        else _clip(body.display_name, 60),
        start_balance=body.start_balance,
        password=body.password or None)
    fresh = db.get_player(player["id"])
    log.info("Игрок %s обновил профиль", player["username"])
    return {"ok": True, "player": players.public_profile(fresh)}


@router.get("/bets")
def list_bets(request: Request, limit: int = 200):
    player = require_player(request)
    return {
        "player": players.public_profile(player),
        "bets": db.list_player_bets(player["id"], max(1, min(limit, 1000))),
    }


class LegBody(BaseModel):
    bookmaker: str = ""
    outcome: str = ""
    odds: float = Field(gt=1.0, le=MAX_ODDS)
    stake: float = Field(ge=0, le=MAX_MONEY)


class SaveBetBody(BaseModel):
    match_key: str = ""
    # prematch | live — как в самой вилке (models.KIND_*)
    kind: str = "prematch"
    kind3: bool = False
    sport: str = ""
    match: str = ""
    market: str = ""
    profit_pct: float = 0.0
    legs: list[LegBody]
    note: str = ""


@router.post("/bets")
def save_bet(body: SaveBetBody, request: Request):
    """Сохраняет поставленную вилку в журнал игрока."""
    player = require_player(request)
    if not 2 <= len(body.legs) <= 3:
        raise HTTPException(status_code=400,
                            detail="У вилки должно быть 2 или 3 плеча")
    stake_total = sum(leg.stake for leg in body.legs)
    if stake_total <= 0:
        raise HTTPException(
            status_code=400,
            detail="Сумма ставки не задана — укажите её в поле «Общая сумма»")
    # Выигрыш при любом исходе — это худшее из плеч: сыграет ровно одно.
    payout = min(leg.stake * leg.odds for leg in body.legs)
    bet_id = db.add_player_bet(
        player["id"],
        match_key=_clip(body.match_key),
        kind="live" if body.kind == "live" else "prematch",
        kind3=body.kind3,
        sport=_clip(body.sport), match=_clip(body.match),
        market=_clip(body.market),
        profit_pct=body.profit_pct,
        stake_total=stake_total, payout=payout,
        profit=payout - stake_total,
        legs=[{"bookmaker": _clip(leg.bookmaker, 60),
               "outcome": _clip(leg.outcome, 60),
               "odds": round(leg.odds, 3),
               "stake": round(leg.stake, 2)} for leg in body.legs],
        note=_clip(body.note))
    fresh = db.get_player(player["id"])
    log.info("Игрок %s сохранил ставку на «%s» (%.2f ₽, прибыль %.2f ₽)",
             player["username"], body.match or body.match_key, stake_total,
             payout - stake_total)
    return {
        "ok": True,
        "bet_id": bet_id,
        "profit": round(payout - stake_total, 2),
        "player": players.public_profile(fresh),
    }


@router.delete("/bets/{bet_id}")
def delete_bet(bet_id: int, request: Request):
    """Убирает ошибочную запись — баланс пересчитается сам."""
    player = require_player(request)
    if not db.delete_player_bet(player["id"], bet_id):
        raise HTTPException(status_code=404, detail="Ставка не найдена")
    fresh = db.get_player(player["id"])
    return {"ok": True, "player": players.public_profile(fresh)}
