"""
Локальные настройки термопринтера: JSON в SQLite (единый файл NurMarketKassa.sqlite3).
Перекрывают значения из config.py (которые берутся из переменных окружения при импорте).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def settings_path() -> Path:
    """Путь к единому файлу данных приложения (SQLite), рядом с exe или с модулем."""
    import app_database

    return app_database.data_db_path()


def as_dict() -> dict[str, Any]:
    import config as c

    return {
        "backend": "lpt",
        "file_path": getattr(c, "RECEIPT_FILE_PATH", "LPT1"),
        "text_encoding": getattr(c, "RECEIPT_TEXT_ENCODING", "wpc1251"),
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

    c.RECEIPT_PRINTER_BACKEND = "lpt"
    _s("file_path", "RECEIPT_FILE_PATH")
    if not getattr(c, "RECEIPT_FILE_PATH", "").strip():
        c.RECEIPT_FILE_PATH = "LPT1"
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
    """Загрузить настройки из SQLite (kv_store). Возвращает True, если запись есть."""
    import app_database

    app_database.init_database()
    raw = app_database.kv_get(app_database.KV_KEY_PRINTER)
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if isinstance(data, dict):
        apply(data)
        return True
    return False


def save(data: dict[str, Any]) -> None:
    """Применить и сохранить JSON в SQLite. Поля из data перекрывают текущие; остальные ключи сохраняются."""
    import app_database

    merged = as_dict()
    merged.update(data)
    apply(merged)
    app_database.init_database()
    app_database.kv_set(
        app_database.KV_KEY_PRINTER,
        json.dumps(merged, ensure_ascii=False),
    )
