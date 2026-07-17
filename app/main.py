"""FastAPI-приложение: API вилок + раздача фронтенда.

Запуск:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import SOUND_ALERT_PROFIT
from .models import KIND_LIVE, KIND_PREMATCH
from .scanner import Scanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Два независимых сканера: прематч (медленный) и лайв (быстрый).
scanner = Scanner(mode=KIND_PREMATCH)
live_scanner = Scanner(mode=KIND_LIVE)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    tasks = [asyncio.create_task(scanner.run()),
             asyncio.create_task(live_scanner.run())]
    yield
    scanner.stop()
    live_scanner.stop()
    for t in tasks:
        t.cancel()


app = FastAPI(title="Сканер вилок (двухисходные рынки)", lifespan=lifespan)


@app.get("/api/arbs")
def get_arbs(
    min_profit: float = Query(0.0, ge=0, description="Мин. доходность, %"),
):
    """Текущие вилки по прематчу (обновляются фоновым сканером)."""
    snap = scanner.snapshot()
    snap["arbs"] = [a for a in snap["arbs"] if a["profit_pct"] >= min_profit]
    snap["sound_alert_profit"] = SOUND_ALERT_PROFIT
    return snap


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
    matches = sc.matches_snapshot()
    matches.sort(key=lambda m: (m["start_ts"] or float("inf"),
                                m["sport"], m["match"]))
    return {
        "scanning": snap["scanning"],
        "last_scan": snap["last_scan"],
        "bookmakers": snap["bookmakers"],
        "matches": matches,
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
    snap["sound_alert_profit"] = SOUND_ALERT_PROFIT
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


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
