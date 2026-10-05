"""REPORT.md / PRODUCTS.md / CHUNKS.jsonl builders."""
from __future__ import annotations

import json
from typing import Any

from barcode_api import KEY_HINTS, product_markdown, rag_chunks


def _esc(v: Any, n: int = 80) -> str:
    if v is None or v == "":
        return "—"
    s = str(v).replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def build_products_markdown(rows: list[dict]) -> str:
    cards = []
    for r in rows:
        if r.get("status") != "ok":
            continue
        cards.append(r.get("markdown") or product_markdown(r))
    return ("\n---\n\n".join(cards) + "\n") if cards else "_No successful product cards._\n"


def build_chunks_jsonl(rows: list[dict]) -> str:
    lines: list[str] = []
    for r in rows:
        if r.get("status") != "ok":
            continue
        for ch in rag_chunks(r):
            lines.append(json.dumps(ch, ensure_ascii=False))
    return "\n".join(lines) + ("\n" if lines else "")


def build_markdown(rows: list[dict], summary: dict, max_cards: int = 100) -> str:
    lines = [
        "# GTIN → product Markdown report",
        "",
        f"- Provider mode: **{summary.get('provider')}**",
        f"- Planned: {summary.get('planned', 0)}",
        f"- OK: **{summary.get('ok', 0)}** · Failed (free): **{summary.get('failed', 0)}** · Skipped: {summary.get('skipped', 0)}",
        f"- API key provided: {'yes' if summary.get('apiKeyProvided') else '**no**'}",
        f"- Open Food Facts fallback: {'on' if summary.get('fallbackOpenFoodFacts') else 'off'}",
        f"- Charged events: {', '.join(f'`{k}` ×{v}' for k, v in (summary.get('charged') or {}).items()) or 'none'}",
        f"- Duration: {summary.get('durationSecs')} s · Peak memory: {summary.get('peakMemoryMb')} MB",
        "",
    ]
    by_src = summary.get("bySource") or {}
    if by_src:
        lines += ["| Source | Count |", "|---|---:|"] + [f"| {_esc(k)} | {v} |" for k, v in sorted(by_src.items())] + [""]
    by = summary.get("byClass") or {}
    if by:
        lines += ["| Result | Count |", "|---|---:|"] + [f"| {_esc(k)} | {v} |" for k, v in sorted(by.items())] + [""]

    need_key = by.get("missing_key") or by.get("unauthorized") or by.get("quota_exceeded")
    if need_key or not summary.get("apiKeyProvided"):
        lines += ["## How to get an API key", ""]
        lines += [
            f"- **UPCitemdb**: {KEY_HINTS['upcitemdb']}",
            f"- **Barcode Lookup**: {KEY_HINTS['barcodelookup']}",
            "- **Open Food Facts**: no key required (food/FMCG). Enable `fallbackOpenFoodFacts`.",
            "",
            "Paste the key into the `apiKey` input (stored encrypted, never logged). "
            "Failed / not-found rows are not charged.",
            "",
        ]

    ok_rows = [r for r in rows if r.get("status") == "ok"]
    lines += ["## Product cards", ""]
    if not ok_rows:
        lines.append("_No successful product cards in this run._")
    for i, r in enumerate(ok_rows[:max_cards], 1):
        lines.append(f"### {i}. {_esc(r.get('title') or r.get('gtin'), 100)}")
        lines.append("")
        lines.append(r.get("markdown") or product_markdown(r))
        lines.append("")
    if len(ok_rows) > max_cards:
        lines.append(f"…{len(ok_rows) - max_cards} more card(s) in PRODUCTS.md / dataset.")

    fail_rows = [r for r in rows if r.get("status") != "ok"]
    if fail_rows:
        lines += ["", "## Failed (not charged)", "",
                  "| GTIN | Source | Error | Hint |",
                  "|---|---|---|---|"]
        for r in fail_rows[:200]:
            lines.append(
                f"| `{_esc(r.get('gtin'), 20)}` | {_esc(r.get('source'), 16)} | "
                f"{_esc(r.get('errorClass'), 24)} | {_esc(r.get('hint') or r.get('errorMessage'), 80)} |"
            )
    return "\n".join(lines).rstrip() + "\n"
