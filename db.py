"""
SQLite: привязки пользователей, OAuth state и кэш ответов FACEIT.

Если задан DB_ENCRYPTION_KEY (ключ Fernet), то steam_id64 и steam_name хранятся
в базе в зашифрованном виде, а ключи кэша — в виде HMAC (по ним нельзя узнать SteamID).
"""

import hashlib
import hmac
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("faceit-bot.db")

DB_PATH = "bot.db"
_fernet: Fernet | None = None
_hmac_key: bytes | None = None
_lock = threading.Lock()  # Flask/waitress и Discord живут в разных потоках


def configure(db_path: str, encryption_key: str | None):
    global DB_PATH, _fernet, _hmac_key
    DB_PATH = db_path
    if encryption_key:
        try:
            _fernet = Fernet(encryption_key.encode())
        except Exception:
            raise RuntimeError(
                "DB_ENCRYPTION_KEY некорректный. Сгенерируй: "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        _hmac_key = hashlib.sha256(b"cache-key:" + encryption_key.encode()).digest()
    else:
        log.warning("DB_ENCRYPTION_KEY не задан — данные в базе хранятся без шифрования")


def _enc(value: str | None) -> str | None:
    if value is None or _fernet is None:
        return value
    return _fernet.encrypt(value.encode()).decode()


def _dec(value: str | None, encrypted: int) -> str | None:
    if value is None or not encrypted:
        return value
    if _fernet is None:
        raise RuntimeError("В базе есть зашифрованные данные, но DB_ENCRYPTION_KEY не задан.")
    try:
        return _fernet.decrypt(value.encode()).decode()
    except InvalidToken:
        raise RuntimeError("Не удалось расшифровать данные: неверный DB_ENCRYPTION_KEY.")


def _cache_key(raw: str) -> str:
    if _hmac_key is None:
        return raw
    return hmac.new(_hmac_key, raw.encode(), hashlib.sha256).hexdigest()


@contextmanager
def get_conn():
    with _lock:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db():
    with get_conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                discord_id  INTEGER PRIMARY KEY,
                steam_id64  TEXT NOT NULL,
                steam_name  TEXT,
                updated_at  INTEGER NOT NULL,
                enc         INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS oauth_states (
                state       TEXT PRIMARY KEY,
                discord_id  INTEGER NOT NULL,
                created_at  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cache (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                created_at  INTEGER NOT NULL
            );
            """
        )
        # база от старой версии могла быть создана без колонки enc
        cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        if "enc" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN enc INTEGER NOT NULL DEFAULT 0")

        now = int(time.time())
        c.execute("DELETE FROM oauth_states WHERE created_at < ?", (now - 3600,))
        c.execute("DELETE FROM cache WHERE created_at < ?", (now - 86400,))

        # дошифровываем старые записи, если ключ появился позже
        if _fernet is not None:
            for r in c.execute(
                "SELECT discord_id, steam_id64, steam_name FROM users WHERE enc = 0"
            ).fetchall():
                c.execute(
                    "UPDATE users SET steam_id64 = ?, steam_name = ?, enc = 1 WHERE discord_id = ?",
                    (_enc(r["steam_id64"]), _enc(r["steam_name"]), r["discord_id"]),
                )

    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass


# --- OAuth state -----------------------------------------------------------

def create_state(state: str, discord_id: int):
    with get_conn() as c:
        c.execute(
            "INSERT INTO oauth_states (state, discord_id, created_at) VALUES (?, ?, ?)",
            (state, discord_id, int(time.time())),
        )


def pop_state(state: str, ttl: int = 600) -> int | None:
    """Возвращает discord_id и удаляет state. None, если не найден или просрочен."""
    with get_conn() as c:
        row = c.execute(
            "SELECT discord_id, created_at FROM oauth_states WHERE state = ?", (state,)
        ).fetchone()
        if not row:
            return None
        c.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
        if time.time() - row["created_at"] > ttl:
            return None
        return row["discord_id"]


# --- Пользователи ----------------------------------------------------------

def save_user(discord_id: int, steam_id64: str, steam_name: str | None):
    encrypted = 1 if _fernet is not None else 0
    with get_conn() as c:
        c.execute(
            """
            INSERT INTO users (discord_id, steam_id64, steam_name, updated_at, enc)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                steam_id64 = excluded.steam_id64,
                steam_name = excluded.steam_name,
                updated_at = excluded.updated_at,
                enc        = excluded.enc
            """,
            (discord_id, _enc(steam_id64), _enc(steam_name), int(time.time()), encrypted),
        )


def get_user(discord_id: int) -> dict | None:
    with get_conn() as c:
        row = c.execute("SELECT * FROM users WHERE discord_id = ?", (discord_id,)).fetchone()
    if row is None:
        return None
    return {
        "discord_id": row["discord_id"],
        "steam_id64": _dec(row["steam_id64"], row["enc"]),
        "steam_name": _dec(row["steam_name"], row["enc"]),
        "updated_at": row["updated_at"],
    }


def delete_user(discord_id: int) -> bool:
    with get_conn() as c:
        cur = c.execute("DELETE FROM users WHERE discord_id = ?", (discord_id,))
        return cur.rowcount > 0


# --- Кэш -------------------------------------------------------------------

def cache_get(key: str, ttl: int) -> str | None:
    with get_conn() as c:
        row = c.execute(
            "SELECT value, created_at FROM cache WHERE key = ?", (_cache_key(key),)
        ).fetchone()
        if row and time.time() - row["created_at"] <= ttl:
            return row["value"]
        return None


def cache_set(key: str, value: str):
    with get_conn() as c:
        c.execute(
            "INSERT INTO cache (key, value, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, created_at = excluded.created_at",
            (_cache_key(key), value, int(time.time())),
        )
