"""
Локальные настройки термопринтера: файл printer_settings.json рядом с приложением.
Перекрывают значения из config.py (которые берутся из переменных окружения при импорте).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SETTINGS_FILENAME = "printer_settings.json"


def settings_path() -> Path:
    """
    Рядом с exe (PyInstaller onefile) или рядом с printer_config.py при запуске из исходников.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / SETTINGS_FILENAME
    return Path(__file__).resolve().parent / SETTINGS_FILENAME


def as_dict() -> dict[str, Any]:
    import config as c

    return {
        "backend": getattr(c, "RECEIPT_PRINTER_BACKEND", "none"),
        "serial_port": getattr(c, "RECEIPT_SERIAL_PORT", "COM3"),
        "serial_baud": getattr(c, "RECEIPT_SERIAL_BAUDRATE", 9600),
        "network_host": getattr(c, "RECEIPT_NETWORK_HOST", ""),
        "network_port": getattr(c, "RECEIPT_NETWORK_PORT", 9100),
        "file_path": getattr(c, "RECEIPT_FILE_PATH", ""),
        "win32_name": getattr(c, "RECEIPT_WIN32_NAME", ""),
        "usb_vendor": getattr(c, "RECEIPT_USB_VENDOR", ""),
        "usb_product": getattr(c, "RECEIPT_USB_PRODUCT", ""),
        "usb_in_ep": getattr(c, "RECEIPT_USB_IN_EP", 0x82),
        "usb_out_ep": getattr(c, "RECEIPT_USB_OUT_EP", 0x01),
        "text_encoding": getattr(c, "RECEIPT_TEXT_ENCODING", "cp866"),
        "escpos_table": getattr(c, "RECEIPT_ESCPOS_TABLE_BYTE", None),
        "esc_r": getattr(c, "RECEIPT_ESC_R_BYTE", None),
        "escpos_profile": getattr(c, "RECEIPT_ESCPOS_PROFILE", None) or "default",
        "scale_port": getattr(c, "SCALE_COM_PORT", "COM3"),
        "scale_baud": getattr(c, "SCALE_COM_BAUD", 9600),
        "scale_lpt": getattr(c, "SCALE_LPT", "LPT1"),
    }


def apply(data: dict[str, Any]) -> None:
    """Применить словарь к модулю config (мутирует глобальные переменные)."""
    import config as c

    if not data:
        return

    def _s(key: str, attr: str) -> None:
        if key in data and data[key] is not None:
            v = data[key]
            setattr(c, attr, str(v).strip() if isinstance(v, str) else str(v))

    def _i(key: str, attr: str, default: int = 0) -> None:
        if key not in data or data[key] is None:
            return
        v = data[key]
        try:
            setattr(c, attr, int(str(v).strip(), 0))
        except (TypeError, ValueError):
            setattr(c, attr, default)

    if "backend" in data and data["backend"] is not None:
        c.RECEIPT_PRINTER_BACKEND = str(data["backend"]).strip().lower()

    _s("serial_port", "RECEIPT_SERIAL_PORT")
    _i("serial_baud", "RECEIPT_SERIAL_BAUDRATE", 9600)
    _s("network_host", "RECEIPT_NETWORK_HOST")
    _i("network_port", "RECEIPT_NETWORK_PORT", 9100)
    _s("file_path", "RECEIPT_FILE_PATH")
    _s("win32_name", "RECEIPT_WIN32_NAME")
    _s("usb_vendor", "RECEIPT_USB_VENDOR")
    _s("usb_product", "RECEIPT_USB_PRODUCT")
    _i("usb_in_ep", "RECEIPT_USB_IN_EP", 0x82)
    _i("usb_out_ep", "RECEIPT_USB_OUT_EP", 1)
    _s("text_encoding", "RECEIPT_TEXT_ENCODING")

    def _opt_int_key(key: str, attr: str) -> None:
        if key not in data:
            return
        v = data[key]
        if v is None or (isinstance(v, str) and not str(v).strip()):
            setattr(c, attr, None)
            return
        try:
            setattr(c, attr, int(str(v).strip(), 0))
        except (TypeError, ValueError):
            setattr(c, attr, None)

    _opt_int_key("escpos_table", "RECEIPT_ESCPOS_TABLE_BYTE")
    _opt_int_key("esc_r", "RECEIPT_ESC_R_BYTE")

    if "escpos_profile" in data and data["escpos_profile"] is not None:
        ep = str(data["escpos_profile"]).strip()
        c.RECEIPT_ESCPOS_PROFILE = None if ep.lower() in ("", "default", "none") else ep

    _s("scale_port", "SCALE_COM_PORT")
    _i("scale_baud", "SCALE_COM_BAUD", 9600)
    _s("scale_lpt", "SCALE_LPT")


def load_from_disk() -> bool:
    """Загрузить printer_settings.json при наличии. Возвращает True, если файл прочитан."""
    p = settings_path()
    if not p.is_file():
        return False
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return False
    if isinstance(data, dict):
        apply(data)
        return True
    return False


def save(data: dict[str, Any]) -> None:
    """Применить и записать JSON на диск. Поля из data перекрывают текущие; остальные ключи сохраняются."""
    merged = as_dict()
    merged.update(data)
    apply(merged)
    p = settings_path()
    p.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
