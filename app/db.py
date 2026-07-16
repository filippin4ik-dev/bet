"""SQLite: история найденных вилок."""
import json
import sqlite3
import threading
from datetime import datetime, timezone

from .config import DB_PATH
from .models import Arb

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS arbs_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                found_at      TEXT NOT NULL,
                kind          TEXT NOT NULL DEFAULT 'live',
                start_time    TEXT,
                match_key     TEXT NOT NULL,
                sport         TEXT NOT NULL,
                team1         TEXT NOT NULL,
                team2         TEXT NOT NULL,
                market        TEXT NOT NULL DEFAULT 'Победитель',
                outcome1      TEXT NOT NULL DEFAULT 'П1',
                outcome2      TEXT NOT NULL DEFAULT 'П2',
                k1_max        REAL NOT NULL,
                k1_bookmaker  TEXT NOT NULL,
                k2_max        REAL NOT NULL,
                k2_bookmaker  TEXT NOT NULL,
                profit_pct    REAL NOT NULL,
                stakes_json   TEXT NOT NULL
            )
        """)
        # Миграция старых баз (добавление появившихся позже колонок)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(arbs_history)")}
        migrations = {
            "kind": "ALTER TABLE arbs_history ADD COLUMN kind TEXT NOT NULL DEFAULT 'live'",
            "start_time": "ALTER TABLE arbs_history ADD COLUMN start_time TEXT",
            "market": "ALTER TABLE arbs_history ADD COLUMN market TEXT NOT NULL DEFAULT 'Победитель'",
            "outcome1": "ALTER TABLE arbs_history ADD COLUMN outcome1 TEXT NOT NULL DEFAULT 'П1'",
            "outcome2": "ALTER TABLE arbs_history ADD COLUMN outcome2 TEXT NOT NULL DEFAULT 'П2'",
        }
        for col, ddl in migrations.items():
            if col not in cols:
                conn.execute(ddl)


def save_arbs(arbs: list[Arb]) -> None:
    if not arbs:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [
        (now, a.kind, a.start_time, a.match_key, a.sport, a.team1, a.team2,
         a.market, a.outcome1, a.outcome2,
         a.k1_max, a.k1_bookmaker, a.k2_max, a.k2_bookmaker,
         round(a.profit_pct, 2), json.dumps(a.stakes, ensure_ascii=False))
        for a in arbs
    ]
    with _lock, _connect() as conn:
        conn.executemany(
            """INSERT INTO arbs_history
               (found_at, kind, start_time, match_key, sport, team1, team2,
                market, outcome1, outcome2,
                k1_max, k1_bookmaker, k2_max, k2_bookmaker,
                profit_pct, stakes_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )


def get_history(limit: int = 100) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM arbs_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["stakes"] = json.loads(d.pop("stakes_json"))
        d["match"] = f"{d['team1']} — {d['team2']}"
        result.append(d)
    return result
