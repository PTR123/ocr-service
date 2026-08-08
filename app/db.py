"""SQLite 存储层：API key 管理与用量记录。

表结构：
- api_keys:  id, name(用途说明), key(明文，管理端可见), created_at, active
- usage_log: id, key_id, created_at, duration_ms, file_size

说明：key 明文存储（管理端需要展示/复制），属于内网服务凭据；
如需更高安全等级，可改为哈希存储 + 展示不可逆掩码。
"""
import os
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(os.getenv("OCR_DB_PATH", "ocr.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,              -- 用途说明（哪个业务/系统）
    key        TEXT NOT NULL UNIQUE,       -- API key（明文）
    created_at INTEGER NOT NULL,           -- unix 秒
    active     INTEGER NOT NULL DEFAULT 1  -- 1=启用 0=停用
);

CREATE TABLE IF NOT EXISTS usage_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id      TEXT NOT NULL,
    created_at  INTEGER NOT NULL,          -- unix 秒
    duration_ms INTEGER NOT NULL,          -- OCR 推理耗时
    file_size   INTEGER NOT NULL           -- 图片字节数
);
CREATE INDEX IF NOT EXISTS idx_usage_key_time ON usage_log(key_id, created_at);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---------- API key ----------

def create_key(name: str, key: str) -> dict:
    conn = _connect()
    try:
        key_id = f"k_{int(time.time())}_{os.urandom(3).hex()}"
        conn.execute(
            "INSERT INTO api_keys (id, name, key, created_at, active) VALUES (?, ?, ?, ?, 1)",
            (key_id, name, key, int(time.time())),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def list_keys() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_key(key_id: str) -> bool:
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_key_active(key_id: str, active: bool) -> bool:
    conn = _connect()
    try:
        cur = conn.execute("UPDATE api_keys SET active = ? WHERE id = ?", (1 if active else 0, key_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def validate_key(key: str) -> str | None:
    """校验 API key，返回 key_id；无效返回 None。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id FROM api_keys WHERE key = ? AND active = 1", (key,)
        ).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


# ---------- 用量 ----------

def log_usage(key_id: str, duration_ms: int, file_size: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO usage_log (key_id, created_at, duration_ms, file_size) VALUES (?, ?, ?, ?)",
            (key_id, int(time.time()), duration_ms, file_size),
        )
        conn.commit()
    finally:
        conn.close()


def usage_for_key(key_id: str, since: int | None = None) -> dict:
    """某 key 的用量：总调用、平均耗时、总图片量。since=unix 秒（可选，按天）。"""
    conn = _connect()
    try:
        where = "WHERE key_id = ?" + (" AND created_at >= ?" if since else "")
        params: list = [key_id] + ([since] if since else [])
        row = conn.execute(
            f"SELECT COUNT(*) AS calls, COALESCE(AVG(duration_ms),0) AS avg_ms, "
            f"COALESCE(SUM(file_size),0) AS total_bytes FROM usage_log {where}",
            params,
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def usage_all(since: int | None = None) -> list[dict]:
    """所有 key 的用量汇总（dashboard 用）。"""
    conn = _connect()
    try:
        since_sql = "AND u.created_at >= ?" if since else ""
        params: list = [since] if since else []
        rows = conn.execute(
            f"""
            SELECT k.id AS key_id, k.name, k.key, k.active, k.created_at AS key_created_at,
                   COUNT(u.id) AS calls,
                   COALESCE(AVG(u.duration_ms), 0) AS avg_ms,
                   COALESCE(SUM(u.file_size), 0) AS total_bytes,
                   MAX(u.created_at) AS last_used_at
            FROM api_keys k
            LEFT JOIN usage_log u ON u.key_id = k.id {since_sql}
            GROUP BY k.id
            ORDER BY k.created_at DESC
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
