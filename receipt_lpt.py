from __future__ import annotations

import codecs
import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RECEIPT_WIDTH = 48

LPT_DRIVER_ESCPOS = "escpos"
LPT_DRIVER_TEXT = "text"

LPT_LINE_ENDING_LF = "lf"
LPT_LINE_ENDING_CRLF = "crlf"


@dataclass(frozen=True)
class CodepageSpec:
    key: str
    label: str
    python_codec: str
    capability_names: tuple[str, ...]
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class CodepagePlan:
    requested_key: str
    python_codec: str
    escpos_table: int | None
    escpos_table_source: str
    capability_name: str | None


CODEPAGE_SPECS: tuple[CodepageSpec, ...] = (
    CodepageSpec(
        key="wpc1251",
        label="WPC1251 / CP1251 — частый выбор для ESC/POS-кириллицы",
        python_codec="cp1251",
        capability_names=("CP1251",),
        aliases=("windows1251", "windows-1251", "cp-1251"),
    ),
    CodepageSpec(
        key="cp1251",
        label="CP1251 — Windows кириллица",
        python_codec="cp1251",
        capability_names=("CP1251",),
        aliases=("windows1251", "windows-1251", "cp-1251"),
    ),
    CodepageSpec(
        key="cp866",
        label="CP866 — DOS/OEM кириллица",
        python_codec="cp866",
        capability_names=("CP866",),
        aliases=("ibm866",),
    ),
    CodepageSpec(
        key="cp855",
        label="CP855 — старая DOS кириллица",
        python_codec="cp855",
        capability_names=("CP855",),
    ),
    CodepageSpec(
        key="cp437",
        label="CP437 — US / Generic POS",
        python_codec="cp437",
        capability_names=("CP437",),
    ),
    CodepageSpec(
        key="cp850",
        label="CP850 — Western Europe",
        python_codec="cp850",
        capability_names=("CP850",),
    ),
    CodepageSpec(
        key="cp852",
        label="CP852 — Central Europe",
        python_codec="cp852",
        capability_names=("CP852",),
    ),
    CodepageSpec(
        key="cp857",
        label="CP857 — Turkish",
        python_codec="cp857",
        capability_names=("CP857",),
    ),
    CodepageSpec(
        key="cp858",
        label="CP858 — Western Europe + euro",
        python_codec="cp858",
        capability_names=("CP858",),
    ),
    CodepageSpec(
        key="cp860",
        label="CP860 — Portuguese",
        python_codec="cp860",
        capability_names=("CP860",),
    ),
    CodepageSpec(
        key="cp861",
        label="CP861 — Icelandic",
        python_codec="cp861",
        capability_names=("CP861",),
    ),
    CodepageSpec(
        key="cp862",
        label="CP862 — Hebrew",
        python_codec="cp862",
        capability_names=("CP862",),
    ),
    CodepageSpec(
        key="cp863",
        label="CP863 — Canadian French",
        python_codec="cp863",
        capability_names=("CP863",),
    ),
    CodepageSpec(
        key="cp864",
        label="CP864 — Arabic",
        python_codec="cp864",
        capability_names=("CP864",),
    ),
    CodepageSpec(
        key="cp865",
        label="CP865 — Nordic",
        python_codec="cp865",
        capability_names=("CP865",),
    ),
    CodepageSpec(
        key="cp869",
        label="CP869 — Greek",
        python_codec="cp869",
        capability_names=("CP869",),
    ),
    CodepageSpec(
        key="cp720",
        label="CP720 — Arabic DOS",
        python_codec="cp720",
        capability_names=("CP720",),
    ),
    CodepageSpec(
        key="cp737",
        label="CP737 — Greek DOS",
        python_codec="cp737",
        capability_names=("CP737",),
    ),
    CodepageSpec(
        key="cp775",
        label="CP775 — Baltic",
        python_codec="cp775",
        capability_names=("CP775",),
    ),
    CodepageSpec(
        key="cp1125",
        label="CP1125 — Ukrainian / Cyrillic",
        python_codec="cp1125",
        capability_names=("CP1125",),
    ),
    CodepageSpec(
        key="cp1250",
        label="CP1250 — Windows Central Europe",
        python_codec="cp1250",
        capability_names=("CP1250",),
    ),
    CodepageSpec(
        key="cp1252",
        label="CP1252 — Windows Latin-1",
        python_codec="cp1252",
        capability_names=("CP1252",),
    ),
    CodepageSpec(
        key="cp1253",
        label="CP1253 — Windows Greek",
        python_codec="cp1253",
        capability_names=("CP1253",),
    ),
    CodepageSpec(
        key="cp1254",
        label="CP1254 — Windows Turkish",
        python_codec="cp1254",
        capability_names=("CP1254",),
    ),
    CodepageSpec(
        key="cp1255",
        label="CP1255 — Windows Hebrew",
        python_codec="cp1255",
        capability_names=("CP1255",),
    ),
    CodepageSpec(
        key="cp1256",
        label="CP1256 — Windows Arabic",
        python_codec="cp1256",
        capability_names=("CP1256",),
    ),
    CodepageSpec(
        key="cp1257",
        label="CP1257 — Windows Baltic",
        python_codec="cp1257",
        capability_names=("CP1257",),
    ),
    CodepageSpec(
        key="cp1258",
        label="CP1258 — Windows Vietnamese",
        python_codec="cp1258",
        capability_names=("CP1258",),
    ),
    CodepageSpec(
        key="koi8-r",
        label="KOI8-R — старая русская кодировка",
        python_codec="koi8_r",
        capability_names=(),
        aliases=("koi8r",),
    ),
    CodepageSpec(
        key="mac-cyrillic",
        label="Mac Cyrillic — редкие старые LPT",
        python_codec="mac_cyrillic",
        capability_names=(),
        aliases=("maccyrillic", "mac_cyr"),
    ),
    CodepageSpec(
        key="iso8859-1",
        label="ISO-8859-1 — Latin-1",
        python_codec="latin_1",
        capability_names=("ISO_8859-1",),
        aliases=("latin1", "latin-1"),
    ),
    CodepageSpec(
        key="iso8859-2",
        label="ISO-8859-2 — Central Europe",
        python_codec="iso8859_2",
        capability_names=("ISO_8859-2",),
    ),
    CodepageSpec(
        key="iso8859-15",
        label="ISO-8859-15 — Latin-9",
        python_codec="iso8859_15",
        capability_names=("ISO_8859-15",),
        aliases=("latin9", "latin-9"),
    ),
    CodepageSpec(
        key="utf-8",
        label="UTF-8 input -> байты CP1251 для совместимости",
        python_codec="cp1251",
        capability_names=("CP1251",),
        aliases=("utf8",),
    ),
)

_CODEPAGE_BY_KEY = {spec.key: spec for spec in CODEPAGE_SPECS}
_CODEPAGE_ALIAS_TO_KEY = {
    alias.lower().replace("_", "").replace(" ", ""): spec.key
    for spec in CODEPAGE_SPECS
    for alias in (spec.key, *spec.aliases)
}


def get_lpt_driver_options() -> list[tuple[str, str]]:
    return [
        (LPT_DRIVER_ESCPOS, "ESC/POS raw — инициализация, жирный, отрезчик"),
        (LPT_DRIVER_TEXT, "Plain text raw — без ESC-команд, максимум совместимости"),
    ]


def get_lpt_line_ending_options() -> list[tuple[str, str]]:
    return [
        (LPT_LINE_ENDING_LF, "LF (\\n) — чаще для термопринтеров"),
        (LPT_LINE_ENDING_CRLF, "CRLF (\\r\\n) — чаще для матричных и text-only LPT"),
    ]


def get_codepage_options() -> list[tuple[str, str]]:
    return [(spec.key, spec.label) for spec in CODEPAGE_SPECS]


def get_profile_options() -> list[tuple[str, str]]:
    return [
        ("default", "Универсальный ESC/POS"),
        ("simple", "Совместимый / упрощённый ESC/POS"),
        ("TEP-200M", "Cashino EP-200 / TEP-200M"),
    ]


def _normalize_key(raw: str | None) -> str:
    s = (raw or "").strip().lower().replace("_", "").replace(" ", "")
    if not s:
        return "cp866"
    return _CODEPAGE_ALIAS_TO_KEY.get(s, s)


def _capabilities_paths() -> list[Path]:
    local = Path(__file__).resolve().parent
    paths = [
        local / "bundle_escpos" / "capabilities.json",
        local / "escpos" / "capabilities.json",
    ]
    env_path = os.environ.get("ESCPOS_CAPABILITIES_FILE", "").strip()
    if env_path:
        paths.insert(0, Path(env_path))
    return paths


@lru_cache(maxsize=1)
def _load_capabilities() -> dict[str, Any]:
    for path in _capabilities_paths():
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except OSError:
            continue
        except json.JSONDecodeError as ex:
            logger.warning("capabilities.json parse failed: %s", ex)
    return {}


def _profile_codepages(profile_name: str | None) -> dict[int, str]:
    prof = (profile_name or "default").strip() or "default"
    data = _load_capabilities()
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, dict):
        return {}
    profile = profiles.get(prof) or profiles.get("default")
    if not isinstance(profile, dict):
        return {}
    raw = profile.get("codePages")
    if not isinstance(raw, dict):
        return {}
    out: dict[int, str] = {}
    for k, v in raw.items():
        try:
            idx = int(str(k).strip(), 0)
        except ValueError:
            continue
        if isinstance(v, str) and v.strip():
            out[idx] = v.strip()
    return out


def _spec_for_key(raw: str | None) -> CodepageSpec:
    key = _normalize_key(raw)
    spec = _CODEPAGE_BY_KEY.get(key)
    if spec:
        return spec
    try:
        codecs.lookup(key)
    except LookupError as ex:
        raise ValueError(f"Неизвестная кодировка LPT: {raw}") from ex
    return CodepageSpec(
        key=key,
        label=key,
        python_codec=key,
        capability_names=(key.upper(),),
    )


def _manual_table(table_raw: Any) -> int | None:
    if table_raw is None:
        return None
    s = str(table_raw).strip()
    if not s:
        return None
    try:
        val = int(s, 0)
    except ValueError as ex:
        raise ValueError("ESC t должен быть числом 0..255") from ex
    if not (0 <= val <= 255):
        raise ValueError("ESC t должен быть в диапазоне 0..255")
    return val


def _candidate_capability_names(spec: CodepageSpec) -> tuple[str, ...]:
    names = []
    seen: set[str] = set()
    for name in spec.capability_names:
        up = name.upper()
        if up not in seen:
            seen.add(up)
            names.append(up)
    if spec.key in ("cp1251", "wpc1251"):
        for extra in ("CP1251",):
            if extra not in seen:
                seen.add(extra)
                names.append(extra)
    return tuple(names)


def resolve_codepage_plan(
    *,
    text_encoding: str,
    escpos_profile: str | None,
    escpos_table_byte: Any = None,
) -> CodepagePlan:
    spec = _spec_for_key(text_encoding)
    manual = _manual_table(escpos_table_byte)
    if manual is not None:
        return CodepagePlan(
            requested_key=spec.key,
            python_codec=spec.python_codec,
            escpos_table=manual,
            escpos_table_source="manual",
            capability_name=None,
        )

    candidates = _candidate_capability_names(spec)
    for profile_name, source in (
        ((escpos_profile or "default").strip() or "default", "profile"),
        ("default", "default-profile"),
    ):
        codepages = _profile_codepages(profile_name)
        if not codepages:
            continue
        for name in candidates:
            matches = [idx for idx, cp_name in codepages.items() if cp_name.upper() == name]
            if matches:
                preferred = min(matches)
                if spec.key in ("cp1251", "wpc1251") and 46 in matches:
                    preferred = 46
                elif spec.key == "cp866" and 17 in matches:
                    preferred = 17
                return CodepagePlan(
                    requested_key=spec.key,
                    python_codec=spec.python_codec,
                    escpos_table=preferred,
                    escpos_table_source=source,
                    capability_name=name,
                )

    return CodepagePlan(
        requested_key=spec.key,
        python_codec=spec.python_codec,
        escpos_table=None,
        escpos_table_source="none",
        capability_name=None,
    )


def describe_codepage_plan(
    *,
    text_encoding: str,
    escpos_profile: str | None,
    escpos_table_byte: Any = None,
) -> str:
    plan = resolve_codepage_plan(
        text_encoding=text_encoding,
        escpos_profile=escpos_profile,
        escpos_table_byte=escpos_table_byte,
    )
    table = "без ESC t" if plan.escpos_table is None else f"ESC t {plan.escpos_table}"
    source_map = {
        "manual": "вручную",
        "profile": "по профилю",
        "default-profile": "по профилю default",
        "none": "не найдено",
    }
    source = source_map.get(plan.escpos_table_source, plan.escpos_table_source)
    return f"{plan.requested_key} -> {plan.python_codec}; {table}; {source}"


def _env_no_esc_pct() -> bool:
    return os.environ.get("DESKTOP_MARKET_RECEIPT_NO_ESC_PCT", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _env_disable_cut() -> bool:
    return os.environ.get("DESKTOP_MARKET_RECEIPT_NO_CUT", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _line_break(kind: str) -> bytes:
    return b"\r\n" if (kind or LPT_LINE_ENDING_LF).strip().lower() == LPT_LINE_ENDING_CRLF else b"\n"


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


def _pad_center(text: str, width: int = RECEIPT_WIDTH) -> str:
    t = (text or "").strip()
    if not t:
        return " " * width
    if len(t) >= width:
        return t[:width]
    pad = width - len(t)
    left = pad // 2
    return (" " * left + t + " " * (pad - left))[:width]


def _pad_right(text: str, width: int = RECEIPT_WIDTH) -> str:
    t = (text or "").strip()
    if not t:
        return " " * width
    if len(t) >= width:
        return t[-width:]
    return (" " * (width - len(t))) + t


def _dashed_rule(width: int = RECEIPT_WIDTH) -> str:
    s = ("- " * ((width // 2) + 2))[:width]
    return s if s else "-" * width


def _receipt_safe_chars(text: str) -> str:
    if not text:
        return text
    return text.replace("\u2116", "N").replace("№", "N")


def _receipt_upper(text: str) -> str:
    if not text:
        return text
    return str(text).upper()


def _normalize_line_text(text: str) -> str:
    return _receipt_upper(_receipt_safe_chars(str(text)))


def _encode_line(text: str, codec: str, line_ending: str) -> bytes:
    normalized = _normalize_line_text(text)
    return normalized.encode(codec, errors="replace") + _line_break(line_ending)


def _row_payloads(rows: list[tuple[Any, ...]]) -> list[tuple[str, bool]]:
    payloads: list[tuple[str, bool]] = []
    for row in rows:
        if not row:
            continue
        kind = row[0]
        if kind == "blank":
            payloads.append(("", False))
            continue
        if kind == "sep":
            payloads.append((_dashed_rule(), False))
            continue
        if kind == "sep_solid":
            payloads.append(("-" * RECEIPT_WIDTH, False))
            continue
        if kind == "center_bold":
            for line in _wrap_to_width(str(row[1]), RECEIPT_WIDTH):
                payloads.append((_pad_center(line), True))
            continue
        if kind == "center":
            for line in _wrap_to_width(str(row[1]), RECEIPT_WIDTH):
                payloads.append((_pad_center(line), False))
            continue
        if kind == "left":
            lines = _wrap_to_width(str(row[1]), RECEIPT_WIDTH)
            payloads.extend((line, False) for line in lines)
            if not lines:
                payloads.append(("", False))
            continue
        if kind == "right":
            payloads.append((_pad_right(str(row[1])), False))
            continue
        if kind == "lr":
            payloads.append((_pad_left_right(str(row[1]), str(row[2])), False))
            continue
        if kind == "lr_bold":
            payloads.append((_pad_left_right(str(row[1]), str(row[2])), True))
            continue
        payloads.append((str(row[-1]), False))
    return payloads


def _escpos_prefix(*, table: int | None, esc_r_byte: Any = None) -> bytes:
    out = bytearray()
    # Жёстко выходим из китайского/Kanji-режима, если прошивка в него провалилась.
    out.extend(b"\x1c\x2e")
    if not _env_no_esc_pct():
        out.extend(b"\x1b\x25\x00")
    if esc_r_byte is not None and str(esc_r_byte).strip():
        try:
            er = int(str(esc_r_byte).strip(), 0)
        except ValueError:
            er = None
        if er is not None and 0 <= er <= 255:
            out.extend(b"\x1b\x52" + bytes([er & 0xFF]))
    if table is not None:
        out.extend(b"\x1b\x74" + bytes([table & 0xFF]))
    return bytes(out)


def _build_escpos_document(
    *,
    rows: list[tuple[Any, ...]],
    line_ending: str,
    plan: CodepagePlan,
    esc_r_byte: Any = None,
) -> bytes:
    out = bytearray()
    out.extend(b"\x1b\x40")
    out.extend(b"\x1b\x61\x00")
    out.extend(b"\x1b\x4d\x00")
    prefix = _escpos_prefix(table=plan.escpos_table, esc_r_byte=esc_r_byte)

    for text, bold in _row_payloads(rows):
        out.extend(prefix)
        out.extend(b"\x1b\x45" + bytes([1 if bold else 0]))
        out.extend(_encode_line(text, plan.python_codec, line_ending))

    out.extend(prefix)
    out.extend(b"\x1b\x45\x00")
    out.extend(_line_break(line_ending) * 4)
    if not _env_disable_cut():
        out.extend(b"\x1d\x56\x00")
    return bytes(out)


def _build_escpos_probe_document(
    *,
    sections: list[tuple[str, CodepagePlan]],
    line_ending: str,
    esc_r_byte: Any = None,
) -> bytes:
    out = bytearray()
    out.extend(b"\x1b\x40")
    out.extend(b"\x1b\x61\x00")
    out.extend(b"\x1b\x4d\x00")

    for idx, (title, plan) in enumerate(sections, start=1):
        prefix = _escpos_prefix(table=plan.escpos_table, esc_r_byte=esc_r_byte)
        header_rows = [
            (f"ПРОФИЛЬ {idx}", True),
            (title, False),
            ("АБВГДЕЖЗИЙКЛМНОП", False),
            ("РСТУФХЦЧШЩЪЫЬЭЮЯ", False),
            ("абвгдежзийклмноп", False),
            ("рстуфхцчшщъыьэюя", False),
            ("ТЕСТ: ТОВАРНЫЙ ЧЕК ИТОГ", True),
            ("-" * RECEIPT_WIDTH, False),
        ]
        for text, bold in header_rows:
            out.extend(prefix)
            out.extend(b"\x1b\x45" + bytes([1 if bold else 0]))
            out.extend(_encode_line(text, plan.python_codec, line_ending))

    out.extend(_line_break(line_ending) * 4)
    if not _env_disable_cut():
        out.extend(b"\x1d\x56\x00")
    return bytes(out)


def _build_plain_text_document(
    *,
    rows: list[tuple[Any, ...]],
    line_ending: str,
    codec: str,
) -> bytes:
    out = bytearray()
    for text, _bold in _row_payloads(rows):
        out.extend(_encode_line(text, codec, line_ending))
    out.extend(_line_break(line_ending) * 4)
    return bytes(out)


def _build_plain_text_probe_document(
    *,
    sections: list[tuple[str, str]],
    line_ending: str,
) -> bytes:
    out = bytearray()
    for title, codec in sections:
        lines = [
            f"TEXT RAW / {title}",
            "АБВГДЕЖЗИЙКЛМНОП",
            "РСТУФХЦЧШЩЪЫЬЭЮЯ",
            "абвгдежзийклмноп",
            "рстуфхцчшщъыьэюя",
            "ТЕСТ: ТОВАРНЫЙ ЧЕК ИТОГ",
            "-" * RECEIPT_WIDTH,
        ]
        for line in lines:
            out.extend(_encode_line(line, codec, line_ending))
    out.extend(_line_break(line_ending) * 4)
    return bytes(out)


def build_lpt_document(
    *,
    rows: list[tuple[Any, ...]],
    lpt_driver: str,
    line_ending: str,
    text_encoding: str,
    escpos_profile: str | None,
    escpos_table_byte: Any = None,
    esc_r_byte: Any = None,
) -> bytes:
    driver = (lpt_driver or LPT_DRIVER_ESCPOS).strip().lower()
    plan = resolve_codepage_plan(
        text_encoding=text_encoding,
        escpos_profile=escpos_profile,
        escpos_table_byte=escpos_table_byte,
    )
    if driver == LPT_DRIVER_TEXT:
        return _build_plain_text_document(
            rows=rows,
            line_ending=line_ending,
            codec=plan.python_codec,
        )
    return _build_escpos_document(
        rows=rows,
        line_ending=line_ending,
        plan=plan,
        esc_r_byte=esc_r_byte,
    )


def print_rows_via_lpt(
    *,
    devfile: str,
    rows: list[tuple[Any, ...]],
    lpt_driver: str,
    line_ending: str,
    text_encoding: str,
    escpos_profile: str | None,
    escpos_table_byte: Any = None,
    esc_r_byte: Any = None,
) -> CodepagePlan:
    from lpt_windows import write_lpt_bytes

    path = (devfile or "").strip() or "LPT1"
    plan = resolve_codepage_plan(
        text_encoding=text_encoding,
        escpos_profile=escpos_profile,
        escpos_table_byte=escpos_table_byte,
    )
    blob = build_lpt_document(
        rows=rows,
        lpt_driver=lpt_driver,
        line_ending=line_ending,
        text_encoding=text_encoding,
        escpos_profile=escpos_profile,
        escpos_table_byte=plan.escpos_table,
        esc_r_byte=esc_r_byte,
    )
    if path.upper().startswith("USB") and (lpt_driver or "").strip().lower() == LPT_DRIVER_TEXT and not blob.endswith(b"\f"):
        blob = blob + b"\f"
    write_lpt_bytes(path, blob, append_lf_if_missing=False)
    return plan


def print_probe_via_lpt(
    *,
    devfile: str,
    sections: list[tuple[str, str, str | None, Any]],
    line_ending: str,
    esc_r_byte: Any = None,
) -> list[CodepagePlan]:
    from lpt_windows import write_lpt_bytes

    path = (devfile or "").strip() or "LPT1"
    plans: list[CodepagePlan] = []
    normalized_sections: list[tuple[str, CodepagePlan]] = []
    for title, text_encoding, escpos_profile, escpos_table_byte in sections:
        plan = resolve_codepage_plan(
            text_encoding=text_encoding,
            escpos_profile=escpos_profile,
            escpos_table_byte=escpos_table_byte,
        )
        plans.append(plan)
        normalized_sections.append((title, plan))
    blob = _build_escpos_probe_document(
        sections=normalized_sections,
        line_ending=line_ending,
        esc_r_byte=esc_r_byte,
    )
    write_lpt_bytes(path, blob, append_lf_if_missing=False)
    return plans


def print_text_probe_via_lpt(
    *,
    devfile: str,
    sections: list[tuple[str, str, str]],
) -> list[CodepagePlan]:
    from lpt_windows import write_lpt_bytes

    path = (devfile or "").strip() or "LPT1"
    plans: list[CodepagePlan] = []
    documents: list[bytes] = []
    for title, text_encoding, line_ending in sections:
        plan = resolve_codepage_plan(
            text_encoding=text_encoding,
            escpos_profile=None,
            escpos_table_byte=None,
        )
        plans.append(plan)
        documents.append(
            _build_plain_text_probe_document(
                sections=[(title, plan.python_codec)],
                line_ending=line_ending,
            )
        )
    blob = b"".join(documents)
    if path.upper().startswith("USB") and not blob.endswith(b"\f"):
        blob = blob + b"\f"
    write_lpt_bytes(path, blob, append_lf_if_missing=False)
    return plans
