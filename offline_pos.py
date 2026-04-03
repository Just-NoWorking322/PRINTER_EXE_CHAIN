from __future__ import annotations

import requests
from typing import Any

from api_client import ApiError, JwtClient
import config
import local_products_cache
import offline_store


class OfflinePosError(Exception):
    pass


class OfflineWeightRequired(OfflinePosError):
    def __init__(self, product: dict[str, Any]):
        super().__init__("Для весового товара укажите вес.")
        self.product = product


def _first_nonempty_str(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _parse_float(value: Any) -> float:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _money(value: Any) -> str:
    return f"{_parse_float(value):.2f}"


def _user_email(user_payload: dict[str, Any]) -> str:
    return _first_nonempty_str(user_payload.get("email")).lower()


def _company_name(user_payload: dict[str, Any]) -> str:
    return _first_nonempty_str(user_payload.get("company"), "—")


def _cashier_name(user_payload: dict[str, Any]) -> str:
    name = _first_nonempty_str(
        f"{user_payload.get('first_name', '')} {user_payload.get('last_name', '')}".strip(),
        user_payload.get("email"),
        "—",
    )
    return name


def _ensure_product_has_price(product: dict[str, Any]) -> dict[str, Any]:
    out = dict(product)
    out["price"] = _money(out.get("price"))
    return out


def ensure_shift_for_session(
    *,
    user_payload: dict[str, Any],
    branch_id: str | None,
    cashbox_id: str | None,
) -> str:
    email = _user_email(user_payload)
    session = offline_store.load_offline_session(email)
    if session and session.get("shift_open") and session.get("shift_id"):
        return str(session["shift_id"])
    return offline_store.create_offline_shift(email, branch_id, cashbox_id)


def start_sale(
    *,
    user_payload: dict[str, Any],
    branch_id: str | None,
    cashbox_id: str | None,
) -> dict[str, Any]:
    email = _user_email(user_payload)
    if not email:
        raise OfflinePosError("Нет сохранённой офлайн-сессии для кассира.")
    shift_id = ensure_shift_for_session(
        user_payload=user_payload,
        branch_id=branch_id,
        cashbox_id=cashbox_id,
    )
    return offline_store.ensure_active_cart(
        email=email,
        branch_id=branch_id,
        company=_company_name(user_payload),
        cashier=_cashier_name(user_payload),
        cashbox_id=cashbox_id,
        shift_id=shift_id,
    )


def current_cart(
    *,
    user_payload: dict[str, Any],
    branch_id: str | None,
) -> dict[str, Any] | None:
    email = _user_email(user_payload)
    if not email:
        return None
    return offline_store.get_active_cart(email, branch_id)


def scan_barcode(
    *,
    user_payload: dict[str, Any],
    branch_id: str | None,
    cashbox_id: str | None,
    barcode: str,
) -> dict[str, Any]:
    cart = start_sale(user_payload=user_payload, branch_id=branch_id, cashbox_id=cashbox_id)
    product = local_products_cache.get_cached_product_by_barcode(branch_id, barcode)
    if not product:
        raise OfflinePosError(
            "Товар не найден в локальной базе. Подключите интернет и обновите каталог."
        )
    product = _ensure_product_has_price(product)
    if bool(product.get("is_weight")):
        raise OfflineWeightRequired(product)
    updated = offline_store.add_or_merge_item(
        cart_id=str(cart["id"]),
        product=product,
        quantity="1",
        unit_price=str(product.get("price") or "0.00"),
        barcode=barcode,
    )
    if not updated:
        raise OfflinePosError("Не удалось добавить товар в локальную корзину.")
    return updated


def add_product_by_id(
    *,
    user_payload: dict[str, Any],
    branch_id: str | None,
    cashbox_id: str | None,
    product_id: str,
    quantity: str | None = None,
) -> dict[str, Any]:
    cart = start_sale(user_payload=user_payload, branch_id=branch_id, cashbox_id=cashbox_id)
    product = local_products_cache.get_cached_product_by_id(branch_id, product_id)
    if not product:
        raise OfflinePosError(
            "Товар не найден в локальном каталоге. Подключите интернет и обновите каталог."
        )
    product = _ensure_product_has_price(product)
    if bool(product.get("is_weight")) and not str(quantity or "").strip():
        raise OfflineWeightRequired(product)
    qty = str(quantity).strip() if quantity is not None and str(quantity).strip() else "1"
    updated = offline_store.add_or_merge_item(
        cart_id=str(cart["id"]),
        product=product,
        quantity=qty,
        unit_price=str(product.get("price") or "0.00"),
        barcode=product.get("barcode"),
    )
    if not updated:
        raise OfflinePosError("Не удалось добавить товар в локальную корзину.")
    return updated


def adopt_online_cart(
    *,
    user_payload: dict[str, Any],
    branch_id: str | None,
    cashbox_id: str | None,
    cart: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(cart, dict):
        return start_sale(user_payload=user_payload, branch_id=branch_id, cashbox_id=cashbox_id)
    local_cart = start_sale(user_payload=user_payload, branch_id=branch_id, cashbox_id=cashbox_id)
    if (local_cart.get("items") or local_cart.get("cart_items") or []):
        return local_cart
    for item in cart.get("items") or cart.get("cart_items") or []:
        if not isinstance(item, dict):
            continue
        product = item.get("product")
        if not isinstance(product, dict):
            product = item.get("product_snapshot")
        if not isinstance(product, dict):
            product = {
                "id": item.get("product_id"),
                "name": item.get("product_name") or item.get("name"),
                "price": item.get("unit_price"),
                "unit": item.get("unit"),
                "barcode": item.get("barcode"),
                "is_weight": bool(item.get("is_weight")),
            }
        product = _ensure_product_has_price(product)
        if item.get("barcode"):
            product["barcode"] = item.get("barcode")
        local_cart = offline_store.add_or_merge_item(
            cart_id=str(local_cart["id"]),
            product=product,
            quantity=str(item.get("quantity") or "1"),
            unit_price=str(item.get("unit_price") or product.get("price") or "0.00"),
            discount_total=str(item.get("discount_total") or "0.00"),
            barcode=str(item.get("barcode") or product.get("barcode") or "").strip() or None,
        ) or local_cart
    order_discount_percent = cart.get("order_discount_percent")
    order_discount_total = cart.get("order_discount_total")
    if (
        order_discount_percent is not None and str(order_discount_percent).strip()
    ) or _parse_float(order_discount_total) > 0:
        local_cart = offline_store.set_order_discount(
            str(local_cart["id"]),
            order_discount_percent=str(order_discount_percent).strip()
            if order_discount_percent is not None and str(order_discount_percent).strip()
            else None,
            order_discount_total=str(order_discount_total or "0.00"),
        ) or local_cart
    return local_cart


def update_item(
    *,
    cart_id: str,
    item_id: str,
    quantity: str | None = None,
    unit_price: str | None = None,
    discount_total: str | None = None,
) -> dict[str, Any]:
    cart = offline_store.update_item(
        cart_id=cart_id,
        item_id=item_id,
        quantity=quantity,
        unit_price=unit_price,
        discount_total=discount_total,
    )
    if not cart:
        raise OfflinePosError("Не удалось обновить позицию локально.")
    return cart


def delete_item(*, cart_id: str, item_id: str) -> dict[str, Any]:
    cart = offline_store.delete_item(cart_id, item_id)
    if not cart:
        raise OfflinePosError("Не удалось удалить позицию локально.")
    return cart


def apply_order_discount(
    *,
    cart_id: str,
    order_discount_percent: str | None = None,
    order_discount_total: str | None = None,
) -> dict[str, Any]:
    cart = offline_store.set_order_discount(
        cart_id,
        order_discount_percent=order_discount_percent,
        order_discount_total=order_discount_total,
    )
    if not cart:
        raise OfflinePosError("Не удалось применить скидку локально.")
    return cart


def clear_order_discount(*, cart_id: str) -> dict[str, Any]:
    cart = offline_store.clear_order_discount(cart_id)
    if not cart:
        raise OfflinePosError("Не удалось сбросить скидку локально.")
    return cart


def close_shift(*, user_payload: dict[str, Any], branch_id: str | None) -> None:
    email = _user_email(user_payload)
    if not email:
        return
    offline_store.update_offline_session_state(
        email,
        branch_id=branch_id,
        shift_id=None,
        shift_open=False,
    )


def checkout(
    *,
    cart_id: str,
    payment_method: str,
    cash_received: str | None,
) -> dict[str, Any]:
    cart = offline_store.get_cart(cart_id)
    if not cart:
        raise OfflinePosError("Локальная корзина не найдена.")
    total = _parse_float(cart.get("total"))
    cash_value = _parse_float(cash_received) if payment_method == "cash" else 0.0
    if payment_method == "cash" and cash_value < total:
        raise OfflinePosError("Получено наличными меньше суммы чека.")
    change = cash_value - total if payment_method == "cash" else 0.0
    sale = offline_store.checkout_cart(
        cart_id=cart_id,
        payment_method=payment_method,
        cash_received=_money(cash_value) if payment_method == "cash" else "0.00",
        change_amount=_money(change),
    )
    if not sale:
        raise OfflinePosError("Не удалось завершить офлайн-продажу.")
    return sale


def search_products(branch_id: str | None, query: str, limit: int = 40) -> list[dict[str, Any]]:
    return local_products_cache.search_cached_products(branch_id, query, limit=limit)


def _stock_shortage_api_error(ex: ApiError) -> bool:
    if ex.status_code != 400:
        return False
    m = str(ex).lower()
    return any(
        x in m
        for x in (
            "остат",
            "пачек",
            "недостаточно",
            "условных",
        )
    )


def _sync_item_display_name(item: dict) -> str:
    prod = item.get("product") if isinstance(item.get("product"), dict) else {}
    name = _first_nonempty_str(
        item.get("product_name"),
        item.get("name"),
        prod.get("name"),
    )
    if name:
        return name
    pid = str(item.get("product_id") or "").strip()
    return f"Товар {pid[:8]}…" if pid else "Позиция чека"


def _sync_item_custom_line_params(item: dict) -> tuple[str, str, int]:
    """Произвольная строка: API принимает только целое quantity — для веса/скидок берём сумму строки."""
    name = _sync_item_display_name(item)
    qty_f = _parse_float(item.get("quantity") or "1")
    disc = _parse_float(item.get("discount_total") or 0)
    unit = _parse_float(item.get("unit_price") or 0)
    line_total = _parse_float(item.get("line_total") or 0)
    if line_total <= 0 and qty_f > 0:
        line_total = max(0.0, qty_f * unit - disc)
    frac_qty = qty_f > 0 and (qty_f < 1.0 - 1e-9 or abs(qty_f - round(qty_f)) > 1e-6)
    if disc > 1e-6 or frac_qty:
        return name, _money(line_total), 1
    if qty_f >= 1.0 and abs(qty_f - int(qty_f)) < 1e-6:
        return name, _money(unit), max(1, int(qty_f))
    return name, _money(line_total), 1


def _sync_push_cart_line(client: JwtClient, cart_id: str, item: dict) -> None:
    product_id = str(item.get("product_id") or "").strip()
    if not product_id:
        raise OfflinePosError("В офлайн-продаже отсутствует product_id.")
    try:
        client.pos_add_item(
            cart_id,
            product_id,
            str(item.get("quantity") or "1"),
            str(item.get("unit_price") or "0.00"),
            str(item.get("discount_total") or "0.00"),
        )
    except ApiError as ex:
        if not config.POS_SYNC_FALLBACK_CUSTOM_ON_STOCK_ERROR:
            raise
        if not _stock_shortage_api_error(ex):
            raise
        nm, price, qty = _sync_item_custom_line_params(item)
        client.pos_cart_custom_item(cart_id, nm, price, qty)


def sync_pending_sales(client: JwtClient, *, limit: int = 10) -> dict[str, Any]:
    sales = offline_store.get_pending_sync_sales(limit=limit)
    synced = 0
    failed = 0
    for sale in sales:
        sale_local_id = str(sale["sale_local_id"])
        try:
            offline_store.mark_sync_processing(sale_local_id)
            payload = sale.get("payload") or {}
            cart = payload.get("cart") if isinstance(payload, dict) else {}
            if not isinstance(cart, dict):
                raise OfflinePosError("Повреждён payload офлайн-продажи.")
            cashbox_id = sale.get("cashbox_id")
            fresh = client.pos_sales_start(cashbox_id=str(cashbox_id) if cashbox_id else None)
            cart_id = str(fresh.get("id") or "").strip()
            if not cart_id:
                raise OfflinePosError("Сервер не вернул id новой корзины.")
            for item in cart.get("items") or cart.get("cart_items") or []:
                if not isinstance(item, dict):
                    continue
                _sync_push_cart_line(client, cart_id, item)
            order_discount_percent = cart.get("order_discount_percent")
            order_discount_total = cart.get("order_discount_total")
            patch_body: dict[str, Any] = {}
            if order_discount_percent is not None and str(order_discount_percent).strip():
                patch_body["order_discount_percent"] = str(order_discount_percent).strip()
            elif order_discount_total is not None and _parse_float(order_discount_total) > 0:
                patch_body["order_discount_total"] = _money(order_discount_total)
            if patch_body:
                client.pos_cart_patch(cart_id, patch_body)
            res = client.pos_checkout(
                cart_id,
                {
                    "payment_method": sale.get("payment_method") or "cash",
                    "cash_received": _money(sale.get("cash_received") or "0.00"),
                    "print_receipt": False,
                },
            )
            server_sale_id = (
                res.get("sale_id")
                or res.get("order_id")
                or (res.get("sale") or {}).get("id")
                if isinstance(res, dict)
                else None
            )
            offline_store.mark_sync_success(sale_local_id, server_sale_id)
            synced += 1
        except requests.exceptions.RequestException:
            offline_store.mark_sync_failed(sale_local_id, "Нет сети для синхронизации.", retry_delay_sec=30.0)
            failed += 1
            break
        except (ApiError, OfflinePosError) as ex:
            offline_store.mark_sync_failed(sale_local_id, str(ex), retry_delay_sec=90.0)
            failed += 1
    stats = offline_store.get_sync_stats()
    stats.update({"synced_now": synced, "failed_now": failed})
    return stats
