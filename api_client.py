from __future__ import annotations

import json
import threading
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from config import API_BASE_URL


class ApiError(Exception):
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


# После входа с другого устройства refresh часто отклоняется — показываем одно понятное сообщение.
_AUTH_INVALID_HINT_RU = (
    "Сессия недействительна (часто из‑за входа с другого ПК или телефона). "
    "Нажмите «Выйти» в кассе и войдите снова."
)


def _parse_error(resp: requests.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            if "detail" in data:
                d = data["detail"]
                if isinstance(d, str):
                    return d
                if isinstance(d, list):
                    return "; ".join(str(x) for x in d)
            parts = []
            for k, v in data.items():
                if isinstance(v, list):
                    parts.append(f"{k}: {', '.join(str(x) for x in v)}")
                else:
                    parts.append(f"{k}: {v}")
            return "; ".join(parts) if parts else resp.text or str(resp.status_code)
    except (json.JSONDecodeError, ValueError):
        pass
    raw = (resp.text or "").strip()
    low = raw[:500].lower()
    if raw.startswith("<!") or "<html" in low:
        return f"HTTP {resp.status_code}: адрес API не найден или неверный путь (ожидался JSON)."
    return raw or f"HTTP {resp.status_code}"


def unwrap_list(data: Any) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "results" in data:
        r = data["results"]
        return r if isinstance(r, list) else []
    return []


class JwtClient:
    @staticmethod
    def _configure_session(s: requests.Session) -> None:
        # Пул соединений: серия запросов (скан + обновление корзины) не открывает TCP заново на каждый вызов.
        adapter = HTTPAdapter(pool_connections=12, pool_maxsize=12, max_retries=0)
        s.mount("https://", adapter)
        s.mount("http://", adapter)

    def __init__(self, base_url: str = API_BASE_URL):
        self.base_url = base_url
        self.access: str | None = None
        self.refresh: str | None = None
        self.user_payload: dict[str, Any] = {}
        self.active_branch_id: str | None = None
        self._session = requests.Session()
        self._configure_session(self._session)
        # Session не потокобезопасен; запросы из asyncio.to_thread обязаны сериализоваться.
        self._http_lock = threading.RLock()

    def set_tokens(self, access: str, refresh: str | None, user_payload: dict[str, Any] | None = None):
        self.access = access
        self.refresh = refresh
        if user_payload:
            self.user_payload = user_payload

    def sync_branch_from_user(self):
        u = self.user_payload
        bid = u.get("primary_branch_id")
        if not bid and u.get("branch_ids"):
            ids = u["branch_ids"]
            if isinstance(ids, list) and ids:
                bid = ids[0]
        self.active_branch_id = bid

    def branch_params(self) -> dict[str, str]:
        if self.active_branch_id:
            return {"branch": str(self.active_branch_id)}
        return {}

    def clear(self):
        with self._http_lock:
            self.access = None
            self.refresh = None
            self.user_payload = {}
            self.active_branch_id = None
            self._session.close()
            self._session = requests.Session()
            self._configure_session(self._session)

    def login(self, email: str, password: str) -> dict[str, Any]:
        url = f"{self.base_url}/api/users/auth/login/"
        with self._http_lock:
            r = self._session.post(
                url, json={"email": email.strip(), "password": password}, timeout=30
            )
            if r.status_code != 200:
                raise ApiError(
                    _parse_error(r), status_code=r.status_code, payload=self._safe_json(r)
                )
            data = r.json()
            self.set_tokens(
                data.get("access"),
                data.get("refresh"),
                {k: v for k, v in data.items() if k not in ("access", "refresh")},
            )
            self.sync_branch_from_user()
            return data

    def refresh_access(self) -> bool:
        with self._http_lock:
            if not self.refresh:
                return False
            url = f"{self.base_url}/api/users/auth/refresh/"
            r = self._session.post(url, json={"refresh": self.refresh}, timeout=30)
            if r.status_code != 200:
                return False
            data = r.json()
            if "access" in data:
                self.access = data["access"]
            return True

    def get_profile(self) -> dict[str, Any]:
        return self._request("GET", "/api/users/profile/")

    def _safe_json(self, resp: requests.Response) -> Any:
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            return None

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict[str, Any] | None = None,
        retry_refresh: bool = True,
        timeout: float | tuple[float, float] | None = None,
    ) -> Any:
        if not self.access:
            raise ApiError(_AUTH_INVALID_HINT_RU, status_code=401)
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self.access}"}
        merged_params = {**self.branch_params(), **(params or {})}
        # Раздельный connect/read: быстрее отказ при «мёртвой» сети, без урезания времени чтения ответа.
        to: float | tuple[float, float] = (6.0, 55.0) if timeout is None else timeout
        with self._http_lock:
            r = self._session.request(
                method,
                url,
                json=json_body,
                headers=headers,
                params=merged_params or None,
                timeout=to,
            )
            if r.status_code == 401 and retry_refresh and self.refresh:
                if self.refresh_access():
                    return self._request(
                        method, path, json_body=json_body, params=params, retry_refresh=False
                    )
                self.clear()
            elif r.status_code == 401 and not self.refresh:
                self.clear()
            if r.status_code >= 400:
                msg = _parse_error(r)
                if r.status_code == 401:
                    msg = _AUTH_INVALID_HINT_RU
                raise ApiError(msg, status_code=r.status_code, payload=self._safe_json(r))
            if not r.content:
                return {}
            try:
                return r.json()
            except (json.JSONDecodeError, ValueError):
                return {}

    # --- construction: смены и кассы ---

    def construction_shifts_list(self, **params: Any) -> list:
        data = self._request("GET", "/api/construction/shifts/", params=dict(params))
        return unwrap_list(data)

    def construction_cashboxes_list(self) -> list:
        try:
            data = self._request("GET", "/api/construction/cashboxes/")
        except ApiError as e:
            if e.status_code == 404:
                return []
            raise
        return unwrap_list(data)

    def construction_shift_open(self, cashbox: str, opening_cash: str = "0.00") -> dict[str, Any]:
        """
        POST …/construction/shifts/open/ — {"cashbox", "opening_cash"}; запасной URL и cashbox_id при 404/400.
        """
        paths = ("/api/construction/shifts/open/", "/api/construction/shift/open/")
        payloads = (
            {"cashbox": cashbox, "opening_cash": opening_cash},
            {"cashbox_id": cashbox, "opening_cash": opening_cash},
        )
        last: ApiError | None = None
        for path in paths:
            for i, body in enumerate(payloads):
                try:
                    return self._request("POST", path, json_body=body)
                except ApiError as e:
                    last = e
                    if e.status_code == 404:
                        break
                    if e.status_code == 400 and i + 1 < len(payloads):
                        continue
                    raise
        if last:
            raise last
        raise ApiError("Не удалось открыть смену", status_code=404)

    def construction_shift_close(self, shift_id: str, closing_cash: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if closing_cash is not None:
            body["closing_cash"] = closing_cash
        paths = (
            f"/api/construction/shifts/{shift_id}/close/",
            f"/api/construction/shift/{shift_id}/close/",
        )
        last: ApiError | None = None
        for path in paths:
            try:
                return self._request("POST", path, json_body=body)
            except ApiError as e:
                last = e
                if e.status_code == 404:
                    continue
                raise
        if last:
            raise last
        raise ApiError("Не удалось закрыть смену", status_code=404)

    # --- main: POS ---

    def pos_sales_start(
        self,
        cashbox_id: str | None = None,
        order_discount_total: str | None = None,
        order_discount_percent: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/main/pos/sales/start/ — см. README_MARKET_POS (cashbox_id, скидки на чек)."""
        body: dict[str, Any] = {}
        if cashbox_id:
            body["cashbox_id"] = cashbox_id
        if order_discount_total is not None:
            body["order_discount_total"] = order_discount_total
        if order_discount_percent is not None:
            body["order_discount_percent"] = order_discount_percent
        return self._request("POST", "/api/main/pos/sales/start/", json_body=body)

    def pos_cart_get(self, cart_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/main/pos/carts/{cart_id}/")

    def pos_cart_patch(self, cart_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/api/main/pos/carts/{cart_id}/", json_body=body)

    def pos_scan(self, cart_id: str, barcode: str, quantity: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"barcode": barcode.strip()}
        if quantity is not None:
            body["quantity"] = quantity
        return self._request(
            "POST",
            f"/api/main/pos/sales/{cart_id}/scan/",
            json_body=body,
            # Короткий connect, умеренный read — быстрее отказ при обрыве; keep-alive в Session.
            timeout=(4, 32),
        )

    def pos_add_item(
        self,
        cart_id: str,
        product_id: str,
        quantity: str | None = None,
        unit_price: str | None = None,
        discount_total: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"product_id": product_id}
        if quantity is not None:
            body["quantity"] = quantity
        if unit_price is not None:
            body["unit_price"] = unit_price
        if discount_total is not None:
            body["discount_total"] = discount_total
        return self._request(
            "POST",
            f"/api/main/pos/sales/{cart_id}/add-item/",
            json_body=body,
            timeout=(5, 45),
        )

    def pos_cart_item_patch(self, cart_id: str, item_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/api/main/pos/carts/{cart_id}/items/{item_id}/", json_body=body)

    def pos_cart_item_delete(self, cart_id: str, item_id: str) -> None:
        self._request("DELETE", f"/api/main/pos/carts/{cart_id}/items/{item_id}/")

    def pos_checkout(self, cart_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """
        POST …/pos/sales/<cart_id>/checkout/ — payment_method, cash_received, print_receipt (README_MARKET_POS).
        При 404 — запасной путь …/pos/carts/<cart_id>/checkout/.
        При 400 без cash_received для безнала — повтор с cash_received=0.00.
        """
        paths = [
            f"/api/main/pos/sales/{cart_id}/checkout/",
            f"/api/main/pos/carts/{cart_id}/checkout/",
        ]
        last_404: ApiError | None = None
        checkout_to = (8.0, 90.0)
        for path in paths:
            try:
                return self._request("POST", path, json_body=body, timeout=checkout_to)
            except ApiError as e:
                if e.status_code == 404:
                    last_404 = e
                    continue
                if (
                    e.status_code == 400
                    and "cash_received" not in body
                    and body.get("payment_method") != "cash"
                ):
                    try:
                        retry_body = {**body, "cash_received": "0.00"}
                        return self._request("POST", path, json_body=retry_body, timeout=checkout_to)
                    except ApiError:
                        raise e
                raise e
        if last_404 is not None:
            raise last_404
        raise ApiError("Checkout: пустой список путей", status_code=500)

    def pos_cart_custom_item(
        self,
        cart_id: str,
        name: str,
        price: str,
        quantity: int | str = 1,
    ) -> dict[str, Any]:
        """POST /api/main/pos/carts/<cart_id>/custom-item/ — позиция без карточки товара (MAIN_SALE_FULL_RU §6)."""
        body: dict[str, Any] = {"name": name.strip(), "price": price, "quantity": quantity}
        return self._request("POST", f"/api/main/pos/carts/{cart_id}/custom-item/", json_body=body)

    def pos_sales_list(self, **params: Any) -> list:
        """GET /api/main/pos/sales/ — фильтры: status, paid, start, end, … (README_MARKET_POS)."""
        data = self._request("GET", "/api/main/pos/sales/", params=dict(params))
        return unwrap_list(data)

    def pos_sale_get(self, sale_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/main/pos/sales/{sale_id}/")

    def pos_sale_pay_debt(
        self,
        sale_id: str,
        payment_method: str,
        cash_received: str | None = None,
        print_receipt: bool = False,
    ) -> dict[str, Any]:
        """
        POST /api/main/pos/sales/<sale_id>/pay-debt/ — только для sale.status == debt.
        Как у checkout: всегда передаём cash_received (для безнала обычно «0.00»).
        """
        body: dict[str, Any] = {
            "payment_method": payment_method,
            "print_receipt": print_receipt,
            "cash_received": cash_received if cash_received is not None else "0.00",
        }
        return self._request("POST", f"/api/main/pos/sales/{sale_id}/pay-debt/", json_body=body)

    def pos_sale_return(self, sale_id: str) -> dict[str, Any]:
        """POST /api/main/pos/sales/<sale_id>/return/ — статусы paid или debt (MAIN_SALE_FULL_RU §5)."""
        return self._request("POST", f"/api/main/pos/sales/{sale_id}/return/", json_body={})

    def pos_sale_receipt(self, sale_id: str) -> dict[str, Any]:
        """GET /api/main/pos/sales/<sale_id>/receipt/ — данные чека для печати (JSON)."""
        return self._request("GET", f"/api/main/pos/sales/{sale_id}/receipt/")

    def analytics_market(self, **params: Any) -> dict[str, Any]:
        """GET /api/main/analytics/market/?tab=sales|stock|cashboxes|shifts — MAIN_SALE_FULL_RU §7."""
        return self._request("GET", "/api/main/analytics/market/", params=dict(params))

    def pos_product_by_barcode(self, barcode: str) -> dict[str, Any]:
        return self._request("GET", f"/api/main/products/barcode/{barcode}/")

    def products_search(self, query: str, limit: int = 40) -> list:
        """
        Поиск по названию. На nurcrm каталог отдаётся с /api/main/products/list/ (results, page),
        старый путь /api/main/products/ может отвечать 404 HTML.
        """
        q = (query or "").strip()
        if not q:
            return []
        candidates: tuple[tuple[str, dict[str, Any]], ...] = (
            ("/api/main/products/list/", {"search": q, "page": 1}),
            ("/api/main/products/", {"search": q}),
        )
        last_err: ApiError | None = None
        for path, params in candidates:
            try:
                data = self._request("GET", path, params=params)
                items = unwrap_list(data)
                if isinstance(items, list):
                    return items[:limit]
            except ApiError as e:
                last_err = e
                if e.status_code == 404:
                    continue
                raise
        if last_err:
            raise last_err
        return []

    def products_catalog(self, limit: int = 120, max_pages: int = 6) -> list:
        """
        Каталог товаров без поисковой строки.
        Используется для быстрого блока слева, когда нужно показать больше позиций, чем даёт точечный search.
        """
        out: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        last_err: ApiError | None = None
        candidates: tuple[str, ...] = (
            "/api/main/products/list/",
            "/api/main/products/",
        )
        for path in candidates:
            out.clear()
            seen_ids.clear()
            try:
                for page in range(1, max(1, int(max_pages)) + 1):
                    data = self._request("GET", path, params={"page": page})
                    items = unwrap_list(data)
                    if not isinstance(items, list) or not items:
                        break
                    for p in items:
                        if not isinstance(p, dict):
                            continue
                        pid = p.get("id")
                        pid_s = str(pid).strip() if pid is not None else ""
                        if not pid_s or pid_s in seen_ids:
                            continue
                        seen_ids.add(pid_s)
                        out.append(p)
                        if len(out) >= max(1, int(limit)):
                            return out[:limit]
                    if len(items) == 0:
                        break
                if out:
                    return out[:limit]
            except ApiError as e:
                last_err = e
                if e.status_code == 404:
                    continue
                raise
        if last_err:
            raise last_err
        return out[:limit]
