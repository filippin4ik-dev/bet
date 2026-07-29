"""SQLite: история найденных вилок, аккаунты БК и журнал авто-ставок."""
import json
import sqlite3
import threading
from datetime import datetime, timezone

from .config import DB_PATH
from .models import Arb, Arb3
from .security import decrypt_str, encrypt_str

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

        # Трёхисходные вилки (рынок «Исход 1X2»: П1/X/П2) — своя таблица,
        # т.к. у неё три плеча вместо двух.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS arbs_history_1x2 (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                found_at      TEXT NOT NULL,
                kind          TEXT NOT NULL DEFAULT 'prematch',
                start_time    TEXT,
                match_key     TEXT NOT NULL,
                sport         TEXT NOT NULL,
                team1         TEXT NOT NULL,
                team2         TEXT NOT NULL,
                market        TEXT NOT NULL DEFAULT 'Исход (1X2)',
                k1_max        REAL NOT NULL,
                k1_bookmaker  TEXT NOT NULL,
                kx_max        REAL NOT NULL,
                kx_bookmaker  TEXT NOT NULL,
                k2_max        REAL NOT NULL,
                k2_bookmaker  TEXT NOT NULL,
                profit_pct    REAL NOT NULL,
                stakes_json   TEXT NOT NULL
            )
        """)

        # Аккаунты БК, подключённые в админке. Логин/пароль хранятся
        # ТОЛЬКО в зашифрованном виде (см. app/security.py).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bk_accounts (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                bookmaker         TEXT NOT NULL,
                label             TEXT NOT NULL DEFAULT '',
                login_enc         TEXT NOT NULL,
                password_enc      TEXT NOT NULL,
                enabled           INTEGER NOT NULL DEFAULT 1,
                balance           REAL,
                balance_updated_at TEXT,
                last_error        TEXT,
                created_at        TEXT NOT NULL
            )
        """)
        # cookies_enc — опциональная сессионная cookie (зашифровано так же,
        # как пароль), вставленная оператором вручную из СВОЕГО браузера,
        # где вход (в т.ч. капча/СМС-код) уже пройден человеком. Коннектор
        # пробует её ПЕРЕД сценарием автологина — см. README и docstring
        # app/connectors/selenium_generic.py.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(bk_accounts)")}
        if "cookies_enc" not in cols:
            conn.execute("ALTER TABLE bk_accounts ADD COLUMN cookies_enc TEXT")

        # Журнал попыток авто-ставки (реальных и в режиме имитации) —
        # для аудита: что, когда и с каким результатом бот пытался поставить.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bet_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT NOT NULL,
                match_key     TEXT NOT NULL,
                kind3         INTEGER NOT NULL DEFAULT 0,
                sport         TEXT,
                match         TEXT,
                market        TEXT,
                profit_pct    REAL,
                dry_run       INTEGER NOT NULL DEFAULT 1,
                ok            INTEGER NOT NULL DEFAULT 0,
                legs_json     TEXT NOT NULL,
                error         TEXT
            )
        """)

        # Настройки, которые оператор меняет из админки и которые должны
        # переживать перезапуск (в отличие от флагов авто-ставки — те
        # сознательно только runtime, см. admin_api.update_settings).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)


def get_setting(key: str) -> str | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?",
                           (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))


def get_settings_prefix(prefix: str) -> dict[str, str]:
    """Все настройки, ключ которых начинается с prefix (напр. флаги БК)."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE ?",
            (prefix.replace("%", r"\%") + "%",)).fetchall()
    return {r["key"][len(prefix):]: r["value"] for r in rows}


def get_bool_setting(key: str, default: bool) -> bool:
    """Сохранённый флаг; нет записи — значение по умолчанию (из окружения)."""
    value = get_setting(key)
    if value is None:
        return default
    return value == "1"


def set_bool_setting(key: str, value: bool) -> None:
    set_setting(key, "1" if value else "0")


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


# ---------------------------------------------------------------------------
# Аккаунты БК (админка)
# ---------------------------------------------------------------------------

def add_account(bookmaker: str, login: str, password: str,
               label: str = "", cookies: str = "") -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO bk_accounts
               (bookmaker, label, login_enc, password_enc, cookies_enc,
                enabled, created_at)
               VALUES (?,?,?,?,?,1,?)""",
            (bookmaker, label, encrypt_str(login), encrypt_str(password),
             encrypt_str(cookies) if cookies else None, now),
        )
        return cur.lastrowid


def _account_row_to_dict(r: sqlite3.Row, reveal: bool = False) -> dict:
    d = dict(r)
    if reveal:
        d["login"] = decrypt_str(d.pop("login_enc"))
        d["password"] = decrypt_str(d.pop("password_enc"))
    else:
        login = decrypt_str(d.pop("login_enc"))
        d.pop("password_enc")
        d["login"] = _mask(login)
    # Cookie никогда не отдаём по API (как и пароль) — только признак,
    # что она задана, чтобы фронтенд мог показать «cookie: есть/нет».
    d["has_cookies"] = bool(d.pop("cookies_enc", None))
    d["enabled"] = bool(d["enabled"])
    return d


def _mask(s: str) -> str:
    if not s:
        return s
    if len(s) <= 3:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 3) + s[-1]


def list_accounts(reveal: bool = False) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM bk_accounts ORDER BY id").fetchall()
    return [_account_row_to_dict(r, reveal=reveal) for r in rows]


def get_account(account_id: int, reveal: bool = False) -> dict | None:
    with _lock, _connect() as conn:
        r = conn.execute(
            "SELECT * FROM bk_accounts WHERE id=?", (account_id,)).fetchone()
    return _account_row_to_dict(r, reveal=reveal) if r else None


def get_account_credentials(account_id: int) -> tuple[str, str] | None:
    """(login, password) в открытом виде — только для внутреннего
    использования коннекторами, никогда не отдаётся по API."""
    with _lock, _connect() as conn:
        r = conn.execute(
            "SELECT login_enc, password_enc FROM bk_accounts WHERE id=?",
            (account_id,)).fetchone()
    if not r:
        return None
    return decrypt_str(r["login_enc"]), decrypt_str(r["password_enc"])


def get_account_cookies(account_id: int) -> str | None:
    """Сырая строка cookie в открытом виде («name1=v1; name2=v2») — только
    для внутреннего использования коннекторами, никогда не отдаётся по
    API. None, если cookie не задана оператором."""
    with _lock, _connect() as conn:
        r = conn.execute(
            "SELECT cookies_enc FROM bk_accounts WHERE id=?",
            (account_id,)).fetchone()
    if not r or not r["cookies_enc"]:
        return None
    return decrypt_str(r["cookies_enc"])


def update_account(account_id: int, *, enabled: bool | None = None,
                   label: str | None = None,
                   login: str | None = None,
                   password: str | None = None,
                   cookies: str | None = None) -> None:
    fields, params = [], []
    if enabled is not None:
        fields.append("enabled=?")
        params.append(1 if enabled else 0)
    if label is not None:
        fields.append("label=?")
        params.append(label)
    if login is not None:
        fields.append("login_enc=?")
        params.append(encrypt_str(login))
    if password is not None:
        fields.append("password_enc=?")
        params.append(encrypt_str(password))
    if cookies is not None:
        # Пустая строка — оператор явно очистил cookie (напр. протухла).
        fields.append("cookies_enc=?")
        params.append(encrypt_str(cookies) if cookies else None)
    if not fields:
        return
    params.append(account_id)
    with _lock, _connect() as conn:
        conn.execute(
            f"UPDATE bk_accounts SET {', '.join(fields)} WHERE id=?", params)


def set_account_balance(account_id: int, balance: float | None,
                        error: str | None) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock, _connect() as conn:
        conn.execute(
            """UPDATE bk_accounts
               SET balance=?, balance_updated_at=?, last_error=?
               WHERE id=?""",
            (balance, now, error, account_id))


def delete_account(account_id: int) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM bk_accounts WHERE id=?", (account_id,))


# ---------------------------------------------------------------------------
# Журнал авто-ставок
# ---------------------------------------------------------------------------

def log_bet_attempt(*, match_key: str, kind3: bool, sport: str, match: str,
                    market: str, profit_pct: float, dry_run: bool, ok: bool,
                    legs: list[dict], error: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO bet_log
               (ts, match_key, kind3, sport, match, market, profit_pct,
                dry_run, ok, legs_json, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (now, match_key, 1 if kind3 else 0, sport, match, market,
             profit_pct, 1 if dry_run else 0, 1 if ok else 0,
             json.dumps(legs, ensure_ascii=False), error))


def get_bet_log(limit: int = 200) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM bet_log ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["legs"] = json.loads(d.pop("legs_json"))
        d["kind3"] = bool(d["kind3"])
        d["dry_run"] = bool(d["dry_run"])
        d["ok"] = bool(d["ok"])
        result.append(d)
    return result


def save_arbs3(arbs: list[Arb3]) -> None:
    """История трёхисходных вилок («Исход 1X2»)."""
    if not arbs:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [
        (now, a.kind, a.start_time, a.match_key, a.sport, a.team1, a.team2,
         a.market, a.k1_max, a.k1_bookmaker, a.kx_max, a.kx_bookmaker,
         a.k2_max, a.k2_bookmaker,
         round(a.profit_pct, 2), json.dumps(a.stakes, ensure_ascii=False))
        for a in arbs
    ]
    with _lock, _connect() as conn:
        conn.executemany(
            """INSERT INTO arbs_history_1x2
               (found_at, kind, start_time, match_key, sport, team1, team2,
                market, k1_max, k1_bookmaker, kx_max, kx_bookmaker,
                k2_max, k2_bookmaker, profit_pct, stakes_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )


def get_history_1x2(limit: int = 100) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM arbs_history_1x2 ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["stakes"] = json.loads(d.pop("stakes_json"))
        d["match"] = f"{d['team1']} — {d['team2']}"
        result.append(d)
    return result
