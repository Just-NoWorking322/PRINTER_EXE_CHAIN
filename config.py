import os

# Базовый URL бэкенда (без завершающего слэша)
API_BASE_URL = os.environ.get("DESKTOP_MARKET_API_URL", "https://app.nurcrm.kg").rstrip("/")

# Предзаполнение формы входа (временно для теста; в продакшене убрать или вынести в .env)
TEST_LOGIN_EMAIL = "market@gmail.com"
TEST_LOGIN_PASSWORD = "12345678!"

# --- Термопринтер ESC/POS: только LPT ---
# Чек всегда печатается через параллельный порт Windows: LPT1 / LPT2.
RECEIPT_PRINTER_BACKEND = "lpt"
# Путь LPT для чека; если переменная не задана, используем LPT1.
RECEIPT_FILE_PATH = os.environ.get("DESKTOP_MARKET_RECEIPT_FILE", "LPT1").strip() or "LPT1"
# Кодировка текста: cp866 | cp1251 | wpc1251 | utf-8 (utf-8 → cp1251; wpc1251 = слот 46)
# По умолчанию включена кириллица WPC1251 — обычно это надёжный старт для 58 мм ESC/POS по LPT.
RECEIPT_TEXT_ENCODING = os.environ.get("DESKTOP_MARKET_RECEIPT_ENCODING", "wpc1251").strip()


def _opt_int_env(key: str) -> int | None:
    v = os.environ.get(key, "").strip()
    if not v:
        return None
    try:
        return int(v, 0)
    except ValueError:
        return None


# Номер таблицы для команды ESC t (0–255). Пусто = по кодировке (866→17, 1251→46).
RECEIPT_ESCPOS_TABLE_BYTE = _opt_int_env("DESKTOP_MARKET_RECEIPT_ESCPOS_TABLE")
# Международный набор ESC R (часто 0 или 6; пусто = не отправлять)
RECEIPT_ESC_R_BYTE = _opt_int_env("DESKTOP_MARKET_RECEIPT_ESC_R")
# Профиль python-escpos: пусто | TEP-200M (Cashino EP-200 / TEP200M, WPC1251 → слот 46)
_ep = os.environ.get("DESKTOP_MARKET_RECEIPT_PROFILE", "").strip()
RECEIPT_ESCPOS_PROFILE = _ep if _ep else None
# Перед ESC t на EP-200/аналогах шлётся ESC % 0 (cp1251/wpc1251, слот 46, cp866+17 и т.д.); отключить: DESKTOP_MARKET_RECEIPT_NO_ESC_PCT=1

# Весы (COM) и LPT для тестовой печати веса — перекрываются JSON в NurMarketKassa.sqlite3
SCALE_COM_PORT = os.environ.get("DESKTOP_MARKET_SCALE_PORT", "COM3").strip() or "COM3"
try:
    SCALE_COM_BAUD = int(os.environ.get("DESKTOP_MARKET_SCALE_BAUD", "9600"), 0)
except ValueError:
    SCALE_COM_BAUD = 9600
SCALE_LPT = (os.environ.get("DESKTOP_MARKET_SCALE_LPT", "LPT1").strip() or "LPT1")
# Блок весов на кассе по умолчанию включён (main.py: DESKTOP_MARKET_SCALE_ENABLED пусто или «1»; «0» — выкл.).


def _env_truthy(key: str, default: str = "0") -> bool:
    v = os.environ.get(key, default).strip().lower()
    return v in ("1", "true", "yes", "on")


# Синхронизация офлайн-чеков: если сервер отклоняет строку из‑за остатков (в т.ч. учёт в пачках),
# добавить её как произвольную позицию без привязки к карточке — чек уйдёт в CRM, остаток по SKU не спишется.
# Отключить: DESKTOP_MARKET_POS_SYNC_STOCK_FALLBACK=0
# «Минус на остатке» задаётся только на стороне NurCRM; клиент не может обойти проверку API иначе.
POS_SYNC_FALLBACK_CUSTOM_ON_STOCK_ERROR = _env_truthy(
    "DESKTOP_MARKET_POS_SYNC_STOCK_FALLBACK", "1"
)

# --- Производительность / слабое железо ---
# DESKTOP_MARKET_LOW_SPEC=1 — снижает лимиты каталога, параллельную подгрузку превью, частоту опроса офлайн-синка.
# DESKTOP_MARKET_DISABLE_CATALOG_IMAGES=1 — не запрашивать картинки товаров в сетке (остаётся иконка-заглушка).
# Точечная настройка (если LOW_SPEC=0): DESKTOP_MARKET_QUICK_CATALOG_LIMIT, _THUMB_CONCURRENCY,
# _THUMB_URL_TRIES, _QUICK_CATALOG_PAGES, _QUICK_CATALOG_POOL_MULT, _QUICK_CATALOG_PAGE_ROWS,
# _LIVE_SEARCH_DEBOUNCE, _OFFLINE_SYNC_POLL_SEC, _CATALOG_IMAGE_BOX_HEIGHT.

LOW_SPEC_MODE = _env_truthy("DESKTOP_MARKET_LOW_SPEC", "0")
DISABLE_CATALOG_IMAGES = _env_truthy("DESKTOP_MARKET_DISABLE_CATALOG_IMAGES", "0")


def catalog_images_disabled() -> bool:
    return DISABLE_CATALOG_IMAGES


def _perf_int(key: str, default: int, *, low: int, clamp_min: int = 1, clamp_max: int | None = None) -> int:
    if LOW_SPEC_MODE:
        v = low
    else:
        raw = os.environ.get(key, "").strip()
        if not raw:
            v = default
        else:
            try:
                v = int(raw, 0)
            except ValueError:
                v = default
    v = max(clamp_min, v)
    if clamp_max is not None:
        v = min(v, clamp_max)
    return v


def _perf_float(key: str, default: float, *, low: float, clamp_min: float) -> float:
    if LOW_SPEC_MODE:
        v = low
    else:
        raw = os.environ.get(key, "").strip()
        if not raw:
            v = default
        else:
            try:
                v = float(raw.replace(",", "."))
            except ValueError:
                v = default
    return max(clamp_min, v)


def pos_performance_quick_catalog_limit() -> int:
    return _perf_int(
        "DESKTOP_MARKET_QUICK_CATALOG_LIMIT",
        60,
        low=36,
        clamp_min=12,
        clamp_max=200,
    )


def pos_performance_catalog_pool_multiplier() -> int:
    return _perf_int(
        "DESKTOP_MARKET_QUICK_CATALOG_POOL_MULT",
        8,
        low=5,
        clamp_min=2,
        clamp_max=24,
    )


def pos_performance_catalog_pages() -> int:
    return _perf_int(
        "DESKTOP_MARKET_QUICK_CATALOG_PAGES",
        10,
        low=6,
        clamp_min=1,
        clamp_max=30,
    )


def pos_performance_thumb_concurrency() -> int:
    return _perf_int(
        "DESKTOP_MARKET_THUMB_CONCURRENCY",
        6,
        low=2,
        clamp_min=1,
        clamp_max=12,
    )


def pos_performance_thumb_url_tries() -> int:
    return _perf_int(
        "DESKTOP_MARKET_THUMB_URL_TRIES",
        6,
        low=3,
        clamp_min=1,
        clamp_max=12,
    )


def pos_performance_quick_catalog_page_rows() -> int:
    return _perf_int(
        "DESKTOP_MARKET_QUICK_CATALOG_PAGE_ROWS",
        3,
        low=2,
        clamp_min=1,
        clamp_max=8,
    )


def pos_performance_live_search_debounce() -> float:
    return _perf_float(
        "DESKTOP_MARKET_LIVE_SEARCH_DEBOUNCE",
        0.22,
        low=0.45,
        clamp_min=0.05,
    )


def pos_performance_offline_sync_poll() -> float:
    return _perf_float(
        "DESKTOP_MARKET_OFFLINE_SYNC_POLL_SEC",
        5.0,
        low=12.0,
        clamp_min=2.0,
    )


def pos_performance_catalog_image_box_height() -> int:
    return _perf_int(
        "DESKTOP_MARKET_CATALOG_IMAGE_BOX_HEIGHT",
        148,
        low=108,
        clamp_min=72,
        clamp_max=220,
    )
