"""Official barcode lookup clients: UPCitemdb, Barcode Lookup, Open Food Facts.

No Idealo / Amazon HTML scraping. Buyer keys go in headers (UPCitemdb user_key) or query
(Barcode Lookup `key`); httpx INFO logging is muted by the caller, URLs are never logged,
and every error string passes through `redact()` before it can reach a dataset row, report or log.
ASIN is only surfaced when a barcode API already returns it — never crawled.
"""
from __future__ import annotations

import asyncio
import os
import random
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx

UA = "apify-gtin-product-markdown/0.1 (+https://apify.com; contact: actor-support@example.invalid)"
OFF_UA = "ApifyActor-gtin-product-markdown/0.1 (https://apify.com; contact: actor-support@example.invalid)"

PROVIDERS = ("auto", "upcitemdb", "barcodelookup")
SOURCES = ("upcitemdb", "barcodelookup", "openfoodfacts", "none")

UPCITEMDB_V1 = "https://api.upcitemdb.com/prod/v1/lookup"
UPCITEMDB_TRIAL = "https://api.upcitemdb.com/prod/trial/lookup"
BARCODELOOKUP = "https://api.barcodelookup.com/v3/products"
OPENFOODFACTS = "https://world.openfoodfacts.org/api/v2/product/{code}.json"

KEY_HINTS = {
    "upcitemdb": (
        "UPCitemdb paid DEV/PRO key: https://www.upcitemdb.com/api → sign up → copy user_key. "
        "Paste into `apiKey` (sent as header `user_key` with `key_type: 3scale` to /prod/v1/lookup)."
    ),
    "barcodelookup": (
        "Barcode Lookup key: https://www.barcodelookup.com/api → create account → dashboard shows your key. "
        "Paste into `apiKey` (sent as query param `key` to /v3/products)."
    ),
    "auto": (
        "Provide an UPCitemdb or Barcode Lookup API key in `apiKey`, or rely on Open Food Facts "
        "(food/FMCG only) when fallbackOpenFoodFacts is enabled."
    ),
}


def _test_base() -> str | None:
    """Test hook: route all provider calls to a local mock. Only localhost is accepted."""
    base = (os.getenv("GTIN_TEST_BASE") or "").strip().rstrip("/")
    if not base:
        return None
    host = urlparse(base).hostname
    if urlparse(base).scheme == "http" and host in ("127.0.0.1", "localhost"):
        return base
    return None


def _endpoint(url: str) -> str:
    base = _test_base()
    if not base:
        return url
    p = urlparse(url)
    return base + p.path


def redact(text: Any, *secrets: str | None) -> str:
    s = "" if text is None else str(text)
    for sec in secrets:
        if sec and len(sec) >= 4:
            s = s.replace(sec, "***")
            s = s.replace(quote(sec, safe=""), "***")
    s = re.sub(r"(?i)\b(key|user_key|apiKey|apikey|access_token)=[^&\s\"']+", r"\1=***", s)
    return s


# ---------------------------------------------------------------------------------------------- GTIN normalize

_DIGIT_RE = re.compile(r"\D+")


def normalize_gtin(raw: Any) -> str | None:
    """Strip spaces/dashes/other non-digits; accept 8–14 digit barcodes (EAN-8 … GTIN-14)."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        s = str(int(raw))
    else:
        s = _DIGIT_RE.sub("", str(raw).strip())
    if not s or not s.isdigit():
        return None
    if len(s) < 8 or len(s) > 14:
        return None
    return s


# ---------------------------------------------------------------------------------------------- results

@dataclass
class LookupResult:
    ok: bool
    item: dict[str, Any]
    attempts: int = 0
    auth_failed: bool = False
    quota_failed: bool = False


@dataclass
class _Attempt:
    ok: bool = False
    retryable: bool = False
    item: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    retry_after: float | None = None
    auth_failed: bool = False
    quota_failed: bool = False


def _base_row(gtin: str, source: str) -> dict[str, Any]:
    return {
        "gtin": gtin,
        "source": source,
        "status": "failed",
        "title": None,
        "brand": None,
        "size": None,
        "category": None,
        "images": [],
        "description": None,
        "ingredients": None,
        "nutrition": None,
        "asin": None,
        "url": None,
        "markdown": None,
    }


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers.get("Retry-After"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _http_error(source: str, resp: httpx.Response, *secrets: str | None) -> _Attempt:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {}
    msg = ""
    if isinstance(body, dict):
        msg = str(
            body.get("message")
            or body.get("error_description")
            or body.get("title")
            or body.get("error_message")
            or body.get("error")
            or body.get("msg")
            or ""
        )
    msg = redact(msg, *secrets)[:300] or f"HTTP {resp.status_code}"
    code = resp.status_code
    err: dict[str, Any] = {"httpStatus": code, "errorMessage": msg, "source": source}
    if code in (401, 403):
        err["errorClass"] = "unauthorized"
        err["hint"] = KEY_HINTS.get(source, KEY_HINTS["auto"])
        return _Attempt(error=err, auth_failed=True)
    if code == 429:
        err["errorClass"] = "rate_limited"
        err["hint"] = "Provider rate limit; lower maxConcurrency or check your plan's quota."
        return _Attempt(error=err, retryable=True, retry_after=_retry_after(resp), quota_failed=False)
    if code == 404:
        err["errorClass"] = "not_found"
        err["hint"] = "Barcode not in this provider's database."
        return _Attempt(error=err)
    if code in (400, 422):
        err["errorClass"] = "invalid_request"
        err["hint"] = "The provider rejected this barcode (malformed or unsupported)."
        return _Attempt(error=err)
    if code >= 500:
        err["errorClass"] = "server_error"
        return _Attempt(error=err, retryable=True)
    err["errorClass"] = "http_error"
    return _Attempt(error=err)


# ---------------------------------------------------------------------------------------------- card helpers

def product_markdown(item: dict[str, Any]) -> str:
    """One Markdown product card."""
    title = item.get("title") or f"Product {item.get('gtin')}"
    lines = [f"# {title}", ""]
    meta = []
    if item.get("brand"):
        meta.append(f"**Brand:** {item['brand']}")
    if item.get("gtin"):
        meta.append(f"**GTIN:** `{item['gtin']}`")
    if item.get("size"):
        meta.append(f"**Size / quantity:** {item['size']}")
    if item.get("category"):
        meta.append(f"**Category:** {item['category']}")
    if item.get("asin"):
        meta.append(f"**ASIN:** `{item['asin']}`")
    if item.get("source"):
        meta.append(f"**Source:** `{item['source']}`")
    if meta:
        lines += meta + [""]
    imgs = item.get("images") or []
    if imgs:
        lines.append("## Images")
        lines.append("")
        for u in imgs[:8]:
            lines.append(f"- ![]({u})")
            lines.append(f"  {u}")
        lines.append("")
    if item.get("description"):
        lines += ["## Description", "", str(item["description"]).strip(), ""]
    if item.get("ingredients"):
        lines += ["## Ingredients", "", str(item["ingredients"]).strip(), ""]
    nutr = item.get("nutrition")
    if nutr:
        lines += ["## Nutrition", ""]
        if isinstance(nutr, dict):
            for k, v in nutr.items():
                if v is not None and v != "":
                    lines.append(f"- **{k}:** {v}")
        else:
            lines.append(str(nutr))
        lines.append("")
    if item.get("url"):
        lines += [f"**Product URL:** {item['url']}", ""]
    lines.append(f"*Attribution: data from `{item.get('source')}` — not scraped from Idealo or Amazon.*")
    return "\n".join(lines).rstrip() + "\n"


def rag_chunks(item: dict[str, Any], target_min: int = 500, target_max: int = 800) -> list[dict[str, Any]]:
    """Split markdown into ~500–800 char chunks, preferring heading boundaries."""
    md = item.get("markdown") or product_markdown(item)
    sections: list[tuple[str, str]] = []
    cur_h, cur_b = "Product", []
    for line in md.splitlines():
        if line.startswith("#"):
            if cur_b:
                sections.append((cur_h, "\n".join(cur_b).strip()))
            cur_h = line.lstrip("#").strip() or cur_h
            cur_b = [line]
        else:
            cur_b.append(line)
    if cur_b:
        sections.append((cur_h, "\n".join(cur_b).strip()))

    chunks: list[dict[str, Any]] = []
    buf_h, buf = "", ""
    idx = 0

    def flush():
        nonlocal idx, buf, buf_h
        text = buf.strip()
        if not text:
            return
        idx += 1
        chunks.append({
            "gtin": item.get("gtin"),
            "source": item.get("source"),
            "chunkIndex": idx,
            "heading": buf_h,
            "text": text,
            "charCount": len(text),
        })
        buf, buf_h = "", ""

    for h, body in sections:
        if not body:
            continue
        candidate = (buf + "\n\n" + body).strip() if buf else body
        if buf and len(candidate) > target_max:
            flush()
            candidate = body
        buf = candidate
        buf_h = buf_h or h
        if len(buf) >= target_min:
            # If still under max, keep accumulating; if over max, split hard
            if len(buf) <= target_max:
                flush()
            else:
                # hard-split oversized section
                text = buf
                while text:
                    piece = text[:target_max]
                    cut = piece.rfind("\n")
                    if cut >= target_min:
                        piece = text[:cut]
                    chunks.append({
                        "gtin": item.get("gtin"),
                        "source": item.get("source"),
                        "chunkIndex": len(chunks) + 1,
                        "heading": buf_h or h,
                        "text": piece.strip(),
                        "charCount": len(piece.strip()),
                    })
                    text = text[len(piece):].lstrip()
                buf, buf_h = "", ""
    flush()
    # renumber
    for i, c in enumerate(chunks, 1):
        c["chunkIndex"] = i
    return chunks


# ---------------------------------------------------------------------------------------------- parsers

def parse_upcitemdb_item(raw: dict, gtin: str) -> dict[str, Any]:
    images = [u for u in (raw.get("images") or []) if isinstance(u, str) and u.strip()]
    size = raw.get("size") or raw.get("dimension") or None
    if isinstance(size, str):
        size = size.strip() or None
    brand = (raw.get("brand") or "").strip() or None
    title = (raw.get("title") or "").strip() or None
    cat = raw.get("category")
    if isinstance(cat, list):
        cat = " > ".join(str(c) for c in cat if c)
    cat = (str(cat).strip() if cat else None) or None
    desc = (raw.get("description") or "").strip() or None
    asin = raw.get("asin") or raw.get("ASIN")
    if asin:
        asin = str(asin).strip() or None
    ean = raw.get("ean") or raw.get("upc") or gtin
    row = _base_row(gtin, "upcitemdb")
    row.update({
        "status": "ok",
        "title": title,
        "brand": brand,
        "size": size,
        "category": cat,
        "images": images[:20],
        "description": desc,
        "asin": asin,
        "url": f"https://www.upcitemdb.com/upc/{ean}" if ean else None,
        "model": (raw.get("model") or None),
        "color": (raw.get("color") or None),
    })
    row["markdown"] = product_markdown(row)
    return row


def parse_barcodelookup_item(raw: dict, gtin: str) -> dict[str, Any]:
    images = [u for u in (raw.get("images") or []) if isinstance(u, str) and u.strip()]
    size_parts = [raw.get(k) for k in ("size", "weight", "length", "width", "height") if raw.get(k)]
    size = ", ".join(str(p) for p in size_parts) if size_parts else None
    brand = (raw.get("brand") or raw.get("manufacturer") or "").strip() or None
    title = (raw.get("title") or "").strip() or None
    cat = raw.get("category")
    if isinstance(cat, list):
        cat = " > ".join(str(c) for c in cat if c)
    cat = (str(cat).strip() if cat else None) or None
    desc = (raw.get("description") or "").strip() or None
    asin = raw.get("asin")
    if asin:
        asin = str(asin).strip() or None
    ingredients = raw.get("ingredients")
    if isinstance(ingredients, list):
        ingredients = ", ".join(str(x) for x in ingredients)
    row = _base_row(gtin, "barcodelookup")
    row.update({
        "status": "ok",
        "title": title,
        "brand": brand,
        "size": size,
        "category": cat,
        "images": images[:20],
        "description": desc,
        "ingredients": (str(ingredients).strip() if ingredients else None),
        "asin": asin,
        "url": f"https://www.barcodelookup.com/{gtin}",
        "mpn": raw.get("mpn"),
        "model": raw.get("model"),
    })
    row["markdown"] = product_markdown(row)
    return row


def _off_nutrition(nutriments: dict | None, grade: str | None) -> dict[str, Any] | None:
    if not nutriments and not grade:
        return None
    n = nutriments or {}
    out: dict[str, Any] = {}
    if grade:
        out["nutritionGrade"] = grade
    mapping = [
        ("energy-kcal_100g", "energyKcal100g"),
        ("fat_100g", "fat100g"),
        ("saturated-fat_100g", "saturatedFat100g"),
        ("carbohydrates_100g", "carbohydrates100g"),
        ("sugars_100g", "sugars100g"),
        ("proteins_100g", "proteins100g"),
        ("salt_100g", "salt100g"),
        ("fiber_100g", "fiber100g"),
    ]
    for src, dst in mapping:
        if n.get(src) is not None:
            out[dst] = n[src]
    return out or None


def parse_openfoodfacts(data: dict, gtin: str) -> dict[str, Any] | None:
    if data.get("status") != 1:
        return None
    p = data.get("product") or {}
    images: list[str] = []
    for k in ("image_front_url", "image_url", "image_front_small_url"):
        u = p.get(k)
        if isinstance(u, str) and u.strip() and u not in images:
            images.append(u.strip())
    # selected images gallery
    sel = (p.get("selected_images") or {}).get("front") or {}
    for size_map in sel.values() if isinstance(sel, dict) else []:
        if isinstance(size_map, dict):
            for u in size_map.values():
                if isinstance(u, str) and u.strip() and u not in images:
                    images.append(u.strip())
    cats = p.get("categories") or p.get("categories_tags")
    if isinstance(cats, list):
        cats = ", ".join(str(c).removeprefix("en:") for c in cats[:8])
    elif isinstance(cats, str):
        cats = cats.strip() or None
    else:
        cats = None
    title = (p.get("product_name") or p.get("product_name_en") or p.get("generic_name") or "").strip() or None
    brand = (p.get("brands") or "").strip() or None
    size = (p.get("quantity") or p.get("product_quantity") or "")
    size = str(size).strip() or None
    ingredients = (p.get("ingredients_text") or p.get("ingredients_text_en") or "").strip() or None
    desc_parts = []
    if p.get("generic_name"):
        desc_parts.append(str(p["generic_name"]).strip())
    if p.get("packaging"):
        desc_parts.append(f"Packaging: {p['packaging']}")
    desc = " — ".join(desc_parts) if desc_parts else None
    code = p.get("code") or gtin
    row = _base_row(gtin, "openfoodfacts")
    row.update({
        "status": "ok",
        "title": title,
        "brand": brand,
        "size": size,
        "category": cats,
        "images": images[:20],
        "description": desc,
        "ingredients": ingredients,
        "nutrition": _off_nutrition(p.get("nutriments"), p.get("nutrition_grades") or p.get("nutrition_grade_fr")),
        "asin": None,
        "url": f"https://world.openfoodfacts.org/product/{code}",
    })
    row["markdown"] = product_markdown(row)
    return row


# ---------------------------------------------------------------------------------------------- HTTP lookups

async def _get_with_retries(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    max_retries: int = 3,
    timeout: float = 30.0,
    backoff_base: float = 0.5,
    secrets: tuple[str | None, ...] = (),
    source: str = "none",
) -> tuple[httpx.Response | None, _Attempt | None, int]:
    """Return (response, error_attempt, attempts). response is set on HTTP completion (any status)."""
    attempts = 0
    last_err: _Attempt | None = None
    for attempt in range(max_retries + 1):
        attempts = attempt + 1
        try:
            resp = await client.get(url, headers=headers, params=params, timeout=timeout)
        except httpx.TimeoutException as e:
            last_err = _Attempt(
                error={"errorClass": "timeout", "errorMessage": redact(str(e), *secrets)[:200], "source": source},
                retryable=True,
            )
            if attempt < max_retries:
                await asyncio.sleep(backoff_base * (2 ** attempt) + random.random() * 0.1)
                continue
            return None, last_err, attempts
        except httpx.HTTPError as e:
            last_err = _Attempt(
                error={"errorClass": "network", "errorMessage": redact(str(e), *secrets)[:200], "source": source},
                retryable=True,
            )
            if attempt < max_retries:
                await asyncio.sleep(backoff_base * (2 ** attempt) + random.random() * 0.1)
                continue
            return None, last_err, attempts

        if resp.status_code in (429,) or resp.status_code >= 500:
            err = _http_error(source, resp, *secrets)
            last_err = err
            if err.retryable and attempt < max_retries:
                delay = err.retry_after if err.retry_after is not None else backoff_base * (2 ** attempt)
                await asyncio.sleep(delay + random.random() * 0.1)
                continue
            return resp, err, attempts
        return resp, None, attempts
    return None, last_err, attempts


async def lookup_upcitemdb(
    client: httpx.AsyncClient,
    gtin: str,
    *,
    api_key: str | None,
    max_retries: int = 3,
    timeout: float = 30.0,
    backoff_base: float = 0.5,
) -> LookupResult:
    """Paid v1 when api_key present; otherwise trial endpoint (used by mocks / explicit no-key probes)."""
    secrets = (api_key,)
    if api_key:
        url = _endpoint(UPCITEMDB_V1)
        headers = {
            "user_key": api_key,
            "key_type": "3scale",
            "Accept": "application/json",
            "Accept-Encoding": "gzip,deflate",
        }
    else:
        url = _endpoint(UPCITEMDB_TRIAL)
        headers = {"Accept": "application/json", "Accept-Encoding": "gzip,deflate"}
    params = {"upc": gtin}
    resp, err, attempts = await _get_with_retries(
        client, url, headers=headers, params=params,
        max_retries=max_retries, timeout=timeout, backoff_base=backoff_base,
        secrets=secrets, source="upcitemdb",
    )
    if resp is None:
        item = _base_row(gtin, "upcitemdb")
        item.update(err.error if err else {"errorClass": "network", "errorMessage": "no response"})
        item["attempts"] = attempts
        return LookupResult(False, item, attempts=attempts,
                            auth_failed=bool(err and err.auth_failed),
                            quota_failed=bool(err and err.quota_failed))

    if resp.status_code != 200:
        a = err or _http_error("upcitemdb", resp, *secrets)
        item = _base_row(gtin, "upcitemdb")
        item.update(a.error or {})
        item["attempts"] = attempts
        return LookupResult(False, item, attempts=attempts, auth_failed=a.auth_failed, quota_failed=a.quota_failed)

    try:
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        item = _base_row(gtin, "upcitemdb")
        item.update({"errorClass": "invalid_response", "errorMessage": redact(str(e), *secrets)[:200], "attempts": attempts})
        return LookupResult(False, item, attempts=attempts)

    code = str(data.get("code") or "").upper()
    items = data.get("items") or []
    if code and code not in ("OK", "200", "") and not items:
        # EXCEED_LIMIT etc.
        cls = "rate_limited" if "LIMIT" in code or "EXCEED" in code else "provider_error"
        item = _base_row(gtin, "upcitemdb")
        item.update({
            "errorClass": cls,
            "providerStatus": code,
            "errorMessage": redact(data.get("message") or code, *secrets)[:300],
            "attempts": attempts,
            "hint": "UPCitemdb quota or plan limit; check https://www.upcitemdb.com/api",
        })
        return LookupResult(False, item, attempts=attempts, quota_failed=("LIMIT" in code))

    if not items:
        item = _base_row(gtin, "upcitemdb")
        item.update({
            "errorClass": "not_found",
            "providerStatus": code or "OK",
            "errorMessage": "No items returned for this GTIN.",
            "attempts": attempts,
            "hint": "Barcode not in UPCitemdb; try Open Food Facts fallback or another provider.",
        })
        return LookupResult(False, item, attempts=attempts)

    row = parse_upcitemdb_item(items[0], gtin)
    row["attempts"] = attempts
    row["providerStatus"] = code or "OK"
    return LookupResult(True, row, attempts=attempts)


async def lookup_barcodelookup(
    client: httpx.AsyncClient,
    gtin: str,
    *,
    api_key: str | None,
    max_retries: int = 3,
    timeout: float = 30.0,
    backoff_base: float = 0.5,
) -> LookupResult:
    if not api_key:
        item = _base_row(gtin, "barcodelookup")
        item.update({
            "errorClass": "missing_key",
            "errorMessage": "Barcode Lookup requires an API key.",
            "hint": KEY_HINTS["barcodelookup"],
            "attempts": 0,
        })
        return LookupResult(False, item, attempts=0)

    url = _endpoint(BARCODELOOKUP)
    params = {"barcode": gtin, "key": api_key}
    resp, err, attempts = await _get_with_retries(
        client, url, params=params,
        max_retries=max_retries, timeout=timeout, backoff_base=backoff_base,
        secrets=(api_key,), source="barcodelookup",
    )
    if resp is None:
        item = _base_row(gtin, "barcodelookup")
        item.update(err.error if err else {"errorClass": "network", "errorMessage": "no response"})
        item["attempts"] = attempts
        return LookupResult(False, item, attempts=attempts,
                            auth_failed=bool(err and err.auth_failed),
                            quota_failed=bool(err and err.quota_failed))

    if resp.status_code != 200:
        a = err or _http_error("barcodelookup", resp, api_key)
        item = _base_row(gtin, "barcodelookup")
        item.update(a.error or {})
        item["attempts"] = attempts
        return LookupResult(False, item, attempts=attempts, auth_failed=a.auth_failed, quota_failed=a.quota_failed)

    try:
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        item = _base_row(gtin, "barcodelookup")
        item.update({"errorClass": "invalid_response", "errorMessage": redact(str(e), api_key)[:200], "attempts": attempts})
        return LookupResult(False, item, attempts=attempts)

    products = data.get("products") or []
    if not products:
        item = _base_row(gtin, "barcodelookup")
        item.update({
            "errorClass": "not_found",
            "errorMessage": "No products returned for this barcode.",
            "attempts": attempts,
            "hint": "Barcode not in Barcode Lookup; try Open Food Facts fallback.",
        })
        return LookupResult(False, item, attempts=attempts)

    row = parse_barcodelookup_item(products[0], gtin)
    row["attempts"] = attempts
    return LookupResult(True, row, attempts=attempts)


async def lookup_openfoodfacts(
    client: httpx.AsyncClient,
    gtin: str,
    *,
    max_retries: int = 3,
    timeout: float = 30.0,
    backoff_base: float = 0.5,
) -> LookupResult:
    url = _endpoint(OPENFOODFACTS.format(code=gtin))
    headers = {"User-Agent": OFF_UA, "Accept": "application/json"}
    resp, err, attempts = await _get_with_retries(
        client, url, headers=headers,
        max_retries=max_retries, timeout=timeout, backoff_base=backoff_base,
        source="openfoodfacts",
    )
    if resp is None:
        item = _base_row(gtin, "openfoodfacts")
        item.update(err.error if err else {"errorClass": "network", "errorMessage": "no response"})
        item["attempts"] = attempts
        return LookupResult(False, item, attempts=attempts)

    if resp.status_code != 200:
        a = err or _http_error("openfoodfacts", resp)
        item = _base_row(gtin, "openfoodfacts")
        item.update(a.error or {})
        item["attempts"] = attempts
        return LookupResult(False, item, attempts=attempts, auth_failed=a.auth_failed)

    try:
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        item = _base_row(gtin, "openfoodfacts")
        item.update({"errorClass": "invalid_response", "errorMessage": str(e)[:200], "attempts": attempts})
        return LookupResult(False, item, attempts=attempts)

    row = parse_openfoodfacts(data, gtin)
    if not row:
        item = _base_row(gtin, "openfoodfacts")
        item.update({
            "errorClass": "not_found",
            "errorMessage": f"Open Food Facts status={data.get('status')} (product not found).",
            "attempts": attempts,
            "hint": "OFF covers food/FMCG primarily. Use UPCitemdb or Barcode Lookup with an API key for general products.",
        })
        return LookupResult(False, item, attempts=attempts)
    row["attempts"] = attempts
    return LookupResult(True, row, attempts=attempts)


async def lookup_one(
    client: httpx.AsyncClient,
    gtin: str,
    *,
    provider: str = "auto",
    api_key: str | None = None,
    upcitemdb_user_id: str | None = None,
    fallback_off: bool = True,
    max_retries: int = 3,
    timeout: float = 30.0,
    backoff_base: float = 0.5,
    skip_paid: bool = False,
) -> LookupResult:
    """Resolve one GTIN through the configured provider chain."""
    upc_key = (upcitemdb_user_id or api_key or None)
    tried: list[str] = []
    last: LookupResult | None = None
    auth_failed = False
    quota_failed = False

    async def try_upc() -> LookupResult:
        return await lookup_upcitemdb(
            client, gtin, api_key=upc_key, max_retries=max_retries, timeout=timeout, backoff_base=backoff_base,
        )

    async def try_bcl() -> LookupResult:
        return await lookup_barcodelookup(
            client, gtin, api_key=api_key, max_retries=max_retries, timeout=timeout, backoff_base=backoff_base,
        )

    async def try_off() -> LookupResult:
        return await lookup_openfoodfacts(
            client, gtin, max_retries=max_retries, timeout=timeout, backoff_base=backoff_base,
        )

    paid_order: list[str] = []
    if provider == "upcitemdb":
        paid_order = ["upcitemdb"]
    elif provider == "barcodelookup":
        paid_order = ["barcodelookup"]
    elif provider == "auto":
        if api_key or upcitemdb_user_id:
            paid_order = ["upcitemdb", "barcodelookup"]
        else:
            paid_order = []
    else:
        item = _base_row(gtin, "none")
        item.update({"errorClass": "invalid_input", "errorMessage": f"Unknown provider {provider!r}", "attempts": 0})
        return LookupResult(False, item)

    if not skip_paid:
        for name in paid_order:
            if name == "upcitemdb":
                if not upc_key and provider == "upcitemdb":
                    # explicit upcitemdb without key → missing_key (unless OFF fallback)
                    last = LookupResult(False, {
                        **_base_row(gtin, "upcitemdb"),
                        "errorClass": "missing_key",
                        "errorMessage": "UPCitemdb paid lookup needs apiKey (user_key).",
                        "hint": KEY_HINTS["upcitemdb"],
                        "attempts": 0,
                    })
                    tried.append("upcitemdb")
                    break
                if not upc_key:
                    continue
                last = await try_upc()
            else:
                if not api_key and provider == "barcodelookup":
                    last = LookupResult(False, {
                        **_base_row(gtin, "barcodelookup"),
                        "errorClass": "missing_key",
                        "errorMessage": "Barcode Lookup needs apiKey.",
                        "hint": KEY_HINTS["barcodelookup"],
                        "attempts": 0,
                    })
                    tried.append("barcodelookup")
                    break
                if not api_key:
                    continue
                last = await try_bcl()
            tried.append(name)
            auth_failed = auth_failed or last.auth_failed
            quota_failed = quota_failed or last.quota_failed
            if last.ok:
                last.item["triedSources"] = tried
                return last
            # On auth failure under auto, try the other paid provider once
            if last.auth_failed and provider == "auto":
                continue
            # not_found / other → break to OFF if enabled
            if last.item.get("errorClass") == "not_found":
                break
            if last.auth_failed and provider != "auto":
                break

    if fallback_off:
        off = await try_off()
        tried.append("openfoodfacts")
        if off.ok:
            off.item["triedSources"] = tried
            return off
        last = off if last is None else last
        # prefer OFF not_found as final if both missed
        if off.item.get("errorClass") == "not_found" and (last is None or last.item.get("errorClass") == "not_found"):
            off.item["triedSources"] = tried
            off.item["source"] = "none"
            return LookupResult(False, off.item, attempts=off.attempts, auth_failed=auth_failed, quota_failed=quota_failed)

    if last is None:
        item = _base_row(gtin, "none")
        if not api_key and not upcitemdb_user_id and not fallback_off:
            item.update({
                "errorClass": "missing_key",
                "errorMessage": "No API key and Open Food Facts fallback disabled.",
                "hint": KEY_HINTS["auto"],
                "attempts": 0,
                "triedSources": tried,
            })
        else:
            item.update({
                "errorClass": "not_found",
                "errorMessage": "No provider returned a product.",
                "attempts": 0,
                "triedSources": tried,
            })
        return LookupResult(False, item, auth_failed=auth_failed, quota_failed=quota_failed)

    last.item["triedSources"] = tried
    if not last.ok and last.item.get("errorClass") == "not_found":
        last.item["source"] = "none"
    last.auth_failed = auth_failed or last.auth_failed
    last.quota_failed = quota_failed or last.quota_failed
    return last
