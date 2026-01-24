#!/usr/bin/env python3
"""QA for 法人税裁決DB."""
from pathlib import Path
from build_utils import load_case_records, extract_issue_hint

BASE_DIR = Path(__file__).resolve().parent.parent
CORE_DIR = BASE_DIR / "core"
SHARDS_DIR = BASE_DIR / "data" / "shards"


def count_shard_rows() -> int:
    total = 0
    for path in sorted(SHARDS_DIR.glob("shard-*.txt")):
        lines = path.read_text(encoding="utf-8").splitlines()
        total += max(0, len(lines) - 1)
    return total


def run():
    records = load_case_records()
    missing_core = [r.case_id for r in records if not (CORE_DIR / f"{r.case_id}.txt").exists()]
    issue_empty = [r.case_id for r in records if not extract_issue_hint(r.text)]
    shards_total = count_shard_rows()

    report = [
        f"records: {len(records)}",
        f"shard rows: {shards_total}",
        f"core files missing: {len(missing_core)}",
        f"records with empty issue hint: {len(issue_empty)}",
    ]
    if missing_core:
        report.append(f"missing core ids: {', '.join(missing_core[:10])}{'...' if len(missing_core)>10 else ''}")
    if issue_empty:
        report.append(f"issue hint empty ids: {', '.join(issue_empty[:10])}{'...' if len(issue_empty)>10 else ''}")
    report_path = BASE_DIR / "qa_report.txt"
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    print("QA report written to", report_path)


if __name__ == "__main__":
    run()
