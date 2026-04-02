"""
Десктоп-касса на Flet: JWT, смена (construction), POS-корзина, скан, оплата.
DESKTOP_MARKET_API_URL — базовый URL API (по умолчанию https://app.nurcrm.kg).
DESKTOP_MARKET_FULLSCREEN — 1 (по умолчанию) полноэкранный режим; 0 — окно.
DESKTOP_MARKET_SCALE_ENABLED — 0 отключает блок весов.
DESKTOP_MARKET_RECEIPT_USE_API_TEXT — 1: печатать сырой текст чека с API; иначе — оформленный локальный макет.
DESKTOP_MARKET_RECEIPT_COERCE_CP866_TABLE — 1: при CP866 принудительно ESC t 17 вместо 46 (по умолчанию выкл.).
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from typing import Any


def _patch_escpos_for_pyinstaller() -> None:
    """PyInstaller onefile: положить capabilities.json в escpos/ и указать путь до импорта escpos."""
    if not getattr(sys, "frozen", False):
        return
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return
    cap = os.path.join(base, "escpos", "capabilities.json")
    if os.path.isfile(cap):
        os.environ["ESCPOS_CAPABILITIES_FILE"] = os.path.abspath(cap)


_patch_escpos_for_pyinstaller()

import flet as ft
import requests

from api_client import ApiError, JwtClient
from config import API_BASE_URL, TEST_LOGIN_EMAIL, TEST_LOGIN_PASSWORD
import app_database
import local_products_cache
import config
import printer_config
from receipt_printer import (
    ReceiptPrinterError,
    is_receipt_printing_enabled,
    print_escpos_text_file,
    print_printer_self_check_page,
    print_receipt_text,
    print_sale_receipt,
)
from scale_manager import ScaleManager
from validators import (
    normalize_barcode_for_scan,
    normalize_decimal_string,
    parse_decimal,
    validate_cash_received,
    validate_email,
    validate_line_discount,
    validate_order_discount_sum,
    validate_password,
    validate_percent_discount,
    validate_product_id,
    validate_quantity,
    validate_search_query,
    validate_unit_price,
    validate_cashbox_id,
)


def install_windows_autostart_shortcut() -> tuple[bool, str]:
    """Создаёт ярлык NurMarketKassa.lnk в папке автозагрузки текущего пользователя Windows."""
    if sys.platform != "win32":
        return False, "Доступно только в Windows."
    appdata = (os.environ.get("APPDATA") or "").strip()
    if not appdata:
        return False, "Не задана переменная APPDATA."
    lnk_path = os.path.join(
        appdata,
        "Microsoft",
        "Windows",
        "Start Menu",
        "Programs",
        "Startup",
        "NurMarketKassa.lnk",
    )
    if getattr(sys, "frozen", False):
        target = sys.executable
        arguments = ""
        workdir = os.path.dirname(target) or os.getcwd()
    else:
        target = sys.executable
        main_py = os.path.abspath(__file__)
        arguments = f'"{main_py}"'
        workdir = os.path.dirname(main_py)

    def _ps_sq(s: str) -> str:
        return s.replace("'", "''")

    ps = (
        "$WshShell = New-Object -ComObject WScript.Shell; "
        f"$sc = $WshShell.CreateShortcut('{_ps_sq(lnk_path)}'); "
        f"$sc.TargetPath = '{_ps_sq(target)}'; "
        f"$sc.Arguments = '{_ps_sq(arguments)}'; "
        f"$sc.WorkingDirectory = '{_ps_sq(workdir)}'; "
        "$sc.Save()"
    )
    try:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps,
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip() or f"код {r.returncode}"
            return False, err
        return True, lnk_path
    except Exception as ex:
        return False, str(ex)


def _money(v: Any) -> str:
    try:
        if v is None:
            return "0.00"
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return str(v) if v is not None else "0.00"


def _first_nonempty_str(*vals: Any) -> str | None:
    for v in vals:
        if v is None:
            continue
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            s = str(v)
            if s.strip():
                return s
    return None


def _name_from_product_dict(p: dict[str, Any]) -> str | None:
    if not p:
        return None
    return _first_nonempty_str(
        p.get("name"),
        p.get("title"),
        p.get("display_name"),
        p.get("name_ru"),
        p.get("title_ru"),
        p.get("full_name"),
        p.get("label"),
    )


def _item_name(it: dict[str, Any]) -> str:
    p = it.get("product")
    if isinstance(p, dict):
        n = _name_from_product_dict(p)
        if n:
            return n
    snap = it.get("product_snapshot")
    if isinstance(snap, dict):
        n = _name_from_product_dict(snap)
        if n:
            return n
    for k in (
        "product_name",
        "name",
        "title",
        "display_name",
        "label",
        "item_name",
        "description",
    ):
        v = it.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    pid = it.get("product_id")
    if pid is None and p is not None and not isinstance(p, dict):
        pid = p
    if pid is not None and str(pid).strip():
        return f"Товар #{pid}"
    return "—"


def _item_id(it: dict[str, Any]) -> str | None:
    rid = it.get("id")
    return str(rid) if rid is not None else None


def _item_line_total(it: dict[str, Any]) -> str:
    """Сумма по позиции: поля из API или qty × unit_price − скидка на строку."""
    keys = (
        "line_total",
        "line_total_amount",
        "line_amount",
        "amount",
        "total",
        "sum",
        "total_price",
        "line_total_display",
        "subtotal",
        "line_sum",
        "total_sum",
    )
    for k in keys:
        v = it.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        try:
            return _money(float(v))
        except (TypeError, ValueError):
            continue
    try:
        q = float(it.get("quantity") or 0)
        up = float(it.get("unit_price") or 0)
        disc = float(
            it.get("discount_total")
            or it.get("line_discount")
            or it.get("discount")
            or 0
        )
        if q > 0 and up >= 0:
            return _money(q * up - disc)
    except (TypeError, ValueError):
        pass
    return "0.00"


def _truthy_api_bool(v: Any) -> bool:
    if v is True:
        return True
    if isinstance(v, str) and v.strip().lower() in ("1", "true", "yes", "on"):
        return True
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v != 0:
        return True
    return False


def _unit_is_kg(unit: Any) -> bool:
    """Как на веб-кассе: единица «кг» / kg — весовой товар."""
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


def _dict_has_kg_unit(d: dict[str, Any]) -> bool:
    for k in ("unit", "unit_display", "measure_unit", "sale_unit", "uom"):
        if _unit_is_kg(d.get(k)):
            return True
    return False


def _product_must_weigh(p: Any) -> bool:
    """Взвешивание: is_wait / is_weight или unit=кг (на бэкенде часто is_weight=false при unit «кг»)."""
    if not isinstance(p, dict):
        return False
    if _truthy_api_bool(p.get("is_wait")):
        return True
    if _truthy_api_bool(p.get("is_weight")):
        return True
    if _dict_has_kg_unit(p):
        return True
    return False


def _cart_line_must_weigh(it: dict[str, Any]) -> bool:
    if _truthy_api_bool(it.get("is_wait")) or _truthy_api_bool(it.get("is_weight")):
        return True
    if _dict_has_kg_unit(it):
        return True
    prod = it.get("product")
    if isinstance(prod, dict) and _product_must_weigh(prod):
        return True
    snap = it.get("product_snapshot")
    if isinstance(snap, dict) and _product_must_weigh(snap):
        return True
    return False


def _shift_id_from_cart(cart: dict[str, Any]) -> str | None:
    if not isinstance(cart, dict):
        return None
    raw = cart.get("shift_id")
    if raw is not None and str(raw).strip():
        return str(raw)
    s = cart.get("shift")
    if isinstance(s, dict):
        x = s.get("id")
        return str(x) if x is not None else None
    if s:
        return str(s)
    return None


def _shift_id_from_open_response(data: Any) -> str | None:
    """Ответ POST …/shifts/open/: часто {id}, либо вложенный shift."""
    if not isinstance(data, dict):
        return None
    if data.get("id") is not None:
        return str(data["id"])
    sh = data.get("shift")
    if isinstance(sh, dict) and sh.get("id") is not None:
        return str(sh["id"])
    return None


def _resolve_shift_id(session: dict[str, Any], cart: dict[str, Any]) -> str | None:
    a = session.get("active_shift_id")
    if a is not None and str(a).strip():
        return str(a)
    return _shift_id_from_cart(cart)


def _is_shift_open_status(st: Any) -> bool:
    s = str(st or "").strip().lower()
    return s in ("open", "active", "opened", "in_progress")


def _row_looks_like_open_shift(row: dict[str, Any]) -> bool:
    if row.get("is_open") is True:
        return True
    return _is_shift_open_status(row.get("status") or row.get("state"))


def _pick_open_shift_id_from_list(shifts: list, cashbox_id: str | None) -> str | None:
    """Выбор открытой смены: при известной кассе — по совпадению, иначе первая «открытая»."""
    if not isinstance(shifts, list):
        return None
    candidates: list[dict[str, Any]] = []
    for row in shifts:
        if not isinstance(row, dict):
            continue
        if not _row_looks_like_open_shift(row):
            continue
        rid = row.get("id")
        if rid is None:
            continue
        candidates.append(row)
    if not candidates:
        return None
    if cashbox_id:
        for row in candidates:
            cb = row.get("cashbox")
            if isinstance(cb, dict):
                cb = cb.get("id")
            elif cb is None:
                cb = row.get("cashbox_id")
            if cb is not None and str(cb) == str(cashbox_id):
                return str(row["id"])
    return str(candidates[0]["id"])


def _cashbox_id_from_dict(c: dict[str, Any]) -> str | None:
    cid = c.get("id") or c.get("pk") or c.get("uuid")
    if cid is None:
        return None
    s = str(cid).strip()
    return s or None


def _first_cashbox_id_from_list(cashboxes: list) -> str | None:
    for c in cashboxes:
        if not isinstance(c, dict):
            continue
        sid = _cashbox_id_from_dict(c)
        if sid:
            return sid
    return None


def _cart_total_due(cart: dict[str, Any]) -> float:
    """Итог к оплате: API может отдавать total под разными именами или в totals."""
    keys = (
        "total",
        "grand_total",
        "total_amount",
        "amount_due",
        "payable_total",
        "order_total",
        "total_to_pay",
        "amount_total",
    )
    for src in (cart, cart.get("totals") if isinstance(cart.get("totals"), dict) else None):
        if not isinstance(src, dict):
            continue
        for k in keys:
            v = src.get(k)
            if v is None or (isinstance(v, str) and not str(v).strip()):
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _checkout_change_amount(res: dict[str, Any]) -> Any:
    """Сдача из ответа checkout (разные ключи)."""
    for k in ("change", "change_amount", "cash_change", "amount_change"):
        if k in res and res[k] is not None:
            return res[k]
    data = res.get("data")
    if isinstance(data, dict):
        for k in ("change", "change_amount", "cash_change"):
            if k in data and data[k] is not None:
                return data[k]
    return None


def _checkout_sale_id(res: dict[str, Any]) -> Any:
    for k in ("sale_id", "order_id"):
        v = res.get(k)
        if v is not None:
            return v
    s = res.get("sale")
    if isinstance(s, dict) and s.get("id") is not None:
        return s.get("id")
    return None


def _receipt_text_from_checkout_response(res: Any) -> str | None:
    """Текст чека из ответа checkout при print_receipt=true."""
    if not isinstance(res, dict):
        return None
    for k in ("receipt_text", "receipt", "text", "content"):
        v = res.get(k)
        if isinstance(v, str) and v.strip():
            return v
    for nest_key in ("data", "sale", "result"):
        nest = res.get(nest_key)
        if isinstance(nest, dict):
            for k in ("receipt_text", "receipt", "text"):
                v = nest.get(k)
                if isinstance(v, str) and v.strip():
                    return v
    return None


def _receipt_text_from_sale_receipt_api(payload: Any) -> str | None:
    """Текст из GET /api/main/pos/sales/<id>/receipt/."""
    if not isinstance(payload, dict):
        return None
    for k in ("receipt_text", "text", "body", "content", "plain"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v
    lines = payload.get("lines")
    if isinstance(lines, list) and lines:
        return "\n".join(str(x) for x in lines)
    nested = payload.get("receipt") or payload.get("data")
    if isinstance(nested, dict):
        for k in ("text", "body", "receipt_text", "plain"):
            v = nested.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return None


def _fetch_receipt_text_via_api(client: JwtClient, sale_id: Any) -> str | None:
    if sale_id is None:
        return None
    try:
        r = client.pos_sale_receipt(str(sale_id))
    except ApiError:
        return None
    return _receipt_text_from_sale_receipt_api(r)


# Сканер HID: между символами обычно < 50 ms; пауза больше порога — новый штрих (не сливаем два скана).
BARCODE_INTERKEY_RESET_MS = 85
BARCODE_BUFFER_MAX_LEN = 64
MIN_BARCODE_LEN = 4
# Поле поиска: не дергать живой поиск по длинной строке «только цифры» (сканер в фокусе поля).
BARCODE_LIKE_MIN_DIGITS = 8
LIVE_SEARCH_DEBOUNCE_SEC = 0.22

# NUR CRM: три колонки, тёмный бренд-бар сверху, акцент #f7d617
UI_BRAND = "#f7d617"
UI_BG = "#f5f5f5"
_COLUMN_CARD_SHADOW = ft.BoxShadow(
    blur_radius=15,
    spread_radius=1,
    color=ft.Colors.with_opacity(0.12, "#000000"),
    offset=ft.Offset(0, 4),
)
UI_SIDEBAR = "#111827"
UI_SIDEBAR_MUTED = "#9ca3af"
UI_SIDEBAR_TEXT = "#e5e7eb"
UI_SURFACE = "#ffffff"
UI_SURFACE_ELEV = "#f9fafb"
UI_BORDER = "#e5e7eb"
UI_ACCENT = "#f7d617"
UI_ACCENT_DIM = "#e6c80f"
UI_TEXT = "#111827"
UI_TEXT_ON_YELLOW = "#0a0a0a"
UI_MUTED = "#6b7280"
UI_ICON_BADGE_BG = "#fef9c3"
UI_WARN_BG = "#fffbeb"
UI_WARN_BORDER = "#f59e0b"
UI_WARN_TEXT = "#b45309"

VK_LAYOUT_RU = (
    ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0"),
    ("й", "ц", "у", "к", "е", "н", "г", "ш", "щ", "з", "х"),
    ("ф", "ы", "в", "а", "п", "р", "о", "л", "д", "ж", "э"),
    ("я", "ч", "с", "м", "и", "т", "ь", "б", "ю", "-", "_"),
    ("@", ".", ",", "/", "!", "?"),
)
VK_LAYOUT_EN = (
    ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0"),
    ("q", "w", "e", "r", "t", "y", "u", "i", "o", "p"),
    ("a", "s", "d", "f", "g", "h", "j", "k", "l"),
    ("z", "x", "c", "v", "b", "n", "m", "-", "_"),
    ("@", ".", "/", "!", "?"),
)
VK_LAYOUT_NUM = (
    ("1", "2", "3"),
    ("4", "5", "6"),
    ("7", "8", "9"),
    (",", "0", "."),
)
QUICK_CATALOG_LIMIT = 45
PRODUCT_GRID_COLUMNS = 3
QUICK_CATALOG_PRESETS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "kg": (
        ("Картошка", ("картошка", "картофель")),
        ("Помидоры", ("помидоры", "помидор", "томат")),
        ("Огурцы", ("огурцы", "огурец")),
        ("Лук", ("лук",)),
        ("Морковь", ("морковь",)),
        ("Капуста", ("капуста",)),
        ("Яблоки", ("яблоки", "яблоко")),
        ("Бананы", ("бананы", "банан")),
        ("Апельсины", ("апельсины", "апельсин")),
        ("Груши", ("груши", "груша")),
        ("Перец", ("перец", "болгарский перец")),
        ("Свекла", ("свекла", "свёкла")),
    ),
    "common": (
        ("Хлеб", ("хлеб", "батон")),
        ("Молоко", ("молоко",)),
        ("Вода", ("вода",)),
        ("Сахар", ("сахар",)),
        ("Масло", ("масло",)),
        ("Рис", ("рис",)),
        ("Макароны", ("макароны",)),
        ("Чай", ("чай",)),
        ("Кофе", ("кофе",)),
        ("Сок", ("сок",)),
        ("Кола", ("кола", "coca cola", "coca-cola")),
        ("Яйца", ("яйца", "яйцо")),
    ),
}
QUICK_CATALOG_PRIORITY: dict[str, tuple[tuple[str, int], ...]] = {
    "kg": (
        ("помид", 1200),
        ("карто", 1100),
        ("огур", 900),
        ("лук", 850),
        ("морков", 800),
        ("капуст", 760),
        ("яблок", 700),
        ("банан", 650),
    ),
    "common": (
        ("хлеб", 1000),
        ("молок", 980),
        ("вода", 950),
        ("сахар", 920),
        ("масло", 900),
        ("рис", 860),
        ("макарон", 830),
        ("чай", 800),
        ("коф", 780),
        ("сок", 760),
        ("кола", 740),
        ("яйц", 720),
    ),
}


def _section_heading(title: str, subtitle: str | None = None) -> ft.Column:
    """Заголовок блока с акцентной полосой."""
    head = ft.Row(
        [
            ft.Container(width=4, height=22, bgcolor=UI_ACCENT, border_radius=3),
            ft.Text(title, size=17, weight=ft.FontWeight.W_600, color=UI_TEXT),
        ],
        spacing=12,
    )
    if subtitle:
        return ft.Column(
            [head, ft.Text(subtitle, size=12, color=UI_MUTED)],
            spacing=4,
            tight=True,
        )
    return ft.Column([head], tight=True)


def _sidebar_nav_item(icon, label: str, *, active: bool = False) -> ft.Container:
    """Декоративный пункт боковой панели (без навигации — только стиль CRM)."""
    return ft.Container(
        content=ft.Row(
            [
                ft.Container(
                    width=4,
                    height=22,
                    bgcolor=UI_ACCENT if active else "transparent",
                    border_radius=2,
                ),
                ft.Icon(icon, size=20, color=UI_ACCENT if active else UI_SIDEBAR_MUTED),
                ft.Text(
                    label,
                    size=14,
                    weight=ft.FontWeight.W_600 if active else ft.FontWeight.W_400,
                    color=UI_ACCENT if active else UI_SIDEBAR_TEXT,
                ),
            ],
            spacing=10,
        ),
        padding=ft.Padding.only(left=4, top=8, bottom=8, right=8),
        bgcolor=ft.Colors.with_opacity(0.14, UI_ACCENT) if active else None,
        border_radius=8,
    )


def _key_to_barcode_char(key: str) -> str | None:
    """Символы штрихкода из события клавиатуры (Windows/macOS/web-имена клавиш)."""
    if not key:
        return None
    k = key.strip()
    if len(k) == 1:
        if k.isdigit():
            return k
        o = ord(k)
        if 0x0410 <= o <= 0x04FF:
            return None
        if 65 <= o <= 90 or 97 <= o <= 122:
            return k
        if k in "-_.":
            return k
        return None
    if k in ("Enter", "Return", "NumpadEnter", "Select"):
        return None
    if k.startswith("Digit") and len(k) >= 6:
        last = k[-1]
        if last.isdigit():
            return last
    if k.startswith("Key") and len(k) == 4:
        c = k[3]
        if c.isdigit():
            return c
        if c.isalpha() and "A" <= c.upper() <= "Z":
            return c.lower()
    if "Numpad" in k:
        tail = re.sub(r"\D", "", k)
        if tail:
            return tail[-1]
    if k in ("Minus", "NumpadSubtract"):
        return "-"
    if k in ("Period", "NumpadDecimal"):
        return "."
    if k == "Space":
        return " "
    return None


def _looks_like_barcode_query(q: str) -> bool:
    s = (q or "").strip()
    return len(s) >= BARCODE_LIKE_MIN_DIGITS and s.isdigit()


def _is_enter_key(key: str) -> bool:
    k = (key or "").strip()
    return k in ("Enter", "Return", "NumpadEnter", "Select")


def _windows_pre_ui_init() -> None:
    """
    Вызывать до ft.run: DPI awareness на Windows (чёткие шрифты при масштабе экрана ≠ 100%%).
    Опционально: winmm timeBeginPeriod(1) — чуть ровнее таймеры UI (DESKTOP_MARKET_WIN_MM_TIMER=1).
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
    except ImportError:
        return
    user32 = ctypes.windll.user32
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = (HANDLE)-4
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        shcore = getattr(ctypes.windll, "shcore", None)
        if shcore is not None:
            try:
                shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
            except Exception:
                try:
                    user32.SetProcessDPIAware()
                except Exception:
                    pass
        else:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass
    if os.environ.get("DESKTOP_MARKET_WIN_MM_TIMER", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)

            def _winmm_time_end():
                try:
                    ctypes.windll.winmm.timeEndPeriod(1)
                except Exception:
                    pass

            import atexit

            atexit.register(_winmm_time_end)
        except Exception:
            pass


def main(page: ft.Page):
    page.title = "Касса — Nur Market"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.bgcolor = UI_BG
    page.theme = ft.Theme(
        color_scheme=ft.ColorScheme(
            primary=UI_ACCENT,
            on_primary=UI_TEXT_ON_YELLOW,
            secondary=UI_ACCENT_DIM,
            on_secondary=UI_TEXT,
            surface=UI_SURFACE,
            on_surface=UI_TEXT,
            surface_container_highest=UI_SURFACE_ELEV,
            outline=UI_BORDER,
            outline_variant=UI_BORDER,
        ),
        use_material3=True,
    )
    page.window.width = 1280
    page.window.height = 840
    page.window.min_width = 960
    page.window.min_height = 640
    _fs = os.environ.get("DESKTOP_MARKET_FULLSCREEN", "1").strip().lower()
    page.window.full_screen = _fs not in ("0", "false", "no", "off")
    page.window.prevent_close = False
    page.padding = 0

    app_database.init_database()
    printer_config.load_from_disk()

    client = JwtClient()
    local_products_cache.init_db()
    session: dict[str, Any] = {
        "cart_id": None,
        "cart": {},
        "needs_shift": False,
        "pos_cashbox_id": None,
        "active_shift_id": None,
        "cashier_active": False,
        "barcode_buf": "",
        "barcode_last_ms": 0.0,
        "search_gen": 0,
        "quick_catalog_tab": "kg",
        "quick_catalog_products": {"kg": [], "common": []},
        "quick_catalog_loading": {"kg": False, "common": False},
        "quick_catalog_loaded": {"kg": False, "common": False},
    }

    error_text = ft.Ref[ft.Text]()
    loading_overlay = ft.Ref[ft.Container]()
    shift_banner = ft.Ref[ft.Container]()
    cart_items_col = ft.Ref[ft.ListView]()
    subtotal_txt = ft.Ref[ft.Text]()
    discount_txt = ft.Ref[ft.Text]()
    total_txt = ft.Ref[ft.Text]()
    search_field_ref = ft.Ref[ft.TextField]()
    search_results_ref = ft.Ref[ft.Column]()
    # Подсказки/ошибки на экране кассы (поле error_text есть только на логине — их не было видно).
    cashier_hint_ref = ft.Ref[ft.Text]()
    status_chip = ft.Ref[ft.Text]()
    order_discount_pct_ref = ft.Ref[ft.TextField]()
    order_discount_sum_ref = ft.Ref[ft.TextField]()
    weight_scale_text = ft.Ref[ft.Text]()
    weight_scale_status = ft.Ref[ft.Text]()
    scale_state: dict[str, Any] = {"mgr": None}
    virtual_keyboard_host = ft.Ref[ft.Container]()
    virtual_keyboard_body = ft.Ref[ft.Column]()
    virtual_keyboard_bindings: dict[int, dict[str, Any]] = {}
    virtual_keyboard_state: dict[str, Any] = {
        "visible": False,
        "target": None,
        "layout": "ru",
        "shift": False,
        "title": "",
        "submit_text": "Готово",
        "submit": None,
    }
    # Весы: по умолчанию включены; отключить: DESKTOP_MARKET_SCALE_ENABLED=0
    _scale_env = os.environ.get("DESKTOP_MARKET_SCALE_ENABLED", "1").strip().lower()
    scale_feature_enabled = _scale_env not in ("0", "false", "no", "off")

    def set_loading(visible: bool, *, flush: bool = True):
        if loading_overlay.current:
            loading_overlay.current.visible = visible
            if flush:
                page.update()

    def show_error(msg: str, *, flush: bool = True):
        if error_text.current:
            error_text.current.value = msg
            error_text.current.visible = bool(msg)
        if cashier_hint_ref.current:
            s = (msg or "").strip()
            cashier_hint_ref.current.value = msg or ""
            cashier_hint_ref.current.visible = bool(s)
        if flush:
            page.update()

    def snack(msg: str, color: str | None = None):
        page.snack_bar = ft.SnackBar(ft.Text(msg), bgcolor=color)
        page.snack_bar.open = True
        page.update()

    def _vk_initial_layout(mode: str) -> str:
        m = (mode or "text").strip().lower()
        if m in ("numeric", "decimal", "money", "num"):
            return "num"
        if m in ("english", "email", "password", "latin", "lpt"):
            return "en"
        return "ru"

    def _vk_display_label(token: str) -> str:
        if token == "SPACE":
            return "Пробел"
        if token == "BACKSPACE":
            return "⌫"
        if token == "CLEAR":
            return "Очистить"
        if token == "DONE":
            return str(virtual_keyboard_state.get("submit_text") or "Готово")
        if token == "SHIFT":
            return "Shift"
        if token == "LAYOUT_RU":
            return "RU"
        if token == "LAYOUT_EN":
            return "EN"
        if token == "LAYOUT_NUM":
            return "123"
        if token == "HIDE":
            return "Скрыть"
        return token

    def _vk_call_submit() -> None:
        cb = virtual_keyboard_state.get("submit")
        if not callable(cb):
            return
        try:
            cb(None)
        except TypeError:
            cb()

    def hide_virtual_keyboard(_e=None) -> None:
        virtual_keyboard_state["visible"] = False
        virtual_keyboard_state["target"] = None
        host = virtual_keyboard_host.current
        if host:
            host.visible = False
        try:
            page.update()
        except Exception:
            pass

    def dismiss_dialog(_e=None) -> None:
        hide_virtual_keyboard()
        page.pop_dialog()
        page.update()

    def _vk_insert_text(text: str) -> None:
        target = virtual_keyboard_state.get("target")
        if target is None:
            return
        try:
            cur = str(target.value or "")
            target.value = cur + text
            page.update()
        except Exception:
            hide_virtual_keyboard()

    def _vk_backspace() -> None:
        target = virtual_keyboard_state.get("target")
        if target is None:
            return
        try:
            cur = str(target.value or "")
            target.value = cur[:-1]
            page.update()
        except Exception:
            hide_virtual_keyboard()

    def _vk_clear() -> None:
        target = virtual_keyboard_state.get("target")
        if target is None:
            return
        try:
            target.value = ""
            page.update()
        except Exception:
            hide_virtual_keyboard()

    def _vk_build_button(
        token: str,
        *,
        active: bool = False,
        accent: bool = False,
        expand: int = 1,
    ) -> ft.Container:
        bg = UI_SURFACE
        fg = UI_TEXT
        border = UI_BORDER
        if accent:
            bg = UI_ACCENT
            fg = UI_TEXT_ON_YELLOW
            border = UI_ACCENT_DIM
        elif active:
            bg = UI_ICON_BADGE_BG
            fg = UI_TEXT
            border = UI_ACCENT
        btn = ft.FilledButton(
            _vk_display_label(token),
            height=28,
            style=ft.ButtonStyle(
                bgcolor=bg,
                color=fg,
                side=ft.BorderSide(1, border),
                shape=ft.RoundedRectangleBorder(radius=8),
                padding=ft.Padding.symmetric(horizontal=3, vertical=3),
                text_style=ft.TextStyle(size=10, weight=ft.FontWeight.W_500),
            ),
            on_click=lambda _e, t=token: _vk_handle_press(t),
        )
        return ft.Container(content=btn, expand=expand)

    def _vk_render() -> None:
        host = virtual_keyboard_host.current
        body = virtual_keyboard_body.current
        if not host or not body:
            return
        if not virtual_keyboard_state.get("visible"):
            host.visible = False
            page.update()
            return
        layout = str(virtual_keyboard_state.get("layout") or "ru")
        shift = bool(virtual_keyboard_state.get("shift"))
        rows_src = VK_LAYOUT_NUM if layout == "num" else (VK_LAYOUT_EN if layout == "en" else VK_LAYOUT_RU)
        rows: list[ft.Control] = []
        title = str(virtual_keyboard_state.get("title") or "Ввод")
        subtitle = "Русская раскладка" if layout == "ru" else ("English layout" if layout == "en" else "Цифры")
        toolbar = ft.Row(
            [
                ft.Column(
                    [
                        ft.Text(title, size=11, weight=ft.FontWeight.W_600, color=UI_SURFACE),
                        ft.Text(subtitle, size=8, color=UI_SIDEBAR_TEXT),
                    ],
                    spacing=1,
                    tight=True,
                ),
                ft.Container(expand=True),
                _vk_build_button("LAYOUT_RU", active=layout == "ru"),
                _vk_build_button("LAYOUT_EN", active=layout == "en"),
                _vk_build_button("LAYOUT_NUM", active=layout == "num"),
                _vk_build_button("SHIFT", active=shift and layout != "num"),
                _vk_build_button("HIDE"),
            ],
            spacing=4,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        rows.append(toolbar)
        for row in rows_src:
            controls: list[ft.Control] = []
            for token in row:
                shown = token.upper() if shift and layout != "num" and token.isalpha() else token
                controls.append(_vk_build_button(shown))
            rows.append(ft.Row(controls, spacing=4))
        if layout == "num":
            rows.append(
                ft.Row(
                    [
                        _vk_build_button("CLEAR"),
                        _vk_build_button("00"),
                        _vk_build_button("BACKSPACE"),
                        _vk_build_button("DONE", accent=True),
                    ],
                    spacing=4,
                )
            )
        else:
            rows.append(
                ft.Row(
                    [
                        _vk_build_button("CLEAR"),
                        _vk_build_button("SPACE", expand=3),
                        _vk_build_button("BACKSPACE"),
                        _vk_build_button("DONE", accent=True),
                    ],
                    spacing=4,
                )
            )
        body.controls = rows
        host.visible = True
        page.update()

    def _vk_handle_press(token: str) -> None:
        if token == "HIDE":
            hide_virtual_keyboard()
            return
        if token == "SHIFT":
            if str(virtual_keyboard_state.get("layout") or "") != "num":
                virtual_keyboard_state["shift"] = not bool(virtual_keyboard_state.get("shift"))
                _vk_render()
            return
        if token == "LAYOUT_RU":
            virtual_keyboard_state["layout"] = "ru"
            _vk_render()
            return
        if token == "LAYOUT_EN":
            virtual_keyboard_state["layout"] = "en"
            _vk_render()
            return
        if token == "LAYOUT_NUM":
            virtual_keyboard_state["layout"] = "num"
            _vk_render()
            return
        if token == "BACKSPACE":
            _vk_backspace()
            return
        if token == "CLEAR":
            _vk_clear()
            return
        if token == "SPACE":
            _vk_insert_text(" ")
            return
        if token == "DONE":
            hide_virtual_keyboard()
            _vk_call_submit()
            return
        _vk_insert_text(token)

    def show_virtual_keyboard(field: ft.TextField) -> None:
        meta = virtual_keyboard_bindings.get(id(field))
        if not meta:
            return
        virtual_keyboard_state["visible"] = True
        virtual_keyboard_state["target"] = field
        virtual_keyboard_state["layout"] = _vk_initial_layout(str(meta.get("mode") or "text"))
        virtual_keyboard_state["shift"] = False
        virtual_keyboard_state["title"] = str(meta.get("title") or getattr(field, "label", None) or "Ввод")
        virtual_keyboard_state["submit_text"] = str(meta.get("submit_text") or "Готово")
        virtual_keyboard_state["submit"] = meta.get("submit")
        _vk_render()

    def bind_virtual_keyboard(
        field: ft.TextField,
        *,
        mode: str = "text",
        title: str = "",
        submit: Any = None,
        submit_text: str = "Готово",
    ) -> ft.TextField:
        prev_focus = getattr(field, "on_focus", None)

        def _on_focus(e):
            if callable(prev_focus):
                prev_focus(e)
            show_virtual_keyboard(field)

        field.on_focus = _on_focus
        virtual_keyboard_bindings[id(field)] = {
            "mode": mode,
            "title": title,
            "submit": submit,
            "submit_text": submit_text,
        }
        return field

    page.overlay.append(
        ft.Stack(
            [
                ft.Container(
                    ref=virtual_keyboard_host,
                    visible=False,
                    left=6,
                    right=6,
                    bottom=6,
                    content=ft.Row(
                        [
                            ft.Container(
                                width=460,
                                bgcolor=UI_SIDEBAR,
                                border_radius=12,
                                padding=8,
                                border=ft.Border.all(1, "#1f2937"),
                                shadow=ft.BoxShadow(
                                    blur_radius=12,
                                    spread_radius=1,
                                    color=ft.Colors.with_opacity(0.20, "#000000"),
                                    offset=ft.Offset(0, 4),
                                ),
                                content=ft.Column(ref=virtual_keyboard_body, spacing=4, tight=True),
                            )
                        ],
                        alignment=ft.MainAxisAlignment.CENTER,
                    ),
                )
            ],
            expand=True,
        )
    )

    def on_window_event(ev: ft.WindowEvent):
        if ev.type != ft.WindowEventType.CLOSE:
            return
        if session.get("cashier_active"):
            snack(
                "Чтобы закрыть программу, выйдите из аккаунта (кнопка выхода справа).",
                ft.Colors.AMBER_700,
            )

    page.window.on_event = on_window_event

    def _cart_from_patch_response(data: Any) -> dict[str, Any] | None:
        """Если PATCH вернул полную корзину — второй GET не нужен."""
        if not isinstance(data, dict):
            return None
        nested = data.get("cart")
        if isinstance(nested, dict) and nested.get("id") is not None:
            if nested.get("items") is not None or nested.get("cart_items") is not None:
                return nested
        if data.get("id") is not None and (
            data.get("items") is not None or data.get("cart_items") is not None
        ):
            return data
        return None

    def _apply_cart_from_response(data: Any, *, flush: bool = True) -> bool:
        cart = _cart_from_patch_response(data)
        if not cart:
            return False
        session["cart"] = cart
        sid = _shift_id_from_cart(cart)
        if sid:
            session["active_shift_id"] = sid
        render_cart_items(flush=flush)
        return True

    async def _reload_cart_async(*, flush: bool = True) -> None:
        cid = session.get("cart_id")
        if not cid:
            return
        try:
            cart = await asyncio.to_thread(client.pos_cart_get, str(cid))
        except ApiError as ex:
            snack(str(ex), ft.Colors.RED_700)
            return
        session["cart"] = cart
        sid = _shift_id_from_cart(cart)
        if sid:
            session["active_shift_id"] = sid
        try:
            local_products_cache.ingest_cart(client.active_branch_id, cart)
        except Exception:
            pass
        render_cart_items(flush=flush)

    def on_print_weight_click(_):
        mgr = scale_state.get("mgr")
        if not mgr:
            snack("Модуль весов не запущен", ft.Colors.AMBER_700)
            return
        w = mgr.get_last_weight()
        lpt = (config.SCALE_LPT or "LPT1").strip() or "LPT1"
        if w is not None:
            body = f"Вес: {w:.3f} кг\n"
        else:
            raw = mgr.get_last_raw()
            body = "Тестовая печать\nВес не распознан (нет числа в строке с COM).\n"
            if raw:
                body += f"Сырое: {raw[:200]}\n"
        try:
            print_escpos_text_file(lpt, body)
            snack(f"Отправлено на {lpt} (ESC/POS как у чека)", ft.Colors.GREEN_700)
        except ReceiptPrinterError as ex:
            snack(str(ex), ft.Colors.RED_700)
        except OSError as ex:
            snack(str(ex), ft.Colors.RED_700)
        except Exception as ex:
            snack(str(ex), ft.Colors.RED_700)

    def _item_discount_display(it: dict[str, Any]) -> str:
        v = it.get("discount_total")
        if v is None:
            v = it.get("line_discount")
        try:
            if v is not None and float(v) != 0:
                return _money(v)
        except (TypeError, ValueError):
            pass
        return ""

    def sync_order_discount_fields():
        cart = session.get("cart") or {}
        p = cart.get("order_discount_percent")
        t = cart.get("order_discount_total")
        if order_discount_pct_ref.current:
            if p is None or p == "" or p == 0:
                order_discount_pct_ref.current.value = ""
            else:
                order_discount_pct_ref.current.value = str(p).strip()
        if order_discount_sum_ref.current:
            try:
                z = float(t) if t is not None and t != "" else 0.0
            except (TypeError, ValueError):
                z = 0.0
            order_discount_sum_ref.current.value = "" if z == 0 else _money(t)

    def apply_order_discount(_):
        cid = session.get("cart_id")
        if not cid:
            snack("Нет корзины", ft.Colors.AMBER_700)
            return
        pct = (order_discount_pct_ref.current.value or "").strip() if order_discount_pct_ref.current else ""
        sm = (order_discount_sum_ref.current.value or "").strip() if order_discount_sum_ref.current else ""
        if pct and sm:
            snack("Укажите только процент или только сумму скидки на чек", ft.Colors.AMBER_700)
            return
        body: dict[str, str] = {}
        if pct:
            err = validate_percent_discount(pct)
            if err:
                snack(err, ft.Colors.AMBER_700)
                return
            body["order_discount_percent"] = normalize_decimal_string(pct)
        elif sm:
            err = validate_order_discount_sum(sm)
            if err:
                snack(err, ft.Colors.AMBER_700)
                return
            body["order_discount_total"] = normalize_decimal_string(sm)
        else:
            snack("Введите % или сумму скидки на чек", ft.Colors.AMBER_700)
            return

        async def _apply_disc():
            set_loading(True)
            try:
                resp = await asyncio.to_thread(client.pos_cart_patch, cid, body)
                if not _apply_cart_from_response(resp):
                    await _reload_cart_async()
                snack("Скидка на чек обновлена", ft.Colors.GREEN_700)
            except ApiError as ex:
                snack(str(ex), ft.Colors.RED_700)
            finally:
                set_loading(False)

        page.run_task(_apply_disc)

    def clear_order_discount(_):
        cid = session.get("cart_id")
        if not cid:
            return

        async def _clear_disc():
            set_loading(True)
            try:
                resp = await asyncio.to_thread(
                    client.pos_cart_patch,
                    cid,
                    {"order_discount_percent": "0", "order_discount_total": "0"},
                )
                if not _apply_cart_from_response(resp):
                    await _reload_cart_async()
                snack("Скидка на чек сброшена", ft.Colors.GREEN_700)
            except ApiError:
                try:
                    resp2 = await asyncio.to_thread(
                        client.pos_cart_patch, cid, {"order_discount_percent": "0"}
                    )
                    if not _apply_cart_from_response(resp2):
                        await _reload_cart_async()
                    snack("Скидка на чек сброшена", ft.Colors.GREEN_700)
                except ApiError as ex2:
                    snack(str(ex2), ft.Colors.RED_700)
            finally:
                set_loading(False)

        page.run_task(_clear_disc)

    def open_line_item_edit(item: dict[str, Any], item_id: str):
        cid = session.get("cart_id")
        if not cid:
            return

        def _save(_e=None):
            err = validate_quantity(tf_qty.value)
            if err:
                snack(err, ft.Colors.AMBER_700)
                return
            err = validate_unit_price(tf_price.value)
            if err:
                snack(err, ft.Colors.AMBER_700)
                return
            err = validate_line_discount(tf_disc.value)
            if err:
                snack(err, ft.Colors.AMBER_700)
                return

            body: dict[str, str] = {
                "quantity": normalize_decimal_string(tf_qty.value),
                "unit_price": normalize_decimal_string(tf_price.value),
                "discount_total": normalize_decimal_string(tf_disc.value)
                if str(tf_disc.value or "").strip()
                else "0.00",
            }

            async def _save_line():
                set_loading(True, flush=False)
                try:
                    resp = await asyncio.to_thread(client.pos_cart_item_patch, cid, item_id, body)
                    hide_virtual_keyboard()
                    page.dialog.open = False
                    if not _apply_cart_from_response(resp, flush=False):
                        await _reload_cart_async(flush=False)
                except ApiError as ex:
                    set_loading(False, flush=False)
                    snack(str(ex), ft.Colors.RED_700)
                    return
                set_loading(False, flush=False)
                snack("Позиция обновлена", ft.Colors.GREEN_700)

            page.run_task(_save_line)

        def _cancel(_e):
            hide_virtual_keyboard()
            page.dialog.open = False
            page.update()

        dval = item.get("discount_total")
        if dval is None:
            dval = item.get("line_discount")
        tf_qty = bind_virtual_keyboard(
            ft.TextField(
                label="Количество",
                value=str(item.get("quantity", "1")),
                dense=True,
                width=280,
                on_submit=_save,
            ),
            mode="numeric",
            title="Количество",
            submit=_save,
            submit_text="Сохранить",
        )
        tf_price = bind_virtual_keyboard(
            ft.TextField(
                label="Цена за ед. (базовая)",
                value=_money(item.get("unit_price")),
                dense=True,
                width=280,
                on_submit=_save,
            ),
            mode="numeric",
            title="Цена",
            submit=_save,
            submit_text="Сохранить",
        )
        tf_disc = bind_virtual_keyboard(
            ft.TextField(
                label="Скидка на строку (сом, всего на позицию)",
                value=_money(dval) if dval not in (None, "") else "0.00",
                dense=True,
                width=280,
                on_submit=_save,
            ),
            mode="numeric",
            title="Скидка по строке",
            submit=_save,
            submit_text="Сохранить",
        )

        dlg = ft.AlertDialog(
            modal=True,
            bgcolor=UI_SURFACE,
            shape=ft.RoundedRectangleBorder(radius=16),
            title=ft.Text(
                "Цена и скидка по строке",
                color=UI_TEXT,
                weight=ft.FontWeight.W_600,
            ),
            content=ft.Column([tf_qty, tf_price, tf_disc], tight=True, width=320, scroll=ft.ScrollMode.AUTO),
            actions=[
                ft.TextButton("Отмена", on_click=_cancel),
                ft.FilledButton(
                    "Сохранить",
                    style=ft.ButtonStyle(bgcolor=UI_ACCENT, color=UI_TEXT_ON_YELLOW),
                    on_click=_save,
                ),
            ],
        )
        page.dialog = dlg
        dlg.open = True
        page.update()

    def set_shift_banner(needs: bool, detail: str = "", *, flush: bool = True):
        session["needs_shift"] = needs
        if shift_banner.current:
            shift_banner.current.visible = needs
            t = shift_banner.current.content
            if isinstance(t, ft.Column) and t.controls:
                row = t.controls[0]
                if isinstance(row, ft.Row) and len(row.controls) > 1:
                    tx = row.controls[1]
                    if isinstance(tx, ft.Text):
                        tx.value = detail or "Смена не открыта. Откройте смену на кассе, затем начните продажу."
        if flush:
            page.update()

    def render_cart_items(*, flush: bool = True):
        col = cart_items_col.current
        if not col:
            return
        cart = session.get("cart") or {}
        items = cart.get("items") or cart.get("cart_items") or []
        if not isinstance(items, list):
            items = []
        col.controls.clear()
        cid = session.get("cart_id")
        if not items:
            col.controls.append(
                ft.Container(
                    width=float("inf"),
                    content=ft.Text(
                        "Нет позиций — отсканируйте штрихкод или добавьте товар из поиска",
                        color=UI_MUTED,
                        size=14,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    padding=ft.Padding.symmetric(vertical=24, horizontal=8),
                    alignment=ft.Alignment.CENTER,
                )
            )
        else:
            for it in items:
                if not isinstance(it, dict):
                    continue
                iid = _item_id(it)
                if not iid:
                    continue
                name = _item_name(it)
                qty = str(it.get("quantity", "1"))
                line = _item_line_total(it)
                disc_line = _item_discount_display(it)
                line_is_weighed = _cart_line_must_weigh(it)
                qty_unit = "кг" if line_is_weighed else "шт"
                qstep = 0.1 if line_is_weighed else 1.0
                tip_minus = "Меньше на 100 г" if line_is_weighed else "−1 шт"
                tip_plus = "Больше на 100 г" if line_is_weighed else "+1 шт"
                _icon_btn_style = ft.ButtonStyle(
                    padding=ft.Padding.symmetric(horizontal=2, vertical=2),
                )
                sub_lines = [
                    ft.Text(
                        name,
                        size=14,
                        weight=ft.FontWeight.W_500,
                        color=UI_TEXT,
                        max_lines=2,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    ft.Text(
                        f"{qty} {qty_unit} × {_money(it.get('unit_price'))} сом",
                        size=12,
                        color=UI_MUTED,
                        no_wrap=True,
                    ),
                ]
                if disc_line:
                    sub_lines.append(
                        ft.Text(
                            f"Скидка: {disc_line} сом",
                            size=11,
                            color=UI_WARN_TEXT,
                            no_wrap=True,
                        ),
                    )

                def qty_patch(direction: int, item=it, item_id=iid, step=qstep):
                    if not cid or not item_id:
                        return
                    try:
                        cur = float(item.get("quantity") or 0)
                    except (TypeError, ValueError):
                        cur = 0.0
                    q = round(cur + direction * step, 4)
                    if q <= 0:
                        remove_item(item_id)
                        return

                    async def _qty_async():
                        set_loading(True, flush=False)
                        try:
                            resp = await asyncio.to_thread(
                                client.pos_cart_item_patch,
                                cid,
                                item_id,
                                {"quantity": str(q)},
                            )
                            if not _apply_cart_from_response(resp, flush=False):
                                await _reload_cart_async(flush=False)
                        except ApiError as ex:
                            snack(str(ex), ft.Colors.RED_700)
                        finally:
                            set_loading(False)

                    page.run_task(_qty_async)

                def remove_item(item_id=iid):
                    if not cid or not item_id:
                        return

                    async def _del_async():
                        set_loading(True, flush=False)
                        try:
                            await asyncio.to_thread(client.pos_cart_item_delete, cid, item_id)
                            await _reload_cart_async(flush=False)
                        except ApiError as ex:
                            snack(str(ex), ft.Colors.RED_700)
                        finally:
                            set_loading(False)

                    page.run_task(_del_async)

                col.controls.append(
                    ft.Container(
                        width=float("inf"),
                        content=ft.Row(
                            [
                                ft.Container(
                                    content=ft.Column(
                                        sub_lines,
                                        spacing=2,
                                        horizontal_alignment=ft.CrossAxisAlignment.START,
                                    ),
                                    expand=True,
                                ),
                                ft.Row(
                                    [
                                        ft.Text(
                                            f"{line} сом",
                                            size=15,
                                            weight=ft.FontWeight.W_600,
                                            color=UI_TEXT,
                                            width=100,
                                            text_align=ft.TextAlign.RIGHT,
                                            no_wrap=True,
                                        ),
                                        ft.IconButton(
                                            ft.Icons.EDIT_NOTE,
                                            tooltip="Цена и скидка строки",
                                            icon_color=UI_MUTED,
                                            icon_size=20,
                                            style=_icon_btn_style,
                                            on_click=lambda e, row=it, rid=iid: open_line_item_edit(row, rid),
                                        ),
                                        ft.IconButton(
                                            ft.Icons.REMOVE,
                                            tooltip=tip_minus,
                                            icon_color=UI_MUTED,
                                            icon_size=20,
                                            style=_icon_btn_style,
                                            on_click=lambda e, d=-1: qty_patch(d),
                                        ),
                                        ft.IconButton(
                                            ft.Icons.ADD,
                                            tooltip=tip_plus,
                                            icon_color=UI_ACCENT,
                                            icon_size=20,
                                            style=_icon_btn_style,
                                            on_click=lambda e, d=1: qty_patch(d),
                                        ),
                                        ft.IconButton(
                                            ft.Icons.DELETE_OUTLINE,
                                            tooltip="Удалить",
                                            icon_color=UI_MUTED,
                                            icon_size=20,
                                            style=_icon_btn_style,
                                            on_click=lambda e: remove_item(),
                                        ),
                                    ],
                                    spacing=0,
                                    tight=True,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                ),
                            ],
                            alignment=ft.MainAxisAlignment.START,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        padding=ft.Padding.symmetric(vertical=8, horizontal=10),
                        margin=ft.Margin.only(bottom=6),
                        bgcolor=UI_SURFACE_ELEV,
                        border_radius=12,
                        border=ft.Border.all(1, UI_BORDER),
                    )
                )
        # totals
        st = cart.get("subtotal")
        disc = cart.get("discount_total")
        tot_due = _cart_total_due(cart)
        if subtotal_txt.current:
            subtotal_txt.current.value = f"{_money(st)} сом"
        if discount_txt.current:
            discount_txt.current.value = f"{_money(disc)} сом"
        if total_txt.current:
            total_txt.current.value = f"{_money(tot_due)} сом"
        if status_chip.current:
            cid = session.get("cart_id")
            status_chip.current.value = f"Корзина: {str(cid)[:8]}…" if cid else "Нет активной корзины"
        sync_order_discount_fields()
        if flush:
            page.update()

    def reload_cart():
        page.run_task(_reload_cart_async)

    def try_start_sale():
        async def _start():
            set_loading(True, flush=False)
            set_shift_banner(False, flush=False)
            try:
                cb = session.get("pos_cashbox_id")
                if not (cb and str(cb).strip()):
                    try:
                        cbl = await asyncio.to_thread(client.construction_cashboxes_list)
                        first_cb = _first_cashbox_id_from_list(cbl)
                        if first_cb:
                            session["pos_cashbox_id"] = first_cb
                            cb = first_cb
                    except ApiError:
                        pass
                cart = await asyncio.to_thread(client.pos_sales_start, cashbox_id=cb if cb else None)
                session["cart"] = cart
                session["cart_id"] = str(cart.get("id")) if cart.get("id") else None
                sid = _shift_id_from_cart(cart)
                if sid:
                    session["active_shift_id"] = sid
                try:
                    local_products_cache.ingest_cart(client.active_branch_id, cart)
                except Exception:
                    pass
                set_shift_banner(False, flush=False)
                render_cart_items(flush=False)
                show_error("", flush=False)
                set_loading(False, flush=False)
                snack("Продажа начата", ft.Colors.GREEN_700)
            except ApiError as ex:
                pl = ex.payload
                detail = ""
                if isinstance(pl, dict):
                    detail = str(pl.get("detail") or "")
                if ex.status_code == 400 and ("Смена не открыта" in detail or "смена" in detail.lower()):
                    set_shift_banner(True, detail, flush=False)
                    session["cart_id"] = None
                    session["cart"] = {}
                    session["active_shift_id"] = None
                    render_cart_items(flush=False)
                    set_loading(False, flush=False)
                    page.update()
                else:
                    snack(str(ex), ft.Colors.RED_700)
            finally:
                set_loading(False, flush=False)

        page.run_task(_start)

    def process_scan_code(code: str):
        show_error("", flush=False)
        code, bc_err = normalize_barcode_for_scan(code)
        if bc_err:
            show_error(bc_err)
            return
        cid = session.get("cart_id")
        if not cid:
            show_error("Сначала начните продажу (кнопка «Начать продажу»)")
            return
        # Отменить отложенный живой поиск и очистить поле — меньше лишних запросов и гонок с API.
        session["search_gen"] = session.get("search_gen", 0) + 1
        reset_search_panel(flush=True)
        session["scan_seq"] = int(session.get("scan_seq") or 0) + 1
        seq = session["scan_seq"]
        cid_s = str(cid)

        async def _scan_task():
            branch = client.active_branch_id
            cached = local_products_cache.get_cached_scan_row(branch, code)
            cached_pid = cached.get("product_id") if cached else None
            cached_unit = cached.get("unit") if cached else None
            # Быстрый pos_add_item только для явно не-кг (в API часто is_weight=false при unit=кг).
            use_fast_add = (
                bool(cached_pid)
                and cached_unit is not None
                and not _unit_is_kg(cached_unit)
            )
            cart: dict[str, Any] | None = None
            if use_fast_add:
                try:
                    cart = await asyncio.to_thread(client.pos_add_item, cid_s, cached_pid)
                except ApiError:
                    cart = None
            if cart is None:
                try:
                    cart = await asyncio.to_thread(client.pos_scan, cid_s, code)
                except ApiError as ex:
                    if seq == session.get("scan_seq"):
                        show_error(str(ex))
                    return
            if seq != session.get("scan_seq"):
                return
            session["cart"] = cart
            try:
                local_products_cache.ingest_cart(branch, cart)
            except Exception:
                pass
            render_cart_items()

        page.run_task(_scan_task)

    def on_global_keyboard(e: ft.KeyboardEvent):
        if not session.get("cashier_active"):
            return
        if e.ctrl or e.alt or e.meta:
            return
        key = e.key or ""
        if _is_enter_key(key):
            buf = (session.get("barcode_buf") or "").strip()
            session["barcode_buf"] = ""
            session["barcode_last_ms"] = 0.0
            if len(buf) >= MIN_BARCODE_LEN:
                process_scan_code(buf)
            return
        ch = _key_to_barcode_char(key)
        if ch is None:
            return
        now_ms = time.perf_counter() * 1000.0
        last = float(session.get("barcode_last_ms") or 0.0)
        if now_ms - last > BARCODE_INTERKEY_RESET_MS:
            session["barcode_buf"] = ""
        nb = (session.get("barcode_buf") or "") + ch
        if len(nb) > BARCODE_BUFFER_MAX_LEN:
            nb = nb[-BARCODE_BUFFER_MAX_LEN:]
        session["barcode_buf"] = nb
        session["barcode_last_ms"] = now_ms

    def clear_search_results() -> bool:
        col = search_results_ref.current
        if col:
            if not col.controls:
                return False
            col.controls.clear()
            return True
        return False

    def reset_search_panel(*, show_quick_catalog: bool = False, flush: bool = True) -> None:
        changed = False
        if search_field_ref.current and (search_field_ref.current.value or ""):
            search_field_ref.current.value = ""
            changed = True
        if show_quick_catalog:
            _render_quick_catalog(flush=flush)
            return
        changed = clear_search_results() or changed
        if changed and flush:
            page.update()

    def add_product_by_id(product_id: str, quantity: str | None = None):
        err = validate_product_id(product_id)
        if err:
            snack(err, ft.Colors.AMBER_700)
            return
        cid = session.get("cart_id")
        if not cid:
            snack("Сначала начните продажу", ft.Colors.AMBER_700)
            return

        async def _add():
            set_loading(True, flush=False)
            try:
                q = (quantity or "").strip()
                if q:
                    resp = await asyncio.to_thread(client.pos_add_item, cid, product_id, q)
                else:
                    resp = await asyncio.to_thread(client.pos_add_item, cid, product_id)
                if not _apply_cart_from_response(resp, flush=False):
                    await _reload_cart_async(flush=False)
                try:
                    local_products_cache.ingest_cart(
                        client.active_branch_id, session.get("cart") or {}
                    )
                except Exception:
                    pass
                reset_search_panel(show_quick_catalog=True, flush=False)
                show_error("", flush=False)
            except ApiError as ex:
                snack(str(ex), ft.Colors.RED_700)
            finally:
                set_loading(False)

        page.run_task(_add)

    def open_weighed_product_dialog(product: dict[str, Any]):
        """Товары с is_wait / is_weight: ввод кг, кнопка «С весов», затем в чек с quantity."""
        if not isinstance(product, dict):
            return
        pid = str(product.get("id") or "").strip()
        if not pid:
            snack("Нет id товара", ft.Colors.AMBER_700)
            return
        if not session.get("cart_id"):
            snack("Сначала начните продажу", ft.Colors.AMBER_700)
            return
        pname = str(product.get("name") or "Товар")

        mgr = scale_state.get("mgr")
        initial = ""
        if mgr:
            w = mgr.get_last_weight()
            if w is not None and w > 0:
                initial = f"{w:.3f}".rstrip("0").rstrip(".") or str(w)

        def close_dlg():
            dismiss_dialog()

        def do_weighed_add(_e=None):
            err = validate_quantity(tf_q.value)
            if err:
                snack(err, ft.Colors.AMBER_700)
                return
            qstr = normalize_decimal_string(tf_q.value)
            close_dlg()
            add_product_by_id(pid, quantity=qstr)

        def apply_from_scale(_e):
            m = scale_state.get("mgr")
            if not m:
                snack("Весы не подключены — настройте COM в «Весы»", ft.Colors.AMBER_700)
                return
            w = m.get_last_weight()
            if w is None or w <= 0:
                snack("Нет веса с весов (поставьте товар и подождите)", ft.Colors.AMBER_700)
                return
            s = f"{w:.3f}".rstrip("0").rstrip(".")
            tf_q.value = s if s else str(w)
            page.update()

        tf_q = bind_virtual_keyboard(
            ft.TextField(
                label="Вес, кг",
                value=initial,
                hint_text="Введите вручную или «С весов»",
                dense=True,
                autofocus=True,
                on_submit=do_weighed_add,
            ),
            mode="numeric",
            title="Вес товара",
            submit=do_weighed_add,
            submit_text="В чек",
        )

        dlg = ft.AlertDialog(
            modal=True,
            bgcolor=UI_SURFACE,
            shape=ft.RoundedRectangleBorder(radius=16),
            title=ft.Text(
                f"Взвесить: {pname}",
                color=UI_TEXT,
                weight=ft.FontWeight.W_600,
            ),
            content=ft.Column(
                [
                    ft.Text(
                        f"Цена за кг: {_money(product.get('price'))} сом",
                        size=12,
                        color=UI_MUTED,
                    ),
                    tf_q,
                    ft.OutlinedButton(
                        "С весов",
                        icon=ft.Icons.SCALE_OUTLINED,
                        on_click=apply_from_scale,
                    ),
                ],
                tight=True,
                width=320,
                spacing=10,
            ),
            actions=[
                ft.TextButton("Отмена", on_click=lambda e: close_dlg()),
                ft.FilledButton(
                    "В чек",
                    style=ft.ButtonStyle(bgcolor=UI_ACCENT, color=UI_TEXT_ON_YELLOW),
                    on_click=do_weighed_add,
                ),
            ],
        )
        page.show_dialog(dlg)

    def pick_product_from_search(p: dict[str, Any]):
        if not isinstance(p, dict) or not p.get("id"):
            snack("Некорректная карточка товара", ft.Colors.AMBER_700)
            return
        if _product_must_weigh(p):
            open_weighed_product_dialog(p)
        else:
            add_product_by_id(str(p.get("id")))

    def _quick_catalog_matches_tab(p: dict[str, Any], tab: str) -> bool:
        return _product_must_weigh(p) if tab == "kg" else not _product_must_weigh(p)

    def _quick_catalog_name(p: dict[str, Any]) -> str:
        return str(p.get("name") or p.get("title") or f"Товар #{p.get('id') or '—'}").strip()

    def _quick_catalog_norm(text: Any) -> str:
        return str(text or "").strip().lower().replace("ё", "е")

    def _quick_catalog_score(p: dict[str, Any], tab: str) -> tuple[int, float, str]:
        name = _quick_catalog_norm(_quick_catalog_name(p))
        boost = 0
        if tab == "kg":
            boost += 3000
        for needle, score in QUICK_CATALOG_PRIORITY.get(tab, ()):
            if needle in name:
                boost = max(boost, score)
        try:
            updated = float(p.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            updated = 0.0
        return boost, updated, name

    def _sort_quick_catalog_products(
        products: list,
        tab: str,
        limit: int = QUICK_CATALOG_LIMIT,
    ) -> list[dict[str, Any]]:
        uniq: dict[str, dict[str, Any]] = {}
        for raw in products:
            if not isinstance(raw, dict) or not _quick_catalog_matches_tab(raw, tab):
                continue
            pid = raw.get("id")
            pid_s = str(pid).strip() if pid is not None else ""
            if not pid_s:
                continue
            item = dict(raw)
            item["id"] = pid_s
            item["name"] = _quick_catalog_name(item) or f"Товар #{pid_s}"
            prev = uniq.get(pid_s)
            if prev is None or _quick_catalog_score(item, tab) > _quick_catalog_score(prev, tab):
                uniq[pid_s] = item
        ordered = sorted(
            uniq.values(),
            key=lambda p: (
                -_quick_catalog_score(p, tab)[0],
                -_quick_catalog_score(p, tab)[1],
                _quick_catalog_score(p, tab)[2],
            ),
        )
        return ordered[: max(1, int(limit))]

    def _quick_catalog_match_score(
        spec: tuple[str, tuple[str, ...]],
        p: dict[str, Any],
        tab: str,
    ) -> int:
        if not _quick_catalog_matches_tab(p, tab):
            return -1
        name = _quick_catalog_norm(_quick_catalog_name(p))
        best = -1
        for alias in spec[1]:
            alias_n = _quick_catalog_norm(alias)
            if not alias_n:
                continue
            if name == alias_n:
                best = max(best, 6000)
            elif name.startswith(alias_n):
                best = max(best, 5200)
            elif alias_n in name:
                best = max(best, 4300)
        return best

    def _pick_quick_catalog_match(
        spec: tuple[str, tuple[str, ...]],
        pool: list[dict[str, Any]],
        tab: str,
    ) -> dict[str, Any] | None:
        best_item: dict[str, Any] | None = None
        best_score = -1
        for p in pool:
            score = _quick_catalog_match_score(spec, p, tab)
            if score > best_score:
                best_item = p
                best_score = score
        return dict(best_item) if best_item is not None and best_score >= 0 else None

    def _build_search_product_card(p: dict[str, Any]) -> ft.Control:
        title = str(p.get("name") or p.get("title") or "—")
        price = _money(p.get("price"))
        must_weigh = _product_must_weigh(p)
        sub_price = f"{price} сом/кг" if must_weigh else f"{price} сом"
        badge_text = "кг" if must_weigh else "шт"

        def on_pick(e, row=p):
            pick_product_from_search(row)

        return ft.GestureDetector(
            on_tap=on_pick,
            content=ft.Container(
                expand=True,
                height=92,
                padding=ft.Padding.symmetric(horizontal=8, vertical=8),
                bgcolor=UI_SURFACE,
                border_radius=12,
                border=ft.Border.all(1, UI_BORDER),
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Container(
                                    content=ft.Text(
                                        badge_text,
                                        size=9,
                                        color=UI_TEXT,
                                        weight=ft.FontWeight.W_600,
                                    ),
                                    bgcolor=UI_ICON_BADGE_BG,
                                    border_radius=999,
                                    padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                                ),
                                ft.Container(expand=True),
                                ft.Icon(
                                    ft.Icons.SCALE_OUTLINED if must_weigh else ft.Icons.SHOPPING_BAG_OUTLINED,
                                    size=14,
                                    color=UI_ACCENT_DIM,
                                ),
                            ],
                            spacing=4,
                        ),
                        ft.Text(
                            title,
                            size=11,
                            color=UI_TEXT,
                            weight=ft.FontWeight.W_600,
                            max_lines=3,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                        ft.Container(expand=True),
                        ft.Text(
                            sub_price,
                            size=10,
                            color=UI_ACCENT,
                            weight=ft.FontWeight.W_500,
                            max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                    ],
                    spacing=4,
                    expand=True,
                ),
            ),
        )

    def _append_products_grid(col: ft.Column, products: list[dict[str, Any]]) -> None:
        chunk: list[dict[str, Any]] = []
        for p in products:
            if not isinstance(p, dict):
                continue
            chunk.append(p)
            if len(chunk) >= PRODUCT_GRID_COLUMNS:
                row_controls: list[ft.Control] = [
                    ft.Container(content=_build_search_product_card(item), expand=1)
                    for item in chunk
                ]
                col.controls.append(ft.Row(row_controls, spacing=6))
                chunk = []
        if chunk:
            row_controls = [
                ft.Container(content=_build_search_product_card(item), expand=1)
                for item in chunk
            ]
            while len(row_controls) < PRODUCT_GRID_COLUMNS:
                row_controls.append(ft.Container(expand=1))
            col.controls.append(ft.Row(row_controls, spacing=6))

    def _render_quick_catalog(
        products: list | None = None,
        tab: str | None = None,
        *,
        flush: bool = True,
    ) -> None:
        col = search_results_ref.current
        if not col:
            return
        active_tab = str(tab or session.get("quick_catalog_tab") or "kg")
        all_products = session.get("quick_catalog_products") or {}
        items = products if products is not None else (all_products.get(active_tab) or [])
        col.controls.clear()
        if not items:
            loading = bool((session.get("quick_catalog_loading") or {}).get(active_tab))
            hint = "Загружаю товары..." if loading else "Быстрые товары ещё не загружены."
            col.controls.append(
                ft.Container(
                    content=ft.Text(hint, color=UI_MUTED, size=12),
                    padding=ft.Padding.symmetric(horizontal=8, vertical=8),
                )
            )
            if flush:
                page.update()
            return
        _append_products_grid(col, items)
        if flush:
            page.update()

    async def _load_quick_catalog(tab: str, force_remote: bool = False) -> None:
        products_map = session.get("quick_catalog_products") or {"kg": [], "common": []}
        loading_map = session.get("quick_catalog_loading") or {"kg": False, "common": False}
        loaded_map = session.get("quick_catalog_loaded") or {"kg": False, "common": False}
        if loading_map.get(tab):
            return
        if loaded_map.get(tab) and products_map.get(tab) and not force_remote:
            q_now = (search_field_ref.current.value or "").strip() if search_field_ref.current else ""
            if len(q_now) < 2 and str(session.get("quick_catalog_tab") or "kg") == tab:
                _render_quick_catalog()
            return

        loading_map[tab] = True
        q_before = (search_field_ref.current.value or "").strip() if search_field_ref.current else ""
        if len(q_before) < 2 and str(session.get("quick_catalog_tab") or "kg") == tab:
            _render_quick_catalog(products_map.get(tab) or [], tab)

        cache_pool = [
            dict(p)
            for p in local_products_cache.get_cached_products(
                client.active_branch_id,
                kg_only=False,
                limit=QUICK_CATALOG_LIMIT * 8,
            )
            if isinstance(p, dict)
        ]
        cache_pool = [p for p in cache_pool if _quick_catalog_matches_tab(p, tab)]

        presets = QUICK_CATALOG_PRESETS.get(tab, ())
        resolved: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        unresolved: list[tuple[str, tuple[str, ...]]] = []

        for spec in presets:
            found = _pick_quick_catalog_match(spec, cache_pool, tab)
            if found:
                pid = str(found.get("id") or "").strip()
                if pid and pid not in seen_ids:
                    resolved.append(found)
                    seen_ids.add(pid)
            else:
                unresolved.append(spec)

        merged_pool: list[dict[str, Any]] = list(cache_pool)
        try:
            for spec in unresolved:
                found: dict[str, Any] | None = None
                for query in spec[1]:
                    try:
                        rows = await asyncio.to_thread(client.products_search, query, 20)
                    except ApiError:
                        continue
                    ready_rows = [
                        dict(p)
                        for p in rows
                        if isinstance(p, dict) and p.get("id") and _quick_catalog_matches_tab(p, tab)
                    ]
                    if not ready_rows:
                        continue
                    merged_pool.extend(ready_rows)
                    try:
                        local_products_cache.ingest_product_list(client.active_branch_id, ready_rows)
                    except Exception:
                        pass
                    found = _pick_quick_catalog_match(spec, ready_rows + merged_pool, tab)
                    if found:
                        break
                if found:
                    pid = str(found.get("id") or "").strip()
                    if pid and pid not in seen_ids:
                        resolved.append(found)
                        seen_ids.add(pid)

            if len(resolved) < QUICK_CATALOG_LIMIT:
                try:
                    catalog_rows = await asyncio.to_thread(
                        client.products_catalog,
                        QUICK_CATALOG_LIMIT * 6,
                        8,
                    )
                except ApiError:
                    catalog_rows = []
                ready_catalog = [
                    dict(p)
                    for p in catalog_rows
                    if isinstance(p, dict) and p.get("id") and _quick_catalog_matches_tab(p, tab)
                ]
                if ready_catalog:
                    merged_pool.extend(ready_catalog)
                    try:
                        local_products_cache.ingest_product_list(client.active_branch_id, ready_catalog)
                    except Exception:
                        pass

            remainder = _sort_quick_catalog_products(merged_pool, tab, QUICK_CATALOG_LIMIT * 3)
            final = resolved + [p for p in remainder if str(p.get("id") or "").strip() not in seen_ids]
            products_map[tab] = final[:QUICK_CATALOG_LIMIT]
            loaded_map[tab] = True

            q_after = (search_field_ref.current.value or "").strip() if search_field_ref.current else ""
            if len(q_after) < 2 and str(session.get("quick_catalog_tab") or "kg") == tab:
                _render_quick_catalog(products_map.get(tab) or [], tab)
        finally:
            loading_map[tab] = False

    def _fill_search_results(products: list):
        col = search_results_ref.current
        if not col:
            return
        col.controls.clear()
        if not products:
            col.controls.append(ft.Text("Ничего не найдено", color=UI_MUTED, size=13))
            return
        valid_products = [
            p for p in products if isinstance(p, dict) and p.get("id")
        ]
        _append_products_grid(col, valid_products)

    def _do_search(q: str, silent: bool = False):
        q = (q or "").strip()
        if not silent:
            show_error("", flush=False)
        if len(q) < 2:
            _render_quick_catalog(flush=not silent)
            if not silent:
                if q:
                    show_error("Введите минимум 2 символа для поиска по названию")
                else:
                    show_error("")
            else:
                show_error("", flush=False)
                page.update()
            return
        qerr = validate_search_query(q)
        if qerr:
            show_error(qerr)
            if not silent:
                snack(qerr, ft.Colors.AMBER_700)
            page.update()
            return
        if not session.get("cart_id"):
            clear_search_results()
            show_error(
                "Сначала нажмите «Начать продажу» (сверху), затем введите от 2 букв названия товара."
            )
            if not silent:
                snack("Сначала начните продажу", ft.Colors.AMBER_700)
            page.update()
            return

        async def _search_task():
            set_loading(True)
            try:
                show_error("")
                products = await asyncio.to_thread(client.products_search, q)
                try:
                    local_products_cache.ingest_product_list(
                        client.active_branch_id, products
                    )
                except Exception:
                    pass
                _fill_search_results(products)
                page.update()
            except ApiError as ex:
                show_error(str(ex))
                snack(str(ex), ft.Colors.RED_700)
            finally:
                set_loading(False)

        page.run_task(_search_task)

    async def _delayed_live_search(my_gen: int):
        await asyncio.sleep(LIVE_SEARCH_DEBOUNCE_SEC)
        if my_gen != session["search_gen"]:
            return
        q = (search_field_ref.current.value or "").strip() if search_field_ref.current else ""
        if _looks_like_barcode_query(q):
            return
        _do_search(q, silent=True)

    def on_search_change(_):
        q = (search_field_ref.current.value or "").strip() if search_field_ref.current else ""
        if not q:
            session["search_gen"] = session.get("search_gen", 0) + 1
            active_tab = str(session.get("quick_catalog_tab") or "kg")
            _render_quick_catalog(flush=True)
            page.run_task(_load_quick_catalog, active_tab, False)
            return
        if _looks_like_barcode_query(q):
            return
        session["search_gen"] = session.get("search_gen", 0) + 1
        page.run_task(_delayed_live_search, session["search_gen"])

    def on_search_submit(_):
        q = (search_field_ref.current.value or "").strip() if search_field_ref.current else ""
        if _looks_like_barcode_query(q):
            process_scan_code(q)
            page.update()
            return
        _do_search(q, silent=False)

    def on_quick_catalog_tab_change(e):
        selected = e.control.selected[0] if getattr(e.control, "selected", None) else "kg"
        session["quick_catalog_tab"] = selected
        q = (search_field_ref.current.value or "").strip() if search_field_ref.current else ""
        if len(q) < 2:
            _render_quick_catalog(tab=selected)
            page.run_task(_load_quick_catalog, selected, False)

    def open_shift_dlg(_):
        async def _load_and_show():
            set_loading(True)
            try:
                cashboxes = await asyncio.to_thread(client.construction_cashboxes_list)
            except ApiError as ex:
                snack(str(ex), ft.Colors.RED_700)
                return
            finally:
                set_loading(False)

            dd_options: list[ft.dropdown.Option] = []
            default_cashbox: str | None = None
            for c in cashboxes:
                if not isinstance(c, dict):
                    continue
                sid_cb = _cashbox_id_from_dict(c)
                if not sid_cb:
                    continue
                label = str(c.get("name") or c.get("title") or sid_cb)
                dd_options.append(ft.dropdown.Option(key=sid_cb, text=label))
                if default_cashbox is None:
                    default_cashbox = sid_cb

            keys_ok = {o.key for o in dd_options}
            pref = session.get("pos_cashbox_id")
            if pref and str(pref) in keys_ok:
                default_cashbox = str(pref)

            manual = bind_virtual_keyboard(
                ft.TextField(label="ID кассы", visible=not dd_options, dense=True, expand=True),
                mode="english",
                title="ID кассы",
                submit_text="Готово",
            )
            dd = ft.Dropdown(
                label="Касса",
                options=dd_options,
                value=default_cashbox,
                visible=bool(dd_options),
                expand=True,
            )
            opening = bind_virtual_keyboard(
                ft.TextField(label="Начальная сумма", value="0.00", dense=True),
                mode="numeric",
                title="Начальная сумма",
                submit=lambda _e=None: do_open(None),
                submit_text="Открыть",
            )

            def do_open(_e):
                box = (dd.value or "").strip() if dd.visible else ""
                if not box:
                    box = (manual.value or "").strip()
                if not box:
                    snack("Укажите кассу", ft.Colors.AMBER_700)
                    return
                err = validate_cashbox_id(box)
                if err:
                    snack(err, ft.Colors.AMBER_700)
                    return
                ov, oerr = parse_decimal(
                    opening.value, field_name="Начальная сумма", allow_negative=False
                )
                if oerr:
                    snack(oerr, ft.Colors.AMBER_700)
                    return

                async def _open_shift():
                    set_loading(True)
                    try:
                        res = await asyncio.to_thread(
                            client.construction_shift_open, box, _money(ov)
                        )
                        session["pos_cashbox_id"] = box
                        new_sid = _shift_id_from_open_response(res)
                        if new_sid:
                            session["active_shift_id"] = new_sid
                        dismiss_dialog()
                        snack("Смена открыта", ft.Colors.GREEN_700)
                        try_start_sale()
                    except ApiError as ex:
                        snack(str(ex), ft.Colors.RED_700)
                    finally:
                        set_loading(False)

                page.run_task(_open_shift)

            dlg = ft.AlertDialog(
                modal=True,
                bgcolor=UI_SURFACE,
                shape=ft.RoundedRectangleBorder(radius=16),
                title=ft.Text("Открыть смену", color=UI_TEXT, weight=ft.FontWeight.W_600),
                content=ft.Column(
                    [dd, manual, opening],
                    tight=True,
                    width=400,
                ),
                actions=[
                    ft.TextButton("Отмена", on_click=dismiss_dialog),
                    ft.FilledButton(
                        "Открыть",
                        style=ft.ButtonStyle(bgcolor=UI_ACCENT, color=UI_TEXT_ON_YELLOW),
                        on_click=do_open,
                    ),
                ],
            )
            page.show_dialog(dlg)

        page.run_task(_load_and_show)

    def close_shift_click(_):
        async def _resolve_and_show():
            cart = session.get("cart") or {}
            sid = _resolve_shift_id(session, cart)
            if not sid:
                try:
                    lst = await asyncio.to_thread(client.construction_shifts_list)
                    sid = _pick_open_shift_id_from_list(lst, session.get("pos_cashbox_id"))
                except ApiError:
                    sid = None
            if not sid:
                snack(
                    "Не найдена открытая смена. Откройте смену или нажмите «Начать продажу», чтобы подтянуть данные с сервера.",
                    ft.Colors.AMBER_700,
                )
                return
            closing = bind_virtual_keyboard(
                ft.TextField(label="Наличные при закрытии", value="0.00", dense=True),
                mode="numeric",
                title="Наличные при закрытии",
                submit=lambda _e=None: do_close(None),
                submit_text="Закрыть",
            )

            def do_close(_e):
                cv, cerr = parse_decimal(
                    closing.value, field_name="Наличные при закрытии", allow_negative=False
                )
                if cerr:
                    snack(cerr, ft.Colors.AMBER_700)
                    return

                async def _close_shift():
                    set_loading(True)
                    try:
                        await asyncio.to_thread(client.construction_shift_close, sid, _money(cv))
                        dismiss_dialog()
                        session["active_shift_id"] = None
                        session["cart_id"] = None
                        session["cart"] = {}
                        render_cart_items()
                        snack("Смена закрыта", ft.Colors.GREEN_700)
                    except ApiError as ex:
                        snack(str(ex), ft.Colors.RED_700)
                    finally:
                        set_loading(False)

                page.run_task(_close_shift)

            dlg = ft.AlertDialog(
                modal=True,
                bgcolor=UI_SURFACE,
                shape=ft.RoundedRectangleBorder(radius=16),
                title=ft.Text("Закрыть смену", color=UI_TEXT, weight=ft.FontWeight.W_600),
                content=closing,
                actions=[
                    ft.TextButton("Отмена", on_click=dismiss_dialog),
                    ft.FilledButton(
                        "Закрыть",
                        style=ft.ButtonStyle(bgcolor=UI_ACCENT, color=UI_TEXT_ON_YELLOW),
                        on_click=do_close,
                    ),
                ],
            )
            page.show_dialog(dlg)

        page.run_task(_resolve_and_show)

    def open_checkout_payment_dialog(cart: dict, cid_key: str):
        checkout_pay_msg = ft.Ref[ft.Text]()

        def set_checkout_pay_msg(text: str):
            t = checkout_pay_msg.current
            if t:
                s = (text or "").strip()
                t.value = s
                t.visible = bool(s)
            page.update()

        tot_f = _cart_total_due(cart)
        u_pay = client.user_payload
        receipt_company = str(u_pay.get("company") or "—")
        receipt_cashier = (
            f"{u_pay.get('first_name', '')} {u_pay.get('last_name', '')}".strip()
            or str(u_pay.get("email") or "—")
        )

        cash_in = bind_virtual_keyboard(
            ft.TextField(
                label="Получено наличными",
                value=_money(tot_f),
                hint_text="Не меньше суммы чека",
                dense=True,
                visible=True,
                filled=True,
                bgcolor=UI_SURFACE_ELEV,
                border_radius=12,
                color=UI_TEXT,
                prefix_icon=ft.Icons.PAYMENTS_OUTLINED,
            ),
            mode="numeric",
            title="Оплата наличными",
            submit=lambda _e=None: do_pay(None),
            submit_text="Оплатить",
        )

        pay_seg = ft.SegmentedButton(
            segments=[
                ft.Segment(
                    value="cash",
                    label=ft.Text("Наличные", size=13),
                    icon=ft.Icons.POINT_OF_SALE,
                ),
                ft.Segment(
                    value="transfer",
                    label=ft.Text("Безнал", size=13),
                    icon=ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED,
                ),
            ],
            selected=["cash"],
            show_selected_icon=False,
            style=ft.ButtonStyle(
                color={
                    ft.ControlState.DEFAULT: UI_TEXT,
                    ft.ControlState.SELECTED: UI_TEXT_ON_YELLOW,
                },
                bgcolor={
                    ft.ControlState.DEFAULT: "#f3f4f6",
                    ft.ControlState.SELECTED: UI_ACCENT,
                },
                side=ft.BorderSide(1, UI_BORDER),
                shape=ft.RoundedRectangleBorder(radius=10),
            ),
        )

        def sync_pay_method(_e=None):
            pm = pay_seg.selected[0] if pay_seg.selected else "cash"
            cash_in.visible = pm == "cash"
            page.update()

        pay_seg.on_change = sync_pay_method

        def do_pay(_e):
            set_checkout_pay_msg("")
            pm = pay_seg.selected[0] if pay_seg.selected else "cash"
            want_print = is_receipt_printing_enabled()
            body: dict[str, Any] = {"payment_method": pm, "print_receipt": want_print}
            if pm == "cash":
                err = validate_cash_received(cash_in.value, tot_f)
                if err:
                    set_checkout_pay_msg(err)
                    snack(err, ft.Colors.AMBER_700)
                    return
                body["cash_received"] = normalize_decimal_string(cash_in.value)
            else:
                body["cash_received"] = "0.00"

            cart_snapshot = dict(session.get("cart") or {})
            cid_pay = cid_key
            cash_recv = normalize_decimal_string(cash_in.value) if pm == "cash" else None

            async def _checkout_async():
                set_loading(True)
                page.update()
                try:
                    res = await asyncio.to_thread(client.pos_checkout, cid_pay, body)
                except ApiError as ex:
                    set_checkout_pay_msg(str(ex))
                    snack(str(ex), ft.Colors.RED_700)
                    return
                except requests.exceptions.RequestException as ex:
                    set_checkout_pay_msg(f"Сеть: {ex}")
                    snack(f"Сеть: {ex}", ft.Colors.RED_700)
                    return
                except Exception as ex:
                    set_checkout_pay_msg(str(ex))
                    snack(str(ex), ft.Colors.RED_700)
                    return
                finally:
                    set_loading(False)
                    page.update()

                dismiss_dialog()
                ch = _checkout_change_amount(res)
                sale_id = _checkout_sale_id(res)
                msg = f"Оплата прошла. Сдача: {_money(ch)} сом" if ch is not None else "Оплата прошла"
                if sale_id:
                    msg += f" (продажа {str(sale_id)[:8]}…)"
                snack(msg, ft.Colors.GREEN_700)
                if want_print:
                    use_api_receipt = os.environ.get(
                        "DESKTOP_MARKET_RECEIPT_USE_API_TEXT", ""
                    ).strip().lower() in ("1", "true", "yes", "on")
                    txt = _receipt_text_from_checkout_response(res)
                    if not txt and sale_id:
                        txt = await asyncio.to_thread(
                            _fetch_receipt_text_via_api, client, sale_id
                        )
                    try:
                        if use_api_receipt and txt:
                            print_receipt_text(txt)
                        else:
                            print_sale_receipt(
                                cart=cart_snapshot,
                                payment_method=pm,
                                cash_received=cash_recv,
                                change_amount=ch,
                                sale_id=sale_id,
                                company=receipt_company,
                                cashier=receipt_cashier,
                            )
                    except ReceiptPrinterError as ex:
                        fallback = False
                        if not use_api_receipt and txt:
                            try:
                                print_receipt_text(txt)
                                fallback = True
                            except ReceiptPrinterError:
                                pass
                        elif use_api_receipt:
                            try:
                                print_sale_receipt(
                                    cart=cart_snapshot,
                                    payment_method=pm,
                                    cash_received=cash_recv,
                                    change_amount=ch,
                                    sale_id=sale_id,
                                    company=receipt_company,
                                    cashier=receipt_cashier,
                                )
                                fallback = True
                            except ReceiptPrinterError:
                                pass
                        if not fallback:
                            snack(f"Чек не напечатан: {ex}", ft.Colors.AMBER_700)
                    except Exception as ex:
                        snack(f"Печать чека: {ex}", ft.Colors.AMBER_700)
                session["cart_id"] = None
                session["cart"] = {}
                try_start_sale()

            page.run_task(_checkout_async)

        def close_checkout_dlg(_e=None):
            dismiss_dialog()

        dlg = ft.AlertDialog(
            modal=True,
            bgcolor=UI_SURFACE,
            elevation=8,
            shape=ft.RoundedRectangleBorder(radius=22),
            icon=ft.Container(
                content=ft.Icon(ft.Icons.RECEIPT_LONG, color=UI_ACCENT, size=26),
                padding=8,
                bgcolor=UI_ICON_BADGE_BG,
                border_radius=10,
            ),
            icon_color=UI_ACCENT,
            title=ft.Text(
                "Оплата чека",
                size=20,
                weight=ft.FontWeight.W_600,
                color=UI_TEXT,
            ),
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text(
                            ref=checkout_pay_msg,
                            value="",
                            size=13,
                            color=ft.Colors.RED_700,
                            visible=False,
                        ),
                        ft.Container(
                            content=ft.Column(
                                [
                                    ft.Text(
                                        "К ОПЛАТЕ",
                                        size=11,
                                        weight=ft.FontWeight.W_600,
                                        color=UI_MUTED,
                                    ),
                                    ft.Text(
                                        f"{_money(tot_f)} сом",
                                        size=34,
                                        weight=ft.FontWeight.W_700,
                                        color=UI_TEXT,
                                    ),
                                ],
                                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                spacing=4,
                                tight=True,
                            ),
                            padding=ft.Padding.symmetric(horizontal=20, vertical=18),
                            bgcolor=UI_SURFACE,
                            border_radius=16,
                            border=ft.Border.only(
                                top=ft.BorderSide(3, UI_ACCENT),
                                left=ft.BorderSide(1, UI_BORDER),
                                right=ft.BorderSide(1, UI_BORDER),
                                bottom=ft.BorderSide(1, UI_BORDER),
                            ),
                        ),
                        ft.Container(height=18),
                        ft.Text(
                            "Способ оплаты",
                            size=13,
                            weight=ft.FontWeight.W_600,
                            color=UI_TEXT,
                        ),
                        ft.Container(height=8),
                        pay_seg,
                        ft.Container(height=14),
                        cash_in,
                    ],
                    tight=True,
                    width=380,
                    spacing=0,
                ),
                padding=ft.Padding.only(top=4),
            ),
            actions=[
                        ft.OutlinedButton(
                    "Отмена",
                    style=ft.ButtonStyle(
                        color=UI_MUTED,
                        side=ft.BorderSide(1, UI_BORDER),
                        bgcolor=UI_SURFACE,
                        shape=ft.RoundedRectangleBorder(radius=10),
                        padding=ft.Padding.symmetric(horizontal=20, vertical=12),
                    ),
                    on_click=close_checkout_dlg,
                ),
                ft.FilledButton(
                    "Провести оплату",
                    icon=ft.Icons.CHECK_CIRCLE_OUTLINED,
                    style=ft.ButtonStyle(
                        bgcolor=UI_ACCENT,
                        color=UI_TEXT_ON_YELLOW,
                        shape=ft.RoundedRectangleBorder(radius=10),
                        padding=ft.Padding.symmetric(horizontal=22, vertical=12),
                    ),
                    on_click=do_pay,
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            actions_padding=ft.Padding.symmetric(horizontal=20, vertical=16),
        )
        page.show_dialog(dlg)

    def checkout_click(_):
        cid = session.get("cart_id")
        if not cid:
            snack("Нет корзины", ft.Colors.AMBER_700)
            return
        cart = session.get("cart") or {}
        items = cart.get("items") or cart.get("cart_items") or []
        if not items:
            snack("Добавьте товары в чек", ft.Colors.AMBER_700)
            return

        async def _prep_checkout():
            set_loading(True)
            try:
                fresh = await asyncio.to_thread(client.pos_cart_get, str(cid))
            except ApiError as ex:
                snack(str(ex), ft.Colors.RED_700)
                return
            finally:
                set_loading(False)
            session["cart"] = fresh
            items2 = fresh.get("items") or fresh.get("cart_items") or []
            if not items2:
                snack(
                    "Корзина на сервере пуста — обновите продажу",
                    ft.Colors.AMBER_700,
                )
                return
            open_checkout_payment_dialog(fresh, str(cid))

        page.run_task(_prep_checkout)

    async def _quit_desktop_app():
        await page.window.destroy()

    def build_login() -> ft.Column:
        _login_field_fill = UI_SURFACE_ELEV
        email = bind_virtual_keyboard(
            ft.TextField(
                label="Email",
                value=TEST_LOGIN_EMAIL,
                hint_text="user@example.com",
                keyboard_type=ft.KeyboardType.EMAIL,
                autofocus=True,
                border_radius=8,
                filled=True,
                border=ft.InputBorder.NONE,
                bgcolor=_login_field_fill,
                border_width=0,
                expand=True,
            ),
            mode="email",
            title="Email",
            submit_text="Готово",
        )
        password = bind_virtual_keyboard(
            ft.TextField(
                label="Пароль",
                value=TEST_LOGIN_PASSWORD,
                password=True,
                can_reveal_password=True,
                border_radius=8,
                filled=True,
                border=ft.InputBorder.NONE,
                bgcolor=_login_field_fill,
                border_width=0,
                expand=True,
                on_submit=lambda e: do_login(None),
            ),
            mode="password",
            title="Пароль",
            submit=lambda _e=None: do_login(None),
            submit_text="Войти",
        )

        def do_login(_):
            show_error("")
            err = validate_email(email.value)
            if err:
                show_error(err)
                return
            err = validate_password(password.value, min_len=4)
            if err:
                show_error(err)
                return

            async def _login_task():
                set_loading(True)
                try:
                    await asyncio.to_thread(
                        client.login, email.value.strip(), password.value
                    )
                    open_cashier()
                except ApiError as ex:
                    show_error(str(ex))
                except requests.exceptions.RequestException as ex:
                    show_error(f"Сеть: {ex}")
                finally:
                    set_loading(False)

            page.run_task(_login_task)

        api_hint = ft.Text(f"API: {API_BASE_URL}", size=11, color=UI_MUTED)

        login_card = ft.Container(
            content=ft.Row(
                [
                    ft.Container(
                        width=5,
                        border_radius=4,
                        bgcolor=UI_ACCENT,
                    ),
                    ft.Container(
                        content=ft.Column(
                            [
                                ft.Text(
                                    "NUR MARKET",
                                    size=12,
                                    weight=ft.FontWeight.W_700,
                                    color=UI_ACCENT,
                                ),
                                ft.Text("Вход кассира", size=26, weight=ft.FontWeight.W_600, color=UI_TEXT),
                                ft.Text("Email и пароль от аккаунта", size=14, color=UI_MUTED),
                                ft.Container(height=22),
                                ft.Row([email], expand=True),
                                ft.Container(height=10),
                                ft.Row([password], expand=True),
                                ft.Text(ref=error_text, value="", color=ft.Colors.RED_400, visible=False),
                                ft.Container(height=18),
                                ft.FilledButton(
                                    "Войти",
                                    width=float("inf"),
                                    height=50,
                                    style=ft.ButtonStyle(
                                        bgcolor=UI_ACCENT,
                                        color=UI_TEXT_ON_YELLOW,
                                        padding=ft.Padding.symmetric(vertical=14),
                                        shape=ft.RoundedRectangleBorder(radius=10),
                                    ),
                                    on_click=do_login,
                                ),
                                ft.Container(height=10),
                                ft.OutlinedButton(
                                    "Закрыть программу",
                                    width=float("inf"),
                                    style=ft.ButtonStyle(
                                        color=UI_MUTED,
                                        side=ft.BorderSide(1, UI_BORDER),
                                        shape=ft.RoundedRectangleBorder(radius=10),
                                    ),
                                    on_click=lambda _: page.run_task(_quit_desktop_app),
                                ),
                                ft.Container(height=14),
                                api_hint,
                            ],
                            tight=True,
                            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                        ),
                        padding=ft.Padding.only(left=28, right=36, top=36, bottom=36),
                        expand=True,
                    ),
                ],
                spacing=0,
                expand=True,
            ),
            width=440,
            bgcolor=UI_SURFACE,
            border_radius=18,
            border=ft.Border.all(1, UI_BORDER),
            shadow=ft.BoxShadow(
                blur_radius=24,
                spread_radius=0,
                color=ft.Colors.with_opacity(0.12, "#111827"),
                offset=ft.Offset(0, 8),
            ),
        )

        return ft.Column(
            [
                ft.Container(expand=True),
                ft.Row(
                    [
                        ft.Container(expand=True),
                        login_card,
                        ft.Container(expand=True),
                    ],
                    expand=False,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Container(expand=True),
            ],
            expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            spacing=0,
        )

    def open_cashier():
        hide_virtual_keyboard()
        session["cashier_active"] = True
        session["barcode_buf"] = ""
        session["barcode_last_ms"] = 0.0
        session["quick_catalog_tab"] = "kg"
        session["quick_catalog_products"] = {"kg": [], "common": []}
        session["quick_catalog_loading"] = {"kg": False, "common": False}
        session["quick_catalog_loaded"] = {"kg": False, "common": False}
        page.on_keyboard_event = on_global_keyboard
        page.controls.clear()
        page.add(build_cashier())
        page.window.prevent_close = True
        page.update()
        _render_quick_catalog([], "kg")
        page.run_task(_load_quick_catalog, "kg", True)
        if scale_feature_enabled:
            old = scale_state.get("mgr")
            if old is not None:
                try:
                    old.stop()
                except Exception:
                    pass
            scale_state["mgr"] = ScaleManager(
                page,
                weight_scale_text,
                weight_scale_status,
                port=config.SCALE_COM_PORT,
                baudrate=config.SCALE_COM_BAUD,
            )
            scale_state["mgr"].start()
        try_start_sale()

    def logout(_):
        hide_virtual_keyboard()
        mgr = scale_state.get("mgr")
        if mgr is not None:
            try:
                mgr.stop()
            except Exception:
                pass
            scale_state["mgr"] = None
        session["cashier_active"] = False
        session["barcode_buf"] = ""
        page.on_keyboard_event = None
        client.clear()
        session["cart_id"] = None
        session["cart"] = {}
        session["needs_shift"] = False
        session["pos_cashbox_id"] = None
        session["active_shift_id"] = None
        page.controls.clear()
        page.window.prevent_close = False
        page.add(ft.Stack([build_login(), build_loading_overlay()], expand=True))
        page.update()

    def open_printer_settings_dlg(_):
        cur = printer_config.as_dict()
        tf_lpt = bind_virtual_keyboard(
            ft.TextField(
                label="LPT-порт чека",
                value=str(cur.get("file_path") or "LPT1"),
                hint_text="Обычно LPT1 или LPT2",
                dense=True,
                width=420,
            ),
            mode="lpt",
            title="LPT-порт чека",
            submit_text="Готово",
        )
        dd_escpos_profile = ft.Dropdown(
            label="Модель / профиль ESC/POS",
            width=420,
            value=str(cur.get("escpos_profile") or "default"),
            options=[
                ft.dropdown.Option(key="default", text="Универсальный профиль"),
                ft.dropdown.Option(
                    key="TEP-200M",
                    text="Cashino EP-200 / TEP200M (WPC1251, слот таблицы 46)",
                ),
            ],
        )
        dd_encoding = ft.Dropdown(
            label="Кодировка текста",
            width=420,
            value=str(cur.get("text_encoding") or "wpc1251"),
            options=[
                ft.dropdown.Option(key="cp866", text="CP866 — кириллица (DOS/OEM)"),
                ft.dropdown.Option(key="cp1251", text="CP1251 — Windows-1251"),
                ft.dropdown.Option(
                    key="wpc1251",
                    text="WPC1251 — как в документации ESC/POS (рекомендуется для Cashino EP-200)",
                ),
                ft.dropdown.Option(key="utf-8", text="UTF-8 — печать как CP1251/WPC1251"),
            ],
        )

        def _opt_str(v: Any) -> str:
            if v is None:
                return ""
            return str(v)

        tf_escpos_table = bind_virtual_keyboard(
            ft.TextField(
                label="Таблица ESC t (0–255)",
                value=_opt_str(cur.get("escpos_table")),
                hint_text=(
                    "Пусто: авто (CP866→17, WPC1251/CP1251→46). Указали число — уходит как есть "
                    "(на части Cashino+LPT читаемее CP866+46). Кракозябры — поменяйте пару кодировка/слот."
                ),
                dense=True,
                width=420,
            ),
            mode="numeric",
            title="Таблица ESC t",
            submit_text="Готово",
        )
        tf_esc_r = bind_virtual_keyboard(
            ft.TextField(
                label="ESC R международный набор (0–255)",
                value=_opt_str(cur.get("esc_r")),
                hint_text="Обычно пусто; редко 0 или 6",
                dense=True,
                width=420,
            ),
            mode="numeric",
            title="ESC R",
            submit_text="Готово",
        )

        def collect() -> dict[str, Any]:
            fp = (tf_lpt.value or "LPT1").strip() or "LPT1"
            enc = (dd_encoding.value or "wpc1251").strip().lower()
            et = (tf_escpos_table.value or "").strip()
            return {
                "backend": "lpt",
                "file_path": fp,
                "text_encoding": enc,
                "escpos_profile": (dd_escpos_profile.value or "default").strip(),
                "escpos_table": et,
                "esc_r": (tf_esc_r.value or "").strip(),
            }

        def do_save(_e):
            try:
                printer_config.save(collect())
                dismiss_dialog()
                snack("Настройки принтера сохранены", ft.Colors.GREEN_700)
            except OSError as ex:
                snack(f"Не удалось записать файл: {ex}", ft.Colors.RED_700)

        def do_test(_e):
            set_loading(True)
            try:
                printer_config.apply(collect())
                if not is_receipt_printing_enabled():
                    snack("Укажите LPT-порт принтера, например LPT1", ft.Colors.AMBER_700)
                    return
                print_printer_self_check_page()
                snack("Страница самопроверки отправлена на принтер", ft.Colors.GREEN_700)
            except ReceiptPrinterError as ex:
                snack(str(ex), ft.Colors.RED_700)
            finally:
                set_loading(False)

        dlg = ft.AlertDialog(
            modal=True,
            bgcolor=UI_SURFACE,
            shape=ft.RoundedRectangleBorder(radius=16),
            title=ft.Text(
                "Принтер чека",
                size=20,
                weight=ft.FontWeight.W_600,
                color=UI_TEXT,
            ),
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text(
                            f"Файл данных (SQLite): {printer_config.settings_path()}",
                            size=11,
                            color=UI_MUTED,
                            selectable=True,
                        ),
                        ft.Text(
                            "«Сохранить» записывает в файл и перезапускает не нужен. "
                            "«Тест» применяет значения из формы и печатает пробную строку.",
                            size=12,
                            color=UI_MUTED,
                        ),
                        ft.Text(
                            "Для чека поддерживается только LPT. По умолчанию используется кириллица WPC1251. "
                            "Если на вашем принтере текст искажён, попробуйте CP866 и таблицу 17.",
                            size=12,
                            color=UI_MUTED,
                        ),
                        ft.Text("LPT", size=12, weight=ft.FontWeight.W_600, color=UI_TEXT),
                        tf_lpt,
                        dd_escpos_profile,
                        dd_encoding,
                        tf_escpos_table,
                        tf_esc_r,
                    ],
                    tight=True,
                    scroll=ft.ScrollMode.AUTO,
                    width=460,
                ),
                height=480,
                padding=ft.Padding.only(right=8),
            ),
            actions=[
                ft.TextButton("Отмена", on_click=dismiss_dialog),
                ft.OutlinedButton(
                    "Самопроверка",
                    icon=ft.Icons.FACT_CHECK_OUTLINED,
                    style=ft.ButtonStyle(
                        color=UI_MUTED,
                        side=ft.BorderSide(1, UI_BORDER),
                    ),
                    on_click=do_test,
                ),
                ft.FilledButton(
                    "Сохранить",
                    icon=ft.Icons.SAVE_OUTLINED,
                    style=ft.ButtonStyle(bgcolor=UI_ACCENT, color=UI_TEXT_ON_YELLOW),
                    on_click=do_save,
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    def restart_scale_manager():
        if not scale_feature_enabled:
            return
        old = scale_state.get("mgr")
        if old is not None:
            try:
                old.stop()
            except Exception:
                pass
        scale_state["mgr"] = ScaleManager(
            page,
            weight_scale_text,
            weight_scale_status,
            port=config.SCALE_COM_PORT,
            baudrate=config.SCALE_COM_BAUD,
        )
        scale_state["mgr"].start()

    def open_scale_settings_dlg(_):
        cur = printer_config.as_dict()

        def _parse_int(raw: str, default: int) -> int:
            try:
                return int(str(raw).strip(), 0)
            except (TypeError, ValueError):
                return default

        def _com_ports_with_desc() -> list[tuple[str, str]]:
            try:
                import serial.tools.list_ports

                rows: list[tuple[str, str]] = []
                for p in serial.tools.list_ports.comports():
                    desc = (p.description or "").strip() or "—"
                    rows.append((p.device, desc))
                rows.sort(key=lambda x: (len(x[0]), x[0]))
                return rows
            except Exception:
                return []

        def _build_scale_port_options(
            selected: str,
        ) -> tuple[list[ft.dropdown.Option], str]:
            rows = _com_ports_with_desc()
            devices = [d for d, _ in rows]
            opts: list[ft.dropdown.Option] = []
            for dev, desc in rows:
                short = desc if len(desc) <= 52 else desc[:49] + "…"
                opts.append(ft.dropdown.Option(key=dev, text=f"{dev} — {short}"))
            sel = (selected or "COM3").strip() or "COM3"
            if sel not in devices:
                opts.insert(0, ft.dropdown.Option(key=sel, text=f"{sel} (текущий в настройках)"))
            if not opts:
                opts = [
                    ft.dropdown.Option(
                        key="COM3",
                        text="COM3 (установите pyserial или подключите устройство)",
                    )
                ]
                sel = "COM3"
            val = sel if any(o.key == sel for o in opts) else str(opts[0].key)
            return opts, val

        _opts, _val = _build_scale_port_options(str(cur.get("scale_port") or "COM3"))
        dd_scale_port = ft.Dropdown(
            label="COM-порт весов",
            width=420,
            value=_val,
            options=_opts,
        )
        tf_scale_baud = bind_virtual_keyboard(
            ft.TextField(
                label="Скорость (бод)",
                value=str(cur.get("scale_baud") or 9600),
                dense=True,
                width=200,
            ),
            mode="numeric",
            title="Скорость весов",
            submit_text="Готово",
        )
        tf_scale_lpt = bind_virtual_keyboard(
            ft.TextField(
                label="LPT для тестовой печати веса",
                value=str(cur.get("scale_lpt") or "LPT1"),
                hint_text="Обычно LPT1 на моноблоке",
                dense=True,
                width=420,
            ),
            mode="lpt",
            title="LPT для веса",
            submit_text="Готово",
        )

        def refresh_ports(_e=None):
            keep = (dd_scale_port.value or "").strip() or str(cur.get("scale_port") or "COM3")
            new_opts, new_val = _build_scale_port_options(keep)
            dd_scale_port.options = new_opts
            dd_scale_port.value = new_val
            page.update()

        def collect_scale() -> dict[str, Any]:
            return {
                "scale_port": (dd_scale_port.value or "COM3").strip() or "COM3",
                "scale_baud": _parse_int(tf_scale_baud.value or "9600", 9600),
                "scale_lpt": (tf_scale_lpt.value or "LPT1").strip() or "LPT1",
            }

        def do_save(_e):
            try:
                printer_config.save(collect_scale())
                dismiss_dialog()
                restart_scale_manager()
                snack("Настройки весов сохранены, COM переподключён", ft.Colors.GREEN_700)
            except OSError as ex:
                snack(f"Не удалось записать файл: {ex}", ft.Colors.RED_700)

        dlg = ft.AlertDialog(
            modal=True,
            bgcolor=UI_SURFACE,
            shape=ft.RoundedRectangleBorder(radius=16),
            title=ft.Row(
                [
                    ft.Icon(ft.Icons.SCALE_OUTLINED, color=UI_ACCENT_DIM),
                    ft.Text(
                        "Весы (COM)",
                        size=20,
                        weight=ft.FontWeight.W_600,
                        color=UI_TEXT,
                    ),
                ],
                spacing=8,
            ),
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text(
                            f"Данные в: {printer_config.settings_path()} (общий файл с кэшем товаров)",
                            size=11,
                            color=UI_MUTED,
                            selectable=True,
                        ),
                        ft.Text(
                            "COM принтера и COM весов должны быть разными. Лог обмена: scale_com.log рядом с приложением.",
                            size=12,
                            color=UI_MUTED,
                        ),
                        ft.Row(
                            [
                                dd_scale_port,
                                ft.IconButton(
                                    ft.Icons.REFRESH,
                                    tooltip="Обновить список COM-портов",
                                    icon_color=UI_TEXT,
                                    style=ft.ButtonStyle(bgcolor=UI_SURFACE_ELEV),
                                    on_click=refresh_ports,
                                ),
                            ],
                            spacing=4,
                            vertical_alignment=ft.CrossAxisAlignment.START,
                        ),
                        tf_scale_baud,
                        tf_scale_lpt,
                    ],
                    tight=True,
                    scroll=ft.ScrollMode.AUTO,
                    width=460,
                ),
                padding=ft.Padding.only(right=8),
            ),
            actions=[
                ft.TextButton("Отмена", on_click=dismiss_dialog),
                ft.FilledButton(
                    "Сохранить",
                    icon=ft.Icons.SAVE_OUTLINED,
                    style=ft.ButtonStyle(bgcolor=UI_ACCENT, color=UI_TEXT_ON_YELLOW),
                    on_click=do_save,
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    def open_brand_settings_menu(_):
        def close_menu(_e=None):
            page.pop_dialog()
            page.update()

        def pick_printer(_e):
            page.pop_dialog()
            page.update()
            open_printer_settings_dlg(_e)

        def pick_scales(_e):
            page.pop_dialog()
            page.update()
            open_scale_settings_dlg(_e)

        def pick_logout(_e):
            page.pop_dialog()
            page.update()
            logout(_e)

        def on_fullscreen_change(e: ft.ControlEvent):
            page.window.full_screen = bool(e.control.value)
            page.update()

        def add_autostart(_e):
            ok, msg = install_windows_autostart_shortcut()
            if ok:
                snack("Автозагрузка: ярлык создан в папке «Автозагрузка»", ft.Colors.GREEN_700)
            else:
                snack(f"Автозагрузка: {msg}", ft.Colors.RED_700)

        menu_rows: list[ft.Control] = [
            ft.ListTile(
                leading=ft.Icon(ft.Icons.PRINT_OUTLINED, color=UI_TEXT),
                title=ft.Text("Принтер"),
                on_click=pick_printer,
            ),
        ]
        if scale_feature_enabled:
            menu_rows.append(
                ft.ListTile(
                    leading=ft.Icon(ft.Icons.SCALE_OUTLINED, color=UI_TEXT),
                    title=ft.Text("Весы (COM)"),
                    on_click=pick_scales,
                )
            )
        menu_rows.extend(
            [
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=8, vertical=4),
                    content=ft.Switch(
                        label="Полноэкранный режим",
                        value=page.window.full_screen,
                        on_change=on_fullscreen_change,
                    ),
                ),
                ft.ListTile(
                    leading=ft.Icon(ft.Icons.START, color=UI_TEXT),
                    title=ft.Text("Автозагрузка Windows"),
                    subtitle=ft.Text(
                        "Ярлык в меню Пуск → Программы → Автозагрузка",
                        size=11,
                        color=UI_MUTED,
                    ),
                    on_click=add_autostart,
                ),
                ft.ListTile(
                    leading=ft.Icon(ft.Icons.LOGOUT, color=ft.Colors.RED_700),
                    title=ft.Text("Выйти из аккаунта", color=ft.Colors.RED_700),
                    on_click=pick_logout,
                ),
            ]
        )

        dlg = ft.AlertDialog(
            modal=True,
            bgcolor=UI_SURFACE,
            shape=ft.RoundedRectangleBorder(radius=16),
            title=ft.Row(
                [
                    ft.Icon(ft.Icons.SETTINGS_OUTLINED, color=UI_ACCENT),
                    ft.Text(
                        "Настройки",
                        size=20,
                        weight=ft.FontWeight.W_600,
                        color=UI_TEXT,
                    ),
                ],
                spacing=8,
            ),
            content=ft.Container(
                content=ft.Column(
                    menu_rows,
                    tight=True,
                    spacing=0,
                    scroll=ft.ScrollMode.AUTO,
                ),
                width=420,
            ),
            actions=[
                ft.TextButton("Закрыть", on_click=close_menu),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    def build_loading_overlay() -> ft.Container:
        return ft.Container(
            ref=loading_overlay,
            visible=False,
            bgcolor=ft.Colors.with_opacity(0.55, "#000000"),
            alignment=ft.Alignment.CENTER,
            content=ft.ProgressRing(width=48, height=48, color=UI_ACCENT, stroke_width=3),
            expand=True,
        )

    def build_cashier() -> ft.Stack:
        u = client.user_payload
        name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or u.get("email", "—")
        company = u.get("company") or "—"
        role = u.get("role") or "—"
        branch_ids = u.get("branch_ids") or []
        branch_label = str(client.active_branch_id or "—")

        branch_ctrl = None
        if isinstance(branch_ids, list) and len(branch_ids) > 1:

            def on_branch(e):
                client.active_branch_id = e.control.value
                session["quick_catalog_products"] = {"kg": [], "common": []}
                session["quick_catalog_loading"] = {"kg": False, "common": False}
                session["quick_catalog_loaded"] = {"kg": False, "common": False}
                active_tab = str(session.get("quick_catalog_tab") or "kg")
                _render_quick_catalog([], active_tab)
                page.run_task(_load_quick_catalog, active_tab, True)
                try_start_sale()

            branch_ctrl = ft.Dropdown(
                label="Филиал",
                width=280,
                value=str(client.active_branch_id) if client.active_branch_id else None,
                options=[ft.dropdown.Option(key=str(b), text=str(b)[:12] + "…") for b in branch_ids],
                on_change=on_branch,
            )

        banner = ft.Container(
            ref=shift_banner,
            visible=False,
            padding=14,
            bgcolor=UI_WARN_BG,
            border_radius=12,
            border=ft.Border.all(1, UI_WARN_BORDER),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color=UI_WARN_TEXT),
                            ft.Text(
                                "Смена не открыта. Сначала откройте смену на кассе.",
                                expand=True,
                                color=UI_WARN_TEXT,
                                size=14,
                            ),
                            ft.FilledButton(
                                "Открыть смену",
                                style=ft.ButtonStyle(bgcolor=UI_ACCENT, color=UI_TEXT_ON_YELLOW),
                                on_click=open_shift_dlg,
                            ),
                        ],
                        spacing=12,
                    ),
                ],
                tight=True,
            ),
        )

        search_row = ft.Row(
            [
                bind_virtual_keyboard(
                    ft.TextField(
                        ref=search_field_ref,
                        label="Поиск по названию",
                        hint_text="Живой поиск от 2 символов",
                        dense=True,
                        expand=True,
                        border_radius=10,
                        filled=True,
                        on_change=on_search_change,
                        on_submit=on_search_submit,
                    ),
                    mode="text",
                    title="Поиск товаров",
                    submit=on_search_submit,
                    submit_text="Найти",
                ),
                ft.IconButton(
                    ft.Icons.SEARCH,
                    tooltip="Найти",
                    icon_color=UI_TEXT_ON_YELLOW,
                    style=ft.ButtonStyle(bgcolor=UI_ACCENT),
                    on_click=on_search_submit,
                ),
            ],
            spacing=8,
        )

        search_results = ft.Column(
            ref=search_results_ref,
            spacing=6,
            scroll=ft.ScrollMode.AUTO,
        )

        quick_catalog_tabs = ft.SegmentedButton(
            segments=[
                ft.Segment(
                    value="kg",
                    label=ft.Text("По кг", size=12),
                    icon=ft.Icons.SCALE_OUTLINED,
                ),
                ft.Segment(
                    value="common",
                    label=ft.Text("Обычные", size=12),
                    icon=ft.Icons.GRID_VIEW_OUTLINED,
                ),
            ],
            selected=[str(session.get("quick_catalog_tab") or "kg")],
            show_selected_icon=False,
            style=ft.ButtonStyle(
                color={
                    ft.ControlState.DEFAULT: UI_TEXT,
                    ft.ControlState.SELECTED: UI_TEXT_ON_YELLOW,
                },
                bgcolor={
                    ft.ControlState.DEFAULT: UI_SURFACE_ELEV,
                    ft.ControlState.SELECTED: UI_ACCENT,
                },
                side=ft.BorderSide(1, UI_BORDER),
                shape=ft.RoundedRectangleBorder(radius=10),
            ),
            on_change=on_quick_catalog_tab_change,
        )

        scan_block = ft.Column(
            [
                ft.Row(
                    [
                        ft.Icon(ft.Icons.BARCODE_READER, size=18, color=UI_ACCENT_DIM),
                        ft.Text(
                            "Скан: символы подряд + Enter. Способ оплаты — в окне после «ОПЛАТИТЬ».",
                            size=11,
                            color=UI_MUTED,
                            expand=True,
                        ),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                search_row,
                quick_catalog_tabs,
            ],
            spacing=8,
        )

        search_list_panel = ft.Container(
            content=search_results,
            expand=True,
            bgcolor=UI_SURFACE_ELEV,
            border=ft.Border.all(1, UI_BORDER),
            border_radius=15,
            padding=4,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

        cart_col = ft.ListView(
            ref=cart_items_col,
            expand=True,
            spacing=0,
            padding=ft.Padding.symmetric(horizontal=0, vertical=2),
        )

        receipt = ft.Container(
            content=ft.Column(
                [
                    ft.Text("Оплата", size=11, weight=ft.FontWeight.W_600, color=UI_MUTED),
                    ft.Text(ref=status_chip, value="—", size=10, color=UI_MUTED),
                    ft.Divider(height=1, color=UI_BORDER),
                    ft.Row(
                        [
                            ft.Text("Подытог", size=12, color=UI_MUTED),
                            ft.Text(ref=subtotal_txt, value="0.00 сом", size=12, color=UI_TEXT),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Row(
                        [
                            ft.Text("Скидка", size=12, color=UI_MUTED),
                            ft.Text(ref=discount_txt, value="0.00 сом", size=12, color=UI_TEXT),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Container(
                        content=ft.Column(
                            [
                                ft.Text(
                                    "К оплате",
                                    size=11,
                                    weight=ft.FontWeight.W_600,
                                    color=UI_MUTED,
                                ),
                                ft.Text(
                                    ref=total_txt,
                                    value="0.00 сом",
                                    size=28,
                                    weight=ft.FontWeight.W_800,
                                    color=UI_ACCENT,
                                ),
                            ],
                            spacing=4,
                            tight=True,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        padding=ft.Padding.symmetric(horizontal=12, vertical=14),
                        bgcolor=UI_ICON_BADGE_BG,
                        border_radius=15,
                        border=ft.Border.all(1, UI_BORDER),
                    ),
                    ft.Divider(height=1, color=UI_BORDER),
                    ft.Text("Скидка на чек", size=11, color=UI_TEXT),
                    ft.Text("% или сумма", size=9, color=UI_MUTED),
                    ft.Row(
                        [
                            bind_virtual_keyboard(
                                ft.TextField(
                                    ref=order_discount_pct_ref,
                                    label="%",
                                    hint_text="10",
                                    dense=True,
                                    expand=True,
                                    on_submit=apply_order_discount,
                                ),
                                mode="numeric",
                                title="Скидка в процентах",
                                submit=apply_order_discount,
                                submit_text="Применить",
                            ),
                            bind_virtual_keyboard(
                                ft.TextField(
                                    ref=order_discount_sum_ref,
                                    label="Сумма",
                                    hint_text="50",
                                    dense=True,
                                    expand=True,
                                    on_submit=apply_order_discount,
                                ),
                                mode="numeric",
                                title="Скидка суммой",
                                submit=apply_order_discount,
                                submit_text="Применить",
                            ),
                        ],
                        spacing=6,
                    ),
                    ft.Row(
                        [
                            ft.OutlinedButton(
                                "Применить",
                                icon=ft.Icons.DISCOUNT,
                                on_click=apply_order_discount,
                                expand=True,
                            ),
                        ],
                    ),
                    ft.TextButton(
                        "Сбросить скидку",
                        icon=ft.Icons.CLEAR,
                        icon_color=UI_MUTED,
                        style=ft.ButtonStyle(color=UI_MUTED),
                        on_click=clear_order_discount,
                    ),
                    ft.Container(expand=True),
                    ft.FilledButton(
                        "ОПЛАТИТЬ",
                        icon=ft.Icons.PAYMENTS,
                        style=ft.ButtonStyle(
                            bgcolor=UI_ACCENT,
                            color=UI_TEXT_ON_YELLOW,
                            shape=ft.RoundedRectangleBorder(radius=15),
                        ),
                        on_click=checkout_click,
                        width=float("inf"),
                        height=56,
                    ),
                ],
                expand=True,
                spacing=6,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
            padding=14,
            bgcolor=UI_SURFACE,
            border_radius=15,
            border=ft.Border.all(1, UI_BORDER),
        )

        scale_weight_panel = None
        if scale_feature_enabled:
            scale_weight_panel = ft.Container(
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Icon(ft.Icons.SCALE_OUTLINED, size=16, color=UI_ACCENT_DIM),
                                ft.Text(
                                    "Весы",
                                    size=11,
                                    weight=ft.FontWeight.W_600,
                                    color=UI_MUTED,
                                ),
                                ft.Container(expand=True),
                                ft.IconButton(
                                    ft.Icons.PRINT_OUTLINED,
                                    tooltip="Печать веса на LPT",
                                    icon_color=UI_ACCENT_DIM,
                                    style=ft.ButtonStyle(bgcolor=UI_SURFACE),
                                    on_click=on_print_weight_click,
                                ),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        ft.Text(ref=weight_scale_text, value="—", size=20, weight=ft.FontWeight.W_600, color=UI_TEXT),
                        ft.Text(ref=weight_scale_status, value="", size=9, color=UI_MUTED),
                    ],
                    tight=True,
                    spacing=4,
                ),
                padding=10,
                bgcolor=UI_SURFACE_ELEV,
                border_radius=12,
                border=ft.Border.all(1, UI_BORDER),
            )

        col_left_children: list = [banner]
        if scale_weight_panel is not None:
            col_left_children.append(scale_weight_panel)
        col_left_children.extend(
            [
                ft.Text("Поиск товаров", size=11, weight=ft.FontWeight.W_600, color=UI_MUTED),
                ft.Text(
                    ref=cashier_hint_ref,
                    value="",
                    size=12,
                    color=UI_WARN_TEXT,
                    visible=False,
                ),
                scan_block,
                search_list_panel,
            ]
        )

        col_left = ft.Container(
            content=ft.Column(
                col_left_children,
                expand=True,
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
            expand=True,
            padding=12,
            bgcolor=UI_SURFACE,
            border_radius=15,
            border=ft.Border.all(1, UI_BORDER),
            shadow=_COLUMN_CARD_SHADOW,
        )

        col_mid = ft.Container(
            content=ft.Column(
                [
                    ft.Text(
                        "Текущий чек",
                        size=11,
                        weight=ft.FontWeight.W_600,
                        color=UI_MUTED,
                    ),
                    ft.Container(
                        content=cart_col,
                        expand=True,
                        bgcolor=UI_SURFACE_ELEV,
                        border=ft.Border.all(1, UI_BORDER),
                        border_radius=15,
                        padding=8,
                        clip_behavior=ft.ClipBehavior.HARD_EDGE,
                    ),
                ],
                expand=True,
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
            expand=True,
            padding=12,
            bgcolor=UI_SURFACE,
            border_radius=15,
            border=ft.Border.all(1, UI_BORDER),
            shadow=_COLUMN_CARD_SHADOW,
        )

        col_pay = ft.Container(
            content=receipt,
            expand=True,
            padding=0,
            shadow=_COLUMN_CARD_SHADOW,
        )

        _hdr_btns: list[ft.Control] = [
            ft.FilledButton(
                "Начать продажу",
                icon=ft.Icons.PLAY_CIRCLE_OUTLINED,
                tooltip="Новая сессия корзины (POST pos/sales/start)",
                style=ft.ButtonStyle(
                    bgcolor=UI_ACCENT,
                    color=UI_TEXT_ON_YELLOW,
                    shape=ft.RoundedRectangleBorder(radius=10),
                ),
                on_click=lambda e: try_start_sale(),
            ),
            ft.OutlinedButton(
                "Закрыть смену",
                icon=ft.Icons.LOCK_CLOCK,
                tooltip="Закрыть текущую смену кассира",
                style=ft.ButtonStyle(
                    color=UI_MUTED,
                    side=ft.BorderSide(1, UI_BORDER),
                    shape=ft.RoundedRectangleBorder(radius=10),
                ),
                on_click=close_shift_click,
            ),
        ]
        header_toolbar = ft.Row(_hdr_btns, spacing=8, tight=True)

        company_short = (str(company)[:28] + "…") if len(str(company)) > 30 else str(company)
        header_left = ft.Row(
            [
                ft.Text(
                    "NUR · Касса",
                    size=12,
                    weight=ft.FontWeight.W_600,
                    color=UI_MUTED,
                ),
                ft.Text(company_short, size=10, color=UI_MUTED),
                branch_ctrl if branch_ctrl else ft.Container(),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        header = ft.Container(
            content=ft.Row(
                [
                    header_left,
                    ft.Container(expand=True),
                    ft.Column(
                        [
                            ft.Text(name, size=9, color=UI_MUTED, text_align=ft.TextAlign.RIGHT),
                            ft.Text(role, size=9, color=UI_MUTED, text_align=ft.TextAlign.RIGHT),
                            ft.Text(
                                f"Филиал: {branch_label[:18]}…"
                                if len(str(branch_label)) > 20
                                else f"Филиал: {branch_label}",
                                size=9,
                                color=UI_MUTED,
                                text_align=ft.TextAlign.RIGHT,
                            ),
                        ],
                        spacing=0,
                        tight=True,
                        horizontal_alignment=ft.CrossAxisAlignment.END,
                    ),
                    header_toolbar,
                    ft.IconButton(
                        ft.Icons.LOGOUT,
                        tooltip="Выйти из аккаунта",
                        icon_color=UI_MUTED,
                        style=ft.ButtonStyle(bgcolor=UI_SURFACE_ELEV),
                        on_click=logout,
                    ),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            bgcolor=UI_SURFACE,
            border=ft.Border.only(
                bottom=ft.BorderSide(1, UI_BORDER),
            ),
        )

        three_columns = ft.Row(
            [col_left, col_mid, col_pay],
            expand=True,
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )
        main_stack = ft.Column(
            [
                header,
                ft.Container(
                    content=three_columns,
                    expand=True,
                    padding=ft.Padding.symmetric(horizontal=8, vertical=10),
                    bgcolor=UI_BG,
                ),
            ],
            expand=True,
            spacing=0,
        )

        top_brand_bar = ft.Container(
            height=52,
            bgcolor=UI_SIDEBAR,
            padding=ft.Padding.symmetric(horizontal=14, vertical=6),
            content=ft.Row(
                [
                    ft.Container(
                        width=40,
                        height=40,
                        bgcolor=UI_ACCENT,
                        border_radius=10,
                        alignment=ft.Alignment.CENTER,
                        content=ft.Icon(
                            ft.Icons.STOREFRONT_OUTLINED,
                            color=UI_TEXT_ON_YELLOW,
                            size=22,
                        ),
                    ),
                    ft.Container(width=12),
                    ft.Column(
                        [
                            ft.Text(
                                "NUR CRM",
                                size=13,
                                weight=ft.FontWeight.W_800,
                                color=UI_SIDEBAR_TEXT,
                                height=18,
                            ),
                            ft.Text(
                                "Касса",
                                size=10,
                                weight=ft.FontWeight.W_500,
                                color=UI_SIDEBAR_MUTED,
                                height=14,
                            ),
                        ],
                        spacing=0,
                        tight=True,
                        alignment=ft.MainAxisAlignment.CENTER,
                    ),
                    ft.Container(width=8),
                    ft.IconButton(
                        icon=ft.Icons.SETTINGS_OUTLINED,
                        icon_size=22,
                        icon_color=UI_SIDEBAR_TEXT,
                        tooltip="Настройки: принтер, весы, автозагрузка, выход",
                        style=ft.ButtonStyle(
                            bgcolor=ft.Colors.TRANSPARENT,
                            overlay_color=ft.Colors.with_opacity(0.12, "#FFFFFF"),
                        ),
                        on_click=open_brand_settings_menu,
                    ),
                    ft.Container(expand=True),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        return ft.Stack(
            [
                ft.Column(
                    [
                        top_brand_bar,
                        ft.Container(content=main_stack, expand=True, bgcolor=UI_BG),
                    ],
                    expand=True,
                    spacing=0,
                ),
                build_loading_overlay(),
            ],
            expand=True,
        )

    def _on_page_disconnect(_):
        hide_virtual_keyboard()
        mgr = scale_state.get("mgr")
        if mgr is not None:
            try:
                mgr.stop()
            except Exception:
                pass
            scale_state["mgr"] = None

    page.on_disconnect = _on_page_disconnect

    page.add(ft.Stack([build_login(), build_loading_overlay()], expand=True))

    if sys.platform == "win32":

        async def _win_center_window():
            try:
                if page.window.full_screen:
                    return
                await page.window.wait_until_ready_to_show()
                await page.window.center()
            except Exception:
                pass

        page.run_task(_win_center_window)


if __name__ == "__main__":
    _windows_pre_ui_init()
    try:
        import flet_desktop  # noqa: F401 — рантайм окна; версия должна совпадать с flet
    except ImportError:
        fv = getattr(ft, "__version__", "0.82.2")
        print(
            "Не установлен пакет flet-desktop (нужен для окна на ПК).\n"
            f"Выполните:  pip install -r requirements.txt\n"
            f"или:        pip install flet-desktop=={fv}"
        )
        raise SystemExit(1) from None
    ft.run(main)
