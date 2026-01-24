#!/usr/bin/env python3
"""
Generate per-case core/{id}.txt for the Corporate Tax (法人税) case set.

- Source metadata: /home/user/ai-law-db/simple/hanketsu/{id}.json
- Optional summary text: extracted from existing core/batch-*.txt (if present)
- Output: core/{id}.txt (YAML frontmatter + summary/placeholder)
"""

import argparse
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
SHARDS_DIR = BASE_DIR / "data" / "shards"


CASE_HEADER_RE = re.compile(r"^##\s+(\d+):", re.MULTILINE)
SHUBUN_RE = re.compile(r"^\s*主\s*文\s*$", re.MULTILINE)
SOUTEN_RE = re.compile(r"^\s*(?:\d+\s*)?争\s*点", re.MULTILINE)
DANIN_RE = re.compile(r"当裁判所の判断")
JIKAN_RE = re.compile(r"事案の概要")

DAI_HEADING_RE = re.compile(r"^\s*第[０-９0-9一二三四五六七八九十]+", re.MULTILINE)
BETSU_RE = re.compile(r"^\s*別紙", re.MULTILINE)

SENTENCE_SPLIT_RE = re.compile(r"(?<=。)")


DECISION_PHRASES = (
    ("解するのが相当", 6),
    ("というべき", 5),
    ("認められる", 4),
    ("判断する", 4),
    ("したがって", 4),
    ("よって", 4),
    ("違法", 4),
    ("適法", 4),
    ("棄却", 3),
    ("取消", 3),
    ("理由がない", 3),
    ("本件", 1),
)


def load_case_ids() -> list[str]:
    return [line.strip() for line in LIST_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]

def load_case_ids_from_shards(shards: list[str]) -> list[str]:
    """
    Load case_ids from shard TSVs (data/shards/shard-XX.txt).
    """
    case_ids: list[str] = []

    for shard in shards:
        s = shard.strip()
        if not s:
            continue
        if s.isdigit():
            shard_file = SHARDS_DIR / f"shard-{int(s):02d}.txt"
        else:
            name = s
            if not name.endswith(".txt"):
                name += ".txt"
            shard_file = SHARDS_DIR / name

        lines = shard_file.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[1:]:
            if not line.strip():
                continue
            cid = line.split("\t", 1)[0].strip()
            if cid:
                case_ids.append(cid)

    # de-duplicate (preserve order)
    seen: set[str] = set()
    uniq: list[str] = []
    for cid in case_ids:
        if cid in seen:
            continue
        seen.add(cid)
        uniq.append(cid)
    return uniq


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


def _extract_after_phrase_next_line(text: str, phrase_re: re.Pattern, max_chars: int) -> str:
    """
    Like _extract_after_phrase, but if the phrase appears in a heading line,
    start from the next line to avoid clipped fragments (e.g., "事案の概要等").
    """
    m = phrase_re.search(text)
    if not m:
        return ""
    start = m.end()
    nl = text.find("\n", start)
    if nl != -1 and nl - start <= 40:
        start = nl + 1
    end = len(text)
    m2 = BETSU_RE.search(text, start)
    if m2:
        end = min(end, m2.start())
    return _clip(text[start:end], max_chars=max_chars)


def _normalize_for_sentences(text: str) -> str:
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _split_sentences(text: str) -> list[str]:
    t = _normalize_for_sentences(text)
    if not t:
        return []
    t = t.replace("\n", " ")
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return []
    parts = SENTENCE_SPLIT_RE.split(t)
    out: list[str] = []
    for part in parts:
        s = part.strip()
        if not s:
            continue
        out.append(s)

    # Heuristic merge: avoid splitting inside parenthetical phrases like "…という。）が…"
    merged: list[str] = []
    for s in out:
        if not merged:
            merged.append(s)
            continue
        cur = s
        prev = merged[-1]
        if cur.startswith(("）", ")", "】", "]")) and (prev.endswith("。") or "以下" in prev):
            merged[-1] = prev + cur
            continue
        if cur[:1] in ("が", "を", "に", "へ", "と", "は", "の") and prev.endswith("。") and ("という" in prev or "以下" in prev):
            merged[-1] = prev + cur
            continue
        merged.append(cur)

    return merged


def _score_sentence(sentence: str, keywords: list[str], laws: list[str]) -> int:
    score = 0
    for phrase, weight in DECISION_PHRASES:
        if phrase in sentence:
            score += weight
    for kw in keywords[:12]:
        if kw and kw in sentence:
            score += 2
    # Laws in raw text may be full-width; treat laws list as hints only.
    for law in laws[:12]:
        if not law:
            continue
        head = re.split(r"\d", law, maxsplit=1)[0]
        head = head.strip()
        if head and head in sentence:
            score += 1
    # Prefer sentences that mention条/項/号 (often legal holding)
    if "条" in sentence:
        score += 1
    if "項" in sentence:
        score += 1
    if "号" in sentence:
        score += 1
    return score


def _clean_sentence(sentence: str) -> str:
    s = sentence.strip()
    s = re.sub(r"^[)）】\]＞>]+", "", s).strip()
    s = re.sub(r"^[,、]+", "", s).strip()
    return s


def _pick_sentences(
    text: str,
    *,
    keywords: list[str],
    laws: list[str],
    max_items: int,
    max_total_chars: int,
    min_chars: int = 25,
    max_chars: int = 220,
) -> list[str]:
    sents = _split_sentences(text)
    if not sents:
        return []

    scored: list[tuple[int, int, str]] = []
    for idx, s in enumerate(sents):
        s2 = _clean_sentence(s)
        if len(s2) < min_chars:
            continue
        if len(s2) > max_chars * 2:
            s2 = _clip(s2, max_chars)
        scored.append((_score_sentence(s2, keywords, laws), idx, s2))

    if not scored:
        return []

    scored.sort(key=lambda x: (x[0], len(x[2])), reverse=True)
    picked: list[tuple[int, str]] = []
    total = 0
    seen_prefix: set[str] = set()

    for score, idx, s in scored:
        if score <= 0:
            continue
        if total + len(s) > max_total_chars:
            continue
        prefix = re.sub(r"\s+", "", s)[:40]
        if prefix and prefix in seen_prefix:
            continue
        seen_prefix.add(prefix)
        picked.append((idx, s))
        total += len(s)
        if len(picked) >= max_items:
            break

    if not picked:
        # fallback: take first N sentences
        out: list[str] = []
        total = 0
        for s in sents:
            s2 = s.strip()
            if len(s2) < min_chars:
                continue
            if len(s2) > max_chars * 2:
                s2 = _clip(s2, max_chars)
            if total + len(s2) > max_total_chars:
                break
            out.append(s2)
            total += len(s2)
            if len(out) >= max_items:
                break
        return out

    picked.sort(key=lambda x: x[0])
    return [s for _, s in picked]


def _pick_first_sentences(
    text: str,
    *,
    max_items: int,
    max_total_chars: int,
    min_chars: int = 25,
    max_chars: int = 220,
) -> list[str]:
    sents = _split_sentences(text)
    out: list[str] = []
    total = 0
    for s in sents:
        s2 = _clean_sentence(s)
        if len(s2) < min_chars:
            continue
        if len(s2) > max_chars * 2:
            s2 = _clip(s2, max_chars)
        if total + len(s2) > max_total_chars:
            break
        out.append(s2)
        total += len(s2)
        if len(out) >= max_items:
            break
    return out


def build_auto_segment(case_id: str, meta: dict) -> tuple[str, str] | tuple[None, None]:
    # Some datasets store the judgment body in `full_text` instead of `text`.
    raw = str(meta.get("text") or meta.get("full_text") or "")
    title = str(meta.get("title") or "").strip()
    court = str(meta.get("court") or "").strip()
    date = str(meta.get("date_iso") or meta.get("date") or "").strip()
    result = str(meta.get("result") or "").strip()

    if not raw.strip():
        topics = meta.get("topics") or []
        keywords = meta.get("keywords") or []
        laws = meta.get("laws") or []

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

    topics = meta.get("topics") or []
    keywords = meta.get("keywords") or []
    laws = meta.get("laws") or []

    shubun = _extract_section(raw, SHUBUN_RE, max_chars=1500)
    jikan = _extract_after_phrase_next_line(raw, JIKAN_RE, max_chars=9000)
    souten = _extract_section(raw, SOUTEN_RE, max_chars=9000)
    handan = _extract_after_phrase_next_line(raw, DANIN_RE, max_chars=22000)

    points: list[str] = []
    if result:
        points.append(f"- 結果: {result}")
    if shubun:
        points.append(f"- 主文: {_first_sentence(shubun)}")

    # Pick key sentences (structured, but still extractive)
    handan_points = _pick_sentences(
        handan or raw,
        keywords=keywords,
        laws=laws,
        max_items=10,
        max_total_chars=1600,
        min_chars=28,
        max_chars=240,
    )
    jikan_points = _pick_first_sentences(
        jikan or raw,
        max_items=6,
        max_total_chars=900,
        min_chars=28,
        max_chars=220,
    )
    souten_points = _pick_sentences(
        souten or raw,
        keywords=keywords,
        laws=laws,
        max_items=6,
        max_total_chars=900,
        min_chars=24,
        max_chars=220,
    )
    quotes = _pick_sentences(
        handan or raw,
        keywords=keywords,
        laws=laws,
        max_items=6,
        max_total_chars=1400,
        min_chars=30,
        max_chars=260,
    )

    lines: list[str] = []
    header = f"## {case_id}: {title}".rstrip()
    if court or date:
        suffix = " ".join([x for x in [court, date] if x]).strip()
        if suffix:
            header += f"（{suffix}）"

    lines.append(header)
    lines.append("")
    lines.append("### 結論（先出し）")
    if points:
        lines.extend(points)
    else:
        lines.append("- （結論抽出に失敗）")

    lines.append("")
    lines.append("### 何が争われたか（争点の手掛かり）")
    if keywords:
        lines.append(f"- keywords: {', '.join(keywords[:12])}")
    if laws:
        lines.append(f"- laws: {', '.join(laws[:12])}")
    if souten_points:
        lines.append("- 争点（抜粋）:")
        for s in souten_points:
            lines.append(f"  - {s}")

    lines.append("")
    lines.append("### 裁判所の判断（要旨・抜粋）")
    if handan_points:
        for s in handan_points:
            lines.append(f"- {s}")
    else:
        lines.append("- （判断部分の抽出に失敗）")

    if quotes:
        lines.append("")
        lines.append("### 重要判示（引用）")
        for q in quotes[:5]:
            lines.append(f"> {q}")

    if jikan_points:
        lines.append("")
        lines.append("### 事案の概要（要旨）")
        for s in jikan_points:
            lines.append(f"- {s}")

    lines.append("")
    lines.append("（注）本ファイルはAI検索用に、判決文から要点・抜粋を機械的に抽出・整形したものです。厳密な引用・要件判定は原文で確認してください。")

    return "\n".join(lines).strip(), "auto_structured_v2"


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

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--shards",
        nargs="*",
        help="Regenerate only specified shards (e.g. 06 07 or shard-06 shard-07).",
    )
    args = parser.parse_args()

    segments, sources = extract_segments_from_batches()
    case_ids = load_case_ids_from_shards(args.shards) if args.shards else load_case_ids()

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
