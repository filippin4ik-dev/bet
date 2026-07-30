"""API админки: логин, аккаунты БК, парсеры, доступ к сайту, авто-ставки.

Все /api/admin/* (кроме /login) требуют валидную сессионную cookie —
см. require_admin().Cookie подписана HMAC (app/security.py), сам пароль
администратора никогда не хранится, а логины/пароли аккаунтов БК
хранятся в базе только в зашифрованном виде.
"""
import logging
import os
import sys
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from . import (access, accounts_manager, bk_control, config, connectors, db,
               otp, visitors)
from .runtime import live_scanner, scanner
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

BOOKMAKERS = ["Winline", "Melbet", "BetBoom", "Fonbet", "Liga Stavok",
              "bc.game"]


@router.get("/bookmakers")
def bookmakers(username: str = Depends(require_admin)):
    """БК, к которым можно привязать аккаунт, и чем в каждую входят.

    `login_types` фронтенд показывает переключателем «по телефону / по
    логину»: предлагать телефон там, где БК его не принимает (bc.game),
    значит обречь оператора на форму, которая молча не отправляется."""
    return {
        "bookmakers": BOOKMAKERS,
        "login_types": {bk: connectors.login_types(bk) for bk in BOOKMAKERS},
        "login_type_names": connectors.LOGIN_TYPE_NAMES,
    }


def _checked_login_type(bookmaker: str, value: str | None) -> str:
    allowed = connectors.login_types(bookmaker)
    if value is None:
        return allowed[0]
    value = value.strip().lower()
    if value not in allowed:
        names = ", ".join(f"«{connectors.LOGIN_TYPE_NAMES[t]}»"
                          for t in allowed)
        raise HTTPException(
            status_code=400,
            detail=f"{bookmaker} так не пускает. Доступные способы входа: "
                   f"{names}.")
    return value


@router.get("/accounts")
def list_accounts(username: str = Depends(require_admin)):
    return {"accounts": db.list_accounts()}


class AccountBody(BaseModel):
    bookmaker: str
    login: str
    password: str
    label: str = ""
    # Чем входим: "phone" или "login". None — способ по умолчанию для
    # этой БК (см. connectors.login_types).
    login_type: str | None = None
    # Опционально: сессионная cookie, скопированная оператором из СВОЕГО
    # браузера (где вход уже пройден вручную — в т.ч. капча/СМС-код). См.
    # README «Вход по cookie». Формат — как заголовок Cookie в DevTools:
    # "name1=v1; name2=v2".
    cookies: str = ""


@router.post("/accounts")
def add_account(body: AccountBody, username: str = Depends(require_admin)):
    if body.bookmaker not in BOOKMAKERS:
        raise HTTPException(status_code=400, detail="Неизвестная БК")
    if not body.login or not body.password:
        raise HTTPException(status_code=400, detail="Логин и пароль обязательны")
    login_type = _checked_login_type(body.bookmaker, body.login_type)
    account_id = db.add_account(body.bookmaker, body.login, body.password,
                                body.label, body.cookies,
                                login_type=login_type)
    accounts_manager.refresh_balance_async(account_id)
    return {"ok": True, "id": account_id}


class AccountUpdateBody(BaseModel):
    enabled: bool | None = None
    label: str | None = None
    login: str | None = None
    password: str | None = None
    login_type: str | None = None
    # "" — явно очистить сохранённую cookie (напр. протухла); None — не
    # трогать текущее значение.
    cookies: str | None = None


@router.put("/accounts/{account_id}")
def update_account(account_id: int, body: AccountUpdateBody,
                   username: str = Depends(require_admin)):
    acc = db.get_account(account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    login_type = None
    if body.login_type is not None:
        login_type = _checked_login_type(acc["bookmaker"], body.login_type)
    db.update_account(account_id, enabled=body.enabled, label=body.label,
                      login=body.login, password=body.password,
                      cookies=body.cookies, login_type=login_type)
    # Способ входа меняет саму форму, по которой коннектор ходит, — баланс
    # после этого надо перечитать так же, как после смены пароля.
    if body.login is not None or body.password is not None \
            or body.cookies is not None or login_type is not None:
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
# Парсеры БК: включение/выключение и перезапуск
# ---------------------------------------------------------------------------

def _bk_state(snap: dict, name: str) -> dict | None:
    """Свежесть и состояние одной БК в одном режиме (из снимка сканера)."""
    return (snap.get("bookmakers") or {}).get(name)


@router.get("/parsers")
def list_parsers(username: str = Depends(require_admin)):
    """Все БК сканера: включена ли, сколько котировок принесла и когда.

    Прематч и лайв показываются отдельно: БК может отдавать линию к
    матчам и молчать в лайве (или наоборот)."""
    pre_snap = scanner.snapshot()
    live_snap = live_scanner.snapshot()
    live_names = set(live_scanner.parser_names())
    parsers = []
    for p in scanner.parsers:
        parsers.append({
            "name": p.name,
            "enabled": bk_control.is_enabled(p.name),
            "supports_live": p.name in live_names,
            "min_refresh": p.min_refresh,
            "prematch": _bk_state(pre_snap, p.name),
            "live": _bk_state(live_snap, p.name),
        })
    return {
        "parsers": parsers,
        "scan_interval": config.SCAN_INTERVAL,
        "prematch_running": scanner.is_running(),
        "live_enabled": bool(config.LIVE_ENABLED),
        "live_running": live_scanner.is_running(),
    }


class ParserBody(BaseModel):
    enabled: bool


def _known_bookmaker(name: str) -> None:
    if name not in scanner.parser_names():
        raise HTTPException(status_code=404,
                            detail=f"БК «{name}» нет в наборе сканера")


@router.post("/parsers/{name}")
def update_parser(name: str, body: ParserBody,
                  username: str = Depends(require_admin)):
    """Включает или выключает БК. Действует сразу и переживает перезапуск.

    Выключенная БК не опрашивается вообще, а её котировки забываются —
    иначе они висели бы в вилках как живые до истечения ODDS_TTL."""
    _known_bookmaker(name)
    bk_control.set_enabled(name, body.enabled)
    # не дожидаясь, пока воркер БК закончит начатый обход: иначе кэфы
    # выключенной БК ещё минуту висят на сайте как живые
    scanner.apply_bk_state(name, body.enabled)
    live_scanner.apply_bk_state(name, body.enabled)
    log.info("БК %s %s из админки (%s)", name,
             "включена" if body.enabled else "выключена", username)
    return {"ok": True, "name": name, "enabled": body.enabled}


@router.post("/parsers/{name}/restart")
def restart_parser(name: str, username: str = Depends(require_admin)):
    """Перезапускает одну БК: новая сессия, котировки с чистого листа."""
    _known_bookmaker(name)
    scanner.request_restart(name)
    live_scanner.request_restart(name)
    log.info("Перезапуск БК %s из админки (%s)", name, username)
    return {"ok": True, "name": name}


class ScannerRestartBody(BaseModel):
    # prematch | live | all
    mode: str = "all"


@router.post("/scanner/restart")
def restart_scanner(body: ScannerRestartBody,
                    username: str = Depends(require_admin)):
    """Перезапускает сканер целиком: все БК получают чистые сессии.

    Помогает, когда линия «поехала» у нескольких БК сразу (например, после
    обрыва сети), и заменяет перезапуск всего сервера."""
    if body.mode not in ("prematch", "live", "all"):
        raise HTTPException(status_code=400,
                            detail="mode: prematch, live или all")
    if body.mode in ("prematch", "all"):
        scanner.request_restart()
    if body.mode in ("live", "all"):
        live_scanner.request_restart()
    log.info("Перезапуск сканера (%s) из админки (%s)", body.mode, username)
    return {"ok": True, "mode": body.mode}


def restart_argv() -> list[str]:
    """Командная строка, которой процесс был запущен.

    Берём sys.orig_argv — она включает и ключи интерпретатора. Склейка
    sys.executable + sys.argv не годится: после `python -m uvicorn` в
    argv[0] лежит путь к uvicorn/__main__.py, и запуск этого файла
    напрямую кладёт его каталог в sys.path — дальше `import logging`
    находит uvicorn/logging.py вместо стандартного модуля, и процесс
    падает на циклическом импорте, не поднявшись."""
    return list(getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv])


@router.post("/restart_app")
def restart_app(username: str = Depends(require_admin)):
    """Перезапускает весь процесс сервера той же командой, что его запустила.

    Нужно, когда меняли переменные окружения или обновили код на VPS:
    процесс поднимается заново сам, без ssh и systemctl. Ответ уходит
    ДО перезапуска, поэтому админка успевает показать сообщение."""
    log.warning("Перезапуск сервера из админки (%s)", username)
    argv = restart_argv()

    def do_restart() -> None:
        time.sleep(0.7)
        try:
            os.execv(sys.executable, argv)
        except Exception:  # noqa: BLE001
            # Процесс остаётся жив: если он под systemd (Restart=always),
            # оператору хотя бы не придётся поднимать сайт руками.
            log.exception("Не удалось перезапустить процесс (execv %s)", argv)

    threading.Thread(target=do_restart, daemon=True).start()
    return {"ok": True, "detail": "Сервер перезапускается — страница "
                                  "переподключится через несколько секунд."}


# ---------------------------------------------------------------------------
# Доступ к сайту: пароль и белый список IP
# ---------------------------------------------------------------------------

@router.get("/access")
def get_access(request: Request, username: str = Depends(require_admin)):
    return access.state(request)


class AccessBody(BaseModel):
    # новый пароль доступа (пусто/None — не менять)
    password: str | None = None
    # снять пароль (сайт снова открыт, если не задан белый список)
    clear_password: bool = False
    # список IP/подсетей: текст, как его ввёл оператор
    whitelist: str | None = None
    # забаненные адреса/подсети (текст, как его ввёл оператор)
    ip_blacklist: str | None = None
    # пускать ТОЛЬКО адреса из списка (пароль не спрашивать)
    ip_only: bool | None = None


@router.post("/access")
def update_access(body: AccessBody, request: Request,
                  username: str = Depends(require_admin)):
    """Меняет защиту сайта.

    Главная проверка здесь — не запереть оператора снаружи: включить
    строгий режим (только IP из списка) можно лишь тогда, когда СВОЙ адрес
    в этом списке уже есть."""
    my_ip = access.client_ip(request)
    entries = access.whitelist() if body.whitelist is None else None
    if body.whitelist is not None:
        try:
            entries = access.parse_whitelist(body.whitelist)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    ip_only = access.ip_only() if body.ip_only is None else body.ip_only
    if ip_only:
        if not entries:
            raise HTTPException(
                status_code=400,
                detail="Строгий режим без списка адресов закрыл бы сайт для "
                       "всех. Добавьте хотя бы свой адрес: " + (my_ip or "—"))
        if not access.ip_in_whitelist(my_ip, entries):
            raise HTTPException(
                status_code=400,
                detail=f"Ваш адрес {my_ip or '—'} не входит в список — со "
                       "строгим режимом вы потеряете доступ к сайту. "
                       "Сначала добавьте его в список.")
    if body.ip_blacklist is not None:
        try:
            banned = access.parse_whitelist(body.ip_blacklist)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        # Свой адрес в бане — это выход из сайта в один клик, причём
        # обратно уже не зайти обычным способом.
        if access.ip_in_whitelist(my_ip, banned):
            raise HTTPException(
                status_code=400,
                detail=f"Ваш адрес {my_ip or '—'} попал в чёрный список — "
                       "вы закроете доступ самому себе.")
        access.set_ip_blacklist(body.ip_blacklist)
    if body.whitelist is not None:
        access.set_whitelist(body.whitelist)
    if body.ip_only is not None:
        access.set_ip_only(body.ip_only)
    if body.clear_password:
        access.clear_password()
    elif body.password:
        if len(body.password) < 4:
            raise HTTPException(status_code=400,
                                detail="Пароль короче 4 символов не защищает")
        access.set_password(body.password)
    log.info("Доступ к сайту изменён из админки (%s): пароль %s, адресов в "
             "списке %d, строгий режим %s", username,
             "задан" if access.password_set() else "не задан",
             len(access.whitelist()), "вкл" if access.ip_only() else "выкл")
    return access.state(request)


# ---------------------------------------------------------------------------
# Кто на сайте: устройства посетителей и бан
# ---------------------------------------------------------------------------

@router.get("/visitors")
def list_visitors(request: Request, username: str = Depends(require_admin)):
    """Устройства, заходившие на сайт: браузер, ОС, адреса, свежесть.

    my_device/my_ip нужны фронтенду, чтобы пометить в списке самого
    оператора — иначе легко забанить собственный телефон."""
    return {
        "visitors": visitors.snapshot(),
        "online_window_sec": config.VISITOR_ONLINE_WINDOW,
        "ip_blacklist": access.ip_blacklist(),
        "my_device": visitors.device_id_of(request),
        "my_ip": access.client_ip(request),
        "tracking": bool(config.VISITORS_ENABLED),
    }


class VisitorBlockBody(BaseModel):
    blocked: bool = True
    # заодно забанить адрес, с которого устройство приходило в последний
    # раз (у телефона он плавает, поэтому это дополнение к бану устройства,
    # а не замена ему)
    with_ip: bool = False


@router.post("/visitors/{device_id}/block")
def block_visitor(device_id: str, body: VisitorBlockBody, request: Request,
                  username: str = Depends(require_admin)):
    """Банит или разбанивает устройство (и, по желанию, его адрес)."""
    my_ip = access.client_ip(request)
    if body.blocked and device_id == visitors.device_id_of(request):
        raise HTTPException(
            status_code=400,
            detail="Это устройство, с которого вы сейчас открыли админку — "
                   "банить его незачем.")
    entry = visitors.set_blocked(device_id, body.blocked)
    if entry is None:
        raise HTTPException(status_code=404, detail="Устройство не найдено")
    banned_ip = None
    if body.with_ip and entry["ip"]:
        if entry["ip"] == my_ip:
            raise HTTPException(
                status_code=400,
                detail=f"Адрес {my_ip} — ваш собственный: забанив его, вы "
                       "закроете доступ себе. Устройство при этом "
                       "заблокировано.")
        current = access.ip_blacklist()
        if not access.ip_in_whitelist(entry["ip"], current):
            access.set_ip_blacklist("\n".join(current + [entry["ip"]]))
        banned_ip = entry["ip"]
    log.info("Устройство %s %s из админки (%s)%s", device_id,
             "забанено" if body.blocked else "разбанено", username,
             f", адрес {banned_ip} в чёрном списке" if banned_ip else "")
    return {"ok": True, "device_id": device_id, "blocked": body.blocked,
            "banned_ip": banned_ip, "ip_blacklist": access.ip_blacklist()}


@router.delete("/visitors/{device_id}")
def forget_visitor(device_id: str, username: str = Depends(require_admin)):
    """Убирает устройство из списка. Бан при этом снимается: устройство
    появится заново при следующем заходе."""
    if not visitors.forget(device_id):
        raise HTTPException(status_code=404, detail="Устройство не найдено")
    return {"ok": True}


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
        # Лайв-сканер: чего хочет оператор (live_enabled) и что происходит
        # прямо сейчас (live_running) — выключение вступает в силу не
        # мгновенно, а когда БК закончат текущие обходы.
        "live_enabled": bool(config.LIVE_ENABLED),
        "live_running": live_scanner.is_running(),
        "scan_interval": config.SCAN_INTERVAL,
    }


class SettingsBody(BaseModel):
    autobet_enabled: bool | None = None
    autobet_dry_run: bool | None = None
    live_enabled: bool | None = None


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
    # Лайв, наоборот, сохраняем: выключили, чтобы не мешал прематчу —
    # значит и после перезапуска он должен остаться выключенным.
    if body.live_enabled is not None:
        config.LIVE_ENABLED = body.live_enabled
        db.set_bool_setting("live_enabled", body.live_enabled)
        log.info("Лайв-сканер %s из админки (%s)",
                 "включён" if body.live_enabled else "выключен", username)
    return {"ok": True}


@router.get("/bet_log")
def bet_log(limit: int = 200, username: str = Depends(require_admin)):
    return {"log": db.get_bet_log(limit)}
