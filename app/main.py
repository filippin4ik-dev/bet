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
from .scanner import Scanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

scanner = Scanner()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    task = asyncio.create_task(scanner.run())
    yield
    scanner.stop()
    task.cancel()


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


@app.get("/api/history")
def get_history(limit: int = Query(100, ge=1, le=1000)):
    """История найденных вилок из SQLite."""
    return {"history": db.get_history(limit)}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
