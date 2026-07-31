"""Учётные записи игроков: вход на сайт по логину и паролю.

Сайт встречает посетителя формой «логин и пароль». Учётку заводит только
администратор (раздел «Игроки» в админке) и передаёт реквизиты человеку:
самостоятельной регистрации нет — сканер не публичный сервис, а рабочий
инструмент на несколько человек.

Что здесь:

- проверка реквизитов (пароль хранится в базе только хэшем PBKDF2,
  см. app/security.py);
- сессия игрока — отдельная подписанная cookie `player_session`, чтобы
  сессия сайта (общий пароль) и сессия админки остались как были;
- деньги игрока: стартовый баланс он задаёт себе сам в профиле, а
  текущий баланс — это стартовый плюс прибыль всех сохранённых ставок
  (см. app/profile_api.py). Отдельного поля «баланс» в базе нет
  СОЗНАТЕЛЬНО: иначе оно рассинхронизировалось бы с журналом при первом
  же удалении ставки.
"""
import logging

from fastapi import Request

from . import config, db
from .security import (create_session_token, verify_password_hash,
                       verify_session_token)

log = logging.getLogger("players")

COOKIE_NAME = "player_session"

# Минимальная длина пароля игрока. Пароль выдаёт администратор, и «1234»
# в качестве общего для всех — самый частый способ открыть сайт наружу.
MIN_PASSWORD_LEN = 6


def check_credentials(username: str, password: str) -> dict | None:
    """Игрок, если логин и пароль подошли, иначе None.

    Выключенный игрок (enabled=0) не пускается: это способ временно
    закрыть доступ, не удаляя журнал ставок."""
    if not username or not password:
        return None
    player = db.get_player_by_username(username, with_hash=True)
    if player is None or not player["enabled"]:
        return None
    if not verify_password_hash(player["password_hash"], password):
        return None
    player.pop("password_hash", None)
    return player


def session_cookie(player: dict) -> str:
    """Токен сессии игрока. Внутри — логин, а не id: логин не переиспользуется
    (UNIQUE), и по нему сразу видно, кто это, в логах."""
    return create_session_token(player["username"],
                                ttl=config.SITE_SESSION_TTL)


def current_player(request: Request) -> dict | None:
    """Игрок этого запроса (по cookie) — или None.

    Учётку перечитываем из базы на каждый запрос: удалили игрока или
    выключили — его cookie перестаёт работать сразу, а не через месяц,
    когда истечёт срок токена."""
    username = verify_session_token(request.cookies.get(COOKIE_NAME))
    if not username:
        return None
    player = db.get_player_by_username(username)
    if player is None or not player["enabled"]:
        return None
    return player


def has_session(request: Request) -> bool:
    return current_player(request) is not None


def display_name(player: dict) -> str:
    return player.get("display_name") or player.get("username") or ""


def balance(player_id: int, start_balance: float) -> dict:
    """Деньги игрока: стартовый баланс, прибыль журнала и их сумма."""
    totals = db.player_totals(player_id)
    return {
        "start_balance": round(start_balance, 2),
        "profit": totals["profit"],
        "staked": totals["staked"],
        "bets_count": totals["bets_count"],
        "balance": round(start_balance + totals["profit"], 2),
    }


def public_profile(player: dict) -> dict:
    """Профиль для фронтенда: кто вошёл и сколько у него денег."""
    return {
        "id": player["id"],
        "username": player["username"],
        "display_name": player.get("display_name") or "",
        "name": display_name(player),
        **balance(player["id"], player["start_balance"]),
    }
