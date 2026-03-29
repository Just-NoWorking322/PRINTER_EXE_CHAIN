"""
Локальный кэш карточек товаров (SQLite, WAL) для ускорения сканирования.
Штрихкод → product_id (+ unit): pos_add_item только если единица не «кг» (иначе в чек ушло бы 1 шт без веса).
Для unit=кг всегда pos_scan; кэш пополняется из списка товаров, корзины и поиска.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any


_lock = threading.Lock()


def _enabled() -> bool:
    v = os.environ.get("DESKTOP_MARKET_SQLITE_CACHE", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def cache_db_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "pos_products_cache.sqlite3"
    return Path(__file__).resolve().parent / "pos_products_cache.sqlite3"


def _connect() -> sqlite3.Connection:
    path = cache_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.row_factory = sqlite3.Row
    return conn


def _init_schema(c: sqlite3.Connection) -> None:
    c.execute(
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
    cols = {row[1] for row in c.execute("PRAGMA table_info(products)").fetchall()}
    if "unit" not in cols:
        c.execute("ALTER TABLE products ADD COLUMN unit TEXT")
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_branch_pid ON products(branch_id, product_id)"
    )


def init_db() -> None:
    if not _enabled():
        return
    with _lock:
        c = _connect()
        try:
            _init_schema(c)
        finally:
            c.close()


def _norm_barcode(b: str) -> str:
    return (b or "").strip()


def _branch_key(branch_id: str | None) -> str:
    return str(branch_id).strip() if branch_id is not None else ""


def _product_id(p: dict[str, Any]) -> str | None:
    pid = p.get("id")
    if pid is None:
        return None
    s = str(pid).strip()
    return s or None


def _product_barcode(p: dict[str, Any]) -> str | None:
    for k in ("barcode", "ean", "ean13", "sku", "article", "code"):
        v = p.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    bcs = p.get("barcodes")
    if isinstance(bcs, list):
        for x in bcs:
            if x is not None and str(x).strip():
                return str(x).strip()
    if isinstance(bcs, str) and bcs.strip():
        try:
            data = json.loads(bcs)
            if isinstance(data, list):
                for x in data:
                    if x is not None and str(x).strip():
                        return str(x).strip()
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def _product_name(p: dict[str, Any]) -> str | None:
    for k in ("name", "title", "display_name", "name_ru", "full_name", "label"):
        v = p.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def _product_price(p: dict[str, Any]) -> str | None:
    for k in ("price", "sale_price", "retail_price", "base_price"):
        v = p.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def _product_unit(p: dict[str, Any]) -> str | None:
    v = p.get("unit")
    if v is not None and str(v).strip():
        return str(v).strip()
    return None


def upsert_row(
    branch_id: str | None,
    barcode: str,
    product_id: str,
    name: str | None = None,
    price: str | None = None,
    unit: str | None = None,
) -> None:
    if not _enabled():
        return
    bc = _norm_barcode(barcode)
    pid = str(product_id).strip()
    if not bc or not pid:
        return
    bk = _branch_key(branch_id)
    now = time.time()
    with _lock:
        c = _connect()
        try:
            _init_schema(c)
            c.execute(
                """
                INSERT INTO products (branch_id, barcode, product_id, name, price, unit, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(branch_id, barcode) DO UPDATE SET
                    product_id = excluded.product_id,
                    name = COALESCE(excluded.name, products.name),
                    price = COALESCE(excluded.price, products.price),
                    unit = COALESCE(excluded.unit, products.unit),
                    updated_at = excluded.updated_at
                """,
                (bk, bc, pid, name, price, unit, now),
            )
        finally:
            c.close()


def get_cached_product_id(branch_id: str | None, barcode: str) -> str | None:
    row = get_cached_scan_row(branch_id, barcode)
    return str(row["product_id"]) if row else None


def get_cached_scan_row(branch_id: str | None, barcode: str) -> dict[str, Any] | None:
    """Кэш по штрихкоду: product_id и unit (кг/шт) для выбора pos_add_item vs pos_scan."""
    if not _enabled():
        return None
    bc = _norm_barcode(barcode)
    if not bc:
        return None
    bk = _branch_key(branch_id)
    with _lock:
        c = _connect()
        try:
            _init_schema(c)
            row = c.execute(
                "SELECT product_id, unit FROM products WHERE branch_id = ? AND barcode = ?",
                (bk, bc),
            ).fetchone()
            if not row:
                return None
            uid = row["product_id"]
            if uid is None:
                return None
            u = row["unit"]
            unit_s = str(u).strip() if u is not None and str(u).strip() else None
            return {"product_id": str(uid), "unit": unit_s}
        except sqlite3.Error:
            return None
        finally:
            c.close()


def ingest_product_dict(branch_id: str | None, p: dict[str, Any], barcode_hint: str | None = None) -> None:
    if not _enabled() or not isinstance(p, dict):
        return
    pid = _product_id(p)
    if not pid:
        return
    bc = _norm_barcode(barcode_hint or "") or _product_barcode(p)
    if not bc:
        return
    upsert_row(
        branch_id, bc, pid, _product_name(p), _product_price(p), unit=_product_unit(p)
    )


def ingest_product_list(branch_id: str | None, products: list) -> None:
    if not _enabled() or not isinstance(products, list):
        return
    bk = _branch_key(branch_id)
    now = time.time()
    rows: list[tuple[str, str, str, str | None, str | None, str | None, float]] = []
    for p in products:
        if not isinstance(p, dict):
            continue
        pid = _product_id(p)
        if not pid:
            continue
        bc = _product_barcode(p)
        if not bc:
            continue
        rows.append(
            (bk, bc, pid, _product_name(p), _product_price(p), _product_unit(p), now)
        )
    if not rows:
        return
    with _lock:
        c = _connect()
        try:
            _init_schema(c)
            c.executemany(
                """
                INSERT INTO products (branch_id, barcode, product_id, name, price, unit, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(branch_id, barcode) DO UPDATE SET
                    product_id = excluded.product_id,
                    name = COALESCE(excluded.name, products.name),
                    price = COALESCE(excluded.price, products.price),
                    unit = COALESCE(excluded.unit, products.unit),
                    updated_at = excluded.updated_at
                """,
                rows,
            )
        finally:
            c.close()


def ingest_cart(branch_id: str | None, cart: dict[str, Any]) -> None:
    if not _enabled() or not isinstance(cart, dict):
        return
    items = cart.get("items") or cart.get("cart_items") or []
    if not isinstance(items, list):
        return
    bk = _branch_key(branch_id)
    now = time.time()
    rows: list[tuple[str, str, str, str | None, str | None, str | None, float]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        pid = it.get("product_id")
        if pid is None:
            prod = it.get("product")
            if isinstance(prod, dict):
                pid = prod.get("id")
        if pid is None:
            continue
        pid_s = str(pid).strip()
        if not pid_s:
            continue
        bc = _norm_barcode(str(it.get("barcode") or ""))
        prod = it.get("product")
        if not bc and isinstance(prod, dict):
            bc = _product_barcode(prod) or ""
        if not bc:
            continue
        name = None
        price = None
        unt: str | None = None
        if isinstance(prod, dict):
            name = _product_name(prod)
            price = _product_price(prod)
            unt = _product_unit(prod)
        if name is None:
            name = _name_from_item(it)
        if price is None:
            for k in ("unit_price", "price"):
                v = it.get(k)
                if v is not None and str(v).strip():
                    price = str(v).strip()
                    break
        if unt is None:
            u = it.get("unit")
            if u is not None and str(u).strip():
                unt = str(u).strip()
        rows.append((bk, bc, pid_s, name, price, unt, now))
    if not rows:
        return
    with _lock:
        c = _connect()
        try:
            _init_schema(c)
            c.executemany(
                """
                INSERT INTO products (branch_id, barcode, product_id, name, price, unit, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(branch_id, barcode) DO UPDATE SET
                    product_id = excluded.product_id,
                    name = COALESCE(excluded.name, products.name),
                    price = COALESCE(excluded.price, products.price),
                    unit = COALESCE(excluded.unit, products.unit),
                    updated_at = excluded.updated_at
                """,
                rows,
            )
        finally:
            c.close()


def _name_from_item(it: dict[str, Any]) -> str | None:
    for k in ("product_name", "name", "title", "display_name"):
        v = it.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def clear_branch(branch_id: str | None) -> None:
    if not _enabled():
        return
    bk = _branch_key(branch_id)
    with _lock:
        c = _connect()
        try:
            _init_schema(c)
            c.execute("DELETE FROM products WHERE branch_id = ?", (bk,))
        finally:
            c.close()


def clear_all() -> None:
    if not _enabled():
        return
    with _lock:
        c = _connect()
        try:
            _init_schema(c)
            c.execute("DELETE FROM products")
        finally:
            c.close()
