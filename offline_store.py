from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

import app_database


def _now() -> float:
    return time.time()


def _connect() -> sqlite3.Connection:
    app_database.init_database()
    return app_database.connect()


def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _json_loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    try:
        data = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return default
    return data


def _row_to_session(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "email": str(row["email"]),
        "access_token": row["access_token"],
        "refresh_token": row["refresh_token"],
        "branch_id": row["branch_id"],
        "cashbox_id": row["cashbox_id"],
        "shift_id": row["shift_id"],
        "shift_open": bool(int(row["shift_open"] or 0)),
        "saved_at": float(row["saved_at"] or 0),
        "user_payload": _json_loads(row["user_payload"], {}),
    }


def save_offline_session(
    *,
    email: str,
    access_token: str | None,
    refresh_token: str | None,
    user_payload: dict[str, Any],
    branch_id: str | None,
    cashbox_id: str | None = None,
    shift_id: str | None = None,
    shift_open: bool = False,
) -> None:
    email_s = (email or "").strip().lower()
    if not email_s:
        return
    ts = _now()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO offline_session
            (email, access_token, refresh_token, branch_id, user_payload, cashbox_id, shift_id, shift_open, saved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                branch_id = excluded.branch_id,
                user_payload = excluded.user_payload,
                cashbox_id = excluded.cashbox_id,
                shift_id = excluded.shift_id,
                shift_open = excluded.shift_open,
                saved_at = excluded.saved_at
            """,
            (
                email_s,
                access_token,
                refresh_token,
                str(branch_id).strip() if branch_id is not None else None,
                _json_dumps(user_payload or {}),
                str(cashbox_id).strip() if cashbox_id is not None and str(cashbox_id).strip() else None,
                str(shift_id).strip() if shift_id is not None and str(shift_id).strip() else None,
                1 if shift_open else 0,
                ts,
            ),
        )
    finally:
        conn.close()


def load_offline_session(email: str | None = None) -> dict[str, Any] | None:
    conn = _connect()
    try:
        if email and str(email).strip():
            row = conn.execute(
                "SELECT * FROM offline_session WHERE email = ?",
                (str(email).strip().lower(),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM offline_session ORDER BY saved_at DESC LIMIT 1"
            ).fetchone()
        return _row_to_session(row)
    finally:
        conn.close()


def update_offline_session_state(
    email: str,
    *,
    branch_id: str | None = None,
    cashbox_id: str | None = None,
    shift_id: str | None = None,
    shift_open: bool | None = None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    user_payload: dict[str, Any] | None = None,
) -> None:
    current = load_offline_session(email)
    if not current:
        return
    save_offline_session(
        email=current["email"],
        access_token=access_token if access_token is not None else current.get("access_token"),
        refresh_token=refresh_token if refresh_token is not None else current.get("refresh_token"),
        user_payload=user_payload if user_payload is not None else dict(current.get("user_payload") or {}),
        branch_id=branch_id if branch_id is not None else current.get("branch_id"),
        cashbox_id=cashbox_id if cashbox_id is not None else current.get("cashbox_id"),
        shift_id=shift_id if shift_id is not None else current.get("shift_id"),
        shift_open=shift_open if shift_open is not None else bool(current.get("shift_open")),
    )


def _parse_float(v: Any) -> float:
    try:
        if v is None or (isinstance(v, str) and not v.strip()):
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _money(v: Any) -> str:
    return f"{_parse_float(v):.2f}"


def _quantity(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        f = 0.0
    if abs(f - int(f)) < 1e-9:
        return str(int(f))
    return f"{f:.3f}".rstrip("0").rstrip(".")


def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    product = _json_loads(row["product_payload"], {})
    if not isinstance(product, dict):
        product = {}
    product.setdefault("id", str(row["product_id"]))
    product.setdefault("name", row["name"])
    product.setdefault("price", row["unit_price"])
    product.setdefault("unit", row["unit"])
    if row["barcode"] and "barcode" not in product:
        product["barcode"] = row["barcode"]
    if int(row["is_weight"] or 0):
        product.setdefault("is_weight", True)
    return {
        "id": str(row["item_id"]),
        "product_id": str(row["product_id"]),
        "barcode": row["barcode"],
        "product_name": row["name"],
        "quantity": _quantity(row["quantity"]),
        "unit": row["unit"],
        "is_weight": bool(int(row["is_weight"] or 0)),
        "unit_price": _money(row["unit_price"]),
        "discount_total": _money(row["discount_total"]),
        "line_total": _money(row["line_total"]),
        "product": product,
        "product_snapshot": product,
    }


def _load_cart_items(conn: sqlite3.Connection, cart_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM offline_cart_items
        WHERE cart_id = ?
        ORDER BY created_at ASC
        """,
        (cart_id,),
    ).fetchall()
    return [_row_to_item(row) for row in rows]


def _cart_from_row(conn: sqlite3.Connection, row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    cart_id = str(row["cart_id"])
    items = _load_cart_items(conn, cart_id)
    return {
        "id": cart_id,
        "offline": True,
        "status": row["status"],
        "email": row["email"],
        "branch_id": row["branch_id"],
        "company": row["company"],
        "cashier": row["cashier"],
        "cashbox_id": row["cashbox_id"],
        "shift_id": row["shift_id"],
        "order_discount_percent": row["order_discount_percent"],
        "order_discount_total": _money(row["order_discount_total"]),
        "subtotal": _money(row["subtotal"]),
        "discount_total": _money(row["discount_total"]),
        "total": _money(row["total"]),
        "amount_due": _money(row["total"]),
        "items": items,
        "cart_items": items,
        "created_at": float(row["created_at"] or 0),
        "updated_at": float(row["updated_at"] or 0),
    }


def _recalc_cart(conn: sqlite3.Connection, cart_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT order_discount_percent, order_discount_total
        FROM offline_cart
        WHERE cart_id = ?
        """,
        (cart_id,),
    ).fetchone()
    if not row:
        return None
    items = conn.execute(
        """
        SELECT quantity, unit_price, discount_total
        FROM offline_cart_items
        WHERE cart_id = ?
        """,
        (cart_id,),
    ).fetchall()
    subtotal = 0.0
    line_discount = 0.0
    for it in items:
        qty = _parse_float(it["quantity"])
        unit_price = _parse_float(it["unit_price"])
        disc = _parse_float(it["discount_total"])
        subtotal += qty * unit_price
        line_discount += disc
    order_discount_total = _parse_float(row["order_discount_total"])
    discount_total = line_discount + order_discount_total
    total = max(0.0, subtotal - discount_total)
    conn.execute(
        """
        UPDATE offline_cart
        SET subtotal = ?, discount_total = ?, total = ?, updated_at = ?
        WHERE cart_id = ?
        """,
        (_money(subtotal), _money(discount_total), _money(total), _now(), cart_id),
    )
    return get_cart(cart_id)


def get_active_cart(email: str, branch_id: str | None) -> dict[str, Any] | None:
    email_s = (email or "").strip().lower()
    if not email_s:
        return None
    conn = _connect()
    try:
        if branch_id is not None and str(branch_id).strip():
            row = conn.execute(
                """
                SELECT *
                FROM offline_cart
                WHERE email = ? AND branch_id = ? AND status = 'active'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (email_s, str(branch_id).strip()),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT *
                FROM offline_cart
                WHERE email = ? AND status = 'active'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (email_s,),
            ).fetchone()
        return _cart_from_row(conn, row)
    finally:
        conn.close()


def get_cart(cart_id: str) -> dict[str, Any] | None:
    cart_id_s = (cart_id or "").strip()
    if not cart_id_s:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM offline_cart WHERE cart_id = ?",
            (cart_id_s,),
        ).fetchone()
        return _cart_from_row(conn, row)
    finally:
        conn.close()


def create_cart(
    *,
    email: str,
    branch_id: str | None,
    company: str,
    cashier: str,
    cashbox_id: str | None,
    shift_id: str | None,
) -> dict[str, Any]:
    ts = _now()
    cart_id = f"offline-{uuid.uuid4()}"
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO offline_cart
            (cart_id, email, branch_id, company, cashier, cashbox_id, shift_id, status,
             order_discount_percent, order_discount_total, subtotal, discount_total, total,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', NULL, '0.00', '0.00', '0.00', '0.00', ?, ?)
            """,
            (
                cart_id,
                (email or "").strip().lower(),
                str(branch_id).strip() if branch_id is not None else None,
                company,
                cashier,
                str(cashbox_id).strip() if cashbox_id is not None and str(cashbox_id).strip() else None,
                str(shift_id).strip() if shift_id is not None and str(shift_id).strip() else None,
                ts,
                ts,
            ),
        )
        return get_cart(cart_id)
    finally:
        conn.close()


def ensure_active_cart(
    *,
    email: str,
    branch_id: str | None,
    company: str,
    cashier: str,
    cashbox_id: str | None,
    shift_id: str | None,
) -> dict[str, Any]:
    existing = get_active_cart(email, branch_id)
    if existing:
        return existing
    return create_cart(
        email=email,
        branch_id=branch_id,
        company=company,
        cashier=cashier,
        cashbox_id=cashbox_id,
        shift_id=shift_id,
    )


def add_or_merge_item(
    *,
    cart_id: str,
    product: dict[str, Any],
    quantity: str,
    unit_price: str,
    discount_total: str = "0.00",
    barcode: str | None = None,
) -> dict[str, Any] | None:
    cart_id_s = (cart_id or "").strip()
    pid = str(product.get("id") or product.get("product_id") or "").strip()
    if not cart_id_s or not pid:
        return None
    qty = _parse_float(quantity)
    if qty <= 0:
        return get_cart(cart_id_s)
    price = _parse_float(unit_price)
    disc = max(0.0, _parse_float(discount_total))
    is_weight = 1 if bool(product.get("is_weight")) else 0
    unit = str(product.get("unit") or "").strip() or None
    name = str(product.get("name") or product.get("title") or f"Товар #{pid}").strip()
    barcode_s = str(barcode or product.get("barcode") or "").strip() or None
    now_ts = _now()
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT *
            FROM offline_cart_items
            WHERE cart_id = ? AND product_id = ? AND unit_price = ? AND discount_total = ? AND is_weight = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (cart_id_s, pid, _money(price), _money(disc), is_weight),
        ).fetchone()
        if row:
            merged_qty = _parse_float(row["quantity"]) + qty
            line_total = max(0.0, merged_qty * price - disc)
            conn.execute(
                """
                UPDATE offline_cart_items
                SET quantity = ?, line_total = ?, updated_at = ?
                WHERE item_id = ?
                """,
                (_quantity(merged_qty), _money(line_total), now_ts, row["item_id"]),
            )
        else:
            item_id = f"offline-item-{uuid.uuid4()}"
            line_total = max(0.0, qty * price - disc)
            payload = dict(product)
            if barcode_s and "barcode" not in payload:
                payload["barcode"] = barcode_s
            conn.execute(
                """
                INSERT INTO offline_cart_items
                (item_id, cart_id, product_id, barcode, name, unit, is_weight, quantity,
                 unit_price, discount_total, line_total, product_payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    cart_id_s,
                    pid,
                    barcode_s,
                    name,
                    unit,
                    is_weight,
                    _quantity(qty),
                    _money(price),
                    _money(disc),
                    _money(line_total),
                    _json_dumps(payload),
                    now_ts,
                    now_ts,
                ),
            )
        return _recalc_cart(conn, cart_id_s)
    finally:
        conn.close()


def update_item(
    *,
    cart_id: str,
    item_id: str,
    quantity: str | None = None,
    unit_price: str | None = None,
    discount_total: str | None = None,
) -> dict[str, Any] | None:
    cart_id_s = (cart_id or "").strip()
    item_id_s = (item_id or "").strip()
    if not cart_id_s or not item_id_s:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT *
            FROM offline_cart_items
            WHERE cart_id = ? AND item_id = ?
            """,
            (cart_id_s, item_id_s),
        ).fetchone()
        if not row:
            return get_cart(cart_id_s)
        qty = _parse_float(quantity if quantity is not None else row["quantity"])
        price = _parse_float(unit_price if unit_price is not None else row["unit_price"])
        disc = max(0.0, _parse_float(discount_total if discount_total is not None else row["discount_total"]))
        if qty <= 0:
            conn.execute(
                "DELETE FROM offline_cart_items WHERE item_id = ?",
                (item_id_s,),
            )
        else:
            line_total = max(0.0, qty * price - disc)
            conn.execute(
                """
                UPDATE offline_cart_items
                SET quantity = ?, unit_price = ?, discount_total = ?, line_total = ?, updated_at = ?
                WHERE item_id = ?
                """,
                (_quantity(qty), _money(price), _money(disc), _money(line_total), _now(), item_id_s),
            )
        return _recalc_cart(conn, cart_id_s)
    finally:
        conn.close()


def delete_item(cart_id: str, item_id: str) -> dict[str, Any] | None:
    cart_id_s = (cart_id or "").strip()
    item_id_s = (item_id or "").strip()
    if not cart_id_s or not item_id_s:
        return None
    conn = _connect()
    try:
        conn.execute(
            "DELETE FROM offline_cart_items WHERE cart_id = ? AND item_id = ?",
            (cart_id_s, item_id_s),
        )
        return _recalc_cart(conn, cart_id_s)
    finally:
        conn.close()


def set_order_discount(
    cart_id: str,
    *,
    order_discount_percent: str | None,
    order_discount_total: str | None,
) -> dict[str, Any] | None:
    cart_id_s = (cart_id or "").strip()
    if not cart_id_s:
        return None
    conn = _connect()
    try:
        subtotal_row = conn.execute(
            "SELECT subtotal FROM offline_cart WHERE cart_id = ?",
            (cart_id_s,),
        ).fetchone()
        if not subtotal_row:
            return None
        subtotal = _parse_float(subtotal_row["subtotal"])
        pct = str(order_discount_percent).strip() if order_discount_percent is not None and str(order_discount_percent).strip() else None
        if pct is not None:
            try:
                pct_f = max(0.0, float(pct))
            except (TypeError, ValueError):
                pct_f = 0.0
            disc_total = subtotal * pct_f / 100.0
            disc_pct = pct
        else:
            disc_total = max(0.0, _parse_float(order_discount_total))
            disc_pct = None
        conn.execute(
            """
            UPDATE offline_cart
            SET order_discount_percent = ?, order_discount_total = ?, updated_at = ?
            WHERE cart_id = ?
            """,
            (disc_pct, _money(disc_total), _now(), cart_id_s),
        )
        return _recalc_cart(conn, cart_id_s)
    finally:
        conn.close()


def clear_order_discount(cart_id: str) -> dict[str, Any] | None:
    return set_order_discount(
        cart_id,
        order_discount_percent=None,
        order_discount_total="0.00",
    )


def checkout_cart(
    *,
    cart_id: str,
    payment_method: str,
    cash_received: str | None,
    change_amount: str | None,
) -> dict[str, Any] | None:
    cart = get_cart(cart_id)
    if not cart:
        return None
    sale_local_id = f"offline-sale-{uuid.uuid4()}"
    sale_payload = {
        "cart": cart,
        "payment_method": payment_method,
        "cash_received": cash_received,
        "change_amount": change_amount,
        "created_at": _now(),
    }
    ts = _now()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO offline_sales
            (sale_local_id, cart_id, email, branch_id, company, cashier, cashbox_id, shift_id,
             payment_method, cash_received, change_amount, subtotal, discount_total, total,
             order_discount_percent, order_discount_total, status, sale_payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_sync', ?, ?)
            """,
            (
                sale_local_id,
                cart["id"],
                cart.get("email"),
                cart.get("branch_id"),
                cart.get("company"),
                cart.get("cashier"),
                cart.get("cashbox_id"),
                cart.get("shift_id"),
                payment_method,
                cash_received,
                change_amount,
                _money(cart.get("subtotal")),
                _money(cart.get("discount_total")),
                _money(cart.get("total")),
                cart.get("order_discount_percent"),
                _money(cart.get("order_discount_total")),
                _json_dumps(sale_payload),
                ts,
            ),
        )
        conn.execute(
            """
            INSERT INTO sync_queue
            (sale_local_id, payload_json, status, attempts, last_error, next_retry_at, created_at, updated_at)
            VALUES (?, ?, 'pending', 0, NULL, 0, ?, ?)
            ON CONFLICT(sale_local_id) DO UPDATE SET
                payload_json = excluded.payload_json,
                status = 'pending',
                updated_at = excluded.updated_at,
                next_retry_at = 0,
                last_error = NULL
            """,
            (sale_local_id, _json_dumps(sale_payload), ts, ts),
        )
        conn.execute(
            """
            UPDATE offline_cart
            SET status = 'checked_out', sale_local_id = ?, checked_out_at = ?, updated_at = ?
            WHERE cart_id = ?
            """,
            (sale_local_id, ts, ts, cart_id),
        )
        return get_sale(sale_local_id)
    finally:
        conn.close()


def get_sale(sale_local_id: str) -> dict[str, Any] | None:
    sale_id_s = (sale_local_id or "").strip()
    if not sale_id_s:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM offline_sales WHERE sale_local_id = ?",
            (sale_id_s,),
        ).fetchone()
        if not row:
            return None
        payload = _json_loads(row["sale_payload"], {})
        return {
            "sale_local_id": str(row["sale_local_id"]),
            "cart_id": str(row["cart_id"]),
            "email": row["email"],
            "branch_id": row["branch_id"],
            "company": row["company"],
            "cashier": row["cashier"],
            "cashbox_id": row["cashbox_id"],
            "shift_id": row["shift_id"],
            "payment_method": row["payment_method"],
            "cash_received": row["cash_received"],
            "change_amount": row["change_amount"],
            "subtotal": _money(row["subtotal"]),
            "discount_total": _money(row["discount_total"]),
            "total": _money(row["total"]),
            "order_discount_percent": row["order_discount_percent"],
            "order_discount_total": _money(row["order_discount_total"]),
            "status": row["status"],
            "server_sale_id": row["server_sale_id"],
            "last_error": row["last_error"],
            "created_at": float(row["created_at"] or 0),
            "synced_at": float(row["synced_at"] or 0) if row["synced_at"] is not None else None,
            "payload": payload,
        }
    finally:
        conn.close()


def get_pending_sync_sales(limit: int = 20) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT sale_local_id
            FROM sync_queue
            WHERE status IN ('pending', 'failed') AND next_retry_at <= ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (_now(), max(1, int(limit))),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            sale = get_sale(str(row["sale_local_id"]))
            if sale:
                out.append(sale)
        return out
    finally:
        conn.close()


def mark_sync_processing(sale_local_id: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            UPDATE sync_queue
            SET status = 'processing', attempts = attempts + 1, updated_at = ?
            WHERE sale_local_id = ?
            """,
            (_now(), sale_local_id),
        )
        conn.execute(
            """
            UPDATE offline_sales
            SET status = 'syncing', last_error = NULL
            WHERE sale_local_id = ?
            """,
            (sale_local_id,),
        )
    finally:
        conn.close()


def mark_sync_success(sale_local_id: str, server_sale_id: Any) -> None:
    ts = _now()
    conn = _connect()
    try:
        conn.execute(
            """
            UPDATE sync_queue
            SET status = 'synced', last_error = NULL, next_retry_at = 0, updated_at = ?
            WHERE sale_local_id = ?
            """,
            (ts, sale_local_id),
        )
        conn.execute(
            """
            UPDATE offline_sales
            SET status = 'synced', server_sale_id = ?, last_error = NULL, synced_at = ?
            WHERE sale_local_id = ?
            """,
            (
                str(server_sale_id).strip() if server_sale_id is not None and str(server_sale_id).strip() else None,
                ts,
                sale_local_id,
            ),
        )
    finally:
        conn.close()


def mark_sync_failed(sale_local_id: str, error_text: str, retry_delay_sec: float = 45.0) -> None:
    ts = _now()
    msg = (error_text or "").strip()[:1000]
    conn = _connect()
    try:
        conn.execute(
            """
            UPDATE sync_queue
            SET status = 'failed', last_error = ?, next_retry_at = ?, updated_at = ?
            WHERE sale_local_id = ?
            """,
            (msg, ts + max(5.0, float(retry_delay_sec)), ts, sale_local_id),
        )
        conn.execute(
            """
            UPDATE offline_sales
            SET status = 'sync_failed', last_error = ?
            WHERE sale_local_id = ?
            """,
            (msg, sale_local_id),
        )
    finally:
        conn.close()


def retry_failed_sync_queue(*, reset_attempts: bool = True) -> int:
    """
    Вернуть в очередь все продажи со статусом failed (ручной «Повторить»).
    Возвращает число затронутых записей.
    """
    ts = _now()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT sale_local_id FROM sync_queue WHERE status = 'failed'"
        ).fetchall()
        ids = [str(r["sale_local_id"]) for r in rows if r and r["sale_local_id"]]
        if not ids:
            return 0
        ph = ",".join("?" * len(ids))
        if reset_attempts:
            conn.execute(
                f"""
                UPDATE sync_queue
                SET status = 'pending',
                    next_retry_at = 0,
                    attempts = 0,
                    last_error = NULL,
                    updated_at = ?
                WHERE sale_local_id IN ({ph})
                """,
                (ts, *ids),
            )
        else:
            conn.execute(
                f"""
                UPDATE sync_queue
                SET status = 'pending', next_retry_at = 0, updated_at = ?
                WHERE sale_local_id IN ({ph})
                """,
                (ts, *ids),
            )
        conn.execute(
            f"""
            UPDATE offline_sales
            SET status = 'pending_sync', last_error = NULL
            WHERE sale_local_id IN ({ph})
            """,
            tuple(ids),
        )
        return len(ids)
    finally:
        conn.close()


def discard_offline_sale_sync(sale_local_id: str) -> bool:
    """
    Убрать офлайн-продажу с этой кассы: удаляется offline_sales, sync_queue чистится по CASCADE.
    Использовать, если продажа уже внесена вручную в CRM или запись лишняя.
    """
    sale_id_s = (sale_local_id or "").strip()
    if not sale_id_s:
        return False
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM offline_sales WHERE sale_local_id = ?",
            (sale_id_s,),
        )
        return cur.rowcount > 0
    finally:
        conn.close()


def get_sync_stats() -> dict[str, Any]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS c
            FROM sync_queue
            GROUP BY status
            """
        ).fetchall()
        counts = {str(row["status"]): int(row["c"]) for row in rows}
        pending = counts.get("pending", 0)
        failed = counts.get("failed", 0)
        syncing = counts.get("processing", 0)
        synced = counts.get("synced", 0)
        return {
            "pending": pending,
            "failed": failed,
            "syncing": syncing,
            "synced": synced,
            "total": pending + failed + syncing + synced,
        }
    finally:
        conn.close()


def list_sync_queue(limit: int = 25) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT q.sale_local_id, q.status, q.attempts, q.last_error, q.updated_at,
                   s.total, s.payment_method, s.created_at, s.server_sale_id
            FROM sync_queue q
            JOIN offline_sales s ON s.sale_local_id = q.sale_local_id
            ORDER BY s.created_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        return [
            {
                "sale_local_id": str(row["sale_local_id"]),
                "status": str(row["status"]),
                "attempts": int(row["attempts"] or 0),
                "last_error": row["last_error"],
                "updated_at": float(row["updated_at"] or 0),
                "created_at": float(row["created_at"] or 0),
                "total": _money(row["total"]),
                "payment_method": row["payment_method"],
                "server_sale_id": row["server_sale_id"],
            }
            for row in rows
        ]
    finally:
        conn.close()


def create_offline_shift(email: str, branch_id: str | None, cashbox_id: str | None) -> str:
    shift_id = f"offline-shift-{uuid.uuid4()}"
    update_offline_session_state(
        email,
        branch_id=branch_id,
        cashbox_id=cashbox_id,
        shift_id=shift_id,
        shift_open=True,
    )
    return shift_id
