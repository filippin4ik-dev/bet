"""FastAPI-приложение: API вилок + раздача фронтенда.

Запуск:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import access, accounts_manager, autobet, config, currency, db, security
from .access_api import router as access_router
from .admin_api import require_admin
from .admin_api import router as admin_router
from .config import LIVE_ENABLED, SOUND_ALERT_PROFIT
from .profile_api import router as profile_router
from .runtime import balance_loop, live_scanner, scanner
from .scanner import Scanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # Сохранённый в админке переключатель лайва важнее переменной окружения:
    # оператор выключил лайв — после перезапуска он остаётся выключенным.
    config.LIVE_ENABLED = db.get_bool_setting("live_enabled", LIVE_ENABLED)
    log = logging.getLogger("main")
    log.info("Лайв-сканер %s (переключается в админке)",
             "включён" if config.LIVE_ENABLED else
             "выключен — все ресурсы прематчу")
    accounts = db.count_players()
    log.info("Вход на сайт по логину и паролю: заведено учёток игроков %d, "
             "общий пароль %s%s", accounts,
             "задан" if access.password_set() else "не задан",
             ", строгий режим (только свои IP)" if access.ip_only() else "")
    log.info("Домен: %s; cookie только по HTTPS: %s; валюта: %s (курс "
             "доллара %s)",
             config.SITE_DOMAIN or "любой (доступ по IP)",
             config.COOKIE_SECURE, currency.currency(),
             currency.usd_rub() or "не задан")
    if not accounts:
        log.warning(
            "Учётных записей игроков нет — войти можно только реквизитами "
            "администратора (ADMIN_USERNAME/ADMIN_PASSWORD). Заведите "
            "игроков в админке, раздел «Игроки».")
    # Лайв-сканер запускается всегда: он сам простаивает, пока выключен, и
    # поднимает воркеры, как только его включили из админки.
    tasks = [asyncio.create_task(scanner.run()),
             asyncio.create_task(live_scanner.run()),
             asyncio.create_task(balance_loop.run())]
    yield
    scanner.stop()
    live_scanner.stop()
    balance_loop.stop()
    for t in tasks:
        t.cancel()


app = FastAPI(title="Сканер вилок (двухисходные рынки)", lifespan=lifespan)
app.include_router(admin_router)
app.include_router(access_router)
app.include_router(profile_router)

# Что доступно БЕЗ пароля: сама страница входа, её стили/скрипт и ручки
# входа. Остальная статика (app.js, admin.js) закрыта вместе с сайтом.
GATE_FREE_PATHS = {"/login", "/favicon.ico", "/static/style.css",
                   "/static/login.js", "/static/theme.js"}
GATE_FREE_PREFIXES = ("/api/access/",)


def _gate_free(path: str) -> bool:
    return path in GATE_FREE_PATHS or path.startswith(GATE_FREE_PREFIXES)


# Методы, которые ЧТО-ТО МЕНЯЮТ: их принимаем только со своей же страницы
# (см. security.same_origin) — чужой сайт не должен уметь нажать кнопку в
# админке вашей cookie.
_UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


@app.middleware("http")
async def guard(request: Request, call_next):
    """Единый шлюз сайта: защита соединения и проверка доступа.

    Порядок проверок — от «этот запрос вообще не наш» к «этому посетителю
    сюда нельзя»:

    1. заголовок Host: задан домен (SITE_DOMAIN) — чужое имя не
       обслуживаем вовсе. Иначе сайт отвечал бы по любому домену, который
       кто-то направил на ваш IP, и по нему собирали бы пароли;
    2. HTTPS: при FORCE_HTTPS открытый запрос уводим на защищённый адрес,
       чтобы пароль и cookie не ушли по каналу, где их читают;
    3. свой ли источник у изменяющего запроса (защита от CSRF);
    4. доступ: чёрный список адресов, строгий режим, вход (app/access.py).

    Ответ в любом случае получает заголовки безопасности, а ответ API —
    ещё и запрет кэширования: вилки и балансы не должны оставаться в кэше
    браузера или прокси.
    """
    path = request.url.path
    api = path.startswith("/api/")
    response = await _guarded_response(request, call_next, api=api)
    for name, value in security.security_headers(request, api=api).items():
        response.headers[name] = value
    return response


async def _guarded_response(request: Request, call_next, *, api: bool):
    path = request.url.path
    if config.TRUSTED_HOSTS:
        host = (request.headers.get("host") or "").lower()
        if host.split(":")[0] not in {h.split(":")[0]
                                      for h in config.TRUSTED_HOSTS}:
            return JSONResponse({"detail": "Неизвестный домен."},
                                status_code=421)
    if config.FORCE_HTTPS and not security.is_https(request) \
            and request.method in ("GET", "HEAD"):
        return RedirectResponse(
            str(request.url.replace(scheme="https")), status_code=308)
    if request.method in _UNSAFE_METHODS and not security.same_origin(request):
        return JSONResponse(
            {"detail": "Запрос пришёл с другого сайта и отклонён."},
            status_code=403)
    ip = access.client_ip(request)
    if _gate_free(path):
        return await call_next(request)
    allowed, reason = access.check_request(request)
    if allowed:
        return await call_next(request)
    if reason == "banned":
        if api:
            return JSONResponse({"detail": "Доступ с этого адреса закрыт."},
                                status_code=403)
        return FileResponse(STATIC_DIR / "blocked.html", status_code=403,
                            headers={"Cache-Control": "no-cache"})
    if reason == "ip":
        detail = (f"Доступ только с разрешённых адресов. Ваш адрес: "
                  f"{ip or 'неизвестен'}.")
        if api:
            return JSONResponse({"detail": detail}, status_code=403)
        return FileResponse(STATIC_DIR / "denied.html", status_code=403,
                            headers={"Cache-Control": "no-cache"})
    if api:
        return JSONResponse(
            {"detail": "Требуется вход: сайт открыт по логину и паролю."},
            status_code=401)
    nxt = path
    if request.url.query:
        nxt += "?" + request.url.query
    return RedirectResponse(f"/login?next={quote(nxt, safe='')}")


def _with_limits(arbs: list[dict], bk_field1: str, bk_field2: str,
                 bk_field3: str | None = None) -> list[dict]:
    """Добавляет к каждой вилке max_stake — минимальный из известных
    лимитов ставки по всем её ногам (None, если хоть один баланс
    неизвестен). Данные берутся из подключённых в админке аккаунтов."""
    balances = accounts_manager.available_balance_by_bookmaker()
    if not balances:
        for a in arbs:
            a["max_stake"] = None
        return arbs
    for a in arbs:
        bks = [a[bk_field1], a[bk_field2]]
        if bk_field3:
            bks.append(a[bk_field3])
        caps = [accounts_manager.max_stake_for_bookmaker(bk, balances)
                for bk in bks]
        a["max_stake"] = None if any(c is None for c in caps) else min(caps)
    return arbs


@app.get("/api/arbs")
def get_arbs(
    min_profit: float = Query(0.0, ge=0, description="Мин. доходность, %"),
):
    """Текущие вилки по прематчу (обновляются фоновым сканером).

    Включает и двухисходные вилки (arbs), и трёхисходные по рынку
    «Исход 1X2» (arbs_1x2, П1/X/П2) — самый частый рынок футбола/хоккея.
    """
    snap = scanner.snapshot()
    snap["arbs"] = [a for a in snap["arbs"] if a["profit_pct"] >= min_profit]
    snap["arbs_1x2"] = [a for a in snap["arbs_1x2"]
                        if a["profit_pct"] >= min_profit]
    snap["arbs"] = _with_limits(snap["arbs"], "k1_bookmaker", "k2_bookmaker")
    snap["arbs_1x2"] = _with_limits(
        snap["arbs_1x2"], "k1_bookmaker", "k2_bookmaker", "kx_bookmaker")
    snap["sound_alert_profit"] = SOUND_ALERT_PROFIT
    snap["autobet_ui"] = config.AUTOBET_UI
    # Валюта едет вместе с вилками: сменил её оператор в админке — открытые
    # вкладки пересчитают суммы на следующем же опросе, без перезагрузки.
    snap["currency"] = currency.state()
    return snap


@app.get("/api/currency")
def get_currency():
    """Валюта отображения и курс доллара (задаются в админке)."""
    return currency.state()


@app.get("/api/rejected")
def get_rejected(
    min_profit: float = Query(0.0, ge=0, description="Мин. доходность, %"),
):
    """Кандидаты, которые движок ОТСЕЯЛ, и причина отсева у каждого.

    Одним списком по обоим сканерам: прематч и лайв различаются полем
    kind, а вкладка «Отсеянные» на сайте одна — разбирать такие строки
    руками приходится в любом режиме. Порядок — по убыванию доходности:
    чем «жирнее» отсеянный кандидат, тем интереснее понять, ошибка это
    сопоставления или честная разница в правилах расчёта у БК.
    """
    snap = scanner.rejected_snapshot()
    live = live_scanner.rejected_snapshot()
    rejected = [a for a in snap["rejected"] + live["rejected"]
                if a["profit_pct"] >= min_profit]
    rejected3 = [a for a in snap["rejected_1x2"] + live["rejected_1x2"]
                 if a["profit_pct"] >= min_profit]
    for rows in (rejected, rejected3):
        rows.sort(key=lambda a: a["profit_pct"], reverse=True)
    status = scanner.snapshot()
    return {
        "scanning": status["scanning"],
        "last_scan": status["last_scan"],
        "bookmakers": status["bookmakers"],
        "rejected": rejected,
        "rejected_1x2": rejected3,
    }


@app.get("/api/odds")
def get_odds():
    """Все найденные прематч-матчи/котировки (по всем БК)."""
    snap = scanner.snapshot()
    odds = scanner.odds_snapshot()
    odds.sort(key=lambda o: (o["start_ts"] or float("inf"),
                             o["sport"], o["match"], o["bookmaker"]))
    return {
        "scanning": snap["scanning"],
        "last_scan": snap["last_scan"],
        "bookmakers": snap["bookmakers"],
        "odds": odds,
    }


def _matches_payload(sc: Scanner) -> dict:
    snap = sc.snapshot()
    return {
        "scanning": snap["scanning"],
        "last_scan": snap["last_scan"],
        "bookmakers": snap["bookmakers"],
        # уже по времени начала — сканер отдаёт список отсортированным
        "matches": sc.matches_snapshot(),
    }


@app.get("/api/matches")
def get_matches():
    """Все найденные прематч-матчи, сгруппированные по событию (все БК)."""
    return _matches_payload(scanner)


@app.get("/api/live/matches")
def get_live_matches():
    """Все найденные ЛАЙВ-матчи, сгруппированные по событию (все БК)."""
    return _matches_payload(live_scanner)


@app.get("/api/match")
def get_match(id: str = Query(..., description="id события из /api/matches"),
              live: int = Query(0, description="1 — искать в лайв-сканере")):
    """Полная роспись одного события: все рынки всех БК бок о бок."""
    sc = live_scanner if live else scanner
    detail = sc.match_detail(id)
    if detail is None:
        # событие могло переехать между сканерами (матч начался)
        other = scanner if live else live_scanner
        detail = other.match_detail(id)
    return {"match": detail}


@app.get("/api/live/arbs")
def get_live_arbs(
    min_profit: float = Query(0.0, ge=0, description="Мин. доходность, %"),
):
    """Текущие ЛАЙВ-вилки (быстрый цикл, матчи в игре)."""
    snap = live_scanner.snapshot()
    snap["arbs"] = [a for a in snap["arbs"] if a["profit_pct"] >= min_profit]
    snap["arbs_1x2"] = [a for a in snap["arbs_1x2"]
                        if a["profit_pct"] >= min_profit]
    snap["arbs"] = _with_limits(snap["arbs"], "k1_bookmaker", "k2_bookmaker")
    snap["arbs_1x2"] = _with_limits(
        snap["arbs_1x2"], "k1_bookmaker", "k2_bookmaker", "kx_bookmaker")
    snap["sound_alert_profit"] = SOUND_ALERT_PROFIT
    snap["autobet_enabled"] = config.AUTOBET_ENABLED
    snap["autobet_dry_run"] = config.AUTOBET_DRY_RUN
    snap["autobet_ui"] = config.AUTOBET_UI
    snap["currency"] = currency.state()
    return snap


@app.get("/api/live/odds")
def get_live_odds():
    """Все найденные ЛАЙВ-матчи/котировки (по всем БК)."""
    snap = live_scanner.snapshot()
    odds = live_scanner.odds_snapshot()
    odds.sort(key=lambda o: (o["sport"], o["match"], o["bookmaker"]))
    return {
        "scanning": snap["scanning"],
        "last_scan": snap["last_scan"],
        "bookmakers": snap["bookmakers"],
        "odds": odds,
    }


@app.get("/api/history")
def get_history(limit: int = Query(100, ge=1, le=1000)):
    """История найденных вилок из SQLite."""
    return {"history": db.get_history(limit)}


@app.get("/api/history_1x2")
def get_history_1x2(limit: int = Query(100, ge=1, le=1000)):
    """История найденных трёхисходных вилок («Исход 1X2») из SQLite."""
    return {"history": db.get_history_1x2(limit)}


@app.post("/api/autobet/place")
def autobet_place(
    match_key: str = Body(..., embed=True),
    kind3: bool = Body(False, embed=True),
    live: bool = Body(True, embed=True),
    username: str = Depends(require_admin),
):
    """Ставит (или имитирует — см. AUTOBET_DRY_RUN) вилку по её match_key.

    Требует вход в админку (мутирует реальные деньги при выключенном
    dry-run). Кнопка авто-ставки отправляет сюда именно match_key активной
    вилки — сумма ставки считается на СЕРВЕРЕ по актуальным кэфам и
    балансам, а не приходит с клиента. Саму кнопку по умолчанию не видно
    (AUTOBET_UI=0), но ручка остаётся рабочей: функция спрятана, а не
    выключена."""
    if not config.AUTOBET_ENABLED:
        raise HTTPException(
            status_code=403,
            detail="Авто-ставки выключены (AUTOBET_ENABLED=0). Включите в "
                   "настройках админки, предварительно проверив расчёт "
                   "лимитов в режиме имитации.")
    sc = live_scanner if live else scanner
    snap = sc.snapshot()
    key = "arbs_1x2" if kind3 else "arbs"
    arb = next((a for a in snap[key] if a["match_key"] == match_key), None)
    if arb is None:
        raise HTTPException(
            status_code=404,
            detail="Вилка уже не актуальна (кэфы изменились/матч исчез) — "
                   "обновите список и попробуйте снова.")
    return autobet.place_on_arb(arb, kind3=kind3)


class _NoCacheStatic(StaticFiles):
    """Статика с Cache-Control: no-cache: браузер всегда перепроверяет
    файл на сервере (304, если не менялся) — после обновления на VPS
    новые app.js/style.css подхватываются обычным F5, без очистки кэша."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/admin")
def admin_page():
    return FileResponse(STATIC_DIR / "admin.html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/profile")
def profile_page():
    """Профиль игрока: баланс, сохранённые ставки, прибыль."""
    return FileResponse(STATIC_DIR / "profile.html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/login")
def login_page(request: Request):
    """Страница входа по логину и паролю. Уже впущенных — сразу на сайт."""
    allowed, reason = access.check_request(request)
    if allowed:
        return RedirectResponse("/")
    if reason == "ip":
        return FileResponse(STATIC_DIR / "denied.html", status_code=403,
                            headers={"Cache-Control": "no-cache"})
    return FileResponse(STATIC_DIR / "login.html",
                        headers={"Cache-Control": "no-cache"})


app.mount("/static", _NoCacheStatic(directory=STATIC_DIR), name="static")
