"""
Модуль весов (COM) и печати веса на LPT (моноблок).
UI обновляется через page.run_task(async) — не вызывать page.update из потока чтения.

Переменные окружения:
  DESKTOP_MARKET_SCALE_NO_CONSOLE_LOG=1 — не печатать сырые данные в консоль
  DESKTOP_MARKET_SCALE_NO_FILE_LOG=1 — не писать scale_com.log рядом с приложением
  DESKTOP_MARKET_SCALE_REQUEST_HEX — опционально: hex-байты запроса веса, напр. 05 или 57 0d
  DESKTOP_MARKET_SCALE_POLL_MS — интервал повторной отправки запроса (мс), 0 = не слать
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_WEIGHT_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


def _env_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _scale_log_path() -> Path:
    from printer_config import settings_path

    return settings_path().parent / "scale_com.log"


def _append_scale_log(msg: str) -> None:
    """Лог рядом с exe/приложением (в exe нет консоли). Отключить: DESKTOP_MARKET_SCALE_NO_FILE_LOG=1."""
    if _env_bool("DESKTOP_MARKET_SCALE_NO_FILE_LOG"):
        return
    try:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
        with _scale_log_path().open("a", encoding="utf-8", errors="replace") as f:
            f.write(line)
    except OSError:
        pass


def _parse_request_hex(raw: str) -> bytes | None:
    """Строка вида '05' или '57 0d' -> bytes."""
    s = (raw or "").strip()
    if not s:
        return None
    parts = s.replace(",", " ").split()
    out = bytearray()
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p.startswith("0x"):
            p = p[2:]
        try:
            out.append(int(p, 16) & 0xFF)
        except ValueError:
            return None
    return bytes(out) if out else None


def parse_weight_line(raw: bytes | str) -> float | None:
    """
    Извлекает число веса из строки (префиксы ST, GS, kg, %@ и т.д.).
    Берёт последнее «похожее на вес» число.
    """
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8", errors="replace").strip()
        except Exception:
            text = raw.decode("latin-1", errors="replace").strip()
    else:
        text = (raw or "").strip()
    if not text:
        return None
    norm = re.sub(r"[^\d,.\-+eE]", " ", text.replace(",", "."))
    norm = re.sub(r"\s+", " ", norm).strip()
    matches = _WEIGHT_RE.findall(norm)
    if not matches:
        matches = re.findall(r"[-+]?\d+\.?\d*", norm.replace(" ", ""))
    if not matches:
        return None
    for candidate in reversed(matches):
        try:
            v = float(candidate.replace(",", "."))
            return v
        except ValueError:
            continue
    return None


def print_to_lpt(
    text: str,
    path: str | None = None,
    *,
    encoding: str = "cp866",
) -> None:
    """Прямая запись на LPT (или файл). Кириллица: cp866."""
    lpt = (path or os.environ.get("DESKTOP_MARKET_SCALE_LPT", "LPT1")).strip() or "LPT1"
    data = (text or "").encode(encoding, errors="replace")
    try:
        with open(lpt, "wb") as f:
            f.write(data)
            if not data.endswith(b"\n"):
                f.write(b"\n")
    except OSError as e:
        logger.warning("LPT write failed: %s", e)
        raise


def _log_raw_line(line: bytes) -> None:
    try:
        dec = line.decode("utf-8", errors="replace").rstrip("\r\n")
    except Exception:
        dec = repr(line)
    hx = line.hex() if line else ""
    msg = f"raw bytes={len(line)} hex={hx!r} decode={dec!r}"
    _append_scale_log(msg)
    if _env_bool("DESKTOP_MARKET_SCALE_NO_CONSOLE_LOG"):
        return
    print(f"[scale] {msg}", flush=True)


class ScaleManager:
    """Фоновое чтение COM-порта весов и безопасное обновление Flet."""

    def __init__(
        self,
        page: Any,
        weight_ref: Any,
        status_ref: Any | None = None,
        *,
        port: str | None = None,
        baudrate: int | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._page = page
        self._weight_ref = weight_ref
        self._status_ref = status_ref
        self._on_error = on_error
        self._port = (port or os.environ.get("DESKTOP_MARKET_SCALE_PORT", "COM3")).strip()
        self._baud = int(baudrate or os.environ.get("DESKTOP_MARKET_SCALE_BAUD", "9600"))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ser: Any = None
        self._lock = threading.Lock()
        self._last_weight: float | None = None
        self._last_raw: str = ""
        req = os.environ.get("DESKTOP_MARKET_SCALE_REQUEST_HEX", "").strip()
        self._request_bytes = _parse_request_hex(req)
        try:
            self._poll_ms = max(0, int(os.environ.get("DESKTOP_MARKET_SCALE_POLL_MS", "0")))
        except ValueError:
            self._poll_ms = 0
        self._next_poll = 0.0

    def get_last_weight(self) -> float | None:
        with self._lock:
            return self._last_weight

    def get_last_raw(self) -> str:
        with self._lock:
            return self._last_raw

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="ScaleCOM", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        ser = self._ser
        if ser is not None:
            try:
                ser.close()
            except Exception as ex:
                logger.debug("serial close: %s", ex)
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
        self._thread = None
        self._ser = None

    def _set_status(self, msg: str) -> None:
        if not self._status_ref:
            return

        async def _upd() -> None:
            if self._status_ref.current:
                self._status_ref.current.value = msg
            self._page.update()

        try:
            self._page.run_task(_upd)
        except Exception as ex:
            logger.debug("run_task status: %s", ex)

    def _set_weight_ui(self, display: str, raw_hint: str = "") -> None:
        async def _upd() -> None:
            if self._weight_ref.current:
                self._weight_ref.current.value = display
            if raw_hint and self._status_ref and self._status_ref.current:
                self._status_ref.current.value = raw_hint[:80]
            self._page.update()

        try:
            self._page.run_task(_upd)
        except Exception as ex:
            logger.debug("run_task weight: %s", ex)

    def _maybe_send_request(self, ser: Any) -> None:
        if not self._request_bytes or self._poll_ms <= 0:
            return
        now = time.monotonic()
        if now < self._next_poll:
            return
        self._next_poll = now + self._poll_ms / 1000.0
        try:
            ser.write(self._request_bytes)
            ser.flush()
        except Exception as ex:
            logger.debug("scale request write: %s", ex)

    def _run_loop(self) -> None:
        try:
            import serial
        except ImportError:
            self._set_status("Нет pyserial: pip install pyserial")
            if self._on_error:
                self._on_error("Нет пакета pyserial")
            return

        try:
            ser = serial.Serial(
                self._port,
                self._baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.3,
            )
            self._ser = ser
        except Exception as e:
            logger.warning("Весы COM %s: %s", self._port, e)
            _append_scale_log(f"COM open failed {self._port}: {e}")
            self._set_status(f"COM недоступен: {e}")
            if self._on_error:
                self._on_error(str(e))
            return

        _append_scale_log(f"COM opened {self._port} baud={self._baud}")

        if self._request_bytes:
            try:
                ser.write(self._request_bytes)
                ser.flush()
                rq = f"отправлен запрос (hex): {self._request_bytes.hex()}"
                _append_scale_log(rq)
                print(f"[scale] {rq}", flush=True)
            except Exception as ex:
                _append_scale_log(f"запрос не отправлен: {ex}")
                print(f"[scale] не удалось отправить запрос: {ex}", flush=True)

        self._set_status(f"OK {self._port} {self._baud}")
        self._next_poll = 0.0  # первая отправка poll — сразу (если включён POLL_MS)

        while not self._stop.is_set():
            try:
                self._maybe_send_request(ser)
                line = ser.readline()
                if not line:
                    continue
                _log_raw_line(line)
                with self._lock:
                    self._last_raw = line.decode("utf-8", errors="replace").strip()
                w = parse_weight_line(line)
                if w is not None:
                    with self._lock:
                        self._last_weight = w
                    _append_scale_log(f"weight kg={w:.4f}")
                    self._set_weight_ui(f"{w:.3f} кг", "")
                else:
                    hint = self._last_raw[:60] if self._last_raw else ""
                    hx = line.hex() if line else ""
                    short_hex = f"{hx[:24]}…" if len(hx) > 24 else hx
                    if hint:
                        self._set_status(f"нет числа: {hint!r}")
                    self._set_weight_ui("—", short_hex or "нет данных")
            except Exception as e:
                if self._stop.is_set():
                    break
                logger.debug("scale read: %s", e)

        try:
            ser.close()
        except Exception:
            pass
        self._ser = None
