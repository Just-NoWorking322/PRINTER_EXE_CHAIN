"""URL превью товара из типичных полей API (общая логика для UI и SQLite-кэша)."""

from __future__ import annotations

from typing import Any


def _to_absolute_url(u: str, base: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http://") or u.startswith("https://"):
        return u
    if u.startswith("/") and base:
        return f"{base}{u}"
    return u


def product_image_url(p: Any, base_url: str | None = None) -> str | None:
    if not isinstance(p, dict):
        return None
    base = (base_url or "").rstrip("/")

    def _abs(u: str) -> str:
        return _to_absolute_url(u, base)

    for k in (
        "primary_image_url",
        "image_url",
        "thumbnail_url",
        "photo_url",
        "picture_url",
        "main_image_url",
        "cover_url",
        "preview_url",
        "image_path",
        "media_url",
    ):
        v = p.get(k)
        if isinstance(v, str):
            s = _abs(v)
            if s:
                return s
    for k in ("image", "photo", "thumbnail", "picture", "cover", "img"):
        v = p.get(k)
        if isinstance(v, str):
            s = _abs(v)
            if s:
                return s
        if isinstance(v, dict):
            for kk in (
                "url",
                "src",
                "image",
                "file",
                "path",
                "thumbnail",
                "full",
                "download_url",
                "href",
            ):
                raw = v.get(kk)
                if isinstance(raw, str):
                    s = _abs(raw)
                    if s:
                        return s
    for nest_key in ("primary_image", "main_image", "cover_image", "image_data", "photo_data"):
        nested = p.get(nest_key)
        if isinstance(nested, dict):
            for kk in (
                "url",
                "src",
                "image",
                "file",
                "path",
                "thumbnail",
                "full",
                "download_url",
            ):
                raw = nested.get(kk)
                if isinstance(raw, str):
                    s = _abs(raw)
                    if s:
                        return s
    for key in ("images", "photos", "gallery", "media", "attachments"):
        imgs = p.get(key)
        if not isinstance(imgs, list):
            continue
        for it in imgs[:8]:
            if isinstance(it, str):
                s = _abs(it)
                if s:
                    return s
            if isinstance(it, dict):
                for kk in (
                    "url",
                    "src",
                    "image",
                    "file",
                    "path",
                    "thumbnail",
                    "full",
                    "full_url",
                    "image_url",
                    "file_url",
                    "public_url",
                    "download_url",
                ):
                    raw = it.get(kk)
                    if isinstance(raw, str):
                        s = _abs(raw)
                        if s:
                            return s
                    if isinstance(raw, dict):
                        for sk in ("url", "src", "path"):
                            rv = raw.get(sk)
                            if isinstance(rv, str):
                                s = _abs(rv)
                                if s:
                                    return s
    nested = p.get("product")
    if isinstance(nested, dict) and nested is not p:
        inner = product_image_url(nested, base_url)
        if inner:
            return inner
    return None


def product_image_candidates(p: Any, base_url: str | None = None) -> list[str]:
    """
    Список URL для попытки загрузки превью (в т.ч. эвристики по id), без дубликатов.
    """
    base = (base_url or "").rstrip("/")
    seen: set[str] = set()
    out: list[str] = []

    def add(raw: str) -> None:
        s = _to_absolute_url(raw, base)
        if not s or s in seen:
            return
        seen.add(s)
        out.append(s)

    first = product_image_url(p, base_url)
    if first:
        add(first)

    for nest_key in ("primary_image", "main_image", "cover_image", "thumbnail", "image_obj"):
        nested = p.get(nest_key)
        if not isinstance(nested, dict):
            continue
        for kk in (
            "url",
            "src",
            "image",
            "file",
            "path",
            "thumbnail",
            "full",
            "download_url",
            "href",
        ):
            raw = nested.get(kk)
            if isinstance(raw, str):
                add(raw)

    pid = p.get("id")
    pid_s = str(pid).strip() if pid is not None else ""
    comp = p.get("company")
    comp_s = str(comp).strip() if comp is not None else ""

    imgs = p.get("images")
    if isinstance(imgs, list) and base:
        for it in imgs[:8]:
            if not isinstance(it, dict):
                continue
            for kk in (
                "url",
                "src",
                "image",
                "file",
                "path",
                "thumbnail",
                "full",
                "full_url",
                "image_url",
                "file_url",
                "public_url",
                "download_url",
            ):
                raw = it.get(kk)
                if isinstance(raw, str):
                    add(raw)
            img_id = it.get("id") or it.get("uuid") or it.get("file_id") or it.get("image_id")
            if img_id is not None and base:
                iid = str(img_id).strip()
                if iid:
                    add(f"{base}/api/main/images/{iid}/")
                    add(f"{base}/api/main/files/{iid}/")
                    add(f"{base}/api/main/product-images/{iid}/")
                    add(f"{base}/api/main/media/{iid}/")

    if base and pid_s:
        for path in (
            f"{base}/api/main/products/{pid_s}/image/",
            f"{base}/api/main/products/{pid_s}/images/",
            f"{base}/api/main/products/{pid_s}/thumbnail/",
            f"{base}/api/main/products/{pid_s}/preview/",
            f"{base}/api/main/market/products/{pid_s}/image/",
        ):
            add(path)
        if comp_s:
            add(f"{base}/api/main/company/{comp_s}/products/{pid_s}/image/")
            add(f"{base}/api/main/company/{comp_s}/products/{pid_s}/images/")
    return out[:16]
