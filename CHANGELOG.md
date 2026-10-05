# Changelog

## 0.1.2

- README: step-by-step key setup for UPCitemdb and Barcode Lookup, Open Food Facts keyless fallback details, common key errors.
- Test fixtures use an obviously fake key string.

## 0.1.0

- Initial private release: GTIN/UPC/EAN → Markdown product cards via UPCitemdb, Barcode Lookup (BYOK) and Open Food Facts (keyless fallback).
- PPE: `apify-actor-start` $0.001, `product-card` $0.004 (primary). Failures free.
- Optional RAG chunks (~500–800 chars) in CHUNKS.jsonl.
