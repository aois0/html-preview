#!/usr/bin/env python3
"""Quality check helper for ai-hanketsu-db/houjinzei.

Examples:
  python3 ai-hanketsu-db/houjinzei/tools/qa_shard.py --shard 08
  python3 ai-hanketsu-db/houjinzei/tools/qa_shard.py --shard 08 --json out.json
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from collections import Counter
from pathlib import Path

import yaml


BASE_DIR = Path(__file__).parent.parent
CORE_DIR = BASE_DIR / "core"
SHARDS_DIR = BASE_DIR / "data" / "shards"

AUTO_NOTE = "（注）本ファイルはAI検索用に、判決文から要点・抜粋を機械的に抽出・整形したものです。"
APPEAL_MARKERS = ("控訴", "上告", "抗告")


def parse_frontmatter_and_body(text: str) -> tuple[dict, str]:
    m = re.search(r"^---\s*\n(.*?)\n---\s*\n", text, re.S | re.M)
    if not m:
        return {}, text
    fm_raw = m.group(1)
    body = text[m.end() :]
    data = yaml.safe_load(fm_raw) or {}
    if not isinstance(data, dict):
        data = {}
    return data, body


def load_ids_from_shard(shard: str) -> list[str]:
    shard_num = int(shard)
    shard_file = SHARDS_DIR / f"shard-{shard_num:02d}.txt"
    rows = list(
        csv.DictReader(
            shard_file.read_text(encoding="utf-8", errors="replace").splitlines(), delimiter="\t"
        )
    )
    return [row["id"].strip() for row in rows if row.get("id", "").strip()]


def extract_shubun_line(body: str) -> str:
    m = re.search(r"^- 主文:\s*(.+)$", body, re.M)
    return (m.group(1).strip() if m else "")


def shubun_result_mismatch(front: dict, body: str) -> dict | None:
    """Rough heuristic comparing YAML `result` and extracted '- 主文:'.

    Notes:
      - For appellate judgments, '控訴棄却' etc may still be compatible with '取消'
        depending on who appealed and what the原審 did.
    """

    result = str(front.get("result") or "").strip()
    shubun = extract_shubun_line(body)
    if not (result and shubun):
        return None

    def has_any(s: str, words: tuple[str, ...]) -> bool:
        return any(w in s for w in words)

    cancel_words = ("取消", "取り消", "取消し", "取り消し")
    dismiss_words = ("棄却", "却下")

    if result == "取消" and has_any(shubun, dismiss_words) and not has_any(shubun, cancel_words):
        return {
            "id": front.get("id"),
            "result": result,
            "shubun": shubun,
            "appeal_like": has_any(shubun, APPEAL_MARKERS),
        }

    if result in dismiss_words and has_any(shubun, cancel_words) and not has_any(shubun, dismiss_words):
        return {
            "id": front.get("id"),
            "result": result,
            "shubun": shubun,
            "appeal_like": has_any(shubun, APPEAL_MARKERS),
        }

    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", required=True, help="e.g. 08")
    parser.add_argument("--json", default="", help="optional output json path")
    args = parser.parse_args()

    ids = load_ids_from_shard(args.shard)
    if not ids:
        raise SystemExit(f"No ids found for shard {args.shard}")

    sources = Counter()
    statuses = Counter()
    body_lens: list[int] = []
    auto_ids: list[str] = []
    mismatch_rows: list[dict] = []
    truncated_ids: list[str] = []

    for cid in ids:
        core_path = CORE_DIR / f"{cid}.txt"
        text = core_path.read_text(encoding="utf-8", errors="replace")
        front, body = parse_frontmatter_and_body(text)

        sources[str(front.get("summary_source") or "(none)")] += 1
        statuses[str(front.get("summary_status") or "(none)")] += 1

        body_stripped = body.strip()
        body_lens.append(len(body_stripped))

        if AUTO_NOTE in body:
            auto_ids.append(cid)
        if "chars truncated" in body:
            truncated_ids.append(cid)

        mismatch = shubun_result_mismatch(front, body)
        if mismatch:
            mismatch_rows.append(mismatch)

    body_lens_sorted = sorted(body_lens)
    report = {
        "shard": str(int(args.shard)),
        "total": len(ids),
        "summary_source": dict(sources),
        "summary_status": dict(statuses),
        "auto_note_count": len(auto_ids),
        "auto_note_ids": auto_ids,
        "truncated_marker_count": len(truncated_ids),
        "truncated_marker_ids": truncated_ids,
        "body_len_chars": {
            "min": body_lens_sorted[0],
            "median": int(statistics.median(body_lens_sorted)),
            "mean": int(statistics.mean(body_lens_sorted)),
            "max": body_lens_sorted[-1],
        },
        "result_vs_shubun_mismatch": {
            "total": len(mismatch_rows),
            "appeal_like": sum(1 for r in mismatch_rows if r.get("appeal_like")),
            "non_appeal": sum(1 for r in mismatch_rows if not r.get("appeal_like")),
            "rows": mismatch_rows,
        },
    }

    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
