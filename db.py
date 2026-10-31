"""Работа с SQLite: привязки пользователей, OAuth state и кэш ответов FACEIT."""

import os
import sqlite3
import threading
import time
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "bot.db")
_lock = threading.Lock()  # Flask и Discord живут в разных потоках


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
                updated_at  INTEGER NOT NULL
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
        # чистим просроченное
        now = int(time.time())
        c.execute("DELETE FROM oauth_states WHERE created_at < ?", (now - 3600,))
        c.execute("DELETE FROM cache WHERE created_at < ?", (now - 86400,))


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
    with get_conn() as c:
        c.execute(
            """
            INSERT INTO users (discord_id, steam_id64, steam_name, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                steam_id64 = excluded.steam_id64,
                steam_name = excluded.steam_name,
                updated_at = excluded.updated_at
            """,
            (discord_id, steam_id64, steam_name, int(time.time())),
        )


def get_user(discord_id: int):
    with get_conn() as c:
        return c.execute("SELECT * FROM users WHERE discord_id = ?", (discord_id,)).fetchone()


def delete_user(discord_id: int) -> bool:
    with get_conn() as c:
        cur = c.execute("DELETE FROM users WHERE discord_id = ?", (discord_id,))
        return cur.rowcount > 0


# --- Кэш -------------------------------------------------------------------

def cache_get(key: str, ttl: int) -> str | None:
    with get_conn() as c:
        row = c.execute("SELECT value, created_at FROM cache WHERE key = ?", (key,)).fetchone()
        if row and time.time() - row["created_at"] <= ttl:
            return row["value"]
        return None


def cache_set(key: str, value: str):
    with get_conn() as c:
        c.execute(
            "INSERT INTO cache (key, value, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, created_at = excluded.created_at",
            (key, value, int(time.time())),
        )
