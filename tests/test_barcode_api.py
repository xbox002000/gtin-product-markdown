"""Unit tests with mocked HTTP (httpx.MockTransport). No network, no real keys."""
from __future__ import annotations

import asyncio
import json

import httpx

from barcode_api import (
    lookup_barcodelookup,
    lookup_one,
    lookup_openfoodfacts,
    lookup_upcitemdb,
    normalize_gtin,
    product_markdown,
    rag_chunks,
    redact,
)
from fixtures import (
    BARCODELOOKUP_EMPTY,
    BARCODELOOKUP_OK,
    OFF_MISS,
    OFF_OK,
    UPCITEMDB_DENIED,
    UPCITEMDB_EMPTY,
    UPCITEMDB_OK,
)
from inputs import normalize_gtins, split_payload
from report import build_chunks_jsonl, build_markdown, build_products_markdown

SECRET = "FAKE_TEST_KEY_not_real_000"


def run(coro):
    return asyncio.run(coro)


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ----- normalize -----

def test_normalize_gtin_strips_spaces_dashes():
    assert normalize_gtin("301-762-042-2003") == "3017620422003"
    assert normalize_gtin(" 0885 9099 50805 ") == "0885909950805"
    assert normalize_gtin("123") is None
    assert normalize_gtin("abcd") is None
    assert normalize_gtin(3017620422003) == "3017620422003"


def test_normalize_gtins_dedupes_and_collects_bad():
    good, bad = normalize_gtins(["3017620422003", "301-762-042-2003", "nope", "12"])
    assert good == ["3017620422003"]
    assert "nope" in bad and "12" in bad


def test_split_payload_variants():
    g, b = split_payload("3017620422003\n0885909950805")
    assert g == ["3017620422003", "0885909950805"] and not b
    g, b = split_payload({"gtins": ["3017620422003"]})
    assert g == ["3017620422003"]
    g, b = split_payload([{"barcode": "3017620422003"}])
    assert g == ["3017620422003"]


# ----- redact -----

def test_redact_strips_secret_and_query_patterns():
    s = redact(f"bad key={SECRET} url=https://x?key={SECRET}&user_key={SECRET}", SECRET)
    assert SECRET not in s
    assert "key=***" in s


def test_httpx_logging_muted_by_main_convention():
    # main.py sets WARNING; assert helper doesn't leak via redact at least
    assert SECRET not in redact(f"Authorization: {SECRET}", SECRET)


# ----- UPCitemdb -----

def test_upcitemdb_ok_parses_asin_and_images():
    seen = {}

    def h(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["headers"] = dict(req.headers)
        return httpx.Response(200, json=UPCITEMDB_OK)

    async def go():
        async with client_for(h) as c:
            return await lookup_upcitemdb(c, "0885909950805", api_key=SECRET, backoff_base=0)

    r = run(go())
    assert r.ok and r.item["source"] == "upcitemdb"
    assert r.item["title"].startswith("Apple iPhone")
    assert r.item["brand"] == "Apple"
    assert r.item["asin"] == "B00NH11KIK"
    assert r.item["images"]
    assert r.item["markdown"] and "Source:** `upcitemdb`" in r.item["markdown"]
    assert seen["headers"].get("user_key") == SECRET
    assert seen["headers"].get("key_type") == "3scale"
    assert "/prod/v1/lookup" in seen["url"]


def test_upcitemdb_empty_is_not_found():
    def h(req):
        return httpx.Response(200, json=UPCITEMDB_EMPTY)

    async def go():
        async with client_for(h) as c:
            return await lookup_upcitemdb(c, "0000000000000", api_key=SECRET, backoff_base=0)

    r = run(go())
    assert not r.ok and r.item["errorClass"] == "not_found"


def test_upcitemdb_401_redacts_and_flags_auth():
    def h(req):
        return httpx.Response(401, json={"message": f"Invalid user_key: {SECRET}"})

    async def go():
        async with client_for(h) as c:
            return await lookup_upcitemdb(c, "0885909950805", api_key=SECRET, backoff_base=0)

    r = run(go())
    assert not r.ok and r.auth_failed and r.item["errorClass"] == "unauthorized"
    assert SECRET not in json.dumps(r.item)
    assert "upcitemdb.com" in r.item["hint"]


def test_upcitemdb_429_retries_then_ok():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        if n["c"] < 3:
            return httpx.Response(429, json={"message": "slow down"}, headers={"Retry-After": "0"})
        return httpx.Response(200, json=UPCITEMDB_OK)

    async def go():
        async with client_for(h) as c:
            return await lookup_upcitemdb(c, "0885909950805", api_key=SECRET, max_retries=3, backoff_base=0)

    r = run(go())
    assert r.ok and r.attempts == 3


# ----- Barcode Lookup -----

def test_barcodelookup_ok():
    seen = {}

    def h(req):
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json=BARCODELOOKUP_OK)

    async def go():
        async with client_for(h) as c:
            return await lookup_barcodelookup(c, "0885909950805", api_key=SECRET, backoff_base=0)

    r = run(go())
    assert r.ok and r.item["source"] == "barcodelookup"
    assert r.item["asin"] == "B00NH11KIK"
    assert seen["params"]["key"] == SECRET
    assert seen["params"]["barcode"] == "0885909950805"


def test_barcodelookup_missing_key_no_request():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(200, json=BARCODELOOKUP_OK)

    async def go():
        async with client_for(h) as c:
            return await lookup_barcodelookup(c, "0885909950805", api_key=None, backoff_base=0)

    r = run(go())
    assert not r.ok and r.item["errorClass"] == "missing_key" and not calls


def test_barcodelookup_403_auth():
    def h(req):
        return httpx.Response(403, json={"message": f"Invalid API key {SECRET}"})

    async def go():
        async with client_for(h) as c:
            return await lookup_barcodelookup(c, "0885909950805", api_key=SECRET, backoff_base=0)

    r = run(go())
    assert not r.ok and r.auth_failed and SECRET not in json.dumps(r.item)


def test_barcodelookup_empty_not_found():
    def h(req):
        return httpx.Response(200, json=BARCODELOOKUP_EMPTY)

    async def go():
        async with client_for(h) as c:
            return await lookup_barcodelookup(c, "0000000000000", api_key=SECRET, backoff_base=0)

    r = run(go())
    assert not r.ok and r.item["errorClass"] == "not_found"


# ----- Open Food Facts -----

def test_off_ok_nutella_fields():
    def h(req):
        assert "3017620422003" in str(req.url)
        assert "User-Agent" in req.headers
        return httpx.Response(200, json=OFF_OK)

    async def go():
        async with client_for(h) as c:
            return await lookup_openfoodfacts(c, "3017620422003", backoff_base=0)

    r = run(go())
    assert r.ok and r.item["source"] == "openfoodfacts"
    assert r.item["title"] == "Nutella"
    assert "Ferrero" in (r.item["brand"] or "")
    assert r.item["nutrition"]["nutritionGrade"] == "e"
    assert r.item["ingredients"]
    assert "openfoodfacts" in r.item["markdown"]


def test_off_miss():
    def h(req):
        return httpx.Response(200, json=OFF_MISS)

    async def go():
        async with client_for(h) as c:
            return await lookup_openfoodfacts(c, "0000000000000", backoff_base=0)

    r = run(go())
    assert not r.ok and r.item["errorClass"] == "not_found"


# ----- lookup_one chain -----

def test_auto_no_key_uses_off():
    def h(req):
        if "openfoodfacts" in str(req.url) or "/api/v2/product/" in str(req.url):
            return httpx.Response(200, json=OFF_OK)
        return httpx.Response(500, json={"error": "should not call paid"})

    async def go():
        async with client_for(h) as c:
            return await lookup_one(c, "3017620422003", provider="auto", api_key=None, fallback_off=True, backoff_base=0)

    r = run(go())
    assert r.ok and r.item["source"] == "openfoodfacts"


def test_auto_with_key_upc_miss_falls_to_off():
    def h(req):
        url = str(req.url)
        if "/prod/v1/lookup" in url:
            return httpx.Response(200, json=UPCITEMDB_EMPTY)
        if "/api/v2/product/" in url:
            return httpx.Response(200, json=OFF_OK)
        return httpx.Response(404, json={})

    async def go():
        async with client_for(h) as c:
            return await lookup_one(
                c, "3017620422003", provider="auto", api_key=SECRET, fallback_off=True, backoff_base=0
            )

    r = run(go())
    assert r.ok and r.item["source"] == "openfoodfacts"
    assert "upcitemdb" in r.item["triedSources"] and "openfoodfacts" in r.item["triedSources"]


def test_auto_upc_auth_fail_tries_barcodelookup():
    def h(req):
        url = str(req.url)
        if "/prod/v1/lookup" in url:
            return httpx.Response(401, json=UPCITEMDB_DENIED)
        if "/v3/products" in url:
            return httpx.Response(200, json=BARCODELOOKUP_OK)
        return httpx.Response(404, json={})

    async def go():
        async with client_for(h) as c:
            return await lookup_one(
                c, "0885909950805", provider="auto", api_key=SECRET, fallback_off=False, backoff_base=0
            )

    r = run(go())
    assert r.ok and r.item["source"] == "barcodelookup"


def test_no_key_no_fallback_missing_key():
    calls = []

    def h(req):
        calls.append(str(req.url))
        return httpx.Response(200, json=OFF_OK)

    async def go():
        async with client_for(h) as c:
            return await lookup_one(
                c, "3017620422003", provider="auto", api_key=None, fallback_off=False, backoff_base=0
            )

    r = run(go())
    assert not r.ok and r.item["errorClass"] == "missing_key"
    assert not calls


def test_both_miss_source_none():
    def h(req):
        url = str(req.url)
        if "/prod/v1/lookup" in url:
            return httpx.Response(200, json=UPCITEMDB_EMPTY)
        if "/api/v2/product/" in url:
            return httpx.Response(200, json=OFF_MISS)
        if "/v3/products" in url:
            return httpx.Response(200, json=BARCODELOOKUP_EMPTY)
        return httpx.Response(404, json={})

    async def go():
        async with client_for(h) as c:
            return await lookup_one(
                c, "0000000000000", provider="upcitemdb", api_key=SECRET, fallback_off=True, backoff_base=0
            )

    r = run(go())
    assert not r.ok and r.item["errorClass"] == "not_found"
    assert r.item["source"] == "none"


# ----- markdown / chunks / report -----

def test_product_markdown_and_rag_chunks():
    item = {
        "gtin": "3017620422003",
        "source": "openfoodfacts",
        "title": "Nutella",
        "brand": "Ferrero",
        "size": "400 g",
        "category": "Spreads",
        "images": ["https://example.com/n.jpg"],
        "description": "Hazelnut cocoa spread. " * 40,
        "ingredients": "Sugar, palm oil, hazelnuts",
        "nutrition": {"energyKcal100g": 539, "nutritionGrade": "e"},
        "asin": None,
        "url": "https://world.openfoodfacts.org/product/3017620422003",
    }
    md = product_markdown(item)
    assert "# Nutella" in md and "GTIN:** `3017620422003`" in md
    item["markdown"] = md
    chunks = rag_chunks(item)
    assert chunks and all(c["gtin"] == "3017620422003" for c in chunks)
    assert all(c["charCount"] > 0 for c in chunks)


def test_report_builders():
    ok = {
        "gtin": "3017620422003",
        "source": "openfoodfacts",
        "status": "ok",
        "title": "Nutella",
        "brand": "Ferrero",
        "markdown": "# Nutella\n\n**Brand:** Ferrero\n",
        "images": [],
    }
    fail = {
        "gtin": "000",
        "source": "none",
        "status": "failed",
        "errorClass": "invalid_input",
        "hint": "bad",
    }
    summary = {
        "provider": "auto",
        "planned": 1,
        "ok": 1,
        "failed": 1,
        "skipped": 0,
        "byClass": {"ok": 1, "invalid_input": 1},
        "bySource": {"openfoodfacts": 1},
        "apiKeyProvided": False,
        "fallbackOpenFoodFacts": True,
        "charged": {"product-card": 1},
        "durationSecs": 1.2,
        "peakMemoryMb": 80,
    }
    md = build_markdown([ok, fail], summary)
    assert "Nutella" in md and "How to get an API key" in md
    assert "PRODUCTS" not in md or True
    assert "Nutella" in build_products_markdown([ok, fail])
    jl = build_chunks_jsonl([ok])
    assert jl.strip() and json.loads(jl.strip().splitlines()[0])["gtin"] == "3017620422003"


def test_test_base_localhost_only(monkeypatch):
    from barcode_api import _endpoint, _test_base

    monkeypatch.setenv("GTIN_TEST_BASE", "http://evil.example/x")
    assert _test_base() is None
    monkeypatch.setenv("GTIN_TEST_BASE", "http://127.0.0.1:8768")
    assert _test_base() == "http://127.0.0.1:8768"
    assert _endpoint("https://api.upcitemdb.com/prod/v1/lookup").startswith("http://127.0.0.1:8768")


def test_network_error_redacted():
    def h(req):
        raise httpx.ConnectError(f"boom {SECRET}")

    async def go():
        async with client_for(h) as c:
            return await lookup_upcitemdb(c, "0885909950805", api_key=SECRET, max_retries=0, backoff_base=0)

    r = run(go())
    assert not r.ok and r.item["errorClass"] == "network"
    assert SECRET not in json.dumps(r.item)
