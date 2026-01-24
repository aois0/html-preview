#!/usr/bin/env python3
"""Generate core/{id}.txt entries for 法人税裁決DB."""
from pathlib import Path
import yaml
from build_utils import (
    load_case_records,
    extract_case_summary,
    extract_conclusion,
    normalize_text,
    extract_issue_hint,
)

BASE_DIR = Path(__file__).resolve().parent.parent
CORE_DIR = BASE_DIR / "core"

CORE_DIR.mkdir(parents=True, exist_ok=True)


def render_frontmatter(record) -> str:
    meta = {
        "id": record.case_id,
        "number": record.number,
        "date": record.date_iso,
        "tribunal": "国税不服審判所",
        "tax_types": record.tax_types,
        "url": record.url,
        "summary_status": "auto",
        "source": {"origin": str(record.source_path)}
    }
    return yaml.dump(meta, allow_unicode=True, sort_keys=False)


def build():
    records = load_case_records()
    for record in records:
        summary = extract_case_summary(record.text)
        issue_hint = extract_issue_hint(record.text)
        conclusion = extract_conclusion(record.text)
        normalized = normalize_text(record.text)

        lines = ["---", render_frontmatter(record).strip(), "---", ""]
        lines.append("# 裁決要約（自動抽出）")
        if summary:
            lines.append(f"- 事案: {summary}")
        if issue_hint:
            lines.append(f"- 争点: {issue_hint}")
        if conclusion:
            lines.append(f"- 結論: {conclusion}")
        lines.append("")
        lines.append("# 原文（整形）")
        lines.append(normalized or "[本文なし]")
        content = "\n".join(lines).strip() + "\n"
        path = CORE_DIR / f"{record.case_id}.txt"
        path.write_text(content, encoding="utf-8")
        print(f"wrote core/{record.case_id}.txt")

if __name__ == "__main__":
    build()
