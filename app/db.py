"""SQLite 访问层：网页进程写，权威进程读（同一文件，WAL 模式）。"""
import os
import sqlite3
import time
from typing import Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    name       TEXT PRIMARY KEY,
    ip         TEXT NOT NULL,
    ttl        INTEGER NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    """每次调用返回新连接。

    权威端每个查询开一个短连接，保证网页写入后立刻能读到最新数据；
    WAL 模式允许 web 写与 authoritative 读并发而互不阻塞。
    """
    path = db_path or config.DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db() -> None:
    """建表并写入默认设置 / 种子记录（仅 web 启动时调用一次）。"""
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        defaults = {
            "neg_ttl": str(config.DEFAULT_NEG_TTL),
        }
        for name in config.DOMAINS:
            defaults[f"delay_{name}"] = str(config.DEFAULT_DELAY_MS)
            defaults[f"dropped_{name}"] = "0"
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )
        # 第一个域名放种子 A 记录，第二个默认不存在（便于演示 NXDOMAIN 负缓存）
        if config.DOMAINS:
            conn.execute(
                "INSERT OR IGNORE INTO records(name, ip, ttl, updated_at) VALUES (?, ?, ?, ?)",
                (config.DOMAINS[0], "10.0.0.10", config.DEFAULT_POS_TTL, time.time()),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------- records ----------------

def get_record(conn: sqlite3.Connection, name: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM records WHERE name = ?", (name,)).fetchone()


def list_records(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM records ORDER BY name").fetchall()


def upsert_record(name: str, ip: str, ttl: int) -> None:
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO records(name, ip, ttl, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                ip = excluded.ip,
                ttl = excluded.ttl,
                updated_at = excluded.updated_at
            """,
            (name, ip, ttl, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def delete_record(name: str) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM records WHERE name = ?", (name,))
        conn.commit()
    finally:
        conn.close()


# ---------------- settings ----------------

def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_int_setting(conn: sqlite3.Connection, key: str, default: int = 0) -> int:
    try:
        return int(get_setting(conn, key, str(default)))
    except ValueError:
        return default


def get_bool_setting(conn: sqlite3.Connection, key: str, default: bool = False) -> bool:
    return get_setting(conn, key, "1" if default else "0") == "1"


def set_setting(key: str, value: str) -> None:
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()
