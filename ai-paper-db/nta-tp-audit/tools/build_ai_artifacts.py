#!/usr/bin/env python3
"""
Build AI-optimized artifacts for:
  National Tax Agency (Japan) transfer-pricing audit materials

Sources (NTA "事務運営指針" page family):
  - 移転価格事務運営要領（事務運営指針）: HTML chapters (01-08.htm)
  - 別冊 移転価格税制の適用に当たっての参考事例集: bessatsu.pdf

Mobile-AI constraint:
  The client can read only the first ~10k tokens per request.

Therefore:
  Provide a small, deterministic retrieval flow:
    shards_index.json -> shard TSV -> packs/*.txt
  Canonical text lives in packs (multi-paragraph files, each <= ~10k tokens/read).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
import json
import re
from pathlib import Path

import pdfplumber


BASE_URL = "https://jplawdb.github.io/html-preview/ai-paper-db/nta-tp-audit"
NTA_BASE = "https://www.nta.go.jp/law/jimu-unei/hojin/010601"

RAW_DIR = Path(__file__).resolve().parents[1] / "source" / "raw"

# Conservative size guardrails for the mobile-AI token limit.
MAX_PACK_CHARS = 9500
SHARD_SIZE = 80
SNIPPET_CHARS = 120  # slightly longer than OECD to reduce “open core to judge” friction

LATIN_TERM_RE = re.compile(r"\b[A-Z][A-Z0-9-]{2,}\b")
MAX_TERM_SHARD_CHARS = 9000
TERM_CONTEXT_RADIUS = 70

BODY_BEGIN = '<!-- InstanceBeginEditable name="bodyArea" -->'
BODY_END = "<!-- InstanceEndEditable -->"

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


def normalize_digits(s: str) -> str:
    return (s or "").translate(_FULLWIDTH_DIGITS)


def normalize_hyphen(s: str) -> str:
    # Common hyphen variants in Japanese PDFs/HTML.
    return (
        (s or "")
        .replace("‐", "-")
        .replace("－", "-")
        .replace("−", "-")
        .replace("‑", "-")
        .replace("―", "-")
    )


def normalize_pid(s: str) -> str:
    t = normalize_hyphen(normalize_digits(s))
    t = re.sub(r"\s*-\s*", "-", t)
    return t.strip()


def clean_ws(s: str) -> str:
    t = (s or "").replace("\u3000", " ")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def extract_body_fragment(html: str) -> str:
    beg = html.find(BODY_BEGIN)
    if beg >= 0:
        beg += len(BODY_BEGIN)
        end = html.find(BODY_END, beg)
        if end >= 0:
            return html[beg:end]
    return html


def strip_tags_keep_text(s: str) -> str:
    # Keep a tiny bit of structure by turning <br> into newlines before stripping tags.
    t = re.sub(r"(?is)<br\s*/?>", "\n", s or "")
    t = re.sub(r"(?is)<[^>]+>", "", t)
    t = unescape(t)
    return clean_ws(t)


@dataclass(frozen=True)
class Para:
    id: str
    pid: str
    page_start: int
    page_end: int
    section_id: str
    section_title: str
    subsection: str
    text: str


TAG_RE = re.compile(r"(?is)<(h1|h2|p)\b([^>]*)>(.*?)</\1>")
CHAPTER_RE = re.compile(r"^第\s*([0-9０-９]+)\s*章")
PID_START_RE = re.compile(r"^(?P<pid>[0-9０-９]+\s*[\-‐－]\s*[0-9０-９]+)\s*(?P<rest>.*)$")


def iter_jimuunei_paras(raw_html: str) -> list[Para]:
    """
    Parse one NTA chapter HTML page into paragraph-like units.

    The pages are very regular:
      <h1>第N章 ...</h1>
      <h2>(...)</h2>
      <p class="indent1"><strong>N-M</strong> ...</p>
      <p class="indent2">(1) ...</p>
    """
    frag = extract_body_fragment(raw_html)

    chapter_no: int | None = None
    chapter_title = ""
    current_h2 = ""

    cur_pid: str | None = None
    cur_id: str | None = None
    buf: list[str] = []
    out: list[Para] = []

    def flush() -> None:
        nonlocal cur_pid, cur_id, buf
        if not cur_pid or not cur_id:
            cur_pid, cur_id, buf = None, None, []
            return
        out.append(
            Para(
                id=cur_id,
                pid=cur_pid,
                page_start=0,
                page_end=0,
                section_id=f"jimu-ch{chapter_no:02d}" if chapter_no is not None else "jimu-unknown",
                section_title=f"移転価格事務運営要領 {chapter_title}".strip(),
                subsection=current_h2,
                text="\n".join(buf).strip(),
            )
        )
        cur_pid, cur_id, buf = None, None, []

    for tag, attrs, inner in TAG_RE.findall(frag):
        tag = tag.lower()

        if tag == "h1":
            # Chapter boundary: close any pending paragraph under the previous heading.
            if cur_pid is not None:
                flush()
            chapter_title = strip_tags_keep_text(inner)
            m = CHAPTER_RE.match(chapter_title)
            if m:
                chapter_no = int(normalize_digits(m.group(1)))
            continue

        if tag == "h2":
            # Section boundary: close any pending paragraph so subsection attribution
            # doesn't drift when the next pid starts.
            if cur_pid is not None:
                flush()
            current_h2 = strip_tags_keep_text(inner)
            continue

        # p tag
        # Skip the “chapter list” navigation at the bottom of each page.
        if "href=\"/law/jimu-unei/hojin/010601/" in inner and "indent" not in attrs:
            continue

        text = strip_tags_keep_text(inner)
        if not text:
            continue

        m = PID_START_RE.match(text)
        if m:
            pid = normalize_pid(m.group("pid"))
            flush()
            cur_pid = pid
            cur_id = f"j{chapter_no:02d}-{pid}" if chapter_no is not None else f"jxx-{pid}"
            buf = [text]
            continue

        if cur_pid is not None:
            buf.append(text)

    flush()

    # Some chapters (e.g., 第7章) are mostly “読み替え” tables and do not use the
    # numbered paragraph style (N-M). In that case, keep the chapter as a single
    # searchable blob so the dataset remains complete.
    if not out and chapter_title:
        blob_lines: list[str] = []
        for tag, attrs, inner in TAG_RE.findall(frag):
            tag = tag.lower()
            if tag == "h2":
                h2 = strip_tags_keep_text(inner)
                if h2:
                    blob_lines.append(h2)
                continue
            if tag != "p":
                continue
            if "href=\"/law/jimu-unei/hojin/010601/" in inner and "indent" not in attrs:
                continue
            t = strip_tags_keep_text(inner)
            if t:
                blob_lines.append(t)

        blob = "\n".join(blob_lines).strip()
        if blob:
            ch = chapter_no if chapter_no is not None else 0
            out.append(
                Para(
                    id=f"j{ch:02d}-all",
                    pid=f"{ch}-all" if ch else "all",
                    page_start=0,
                    page_end=0,
                    section_id=f"jimu-ch{ch:02d}" if ch else "jimu-unknown",
                    section_title=f"移転価格事務運営要領 {chapter_title}".strip(),
                    subsection="",
                    text=blob,
                )
            )
    return out


CASE_HEADER_RE = re.compile(r"^【事例(?P<num>[0-9０-９]+)】（(?P<title>.*)$")
JP_CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十]+章")
JP_SUBHEADING_RE = re.compile(r"^\([0-9０-９]+\)")


def iter_bessatsu_cases(pdf_path: Path) -> list[Para]:
    """
    Parse the NTA casebook PDF into case-level units:
      - preface (until first 【事例N】)
      - 【事例1】...【事例31】...
    """
    section_id = "bessatsu"
    section_title = "別冊 移転価格税制の適用に当たっての参考事例集"

    current_chapter = ""
    current_subheading = ""

    preface_buf: list[str] = []
    preface_page_start = 1
    preface_page_end = 1
    started_cases = False

    cur_case_no: int | None = None
    cur_pid: str | None = None
    cur_id: str | None = None
    cur_subsection = ""
    cur_buf: list[str] = []
    cur_page_start = 0
    cur_page_end = 0

    out: list[Para] = []

    def flush_preface() -> None:
        nonlocal preface_buf
        if not preface_buf:
            return
        out.append(
            Para(
                id="bessatsu-preface",
                pid="preface",
                page_start=preface_page_start,
                page_end=preface_page_end,
                section_id=section_id,
                section_title=section_title,
                subsection="(留意事項/定義/目次)",
                text="\n".join(preface_buf).strip(),
            )
        )
        preface_buf = []

    def flush_case() -> None:
        nonlocal cur_case_no, cur_pid, cur_id, cur_subsection, cur_buf, cur_page_start, cur_page_end
        if cur_case_no is None or cur_pid is None or cur_id is None or not cur_buf:
            cur_case_no, cur_pid, cur_id, cur_subsection, cur_buf = None, None, None, "", []
            cur_page_start, cur_page_end = 0, 0
            return
        out.append(
            Para(
                id=cur_id,
                pid=cur_pid,
                page_start=cur_page_start,
                page_end=cur_page_end,
                section_id=section_id,
                section_title=section_title,
                subsection=cur_subsection,
                text="\n".join(cur_buf).strip(),
            )
        )
        cur_case_no, cur_pid, cur_id, cur_subsection, cur_buf = None, None, None, "", []
        cur_page_start, cur_page_end = 0, 0

    with pdfplumber.open(pdf_path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if not lines:
                continue

            for ln in lines:
                # Drop footer page numbers (usually a bare integer).
                if ln.isdigit() and len(ln) <= 3:
                    continue

                if JP_CHAPTER_RE.match(ln):
                    current_chapter = clean_ws(ln)
                    continue
                if JP_SUBHEADING_RE.match(ln):
                    current_subheading = clean_ws(ln)
                    continue

                m = CASE_HEADER_RE.match(ln)
                if m:
                    n = int(normalize_digits(m.group("num")))
                    # Some pages repeat the same case header (continuation). Treat it
                    # as part of the current case instead of starting a new one.
                    if started_cases and cur_case_no is not None and n == cur_case_no:
                        cur_buf.append(ln)
                        cur_page_end = page_no
                        continue

                    if not started_cases:
                        preface_page_end = page_no
                        flush_preface()
                        started_cases = True
                    else:
                        flush_case()

                    cur_case_no = n
                    cur_pid = f"事例{n}"
                    cur_id = f"b-ex{n:02d}"
                    cur_page_start = page_no
                    cur_page_end = page_no

                    parts = []
                    if current_chapter:
                        parts.append(current_chapter)
                    if current_subheading:
                        parts.append(current_subheading)
                    cur_subsection = " / ".join(parts)

                    cur_buf = []
                    if current_chapter:
                        cur_buf.append(current_chapter)
                    if current_subheading:
                        cur_buf.append(current_subheading)
                    cur_buf.append(ln)
                    continue

                if started_cases and cur_case_no is not None:
                    cur_buf.append(ln)
                    cur_page_end = page_no
                else:
                    preface_buf.append(ln)
                    preface_page_end = page_no

    if started_cases:
        flush_case()
    else:
        flush_preface()
    return out


def strip_pid_prefix(text: str, pid: str) -> str:
    """
    Packs include an explicit header per paragraph/case, so remove repeated pid
    prefix to save space (only for obvious numeric pids like 1-1 / 3-12).
    """
    t = (text or "").lstrip()
    if not t:
        return t
    if not re.match(r"^[0-9]+-[0-9]+$", pid):
        return t
    pat = re.compile(rf"^{re.escape(pid)}\s+")
    return pat.sub("", t, count=1)


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
                s = str(item).replace('\"', '\\\\\"')
                lines.append(f'  - \"{s}\"')
            continue
        s = str(v).replace('\"', '\\\\\"')
        lines.append(f'{k}: \"{s}\"')
    return "\n".join(lines) + "\n"


def pack_total_len(
    pack_file: str, first: Para, last: Para, para_count: int, projected_body_len: int, source_url: str
) -> int:
    header = {
        "id": pack_file.removesuffix(".txt"),
        "section_id": first.section_id,
        "section_title": first.section_title,
        "page_start": first.page_start,
        "page_end": last.page_end,
        "pid_start": first.pid,
        "pid_end": last.pid,
        "para_count": para_count,
        "source_url": source_url,
        "url": f"{BASE_URL}/packs/{pack_file}",
    }
    header_block = "---\n" + yaml_like(header) + "---\n\n"
    # +1 for trailing newline
    return len(header_block) + projected_body_len + 1


def write_pack_files(root: Path, paras: list[Para], section_source_urls: dict[str, str]) -> tuple[dict[str, dict], dict]:
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    out_dir = root / "packs"
    out_dir.mkdir(parents=True, exist_ok=True)

    for old in out_dir.glob("*.txt"):
        try:
            old.unlink()
        except FileNotFoundError:
            pass

    manifest: dict[str, dict] = {}
    packs_meta: list[dict] = []

    cur_section_id: str | None = None
    cur_section_title = ""
    cur_pack_idx = 0
    cur_pack_file = ""
    cur_pack_paras: list[Para] = []
    cur_entries: list[str] = []
    cur_body_len = 0
    last_subsection = ""

    def start_pack(section_id: str, section_title: str) -> None:
        nonlocal cur_pack_file, cur_pack_paras, cur_entries, cur_body_len, last_subsection
        cur_pack_file = f"{section_id}-pack-{cur_pack_idx:02d}.txt"
        cur_pack_paras = []
        cur_entries = []
        cur_body_len = 0
        last_subsection = ""

    def render_pack_header(pack_file: str, first: Para, last: Para, para_count: int) -> dict:
        source_url = section_source_urls.get(first.section_id, "")
        return {
            "id": pack_file.removesuffix(".txt"),
            "section_id": first.section_id,
            "section_title": first.section_title,
            "page_start": first.page_start,
            "page_end": last.page_end,
            "pid_start": first.pid,
            "pid_end": last.pid,
            "para_count": para_count,
            "source_url": source_url,
            "url": f"{BASE_URL}/packs/{pack_file}",
        }

    def flush_pack() -> None:
        nonlocal cur_pack_idx, cur_pack_file, cur_pack_paras, cur_entries, cur_body_len
        if not cur_pack_paras:
            return

        first = cur_pack_paras[0]
        last = cur_pack_paras[-1]
        header = render_pack_header(cur_pack_file, first, last, len(cur_pack_paras))
        content = "---\n" + yaml_like(header) + "---\n\n" + "\n\n".join(cur_entries) + "\n"
        if len(content) > MAX_PACK_CHARS:
            raise RuntimeError(
                f"Pack too large: {cur_pack_file} len={len(content)} max={MAX_PACK_CHARS} "
                f"section={cur_section_id} pid_range={first.pid}-{last.pid}"
            )

        (out_dir / cur_pack_file).write_text(content, encoding="utf-8", errors="replace")

        packs_meta.append(
            {
                "id": cur_pack_file.removesuffix(".txt"),
                "file": f"packs/{cur_pack_file}",
                "section_id": cur_section_id,
                "section_title": cur_section_title,
                "page_start": first.page_start,
                "page_end": last.page_end,
                "pid_start": first.pid,
                "pid_end": last.pid,
                "para_count": len(cur_pack_paras),
            }
        )

        cur_pack_idx += 1

    def write_big_para_as_parts(p: Para) -> None:
        nonlocal cur_pack_idx

        base = f"{cur_section_id}-pack-{cur_pack_idx:02d}"
        body = strip_pid_prefix(p.text, p.pid)
        source_url = section_source_urls.get(p.section_id, "")

        common_prefix = ""
        if p.subsection:
            common_prefix = f"### {p.subsection}\n\n"

        temp_header = {
            "id": f"{base}.part-01",
            "pid": p.pid,
            "page_start": p.page_start,
            "page_end": p.page_end,
            "section_id": p.section_id,
            "section_title": p.section_title,
            "source_url": source_url,
            "part_index": 1,
            "part_total": 1,
            "prev": None,
            "next": None,
            "url": f"{BASE_URL}/packs/{base}.part-01.txt",
        }
        temp_header_block = "---\n" + yaml_like(temp_header) + "---\n\n"
        entry_head = f"## {p.pid} ({p.id}, pages {p.page_start}-{p.page_end}) [part 01/??]\n"
        max_chunk = MAX_PACK_CHARS - len(temp_header_block) - len(common_prefix) - len(entry_head) - 1 - 200
        if max_chunk < 500:
            raise RuntimeError(f"MAX_PACK_CHARS too small to split paragraph safely: {p.id}")

        chunks = split_text_parts(body, max_chunk)
        part_total = len(chunks)
        part_files = [f"packs/{base}.part-{i:02d}.txt" for i in range(1, part_total + 1)]

        for i, chunk in enumerate(chunks, start=1):
            part_id = f"{base}.part-{i:02d}"
            part_file = f"{part_id}.txt"
            prev_file = part_files[i - 2] if i > 1 else None
            next_file = part_files[i] if i < part_total else None

            header = {
                "id": part_id,
                "pid": p.pid,
                "page_start": p.page_start,
                "page_end": p.page_end,
                "section_id": p.section_id,
                "section_title": p.section_title,
                "source_url": source_url,
                "part_index": i,
                "part_total": part_total,
                "prev": prev_file,
                "next": next_file,
                "url": f"{BASE_URL}/packs/{part_file}",
            }
            head = "---\n" + yaml_like(header) + "---\n\n"
            entry_head = f"## {p.pid} ({p.id}, pages {p.page_start}-{p.page_end}) [part {i:02d}/{part_total:02d}]\n"
            content = head + common_prefix + entry_head + chunk + "\n"
            if len(content) > MAX_PACK_CHARS:
                raise RuntimeError(f"Part too large: {part_file} len={len(content)} max={MAX_PACK_CHARS}")

            (out_dir / part_file).write_text(content, encoding="utf-8", errors="replace")

            packs_meta.append(
                {
                    "id": part_id,
                    "file": f"packs/{part_file}",
                    "section_id": cur_section_id,
                    "section_title": cur_section_title,
                    "page_start": p.page_start,
                    "page_end": p.page_end,
                    "pid_start": p.pid,
                    "pid_end": p.pid,
                    "para_count": 1,
                    "part_index": i,
                    "part_total": part_total,
                }
            )

        manifest[p.id] = {"core": part_files[0], "parts": part_files}
        cur_pack_idx += 1

    for p in paras:
        if cur_section_id is None or p.section_id != cur_section_id:
            flush_pack()
            cur_section_id = p.section_id
            cur_section_title = p.section_title
            cur_pack_idx = 0
            start_pack(cur_section_id, cur_section_title)

        while True:
            prefix = ""
            if p.subsection and (not cur_pack_paras or p.subsection != last_subsection):
                prefix = f"### {p.subsection}\n\n"

            body = strip_pid_prefix(p.text, p.pid)
            entry = prefix + f"## {p.pid} ({p.id}, pages {p.page_start}-{p.page_end})\n{body}"

            if not cur_pack_paras:
                first = p
                projected_body = len(entry)
                source_url = section_source_urls.get(first.section_id, "")
                total = pack_total_len(cur_pack_file, first, p, 1, projected_body, source_url)
                if total > MAX_PACK_CHARS:
                    write_big_para_as_parts(p)
                    start_pack(cur_section_id, cur_section_title)
                    break

            projected_body = cur_body_len
            if cur_entries:
                projected_body += 2
            projected_body += len(entry)

            first = cur_pack_paras[0] if cur_pack_paras else p
            source_url = section_source_urls.get(first.section_id, "")
            total = pack_total_len(cur_pack_file, first, p, len(cur_pack_paras) + 1, projected_body, source_url)
            if total <= MAX_PACK_CHARS:
                cur_pack_paras.append(p)
                cur_entries.append(entry)
                cur_body_len = projected_body
                last_subsection = p.subsection
                manifest[p.id] = {"core": f"packs/{cur_pack_file}", "parts": None}
                break

            if cur_pack_paras:
                flush_pack()
                start_pack(cur_section_id, cur_section_title)
                continue

            write_big_para_as_parts(p)
            start_pack(cur_section_id, cur_section_title)
            break

    flush_pack()

    # packs index (small + deterministic)
    pack_fields = [
        "file",
        "section_id",
        "section_title",
        "page_start",
        "page_end",
        "pid_start",
        "pid_end",
        "para_count",
        "part_index",
        "part_total",
    ]
    pack_rows = []
    for m in packs_meta:
        pack_rows.append([m.get(k) for k in pack_fields])

    packs_index = {
        "dataset": {
            "name": "NTA TP audit guidance (jimuunei + casebook)",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "base_url": BASE_URL,
        },
        "note": "Canonical text is stored as packs (multi-paragraph files).",
        "max_pack_chars": MAX_PACK_CHARS,
        "fields": pack_fields,
        "rows": pack_rows,
    }

    (data_dir / "packs_index.json").write_text(
        json.dumps(packs_index, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    tsv_lines = ["\t".join(pack_fields)]
    for row in pack_rows:
        tsv_lines.append("\t".join("" if v is None else str(v) for v in row))
    (data_dir / "packs_index.tsv").write_text("\n".join(tsv_lines) + "\n", encoding="utf-8", errors="replace")

    return manifest, packs_index


def build_shards(root: Path, paras: list[Para], manifest: dict[str, dict]) -> dict:
    data_dir = root / "data"
    shards_dir = data_dir / "shards"
    shards_dir.mkdir(parents=True, exist_ok=True)

    for old in shards_dir.glob("shard-*.txt"):
        try:
            old.unlink()
        except FileNotFoundError:
            pass

    fields = [
        "id",
        "pid",
        "page_start",
        "page_end",
        "snippet",
        "core",
    ]

    section_groups: dict[str, list[Para]] = {}
    section_titles: dict[str, str] = {}
    for p in paras:
        section_groups.setdefault(p.section_id, []).append(p)
        if p.section_title:
            section_titles.setdefault(p.section_id, p.section_title)

    def section_sort_key(sid: str):
        m = re.match(r"^jimu-ch(\d+)$", sid)
        if m:
            return (0, int(m.group(1)), sid)
        if sid == "bessatsu":
            return (1, 0, sid)
        return (9, 9_999, sid)

    sections_index = []
    shards_flat = []
    shard_counter = 0

    for section_id in sorted(section_groups.keys(), key=section_sort_key):
        items = section_groups[section_id]
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
                    snippet = snippet[len(p.pid) :].lstrip(" .．　")
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
            "name": "NTA TP audit guidance (jimuunei + casebook)",
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
    data_dir = root / "data"
    out_dir = data_dir / "latin_terms"
    out_dir.mkdir(parents=True, exist_ok=True)

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

    hits: dict[str, list[dict]] = {}
    for p in paras:
        terms = sorted(set(LATIN_TERM_RE.findall(p.text or "")))
        if not terms:
            continue
        for term in terms:
            hits.setdefault(term, []).append(
                {
                    "id": p.id,
                    "pid": p.pid,
                    "page_start": p.page_start,
                    "page_end": p.page_end,
                    "section_id": p.section_id,
                    "context": extract_term_context(p.text, term),
                    "core": manifest.get(p.id, {}).get("core", f"core/{p.id}.txt"),
                }
            )

    terms_index = []

    for term in sorted(hits.keys()):
        rows = hits[term]
        slug = safe_slug(term)
        shard_files = []

        buf_lines = ["\t".join(fields)]
        shard_idx = 0

        def flush_shard() -> None:
            nonlocal buf_lines, shard_idx
            if len(buf_lines) <= 1:
                return
            file = f"{slug}-{shard_idx:02d}.tsv"
            (out_dir / file).write_text("\n".join(buf_lines) + "\n", encoding="utf-8", errors="replace")
            shard_files.append(f"data/latin_terms/{file}")
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
            "name": "NTA TP audit guidance (jimuunei + casebook)",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "base_url": BASE_URL,
        },
        "note": "Uppercase latin keyword reverse index (paragraph/case level).",
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
    text = f"""# 国税庁（NTA）移転価格 - 事務運営指針/参考事例集 - Quickstart

前提（重要）
- あなたのAIは「1回のReadで最初の1万トークンのみ」読める。
- したがって、このDBは『索引→shard→本文』の3段階で最短到達する。

入口（迷ったら）
- `data/topics.txt`（テーマ別索引）

推奨フロー（最小Read）
1) `data/shards_index.json` を読む（章=section と shard の対応を得る）
2) 関連する `data/shards/*.txt` を1〜3個読む（TSVを検索）
3) 行にある `packs/...` を開いて本文を読む（packは複数項目をまとめたもの）

追加: 英字キーワード（APA/DCF/OECD/TNMM等）で横断検索したい場合
1) `data/latin_terms/index.json` を読む
2) 該当する `data/latin_terms/*.tsv` を読む（該当箇所一覧）
3) 行にある `packs/...` を開いて本文を読む

収録（目安）
- 移転価格事務運営要領（事務運営指針）: 第1章〜第8章（HTMLを段落単位で索引化）
- 別冊 参考事例集（PDF）: preface + 事例1〜31（事例単位で索引化）

URLパターン
- shard index: `{BASE_URL}/data/shards_index.json`
- shard TSV: `{BASE_URL}/data/shards/shard-XX.txt`
- 本文(pack): `{BASE_URL}/packs/{{section}}-pack-XX.txt`
- packs index: `{BASE_URL}/data/packs_index.tsv`
- 英字キーワード索引: `{BASE_URL}/data/latin_terms/index.json`
"""
    qs_path.write_text(text, encoding="utf-8")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    if not RAW_DIR.exists():
        raise SystemExit(f"Missing RAW_DIR: {RAW_DIR}")

    # 1) NTA guideline chapters (HTML)
    html_paths = [RAW_DIR / f"{i:02d}.htm" for i in range(1, 9)]
    jimu_paras: list[Para] = []
    section_source_urls: dict[str, str] = {}

    for p in html_paths:
        raw = p.read_bytes().decode("shift_jis", errors="replace")
        paras = iter_jimuunei_paras(raw)
        if not paras:
            continue
        jimu_paras.extend(paras)
        sid = paras[0].section_id
        section_source_urls[sid] = f"{NTA_BASE}/{p.name}"

    # Ensure chapters are ordered by chapter number.
    def jimu_key(p: Para):
        m = re.match(r"^jimu-ch(\d+)$", p.section_id)
        return (int(m.group(1)) if m else 9_999, p.pid, p.id)

    jimu_paras.sort(key=jimu_key)

    # 2) Casebook PDF (事例集)
    pdf_path = RAW_DIR / "bessatsu.pdf"
    if not pdf_path.exists():
        raise SystemExit(f"Missing PDF: {pdf_path}")
    bessatsu_paras = iter_bessatsu_cases(pdf_path)
    section_source_urls["bessatsu"] = f"{NTA_BASE}/pdf/bessatsu.pdf"

    paras = jimu_paras + bessatsu_paras

    manifest, _packs_index = write_pack_files(root, paras, section_source_urls)
    build_shards(root, paras, manifest)
    build_latin_terms_index(root, paras, manifest)
    write_quickstart(root)

    print(f"paras: {len(paras)} (jimu: {len(jimu_paras)}, bessatsu: {len(bessatsu_paras)})")
    print(f"packs_dir: {root / 'packs'}")
    print(f"shards_dir: {root / 'data' / 'shards'}")
    print(f"index: {root / 'data' / 'shards_index.json'}")
    print(f"packs_index: {root / 'data' / 'packs_index.tsv'}")
    print(f"quickstart: {root / 'quickstart.txt'}")


if __name__ == "__main__":
    main()
