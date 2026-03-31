import os

# Базовый URL бэкенда (без завершающего слэша)
API_BASE_URL = os.environ.get("DESKTOP_MARKET_API_URL", "https://app.nurcrm.kg").rstrip("/")

# Предзаполнение формы входа (временно для теста; в продакшене убрать или вынести в .env)
TEST_LOGIN_EMAIL = "market@gmail.com"
TEST_LOGIN_PASSWORD = "12345678!"

# --- Печать чека ---

# Оставлен USB-режим raw-печати.
RECEIPT_PRINT_MODE = os.environ.get("DESKTOP_MARKET_RECEIPT_PRINT_MODE", "lpt").strip().lower()
RECEIPT_GDI_PRINTER_NAME = os.environ.get(
    "DESKTOP_MARKET_RECEIPT_GDI_PRINTER",
    "",
).strip()
# USB-печать: target = USB-порт или имя Windows-принтера, если оно появится в системе.
RECEIPT_PRINTER_BACKEND = os.environ.get("DESKTOP_MARKET_RECEIPT_BACKEND", "usb").strip().lower() or "usb"
RECEIPT_FILE_PATH = os.environ.get("DESKTOP_MARKET_RECEIPT_FILE", "").strip()
RECEIPT_USB_DEVICE_ID = os.environ.get("DESKTOP_MARKET_RECEIPT_USB_DEVICE_ID", "").strip()
RECEIPT_USB_PRINTER_NAME = os.environ.get("DESKTOP_MARKET_RECEIPT_USB_PRINTER_NAME", "").strip()
RECEIPT_USB_FRIENDLY_NAME = os.environ.get("DESKTOP_MARKET_RECEIPT_USB_FRIENDLY_NAME", "").strip()
RECEIPT_USB_PORT_NAME = os.environ.get("DESKTOP_MARKET_RECEIPT_USB_PORT_NAME", "").strip()
RECEIPT_USB_VENDOR_ID = os.environ.get("DESKTOP_MARKET_RECEIPT_USB_VENDOR_ID", "").strip()
RECEIPT_USB_PRODUCT_ID = os.environ.get("DESKTOP_MARKET_RECEIPT_USB_PRODUCT_ID", "").strip()
# Для generic USB-драйверов вроде PrinterPOS-80 безопасный старт: plain text + CRLF.
RECEIPT_LPT_DRIVER = os.environ.get("DESKTOP_MARKET_RECEIPT_LPT_DRIVER", "text").strip().lower()
RECEIPT_LPT_LINE_ENDING = os.environ.get("DESKTOP_MARKET_RECEIPT_LPT_EOL", "crlf").strip().lower()
# Кодировка текста: cp866 | cp1251 | wpc1251 | utf-8 (utf-8 → cp1251; wpc1251 = слот 46)
# Для XPrinter 80 мм по USB разумный старт: CP866 с таблицей ESC t 17.
RECEIPT_TEXT_ENCODING = os.environ.get("DESKTOP_MARKET_RECEIPT_ENCODING", "cp866").strip()


def _opt_int_env(key: str) -> int | None:
    v = os.environ.get(key, "").strip()
    if not v:
        return None
    try:
        return int(v, 0)
    except ValueError:
        return None


# Номер таблицы для команды ESC t (0–255). Пусто в env = стартовый пресет 17 (CP866).
_escpos_table = _opt_int_env("DESKTOP_MARKET_RECEIPT_ESCPOS_TABLE")
RECEIPT_ESCPOS_TABLE_BYTE = 17 if _escpos_table is None else _escpos_table
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
