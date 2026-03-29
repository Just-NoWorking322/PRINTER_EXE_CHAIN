"""
Единый локальный файл данных рядом с exe: SQLite + JSON в таблице kv_store.
Товары (кэш штрихкодов) — таблица products. Без WAL — только один файл .sqlite3 рядом с программой.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import threading
from pathlib import Path
from typing import Any

DATA_FILENAME = "NurMarketKassa.sqlite3"

_lock = threading.Lock()
_initialized = False

KV_KEY_PRINTER = "printer_settings"


def data_db_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / DATA_FILENAME
    return Path(__file__).resolve().parent / DATA_FILENAME


def connect() -> sqlite3.Connection:
    path = data_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=30.0,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_kv_and_products(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kv_store (
            key TEXT PRIMARY KEY NOT NULL,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            branch_id TEXT NOT NULL DEFAULT '',
            barcode TEXT NOT NULL,
            product_id TEXT NOT NULL,
            name TEXT,
            price TEXT,
            updated_at REAL NOT NULL,
            PRIMARY KEY (branch_id, barcode)
        )
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(products)").fetchall()}
    if "unit" not in cols:
        conn.execute("ALTER TABLE products ADD COLUMN unit TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_branch_pid ON products(branch_id, product_id)"
    )


def _migrate_legacy_json(conn: sqlite3.Connection) -> None:
    base = data_db_path().parent
    legacy = base / "printer_settings.json"
    if not legacy.is_file():
        return
    row = conn.execute(
        "SELECT 1 FROM kv_store WHERE key = ?", (KV_KEY_PRINTER,)
    ).fetchone()
    if row:
        return
    try:
        raw = legacy.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            conn.execute(
                "INSERT INTO kv_store (key, value) VALUES (?, ?)",
                (KV_KEY_PRINTER, json.dumps(data, ensure_ascii=False)),
            )
            legacy.rename(base / "printer_settings.json.migrated")
    except (OSError, json.JSONDecodeError):
        pass


def _migrate_legacy_products_db(conn: sqlite3.Connection) -> None:
    base = data_db_path().parent
    old = base / "pos_products_cache.sqlite3"
    if not old.is_file():
        return
    try:
        n = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
        if int(n) > 0:
            return
    except sqlite3.Error:
        return
    try:
        leg = sqlite3.connect(str(old), timeout=15.0)
        leg.row_factory = sqlite3.Row
        try:
            cols = {r[1] for r in leg.execute("PRAGMA table_info(products)").fetchall()}
            if not cols:
                return
            has_unit = "unit" in cols
            rows = leg.execute("SELECT * FROM products").fetchall()
            for r in rows:
                d = dict(r)
                uid = d.get("unit") if has_unit else None
                try:
                    uat = float(d.get("updated_at") or 0)
                except (TypeError, ValueError):
                    uat = 0.0
                if uat <= 0:
                    uat = time.time()
                conn.execute(
                    """
                    INSERT OR IGNORE INTO products
                    (branch_id, barcode, product_id, name, price, unit, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        d.get("branch_id") or "",
                        d.get("barcode") or "",
                        d.get("product_id") or "",
                        d.get("name"),
                        d.get("price"),
                        str(uid).strip() if uid is not None and str(uid).strip() else None,
                        uat,
                    ),
                )
        finally:
            leg.close()
        old.rename(base / "pos_products_cache.sqlite3.migrated")
    except (OSError, sqlite3.Error, TypeError, ValueError):
        pass


def init_database() -> None:
    """Создать схему, перенести старые printer_settings.json и pos_products_cache.sqlite3 (один раз)."""
    global _initialized
    with _lock:
        if _initialized:
            return
        conn = connect()
        try:
            _init_kv_and_products(conn)
            _migrate_legacy_json(conn)
            _migrate_legacy_products_db(conn)
            _initialized = True
        finally:
            conn.close()


def kv_get(key: str) -> str | None:
    init_database()
    with _lock:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT value FROM kv_store WHERE key = ?", (key,)
            ).fetchone()
            return str(row["value"]) if row else None
        finally:
            conn.close()


def kv_set(key: str, value_json: str) -> None:
    init_database()
    with _lock:
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO kv_store (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value_json),
            )
        finally:
            conn.close()
