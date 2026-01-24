#!/usr/bin/env python3
"""Generate TSV shards for 法人税裁決DB."""
from pathlib import Path
import json
from build_utils import chunk_records, load_case_records, extract_issue_hint

SHARD_SIZE = 100
BASE_DIR = Path(__file__).resolve().parent.parent
SHARDS_DIR = BASE_DIR / "data" / "shards"
HEADER = "id\tnumber\tdate_iso\ttax_types\tissue_hint\turl"


def sanitize(text: str) -> str:
    if not text:
        return ""
    return " ".join(text.replace("\t", " ").split())


def build():
    SHARDS_DIR.mkdir(parents=True, exist_ok=True)
    records = load_case_records()
    shard_meta = []
    for idx, batch in enumerate(chunk_records(records, SHARD_SIZE)):
        shard_path = SHARDS_DIR / f"shard-{idx:02d}.txt"
        lines = [HEADER]
        for rec in batch:
            issue_hint = sanitize(extract_issue_hint(rec.text))
            tax_types = ",".join(rec.tax_types)
            row = "\t".join([
                rec.case_id,
                sanitize(rec.number),
                rec.date_iso,
                tax_types,
                issue_hint,
                rec.url,
            ])
            lines.append(row)
        shard_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        shard_meta.append({
            "file": f"data/shards/shard-{idx:02d}.txt",
            "range": f"{batch[0].case_id}-{batch[-1].case_id}",
            "count": len(batch),
        })
        print(f"wrote {len(batch)} records to {shard_path.name}")
    index = {
        "total": len(records),
        "shard_size": SHARD_SIZE,
        "format": "tsv",
        "fields": ["id", "number", "date_iso", "tax_types", "issue_hint", "url"],
        "shards": shard_meta,
    }
    with open(SHARDS_DIR.parent / "shards_index.json", "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    print("shards_index.json updated")


if __name__ == "__main__":
    build()
