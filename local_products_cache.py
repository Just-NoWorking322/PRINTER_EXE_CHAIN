"""
Локальный кэш карточек товаров (SQLite, тот же файл NurMarketKassa.sqlite3, что и настройки).
Штрихкод → product_id (+ unit): pos_add_item только если единица не «кг» (иначе в чек ушло бы 1 шт без веса).
Для unit=кг всегда pos_scan; кэш пополняется из списка товаров, корзины и поиска.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from config import API_BASE_URL
from product_media import product_image_url


_lock = threading.Lock()
_schema_ready = False


def _enabled() -> bool:
    v = os.environ.get("DESKTOP_MARKET_SQLITE_CACHE", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def cache_db_path() -> Path:
    import app_database

    return app_database.data_db_path()


def _connect() -> sqlite3.Connection:
    import app_database

    app_database.init_database()
    conn = app_database.connect()
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


def _ensure_schema(c: sqlite3.Connection) -> None:
    global _schema_ready
    if _schema_ready:
        return
    _init_schema(c)
    _schema_ready = True


def init_db() -> None:
    if not _enabled():
        return
    import app_database

    app_database.init_database()
    with _lock:
        c = _connect()
        try:
            _ensure_schema(c)
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


def _unit_is_kg_value(unit: Any) -> bool:
    if unit is None:
        return False
    raw = str(unit).strip().lower()
    if not raw:
        return False
    compact = raw.replace(" ", "").replace(".", "")
    if compact in ("кг", "kg", "kг", "kilogram", "kilograms"):
        return True
    if "килограм" in raw:
        return True
    if compact.endswith("кг") or raw.endswith(" kg"):
        return True
    return False


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
            _ensure_schema(c)
            c.execute(
                """
                INSERT INTO products (branch_id, barcode, product_id, name, price, unit, image_url, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(branch_id, barcode) DO UPDATE SET
                    product_id = excluded.product_id,
                    name = COALESCE(excluded.name, products.name),
                    price = COALESCE(excluded.price, products.price),
                    unit = COALESCE(excluded.unit, products.unit),
                    image_url = COALESCE(NULLIF(TRIM(excluded.image_url), ''), products.image_url),
                    updated_at = excluded.updated_at
                """,
                (bk, bc, pid, name, price, unit, image_url, now),
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
            _ensure_schema(c)
            row = c.execute(
                """
                SELECT product_id, unit
                FROM products
                WHERE barcode = ? AND branch_id IN (?, '')
                ORDER BY CASE WHEN branch_id = ? THEN 0 ELSE 1 END, updated_at DESC
                LIMIT 1
                """,
                (bc, bk, bk),
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


def get_cached_products(
    branch_id: str | None,
    *,
    kg_only: bool = False,
    limit: int = 80,
) -> list[dict[str, Any]]:
    if not _enabled():
        return []
    bk = _branch_key(branch_id)
    with _lock:
        c = _connect()
        try:
            _ensure_schema(c)
            rows = c.execute(
                """
                SELECT product_id, barcode, name, price, unit, updated_at, branch_id
                FROM products
                WHERE branch_id IN (?, '')
                ORDER BY CASE WHEN branch_id = ? THEN 0 ELSE 1 END, updated_at DESC
                """,
                (bk, bk),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            c.close()

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        pid = row["product_id"]
        pid_s = str(pid).strip() if pid is not None else ""
        if not pid_s or pid_s in seen:
            continue
        unit = row["unit"]
        unit_s = str(unit).strip() if unit is not None and str(unit).strip() else None
        is_kg = _unit_is_kg_value(unit_s)
        if kg_only and not is_kg:
            continue
        seen.add(pid_s)
        name = row["name"]
        price = row["price"]
        barcode = row["barcode"]
        img = row["image_url"] if "image_url" in row.keys() else None
        img_s = str(img).strip() if img is not None and str(img).strip() else None
        row_d: dict[str, Any] = {
            "id": pid_s,
            "barcode": str(barcode).strip() if barcode is not None and str(barcode).strip() else None,
            "name": str(name).strip() if name is not None and str(name).strip() else f"Товар #{pid_s}",
            "price": str(price).strip() if price is not None and str(price).strip() else "",
            "unit": unit_s,
            "is_weight": is_kg,
            "updated_at": row["updated_at"],
        }
        if img_s:
            row_d["image_url"] = img_s
        out.append(row_d)
        if len(out) >= max(1, int(limit)):
            break
    return out


def get_cached_product_by_barcode(
    branch_id: str | None,
    barcode: str,
) -> dict[str, Any] | None:
    if not _enabled():
        return None
    bc = _norm_barcode(barcode)
    if not bc:
        return None
    bk = _branch_key(branch_id)
    with _lock:
        c = _connect()
        try:
            _ensure_schema(c)
            row = c.execute(
                """
                SELECT product_id, barcode, name, price, unit, updated_at
                FROM products
                WHERE barcode = ? AND branch_id IN (?, '')
                ORDER BY CASE WHEN branch_id = ? THEN 0 ELSE 1 END, updated_at DESC
                LIMIT 1
                """,
                (bc, bk, bk),
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            c.close()
    if not row:
        return None
    pid = str(row["product_id"]).strip() if row["product_id"] is not None else ""
    if not pid:
        return None
    unit = row["unit"]
    unit_s = str(unit).strip() if unit is not None and str(unit).strip() else None
    name = row["name"]
    price = row["price"]
    return {
        "id": pid,
        "product_id": pid,
        "barcode": bc,
        "name": str(name).strip() if name is not None and str(name).strip() else f"Товар #{pid}",
        "price": str(price).strip() if price is not None and str(price).strip() else "",
        "unit": unit_s,
        "is_weight": _unit_is_kg_value(unit_s),
        "updated_at": row["updated_at"],
    }


def get_cached_product_by_id(
    branch_id: str | None,
    product_id: str,
) -> dict[str, Any] | None:
    if not _enabled():
        return None
    pid = str(product_id or "").strip()
    if not pid:
        return None
    bk = _branch_key(branch_id)
    with _lock:
        c = _connect()
        try:
            _ensure_schema(c)
            rows = c.execute(
                """
                SELECT product_id, barcode, name, price, unit, updated_at
                FROM products
                WHERE product_id = ? AND branch_id IN (?, '')
                ORDER BY CASE WHEN branch_id = ? THEN 0 ELSE 1 END, updated_at DESC
                LIMIT 1
                """,
                (pid, bk, bk),
            ).fetchall()
        except sqlite3.Error:
            return None
        finally:
            c.close()
    if not rows:
        return None
    row = rows[0]
    unit = row["unit"]
    unit_s = str(unit).strip() if unit is not None and str(unit).strip() else None
    name = row["name"]
    price = row["price"]
    barcode = row["barcode"]
    return {
        "id": pid,
        "product_id": pid,
        "barcode": str(barcode).strip() if barcode is not None and str(barcode).strip() else None,
        "name": str(name).strip() if name is not None and str(name).strip() else f"Товар #{pid}",
        "price": str(price).strip() if price is not None and str(price).strip() else "",
        "unit": unit_s,
        "is_weight": _unit_is_kg_value(unit_s),
        "updated_at": row["updated_at"],
    }


def search_cached_products(
    branch_id: str | None,
    query: str,
    *,
    limit: int = 40,
) -> list[dict[str, Any]]:
    q = str(query or "").strip().lower()
    if not q:
        return []
    products = get_cached_products(branch_id, kg_only=False, limit=max(80, int(limit) * 6))
    ranked: list[tuple[int, dict[str, Any]]] = []
    for product in products:
        name = str(product.get("name") or "").strip().lower()
        barcode = str(product.get("barcode") or "").strip().lower()
        if not name and not barcode:
            continue
        score = -1
        if name.startswith(q):
            score = 300
        elif q in name:
            score = 200
        elif barcode and barcode.startswith(q):
            score = 180
        elif barcode and q in barcode:
            score = 120
        if score >= 0:
            ranked.append((score, product))
    ranked.sort(
        key=lambda item: (
            -item[0],
            -float(item[1].get("updated_at") or 0),
            str(item[1].get("name") or ""),
        )
    )
    return [product for _, product in ranked[: max(1, int(limit))]]


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
        branch_id,
        bc,
        pid,
        _product_name(p),
        _product_price(p),
        unit=_product_unit(p),
        image_url=_image_url_for_row(p),
    )


def ingest_product_list(branch_id: str | None, products: list) -> None:
    if not _enabled() or not isinstance(products, list):
        return
    bk = _branch_key(branch_id)
    now = time.time()
    rows: list[
        tuple[str, str, str, str | None, str | None, str | None, str | None, float]
    ] = []
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
            (
                bk,
                bc,
                pid,
                _product_name(p),
                _product_price(p),
                _product_unit(p),
                _image_url_for_row(p),
                now,
            )
        )
    if not rows:
        return
    with _lock:
        c = _connect()
        try:
            _ensure_schema(c)
            c.executemany(
                """
                INSERT INTO products (branch_id, barcode, product_id, name, price, unit, image_url, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(branch_id, barcode) DO UPDATE SET
                    product_id = excluded.product_id,
                    name = COALESCE(excluded.name, products.name),
                    price = COALESCE(excluded.price, products.price),
                    unit = COALESCE(excluded.unit, products.unit),
                    image_url = COALESCE(NULLIF(TRIM(excluded.image_url), ''), products.image_url),
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
    rows: list[
        tuple[str, str, str, str | None, str | None, str | None, str | None, float]
    ] = []
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
        img_u: str | None = None
        if isinstance(prod, dict):
            name = _product_name(prod)
            price = _product_price(prod)
            unt = _product_unit(prod)
            img_u = _image_url_for_row(prod)
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
        rows.append((bk, bc, pid_s, name, price, unt, img_u, now))
    if not rows:
        return
    with _lock:
        c = _connect()
        try:
            _ensure_schema(c)
            c.executemany(
                """
                INSERT INTO products (branch_id, barcode, product_id, name, price, unit, image_url, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(branch_id, barcode) DO UPDATE SET
                    product_id = excluded.product_id,
                    name = COALESCE(excluded.name, products.name),
                    price = COALESCE(excluded.price, products.price),
                    unit = COALESCE(excluded.unit, products.unit),
                    image_url = COALESCE(NULLIF(TRIM(excluded.image_url), ''), products.image_url),
                    updated_at = excluded.updated_at
                """,
                rows,
            )
        finally:
            c.close()


KV_CATALOG_FULL_SYNC = "pos_catalog_full_sync_v1"


def is_catalog_full_synced(branch_id: str | None) -> bool:
    """По филиалу уже выполнялась полная выгрузка каталога в SQLite — дальше можно не дёргать list/ на каждый заход."""
    if not _enabled():
        return False
    import app_database
    import json

    app_database.init_database()
    raw = app_database.kv_get(KV_CATALOG_FULL_SYNC)
    if not raw:
        return False
    try:
        d = json.loads(raw)
        return str(d.get("branch_id") or "") == _branch_key(branch_id) and bool(d.get("ok"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return False


def set_catalog_full_synced(branch_id: str | None) -> None:
    if not _enabled():
        return
    import app_database
    import json

    app_database.init_database()
    app_database.kv_set(
        KV_CATALOG_FULL_SYNC,
        json.dumps(
            {"branch_id": _branch_key(branch_id), "ok": True, "at": time.time()},
            ensure_ascii=False,
        ),
    )


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
            _ensure_schema(c)
            c.execute("DELETE FROM products WHERE branch_id = ?", (bk,))
        finally:
            c.close()


def clear_all() -> None:
    if not _enabled():
        return
    with _lock:
        c = _connect()
        try:
            _ensure_schema(c)
            c.execute("DELETE FROM products")
        finally:
            c.close()
