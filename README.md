# GTIN / UPC / EAN → Product Markdown Card

Turn a list of barcodes into structured product records and Markdown cards for catalogs, ERP cleanup, and RAG product stores.

## What you get

- **Dataset** — one row per GTIN: `gtin`, `source` (`upcitemdb` | `barcodelookup` | `openfoodfacts` | `none`), `title`, `brand`, `size`, `category`, `images[]`, `description`, `ingredients`, `nutrition` (OFF), `asin` (only if the barcode API returned it), `url`, `markdown`, `errorClass` / `hint`
- **Key-value store**
  - `REPORT.md` — summary + one card per successful product
  - `PRODUCTS.md` — cards only
  - `CHUNKS.jsonl` — optional ~500–800 character RAG chunks with headings
  - `SUMMARY` / `OUTPUT` — run stats and charged events

## Providers (bring your own key)

| Provider | Auth | Coverage |
|---|---|---|
| **UPCitemdb** | Paid `user_key` in `apiKey` (header + `key_type: 3scale`) | General products |
| **Barcode Lookup** | `key` query param from `apiKey` | General products (+ ASIN when present) |
| **Open Food Facts** | **No key** | Food / FMCG fallback — always labeled `source=openfoodfacts` |

**Not included:** Idealo price scraping, Amazon account / storefront crawling. ASIN is an optional field only when UPCitemdb or Barcode Lookup already return it.

### How to get a key

You pay the barcode provider directly; this Actor never ships with a built-in key.

**UPCitemdb** (general retail products)

1. Open https://www.upcitemdb.com/api and pick a paid plan (DEV or PRO). Check that page for current quotas and prices.
2. After signup, copy your `user_key` from the account dashboard.
3. Paste it into the secret `apiKey` input and set `provider` to `upcitemdb` or `auto`.
   The Actor sends it as the `user_key` header with `key_type: 3scale` to `/prod/v1/lookup`.
4. Optional: `upcitemdbUserId` overrides the UPCitemdb `user_key` if you want a different key than `apiKey`.

**Barcode Lookup** (general products, sometimes includes an ASIN)

1. Open https://www.barcodelookup.com/api and create an account / choose a plan.
2. Copy the API key shown on your dashboard.
3. Paste it into `apiKey` and set `provider` to `barcodelookup` or `auto`.
   The Actor sends it as the `key` query parameter to `/v3/products`.

**Open Food Facts fallback (no key)**

- Leave `fallbackOpenFoodFacts: true` (the default). Barcodes that the paid provider misses, or every barcode when no key is set, are looked up in the free, community-run [Open Food Facts](https://world.openfoodfacts.org) database.
- Coverage is mainly **food, drinks and other FMCG**. Electronics, apparel and similar products usually return `not_found` (free).
- Rows from this source are always labeled `source=openfoodfacts`. Data is under the Open Database License (ODbL); keep attribution when you republish it.
- To try the Actor without any key, run the first example input below with a food barcode.

**Common key errors** (all free rows, with a `hint`)

| `errorClass` | Usual cause | Fix |
|---|---|---|
| `missing_key` | `provider` is `upcitemdb` / `barcodelookup` but `apiKey` is empty, and Open Food Facts did not match | Add your key, or use `provider: auto` with the fallback on |
| `unauthorized` | Wrong, expired or wrong-provider key (HTTP 401/403) | Re-copy the key; make sure it belongs to the provider you selected. With `stopOnAuthError` the run stops calling that provider |
| `rate_limited` | Plan quota or per-minute limit hit (HTTP 429) after retries | Lower `maxConcurrency`, raise your plan, or re-run later |

Never put real keys in Actor environment variables, README examples or shared inputs. Use the secret `apiKey` input (placeholder in examples: `YOUR_KEY`).

## Input highlights

| Field | Default | Notes |
|---|---|---|
| `gtins` / `barcodes` | — | String list; digits only after normalize (strip spaces/dashes) |
| `datasetId` / `keyValueStoreId` | — | Optional bulk sources |
| `provider` | `auto` | `auto` \| `upcitemdb` \| `barcodelookup` |
| `apiKey` | — | Secret. Required for paid providers |
| `fallbackOpenFoodFacts` | `true` | Keyless food/FMCG fallback |
| `includeRagChunks` | `true` | Write `CHUNKS.jsonl` |
| `maxItems` | `100` | Cap |
| `maxConcurrency` | `2` | Max 5 |
| `maxRetries` | `3` | 429 / 5xx / timeouts |
| `stopOnAuthError` | `true` | Skip further paid calls after 401/403 |

### `provider=auto` behavior

1. If `apiKey` is set → try **UPCitemdb**, then **Barcode Lookup** on auth failure.
2. On miss (or no key) → **Open Food Facts** when `fallbackOpenFoodFacts` is true.
3. Still missing → `source=none`, `errorClass=not_found` (free).

## Pricing (pay-per-event)

| Event | Price | When |
|---|---|---|
| `apify-actor-start` | $0.001 | Once per run |
| `product-card` | **$0.004** | Each successful product card (any source, including Open Food Facts) |

**Free:** not found, missing key (when OFF also misses / OFF disabled), unauthorized, rate-limited after retries, invalid input, timeouts, network/server errors.

## Example input

```json
{
  "gtins": ["3017620422003"],
  "provider": "auto",
  "fallbackOpenFoodFacts": true,
  "includeRagChunks": true,
  "maxConcurrency": 2
}
```

With a paid key:

```json
{
  "gtins": ["0885909950805", "3017620422003"],
  "provider": "auto",
  "apiKey": "YOUR_KEY",
  "fallbackOpenFoodFacts": true
}
```

## Memory & limits

Default **256 MB** (max 512 MB). Timeout 3600 s. Categories: ECOMMERCE, DEVELOPER_TOOLS, AUTOMATION.

## Attribution

Product data belongs to the upstream provider named in `source`. This Actor only calls official HTTP APIs.
