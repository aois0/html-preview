#!/usr/bin/env python3
"""
Generate per-case core/{id}.txt for the Corporate Tax (法人税) case set.

- Source metadata: /home/user/ai-law-db/simple/hanketsu/{id}.json
- Optional summary text: extracted from existing core/batch-*.txt (if present)
- Output: core/{id}.txt (YAML frontmatter + summary/placeholder)
"""

import json
import re
from pathlib import Path

import yaml


SOURCE_DIR = Path("/home/user/ai-law-db/simple/hanketsu")
LIST_FILE = Path("/tmp/houjinzei_list.txt")

BASE_DIR = Path(__file__).parent.parent
CORE_DIR = BASE_DIR / "core"


CASE_HEADER_RE = re.compile(r"^##\s+(\d+):", re.MULTILINE)


def load_case_ids() -> list[str]:
    return [line.strip() for line in LIST_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]


def extract_segments_from_batches() -> tuple[dict[str, str], dict[str, str]]:
    """
    Returns:
      - segments: case_id -> markdown segment (starts with '## {id}:')
      - sources: case_id -> batch filename (e.g., 'batch-004.txt')
    """
    segments: dict[str, str] = {}
    sources: dict[str, str] = {}

    for batch_path in sorted(CORE_DIR.glob("batch-*.txt")):
        text = batch_path.read_text(encoding="utf-8", errors="replace")
        matches = list(CASE_HEADER_RE.finditer(text))
        if not matches:
            continue

        for idx, match in enumerate(matches):
            case_id = match.group(1)
            start = match.start()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            segment = text[start:end].strip()
            if not segment:
                continue
            if case_id in segments:
                continue
            segments[case_id] = segment
            sources[case_id] = batch_path.name

    return segments, sources


def load_metadata(case_id: str) -> dict:
    json_path = SOURCE_DIR / f"{case_id}.json"
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected JSON root for {case_id}: {type(data)}")
    return data


def write_core_file(case_id: str, meta: dict, segment: str | None, segment_source: str | None) -> None:
    out_path = CORE_DIR / f"{case_id}.txt"

    frontmatter = {
        "id": case_id,
        "date": meta.get("date_iso") or meta.get("date") or "",
        "court": meta.get("court") or "",
        "title": meta.get("title") or "",
        "topics": meta.get("topics") or [],
        "keywords": meta.get("keywords") or [],
        "laws": meta.get("laws") or [],
        "result": meta.get("result") or "",
        "summary_status": "available" if segment else "missing",
        "summary_source": segment_source or "",
        "source": {
            "origin": f"aois0/ai-law-db simple/hanketsu/{case_id}.json",
        },
    }

    yaml_txt = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()

    body_lines: list[str] = []
    body_lines.append("# 判決要約")
    body_lines.append("")

    if segment:
        body_lines.append(segment)
    else:
        body_lines.append("（要約未生成）")
        body_lines.append("")
        body_lines.append("## 手掛かり（メタデータ）")
        body_lines.append(f"- topics: {', '.join(frontmatter['topics']) if frontmatter['topics'] else ''}")
        body_lines.append(f"- keywords: {', '.join(frontmatter['keywords'][:10]) if frontmatter['keywords'] else ''}")
        body_lines.append(f"- laws: {', '.join(frontmatter['laws'][:10]) if frontmatter['laws'] else ''}")
        body_lines.append(f"- result: {frontmatter['result']}")

    body_lines.append("")
    body_lines.append("---")
    body_lines.append("*このファイルはAI検索用です。正確な内容は原文を参照してください。*")
    body_lines.append("")

    out_path.write_text(f"---\n{yaml_txt}\n---\n\n" + "\n".join(body_lines), encoding="utf-8")


def main() -> None:
    CORE_DIR.mkdir(parents=True, exist_ok=True)

    segments, sources = extract_segments_from_batches()
    case_ids = load_case_ids()

    ok = 0
    missing_meta = 0
    for case_id in case_ids:
        try:
            meta = load_metadata(case_id)
        except Exception:
            missing_meta += 1
            continue

        segment = segments.get(case_id)
        segment_source = sources.get(case_id)
        write_core_file(case_id, meta, segment, segment_source)
        ok += 1

    print(f"Generated core files: {ok} / {len(case_ids)}")
    if missing_meta:
        print(f"Missing metadata JSON: {missing_meta}")


if __name__ == "__main__":
    main()

