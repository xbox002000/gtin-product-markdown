"""Normalize GTIN / barcode lists from input, datasets and key-value records."""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from barcode_api import normalize_gtin

BARCODE_FIELDS = ("gtin", "upc", "ean", "barcode", "code", "gtin14", "ean13", "upc12")


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in re.split(r"[\r\n]+", text) if ln.strip()]


def barcode_from_item(item: dict, barcode_field: str | None = None) -> str | None:
    fields = (barcode_field,) + BARCODE_FIELDS if barcode_field else BARCODE_FIELDS
    for f in fields:
        if not f:
            continue
        v = item.get(f)
        g = normalize_gtin(v)
        if g:
            return g
    return None


def normalize_gtins(values: Iterable[Any] | str | None, barcode_field: str | None = None) -> tuple[list[str], list[str]]:
    """Return (unique valid GTINs, invalid raw strings)."""
    if values is None:
        return [], []
    if isinstance(values, str):
        values = _lines(values)
    out: list[str] = []
    bad: list[str] = []
    seen: set[str] = set()
    for v in values:
        if isinstance(v, dict):
            g = barcode_from_item(v, barcode_field)
            raw = json.dumps(v)[:80]
        else:
            g = normalize_gtin(v)
            raw = str(v).strip()[:80] if v is not None else ""
        if g:
            if g not in seen:
                seen.add(g)
                out.append(g)
        elif raw:
            bad.append(raw)
    return out, bad


def split_payload(payload: Any, barcode_field: str | None = None) -> tuple[list[str], list[str]]:
    """KV record → (gtins, invalid). Accepts list, newline text, or {gtins/barcodes:[...]}."""
    if payload is None:
        return [], []
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    if isinstance(payload, str):
        s = payload.strip()
        if s.startswith(("[", "{")):
            try:
                return split_payload(json.loads(s), barcode_field)
            except ValueError:
                pass
        return normalize_gtins(_lines(s), barcode_field)
    if isinstance(payload, dict):
        a = payload.get("gtins") or payload.get("barcodes") or []
        if not a:
            one = barcode_from_item(payload, barcode_field)
            return ([one] if one else []), []
        return normalize_gtins(a, barcode_field)
    if isinstance(payload, list):
        return normalize_gtins(payload, barcode_field)
    return [], []
