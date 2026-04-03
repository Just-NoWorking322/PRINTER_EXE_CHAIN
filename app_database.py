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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS offline_session (
            email TEXT PRIMARY KEY NOT NULL,
            access_token TEXT,
            refresh_token TEXT,
            branch_id TEXT,
            user_payload TEXT NOT NULL,
            cashbox_id TEXT,
            shift_id TEXT,
            shift_open INTEGER NOT NULL DEFAULT 0,
            saved_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS offline_cart (
            cart_id TEXT PRIMARY KEY NOT NULL,
            email TEXT NOT NULL,
            branch_id TEXT,
            company TEXT,
            cashier TEXT,
            cashbox_id TEXT,
            shift_id TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            order_discount_percent TEXT,
            order_discount_total TEXT NOT NULL DEFAULT '0.00',
            subtotal TEXT NOT NULL DEFAULT '0.00',
            discount_total TEXT NOT NULL DEFAULT '0.00',
            total TEXT NOT NULL DEFAULT '0.00',
            sale_local_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            checked_out_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS offline_cart_items (
            item_id TEXT PRIMARY KEY NOT NULL,
            cart_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            barcode TEXT,
            name TEXT NOT NULL,
            unit TEXT,
            is_weight INTEGER NOT NULL DEFAULT 0,
            quantity TEXT NOT NULL,
            unit_price TEXT NOT NULL,
            discount_total TEXT NOT NULL DEFAULT '0.00',
            line_total TEXT NOT NULL DEFAULT '0.00',
            product_payload TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY(cart_id) REFERENCES offline_cart(cart_id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_offline_cart_status ON offline_cart(status, updated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_offline_cart_email_branch ON offline_cart(email, branch_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_offline_cart_items_cart ON offline_cart_items(cart_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS offline_sales (
            sale_local_id TEXT PRIMARY KEY NOT NULL,
            cart_id TEXT NOT NULL,
            email TEXT NOT NULL,
            branch_id TEXT,
            company TEXT,
            cashier TEXT,
            cashbox_id TEXT,
            shift_id TEXT,
            payment_method TEXT NOT NULL,
            cash_received TEXT,
            change_amount TEXT,
            subtotal TEXT NOT NULL DEFAULT '0.00',
            discount_total TEXT NOT NULL DEFAULT '0.00',
            total TEXT NOT NULL DEFAULT '0.00',
            order_discount_percent TEXT,
            order_discount_total TEXT NOT NULL DEFAULT '0.00',
            status TEXT NOT NULL DEFAULT 'pending_sync',
            server_sale_id TEXT,
            sale_payload TEXT NOT NULL,
            last_error TEXT,
            created_at REAL NOT NULL,
            synced_at REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_offline_sales_status ON offline_sales(status, created_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_queue (
            sale_local_id TEXT PRIMARY KEY NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            next_retry_at REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY(sale_local_id) REFERENCES offline_sales(sale_local_id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_queue_status_retry ON sync_queue(status, next_retry_at, updated_at)"
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
