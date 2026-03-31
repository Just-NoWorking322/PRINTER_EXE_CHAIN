"""
Доступ к LPT на Windows: несколько имён устройства и открытие через CreateFile.

«LPT1» в проводнике часто лучше открывать как \\\\.\\LPT1; встроенный open() иногда даёт WinError 22,
а CreateFile с FILE_SHARE_* — нет. pywin32 уже в requirements.txt.
"""

from __future__ import annotations

import re
import sys
from typing import Any

from escpos.escpos import Escpos

_LPT_RE = re.compile(r"^LPT(\d+)$", re.I)


def expand_lpt_device_paths(raw: str) -> list[str]:
    """Варианты пути для одного логического порта (порядок: сначала \\\\.\\ — обычно надёжнее)."""
    s = (raw or "").strip().replace("/", "\\")
    if not s:
        return []
    if sys.platform != "win32":
        return [s]
    out: list[str] = []
    seen: set[str] = set()

    def add(p: str) -> None:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)

    m = _LPT_RE.match(s)
    if m:
        u = f"LPT{m.group(1)}"
        add(f"\\\\.\\{u}")
        add(u)
        if s != u:
            add(s)
    elif s.lower().startswith("\\\\.\\") or s.lower().startswith("//./"):
        norm = s if s.lower().startswith("\\\\.\\") else "\\\\.\\" + s[4:].lstrip("\\")
        add(norm)
        if s != norm:
            add(s)
    elif len(s) >= 2 and s[1] == ":":
        # Файл на диске C:\... — не трогаем
        add(s)
    elif s.startswith("\\\\") and not s.lower().startswith("\\\\.\\"):
        # UNC \\server\share
        add(s)
    else:
        add(s)
        if not s.startswith("\\\\"):
            add("\\\\.\\" + s.lstrip("\\"))
    return out


class Win32RawEscpos(Escpos):
    """ESC/POS на LPT через CreateFile (GENERIC_READ|WRITE + share), без встроенного open()."""

    def __init__(self, devfile: str, auto_flush: bool = True, *args: Any, **kwargs: Any) -> None:
        import win32con
        import win32file

        super().__init__(*args, **kwargs)
        self.devfile = devfile
        self.auto_flush = auto_flush
        self._h: Any = None
        access = win32con.GENERIC_WRITE | win32con.GENERIC_READ
        share = win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE
        self._h = win32file.CreateFile(
            devfile,
            access,
            share,
            None,
            win32con.OPEN_EXISTING,
            0,
            None,
        )

    def flush(self) -> None:
        if self._h is None:
            return
        import win32file

        win32file.FlushFileBuffers(self._h)

    def _raw(self, msg: bytes) -> None:
        import win32file

        if self._h is None:
            raise OSError("LPT: handle закрыт")
        win32file.WriteFile(self._h, msg)
        if self.auto_flush:
            self.flush()

    def close(self) -> None:
        if self._h is None:
            return
        import win32file

        try:
            self.flush()
        except Exception:
            pass
        try:
            win32file.CloseHandle(self._h)
        finally:
            self._h = None


def open_escpos_lpt(devfile: str, **prof_kw: Any) -> Any:
    """
    Подключение ESC/POS к LPT (или другому raw-устройству по имени).
    Сначала встроенный open('wb'), затем Win32RawEscpos по каждому кандидату пути.
    """
    from escpos.printer import File

    paths = expand_lpt_device_paths(devfile)
    if not paths:
        paths = [(devfile or "").strip() or "LPT1"]

    errors: list[tuple[str, BaseException]] = []

    for p in paths:
        try:
            return File(devfile=p, **prof_kw)
        except OSError as e:
            errors.append((p, e))
        except Exception as e:
            errors.append((p, e))

    if sys.platform == "win32":
        for p in paths:
            try:
                return Win32RawEscpos(devfile=p, **prof_kw)
            except BaseException as e:
                errors.append((p + " (Win32)", e))

    msg = "; ".join(f"{path}: {err}" for path, err in errors[-8:])
    raise OSError(f"LPT не открыть ({devfile!r}): {msg}")


def write_lpt_bytes(devfile: str, data: bytes, *, append_lf_if_missing: bool = True) -> None:
    """Прямая запись байтов (тест весов и т.п.): те же кандидаты и fallback Win32."""
    blob = data
    if append_lf_if_missing and blob and not blob.endswith(b"\n"):
        blob = blob + b"\n"

    paths = expand_lpt_device_paths(devfile)
    if not paths:
        paths = [(devfile or "").strip() or "LPT1"]

    errors: list[str] = []

    for p in paths:
        try:
            with open(p, "wb") as f:
                f.write(blob)
                f.flush()
            return
        except OSError as e:
            errors.append(f"{p}: {e}")

    if sys.platform == "win32":
        import win32con
        import win32file

        for p in paths:
            h = None
            try:
                access = win32con.GENERIC_WRITE | win32con.GENERIC_READ
                share = win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE
                h = win32file.CreateFile(
                    p,
                    access,
                    share,
                    None,
                    win32con.OPEN_EXISTING,
                    0,
                    None,
                )
                win32file.WriteFile(h, blob)
                win32file.FlushFileBuffers(h)
                return
            except BaseException as e:
                errors.append(f"{p} (Win32): {e}")
            finally:
                if h is not None:
                    try:
                        win32file.CloseHandle(h)
                    except Exception:
                        pass

    raise OSError("LPT: " + "; ".join(errors[-6:]))
