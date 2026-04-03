"""
Клиентская валидация ввода для кассового приложения.
Возвращает пустую строку при успехе, иначе текст ошибки для показа пользователю.
"""

from __future__ import annotations

import re
from typing import Any

# Простая проверка email (без полного RFC)
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def parse_decimal(
    raw: Any,
    *,
    field_name: str = "Значение",
    allow_negative: bool = False,
) -> tuple[float | None, str]:
    """
    Разбор десятичного числа. Поддерживает запятую как разделитель.
    Возвращает (value, error). При error непустом value = None.
    """
    if raw is None:
        return None, f"{field_name}: пусто"
    s = str(raw).strip().replace(",", ".")
    if not s:
        return None, f"{field_name}: пусто"
    try:
        v = float(s)
    except ValueError:
        return None, f"{field_name}: введите число"
    if not allow_negative and v < 0:
        return None, f"{field_name}: не может быть отрицательным"
    return v, ""


def validate_email(raw: Any) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return "Введите email"
    if not _EMAIL_RE.match(s):
        return "Некорректный формат email"
    return ""


def validate_password(raw: Any, min_len: int = 1) -> str:
    if raw is None or str(raw).strip() == "":
        return "Введите пароль"
    if len(str(raw)) < min_len:
        return f"Пароль не короче {min_len} символов"
    return ""


def validate_uuid(raw: Any) -> str:
    s = (raw or "").strip()
    if not s:
        return "Укажите идентификатор (UUID) кассы"
    if not _UUID_RE.match(s):
        return "Некорректный UUID (ожидается формат xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)"
    return ""


_CASHBOX_ID_RE = re.compile(r"^[a-zA-Z0-9_.:-]+$")


def validate_cashbox_id(raw: Any) -> str:
    """
    Идентификатор кассы для открытия смены: UUID или любой непустой id от API (число, slug и т.д.).
    """
    s = (raw or "").strip()
    if not s:
        return "Укажите кассу"
    if len(s) > 64:
        return "Идентификатор кассы не длиннее 64 символов"
    if _UUID_RE.match(s) or _CASHBOX_ID_RE.match(s):
        return ""
    return "Некорректный идентификатор кассы"


def validate_non_negative_money(raw: Any, field_name: str = "Сумма") -> str:
    v, err = parse_decimal(raw, field_name=field_name, allow_negative=False)
    if err:
        return err
    assert v is not None
    return ""


def validate_percent_discount(raw: Any) -> str:
    """Процент скидки на чек: 0–100."""
    s = (raw or "").strip()
    if not s:
        return "Введите процент"
    v, err = parse_decimal(s, field_name="Процент", allow_negative=False)
    if err:
        return err
    assert v is not None
    if v > 100:
        return "Процент не может быть больше 100"
    return ""


def validate_order_discount_sum(raw: Any) -> str:
    """Фиксированная скидка на чек (сумма)."""
    s = (raw or "").strip()
    if not s:
        return "Введите сумму скидки"
    return validate_non_negative_money(s, "Сумма скидки")


def validate_quantity(raw: Any) -> str:
    v, err = parse_decimal(raw, field_name="Количество", allow_negative=False)
    if err:
        return err
    assert v is not None
    if v <= 0:
        return "Количество должно быть больше нуля"
    if v > 1_000_000:
        return "Слишком большое количество"
    return ""


def validate_unit_price(raw: Any) -> str:
    v, err = parse_decimal(raw, field_name="Цена", allow_negative=False)
    if err:
        return err
    assert v is not None
    if v > 1e12:
        return "Некорректно большая цена"
    return ""


def validate_line_discount(raw: Any) -> str:
    v, err = parse_decimal(raw, field_name="Скидка на строку", allow_negative=False)
    if err:
        return err
    return ""


def validate_cash_received(raw: Any, total_due: float) -> str:
    v, err = parse_decimal(raw, field_name="Получено наличными", allow_negative=False)
    if err:
        return err
    assert v is not None
    if total_due > 0 and v + 1e-9 < total_due:
        return f"Сумма не меньше к оплате ({total_due:.2f} сом)"
    return ""


def normalize_decimal_string(raw: Any) -> str:
    """Строка для API, без лишних символов."""
    s = str(raw or "").strip().replace(",", ".")
    return s


def validate_search_query(q: str, *, max_len: int = 200) -> str:
    s = (q or "").strip()
    if len(s) > max_len:
        return f"Запрос не длиннее {max_len} символов"
    return ""


def validate_product_id(raw: Any) -> str:
    """Идентификатор товара (UUID или другой id от API)."""
    s = str(raw or "").strip()
    if not s:
        return "Не выбран товар"
    if len(s) > 64:
        return "Некорректный идентификатор товара"
    return ""


def normalize_barcode_for_scan(barcode: str, *, max_len: int = 64) -> tuple[str, str]:
    s = (barcode or "").strip()
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)
    s = s.replace("\ufeff", "").strip()
    if not s:
        return "", "Пустой штрихкод"
    if len(s) > max_len:
        return "", f"Штрихкод не длиннее {max_len} символов"
    return s, ""
