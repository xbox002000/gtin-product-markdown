"""Apify Actor: GTIN/UPC/EAN → product Markdown cards via UPCitemdb / Barcode Lookup / Open Food Facts.

Bring your own barcode API key. Open Food Facts is the keyless food/FMCG fallback (always labeled `source`).
No Idealo price scraping. No Amazon account scraping. ASIN only if a barcode API returns it.
Output: one dataset row per GTIN + REPORT.md, PRODUCTS.md, optional CHUNKS.jsonl, SUMMARY, OUTPUT.
Pay-per-event: `product-card` per successful product (any source including OFF). Failures are free.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import resource
import time
from collections import Counter

import httpx
from apify import Actor

from barcode_api import KEY_HINTS, UA, lookup_one
from charging import Charger
from inputs import barcode_from_item, normalize_gtins, split_payload
from report import build_chunks_jsonl, build_markdown, build_products_markdown

EVENT_CARD = "product-card"
PLACEHOLDER_KEYS = {"", "YOUR_KEY", "your_key", "xxx", "TODO", "changeme"}

for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


def _int(inp: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = inp.get(key)
    try:
        v = int(default if v is None or v == "" else v)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


def _str(inp: dict, key: str) -> str | None:
    v = inp.get(key)
    return str(v).strip() or None if v is not None else None


def _clean_key(raw: str | None) -> str | None:
    if not raw:
        return None
    k = raw.strip()
    if k in PLACEHOLDER_KEYS or k.upper() == "YOUR_KEY":
        return None
    return k


async def load_from_dataset(dataset_id: str, barcode_field: str | None, limit: int | None) -> tuple[list[str], list[str]]:
    gtins: list[str] = []
    try:
        ds = await Actor.open_dataset(id=dataset_id, force_cloud=bool(os.getenv("APIFY_IS_AT_HOME")))
        async for item in ds.iterate_items():
            if not isinstance(item, dict):
                continue
            g = barcode_from_item(item, barcode_field)
            if g:
                gtins.append(g)
            if limit and len(gtins) >= limit:
                break
    except Exception as e:  # noqa: BLE001
        return [], [f"Could not read dataset {dataset_id}: {e}"]
    return gtins, ([] if gtins else [f"No barcode fields found in dataset {dataset_id}"])


async def load_from_kv(store_id: str, record_key: str, barcode_field: str | None) -> tuple[list[str], list[str], list[str]]:
    try:
        store = await Actor.open_key_value_store(id=store_id, force_cloud=bool(os.getenv("APIFY_IS_AT_HOME")))
        rec = await store.get_value(record_key)
    except Exception as e:  # noqa: BLE001
        return [], [], [f"Could not read KV {store_id}/{record_key}: {e}"]
    if rec is None:
        return [], [], [f"KV record {record_key} not found in store {store_id}"]
    good, bad = split_payload(rec, barcode_field)
    return good, bad, ([] if good or bad else [f"No barcodes found in KV record {record_key}"])


async def main() -> None:
    async with Actor:
        t0 = time.time()
        inp = await Actor.get_input() or {}
        charger = Charger(EVENT_CARD)

        provider = str(inp.get("provider") or "auto").strip().lower()
        if provider not in ("auto", "upcitemdb", "barcodelookup"):
            raise ValueError(f"`provider` must be auto|upcitemdb|barcodelookup, got {provider!r}")

        api_key = _clean_key(inp.get("apiKey"))
        upc_uid = _clean_key(inp.get("upcitemdbUserId"))
        key_source = "input" if api_key or upc_uid else None
        # Private smoke env fallbacks only — never set as Actor default env vars.
        if not api_key and not upc_uid:
            for env_name in ("UPCITEMDB_API_KEY", "BARCODELOOKUP_API_KEY", "BARCODE_API_KEY"):
                if os.getenv(env_name, "").strip():
                    api_key, key_source = os.environ[env_name].strip(), f"env:{env_name}"
                    break

        fallback_off = bool(inp.get("fallbackOpenFoodFacts", True))
        include_chunks = bool(inp.get("includeRagChunks", True))
        max_items = _int(inp, "maxItems", 100, 0, 100000) or None
        conc = _int(inp, "maxConcurrency", 2, 1, 5)
        max_retries = _int(inp, "maxRetries", 3, 0, 6)
        timeout = float(_int(inp, "requestTimeoutSecs", 30, 5, 120))
        stop_on_auth = bool(inp.get("stopOnAuthError", True))
        barcode_field = _str(inp, "barcodeField")

        combined = list(inp.get("gtins") or []) + list(inp.get("barcodes") or [])
        gtins, bad = normalize_gtins(combined, barcode_field)
        warnings: list[str] = []
        if inp.get("datasetId"):
            g, w = await load_from_dataset(str(inp["datasetId"]), barcode_field, max_items)
            gtins += g
            warnings += w
            Actor.log.info(f"Loaded {len(g)} GTIN(s) from dataset {inp['datasetId']}")
        if inp.get("keyValueStoreId"):
            key = inp.get("keyValueRecordKey") or "INPUT_GTINS"
            g, b, w = await load_from_kv(str(inp["keyValueStoreId"]), str(key), barcode_field)
            gtins += g
            bad += b
            warnings += w
            Actor.log.info(f"Loaded {len(g)} GTIN(s) from KV {inp['keyValueStoreId']}/{key}")
        gtins, more_bad = normalize_gtins(gtins, barcode_field)
        bad += more_bad
        for w in warnings:
            Actor.log.warning(w)

        if max_items is not None and len(gtins) > max_items:
            Actor.log.warning(f"{len(gtins)} GTINs; capped to maxItems={max_items}.")
            gtins = gtins[:max_items]

        if not gtins and not bad:
            raise ValueError(
                "Provide at least one entry in `gtins` / `barcodes` (or a datasetId / keyValueStoreId with barcodes)."
            )

        if not api_key and not upc_uid:
            Actor.log.warning(
                "No apiKey. Paid providers will be skipped; "
                + ("Open Food Facts fallback is ON." if fallback_off else "OFF fallback is OFF — rows will be missing_key.")
                + f" {KEY_HINTS['auto']}"
            )
        else:
            Actor.log.info(f"API key from {key_source} (value hidden). Provider mode={provider}.")
        Actor.log.info(f"{len(gtins)} GTIN(s) (+{len(bad)} invalid); concurrency={conc}; fallback_off={fallback_off}")
        await Actor.set_status_message(f"Looking up {len(gtins)} GTIN(s) ({provider})…")

        sem = asyncio.Semaphore(conc)
        lock = asyncio.Lock()
        results: list[tuple[int, dict]] = []
        cls_counts: Counter[str] = Counter()
        src_counts: Counter[str] = Counter()
        state = {"done": 0, "ok": 0, "fail": 0, "skipped": 0, "inflight": 0, "auth_failed": False}

        # Invalid inputs → free rows up front
        for i, raw in enumerate(bad):
            item = {
                "gtin": raw,
                "source": "none",
                "status": "failed",
                "errorClass": "invalid_input",
                "errorMessage": "Not a valid 8–14 digit GTIN/UPC/EAN after normalization.",
                "hint": "Strip spaces/dashes; provide digits only (EAN-8 … GTIN-14).",
                "attempts": 0,
                "images": [],
            }
            results.append((-1000 + i, item))
            cls_counts["invalid_input"] += 1
            state["fail"] += 1
            await charger.push_free(item)

        async with httpx.AsyncClient(
            headers={"User-Agent": UA, "Accept": "application/json"},
            follow_redirects=True,
            limits=httpx.Limits(max_connections=conc + 2),
        ) as client:

            async def one(idx: int, gtin: str) -> None:
                async with sem:
                    async with lock:
                        if charger.limit_reached:
                            state["skipped"] += 1
                            return
                        b = charger.budget()
                        if b is not None and b - state["inflight"] <= 0:
                            state["skipped"] += 1
                            charger.limit_reached = True
                            return
                        skip_paid = stop_on_auth and state["auth_failed"]
                        state["inflight"] += 1
                    try:
                        res = await lookup_one(
                            client,
                            gtin,
                            provider=provider,
                            api_key=api_key,
                            upcitemdb_user_id=upc_uid,
                            fallback_off=fallback_off,
                            max_retries=max_retries,
                            timeout=timeout,
                            skip_paid=skip_paid,
                        )
                        if res.auth_failed or res.quota_failed:
                            # Only mark auth_failed for hard key rejection (not OFF-only path)
                            if res.auth_failed:
                                state["auth_failed"] = True
                    finally:
                        async with lock:
                            state["inflight"] -= 1

                    item = res.item
                    item["checkedAt"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
                    # Never leak secrets into row
                    async with lock:
                        state["done"] += 1
                        if res.ok:
                            n = await charger.push_and_charge([item], event=EVENT_CARD)
                            if n:
                                state["ok"] += 1
                                cls_counts["ok"] += 1
                                src_counts[item.get("source") or "unknown"] += 1
                                results.append((idx, item))
                            else:
                                state["skipped"] += 1
                        else:
                            state["fail"] += 1
                            cls_counts[item.get("errorClass") or "error"] += 1
                            results.append((idx, item))
                            await charger.push_free(item)
                        if res.ok or state["done"] <= 20 or state["done"] % 25 == 0:
                            Actor.log.info(
                                f"[{state['done']}/{len(gtins)}] {gtin}: "
                                + (
                                    f"OK source={item.get('source')} title={str(item.get('title'))[:60]!r}"
                                    if res.ok
                                    else f"FAILED {item.get('errorClass')} (free)"
                                )
                            )
                        if state["done"] % 10 == 0 or state["done"] == len(gtins):
                            await Actor.set_status_message(
                                f"{state['done']}/{len(gtins)} — ok {state['ok']}, failed {state['fail']} (free)"
                            )

            await asyncio.gather(*(one(i, g) for i, g in enumerate(gtins)))

        rows = [r for _, r in sorted(results, key=lambda t: t[0])]
        store = await Actor.open_key_value_store()
        peak_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
        need_key = bool(cls_counts.get("missing_key") or cls_counts.get("unauthorized"))
        summary = {
            "provider": provider,
            "planned": len(gtins),
            "invalidInputs": len(bad),
            "ok": state["ok"],
            "failed": state["fail"],
            "skipped": state["skipped"],
            "byClass": dict(cls_counts),
            "bySource": dict(src_counts),
            "apiKeyProvided": bool(api_key or upc_uid),
            "fallbackOpenFoodFacts": fallback_off,
            "includeRagChunks": include_chunks,
            "keyHint": KEY_HINTS[provider if provider != "auto" else "auto"] if need_key else None,
            "charged": dict(charger.counts),
            "spendingLimitReached": charger.limit_reached,
            "durationSecs": round(time.time() - t0, 1),
            "peakMemoryMb": peak_mb,
            "finishedAt": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
        await store.set_value(
            "REPORT.md",
            build_markdown(rows, summary),
            content_type="text/markdown; charset=utf-8",
        )
        await store.set_value(
            "PRODUCTS.md",
            build_products_markdown(rows),
            content_type="text/markdown; charset=utf-8",
        )
        if include_chunks:
            await store.set_value(
                "CHUNKS.jsonl",
                build_chunks_jsonl(rows),
                content_type="application/x-ndjson; charset=utf-8",
            )
        await store.set_value("SUMMARY", summary)
        await store.set_value("OUTPUT", summary)
        msg = (
            f"Done: {state['ok']} product card(s), {state['fail']} failed (free), {state['skipped']} skipped. "
            f"Charged: {charger.counts or 'nothing'}."
        )
        if need_key and not state["ok"]:
            msg += " Supply a working `apiKey` or enable Open Food Facts fallback — see REPORT.md."
        Actor.log.info(msg + f" Peak memory {peak_mb} MB, {summary['durationSecs']} s.")
        await Actor.set_status_message(msg, is_terminal=True)


if __name__ == "__main__":
    asyncio.run(main())
