#!/usr/bin/env python3
"""
Build AI-optimized artifacts for:
  OECD Transfer Pricing Guidelines 2022 (Japanese translated PDF text)

Mobile-AI constraint:
  The client can read only the first ~10k tokens per request.

Therefore:
- Provide small, deterministic artifacts.
- Search flow: shards_index.json -> shard TSV -> core/{id}.txt
- Canonical core files are small; very long paragraphs are split into parts.

Input:
- source/2022translated.txt
  - Must include page markers: "===== Page N ====="

Outputs (under this dataset directory):
- quickstart.txt
- data/shards_index.json
- data/shards/shard-XX.txt (TSV)
- core/*.txt (YAML-ish header + text)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from pathlib import Path
from typing import Iterable


BASE_URL = "https://jplawdb.github.io/html-preview/ai-paper-db/oecd-tpg-2022"

PAGE_MARKER_RE = re.compile(r"^===== Page (\d+) =====$")

# Paragraph IDs like: 1.1 / 2.2.2 / 10.1. (allow fullwidth dot, allow trailing dot)
PARA_START_RE = re.compile(r"^(?P<pid>\d+(?:[\.．]\d+)+)[\.．]?\s+(?P<rest>.*)$")

# Headings for context (not part of paragraph body).
CHAPTER_APPENDIX_RE = re.compile(r"^第\s*([0-9０-９]+)\s*章別添\s*([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩI]+)\s+(.+)$")
CHAPTER_HEADING_RE = re.compile(r"^第\s*([0-9０-９]+)\s*章\s+(.+)$")
SUBSECTION_HEADING_RE = re.compile(r"^(?P<label>[A-ZＡ-Ｚ]\.\d+(?:\.\d+)*)\.?\s+(?P<title>.+)$")
SECTION_HEADING_RE = re.compile(r"^(?P<label>[A-ZＡ-Ｚ])\s+(?P<title>.+)$")

NOISE_LINES = {
    "参考仮訳",
    "仮 訳",
    "仮訳",
    "（本資料は参考仮訳であるため、正確には原文を参照されたい。）",
}

# Conservative size guardrails for the mobile-AI token limit.
MAX_CORE_CHARS = 9000
SHARD_SIZE = 80  # rows per shard (chapter-local); keep files under ~10k tokens/read
SNIPPET_CHARS = 70

LATIN_TERM_RE = re.compile(r"\b[A-Z][A-Z0-9-]{2,}\b")
# Keep each keyword-index shard small for the 10k tokens/read constraint.
MAX_TERM_SHARD_CHARS = 9000
TERM_CONTEXT_RADIUS = 60  # characters on each side around the first match


_FULLWIDTH_DIGITS = str.maketrans(
    {
        "０": "0",
        "１": "1",
        "２": "2",
        "３": "3",
        "４": "4",
        "５": "5",
        "６": "6",
        "７": "7",
        "８": "8",
        "９": "9",
    }
)

_ROMAN_MAP = {
    "Ⅰ": 1,
    "Ⅱ": 2,
    "Ⅲ": 3,
    "Ⅳ": 4,
    "Ⅴ": 5,
    "Ⅵ": 6,
    "Ⅶ": 7,
    "Ⅷ": 8,
    "Ⅸ": 9,
    "Ⅹ": 10,
    "I": 1,
    "II": 2,
    "III": 3,
    "IV": 4,
    "V": 5,
    "VI": 6,
    "VII": 7,
    "VIII": 8,
    "IX": 9,
    "X": 10,
}

_LATIN_UPPER = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_LATIN_UPPER_FULLWIDTH = set("ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ")


def looks_like_real_chapter_title(title: str) -> bool:
    """
    Avoid false positives such as:
      "第1章 D.1.3 参照..."
    which can appear at the start of a line due to PDF line wrapping.
    """
    t = (title or "").lstrip()
    if not t:
        return False
    first = t[0]
    if first in _LATIN_UPPER or first in _LATIN_UPPER_FULLWIDTH:
        return False
    if first.isdigit():
        return False
    return True


def normalize_digits(s: str) -> str:
    return (s or "").translate(_FULLWIDTH_DIGITS)


def clean_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def section_id_from_chapter(ch: int) -> str:
    return f"ch{ch:02d}"


def section_id_from_appendix(ch: int, app_num: int) -> str:
    return f"ch{ch:02d}-app{app_num:02d}"


@dataclass(frozen=True)
class Para:
    id: str  # unique file id (page-prefixed)
    pid: str  # original paragraph id (e.g. 6.63)
    page_start: int
    page_end: int
    section_id: str
    section_title: str
    subsection: str
    text: str


def iter_paragraphs(lines: list[str]) -> Iterable[Para]:
    current_page: int | None = None
    page_line_index = 0  # counts non-noise lines since the last page marker
    current_section_id = "unknown"
    current_section_title = ""
    current_subsection = ""

    cur_pid: str | None = None
    cur_id: str | None = None
    cur_page_start: int | None = None
    cur_page_end: int | None = None
    buf: list[str] = []

    def flush() -> Para | None:
        nonlocal cur_pid, cur_id, cur_page_start, cur_page_end, buf
        if cur_pid is None or cur_id is None or cur_page_start is None or cur_page_end is None:
            return None
        text = "\n".join(buf).strip()
        out = Para(
            id=cur_id,
            pid=cur_pid,
            page_start=cur_page_start,
            page_end=cur_page_end,
            section_id=current_section_id,
            section_title=current_section_title,
            subsection=current_subsection,
            text=text,
        )
        cur_pid = None
        cur_id = None
        cur_page_start = None
        cur_page_end = None
        buf = []
        return out

    for raw in lines:
        s = raw.strip()

        m = PAGE_MARKER_RE.match(s)
        if m:
            current_page = int(m.group(1))
            page_line_index = 0
            if cur_pid is not None:
                cur_page_end = current_page
            continue

        if not s:
            continue

        # Skip repeated noise / boilerplate.
        if s in NOISE_LINES:
            continue

        # Skip bare page numbers.
        if s.isdigit() and len(s) <= 4:
            continue

        # Detect chapter headings (both at top-of-page and mid-page).
        # This is needed because some chapter headings can appear mid-page.
        m_app = CHAPTER_APPENDIX_RE.match(s)
        if m_app and looks_like_real_chapter_title(m_app.group(3)):
            flushed = flush()
            if flushed is not None:
                yield flushed
            ch = int(normalize_digits(m_app.group(1)))
            roman = m_app.group(2).strip()
            title = m_app.group(3)
            app_num = _ROMAN_MAP.get(roman, 0)
            current_section_id = section_id_from_appendix(ch, int(app_num))
            current_section_title = clean_ws(f"第{ch}章別添{roman} {title}")
            current_subsection = ""
            page_line_index += 1
            continue

        m_ch = CHAPTER_HEADING_RE.match(s)
        if m_ch and looks_like_real_chapter_title(m_ch.group(2)):
            flushed = flush()
            if flushed is not None:
                yield flushed
            ch = int(normalize_digits(m_ch.group(1)))
            title = m_ch.group(2)
            current_section_id = section_id_from_chapter(ch)
            current_section_title = clean_ws(f"第{ch}章 {title}")
            current_subsection = ""
            page_line_index += 1
            continue

        is_page_top = page_line_index <= 5

        # Track headings for context (outside paragraphs only).
        if cur_pid is None and is_page_top:
            m_sub = SUBSECTION_HEADING_RE.match(s)
            if m_sub:
                current_subsection = clean_ws(f"{m_sub.group('label')} {m_sub.group('title')}")
                page_line_index += 1
                continue

            m_sec = SECTION_HEADING_RE.match(s)
            if m_sec:
                current_subsection = clean_ws(f"{m_sec.group('label')} {m_sec.group('title')}")
                page_line_index += 1
                continue

        # Paragraph start.
        m_p = PARA_START_RE.match(s)
        if m_p:
            pid = normalize_digits(m_p.group("pid")).replace("．", ".")
            rest = m_p.group("rest").lstrip()

            # Avoid false-positive line-wrapped references like: "2.47 参照）".
            if rest.startswith("参照"):
                if cur_pid is not None:
                    buf.append(s)
                continue

            if current_page is None:
                current_page = 0

            flushed = flush()
            if flushed is not None:
                yield flushed

            cur_pid = pid
            cur_page_start = current_page
            cur_page_end = current_page
            cur_id = f"p{current_page:03d}-{pid}"
            buf = [s]
            page_line_index += 1
            continue

        # Inside paragraph.
        if cur_pid is not None:
            buf.append(s)
            page_line_index += 1
            continue

        # Non-paragraph, non-heading content.
        page_line_index += 1

    flushed = flush()
    if flushed is not None:
        yield flushed


def split_text_parts(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n", 0, max_chars)
        if cut < int(max_chars * 0.6):
            cut = max_chars
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return parts


def yaml_like(d: dict) -> str:
    # Minimal YAML-ish emitter (no dependency; stable enough for our use).
    lines: list[str] = []
    for k, v in d.items():
        if v is None:
            lines.append(f"{k}: null")
            continue
        if isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
            continue
        if isinstance(v, int):
            lines.append(f"{k}: {v}")
            continue
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                s = str(item).replace('"', '\\"')
                lines.append(f'  - "{s}"')
            continue
        s = str(v).replace('"', '\\"')
        lines.append(f'{k}: "{s}"')
    return "\n".join(lines) + "\n"


def write_core_files(root: Path, paras: list[Para]) -> dict[str, dict]:
    out_dir = root / "core"
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, dict] = {}

    for p in paras:
        # If the parsing logic changes between runs, a paragraph that used to be
        # split into parts may become short enough to be stored as a single file
        # (or vice versa). Remove any previously generated part files first to
        # avoid leaving stale artifacts around.
        for old in out_dir.glob(f"{p.id}.part-*.txt"):
            try:
                old.unlink()
            except FileNotFoundError:
                pass

        core_index_path = out_dir / f"{p.id}.txt"
        parts = split_text_parts(p.text, MAX_CORE_CHARS)

        if len(parts) == 1:
            header = {
                "id": p.id,
                "pid": p.pid,
                "page_start": p.page_start,
                "page_end": p.page_end,
                "section_id": p.section_id,
                "section_title": p.section_title,
                "subsection": p.subsection,
                "source": "2022translated.pdf",
                "url": f"{BASE_URL}/core/{p.id}.txt",
            }
            core_index_path.write_text(
                "---\n" + yaml_like(header) + "---\n\n" + p.text + "\n",
                encoding="utf-8",
                errors="replace",
            )
            manifest[p.id] = {"core": f"core/{p.id}.txt", "parts": None}
            continue

        # Long paragraph: write an index + part files.
        part_paths: list[str] = []
        for idx, part_text in enumerate(parts, start=1):
            part_id = f"{p.id}.part-{idx:02d}"
            part_path = out_dir / f"{part_id}.txt"
            header = {
                "id": part_id,
                "pid": p.pid,
                "page_start": p.page_start,
                "page_end": p.page_end,
                "section_id": p.section_id,
                "section_title": p.section_title,
                "subsection": p.subsection,
                "source": "2022translated.pdf",
                "part_index": idx,
                "part_total": len(parts),
                "url": f"{BASE_URL}/core/{part_id}.txt",
            }
            part_path.write_text(
                "---\n" + yaml_like(header) + "---\n\n" + part_text + "\n",
                encoding="utf-8",
                errors="replace",
            )
            part_paths.append(f"core/{part_id}.txt")

        index_header = {
            "id": p.id,
            "pid": p.pid,
            "page_start": p.page_start,
            "page_end": p.page_end,
            "section_id": p.section_id,
            "section_title": p.section_title,
            "subsection": p.subsection,
            "source": "2022translated.pdf",
            "note": "Split into parts due to the 10k tokens/read constraint.",
            "parts": part_paths,
            "url": f"{BASE_URL}/core/{p.id}.txt",
        }
        preview = clean_ws(p.text)[:300]
        core_index_path.write_text(
            "---\n" + yaml_like(index_header) + "---\n\n" + preview + "\n",
            encoding="utf-8",
            errors="replace",
        )
        manifest[p.id] = {"core": f"core/{p.id}.txt", "parts": part_paths}

    return manifest


def build_shards(root: Path, paras: list[Para], manifest: dict[str, dict]) -> dict:
    data_dir = root / "data"
    shards_dir = data_dir / "shards"
    shards_dir.mkdir(parents=True, exist_ok=True)

    # Remove old shard files so re-runs don't leave stale shards behind if the
    # shard sizing logic changes.
    for old in shards_dir.glob("shard-*.txt"):
        try:
            old.unlink()
        except FileNotFoundError:
            pass

    # Keep shard rows compact so each shard file stays under the ~10k tokens/read
    # constraint. Section/subsection text is available in core/{id}.txt and the
    # shard already corresponds to a single section via shards_index.json.
    fields = [
        "id",
        "pid",
        "page_start",
        "page_end",
        "snippet",
        "core",
    ]

    # Group by section_id so a user can jump directly to the relevant chapter.
    section_groups: dict[str, list[Para]] = {}
    section_titles: dict[str, str] = {}
    for p in paras:
        section_groups.setdefault(p.section_id, []).append(p)
        if p.section_title:
            section_titles.setdefault(p.section_id, p.section_title)

    def section_sort_key(sid: str):
        m = re.match(r"^ch(\d+)(?:-app(\d+))?$", sid)
        if not m:
            return (9_999, 9_999, sid)
        ch = int(m.group(1))
        app = int(m.group(2)) if m.group(2) else 0
        return (ch, app, sid)

    sections_index = []
    shards_flat = []
    shard_counter = 0

    for section_id in sorted(section_groups.keys(), key=section_sort_key):
        items = section_groups[section_id]  # already in document order
        title = section_titles.get(section_id, "")
        section_shards = []

        for i in range(0, len(items), SHARD_SIZE):
            shard_items = items[i : i + SHARD_SIZE]
            shard_file = f"shard-{shard_counter:02d}.txt"
            shard_path = shards_dir / shard_file

            rows = ["\t".join(fields)]
            for p in shard_items:
                snippet = clean_ws(p.text)
                if snippet.startswith(p.pid):
                    snippet = snippet[len(p.pid) :].lstrip(" .．")
                snippet = snippet[:SNIPPET_CHARS]
                core_rel = manifest.get(p.id, {}).get("core", f"core/{p.id}.txt")

                rows.append(
                    "\t".join(
                        [
                            p.id,
                            p.pid,
                            str(p.page_start),
                            str(p.page_end),
                            snippet,
                            core_rel,
                        ]
                    )
                )

            shard_path.write_text("\n".join(rows) + "\n", encoding="utf-8", errors="replace")

            meta = {
                "file": f"data/shards/{shard_file}",
                "count": len(shard_items),
                "section_id": section_id,
                "section_title": title,
                "page_start": shard_items[0].page_start,
                "page_end": shard_items[-1].page_end,
                "pid_start": shard_items[0].pid,
                "pid_end": shard_items[-1].pid,
            }
            shards_flat.append(meta)
            section_shards.append(meta)
            shard_counter += 1

        sections_index.append(
            {
                "id": section_id,
                "title": title,
                "count": len(items),
                "shards": [
                    {"file": s["file"], "count": s["count"], "pid_start": s["pid_start"], "pid_end": s["pid_end"]}
                    for s in section_shards
                ],
            }
        )

    index = {
        "dataset": {
            "name": "OECD TP Guidelines 2022 (JP translated)",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "base_url": BASE_URL,
        },
        "format": "tsv",
        "fields": fields,
        "shard_size": SHARD_SIZE,
        "sections": sections_index,
        "shards": shards_flat,
    }

    (data_dir / "shards_index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return index


def safe_slug(s: str) -> str:
    # Stable filename slug (avoid punctuation like "I-P" -> "I_P").
    return re.sub(r"[^A-Za-z0-9]+", "_", (s or "").strip()).strip("_") or "term"


def extract_term_context(text: str, term: str) -> str:
    t = clean_ws(text)
    i = t.find(term)
    if i < 0:
        return t[: (TERM_CONTEXT_RADIUS * 2 + len(term) + 10)]
    start = max(0, i - TERM_CONTEXT_RADIUS)
    end = min(len(t), i + len(term) + TERM_CONTEXT_RADIUS)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(t) else ""
    return prefix + t[start:end] + suffix


def build_latin_terms_index(root: Path, paras: list[Para], manifest: dict[str, dict]) -> dict:
    """
    Reverse index for uppercase latin terms (e.g., HTVI/CCA/APA/CUP).

    This is primarily to address the "cross-chapter keyword search" workflow without
    having to open all shard files, while still respecting the 10k tokens/read cap.
    """
    data_dir = root / "data"
    out_dir = data_dir / "latin_terms"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Clean previous generated shards to avoid leaving stale files behind.
    for old in out_dir.glob("*.tsv"):
        try:
            old.unlink()
        except FileNotFoundError:
            pass

    fields = [
        "id",
        "pid",
        "page_start",
        "page_end",
        "section_id",
        "context",
        "core",
    ]

    # Collect per-paragraph terms (dedupe within a paragraph).
    hits: dict[str, list[dict]] = {}
    for p in paras:
        terms = set(LATIN_TERM_RE.findall(p.text))
        if not terms:
            continue
        for term in terms:
            core_rel = manifest.get(p.id, {}).get("core", f"core/{p.id}.txt")
            hits.setdefault(term, []).append(
                {
                    "id": p.id,
                    "pid": p.pid,
                    "page_start": p.page_start,
                    "page_end": p.page_end,
                    "section_id": p.section_id,
                    "context": extract_term_context(p.text, term),
                    "core": core_rel,
                }
            )

    # Write term shards (TSV), keeping each file under MAX_TERM_SHARD_CHARS.
    terms_index = []
    for term in sorted(hits.keys()):
        slug = safe_slug(term)
        rows = hits[term]  # already in document order via paras iteration

        shard_files: list[str] = []
        shard_idx = 0
        buf_lines: list[str] = ["\t".join(fields)]

        def flush_shard() -> None:
            nonlocal shard_idx, buf_lines
            if len(buf_lines) <= 1:
                return
            shard_file = f"{slug}-{shard_idx:02d}.tsv"
            (out_dir / shard_file).write_text("\n".join(buf_lines) + "\n", encoding="utf-8", errors="replace")
            shard_files.append(f"data/latin_terms/{shard_file}")
            shard_idx += 1
            buf_lines = ["\t".join(fields)]

        for r in rows:
            line = "\t".join(
                [
                    str(r["id"]),
                    str(r["pid"]),
                    str(r["page_start"]),
                    str(r["page_end"]),
                    str(r["section_id"]),
                    str(r["context"]).replace("\t", " "),
                    str(r["core"]),
                ]
            )
            # If adding this line would exceed the per-file size budget, flush first.
            projected = sum(len(x) + 1 for x in buf_lines) + len(line) + 1
            if projected > MAX_TERM_SHARD_CHARS and len(buf_lines) > 1:
                flush_shard()
            buf_lines.append(line)

        flush_shard()

        terms_index.append(
            {
                "term": term,
                "slug": slug,
                "para_count": len(rows),
                "files": shard_files,
            }
        )

    index = {
        "dataset": {
            "name": "OECD TP Guidelines 2022 (JP translated)",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "base_url": BASE_URL,
        },
        "note": "Uppercase latin keyword reverse index (paragraph-level).",
        "term_pattern": LATIN_TERM_RE.pattern,
        "max_term_shard_chars": MAX_TERM_SHARD_CHARS,
        "fields": fields,
        "terms": terms_index,
    }

    (out_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return index


def write_quickstart(root: Path) -> None:
    qs_path = root / "quickstart.txt"
    text = f"""# OECD 移転価格ガイドライン 2022（参考仮訳） - Quickstart

前提（重要）
- あなたのAIは「1回のReadで最初の1万トークンのみ」読める。
- したがって、このDBは『索引→shard→本文』の3段階で最短到達する。

入口（迷ったら）
- `data/topics.txt`（テーマ別索引: DEMPE/無形資産/役務提供/リスク等）

推奨フロー（最小Read）
1) `data/shards_index.json` を読む（章=section と shard の対応を得る）
2) 関連する `data/shards/*.txt` を1〜3個読む（TSVを検索）
3) 行にある `core/...` を開いて本文を読む

追加: 英字キーワード（HTVI/CCA/APA/CUP等）で横断検索したい場合
1) `data/latin_terms/index.json` を読む
2) 該当する `data/latin_terms/*.tsv` を読む（該当パラグラフ一覧）
3) 行にある `core/...` を開いて本文を読む

章（目安）
- 第1章: 独立企業原則（ALP）
- 第2章: 移転価格算定手法（CUP/RP/CP/TNMM/利益分割）
- 第3章: 比較可能性分析
- 第4章: 紛争回避・解決（MAP/APA等）
- 第5章: 文書化（Master/Local/CbCR）
- 第6章: 無形資産（DEMPE等）
- 第7章: グループ内役務提供（Benefit test 等）
- 第8章: 費用分担契約（CCA）
- 第9章: 事業再編
- 第10章: 金融取引

URLパターン
- shard index: `{BASE_URL}/data/shards_index.json`
- shard TSV: `{BASE_URL}/data/shards/shard-XX.txt`
- 本文: `{BASE_URL}/core/{{id}}.txt`
- 英字キーワード索引: `{BASE_URL}/data/latin_terms/index.json`
"""
    qs_path.write_text(text, encoding="utf-8")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    src = root / "source" / "2022translated.txt"
    if not src.exists():
        raise SystemExit(f"Missing input: {src}")

    lines = src.read_text(encoding="utf-8", errors="replace").splitlines()
    paras = list(iter_paragraphs(lines))

    manifest = write_core_files(root, paras)
    build_shards(root, paras, manifest)
    build_latin_terms_index(root, paras, manifest)
    write_quickstart(root)

    print(f"paras: {len(paras)}")
    print(f"core_dir: {root / 'core'}")
    print(f"shards_dir: {root / 'data' / 'shards'}")
    print(f"index: {root / 'data' / 'shards_index.json'}")
    print(f"quickstart: {root / 'quickstart.txt'}")


if __name__ == "__main__":
    main()
