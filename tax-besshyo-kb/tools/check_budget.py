#!/usr/bin/env python3
"""Check that each card keeps the important parts early.

We approximate token budget by character count. The goal is to avoid very long pages.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CARDS_DIR = ROOT / "kb/cards"


def approx_tokens(text: str) -> int:
    # rough: 1 token ~ 3.5 chars (JP/EN mixed is messy, but this is a safe-ish upper bound)
    return int(len(text) / 3.5)


def strip_html(s: str) -> str:
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10000, help="approx token limit")
    args = ap.parse_args()

    bad = 0
    for p in sorted(CARDS_DIR.glob("*.html")):
        if p.name.startswith("_"):
            continue
        raw = p.read_text(encoding="utf-8", errors="replace")
        txt = strip_html(raw)
        t = approx_tokens(txt)
        if t > args.limit:
            print(f"NG {p.name}: approx_tokens={t} > {args.limit}")
            bad += 1
        else:
            print(f"OK {p.name}: approx_tokens={t}")
        # require TL;DR early
        head = txt[:1200]
        if "TL;DR" not in head:
            print(f"  WARN: TL;DR not in early head")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
