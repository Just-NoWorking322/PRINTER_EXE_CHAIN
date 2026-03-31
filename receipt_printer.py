"""
Печать чека на Windows: либо через системный GDI-принтер, либо raw-печать в LPT/USB-принтер.

Для raw-печати доступны два режима:
- ESC/POS raw: печатаем командами ESC/POS, выбираем codepage и при необходимости ESC t вручную.
- Plain text raw: отправляем только текст в выбранной кодировке, без ESC-команд.

Это позволяет запускать кассу не только с типовыми 58 мм ESC/POS-принтерами,
но и с USB-моделями 80 мм, которым нужен raw ESC/POS через Windows spooler.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
import time
from typing import Any

from receipt_lpt import (
    describe_codepage_plan,
    print_probe_via_lpt,
    print_rows_via_lpt,
    print_text_probe_via_lpt,
)

logger = logging.getLogger(__name__)


class ReceiptPrinterError(Exception):
    """Ошибка подключения или печати на принтере."""


def _usb_log_path() -> Path:
    from printer_config import settings_path

    return settings_path().parent / "usb_print.log"


def _append_usb_log(message: str) -> None:
    try:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}\n"
        with _usb_log_path().open("a", encoding="utf-8", errors="replace") as f:
            f.write(line)
    except OSError:
        pass


def _money(v: Any) -> str:
    try:
        if v is None:
            return "0.00"
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "0.00"


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


def _item_title(it: dict[str, Any]) -> str:
    """Имя позиции как на экране кассы: product → product_snapshot → поля строки."""
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


def _line_amount(it: dict[str, Any]) -> str:
    keys = (
        "line_total",
        "line_total_amount",
        "line_amount",
        "amount",
        "total",
        "sum",
        "total_price",
        "subtotal",
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
        disc = float(it.get("discount_total") or it.get("line_discount") or it.get("discount") or 0)
        if q > 0 and up >= 0:
            return _money(q * up - disc)
    except (TypeError, ValueError):
        pass
    return "0.00"


# Ширина строки для 80 мм XPrinter (Font A ~48 символов)
RECEIPT_WIDTH = 48


def _dashed_rule(width: int = RECEIPT_WIDTH) -> str:
    """Пунктирная линия как на типовом товарном чеке."""
    s = ("- " * ((width // 2) + 2))[:width]
    return s if s else "-" * width


def _money_display(v: Any) -> str:
    """Сумма с запятой как десятичный разделитель (как на этикетках)."""
    return _money(v).replace(".", ",")


def _qty_display(q: Any) -> str:
    try:
        f = float(q)
        if abs(f - int(f)) < 1e-9:
            return str(int(f))
        s = f"{f:.6f}".rstrip("0").rstrip(".")
        return s if s else "0"
    except (TypeError, ValueError):
        return str(q or "0")


def _wrap_to_width(text: str, width: int = RECEIPT_WIDTH) -> list[str]:
    t = (text or "").strip()
    if not t:
        return []
    out: list[str] = []
    while len(t) > width:
        out.append(t[:width])
        t = t[width:]
    out.append(t)
    return out


def _pad_left_right(left: str, right: str, width: int = RECEIPT_WIDTH) -> str:
    left = (left or "").strip()
    right = (right or "").strip()
    if len(left) + len(right) + 1 > width:
        left = left[: max(1, width - len(right) - 1)]
    gap = width - len(left) - len(right)
    if gap < 1:
        return left[: width - 1]
    return left + (" " * gap) + right


def _pad_center(text: str, width: int = RECEIPT_WIDTH) -> str:
    """Центр без ESC a 1: на части Cashino/EP-200 после выравнивания по центру ломается кириллица."""
    t = (text or "").strip()
    if not t:
        return " " * width
    if len(t) >= width:
        return t[:width]
    pad = width - len(t)
    left = pad // 2
    return (" " * left + t + " " * (pad - left))[:width]


def _receipt_safe_chars(text: str) -> str:
    """U+2116 № в слотах 17/46 часто нет или портит поток; заменяем на латиницу."""
    if not text:
        return text
    return text.replace("\u2116", "N").replace("№", "N")


def _receipt_upper(text: str) -> str:
    """Чек печатаем заглавными буквами для более читаемого вида на термоленте."""
    if not text:
        return text
    return str(text).upper()


def _raw_esc_bold(p: Any, on: bool) -> None:
    p._raw(b"\x1b\x45" + bytes([1 if on else 0]))


def _raw_esc_align(p: Any, mode: int) -> None:
    """0=left, 1=center, 2=right."""
    p._raw(b"\x1b\x61" + bytes([mode & 0xFF]))


def _emit_line_encoded(p: Any, codec: str, text: str, user_enc: str = "") -> None:
    """Строгая таблица ESC/POS: символы вне кодировки заменяются, печать не обрывается."""
    text = _receipt_upper(_receipt_safe_chars(str(text)))
    if user_enc:
        _touch_codepage_after_esc_a(p, user_enc)
    try:
        p._raw(text.encode(codec, errors="replace") + b"\n")
    except LookupError:
        p._raw(text.encode("cp866", errors="replace") + b"\n")


def _looks_like_amount(text: str) -> bool:
    t = text.strip().replace(" ", "").replace(",", ".").replace("–", "-")
    if not t:
        return False
    try:
        float(t)
        return True
    except ValueError:
        return False


ReceiptRow = tuple[Any, ...]


def _emit_receipt_rows(
    p: Any, codec: str, rows: list[ReceiptRow], user_enc: str = ""
) -> None:
    """Печать строк чека: жирный/центр через сырые ESC (без p.set — не сбрасывать кодовую страницу)."""
    for row in rows:
        if not row:
            continue
        kind = row[0]
        if kind == "sep":
            _raw_esc_align(p, 0)
            _raw_esc_bold(p, False)
            _emit_line_encoded(p, codec, _dashed_rule(), user_enc)
        elif kind == "sep_solid":
            _raw_esc_align(p, 0)
            _raw_esc_bold(p, False)
            _emit_line_encoded(p, codec, "-" * RECEIPT_WIDTH, user_enc)
        elif kind == "blank":
            p._raw(b"\n")
        elif kind == "center_bold":
            _raw_esc_align(p, 0)
            _raw_esc_bold(p, True)
            _emit_line_encoded(p, codec, _pad_center(str(row[1])), user_enc)
            _raw_esc_bold(p, False)
        elif kind == "center":
            _raw_esc_align(p, 0)
            _raw_esc_bold(p, False)
            _emit_line_encoded(p, codec, _pad_center(str(row[1])), user_enc)
        elif kind == "left":
            _raw_esc_align(p, 0)
            _raw_esc_bold(p, False)
            _emit_line_encoded(p, codec, str(row[1]), user_enc)
        elif kind == "lr":
            # На EP-200 обычный шрифт иногда даёт другую интерпретацию байт; жирный совпадает с «ИТОГ».
            _raw_esc_align(p, 0)
            _raw_esc_bold(p, True)
            _emit_line_encoded(p, codec, _pad_left_right(str(row[1]), str(row[2])), user_enc)
            _raw_esc_bold(p, False)
        elif kind == "lr_bold":
            _raw_esc_align(p, 0)
            _raw_esc_bold(p, True)
            _emit_line_encoded(p, codec, _pad_left_right(str(row[1]), str(row[2])), user_enc)
            _raw_esc_bold(p, False)
        elif kind == "right":
            _raw_esc_align(p, 2)
            _raw_esc_bold(p, False)
            _emit_line_encoded(p, codec, str(row[1]), user_enc)
            _raw_esc_align(p, 0)
            if user_enc:
                _touch_codepage_after_esc_a(p, user_enc)
        else:
            _raw_esc_align(p, 0)
            _emit_line_encoded(p, codec, str(row[-1]), user_enc)


def _plain_lines_to_receipt_rows(lines: list[str]) -> list[ReceiptRow]:
    """Эвристика для текста чека с API: разделители, заголовок, метка: сумма, итоги."""
    rows: list[ReceiptRow] = []
    first_content = True
    for line in lines:
        s = line.rstrip()
        if not s:
            rows.append(("blank",))
            continue
        if len(s) >= 8 and set(s) <= set("-–._="):
            rows.append(("sep",))
            continue
        if re.match(r"^\s*ЧЕК\s*$", s, re.IGNORECASE):
            rows.append(("center_bold", s))
            first_content = False
            continue
        if "спасибо" in s.lower():
            rows.append(("center_bold", s))
            first_content = False
            continue
        if ":" in s:
            left, right = s.rsplit(":", 1)
            rl = left.strip()
            rr = right.strip()
            if _looks_like_amount(rr) and len(rl) <= 28:
                lu = rl.upper()
                if lu.startswith(("ИТОГО", "ИТОГ", "СУММА", "СДАЧА")) or "ПОЛУЧЕНО" in lu:
                    rows.append(("lr_bold", rl + ":", rr))
                else:
                    rows.append(("lr", rl + ":", rr))
                first_content = False
                continue
        if first_content and len(s) <= 40 and not re.search(r"\s[x×]\s", s, re.I):
            rows.append(("center_bold", s))
            first_content = False
            continue
        rows.append(("left", s))
        first_content = False
    return rows


def _compose_sale_receipt_rows(
    *,
    cart: dict[str, Any],
    payment_method: str,
    cash_received: str | None,
    change_amount: Any,
    sale_id: Any,
    company: str = "",
    cashier: str = "",
) -> list[ReceiptRow]:
    """Макет как у типового товарного чека: магазин, дата, позиции, оплата, продавец."""
    items = cart.get("items") or cart.get("cart_items") or []
    if not isinstance(items, list):
        items = []
    valid_items = [it for it in items if isinstance(it, dict)]

    st = cart.get("subtotal")
    disc = cart.get("discount_total")
    tot = _cart_total(cart)

    company_clean = (company or "").strip()
    if company_clean in ("—", "-", "none", "null"):
        company_clean = ""

    rows: list[ReceiptRow] = []

    if company_clean:
        for ln in _wrap_to_width(company_clean, RECEIPT_WIDTH):
            rows.append(("center_bold", ln))
        rows.append(("blank",))

    rows.append(("center", "Добро пожаловать!"))
    rows.append(("blank",))

    if sale_id is not None and str(sale_id).strip():
        sid = str(sale_id).strip()
        sid_show = sid if len(sid) <= 14 else (sid[:12] + "…")
        rows.append(("center_bold", f"Товарный чек № {sid_show}"))
    else:
        rows.append(("center_bold", "Товарный чек"))
    rows.append(("center", f"от {datetime.now().strftime('%d.%m.%Y %H:%M')}"))
    rows.append(("sep",))

    first_line = True
    for idx, it in enumerate(valid_items, start=1):
        if not first_line:
            rows.append(("blank",))
        first_line = False
        title = _item_title(it)
        block = _wrap_to_width(f"{idx}. {title}", RECEIPT_WIDTH)
        for ln in block:
            rows.append(("left", ln))
        up = _money_display(it.get("unit_price"))
        qty = _qty_display(it.get("quantity", "1")).replace(".", ",")
        lam = _money_display(_line_amount(it))
        calc = f"{up} x {qty} = {lam}"
        rows.append(("right", calc))

    rows.append(("sep",))

    try:
        dval = float(disc) if disc is not None else 0.0
    except (TypeError, ValueError):
        dval = 0.0
    rows.append(("lr", "Подытог:", _money_display(st)))
    if abs(dval) > 0.001:
        rows.append(("lr", "Скидка:", _money_display(abs(dval))))

    pm_label = (
        "Наличные"
        if payment_method == "cash"
        else ("Безнал" if payment_method == "transfer" else str(payment_method))
    )
    rows.append(("lr", pm_label, _money_display(tot)))
    rows.append(("lr_bold", "ИТОГ:", _money_display(tot)))
    if payment_method == "cash" and cash_received:
        rows.append(("lr", "Получено:", _money_display(cash_received)))
    if change_amount is not None:
        try:
            chf = float(change_amount)
        except (TypeError, ValueError):
            chf = None
        if chf is not None:
            rows.append(("lr", "Сдача:", _money_display(chf)))

    rows.append(("sep",))

    cashier_clean = (cashier or "").strip()
    if cashier_clean and cashier_clean not in ("—", "-", "none", "null"):
        rows.append(("left", f"Продавец: {cashier_clean}"))
        rows.append(("blank",))

    rows.append(("center_bold", "Спасибо за покупку!"))
    if company_clean:
        rows.append(("center", company_clean))
    rows.append(("blank",))
    return rows


def _cart_total(cart: dict[str, Any]) -> float:
    keys = (
        "total",
        "grand_total",
        "total_amount",
        "amount_due",
        "payable_total",
        "order_total",
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


def is_receipt_printing_enabled() -> bool:
    from config import (
        RECEIPT_FILE_PATH,
        RECEIPT_GDI_PRINTER_NAME,
        RECEIPT_PRINT_MODE,
        RECEIPT_PRINTER_BACKEND,
        RECEIPT_USB_PORT_NAME,
        RECEIPT_USB_PRINTER_NAME,
    )

    if (RECEIPT_PRINT_MODE or "lpt").strip().lower() == "gdi":
        return bool((RECEIPT_GDI_PRINTER_NAME or "").strip())
    b = (RECEIPT_PRINTER_BACKEND or "").strip().lower()
    if b == "usb":
        return bool((RECEIPT_USB_PRINTER_NAME or RECEIPT_USB_PORT_NAME or RECEIPT_FILE_PATH or "").strip())
    return b == "lpt" and bool((RECEIPT_FILE_PATH or "").strip())


def _use_gdi_print() -> bool:
    from config import RECEIPT_PRINT_MODE

    return (RECEIPT_PRINT_MODE or "lpt").strip().lower() == "gdi"


def _current_lpt_settings() -> dict[str, Any]:
    from config import (
        RECEIPT_ESC_R_BYTE,
        RECEIPT_ESCPOS_PROFILE,
        RECEIPT_ESCPOS_TABLE_BYTE,
        RECEIPT_FILE_PATH,
        RECEIPT_LPT_DRIVER,
        RECEIPT_LPT_LINE_ENDING,
        RECEIPT_PRINTER_BACKEND,
        RECEIPT_TEXT_ENCODING,
        RECEIPT_USB_DEVICE_ID,
        RECEIPT_USB_FRIENDLY_NAME,
        RECEIPT_USB_PORT_NAME,
        RECEIPT_USB_PRINTER_NAME,
        RECEIPT_USB_PRODUCT_ID,
        RECEIPT_USB_VENDOR_ID,
    )

    return {
        "backend": (RECEIPT_PRINTER_BACKEND or "lpt").strip().lower() or "lpt",
        "devfile": (
            (
                RECEIPT_USB_PRINTER_NAME
                or RECEIPT_USB_PORT_NAME
                or RECEIPT_FILE_PATH
            )
            if (RECEIPT_PRINTER_BACKEND or "").strip().lower() == "usb"
            else RECEIPT_FILE_PATH
        ).strip()
        or ("USB001" if (RECEIPT_PRINTER_BACKEND or "").strip().lower() == "usb" else "LPT1"),
        "usb_device_id": (RECEIPT_USB_DEVICE_ID or "").strip(),
        "usb_printer_name": (RECEIPT_USB_PRINTER_NAME or "").strip(),
        "usb_friendly_name": (RECEIPT_USB_FRIENDLY_NAME or "").strip(),
        "usb_port_name": (RECEIPT_USB_PORT_NAME or "").strip(),
        "usb_vendor_id": (RECEIPT_USB_VENDOR_ID or "").strip(),
        "usb_product_id": (RECEIPT_USB_PRODUCT_ID or "").strip(),
        "lpt_driver": (RECEIPT_LPT_DRIVER or "text").strip().lower(),
        "line_ending": (RECEIPT_LPT_LINE_ENDING or "crlf").strip().lower(),
        "text_encoding": (RECEIPT_TEXT_ENCODING or "cp866").strip().lower(),
        "escpos_profile": (RECEIPT_ESCPOS_PROFILE or "default").strip() or "default",
        "escpos_table_byte": RECEIPT_ESCPOS_TABLE_BYTE,
        "esc_r_byte": RECEIPT_ESC_R_BYTE,
    }


def _print_rows_via_gdi(rows: list[ReceiptRow]) -> None:
    from config import RECEIPT_GDI_PRINTER_NAME
    from receipt_gdi import GdiPrintError, print_receipt_rows_gdi

    try:
        print_receipt_rows_gdi(rows, RECEIPT_GDI_PRINTER_NAME)
    except GdiPrintError as ex:
        raise ReceiptPrinterError(str(ex)) from ex


def _open_printer():
    """Создаёт raw-подключение к ESC/POS-принтеру."""
    try:
        from escpos.printer import File  # noqa: F401 — проверка установки escpos
    except ImportError as e:
        raise ReceiptPrinterError(
            "Не установлен пакет python-escpos. Выполните: pip install python-escpos"
        ) from e

    from config import (
        RECEIPT_ESCPOS_PROFILE,
        RECEIPT_FILE_PATH,
        RECEIPT_PRINTER_BACKEND,
        RECEIPT_USB_PORT_NAME,
        RECEIPT_USB_PRINTER_NAME,
    )

    _prof_kw: dict[str, Any] = {}
    if RECEIPT_ESCPOS_PROFILE:
        _prof_kw["profile"] = RECEIPT_ESCPOS_PROFILE

    backend = (RECEIPT_PRINTER_BACKEND or "").strip().lower()

    if backend in ("lpt", "usb"):
        dev = (
            ((RECEIPT_USB_PRINTER_NAME or "").strip() or (RECEIPT_USB_PORT_NAME or "").strip())
            if backend == "usb"
            else (RECEIPT_FILE_PATH or "").strip()
        ) or ("USB001" if backend == "usb" else "LPT1")
        try:
            from lpt_windows import open_escpos_lpt

            return open_escpos_lpt(dev, **_prof_kw)
        except OSError as e:
            target_hint = (
                "Проверьте USB-порт/очередь принтера в Windows и что устройство принимает RAW."
                if backend == "usb"
                else "Проверьте в Windows: принтер на порту LPT1/LPT2."
            )
            raise ReceiptPrinterError(
                f"Не удалось открыть RAW-канал «{dev}»: {e}. {target_hint}"
            ) from e

    raise ReceiptPrinterError("Печать чека поддерживается только через RAW LPT/USB или Windows/GDI")


def _python_text_codec(user_enc: str) -> str:
    """Кодек Python для байтов в выбранной таблице ESC/POS."""
    u = (user_enc or "cp866").strip().lower().replace(" ", "").replace("_", "")
    if u in ("utf-8", "utf8"):
        return "cp1251"
    if u in ("cp1251", "windows-1251", "wpc1251"):
        return "cp1251"
    if u in ("cp866", "ibm866"):
        return "cp866"
    if u == "cp855":
        return "cp855"
    return "cp866"


def _default_escpos_table_byte(user_enc: str) -> int:
    """
    Номер таблицы для ESC t n (Epson-совместимые прошивки, см. документацию ESC/POS).
    CP866 → 17, CP1251/WPC1251 → 46 (в т.ч. Cashino EP-200 / TEP-200M), CP855 → 34.
    """
    u = (user_enc or "cp866").strip().lower().replace(" ", "").replace("_", "")
    if u in ("utf-8", "utf8", "cp1251", "windows-1251", "wpc1251"):
        return 46
    if u == "cp855":
        return 34
    return 17


def _env_no_esc_pct() -> bool:
    """Отключить ESC % 0 (редко, если принтер ломается): DESKTOP_MARKET_RECEIPT_NO_ESC_PCT=1"""
    return os.environ.get("DESKTOP_MARKET_RECEIPT_NO_ESC_PCT", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _resolve_codec_and_table(user_enc: str) -> tuple[str, int]:
    from config import RECEIPT_ESCPOS_TABLE_BYTE

    codec = _python_text_codec(user_enc)
    table_raw = RECEIPT_ESCPOS_TABLE_BYTE
    if table_raw is None:
        table = _default_escpos_table_byte(user_enc)
    else:
        try:
            table = int(table_raw)
            if not (0 <= table <= 255):
                table = _default_escpos_table_byte(user_enc)
        except (TypeError, ValueError):
            table = _default_escpos_table_byte(user_enc)
    if os.environ.get("DESKTOP_MARKET_RECEIPT_COERCE_CP866_TABLE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        c = (codec or "cp866").strip().lower()
        t = int(table) & 0xFF
        if c == "cp866" and t == 46:
            logger.warning("COERCE_CP866_TABLE: ESC t 46 → 17 при CP866")
            table = 17
    return codec, table


def _apply_escpos_codepage(p: Any, user_enc: str) -> str:
    """
    Выход из CP936/GBK (ESC % 0) + ESC t n + опционально ESC R.
    На Cashino EP-200 после ESC @ и иногда после ESC M / ESC a прошивка снова в GBK —
    поэтому вызываем дважды: до и после p.set() (см. print_receipt_text).
    ESC % 0 всегда (если не NO_ESC_PCT): надёжнее, чем угадывать по профилю.
    """
    from config import RECEIPT_ESC_R_BYTE

    codec, table = _resolve_codec_and_table(user_enc)
    if not _env_no_esc_pct():
        p._raw(b"\x1b\x25\x00")
    esc_r = RECEIPT_ESC_R_BYTE
    if esc_r is not None:
        try:
            er = int(esc_r)
            if 0 <= er <= 255:
                p._raw(b"\x1b\x52" + bytes([er & 0xFF]))
        except (TypeError, ValueError):
            pass
    p._raw(b"\x1b\x74" + bytes([table & 0xFF]))
    return codec


def _touch_codepage_after_esc_a(p: Any, user_enc: str) -> None:
    """
    После ESC a (выравнивание) часть термопринтеров (Cashino EP-200 и др.) снова уходит в CP936/GBK.
    Короткий повтор ESC % 0 + ESC t без полного init сохраняет читаемую кириллицу на следующих строках.
    """
    if not (user_enc or "").strip():
        return
    _, table = _resolve_codec_and_table(user_enc)
    if not _env_no_esc_pct():
        p._raw(b"\x1b\x25\x00")
    p._raw(b"\x1b\x74" + bytes([table & 0xFF]))


def _emit_raw_lines(p: Any, lines: list[str], codec: str) -> None:
    """Только печать строк в уже выбранной таблице ESC t."""
    for s in lines:
        chunk = (s or "") + "\n"
        try:
            p._raw(chunk.encode(codec, errors="replace"))
        except LookupError:
            p._raw(chunk.encode("cp866", errors="replace"))


def _safe_cut(p: Any) -> None:
    try:
        p.cut()
    except Exception as ex:
        logger.warning("cut() недоступен на этом принтере, только протяжка: %s", ex)
        try:
            p._raw(b"\n\n\n")
        except Exception:
            pass


def print_printer_quick_test() -> None:
    """Короткий тест (3 строки)."""
    print_receipt_text("Тест печати\nDesktop Market POS\n— OK —")


def print_printer_usb_test() -> None:
    """Совместимый USB-тест: отправляет несколько простых вариантов в USB001/USBPRINT и пишет лог."""
    from lpt_windows import write_lpt_bytes

    cfg = _current_lpt_settings()
    path = (
        cfg.get("usb_port_name")
        or cfg.get("usb_printer_name")
        or cfg.get("devfile")
        or "USB001"
    )
    backend = str(cfg.get("backend") or "").strip().lower()
    if backend != "usb":
        print_printer_quick_test()
        return

    lines = [
        "USB TEST",
        str(cfg.get("usb_friendly_name") or "THERMAL PRINTER"),
        str(path),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "OK",
    ]

    variants: list[tuple[str, bytes]] = [
        ("LF", ("\n".join(lines) + "\n\n\n").encode("ascii", errors="replace")),
        ("CRLF", ("\r\n".join(lines) + "\r\n\r\n\r\n").encode("ascii", errors="replace")),
        ("CRLF+FF", ("\r\n".join(lines) + "\r\n\r\n\r\n\f").encode("ascii", errors="replace")),
        ("CRLF+FF+SUB", ("\r\n".join(lines) + "\r\n\r\n\r\n\f\x1a").encode("ascii", errors="replace")),
    ]

    _append_usb_log(
        "USB test start "
        f"path={path} backend={backend} driver={cfg.get('lpt_driver')} "
        f"line_ending={cfg.get('line_ending')} device={cfg.get('usb_friendly_name') or '—'}"
    )
    errors: list[str] = []
    for label, payload in variants:
        preview = " ".join(f"{b:02X}" for b in payload[:24])
        try:
            _append_usb_log(f"send variant={label} bytes={len(payload)} preview={preview}")
            write_lpt_bytes(str(path), payload, append_lf_if_missing=False)
            _append_usb_log(f"send ok variant={label}")
            time.sleep(0.25)
        except OSError as ex:
            errors.append(f"{label}: {ex}")
            _append_usb_log(f"send fail variant={label} error={ex}")

    if len(errors) == len(variants):
        raise ReceiptPrinterError(
            f"Не удалось отправить USB-тест на «{path}»: {'; '.join(errors)}"
        )


def print_printer_self_check_page() -> None:
    """
    Тестовая страница в духе встроенного отчёта EP-200: пояснение про CP936 GBK
    в прошивке, кириллица, латиница, цифры. Параметры — из настроек приложения.
    """
    from config import (
        RECEIPT_ESCPOS_PROFILE,
        RECEIPT_ESCPOS_TABLE_BYTE,
        RECEIPT_FILE_PATH,
        RECEIPT_GDI_PRINTER_NAME,
        RECEIPT_LPT_DRIVER,
        RECEIPT_LPT_LINE_ENDING,
        RECEIPT_PRINT_MODE,
        RECEIPT_PRINTER_BACKEND,
        RECEIPT_TEXT_ENCODING,
    )

    enc = (RECEIPT_TEXT_ENCODING or "cp866").strip().lower()
    ts = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    profile = (RECEIPT_ESCPOS_PROFILE or "универсальный").strip()
    back = (RECEIPT_PRINTER_BACKEND or "none").strip().lower()
    lpt = (RECEIPT_FILE_PATH or "LPT1").strip() or "LPT1"
    lpt_driver = (RECEIPT_LPT_DRIVER or "escpos").strip().lower()
    line_ending = (RECEIPT_LPT_LINE_ENDING or "lf").strip().lower()
    mode = (RECEIPT_PRINT_MODE or "lpt").strip().lower()
    gdi_name = (RECEIPT_GDI_PRINTER_NAME or "").strip()
    if mode != "gdi" and back == "usb" and lpt_driver == "text" and str(lpt).strip().upper().startswith("USB"):
        print_printer_usb_test()
        return
    cp_plan = describe_codepage_plan(
        text_encoding=enc,
        escpos_profile=RECEIPT_ESCPOS_PROFILE,
        escpos_table_byte=RECEIPT_ESCPOS_TABLE_BYTE,
    )
    if mode != "gdi":
        try:
            if lpt_driver == "escpos":
                print_probe_via_lpt(
                    devfile=lpt,
                    line_ending=line_ending,
                    esc_r_byte=None,
                    sections=[
                        ("WPC1251 / TEP-200M / ESC t 46", "wpc1251", "TEP-200M", 46),
                        ("CP866 / DEFAULT / ESC t 17", "cp866", "default", 17),
                        ("CP1251 / DEFAULT / ESC t 46", "cp1251", "default", 46),
                        ("CP1251 / GENERIC / ESC t 51", "cp1251", "default", 51),
                        ("CP1251 / GENERIC / ESC t 73", "cp1251", "default", 73),
                        ("CP855 / DEFAULT / ESC t 34", "cp855", "default", 34),
                    ],
                )
            else:
                print_text_probe_via_lpt(
                    devfile=lpt,
                    sections=[
                        ("CP866 / LF", "cp866", "lf"),
                        ("CP866 / CRLF", "cp866", "crlf"),
                        ("CP1251 / LF", "cp1251", "lf"),
                        ("CP1251 / CRLF", "cp1251", "crlf"),
                        ("KOI8-R / CRLF", "koi8-r", "crlf"),
                        ("MAC-CYR / CRLF", "mac-cyrillic", "crlf"),
                    ],
                )
            return
        except Exception as ex:
            logger.warning("probe print failed, fallback to standard self-check: %s", ex)
    w = RECEIPT_WIDTH
    lines: list[str] = [
        "=" * w,
        "  САМОПРОВЕРКА (ПРИЛОЖЕНИЕ)",
        "  Desktop Market POS",
        "=" * w,
        f"Дата/время: {ts}",
        "-" * w,
        "Модель (цель): XPrinter 80mm USB",
        "Версия прошивки: см. селф-тест",
        "принтера",
        "-" * w,
        "Стартовый пресет для этой модели:",
        "Кодировка текста: CP866",
        "Таблица ESC/POS: ESC t 17",
        "Режим USB: RAW / USB Printing",
        "При кракозябрах проверьте порт",
        "-" * w,
        f"Профиль в приложении: {profile[:44]}",
        f"RAW драйвер: {lpt_driver}",
        f"Конец строки: {line_ending}",
        f"Кодировка текста: {enc}",
        f"План таблицы: {cp_plan[:44]}",
        f"Режим: {mode}",
        "-" * w,
        (f"GDI: {gdi_name[:40]}" if mode == "gdi" else f"RAW: {str(lpt).strip() or ('XPrinter N160II' if back == 'usb' else 'LPT1')}"),
        "-" * w,
        (f"Канал ESC/POS: {back or 'lpt'}" if mode != "gdi" else "Канал ESC/POS: —"),
        "-" * w,
        "Кириллица (тест):",
        "АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ",
        "абвгдежзийклмнопрстуфхцчшщъыьэюя",
        "-" * w,
        "Латиница:",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "abcdefghijklmnopqrstuvwxyz",
        "-" * w,
        "Цифры: 0123456789",
        "Символы: !\"#$%&'()*+,-./:;<=>?",
        "-" * w,
        "Штрихкод CODE39 (текстом):",
        "*123456*",
        "=" * w,
        "Самопроверка завершена.",
        "",
    ]
    print_receipt_text("\n".join(lines))


def print_printer_test() -> None:
    """Обратная совместимость: полная самопроверка."""
    print_printer_self_check_page()


def print_escpos_text_file(devfile: str, text: str) -> None:
    """
    Печать простого текста на указанный raw-канал/файл с тем же ESC/POS и кодировкой, что и чек.
    Нужна для тестовой печати веса на LPT.
    """
    raw = (text or "").strip()
    if not raw:
        raise ReceiptPrinterError("Пустой текст")

    path = (devfile or "").strip()
    if not path:
        raise ReceiptPrinterError("Не указан путь или имя принтера (например LPT1 или XPrinter)")
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    rows = _plain_lines_to_receipt_rows(lines)
    cfg = _current_lpt_settings()
    try:
        print_rows_via_lpt(
            devfile=path,
            rows=rows,
            lpt_driver=cfg["lpt_driver"],
            line_ending=cfg["line_ending"],
            text_encoding=cfg["text_encoding"],
            escpos_profile=cfg["escpos_profile"],
            escpos_table_byte=cfg["escpos_table_byte"],
            esc_r_byte=cfg["esc_r_byte"],
        )
    except (OSError, ValueError) as e:
        raise ReceiptPrinterError(
            f"Не удалось отправить печать на «{path}»: {e}. "
            "Проверьте имя USB-принтера/порт, RAW-драйвер и кодировку."
        ) from e


def print_receipt_text(text: str) -> None:
    """
    Печать готового текста чека (с бэкенда: receipt_text или GET …/receipt/).
    """
    raw = (text or "").strip()
    if not raw:
        raise ReceiptPrinterError("Пустой текст чека")

    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    rows = _plain_lines_to_receipt_rows(lines)
    if _use_gdi_print():
        _print_rows_via_gdi(rows)
        return

    cfg = _current_lpt_settings()
    try:
        print_rows_via_lpt(
            devfile=cfg["devfile"],
            rows=rows,
            lpt_driver=cfg["lpt_driver"],
            line_ending=cfg["line_ending"],
            text_encoding=cfg["text_encoding"],
            escpos_profile=cfg["escpos_profile"],
            escpos_table_byte=cfg["escpos_table_byte"],
            esc_r_byte=cfg["esc_r_byte"],
        )
    except (OSError, ValueError) as e:
        raise ReceiptPrinterError(str(e)) from e


def print_sale_receipt(
    *,
    cart: dict[str, Any],
    payment_method: str,
    cash_received: str | None,
    change_amount: Any,
    sale_id: Any,
    company: str,
    cashier: str,
) -> None:
    """
    Печать текстового чека. Кириллица: выбор таблицы ESC/POS через charcode (см. RECEIPT_TEXT_ENCODING).
    """
    rows = _compose_sale_receipt_rows(
        cart=cart,
        payment_method=payment_method,
        cash_received=cash_received,
        change_amount=change_amount,
        sale_id=sale_id,
        company=company,
        cashier=cashier,
    )
    if _use_gdi_print():
        _print_rows_via_gdi(rows)
        return

    cfg = _current_lpt_settings()
    try:
        print_rows_via_lpt(
            devfile=cfg["devfile"],
            rows=rows,
            lpt_driver=cfg["lpt_driver"],
            line_ending=cfg["line_ending"],
            text_encoding=cfg["text_encoding"],
            escpos_profile=cfg["escpos_profile"],
            escpos_table_byte=cfg["escpos_table_byte"],
            esc_r_byte=cfg["esc_r_byte"],
        )
    except (OSError, ValueError) as e:
        raise ReceiptPrinterError(str(e)) from e


def try_print_sale_receipt(**kwargs: Any) -> None:
    """Печать при включённой настройке; ошибки только в лог, без исключения наружу."""
    if not is_receipt_printing_enabled():
        return
    try:
        print_sale_receipt(**kwargs)
    except ReceiptPrinterError as e:
        logger.warning("Печать чека: %s", e)
    except Exception as e:
        logger.exception("Печать чека: неожиданная ошибка: %s", e)
