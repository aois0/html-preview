#!/usr/bin/env python3
"""
Normalize legacy batch-*.txt files into the standard format:

- File header: "# batch-XYZ"
- Separator: "---"
- Per-case section header: "## {case_id}: {title}"

This makes downstream extraction deterministic (e.g., build_core_by_id.py expects
"## {id}:" headings).
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path


SOURCE_DIR = Path("/home/user/ai-law-db/simple/hanketsu")

BASE_DIR = Path(__file__).parent.parent
CORE_DIR = BASE_DIR / "core"


STANDARD_CASE_HEADER_RE = re.compile(r"^##\s+\d{5}:", re.MULTILINE)
LEGACY_CASE_ID_RE = re.compile(r"^##\s*case_id:\s*(\d{5})\s*$", re.MULTILINE)
LEGACY_LABEL_RE = re.compile(
    r"^(?:#{1,6}\s*)?(?:判決|判例)\s+(?P<id>\d{5})\s*$",
    re.MULTILINE,
)

STRUCTURAL_HEADINGS = (
    "結論",
    "事案の概要",
    "争点",
    "裁判所の判断",
    "条文・通達の解説",
    "法的解説",
    "理由",
)


def is_separator_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if re.fullmatch(r"[=\-_*—─]{3,}", s):
        return True
    return False


def strip_heading_prefix(line: str) -> str:
    s = line.strip()
    s = re.sub(r"^#{1,6}\s*", "", s)
    s = s.strip()
    # Trim trailing parenthetical notes often appended to titles.
    s = re.sub(r"\s*[（(].*[)）]\s*$", "", s).strip()
    return s


def load_title(case_id: str) -> str:
    path = SOURCE_DIR / f"{case_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return ""
    return str(data.get("title") or "").strip()


@dataclass(frozen=True)
class CaseBlock:
    case_id: str
    raw: str


def split_cases(text: str) -> list[CaseBlock]:
    matches: list[tuple[int, int, str]] = []

    for m in LEGACY_CASE_ID_RE.finditer(text):
        matches.append((m.start(), m.end(), m.group(1)))

    for m in LEGACY_LABEL_RE.finditer(text):
        matches.append((m.start(), m.end(), m.group("id")))

    matches.sort(key=lambda x: x[0])

    if not matches:
        return []

    blocks: list[CaseBlock] = []
    for idx, (start, _end, case_id) in enumerate(matches):
        end = matches[idx + 1][0] if idx + 1 < len(matches) else len(text)
        raw = text[start:end]
        blocks.append(CaseBlock(case_id=case_id, raw=raw))

    return blocks


def normalize_body(raw: str, title: str) -> str:
    lines = raw.splitlines()

    if not lines:
        return ""

    # Drop the legacy case header line (判決/判例/case_id).
    lines = lines[1:]

    # Trim leading noise.
    while lines and (not lines[0].strip() or is_separator_line(lines[0])):
        lines.pop(0)

    # Drop a duplicated title line if it appears immediately after the header.
    if title and lines:
        first = lines[0].strip()
        first_text = strip_heading_prefix(first)
        if first_text and not first_text.startswith(STRUCTURAL_HEADINGS):
            if title in first_text or first_text in title:
                lines.pop(0)
                while lines and (not lines[0].strip() or is_separator_line(lines[0])):
                    lines.pop(0)

    # Trim trailing noise (common separators between cases).
    while lines and (not lines[-1].strip() or is_separator_line(lines[-1])):
        lines.pop()

    return "\n".join(lines).strip()


def normalize_batch_file(path: Path, dry_run: bool) -> bool:
    text = path.read_text(encoding="utf-8", errors="replace")

    if STANDARD_CASE_HEADER_RE.search(text):
        return False

    blocks = split_cases(text)
    if not blocks:
        return False

    out: list[str] = [f"# {path.stem}", "", "---", ""]
    for idx, block in enumerate(blocks):
        title = ""
        try:
            title = load_title(block.case_id)
        except Exception:
            title = ""

        header = f"## {block.case_id}: {title}".rstrip()
        out.append(header)
        out.append("")

        body = normalize_body(block.raw, title=title)
        out.append(body if body else "（要約未生成）")
        out.append("")

        if idx + 1 < len(blocks):
            out.append("---")
            out.append("")

    new_text = "\n".join(out).rstrip() + "\n"
    if new_text == text:
        return False

    if not dry_run:
        path.write_text(new_text, encoding="utf-8")
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing files.")
    args = parser.parse_args()

    changed = 0
    for path in sorted(CORE_DIR.glob("batch-*.txt")):
        if normalize_batch_file(path, dry_run=args.dry_run):
            changed += 1
            print(f"normalized: {path.name}")

    print(f"done. changed={changed}")


if __name__ == "__main__":
    main()

