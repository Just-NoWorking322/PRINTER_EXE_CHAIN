"""
Печать чека через драйвер Windows (GDI): Unicode без ESC/POS таблиц.
Подходит для системных принтеров вроде RONGTA 58mm Series Printer.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

ReceiptRow = tuple[Any, ...]


class GdiPrintError(Exception):
    pass


def _ensure_win32() -> None:
    try:
        import win32api  # noqa: F401
        import win32con  # noqa: F401
        import win32ui  # noqa: F401
    except ImportError as e:
        raise GdiPrintError(
            "Для печати через Windows/GDI установите pywin32: pip install pywin32"
        ) from e


def print_receipt_rows_gdi(rows: list[ReceiptRow], printer_name: str) -> None:
    _ensure_win32()
    import win32api
    import win32con
    import win32ui

    name = (printer_name or "").strip()
    if not name:
        raise GdiPrintError("Не указано имя принтера Windows.")

    hdc = win32ui.CreateDC()
    try:
        hdc.CreatePrinterDC(name)
    except Exception as ex:
        raise GdiPrintError(
            f"Не удалось открыть принтер «{name}». Проверьте имя в «Устройства и принтеры»."
        ) from ex

    try:
        horz = hdc.GetDeviceCaps(win32con.HORZRES)
        logpx_y = max(hdc.GetDeviceCaps(win32con.LOGPIXELSY), 72)
        margin_x = max(10, horz // 48)
        content_w = max(horz - 2 * margin_x, 80)

        pt = 9
        h_px = win32api.MulDiv(pt, logpx_y, 72)
        font_normal = win32ui.CreateFont(
            {"name": "Arial", "height": -h_px, "weight": win32con.FW_NORMAL}
        )
        font_bold = win32ui.CreateFont(
            {"name": "Arial", "height": -h_px, "weight": win32con.FW_BOLD}
        )

        hdc.SetMapMode(win32con.MM_TEXT)
        hdc.StartDoc("NUR CRM чек")
        hdc.StartPage()

        def line_h() -> int:
            hdc.SelectObject(font_normal)
            _, cy = hdc.GetTextExtent("Йy")
            return max(cy + 4, h_px + 6)

        lh = line_h()
        y = margin_x

        for row in rows:
            if not row:
                continue
            kind = row[0]
            if kind == "blank":
                y += lh // 2
                continue
            if kind in ("sep", "sep_solid"):
                hdc.SelectObject(font_normal)
                cw, _ = hdc.GetTextExtent("- ")
                n = max(8, content_w // max(cw, 4))
                sep = ("- " * ((n // 2) + 1))[:n]
                hdc.TextOut(margin_x, y, sep)
                y += lh
                continue
            if kind == "center_bold":
                hdc.SelectObject(font_bold)
                s = str(row[1])
                w, _ = hdc.GetTextExtent(s)
                hdc.TextOut(margin_x + max(0, (content_w - w) // 2), y, s)
                y += lh
                continue
            if kind == "center":
                hdc.SelectObject(font_normal)
                s = str(row[1])
                w, _ = hdc.GetTextExtent(s)
                hdc.TextOut(margin_x + max(0, (content_w - w) // 2), y, s)
                y += lh
                continue
            if kind == "left":
                hdc.SelectObject(font_normal)
                hdc.TextOut(margin_x, y, str(row[1]))
                y += lh
                continue
            if kind == "right":
                hdc.SelectObject(font_normal)
                s = str(row[1])
                w, _ = hdc.GetTextExtent(s)
                hdc.TextOut(margin_x + content_w - w, y, s)
                y += lh
                continue
            if kind in ("lr", "lr_bold"):
                hdc.SelectObject(font_bold if kind == "lr_bold" else font_normal)
                left = str(row[1])
                right = str(row[2])
                rw, _ = hdc.GetTextExtent(right)
                hdc.TextOut(margin_x, y, left)
                hdc.TextOut(margin_x + content_w - rw, y, right)
                y += lh
                continue
            hdc.SelectObject(font_normal)
            hdc.TextOut(margin_x, y, str(row[-1]))
            y += lh

        y += lh * 2
        hdc.EndPage()
        hdc.EndDoc()
    except GdiPrintError:
        raise
    except Exception as ex:
        logger.exception("GDI print failed")
        raise GdiPrintError(f"Ошибка печати Windows/GDI: {ex}") from ex
    finally:
        try:
            hdc.DeleteDC()
        except Exception:
            pass
