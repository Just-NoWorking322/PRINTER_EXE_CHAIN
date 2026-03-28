"""
Печать чека на термопринтере ESC/POS (Xprinter XP-58II, Cashino EP-200 и аналоги).

Cashino EP-200: в самотесте часто «по умолчанию: CP936 GBK». Для кириллицы: кодировка WPC1251/cp1251
(или cp866), слот ESC t 46 или 17; перед ESC t всегда шлём ESC % 0 (выход из двухбайтового режима),
если не отключено DESKTOP_MARKET_RECEIPT_NO_ESC_PCT=1. Профиль TEP-200M в python-escpos — по желанию.

Требуется: pip install python-escpos
Для COM (USB-принтер как виртуальный порт): укажите порт COM в конфиге.
Для Windows RAW по имени принтера: установите pywin32 и используйте backend win32raw.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Any

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


def _item_title(it: dict[str, Any]) -> str:
    p = it.get("product")
    if isinstance(p, dict):
        return str(p.get("name") or p.get("title") or "—")
    return str(it.get("product_name") or it.get("name") or "—")


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


def _raw_esc_bold(p: Any, on: bool) -> None:
    p._raw(b"\x1b\x45" + bytes([1 if on else 0]))


def _raw_esc_align(p: Any, mode: int) -> None:
    """0=left, 1=center, 2=right."""
    p._raw(b"\x1b\x61" + bytes([mode & 0xFF]))


def _emit_line_encoded(p: Any, codec: str, text: str) -> None:
    try:
        p._raw(text.encode(codec) + b"\n")
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


def _emit_receipt_rows(p: Any, codec: str, rows: list[ReceiptRow]) -> None:
    """Печать строк чека: жирный/центр через сырые ESC (без p.set — не сбрасывать кодовую страницу)."""
    for row in rows:
        if not row:
            continue
        kind = row[0]
        if kind == "sep":
            _raw_esc_align(p, 0)
            _raw_esc_bold(p, False)
            _emit_line_encoded(p, codec, "-" * RECEIPT_WIDTH)
        elif kind == "blank":
            p._raw(b"\n")
        elif kind == "center_bold":
            _raw_esc_align(p, 1)
            _raw_esc_bold(p, True)
            _emit_line_encoded(p, codec, str(row[1]))
            _raw_esc_bold(p, False)
            _raw_esc_align(p, 0)
        elif kind == "center":
            _raw_esc_align(p, 1)
            _raw_esc_bold(p, False)
            _emit_line_encoded(p, codec, str(row[1]))
            _raw_esc_align(p, 0)
        elif kind == "left":
            _raw_esc_align(p, 0)
            _raw_esc_bold(p, False)
            _emit_line_encoded(p, codec, str(row[1]))
        elif kind == "lr":
            _raw_esc_align(p, 0)
            _raw_esc_bold(p, False)
            _emit_line_encoded(p, codec, _pad_left_right(str(row[1]), str(row[2])))
        elif kind == "lr_bold":
            _raw_esc_align(p, 0)
            _raw_esc_bold(p, True)
            _emit_line_encoded(p, codec, _pad_left_right(str(row[1]), str(row[2])))
            _raw_esc_bold(p, False)
        else:
            _raw_esc_align(p, 0)
            _emit_line_encoded(p, codec, str(row[-1]))


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
    """company/cashier оставлены в сигнатуре для совместимости с print_sale_receipt."""
    items = cart.get("items") or cart.get("cart_items") or []
    if not isinstance(items, list):
        items = []

    st = cart.get("subtotal")
    disc = cart.get("discount_total")
    tot = _cart_total(cart)

    rows: list[ReceiptRow] = [
        ("sep",),
        ("center", "Добро пожаловать!"),
    ]
    if sale_id is not None:
        sid = str(sale_id)
        sid_show = sid[:10] + "…" if len(sid) > 12 else sid
        rows.append(("center_bold", f"Товарный чек № {sid_show}"))
    else:
        rows.append(("center_bold", "Товарный чек"))
    rows.append(("center", f"от {datetime.now().strftime('%d.%m.%Y %H:%M')}"))
    rows.append(("sep",))

    for idx, it in enumerate(items, start=1):
        if not isinstance(it, dict):
            continue
        title = _item_title(it)
        block = _wrap_to_width(f"{idx}. {title}")
        for ln in block:
            rows.append(("left", ln))
        up = _money_display(it.get("unit_price"))
        qty = _qty_display(it.get("quantity", "1"))
        lam = _money_display(_line_amount(it))
        rows.append(("left", f"  {up} x {qty} = {lam}"))

    rows.append(("sep",))
    try:
        dval = float(disc) if disc is not None else 0.0
    except (TypeError, ValueError):
        dval = 0.0
    rows.append(("lr", "Подытог:", _money_display(st)))
    if abs(dval) > 0.001:
        rows.append(("lr", "Скидка:", _money_display(dval)))

    pm_label = (
        "Наличные"
        if payment_method == "cash"
        else ("Безнал" if payment_method == "transfer" else str(payment_method))
    )
    rows.append(("lr_bold", "ИТОГ:", _money_display(tot)))
    rows.append(("lr", pm_label, _money_display(tot)))
    if payment_method == "cash" and cash_received:
        rows.append(("lr", "Получено:", _money_display(cash_received)))
    if change_amount is not None:
        rows.append(("lr", "Сдача:", _money_display(change_amount)))
    rows.append(("sep",))
    rows.append(("center_bold", "Спасибо за покупку!"))
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
    from config import RECEIPT_PRINTER_BACKEND

    b = (RECEIPT_PRINTER_BACKEND or "").strip().lower()
    return bool(b and b not in ("none", "off", "false", "0"))


def _open_printer():
    """Создаёт подключение escpos и вызывает open() где нужно."""
    try:
        from escpos.printer import File, Network, Serial, Usb
    except ImportError as e:
        raise ReceiptPrinterError(
            "Не установлен пакет python-escpos. Выполните: pip install python-escpos"
        ) from e

    from config import (
        RECEIPT_ESCPOS_PROFILE,
        RECEIPT_FILE_PATH,
        RECEIPT_NETWORK_HOST,
        RECEIPT_NETWORK_PORT,
        RECEIPT_PRINTER_BACKEND,
        RECEIPT_SERIAL_BAUDRATE,
        RECEIPT_SERIAL_PORT,
        RECEIPT_USB_IN_EP,
        RECEIPT_USB_OUT_EP,
        RECEIPT_USB_PRODUCT,
        RECEIPT_USB_VENDOR,
        RECEIPT_WIN32_NAME,
    )

    _prof_kw: dict[str, Any] = {}
    if RECEIPT_ESCPOS_PROFILE:
        _prof_kw["profile"] = RECEIPT_ESCPOS_PROFILE

    backend = (RECEIPT_PRINTER_BACKEND or "").strip().lower()

    if backend == "serial":
        try:
            import serial  # noqa: F401 — нужен pyserial для escpos.printer.Serial
        except ImportError as e:
            raise ReceiptPrinterError(
                "Для печати через COM-порт установите pyserial: pip install pyserial"
            ) from e
        if not RECEIPT_SERIAL_PORT:
            raise ReceiptPrinterError("Не задан порт (в настройках принтера или DESKTOP_MARKET_RECEIPT_SERIAL, например COM3)")
        p = Serial(devfile=RECEIPT_SERIAL_PORT, baudrate=RECEIPT_SERIAL_BAUDRATE, **_prof_kw)
        p.open()
        return p

    if backend == "network":
        if not RECEIPT_NETWORK_HOST:
            raise ReceiptPrinterError("Не задан DESKTOP_MARKET_RECEIPT_HOST")
        p = Network(host=RECEIPT_NETWORK_HOST, port=RECEIPT_NETWORK_PORT, **_prof_kw)
        p.open()
        return p

    if backend == "file":
        if not RECEIPT_FILE_PATH:
            raise ReceiptPrinterError("Не задан путь к файлу (настройки принтера)")
        p = File(devfile=RECEIPT_FILE_PATH, **_prof_kw)
        p.open()
        return p

    if backend == "lpt":
        # Параллельный порт Windows: LPT1, LPT2 — тот же драйвер File в escpos
        dev = (RECEIPT_FILE_PATH or "").strip() or "LPT1"
        p = File(devfile=dev, **_prof_kw)
        p.open()
        return p

    if backend == "usb":
        try:
            import usb.core  # noqa: F401 — нужен pyusb для escpos.printer.Usb
        except ImportError as e:
            raise ReceiptPrinterError(
                "Для печати по USB установите pyusb: pip install pyusb"
            ) from e
        if not RECEIPT_USB_VENDOR or not RECEIPT_USB_PRODUCT:
            raise ReceiptPrinterError(
                "Укажите Vendor ID и Product ID в настройках принтера (hex, например 0x0416 и 0x5010)"
            )
        p = Usb(
            idVendor=int(str(RECEIPT_USB_VENDOR).replace("0x", ""), 16),
            idProduct=int(str(RECEIPT_USB_PRODUCT).replace("0x", ""), 16),
            in_ep=RECEIPT_USB_IN_EP,
            out_ep=RECEIPT_USB_OUT_EP,
            **_prof_kw,
        )
        p.open()
        return p

    if backend == "win32raw":
        try:
            from escpos.printer import Win32Raw
        except ImportError as e:
            raise ReceiptPrinterError("Для win32raw нужен пакет pywin32 (pip install pywin32)") from e
        if not Win32Raw.is_usable():
            raise ReceiptPrinterError("Win32Raw недоступен: установите pywin32")
        p = Win32Raw(printer_name=RECEIPT_WIN32_NAME or "", **_prof_kw)
        p.open()
        return p

    raise ReceiptPrinterError(f"Неизвестный DESKTOP_MARKET_RECEIPT_BACKEND: {backend!r}")


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


def _emit_raw_lines(p: Any, lines: list[str], codec: str) -> None:
    """Только печать строк в уже выбранной таблице ESC t."""
    for s in lines:
        chunk = (s or "") + "\n"
        try:
            p._raw(chunk.encode(codec))
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
        RECEIPT_PRINTER_BACKEND,
        RECEIPT_SERIAL_BAUDRATE,
        RECEIPT_TEXT_ENCODING,
    )

    enc = (RECEIPT_TEXT_ENCODING or "cp866").strip().lower()
    ts = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    profile = (RECEIPT_ESCPOS_PROFILE or "универсальный").strip()
    back = (RECEIPT_PRINTER_BACKEND or "none").strip().lower()
    baud = int(RECEIPT_SERIAL_BAUDRATE or 9600)
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
        f"COM (если serial): {baud},8,N,1",
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
    Нужна, когда основной принтер в режиме serial/network, а тест веса идёт на LPT.
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

    p = File(devfile=path, **_prof_kw)
    p.open()
    user_enc = (RECEIPT_TEXT_ENCODING or "cp866").strip().lower()
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    try:
        p.hw("INIT")
        codec = _apply_escpos_codepage(p, user_enc)
        p.set(align="left", font="a")
        _apply_escpos_codepage(p, user_enc)
        rows = _plain_lines_to_receipt_rows(lines)
        _emit_receipt_rows(p, codec, rows)
        _safe_cut(p)
    finally:
        try:
            p.close()
        except Exception as ex:
            logger.warning("close printer: %s", ex)


def print_receipt_text(text: str) -> None:
    """
    Печать готового текста чека (с бэкенда: receipt_text или GET …/receipt/).
    """
    from config import RECEIPT_TEXT_ENCODING

    raw = (text or "").strip()
    if not raw:
        raise ReceiptPrinterError("Пустой текст чека")

    p = _open_printer()
    user_enc = (RECEIPT_TEXT_ENCODING or "cp866").strip().lower()
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    try:
        p.hw("INIT")
        codec = _apply_escpos_codepage(p, user_enc)
        p.set(align="left", font="a")
        _apply_escpos_codepage(p, user_enc)
        rows = _plain_lines_to_receipt_rows(lines)
        _emit_receipt_rows(p, codec, rows)
        _safe_cut(p)
    finally:
        try:
            p.close()
        except Exception as ex:
            logger.warning("close printer: %s", ex)


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

    p = _open_printer()
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

    try:
        p.hw("INIT")
        codec = _apply_escpos_codepage(p, user_enc)
        p.set(align="left", font="a")
        _apply_escpos_codepage(p, user_enc)
        _emit_receipt_rows(p, codec, rows)
        _safe_cut(p)
    finally:
        try:
            p.close()
        except Exception as ex:
            logger.warning("close printer: %s", ex)


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
