#!/usr/bin/env python3
"""
Build AI-optimized artifacts for Japan's tax treaties (major jurisdictions).

Source of truth:
  Ministry of Finance (Japan) / The List of Japan's Tax Conventions
  https://www.mof.go.jp/english/policy/tax_policy/tax_conventions/tax_convetion_list_en.html

Mobile-AI constraint:
  The client can read only the first ~10k tokens per request.

Therefore:
  Provide a small, deterministic retrieval flow:
    docs_index.tsv -> shards_index.json -> shard TSV -> packs/*.txt
  Canonical text is stored as packs (multi-article files, each <= ~10k tokens/read).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
import urllib.request

from bs4 import BeautifulSoup
import pdfplumber
from pypdf import PdfReader


BASE_URL = "https://jplawdb.github.io/html-preview/ai-treaty-db/jp-tax-treaties"
MOF_LIST_URL = "https://www.mof.go.jp/english/policy/tax_policy/tax_conventions/tax_convetion_list_en.html"

RAW_DIR = Path(__file__).resolve().parents[1] / "source" / "raw"

# Conservative size guardrails for the mobile-AI token limit.
MAX_PACK_CHARS = 9500
SHARD_SIZE = 80
SNIPPET_CHARS = 120

LATIN_TERM_RE = re.compile(r"\b[A-Z][A-Z0-9-]{2,}\b")
MAX_TERM_SHARD_CHARS = 9000
TERM_CONTEXT_RADIUS = 70


# Major jurisdictions (substring match; case-insensitive).
# Add/remove freely.
MAJOR_KEYWORDS = [
    "United States",
    "United Kingdom",
    "Germany",
    "France",
    "Italy",
    "Canada",
    "Australia",
    "China",
    "Korea",
    "Singapore",
    "Hong Kong",
    "Netherlands",
    "Switzerland",
    "Ireland",
    "India",
    "Brazil",
    "Russia",
    "South Africa",
    "Spain",
    "Mexico",
    "Indonesia",
    "Malaysia",
    "Thailand",
    "Viet",
    "Philippines",
    "New Zealand",
    "Belgium",
    "Luxembourg",
    "Sweden",
    "Norway",
    "Denmark",
    "Finland",
    "Austria",
    "United Arab Emirates",
    "Saudi",
    "Qatar",
    "Kuwait",
]


def is_major(jurisdiction: str) -> bool:
    lj = (jurisdiction or "").lower()
    return any(k.lower() in lj for k in MAJOR_KEYWORDS)


def safe_slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "x"


def clean_ws(s: str) -> str:
    t = (s or "").replace("\u3000", " ")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def fetch_bytes(url: str) -> bytes:
    # MOF is generally accessible without special headers, but keep a UA to be safe.
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def is_synth_pdf(url: str) -> bool:
    """
    Detect "Synthesized/Synthesised Text" style PDFs.
    """
    path = urlparse(url).path
    name = Path(path).name.lower()
    if "synthesizedtextforjapan" in name or "synthesisedtextforjapan" in name:
        return True
    if "_st_jp" in name or name.endswith("_st_jp.pdf"):
        return True
    # Some files use hyphenated variants: SynthesizedTextforJapan-SingaporeJP.pdf
    if name.startswith("synthes") and "japan" in name:
        return True
    return False


@dataclass(frozen=True)
class PdfLink:
    jurisdiction: str
    label: str
    url: str


def parse_mof_list_major_links() -> list[PdfLink]:
    html = fetch_bytes(MOF_LIST_URL).decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")

    table = None
    for t in soup.find_all("table"):
        txt = t.get_text(" ", strip=True)
        if "Jurisdictions" in txt and "Text/Outline" in txt:
            table = t
            break
    if table is None:
        raise RuntimeError("Failed to locate treaty list table on MOF page.")

    links_by_j: dict[str, list[PdfLink]] = {}

    last_j = ""
    for r in table.find_all("tr")[1:]:
        tds = [c.get_text(" ", strip=True) for c in r.find_all("td")]
        if not tds:
            continue

        # Determine the current jurisdiction.
        j = ""
        if len(tds) >= 2 and len(tds[0]) == 1 and tds[0].isalpha():
            j = tds[1]
        elif len(tds) >= 1 and not tds[0].startswith(("New Convention", "Amending Protocol", "Convention to Implement")):
            j = tds[0]
        else:
            j = last_j

        if j:
            last_j = j
        if not j or not is_major(j):
            continue

        for a in r.find_all("a"):
            label = a.get_text(" ", strip=True)
            href = (a.get("href") or "").strip()
            if not href:
                continue
            href = urljoin(MOF_LIST_URL, href)
            if not href.lower().endswith(".pdf"):
                continue
            if "Japanese" not in label:
                continue
            links_by_j.setdefault(j, []).append(PdfLink(jurisdiction=j, label=label, url=href))

    # Selection policy:
    # - If the jurisdiction has at least one /tax_convention/ Japanese PDF, include:
    #   - all /tax_convention/ Japanese PDFs (treaty texts, protocols, etc.)
    #   - plus any synthesized-text Japanese PDFs (even if not under /tax_convention/)
    # - Otherwise (no /tax_convention/ PDFs), include all Japanese PDFs for that jurisdiction.
    selected: list[PdfLink] = []
    seen: set[str] = set()

    for j, links in sorted(links_by_j.items(), key=lambda kv: kv[0].lower()):
        urls = [lnk.url.strip() for lnk in links]
        has_tax_convention = any("/tax_convention/" in u for u in urls)

        chosen: list[PdfLink] = []
        if has_tax_convention:
            for lnk in links:
                u = lnk.url.strip()
                if "/tax_convention/" in u or is_synth_pdf(u):
                    chosen.append(lnk)
        else:
            chosen = list(links)

        for lnk in chosen:
            u = lnk.url.strip()
            if u in seen:
                continue
            seen.add(u)
            selected.append(PdfLink(jurisdiction=j, label=lnk.label, url=u))

    return selected


def download_pdfs(links: list[PdfLink], out_dir: Path) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    docs: list[dict] = []

    for lnk in links:
        path = urlparse(lnk.url).path
        basename = Path(path).name
        juris_slug = safe_slug(lnk.jurisdiction)
        base_slug = safe_slug(basename).removesuffix("-pdf")
        doc_id = f"{juris_slug}--{base_slug}"
        out_pdf = out_dir / f"{doc_id}.pdf"

        if not out_pdf.exists():
            data = fetch_bytes(lnk.url)
            out_pdf.write_bytes(data)

        docs.append(
            {
                "doc_id": doc_id,
                "jurisdiction": lnk.jurisdiction,
                "label": lnk.label,
                "source_url": lnk.url,
                "source_basename": basename,
                "file": str(out_pdf.relative_to(out_dir.parent.parent)),  # source/raw/...
                "is_synth": is_synth_pdf(lnk.url),
            }
        )

    return docs


_FW_DIGITS = str.maketrans(
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
    return (s or "").translate(_FW_DIGITS)


KANJI_DIGITS = {
    "〇": 0,
    "零": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def kanji_to_int(s: str) -> int | None:
    """
    Minimal kanji numeral parser for treaty article numbers (typically <= 50).
    Supports: 十, 百 (e.g., 二十五, 三十, 百一).
    """
    t = (s or "").strip()
    if not t:
        return None
    if t.isdigit():
        return int(t)

    total = 0
    num = 0
    unit = 0

    for ch in t:
        if ch in KANJI_DIGITS:
            num = KANJI_DIGITS[ch]
            continue
        if ch == "十":
            unit = 10
        elif ch == "百":
            unit = 100
        else:
            return None

        if num == 0:
            num = 1
        total += num * unit
        num = 0
        unit = 0

    total += num
    return total if total > 0 else None


ROMAN_MAP = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def roman_to_int(s: str) -> int | None:
    t = (s or "").strip().upper()
    if not t or any(ch not in ROMAN_MAP for ch in t):
        return None
    total = 0
    prev = 0
    for ch in reversed(t):
        v = ROMAN_MAP[ch]
        if v < prev:
            total -= v
        else:
            total += v
            prev = v
    return total if total > 0 else None


JP_ART_RE = re.compile(
    r"^第\s*(?P<num>[一二三四五六七八九十百〇零0-9０-９]+)\s*条(?:\s*の\s*(?P<sub>[一二三四五六七八九十百〇零0-9０-９]+))?"
)
EN_ART_RE = re.compile(r"^Article\s+(?P<num>\d+[A-Z]?|[IVXLC]+)\b", re.IGNORECASE)


def article_pid_from_line(line: str) -> str | None:
    s = (line or "").strip()
    if not s:
        return None

    m = JP_ART_RE.match(s)
    if m:
        raw = normalize_digits(m.group("num"))
        main = int(raw) if raw.isdigit() else (kanji_to_int(raw) or kanji_to_int(m.group("num")))
        if main is None:
            return None
        sub = None
        if m.group("sub"):
            raw2 = normalize_digits(m.group("sub"))
            sub = int(raw2) if raw2.isdigit() else (kanji_to_int(raw2) or kanji_to_int(m.group("sub")))
        return f"{main}-{sub}" if sub is not None else str(main)

    m = EN_ART_RE.match(s)
    if m:
        raw = m.group("num").strip()
        if raw.isdigit():
            return str(int(raw))
        ri = roman_to_int(raw)
        if ri is not None:
            return str(ri)
        return raw  # e.g., 10A

    return None


def extract_pdf_text_pages(pdf_path: Path) -> list[str]:
    """
    Extract page texts.

    Primary: pypdf (works well for many MOF PDFs).
    Fallback: pdfplumber (for pages where pypdf returns empty).
    """
    reader = PdfReader(str(pdf_path))
    out: list[str] = []

    plumber = None
    try:
        plumber = pdfplumber.open(pdf_path)
    except Exception:
        plumber = None

    for i, page in enumerate(reader.pages):
        t = page.extract_text() or ""
        if not t and plumber is not None and i < len(plumber.pages):
            try:
                t = plumber.pages[i].extract_text() or ""
            except Exception:
                t = ""
        out.append(t)

    if plumber is not None:
        try:
            plumber.close()
        except Exception:
            pass
    return out


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


def iter_doc_paras(doc_id: str, section_title: str, pages: list[str]) -> list[Para]:
    """
    Split a treaty PDF into article-level paragraphs when possible.

    If no article headings are detected, fall back to 1 blob.
    """
    out: list[Para] = []
    preamble_buf: list[str] = []
    in_articles = False

    cur_pid: str | None = None
    cur_id: str | None = None
    cur_page_start: int | None = None
    cur_page_end: int | None = None
    buf: list[str] = []

    def flush_article() -> None:
        nonlocal cur_pid, cur_id, cur_page_start, cur_page_end, buf
        if cur_pid is None or cur_id is None or cur_page_start is None or cur_page_end is None:
            cur_pid, cur_id, cur_page_start, cur_page_end, buf = None, None, None, None, []
            return
        out.append(
            Para(
                id=cur_id,
                pid=cur_pid,
                page_start=cur_page_start,
                page_end=cur_page_end,
                section_id=doc_id,
                section_title=section_title,
                subsection="",
                text="\n".join(buf).strip(),
            )
        )
        cur_pid, cur_id, cur_page_start, cur_page_end, buf = None, None, None, None, []

    for page_no, t in enumerate(pages, start=1):
        lines = (t or "").splitlines()
        for raw in lines:
            s = raw.strip()
            if not s:
                continue
            if s.isdigit() and len(s) <= 3:
                continue

            pid = article_pid_from_line(s)
            if pid:
                if not in_articles:
                    in_articles = True
                    if preamble_buf:
                        out.append(
                            Para(
                                id=f"{doc_id}-preamble",
                                pid="preamble",
                                page_start=1,
                                page_end=max(1, page_no),
                                section_id=doc_id,
                                section_title=section_title,
                                subsection="",
                                text="\n".join(preamble_buf).strip(),
                            )
                        )
                        preamble_buf = []
                else:
                    flush_article()

                cur_pid = pid
                cur_id = f"{doc_id}-a{pid}"
                cur_page_start = page_no
                cur_page_end = page_no
                buf = [s]
                continue

            if in_articles:
                if cur_pid is None:
                    # Defensive: shouldn't happen, but keep content.
                    continue
                buf.append(s)
                cur_page_end = page_no
            else:
                preamble_buf.append(s)

    if in_articles:
        flush_article()
    elif preamble_buf:
        out.append(
            Para(
                id=f"{doc_id}-all",
                pid="all",
                page_start=1,
                page_end=len(pages),
                section_id=doc_id,
                section_title=section_title,
                subsection="",
                text="\n".join(preamble_buf).strip(),
            )
        )
    return out


def yaml_like(d: dict) -> str:
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


def write_pack_files(root: Path, paras: list[Para], source_urls: dict[str, str]) -> tuple[dict[str, dict], dict]:
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

    def start_pack(section_id: str, section_title: str) -> None:
        nonlocal cur_pack_file, cur_pack_paras, cur_entries, cur_body_len
        cur_pack_file = f"{section_id}-pack-{cur_pack_idx:02d}.txt"
        cur_pack_paras = []
        cur_entries = []
        cur_body_len = 0

    def render_pack_header(pack_file: str, first: Para, last: Para, para_count: int) -> dict:
        src = source_urls.get(first.section_id, "")
        return {
            "id": pack_file.removesuffix(".txt"),
            "section_id": first.section_id,
            "section_title": first.section_title,
            "page_start": first.page_start,
            "page_end": last.page_end,
            "pid_start": first.pid,
            "pid_end": last.pid,
            "para_count": para_count,
            "source_url": src,
            "url": f"{BASE_URL}/packs/{pack_file}",
        }

    def pack_total_len(pack_file: str, first: Para, last: Para, para_count: int, projected_body_len: int) -> int:
        header_block = "---\n" + yaml_like(render_pack_header(pack_file, first, last, para_count)) + "---\n\n"
        return len(header_block) + projected_body_len + 1

    def flush_pack() -> None:
        nonlocal cur_pack_idx, cur_pack_file, cur_pack_paras, cur_entries, cur_body_len
        if not cur_pack_paras:
            return
        first = cur_pack_paras[0]
        last = cur_pack_paras[-1]
        header = render_pack_header(cur_pack_file, first, last, len(cur_pack_paras))
        content = "---\n" + yaml_like(header) + "---\n\n" + "\n\n".join(cur_entries) + "\n"
        if len(content) > MAX_PACK_CHARS:
            raise RuntimeError(f"Pack too large: {cur_pack_file} len={len(content)} max={MAX_PACK_CHARS}")
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
        body = p.text.strip()
        src = source_urls.get(p.section_id, "")

        temp_header = {
            "id": f"{base}.part-01",
            "pid": p.pid,
            "page_start": p.page_start,
            "page_end": p.page_end,
            "section_id": p.section_id,
            "section_title": p.section_title,
            "source_url": src,
            "part_index": 1,
            "part_total": 1,
            "prev": None,
            "next": None,
            "url": f"{BASE_URL}/packs/{base}.part-01.txt",
        }
        temp_header_block = "---\n" + yaml_like(temp_header) + "---\n\n"
        entry_head = f"## {p.pid} ({p.id}, pages {p.page_start}-{p.page_end}) [part 01/??]\n"
        max_chunk = MAX_PACK_CHARS - len(temp_header_block) - len(entry_head) - 1 - 200
        if max_chunk < 500:
            raise RuntimeError("MAX_PACK_CHARS too small to split safely")

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
                "source_url": src,
                "part_index": i,
                "part_total": part_total,
                "prev": prev_file,
                "next": next_file,
                "url": f"{BASE_URL}/packs/{part_file}",
            }
            head = "---\n" + yaml_like(header) + "---\n\n"
            entry_head = f"## {p.pid} ({p.id}, pages {p.page_start}-{p.page_end}) [part {i:02d}/{part_total:02d}]\n"
            content = head + entry_head + chunk + "\n"
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

    # Ensure stable order by (section_id, pid, page_start).
    def sort_key(p: Para):
        return (p.section_id, p.page_start, p.pid)

    for p in sorted(paras, key=sort_key):
        if cur_section_id is None or p.section_id != cur_section_id:
            flush_pack()
            cur_section_id = p.section_id
            cur_section_title = p.section_title
            cur_pack_idx = 0
            start_pack(cur_section_id, cur_section_title)

        while True:
            entry = f"## {p.pid} ({p.id}, pages {p.page_start}-{p.page_end})\n{p.text.strip()}"

            if not cur_pack_paras:
                projected_body = len(entry)
                total = pack_total_len(cur_pack_file, p, p, 1, projected_body)
                if total > MAX_PACK_CHARS:
                    write_big_para_as_parts(p)
                    start_pack(cur_section_id, cur_section_title)
                    break

            projected_body = cur_body_len
            if cur_entries:
                projected_body += 2
            projected_body += len(entry)

            first = cur_pack_paras[0] if cur_pack_paras else p
            total = pack_total_len(cur_pack_file, first, p, len(cur_pack_paras) + 1, projected_body)
            if total <= MAX_PACK_CHARS:
                cur_pack_paras.append(p)
                cur_entries.append(entry)
                cur_body_len = projected_body
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
    pack_rows = [[m.get(k) for k in pack_fields] for m in packs_meta]

    packs_index = {
        "dataset": {
            "name": "Japan tax treaties (major jurisdictions)",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "base_url": BASE_URL,
        },
        "note": "Canonical text is stored as packs (multi-article files).",
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

    fields = ["id", "pid", "page_start", "page_end", "snippet", "core"]

    section_groups: dict[str, list[Para]] = {}
    section_titles: dict[str, str] = {}
    for p in paras:
        section_groups.setdefault(p.section_id, []).append(p)
        section_titles.setdefault(p.section_id, p.section_title)

    sections_index = []
    shards_flat = []
    shard_counter = 0

    for section_id in sorted(section_groups.keys()):
        items = sorted(section_groups[section_id], key=lambda p: (p.page_start, p.pid))
        title = section_titles.get(section_id, "")
        section_shards = []

        for i in range(0, len(items), SHARD_SIZE):
            shard_items = items[i : i + SHARD_SIZE]
            shard_file = f"shard-{shard_counter:03d}.txt"
            shard_path = shards_dir / shard_file

            rows = ["\t".join(fields)]
            for p in shard_items:
                snippet = clean_ws(p.text)[:SNIPPET_CHARS]
                core_rel = manifest.get(p.id, {}).get("core", f"core/{p.id}.txt")
                rows.append("\t".join([p.id, p.pid, str(p.page_start), str(p.page_end), snippet, core_rel]))

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
            "name": "Japan tax treaties (major jurisdictions)",
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
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return index


def safe_slug_term(s: str) -> str:
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

    fields = ["id", "pid", "page_start", "page_end", "section_id", "context", "core"]

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
        slug = safe_slug_term(term)
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
        terms_index.append({"term": term, "slug": slug, "para_count": len(rows), "files": shard_files})

    index = {
        "dataset": {
            "name": "Japan tax treaties (major jurisdictions)",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "base_url": BASE_URL,
        },
        "note": "Uppercase latin keyword reverse index (article-level).",
        "term_pattern": LATIN_TERM_RE.pattern,
        "max_term_shard_chars": MAX_TERM_SHARD_CHARS,
        "fields": fields,
        "terms": terms_index,
    }
    (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return index


def write_docs_index(root: Path, docs_meta: list[dict]) -> None:
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    fields = ["doc_id", "jurisdiction", "source_url", "source_basename", "is_synth", "file"]
    lines = ["\t".join(fields)]
    for d in docs_meta:
        lines.append("\t".join(str(d.get(k, "")) for k in fields))
    (data_dir / "docs_index.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8", errors="replace")

    (data_dir / "docs_index.json").write_text(
        json.dumps(
            {
                "dataset": {
                    "name": "Japan tax treaties (major jurisdictions)",
                    "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "base_url": BASE_URL,
                    "source_list_url": MOF_LIST_URL,
                },
                "fields": fields,
                "rows": [{k: d.get(k, "") for k in fields} for d in docs_meta],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def write_topics(root: Path) -> None:
    (root / "data").mkdir(parents=True, exist_ok=True)
    text = """# 日本の租税条約（主要国） - テーマ別索引（人間/AI向け）

NOTE（重要）
- 本DBはモバイルAIの「1回のReadで先頭1万トークンのみ」制約を前提にしている。
- したがって、AI用途では `docs_index.tsv` → `shards_index.json` → `shard TSV` → `packs` が基本。

## よくある条文テーマ（横断）

典型キーワード（日本語）
- 恒久的施設 / PE
- 事業所得 / 企業の利得 / Business profits
- 関連企業 / 移転価格 / 独立企業原則 / Article 9
- 配当 / 利子 / 使用料（ロイヤルティ）
- 受益者 / beneficial owner
- 相互協議 / MAP / 仲裁
- 情報交換 / EOI

典型キーワード（英字）
- PE / MAP / APA / MLI / PPT / LOB / CFC

実務フロー（最短）
1) `data/docs_index.tsv`（対象国の doc_id を探す）
2) `data/shards_index.json`（doc_id=section と shard の対応を得る）
3) `data/shards/shard-XXX.txt`（TSVを検索）
4) `packs/...`（本文）
"""
    (root / "data" / "topics.txt").write_text(text, encoding="utf-8")


def write_quickstart(root: Path) -> None:
    text = f"""# 日本の租税条約（主要国） - Quickstart

前提（重要）
- あなたのAIは「1回のReadで最初の1万トークンのみ」読める。
- したがって、このDBは『docs_index → shards_index → shard → 本文(pack)』で最短到達する。

推奨フロー（最小Read）
1) `data/docs_index.tsv` を読む（対象国/文書を選ぶ）
2) `data/shards_index.json` を読む（section=doc_id と shard の対応）
3) 関連する `data/shards/*.txt` を1〜3個読む（TSVを検索）
4) 行にある `packs/...` を開いて本文を読む（packは複数条文をまとめたもの）

追加: 英字キーワード（PE/MAP/MLI/PPT等）で横断検索したい場合
1) `data/latin_terms/index.json` を読む
2) 該当する `data/latin_terms/*.tsv` を読む
3) 行にある `packs/...` を開いて本文を読む

URLパターン
- docs index (tsv): `{BASE_URL}/data/docs_index.tsv`
- shard index: `{BASE_URL}/data/shards_index.json`
- shard TSV: `{BASE_URL}/data/shards/shard-XXX.txt`
- 本文(pack): `{BASE_URL}/packs/{{doc_id}}-pack-XX.txt`
- topics: `{BASE_URL}/data/topics.txt`
"""
    (root / "quickstart.txt").write_text(text, encoding="utf-8")


def write_llms(root: Path) -> None:
    text = f"""# 日本の租税条約（主要国） / AI検索最適化
# Base URL: {BASE_URL}/

推奨読手順（10k tokens/read 制約前提）
1) `quickstart.txt`
2) `data/docs_index.tsv`
3) `data/shards_index.json`
4) `data/shards/*.txt`（TSVを読む＝検索）
5) `packs/*.txt`（本文。複数条文を1ファイルにまとめたpack）

英字キーワード（PE/MAP/MLI/PPT等）での横断検索
- `data/latin_terms/index.json` → `data/latin_terms/*.tsv` → `packs/...`
"""
    (root / "llms.txt").write_text(text, encoding="utf-8")


def write_index_html(root: Path) -> None:
    html = """<!doctype html>
<html lang=\"ja\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>日本の租税条約（主要国） - AI検索最適化</title>
  <meta name=\"description\" content=\"財務省公開資料を基に、日本の租税条約（主要国）を10k tokens/read 制約下で検索しやすい粒度に分割して提供します。\" />
  <link rel=\"alternate\" type=\"text/plain\" href=\"llms.txt\" title=\"LLM Site Map\" />
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, 'Noto Sans JP', sans-serif; line-height: 1.7; max-width: 900px; margin: 0 auto; padding: 24px; }
    h1 { margin: 0 0 8px; }
    .meta { color: #555; margin: 0 0 16px; }
    code { background: #f3f3f3; padding: 0 6px; border-radius: 4px; }
    ul { padding-left: 1.2rem; }
  </style>
</head>
<body>
  <h1>日本の租税条約（主要国）</h1>
  <p class=\"meta\">モバイルAIの「1回のReadで最初の1万トークンのみ」制約を前提に、docs_index→shard→本文(pack)で最短到達できるように分割済み。</p>

  <ul>
    <li><a href=\"quickstart.txt\">quickstart.txt</a>（必読。使い方）</li>
    <li><a href=\"data/docs_index.tsv\">data/docs_index.tsv</a>（対象国/文書一覧）</li>
    <li><a href=\"data/shards_index.json\">data/shards_index.json</a>（文書別マップ + shard一覧）</li>
    <li><a href=\"data/packs_index.tsv\">data/packs_index.tsv</a>（本文pack一覧。本文は packs/*.txt）</li>
    <li><a href=\"data/topics.txt\">data/topics.txt</a>（テーマ別索引）</li>
    <li><a href=\"data/latin_terms/index.json\">data/latin_terms/index.json</a>（英字キーワード逆引き）</li>
  </ul>
</body>
</html>
"""
    (root / "index.html").write_text(html, encoding="utf-8")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    links = parse_mof_list_major_links()
    docs_meta = download_pdfs(links, RAW_DIR)
    write_docs_index(root, docs_meta)

    # Build paras across all docs.
    paras: list[Para] = []
    source_urls: dict[str, str] = {}

    for d in docs_meta:
        doc_id = d["doc_id"]
        section_title = f"{d['jurisdiction']} ({d['source_basename']})"
        pdf_path = root / d["file"]
        pages = extract_pdf_text_pages(pdf_path)
        doc_paras = iter_doc_paras(doc_id, section_title, pages)
        paras.extend(doc_paras)
        source_urls[doc_id] = d["source_url"]

    manifest, _packs_index = write_pack_files(root, paras, source_urls)
    build_shards(root, paras, manifest)
    build_latin_terms_index(root, paras, manifest)
    write_topics(root)
    write_quickstart(root)
    write_llms(root)
    write_index_html(root)

    print(f"docs: {len(docs_meta)}")
    print(f"paras: {len(paras)}")
    print(f"packs_dir: {root / 'packs'}")
    print(f"shards_dir: {root / 'data' / 'shards'}")
    print(f"index: {root / 'data' / 'shards_index.json'}")


if __name__ == "__main__":
    main()
