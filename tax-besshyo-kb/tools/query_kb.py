#!/usr/bin/env python3
"""Minimal local search for tax-besshyo-kb.

Design constraints:
- No external deps.
- Optimized for "few reads" workflows: return card URLs first.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "kb/index/field_index.jsonl"
CARDS_DIR = ROOT / "kb/cards"

TOK_RE = re.compile(r"[A-Za-z0-9_]+|[一-龠々〆ヵヶぁ-んァ-ヴー]+")


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOK_RE.findall(text or "")]


def load_index() -> list[dict]:
    rows = []
    for line in INDEX_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def read_card_text(card_id: str, limit_chars: int = 3500) -> str:
    p = CARDS_DIR / f"{card_id}.html"
    if not p.exists():
        return ""
    s = p.read_text(encoding="utf-8", errors="replace")
    # crude html strip
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit_chars]


def bm25_score(query_toks: list[str], doc_toks: list[str]) -> float:
    # tiny BM25-ish without corpus idf; good enough for a small curated index.
    if not query_toks or not doc_toks:
        return 0.0
    tf = {}
    for t in doc_toks:
        tf[t] = tf.get(t, 0) + 1
    score = 0.0
    doc_len = len(doc_toks)
    k1 = 1.2
    b = 0.75
    avgdl = 400.0
    norm = (1 - b) + b * (doc_len / avgdl)
    for q in query_toks:
        f = tf.get(q, 0)
        if f == 0:
            continue
        score += (f * (k1 + 1)) / (f + k1 * norm)
    return score


def iter_candidates(rows: list[dict]) -> Iterable[tuple[dict, str]]:
    for r in rows:
        blob = " ".join(
            [
                r.get("card_id", ""),
                r.get("title", ""),
                " ".join(r.get("tags", []) or []),
                " ".join(r.get("synonyms", []) or []),
            ]
        )
        yield r, blob


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help="free text query")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--show", action="store_true", help="show snippet")
    args = ap.parse_args()

    rows = load_index()
    q = args.query.strip()
    qt = tokenize(q)

    scored = []
    for r, blob in iter_candidates(rows):
        base = bm25_score(qt, tokenize(blob))
        # bonus for exact card_id / number hits
        bonus = 0.0
        if r.get("card_id", "") in q:
            bonus += 3.0
        if any(re.fullmatch(r"\d+", t) for t in qt) and any(re.fullmatch(r"\d+", t) for t in tokenize(r.get("title", ""))):
            bonus += 0.2
        scored.append((base + bonus, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    for s, r in scored[: args.topk]:
        card_id = r["card_id"]
        print(f"[{card_id}] {r.get('title','')} score={s:.3f}")
        print(f"  url: kb/cards/{card_id}.html")
        if args.show:
            snippet = read_card_text(card_id)
            print(f"  snippet: {snippet[:220]}{'...' if len(snippet)>220 else ''}")
        links = r.get("links") or []
        if links:
            print(f"  links: {', '.join(links[:8])}{'...' if len(links)>8 else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
