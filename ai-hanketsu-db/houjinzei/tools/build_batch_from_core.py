#!/usr/bin/env python3
"""Build a new batch-XXX.txt from existing metadata/full_text for a shard.

Purpose: help convert remaining non-batch cases in a shard into batch summaries.

This script generates *draft* summaries using the same auto-structuring logic as
build_core_by_id.py (sentence extraction). It intentionally avoids LLM calls.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import build_core_by_id


BASE_DIR = Path(__file__).parent.parent
SHARDS_DIR = BASE_DIR / "data" / "shards"
CORE_DIR = BASE_DIR / "core"
SOURCE_DIR = Path("/home/user/ai-law-db/simple/hanketsu")

SUMMARY_SOURCE_RE = re.compile(r"^summary_source:\s*(.*)\s*$", re.M)


def load_ids_from_shard(shard: str) -> list[str]:
    s = shard.strip()
    if s.isdigit():
        shard_file = SHARDS_DIR / f"shard-{int(s):02d}.txt"
    else:
        name = s
        if not name.startswith("shard-"):
            name = f"shard-{name}"
        if not name.endswith(".txt"):
            name += ".txt"
        shard_file = SHARDS_DIR / name

    lines = shard_file.read_text(encoding="utf-8", errors="replace").splitlines()
    ids: list[str] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        ids.append(line.split("\t", 1)[0].strip())
    return ids


def get_current_summary_source(case_id: str) -> str:
    p = CORE_DIR / f"{case_id}.txt"
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8", errors="replace")
    m = SUMMARY_SOURCE_RE.search(text)
    return m.group(1).strip() if m else ""


def load_meta(case_id: str) -> dict:
    path = SOURCE_DIR / f"{case_id}.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise ValueError(f"Unexpected JSON root for {case_id}: {type(meta)}")

    # Normalize raw text into meta['text'] so build_auto_segment() can operate.
    raw = (meta.get("text") or "").strip()
    if not raw:
        raw = (meta.get("full_text") or "").strip()
    if not raw:
        html_path = SOURCE_DIR / f"{case_id}.html"
        if html_path.exists():
            raw = html_path.read_text(encoding="utf-8", errors="replace")
    meta["text"] = raw
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", required=True, help="Shard number (e.g. 08) or filename suffix.")
    parser.add_argument("--out", required=True, help="Output batch filename (e.g. batch-306.txt)")
    parser.add_argument(
        "--include",
        choices=["non_batch", "all"],
        default="non_batch",
        help="Which cases to include from the shard.",
    )
    args = parser.parse_args()

    out_name = args.out
    if not out_name.startswith("batch-"):
        raise SystemExit("--out must be like batch-XXX.txt")
    if not out_name.endswith(".txt"):
        out_name += ".txt"

    ids = load_ids_from_shard(args.shard)

    targets: list[str] = []
    for cid in ids:
        src = get_current_summary_source(cid)
        if args.include == "non_batch":
            if src.startswith("batch-"):
                continue
        targets.append(cid)

    out_lines: list[str] = [f"# {Path(out_name).stem}", "", "---", ""]

    ok = 0
    meta_only = 0
    for idx, cid in enumerate(targets):
        meta = load_meta(cid)
        segment, source = build_core_by_id.build_auto_segment(cid, meta)
        if not segment:
            segment = f"## {cid}: {meta.get('title','') or ''}".rstrip() + "\n\n（要約未生成）"
            source = "meta_only_v1"

        if source == "meta_only_v1":
            meta_only += 1

        out_lines.append(segment.strip())
        out_lines.append("")
        if idx + 1 < len(targets):
            out_lines.append("---")
            out_lines.append("")
        ok += 1

    out_path = CORE_DIR / out_name
    out_path.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8")

    print(f"wrote: {out_path}")
    print(f"cases: {ok} (meta_only_fallback={meta_only})")


if __name__ == "__main__":
    main()
