from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any


_VID_PID_RE = re.compile(r"VID_([0-9A-F]{4}).*PID_([0-9A-F]{4})", re.IGNORECASE)


def _ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _extract_vid_pid(device_id: str) -> tuple[str, str]:
    raw = (device_id or "").strip()
    if not raw:
        return "", ""
    m = _VID_PID_RE.search(raw)
    if not m:
        return "", ""
    return m.group(1).upper(), m.group(2).upper()


def _run_powershell_json(script: str) -> Any:
    if sys.platform != "win32":
        return None
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _normalize_powershell_inventory(data: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(data, dict):
        return [], []
    printers = [row for row in _ensure_list(data.get("printers")) if isinstance(row, dict)]
    devices = [row for row in _ensure_list(data.get("devices")) if isinstance(row, dict)]
    return printers, devices


def _printer_ports() -> list[dict[str, Any]]:
    script = r"""
$rows = @(Get-PrinterPort | ForEach-Object {
    [pscustomobject]@{
        name = [string]$_.Name
        description = [string]$_.Description
        monitor = [string]$_.PortMonitor
    }
})
$rows | ConvertTo-Json -Depth 4 -Compress
"""
    data = _run_powershell_json(script)
    return [row for row in _ensure_list(data) if isinstance(row, dict)]


def _is_usb_printer(printer: dict[str, Any]) -> bool:
    port_name = str(printer.get("port_name") or "").strip().lower()
    pnp_device_id = str(printer.get("pnp_device_id") or "").strip().lower()
    name = str(printer.get("name") or "").strip().lower()
    if "usb" in port_name or "dot4" in port_name:
        return True
    if pnp_device_id.startswith("usbprint\\") or pnp_device_id.startswith("usb\\"):
        return True
    return "xprinter" in name


def _device_key(printer: dict[str, Any], device: dict[str, Any] | None) -> str:
    pnp = str(printer.get("pnp_device_id") or "").strip()
    if pnp:
        return pnp
    instance_id = str((device or {}).get("instance_id") or "").strip()
    if instance_id:
        return instance_id
    name = str(printer.get("name") or "").strip()
    if name:
        return name
    return "usb-printer"


def _match_usb_device(
    printer: dict[str, Any],
    devices: list[dict[str, Any]],
) -> dict[str, Any] | None:
    pnp = str(printer.get("pnp_device_id") or "").strip().lower()
    name = str(printer.get("name") or "").strip().lower()
    if pnp:
        for item in devices:
            instance = str(item.get("instance_id") or "").strip().lower()
            if not instance:
                continue
            if pnp == instance or pnp.startswith(instance) or instance.startswith(pnp):
                return item
    for item in devices:
        friendly = str(item.get("friendly_name") or "").strip().lower()
        if friendly and friendly in name:
            return item
    return None


def _compose_display_name(printer: dict[str, Any], device: dict[str, Any] | None) -> str:
    name = str(printer.get("name") or "").strip() or str((device or {}).get("friendly_name") or "").strip() or "USB printer"
    port_name = str(printer.get("port_name") or "").strip()
    vid, pid = _extract_vid_pid(
        str(printer.get("pnp_device_id") or "").strip() or str((device or {}).get("instance_id") or "").strip()
    )
    suffix: list[str] = []
    if port_name:
        suffix.append(port_name)
    if vid and pid:
        suffix.append(f"VID {vid} PID {pid}")
    if not suffix:
        return name
    return f"{name} | {' | '.join(suffix)}"


def _powershell_inventory() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    script = r"""
$printers = @(Get-CimInstance Win32_Printer | ForEach-Object {
    [pscustomobject]@{
        name = [string]$_.Name
        driver_name = [string]$_.DriverName
        port_name = [string]$_.PortName
        pnp_device_id = [string]$_.PNPDeviceID
        printer_status = [string]$_.PrinterStatus
    }
})
$devices = @()
try {
    $devices = @(Get-PnpDevice -PresentOnly | Where-Object {
        $_.InstanceId -like 'USB*' -or $_.InstanceId -like 'USBPRINT*'
    } | ForEach-Object {
        [pscustomobject]@{
            friendly_name = [string]$_.FriendlyName
            instance_id = [string]$_.InstanceId
            class_name = [string]$_.Class
            status = [string]$_.Status
        }
    })
} catch {
    $devices = @()
}
[pscustomobject]@{
    printers = $printers
    devices = $devices
} | ConvertTo-Json -Depth 6 -Compress
"""
    return _normalize_powershell_inventory(_run_powershell_json(script))


def _pnp_usb_candidates() -> list[dict[str, Any]]:
    script = r"""
$rows = @(Get-CimInstance Win32_PnPEntity | Where-Object {
    $_.DeviceID -like 'USB\VID_*' -or
    $_.DeviceID -like 'USBPRINT\*' -or
    $_.Name -like '*PrinterPOS*' -or
    $_.Name -like '*printer*'
} | ForEach-Object {
    [pscustomobject]@{
        name = [string]$_.Name
        device_id = [string]$_.DeviceID
        pnp_class = [string]$_.PNPClass
        manufacturer = [string]$_.Manufacturer
        status = [string]$_.Status
    }
})
$rows | ConvertTo-Json -Depth 5 -Compress
"""
    data = _run_powershell_json(script)
    return [row for row in _ensure_list(data) if isinstance(row, dict)]


def _match_usb_port(name: str, device_id: str, ports: list[dict[str, Any]]) -> dict[str, Any] | None:
    name_low = (name or "").strip().lower()
    device_low = (device_id or "").strip().lower()
    for port in ports:
        port_name = str(port.get("name") or "").strip()
        description = str(port.get("description") or "").strip()
        monitor = str(port.get("monitor") or "").strip().lower()
        desc_low = description.lower()
        if name_low and name_low == desc_low:
            return port
        if name_low and name_low in desc_low:
            return port
        if device_low and port_name and device_low.endswith("\\" + port_name.lower()):
            return port
        if monitor == "dynamic print monitor" and desc_low and (name_low in desc_low or desc_low in name_low):
            return port
    return None


def _looks_like_printer_candidate(item: dict[str, Any]) -> bool:
    name = str(item.get("name") or "").strip().lower()
    device_id = str(item.get("device_id") or "").strip().lower()
    pnp_class = str(item.get("pnp_class") or "").strip().lower()
    manufacturer = str(item.get("manufacturer") or "").strip().lower()
    haystack = " ".join(part for part in (name, device_id, pnp_class, manufacturer) if part)
    include_tokens = (
        "printer",
        "print",
        "printerpos",
        "xprinter",
        "receipt",
        "thermal",
        "pos",
        "label",
        "1fc9",
        "2016",
        "usbprint\\",
        "softwaredevice",
    )
    exclude_tokens = (
        "controller",
        "host",
        "hub",
        "bluetooth",
        "wireless",
        "camera",
        "audio",
        "keyboard",
        "mouse",
        "composite",
        "storage",
        "flash",
    )
    if any(token in haystack for token in exclude_tokens):
        return False
    return any(token in haystack for token in include_tokens)


def _fallback_win32_printers() -> list[dict[str, Any]]:
    try:
        import win32print
    except Exception:
        return []

    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    try:
        rows = win32print.EnumPrinters(flags, None, 4)
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not (isinstance(row, tuple) and len(row) >= 3):
            continue
        name = str(row[2] or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        port_name = ""
        driver_name = ""
        try:
            hprinter = win32print.OpenPrinter(name)
            try:
                info = win32print.GetPrinter(hprinter, 2)
                if isinstance(info, dict):
                    port_name = str(info.get("pPortName") or info.get("PortName") or "").strip()
                    driver_name = str(info.get("pDriverName") or info.get("DriverName") or "").strip()
            finally:
                win32print.ClosePrinter(hprinter)
        except Exception:
            pass
        printer = {
            "name": name,
            "driver_name": driver_name,
            "port_name": port_name,
            "pnp_device_id": "",
            "printer_status": "",
        }
        if _is_usb_printer(printer):
            out.append(
                {
                    "device_key": name,
                    "display_name": _compose_display_name(printer, None),
                    "printer_name": name,
                    "friendly_name": name,
                    "driver_name": driver_name,
                    "port_name": port_name,
                    "pnp_device_id": "",
                    "instance_id": "",
                    "class_name": "Printer",
                    "status": "",
                    "vid": "",
                    "pid": "",
                }
            )
    return out


def list_usb_printers() -> list[dict[str, Any]]:
    if sys.platform != "win32":
        return []

    printers, devices = _powershell_inventory()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for printer in printers:
        if not _is_usb_printer(printer):
            continue
        matched = _match_usb_device(printer, devices)
        device_key = _device_key(printer, matched)
        seen_key = device_key.lower()
        if seen_key in seen:
            continue
        seen.add(seen_key)
        instance_id = str((matched or {}).get("instance_id") or "").strip()
        pnp_device_id = str(printer.get("pnp_device_id") or "").strip()
        vid, pid = _extract_vid_pid(pnp_device_id or instance_id)
        out.append(
            {
                "device_key": device_key,
                "display_name": _compose_display_name(printer, matched),
                "printer_name": str(printer.get("name") or "").strip(),
                "friendly_name": str((matched or {}).get("friendly_name") or printer.get("name") or "").strip(),
                "driver_name": str(printer.get("driver_name") or "").strip(),
                "port_name": str(printer.get("port_name") or "").strip(),
                "pnp_device_id": pnp_device_id,
                "instance_id": instance_id,
                "class_name": str((matched or {}).get("class_name") or "Printer").strip(),
                "status": str((matched or {}).get("status") or printer.get("printer_status") or "").strip(),
                "vid": vid,
                "pid": pid,
            }
        )

    if out:
        return sorted(out, key=lambda x: str(x.get("display_name") or "").lower())

    fallback_printers = _fallback_win32_printers()
    if fallback_printers:
        return fallback_printers

    raw_usb = _pnp_usb_candidates()
    ports = _printer_ports()
    raw_out: list[dict[str, Any]] = []
    seen_raw: set[str] = set()
    for item in raw_usb:
        if not _looks_like_printer_candidate(item):
            continue
        device_id = str(item.get("device_id") or "").strip()
        if not device_id:
            continue
        seen_key = device_id.lower()
        if seen_key in seen_raw:
            continue
        seen_raw.add(seen_key)
        vid, pid = _extract_vid_pid(device_id)
        name = str(item.get("name") or "").strip() or "USB printer device"
        manufacturer = str(item.get("manufacturer") or "").strip()
        matched_port = _match_usb_port(name, device_id, ports)
        port_name = str((matched_port or {}).get("name") or "").strip()
        suffix = [
            part
            for part in (
                manufacturer,
                port_name,
                f"VID {vid} PID {pid}" if vid and pid else "",
            )
            if part
        ]
        display_name = f"{name} | {' | '.join(suffix)}" if suffix else name
        raw_out.append(
            {
                "device_key": device_id,
                "display_name": display_name,
                "printer_name": "",
                "friendly_name": name,
                "driver_name": "",
                "port_name": port_name,
                "pnp_device_id": device_id,
                "instance_id": device_id,
                "class_name": str(item.get("pnp_class") or "USB").strip(),
                "status": str(item.get("status") or "").strip(),
                "manufacturer": manufacturer,
                "vid": vid,
                "pid": pid,
            }
        )
    return sorted(raw_out, key=lambda x: str(x.get("display_name") or "").lower())
