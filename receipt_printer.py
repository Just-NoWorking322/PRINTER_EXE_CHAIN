"""
Печать чека на термопринтере ESC/POS (Xprinter XP-58II, Cashino EP-200 и аналоги).

Cashino EP-200: в самотесте часто «по умолчанию: CP936 GBK». Для кириллицы: кодировка WPC1251/cp1251
(или cp866), слот ESC t 46 или 17; перед ESC t всегда шлём ESC % 0 (выход из двухбайтового режима),
если не отключено DESKTOP_MARKET_RECEIPT_NO_ESC_PCT=1. Профиль TEP-200M в python-escpos — по желанию.
Опционально: DESKTOP_MARKET_RECEIPT_COERCE_CP866_TABLE=1 — при CP866 принудительно заменить ESC t 46 на 17 (по умолчанию выкл., слот не трогаем).

Требуется: pip install python-escpos
Поддерживается только печать чека через LPT (LPT1 / LPT2) с явной установкой кодовой страницы ESC/POS.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Callable

logger = logging.getLogger(__name__)


class ReceiptPrinterError(Exception):
    """Ошибка подключения или печати на принтере."""


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


# Ширина строки для 58 мм (Font A ~32 символа)
RECEIPT_WIDTH = 32


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


def _unit_is_kg(unit: Any) -> bool:
    raw = str(unit or "").strip().lower()
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


def _truthy_api_bool(v: Any) -> bool:
    if v is True:
        return True
    if isinstance(v, str) and v.strip().lower() in ("1", "true", "yes", "on"):
        return True
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v != 0:
        return True
    return False


def _dict_has_kg_unit(d: dict[str, Any]) -> bool:
    for key in ("unit", "unit_display", "measure_unit", "sale_unit", "uom"):
        if _unit_is_kg(d.get(key)):
            return True
    return False


def _product_must_weigh_for_receipt(p: dict[str, Any]) -> bool:
    """Совпадает с логикой кассы (main._product_must_weigh): кг по флагам и по полю unit."""
    if _truthy_api_bool(p.get("is_wait")):
        return True
    if _truthy_api_bool(p.get("is_weight")):
        return True
    if _dict_has_kg_unit(p):
        return True
    return False


def _item_unit_label(it: dict[str, Any]) -> str:
    if not isinstance(it, dict):
        return "шт"
    if _truthy_api_bool(it.get("is_wait")) or _truthy_api_bool(it.get("is_weight")):
        return "кг"
    if _dict_has_kg_unit(it):
        return "кг"
    for src in (it.get("product"), it.get("product_snapshot")):
        if isinstance(src, dict) and _product_must_weigh_for_receipt(src):
            return "кг"
    return "шт"


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
    """Чек печатаем заглавными буквами для более читаемого вида на 58 мм."""
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
        unit_label = _item_unit_label(it)
        unit_suffix = "сом/кг" if unit_label == "кг" else "сом/шт"
        rows.append(("left", f"{qty} {unit_label} x {up} {unit_suffix}"))
        rows.append(("right", lam))

    rows.append(("sep",))
    rows.append(("lr_bold", "ИТОГ:", _money_display(tot)))
    if payment_method == "cash" and cash_received:
        rows.append(("lr_bold", "ПОЛУЧЕНО:", _money_display(cash_received)))
    if change_amount is not None:
        try:
            chf = float(change_amount)
        except (TypeError, ValueError):
            chf = None
        if chf is not None:
            rows.append(("lr_bold", "СДАЧА:", _money_display(chf)))

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
    from config import RECEIPT_FILE_PATH, RECEIPT_PRINTER_BACKEND

    b = (RECEIPT_PRINTER_BACKEND or "").strip().lower()
    return b == "lpt" and bool((RECEIPT_FILE_PATH or "").strip())


def _lpt_retry_attempts() -> int:
    try:
        n = int(os.environ.get("DESKTOP_MARKET_RECEIPT_RETRY", "3").strip() or "3")
    except ValueError:
        n = 3
    return max(1, min(8, n))


def _run_lpt_job(job: Callable[[], None], *, what: str = "print") -> None:
    """
    Повтор при временных сбоях LPT/драйвера (чек «не вышел», повторно сработало).
    Число попыток: DESKTOP_MARKET_RECEIPT_RETRY (по умолчанию 3).
    """
    attempts = _lpt_retry_attempts()
    last: Exception | None = None
    for i in range(attempts):
        try:
            job()
            return
        except ReceiptPrinterError as e:
            last = e
            logger.warning("%s: попытка %s/%s: %s", what, i + 1, attempts, e)
        except OSError as e:
            last = ReceiptPrinterError(str(e))
            last.__cause__ = e
            logger.warning("%s: LPT OSError %s/%s: %s", what, i + 1, attempts, e)
        if i + 1 < attempts:
            time.sleep(0.07 + 0.06 * i)
    if last is not None:
        raise last


def _open_printer():
    """Создаёт LPT-подключение к ESC/POS-принтеру."""
    try:
        from escpos.printer import File
    except ImportError as e:
        raise ReceiptPrinterError(
            "Не установлен пакет python-escpos. Выполните: pip install python-escpos"
        ) from e

    from config import (
        RECEIPT_ESCPOS_PROFILE,
        RECEIPT_FILE_PATH,
        RECEIPT_PRINTER_BACKEND,
    )

    _prof_kw: dict[str, Any] = {}
    if RECEIPT_ESCPOS_PROFILE:
        _prof_kw["profile"] = RECEIPT_ESCPOS_PROFILE

    backend = (RECEIPT_PRINTER_BACKEND or "").strip().lower()

    if backend == "lpt":
        dev = (RECEIPT_FILE_PATH or "").strip() or "LPT1"
        p = File(devfile=dev, **_prof_kw)
        p.open()
        time.sleep(0.02)
        return p

    raise ReceiptPrinterError("Печать чека поддерживается только через LPT1 / LPT2")


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


def print_printer_self_check_page() -> None:
    """
    Тестовая страница в духе встроенного отчёта EP-200: пояснение про CP936 GBK
    в прошивке, кириллица, латиница, цифры. Параметры — из настроек приложения.
    """
    from config import (
        RECEIPT_ESCPOS_PROFILE,
        RECEIPT_FILE_PATH,
        RECEIPT_PRINTER_BACKEND,
        RECEIPT_TEXT_ENCODING,
    )

    enc = (RECEIPT_TEXT_ENCODING or "cp866").strip().lower()
    ts = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    profile = (RECEIPT_ESCPOS_PROFILE or "универсальный").strip()
    back = (RECEIPT_PRINTER_BACKEND or "none").strip().lower()
    lpt = (RECEIPT_FILE_PATH or "LPT1").strip() or "LPT1"
    w = 32
    lines: list[str] = [
        "=" * w,
        "  САМОПРОВЕРКА (ПРИЛОЖЕНИЕ)",
        "  Desktop Market POS",
        "=" * w,
        f"Дата/время: {ts}",
        "-" * w,
        "Модель (цель): Cashino EP-200",
        "Версия прошивки: см. отчёт",
        "  принтера (у держ. кнопки)",
        "-" * w,
        "В отчёте принтера часто:",
        "Код.стр. по умол.: CP936 GBK",
        "Это нормально. Печать из",
        "кассы: ESC % 0 + ESC t 46 +",
        "WPC1251 (байты cp1251).",
        "-" * w,
        f"Профиль в приложении: {profile[:28]}",
        f"Кодировка текста: {enc}",
        f"Канал: {back}",
        "-" * w,
        f"LPT: {str(lpt).strip() or 'LPT1'}",
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
    Печать простого текста на указанный LPT/файл с тем же ESC/POS и кодировкой, что и чек.
    Нужна для тестовой печати веса на LPT.
    """
    try:
        from escpos.printer import File
    except ImportError as e:
        raise ReceiptPrinterError(
            "Не установлен пакет python-escpos. Выполните: pip install python-escpos"
        ) from e

    from config import RECEIPT_ESCPOS_PROFILE, RECEIPT_TEXT_ENCODING

    raw = (text or "").strip()
    if not raw:
        raise ReceiptPrinterError("Пустой текст")

    path = (devfile or "").strip()
    if not path:
        raise ReceiptPrinterError("Не указан путь (LPT1 или файл)")

    _prof_kw: dict[str, Any] = {}
    if RECEIPT_ESCPOS_PROFILE:
        _prof_kw["profile"] = RECEIPT_ESCPOS_PROFILE

    user_enc = (RECEIPT_TEXT_ENCODING or "cp866").strip().lower()
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    def _once() -> None:
        p = File(devfile=path, **_prof_kw)
        p.open()
        time.sleep(0.02)
        try:
            p.hw("INIT")
            codec = _apply_escpos_codepage(p, user_enc)
            p.set(align="left", font="a")
            _apply_escpos_codepage(p, user_enc)
            rows = _plain_lines_to_receipt_rows(lines)
            _emit_receipt_rows(p, codec, rows, user_enc)
            _safe_cut(p)
        finally:
            try:
                p.close()
            except Exception as ex:
                logger.warning("close printer: %s", ex)

    _run_lpt_job(_once, what="print_escpos_text_file")


def print_receipt_text(text: str) -> None:
    """
    Печать готового текста чека (с бэкенда: receipt_text или GET …/receipt/).
    """
    from config import RECEIPT_TEXT_ENCODING

    raw = (text or "").strip()
    if not raw:
        raise ReceiptPrinterError("Пустой текст чека")

    user_enc = (RECEIPT_TEXT_ENCODING or "cp866").strip().lower()
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    def _once() -> None:
        p = _open_printer()
        try:
            p.hw("INIT")
            codec = _apply_escpos_codepage(p, user_enc)
            p.set(align="left", font="a")
            _apply_escpos_codepage(p, user_enc)
            rows = _plain_lines_to_receipt_rows(lines)
            _emit_receipt_rows(p, codec, rows, user_enc)
            _safe_cut(p)
        finally:
            try:
                p.close()
            except Exception as ex:
                logger.warning("close printer: %s", ex)

    _run_lpt_job(_once, what="print_receipt_text")


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
    from config import RECEIPT_TEXT_ENCODING

    user_enc = (RECEIPT_TEXT_ENCODING or "cp866").strip().lower()
    rows = _compose_sale_receipt_rows(
        cart=cart,
        payment_method=payment_method,
        cash_received=cash_received,
        change_amount=change_amount,
        sale_id=sale_id,
        company=company,
        cashier=cashier,
    )

    def _once() -> None:
        p = _open_printer()
        try:
            p.hw("INIT")
            codec = _apply_escpos_codepage(p, user_enc)
            p.set(align="left", font="a")
            _apply_escpos_codepage(p, user_enc)
            _emit_receipt_rows(p, codec, rows, user_enc)
            _safe_cut(p)
        finally:
            try:
                p.close()
            except Exception as ex:
                logger.warning("close printer: %s", ex)

    _run_lpt_job(_once, what="print_sale_receipt")


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
