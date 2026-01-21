#!/usr/bin/env python3
"""
Generate per-case core/{id}.txt for the Corporate Tax (法人税) case set.

- Source metadata: /home/user/ai-law-db/simple/hanketsu/{id}.json
- Optional summary text: extracted from existing core/batch-*.txt (if present)
- Output: core/{id}.txt (YAML frontmatter + summary/placeholder)
"""

import json
import re
import subprocess
from pathlib import Path

import yaml


SOURCE_DIR = Path("/home/user/ai-law-db/simple/hanketsu")
LIST_FILE = Path("/tmp/houjinzei_list.txt")

BASE_DIR = Path(__file__).parent.parent
CORE_DIR = BASE_DIR / "core"
REPO_ROOT = BASE_DIR.parent.parent


CASE_HEADER_RE = re.compile(r"^##\s+(\d+):", re.MULTILINE)
SHUBUN_RE = re.compile(r"^\s*主\s*文\s*$", re.MULTILINE)
SOUTEN_RE = re.compile(r"^\s*(?:\d+\s*)?争\s*点", re.MULTILINE)
DANIN_RE = re.compile(r"当裁判所の判断")
JIKAN_RE = re.compile(r"事案の概要")

DAI_HEADING_RE = re.compile(r"^\s*第[０-９0-9一二三四五六七八九十]+", re.MULTILINE)
BETSU_RE = re.compile(r"^\s*別紙", re.MULTILINE)


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

    tracked_batches = load_tracked_batch_paths()
    for batch_path in sorted(CORE_DIR.glob("batch-*.txt")):
        if tracked_batches is not None and batch_path not in tracked_batches:
            continue
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


def load_tracked_batch_paths() -> set[Path] | None:
    """
    Returns a set of tracked-or-staged batch files (absolute Paths).

    Rationale: during iterative authoring, it's common to have untracked draft batch files in CORE_DIR.
    Those should not affect the generated per-case core/*.txt outputs unless they are explicitly added
    to git (tracked or staged).
    """
    try:
        rel_pathspec = str((CORE_DIR / "batch-*.txt").relative_to(REPO_ROOT))
    except ValueError:
        return None

    try:
        proc = subprocess.run(
            ["git", "ls-files", "--", rel_pathspec],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None

    if proc.returncode != 0:
        return None

    paths: set[Path] = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        paths.add(REPO_ROOT / line)
    return paths


def load_metadata(case_id: str) -> dict:
    json_path = SOURCE_DIR / f"{case_id}.json"
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected JSON root for {case_id}: {type(data)}")
    return data


def _clip(text: str, max_chars: int) -> str:
    s = text.strip()
    if not s:
        return ""
    if len(s) <= max_chars:
        return s
    cut = s[:max_chars]
    nl = cut.rfind("\n")
    if nl >= max_chars * 0.6:
        cut = cut[:nl]
    return cut.rstrip() + "…"


def _first_sentence(text: str, max_chars: int = 180) -> str:
    s = " ".join(text.split())
    if not s:
        return ""
    end = s.find("。")
    if 0 < end < max_chars:
        return s[: end + 1]
    return (s[:max_chars] + "…") if len(s) > max_chars else s


def _extract_section(text: str, start_re: re.Pattern, max_chars: int) -> str:
    m = start_re.search(text)
    if not m:
        return ""
    start = m.end()

    next_candidates: list[int] = []
    for heading_re in (DAI_HEADING_RE, BETSU_RE, SHUBUN_RE, SOUTEN_RE):
        m2 = heading_re.search(text, start)
        if m2:
            next_candidates.append(m2.start())
    end = min(next_candidates) if next_candidates else len(text)
    return _clip(text[start:end], max_chars=max_chars)


def _extract_after_phrase(text: str, phrase_re: re.Pattern, max_chars: int) -> str:
    m = phrase_re.search(text)
    if not m:
        return ""
    start = m.end()
    end = len(text)
    m2 = BETSU_RE.search(text, start)
    if m2:
        end = min(end, m2.start())
    return _clip(text[start:end], max_chars=max_chars)


def build_auto_segment(case_id: str, meta: dict) -> tuple[str, str] | tuple[None, None]:
    raw = str(meta.get("text") or "")
    title = str(meta.get("title") or "").strip()
    court = str(meta.get("court") or "").strip()
    date = str(meta.get("date_iso") or meta.get("date") or "").strip()

    if not raw.strip():
        topics = meta.get("topics") or []
        keywords = meta.get("keywords") or []
        laws = meta.get("laws") or []
        result = str(meta.get("result") or "").strip()

        lines: list[str] = []
        header = f"## {case_id}: {title}".rstrip()
        suffix = " ".join([x for x in [court, date] if x]).strip()
        if suffix:
            header += f"（{suffix}）"
        lines.append(header)
        lines.append("")
        lines.append("### 要点（メタデータのみ）")
        lines.append(f"- topics: {', '.join(topics) if topics else ''}")
        lines.append(f"- keywords: {', '.join(keywords[:10]) if keywords else ''}")
        lines.append(f"- laws: {', '.join(laws[:10]) if laws else ''}")
        lines.append(f"- result: {result}")
        lines.append("")
        lines.append("（注）原文テキストがデータソースに未収録のため、本文抜粋はありません。")
        return "\n".join(lines).strip(), "meta_only_v1"

    shubun = _extract_section(raw, SHUBUN_RE, max_chars=800)
    jikan = _extract_after_phrase(raw, JIKAN_RE, max_chars=1800)
    souten = _extract_section(raw, SOUTEN_RE, max_chars=1200)
    handan = _extract_after_phrase(raw, DANIN_RE, max_chars=4200)

    points: list[str] = []
    if shubun:
        points.append(f"- 主文: {_first_sentence(shubun)}")
    if souten:
        points.append(f"- 争点: {_first_sentence(souten)}")
    if handan:
        points.append(f"- 判断: {_first_sentence(handan)}")

    lines: list[str] = []
    header = f"## {case_id}: {title}".rstrip()
    if court or date:
        suffix = " ".join([x for x in [court, date] if x]).strip()
        if suffix:
            header += f"（{suffix}）"

    lines.append(header)
    lines.append("")
    lines.append("### 要点（自動抽出・要約補助）")
    if points:
        lines.extend(points)
    else:
        lines.append("- （要点抽出に失敗）")

    lines.append("")
    if shubun:
        lines.append("### 主文（抜粋）")
        lines.append(shubun)
        lines.append("")
    if jikan:
        lines.append("### 事案の概要（抜粋）")
        lines.append(jikan)
        lines.append("")
    if souten:
        lines.append("### 争点（抜粋）")
        lines.append(souten)
        lines.append("")
    if handan:
        lines.append("### 当裁判所の判断（抜粋）")
        lines.append(handan)
        lines.append("")

    lines.append("（注）本ファイルはAI検索用に、判決文から要点・抜粋を機械的に切り出したものです。厳密な引用・要件判定は原文で確認してください。")

    return "\n".join(lines).strip(), "auto_extract_v1"


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
        if not segment:
            segment, segment_source = build_auto_segment(case_id, meta)
        write_core_file(case_id, meta, segment, segment_source)
        ok += 1

    print(f"Generated core files: {ok} / {len(case_ids)}")
    if missing_meta:
        print(f"Missing metadata JSON: {missing_meta}")


if __name__ == "__main__":
    main()
