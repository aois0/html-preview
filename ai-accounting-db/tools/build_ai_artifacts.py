#!/usr/bin/env python3
"""
Build AI-optimized artifacts for accounting standards related corpora under
1-read ~= 10k-token constraints.

Current corpus (as of 2026-02-11):
  - ASBJ (企業会計基準 / 実務対応報告 / 適用指針 / 公開草案等)
  - 金融庁 企業会計審議会（答申・報告書 / 議事録・資料）
  - JICPA 実務指針等公表物（主要カテゴリ）

Output layout (ai-accounting-db/):
  - enhanced/{doc_code}/index.html
  - enhanced/{doc_code}/{item_id}.html
  - text/{doc_code}/{item_id}.txt
  - data/doc_aliases.json
  - data/docs_index.tsv
  - data/resolve_lite/index.json
  - data/resolve_lite/{doc_code}.json
  - data/chunks/{doc_code}.jsonl
  - data/shards_index.json
  - data/shards/shard-XXX.txt
  - quickstart.txt
  - llms.txt
  - index.html
  - sitemap.xml
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
import json
import logging
import re
import shutil
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from pypdf import PdfReader
import requests


BASE_URL = "https://jplawdb.github.io/html-preview/ai-accounting-db"
ROOT_DIR = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT_DIR / "source" / "raw"

MAX_PART_CHARS = 6500
MAX_SHARD_ROWS = 12
SHARD_SNIPPET_CHARS = 120
MAX_RESOLVE_ITEMS_PER_PART = 15
REQUEST_TIMEOUT = 60
USER_AGENT = "jplawdb-ai-accounting-db-bot/1.0 (+https://jplawdb.github.io/html-preview/)"
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 120

COMMON_NOISE_LINES = {
    "このページの先頭へ",
    "ページの先頭へ",
    "ホーム",
    "戻る",
    "English",
    "Back to Japanese",
    "MENU",
    "検索",
}

NOISE_TITLE_WORDS = {
    "ホーム",
    "本文へ移動",
    "ページの先頭へ",
    "English",
    "Back to Japanese",
    "Language",
    "SNSメニュー",
    "MENU",
    "検索",
    "大",
    "中",
    "小",
    "さらに見る",
}

# Reduce noisy parser warnings from some legacy JP PDF encodings.
logging.getLogger("pypdf").setLevel(logging.ERROR)


@dataclass(frozen=True)
class SourceSpec:
    doc_code: str
    doc_title: str
    year: str
    index_url: str
    aliases: list[str]
    kind: str
    allowed_domains: tuple[str, ...]
    allowed_prefixes: tuple[str, ...]
    max_depth: int = 1
    max_sections: int = 1200
    max_html_follow: int = 300
    max_pdfs_per_detail: int = 999
    include_pdf: bool = True


@dataclass(frozen=True)
class Section:
    section_no: int
    section_id: str
    section_title: str
    source_url: str
    source_type: str


@dataclass
class Item:
    doc_code: str
    doc_title: str
    year: str
    item_id: str
    item_title: str
    section_id: str
    section_no: int
    section_title: str
    source_url: str
    source_type: str
    part_index: int
    part_total: int
    lines: list[str]


SOURCES: list[SourceSpec] = [
    SourceSpec(
        doc_code="asbj_accounting_standards",
        doc_title="ASBJ 企業会計基準（現行）",
        year="2026",
        index_url="https://www.asb-j.jp/jp/accounting_standards.html",
        aliases=[
            "ASBJ 企業会計基準",
            "企業会計基準 ASBJ",
            "asbj accounting standards",
        ],
        kind="asbj",
        allowed_domains=("www.asb-j.jp", "www.fasf-j.jp"),
        allowed_prefixes=(
            "/jp/accounting_standards.html",
            "/jp/accounting_standards/",
            "/jp/project/",
            "/jp/wp-content/uploads/sites/",
        ),
        max_depth=2,
        max_sections=900,
        max_html_follow=260,
        max_pdfs_per_detail=8,
    ),
    SourceSpec(
        doc_code="asbj_implementation_guidance",
        doc_title="ASBJ 適用指針（現行）",
        year="2026",
        index_url="https://www.asb-j.jp/jp/implementation_guidance.html",
        aliases=[
            "ASBJ 適用指針",
            "実務指針 ASBJ",
            "asbj implementation guidance",
        ],
        kind="asbj",
        allowed_domains=("www.asb-j.jp", "www.fasf-j.jp"),
        allowed_prefixes=(
            "/jp/implementation_guidance.html",
            "/jp/implementation_guidance/",
            "/jp/project/",
            "/jp/wp-content/uploads/sites/",
        ),
        max_depth=2,
        max_sections=800,
        max_html_follow=240,
        max_pdfs_per_detail=8,
    ),
    SourceSpec(
        doc_code="asbj_practical_solution",
        doc_title="ASBJ 実務対応報告（現行）",
        year="2026",
        index_url="https://www.asb-j.jp/jp/practical_solution.html",
        aliases=[
            "ASBJ 実務対応報告",
            "実務対応報告",
            "asbj practical solution",
        ],
        kind="asbj",
        allowed_domains=("www.asb-j.jp", "www.fasf-j.jp"),
        allowed_prefixes=(
            "/jp/practical_solution.html",
            "/jp/practical_solution/",
            "/jp/project/",
            "/jp/wp-content/uploads/sites/",
        ),
        max_depth=2,
        max_sections=800,
        max_html_follow=240,
        max_pdfs_per_detail=8,
    ),
    SourceSpec(
        doc_code="asbj_completed_drafts",
        doc_title="ASBJ 公開草案・過年度分（completed）",
        year="2026",
        index_url="https://www.asb-j.jp/jp/completed_accounting_standards.html",
        aliases=[
            "ASBJ completed accounting standards",
            "ASBJ 公開草案",
            "asbj completed",
        ],
        kind="asbj",
        allowed_domains=("www.asb-j.jp", "www.fasf-j.jp"),
        allowed_prefixes=(
            "/jp/completed_accounting_standards.html",
            "/jp/completed_accounting_standards/",
            "/jp/project/",
            "/jp/wp-content/uploads/sites/",
        ),
        max_depth=2,
        max_sections=700,
        max_html_follow=220,
        max_pdfs_per_detail=8,
    ),
    SourceSpec(
        doc_code="fsa_kigyou_toushin",
        doc_title="金融庁 企業会計審議会（答申・報告書等）",
        year="2026",
        index_url="https://www.fsa.go.jp/singi/singi_kigyou/top_tousin.html",
        aliases=[
            "企業会計審議会 答申",
            "金融庁 企業会計審議会",
            "fsa kigyou toushin",
        ],
        kind="fsa",
        allowed_domains=("www.fsa.go.jp",),
        allowed_prefixes=("/singi/singi_kigyou/",),
        max_depth=2,
        max_sections=1000,
        max_html_follow=260,
        max_pdfs_per_detail=12,
    ),
    SourceSpec(
        doc_code="fsa_kigyou_gijiroku",
        doc_title="金融庁 企業会計審議会（議事録・会議資料）",
        year="2026",
        index_url="https://www.fsa.go.jp/singi/singi_kigyou/top_gijiroku.html",
        aliases=[
            "企業会計審議会 議事録",
            "企業会計審議会 会議資料",
            "fsa kigyou gijiroku",
        ],
        kind="fsa",
        allowed_domains=("www.fsa.go.jp",),
        allowed_prefixes=("/singi/singi_kigyou/",),
        max_depth=2,
        max_sections=800,
        max_html_follow=220,
        max_pdfs_per_detail=12,
        include_pdf=False,
    ),
    SourceSpec(
        doc_code="jicpa_practical_guidelines",
        doc_title="JICPA 実務指針等公表物（実務指針）",
        year="2026",
        index_url="https://jicpa.or.jp/specialized_field/publication/practical_guidelines/",
        aliases=[
            "JICPA 実務指針",
            "日本公認会計士協会 実務指針",
            "jicpa practical guidelines",
        ],
        kind="jicpa",
        allowed_domains=("jicpa.or.jp",),
        allowed_prefixes=(
            "/specialized_field/publication/practical_guidelines/",
            "/specialized_field/",
        ),
        max_depth=1,
        max_sections=700,
        max_html_follow=300,
        max_pdfs_per_detail=1,
    ),
    SourceSpec(
        doc_code="jicpa_notification",
        doc_title="JICPA 実務指針等公表物（お知らせ・公表物）",
        year="2026",
        index_url="https://jicpa.or.jp/specialized_field/publication/notification/",
        aliases=[
            "JICPA 通知",
            "JICPA 公表物",
            "jicpa notification",
        ],
        kind="jicpa",
        allowed_domains=("jicpa.or.jp",),
        allowed_prefixes=(
            "/specialized_field/publication/notification/",
            "/specialized_field/",
        ),
        max_depth=1,
        max_sections=700,
        max_html_follow=300,
        max_pdfs_per_detail=1,
    ),
    SourceSpec(
        doc_code="jicpa_research_report",
        doc_title="JICPA 実務指針等公表物（研究報告）",
        year="2026",
        index_url="https://jicpa.or.jp/specialized_field/publication/research_report/",
        aliases=[
            "JICPA 研究報告",
            "研究報告 JICPA",
            "jicpa research report",
        ],
        kind="jicpa",
        allowed_domains=("jicpa.or.jp",),
        allowed_prefixes=(
            "/specialized_field/publication/research_report/",
            "/specialized_field/",
        ),
        max_depth=1,
        max_sections=800,
        max_html_follow=350,
        max_pdfs_per_detail=1,
    ),
    SourceSpec(
        doc_code="jicpa_research_data",
        doc_title="JICPA 実務指針等公表物（研究資料）",
        year="2026",
        index_url="https://jicpa.or.jp/specialized_field/publication/research_data/",
        aliases=[
            "JICPA 研究資料",
            "研究資料 JICPA",
            "jicpa research data",
        ],
        kind="jicpa",
        allowed_domains=("jicpa.or.jp",),
        allowed_prefixes=(
            "/specialized_field/publication/research_data/",
            "/specialized_field/",
        ),
        max_depth=1,
        max_sections=700,
        max_html_follow=320,
        max_pdfs_per_detail=1,
    ),
]


def clean_ws(s: str) -> str:
    s = (s or "").replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def reset_generated_dirs() -> None:
    for rel in [
        "enhanced",
        "text",
        "data/chunks",
        "data/resolve_lite",
        "data/resolve_lite_parts",
        "data/shards",
    ]:
        target = ROOT_DIR / rel
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)

    ensure_dir(ROOT_DIR / "source")
    src_gitignore = ROOT_DIR / "source" / ".gitignore"
    src_gitignore.write_text("# Build cache (not committed)\nraw/\n", encoding="utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fetch(url: str, timeout: int = REQUEST_TIMEOUT) -> requests.Response:
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, timeout=timeout, headers=headers)
    r.raise_for_status()
    return r


def decode_html_bytes(data: bytes, hints: list[str]) -> str:
    tried: list[str] = []
    for enc in hints + ["cp932", "shift_jis", "utf-8", "euc_jp"]:
        enc = clean_ws(enc)
        if not enc or enc in tried:
            continue
        tried.append(enc)
        try:
            return data.decode(enc)
        except Exception:
            pass
    return data.decode("utf-8", errors="replace")


def fetch_html(url: str) -> str:
    r = _fetch(url)
    hints: list[str] = []
    if r.apparent_encoding:
        hints.append(r.apparent_encoding)
    if r.encoding and "iso-8859" not in r.encoding.lower():
        hints.append(r.encoding)
    return decode_html_bytes(r.content, hints)


def normalize_url(url: str) -> str:
    p = urlparse(url)
    p = p._replace(fragment="")
    return urlunparse(p)


def is_supported_doc_url(url: str) -> bool:
    path_lc = urlparse(url).path.lower()
    return path_lc.endswith(".pdf") or path_lc.endswith(".htm") or path_lc.endswith(".html")


def is_pdf_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


def is_html_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(".htm") or path.endswith(".html")


def is_meaningful_title(text: str) -> bool:
    t = clean_ws(text)
    if not t:
        return False
    if t in NOISE_TITLE_WORDS:
        return False
    if re.fullmatch(r"[\-ー―–]+", t):
        return False
    if len(t) <= 1:
        return False
    return True


def derive_fallback_title(source_url: str) -> str:
    name = Path(urlparse(source_url).path).name
    stem = Path(name).stem
    stem = clean_ws(stem.replace("_", " ").replace("-", " "))
    if stem:
        return stem
    return name or source_url


def page_title_from_soup(soup: BeautifulSoup) -> str:
    candidates: list[str] = []
    for sel in ["main h1", "#main h1", ".content h1", "h1", "title"]:
        node = soup.select_one(sel)
        if node:
            text = clean_ws(node.get_text(" ", strip=True))
            if text:
                candidates.append(text)
    for t in candidates:
        if is_meaningful_title(t):
            return t
    return candidates[0] if candidates else ""


def url_allowed(spec: SourceSpec, url: str) -> bool:
    p = urlparse(url)
    if p.scheme not in {"http", "https"}:
        return False
    if p.netloc not in spec.allowed_domains:
        return False

    path = p.path
    for prefix in spec.allowed_prefixes:
        if prefix.endswith(".html") or prefix.endswith(".htm"):
            if path == prefix:
                return True
        if path.startswith(prefix):
            return True
    return False


def link_title_from_context(anchor_text: str, row_cells: list[str], source_url: str) -> str:
    t = clean_ws(anchor_text)
    if is_meaningful_title(t):
        return t

    for c in row_cells:
        x = clean_ws(c)
        if not is_meaningful_title(x):
            continue
        if re.search(r"PDF|KB", x, flags=re.IGNORECASE):
            continue
        return x

    return derive_fallback_title(source_url)


def add_candidate(
    candidates: dict[str, dict],
    url: str,
    title: str,
    source_page: str,
    max_sections: int,
) -> None:
    if len(candidates) >= max_sections and url not in candidates:
        return
    title = clean_ws(title)
    if not title:
        title = derive_fallback_title(url)

    current = candidates.get(url)
    if current is None:
        candidates[url] = {
            "title": title,
            "source_page": source_page,
            "source_type": "pdf" if is_pdf_url(url) else "html",
        }
        return

    # Keep better title.
    if len(title) > len(current["title"]) and is_meaningful_title(title):
        current["title"] = title


def extract_links_from_table_rows(base_url: str, soup: BeautifulSoup) -> list[tuple[str, str, list[str]]]:
    out: list[tuple[str, str, list[str]]] = []
    for tr in soup.select("table tr"):
        cells = [clean_ws(td.get_text(" ", strip=True)) for td in tr.find_all(["th", "td"])]
        for a in tr.find_all("a", href=True):
            href = normalize_url(urljoin(base_url, a.get("href", "")))
            txt = clean_ws(a.get_text(" ", strip=True))
            out.append((href, txt, cells))
    return out


def extract_links_generic(base_url: str, soup: BeautifulSoup) -> list[tuple[str, str, list[str]]]:
    out: list[tuple[str, str, list[str]]] = []
    for a in soup.select("a[href]"):
        href = normalize_url(urljoin(base_url, a.get("href", "")))
        txt = clean_ws(a.get_text(" ", strip=True))
        out.append((href, txt, []))
    return out


def collect_from_index(
    spec: SourceSpec,
    index_html: str,
    source_url: str,
    candidates: dict[str, dict],
    queue: deque[tuple[str, int]],
) -> None:
    soup = BeautifulSoup(index_html, "html.parser")

    title = page_title_from_soup(soup) or spec.doc_title
    add_candidate(candidates, normalize_url(source_url), title, source_url, spec.max_sections)

    links = extract_links_from_table_rows(source_url, soup)
    if not links:
        links = extract_links_generic(source_url, soup)

    for href, anchor_text, cells in links:
        if not url_allowed(spec, href):
            continue
        if not is_supported_doc_url(href):
            continue
        if not spec.include_pdf and is_pdf_url(href):
            continue

        title = link_title_from_context(anchor_text, cells, href)
        add_candidate(candidates, href, title, source_url, spec.max_sections)

        if is_html_url(href) and href != normalize_url(source_url):
            queue.append((href, 1))


def collect_from_detail_page(
    spec: SourceSpec,
    page_url: str,
    html: str,
    depth: int,
    candidates: dict[str, dict],
    queue: deque[tuple[str, int]],
) -> None:
    soup = BeautifulSoup(html, "html.parser")
    p_title = page_title_from_soup(soup) or derive_fallback_title(page_url)
    add_candidate(candidates, page_url, p_title, page_url, spec.max_sections)

    pdf_added = 0

    # Table-first, fallback generic for less noise.
    links = extract_links_from_table_rows(page_url, soup)
    if not links:
        links = extract_links_generic(page_url, soup)

    for href, anchor_text, cells in links:
        if not url_allowed(spec, href):
            continue
        if not is_supported_doc_url(href):
            continue

        if is_pdf_url(href):
            if not spec.include_pdf:
                continue
            if pdf_added >= spec.max_pdfs_per_detail:
                continue
            title = link_title_from_context(anchor_text, cells, href)
            add_candidate(candidates, href, title, page_url, spec.max_sections)
            pdf_added += 1
            continue

        if is_html_url(href):
            title = link_title_from_context(anchor_text, cells, href)
            add_candidate(candidates, href, title, page_url, spec.max_sections)
            if depth < spec.max_depth:
                queue.append((href, depth + 1))


def extract_sections(spec: SourceSpec, index_html: str) -> list[Section]:
    candidates: dict[str, dict] = {}
    queue: deque[tuple[str, int]] = deque()

    collect_from_index(spec, index_html, spec.index_url, candidates, queue)

    visited_html: set[str] = set()
    follow_count = 0

    while queue:
        page_url, depth = queue.popleft()
        page_url = normalize_url(page_url)

        if follow_count >= spec.max_html_follow:
            break
        if page_url in visited_html:
            continue
        if not url_allowed(spec, page_url):
            continue
        if not is_html_url(page_url):
            continue

        visited_html.add(page_url)
        follow_count += 1

        try:
            html = fetch_html(page_url)
        except Exception as e:
            print(f"[warn] failed to fetch html: {page_url} ({e})")
            continue

        collect_from_detail_page(spec, page_url, html, depth, candidates, queue)

    # Keep insertion-like order with deterministic sort by URL path for stable IDs.
    sorted_urls = sorted(candidates.keys(), key=lambda u: (urlparse(u).path, u))

    sections: list[Section] = []
    for i, u in enumerate(sorted_urls, start=1):
        info = candidates[u]
        sections.append(
            Section(
                section_no=i,
                section_id=f"s{i:04d}",
                section_title=info["title"],
                source_url=u,
                source_type=info["source_type"],
            )
        )

    return sections


def download_file(url: str, out_path: Path) -> None:
    if out_path.exists() and out_path.stat().st_size > 0:
        return
    ensure_dir(out_path.parent)
    headers = {"User-Agent": USER_AGENT}
    with requests.get(url, timeout=(20, 45), headers=headers, stream=True) as r:
        r.raise_for_status()
        clen = r.headers.get("Content-Length")
        if clen and clen.isdigit() and int(clen) > MAX_PDF_BYTES:
            raise ValueError(f"PDF too large ({int(clen)} bytes > {MAX_PDF_BYTES})")

        total = 0
        with out_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    f.close()
                    out_path.unlink(missing_ok=True)
                    raise ValueError(f"PDF too large while streaming ({total} bytes > {MAX_PDF_BYTES})")
                f.write(chunk)


def save_html_snapshot(url: str, out_path: Path) -> str:
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path.read_text(encoding="utf-8", errors="replace")
    ensure_dir(out_path.parent)
    html = fetch_html(url)
    out_path.write_text(html, encoding="utf-8", errors="replace")
    return html


def normalize_line(line: str) -> str:
    t = clean_ws(line)
    t = t.replace("’", "'").replace("“", '"').replace("”", '"')
    return t


def extract_pdf_lines(pdf_path: Path) -> list[str]:
    out: list[str] = []
    reader = PdfReader(str(pdf_path))
    for page_no, page in enumerate(reader.pages, start=1):
        if page_no > MAX_PDF_PAGES:
            out.append(f"※ PDFが長大なため先頭{MAX_PDF_PAGES}ページまでを抽出")
            break
        txt = page.extract_text() or ""
        lines: list[str] = []
        for raw in txt.splitlines():
            ln = normalize_line(raw)
            if not ln:
                continue
            if ln in COMMON_NOISE_LINES:
                continue
            if re.fullmatch(r"[0-9]{1,4}", ln):
                continue
            lines.append(ln)
        if lines:
            out.append(f"[page {page_no}]")
            out.extend(lines)
    return out


def extract_html_lines_from_text(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    body = (
        soup.select_one("main")
        or soup.select_one("#main")
        or soup.select_one(".content")
        or soup.select_one("#bodyArea")
        or soup.body
        or soup
    )

    for tag in body.select("script, style, noscript"):
        tag.extract()

    text = body.get_text("\n")
    out: list[str] = []
    for raw in text.splitlines():
        ln = normalize_line(raw)
        if not ln:
            continue
        if ln in COMMON_NOISE_LINES:
            continue
        if re.fullmatch(r"[0-9]{1,4}", ln):
            continue
        out.append(ln)

    deduped: list[str] = []
    prev = ""
    for ln in out:
        if ln == prev:
            continue
        deduped.append(ln)
        prev = ln
    return deduped


def split_lines(lines: list[str], max_chars: int) -> list[list[str]]:
    if not lines:
        return []
    parts: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0

    for ln in lines:
        add = len(ln) + 1
        if cur and cur_len + add > max_chars:
            parts.append(cur)
            cur = [ln]
            cur_len = add
        else:
            cur.append(ln)
            cur_len += add

    if cur:
        parts.append(cur)

    if len(parts) >= 2:
        tail_len = sum(len(x) + 1 for x in parts[-1])
        if tail_len < int(max_chars * 0.25):
            parts[-2].extend(parts[-1])
            parts.pop()

    return parts


def to_paragraphs(lines: list[str]) -> list[str]:
    paras: list[str] = []
    buf: list[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        if not buf:
            return
        paras.append(clean_ws(" ".join(buf)))
        buf = []
        buf_len = 0

    for ln in lines:
        if ln.startswith("[page "):
            flush()
            paras.append(ln)
            continue

        if buf_len + len(ln) > 220:
            flush()

        buf.append(ln)
        buf_len += len(ln) + 1

        if ln.endswith(("。", "．", ".", "!", "?", "）", ")", "」", "】")):
            flush()

    flush()

    cleaned = [p for p in (clean_ws(p) for p in paras) if p]
    return cleaned or [clean_ws(" ".join(lines))]


def build_items(spec: SourceSpec, sections: list[Section]) -> list[Item]:
    out: list[Item] = []
    raw_dir = RAW_DIR / spec.doc_code
    ensure_dir(raw_dir)

    for s in sections:
        filename = Path(urlparse(s.source_url).path).name
        if not filename:
            filename = f"{s.section_id}.html"
        local = raw_dir / filename

        try:
            if s.source_type == "pdf":
                download_file(s.source_url, local)
                lines = extract_pdf_lines(local)
            else:
                html = save_html_snapshot(s.source_url, local)
                lines = extract_html_lines_from_text(html)
        except Exception as e:
            lines = [f"※ 取得/抽出に失敗しました: {e}", f"原本URL: {s.source_url}"]

        lines = [ln for ln in lines if ln]
        if not lines:
            lines = [f"※ テキスト抽出結果が空でした。原本を参照してください: {s.source_url}"]

        parts = split_lines(lines, MAX_PART_CHARS)
        part_total = len(parts)
        for idx, part_lines in enumerate(parts, start=1):
            item_id = s.section_id if part_total == 1 else f"{s.section_id}-part-{idx:02d}"
            item_title = s.section_title if part_total == 1 else f"{s.section_title} (part {idx}/{part_total})"
            out.append(
                Item(
                    doc_code=spec.doc_code,
                    doc_title=spec.doc_title,
                    year=spec.year,
                    item_id=item_id,
                    item_title=item_title,
                    section_id=s.section_id,
                    section_no=s.section_no,
                    section_title=s.section_title,
                    source_url=s.source_url,
                    source_type=s.source_type,
                    part_index=idx,
                    part_total=part_total,
                    lines=to_paragraphs(part_lines),
                )
            )

    return out


def write_text_item(item: Item) -> None:
    out_path = ROOT_DIR / "text" / item.doc_code / f"{item.item_id}.txt"
    ensure_dir(out_path.parent)
    url = f"{BASE_URL}/enhanced/{item.doc_code}/{item.item_id}.html"

    lines = [
        f"doc: {item.doc_title} ({item.doc_code})",
        f"year: {item.year}",
        f"item: {item.item_id} / title: {item.item_title}",
        f"section: {item.section_no} / {item.section_title}",
        f"source_type: {item.source_type}",
        f"source_url: {item.source_url}",
        f"url: {url}",
        "",
    ]
    for i, p in enumerate(item.lines, start=1):
        lines.append(f"[p{i}] {p}")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8", errors="replace")


def write_html_item(item: Item) -> None:
    out_path = ROOT_DIR / "enhanced" / item.doc_code / f"{item.item_id}.html"
    ensure_dir(out_path.parent)
    text_href = f"../../text/{item.doc_code}/{item.item_id}.txt"

    lis = []
    for i, p in enumerate(item.lines, start=1):
        lis.append(f'      <li id="p{i}">{escape(p)}</li>')

    html = f"""<!doctype html>
<html lang=\"ja\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{escape(item.item_title)}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, 'Segoe UI', sans-serif; line-height: 1.7; max-width: 920px; margin: 0 auto; padding: 24px; }}
    .meta {{ color: #555; font-size: 14px; }}
    code {{ background: #f3f3f3; padding: 1px 5px; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>{escape(item.item_title)}</h1>
  <p class=\"meta\">{escape(item.doc_title)} / item: <code>{escape(item.item_id)}</code></p>
  <p class=\"meta\">source: <a href=\"{escape(item.source_url)}\">{escape(item.source_url)}</a></p>
  <p class=\"meta\">text: <a href=\"{escape(text_href)}\">{escape(text_href)}</a></p>
  <ol>
{chr(10).join(lis)}
  </ol>
</body>
</html>
"""
    out_path.write_text(html, encoding="utf-8", errors="replace")


def write_doc_index(spec: SourceSpec, items: list[Item]) -> None:
    out_path = ROOT_DIR / "enhanced" / spec.doc_code / "index.html"
    ensure_dir(out_path.parent)

    lis = []
    for it in items:
        lis.append(
            "<li>"
            f"<a href=\"{escape(it.item_id)}.html\">{escape(it.item_title)}</a>"
            f" (<code>{escape(it.item_id)}</code>)"
            "</li>"
        )

    html = f"""<!doctype html>
<html lang=\"ja\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{escape(spec.doc_title)}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, 'Segoe UI', sans-serif; line-height: 1.7; max-width: 920px; margin: 0 auto; padding: 24px; }}
    .meta {{ color: #555; }}
    code {{ background: #f3f3f3; padding: 0 6px; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>{escape(spec.doc_title)}</h1>
  <p class=\"meta\">doc_code: <code>{escape(spec.doc_code)}</code> / items: {len(items)}</p>
  <ul>
    {chr(10).join(lis)}
  </ul>
</body>
</html>
"""
    out_path.write_text(html, encoding="utf-8")


def write_doc_aliases(specs: list[SourceSpec]) -> None:
    out = ROOT_DIR / "data" / "doc_aliases.json"
    ensure_dir(out.parent)

    aliases: dict[str, str] = {}
    for s in specs:
        aliases[s.doc_code] = s.doc_code
        aliases[s.doc_title] = s.doc_code
        for a in s.aliases:
            aliases[a] = s.doc_code

    out.write_text(json.dumps(aliases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_docs_index_tsv(specs: list[SourceSpec], items_by_doc: dict[str, list[Item]]) -> None:
    out = ROOT_DIR / "data" / "docs_index.tsv"
    rows = ["doc_code\tdoc_title\titem_count"]
    for s in specs:
        rows.append(f"{s.doc_code}\t{s.doc_title}\t{len(items_by_doc.get(s.doc_code, []))}")
    out.write_text("\n".join(rows) + "\n", encoding="utf-8")


def write_resolve_lite(specs: list[SourceSpec], items_by_doc: dict[str, list[Item]]) -> None:
    base = ROOT_DIR / "data" / "resolve_lite"
    parts_base = ROOT_DIR / "data" / "resolve_lite_parts"
    ensure_dir(base)
    ensure_dir(parts_base)

    index_docs: list[dict] = []

    for s in specs:
        items = items_by_doc.get(s.doc_code, [])
        part_dir = parts_base / s.doc_code
        ensure_dir(part_dir)

        part_total = (len(items) + MAX_RESOLVE_ITEMS_PER_PART - 1) // MAX_RESOLVE_ITEMS_PER_PART
        part_refs: list[dict] = []

        part_no = 0
        for i in range(0, len(items), MAX_RESOLVE_ITEMS_PER_PART):
            chunk = items[i : i + MAX_RESOLVE_ITEMS_PER_PART]
            part_no += 1
            part_file = f"part-{part_no:03d}.json"
            rel = f"data/resolve_lite_parts/{s.doc_code}/{part_file}"

            part_data = {
                "doc_code": s.doc_code,
                "doc_title": s.doc_title,
                "year": s.year,
                "part": part_no,
                "part_total": part_total,
                "count": len(chunk),
                "items": [
                    {
                        "item_id": it.item_id,
                        "title": it.item_title,
                        "section_id": it.section_id,
                        "section_no": it.section_no,
                        "section_title": it.section_title,
                        "part_index": it.part_index,
                        "part_total": it.part_total,
                        "source_url": it.source_url,
                        "text_url": f"{BASE_URL}/text/{it.doc_code}/{it.item_id}.txt",
                        "enhanced_url": f"{BASE_URL}/enhanced/{it.doc_code}/{it.item_id}.html",
                    }
                    for it in chunk
                ],
            }
            (part_dir / part_file).write_text(
                json.dumps(part_data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )

            first = chunk[0]
            last = chunk[-1]
            part_refs.append(
                {
                    "part": part_no,
                    "count": len(chunk),
                    "first_item_id": first.item_id,
                    "last_item_id": last.item_id,
                    "file": rel,
                    "url": f"{BASE_URL}/{rel}",
                }
            )

        data = {
            "doc_code": s.doc_code,
            "doc_title": s.doc_title,
            "year": s.year,
            "count": len(items),
            "part_size": MAX_RESOLVE_ITEMS_PER_PART,
            "part_count": part_total,
            "parts": part_refs,
        }
        (base / f"{s.doc_code}.json").write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

        index_docs.append(
            {
                "doc_code": s.doc_code,
                "doc_title": s.doc_title,
                "year": s.year,
                "count": len(items),
                "url": f"{BASE_URL}/data/resolve_lite/{s.doc_code}.json",
            }
        )

    index_data = {
        "generated_at": now_iso(),
        "base_url": BASE_URL,
        "docs": index_docs,
    }
    (base / "index.json").write_text(
        json.dumps(index_data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def write_chunks(specs: list[SourceSpec], items_by_doc: dict[str, list[Item]]) -> None:
    out_dir = ROOT_DIR / "data" / "chunks"
    ensure_dir(out_dir)

    for s in specs:
        out_path = out_dir / f"{s.doc_code}.jsonl"
        lines: list[str] = []
        for it in items_by_doc.get(s.doc_code, []):
            text = "\n".join(it.lines)
            row = {
                "id": f"{it.doc_code}:{it.item_id}",
                "doc_code": it.doc_code,
                "doc_title": it.doc_title,
                "item_id": it.item_id,
                "title": it.item_title,
                "section_id": it.section_id,
                "section_no": it.section_no,
                "section_title": it.section_title,
                "source_url": it.source_url,
                "url": f"{BASE_URL}/enhanced/{it.doc_code}/{it.item_id}.html",
                "text": text,
            }
            lines.append(json.dumps(row, ensure_ascii=False))
        out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def snippet(text: str, n: int) -> str:
    t = clean_ws(text)
    if len(t) <= n:
        return t
    return t[: n - 1] + "…"


def write_shards(items: list[Item]) -> None:
    out_dir = ROOT_DIR / "data" / "shards"
    ensure_dir(out_dir)

    rows: list[str] = []
    for it in items:
        text_url = f"{BASE_URL}/text/{it.doc_code}/{it.item_id}.txt"
        enhanced_url = f"{BASE_URL}/enhanced/{it.doc_code}/{it.item_id}.html"
        text_snippet = snippet(" ".join(it.lines), SHARD_SNIPPET_CHARS)
        rows.append(
            "\t".join(
                [
                    it.doc_code,
                    it.item_id,
                    it.item_title.replace("\t", " "),
                    text_snippet.replace("\t", " "),
                    text_url,
                    enhanced_url,
                ]
            )
        )

    shards_meta: list[dict] = []

    for i in range(0, len(rows), MAX_SHARD_ROWS):
        chunk = rows[i : i + MAX_SHARD_ROWS]
        shard_no = (i // MAX_SHARD_ROWS) + 1
        fname = f"shard-{shard_no:03d}.txt"
        content = [
            "doc_code\titem_id\ttitle\tsnippet\ttext_url\tenhanced_url",
            *chunk,
            "",
        ]
        (out_dir / fname).write_text("\n".join(content), encoding="utf-8")

        first = chunk[0].split("\t")
        last = chunk[-1].split("\t")
        shards_meta.append(
            {
                "file": f"data/shards/{fname}",
                "count": len(chunk),
                "first": {"doc_code": first[0], "item_id": first[1], "title": first[2]},
                "last": {"doc_code": last[0], "item_id": last[1], "title": last[2]},
                "url": f"{BASE_URL}/data/shards/{fname}",
            }
        )

    idx = {
        "generated_at": now_iso(),
        "base_url": BASE_URL,
        "shard_count": len(shards_meta),
        "row_count": len(rows),
        "shards": shards_meta,
    }
    (ROOT_DIR / "data" / "shards_index.json").write_text(
        json.dumps(idx, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def write_quickstart(specs: list[SourceSpec], items_by_doc: dict[str, list[Item]]) -> None:
    lines = [
        "# AI向け 会計基準DB クイックスタート（1URL≒1万トークン制約向け）",
        "#",
        "# 基本方針:",
        "# 1) 小さい索引で item_id を決める",
        "# 2) resolve_liteのpartを開いて item_id を決める",
        "# 3) 本文は text/{doc_code}/{item_id}.txt を読む",
        "# 4) 引用URLが必要なときだけ enhanced/...#pX を使う",
        "",
        "# 入口",
        f"# - {BASE_URL}/llms.txt",
        f"# - {BASE_URL}/data/resolve_lite/index.json",
        f"# - {BASE_URL}/data/shards_index.json",
        "",
        "# 本文URL",
        f"# - {BASE_URL}/text/{{doc_code}}/{{item_id}}.txt",
        "",
        "# 利用可能な doc_code",
    ]
    for s in specs:
        lines.append(f"# - {s.doc_code} : {s.doc_title} ({len(items_by_doc.get(s.doc_code, []))} items)")

    for s in specs:
        items = items_by_doc.get(s.doc_code, [])
        if not items:
            continue
        sample = items[0]
        lines.extend(
            [
                "",
                f"# 例: {s.doc_title}",
                f"# - {BASE_URL}/text/{sample.doc_code}/{sample.item_id}.txt",
                f"# - {BASE_URL}/enhanced/{sample.doc_code}/{sample.item_id}.html",
            ]
        )

    (ROOT_DIR / "quickstart.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_llms(specs: list[SourceSpec], items_by_doc: dict[str, list[Item]]) -> None:
    lines = [
        "# AI向け 会計基準DB",
        "# Base URL: https://jplawdb.github.io/html-preview/ai-accounting-db/",
        "",
        "入口（推奨）",
        "- `quickstart.txt`",
        "- `data/resolve_lite/index.json`",
        "- `data/shards_index.json`",
        "",
        "高速フロー（10kトークン制約向け）",
        "1) `data/resolve_lite/index.json` で doc_code を選ぶ",
        "2) `data/resolve_lite/{doc_code}.json` で part URL を選ぶ",
        "3) `data/resolve_lite_parts/{doc_code}/part-XXX.json` で item_id を選ぶ",
        "4) `text/{doc_code}/{item_id}.txt` を読む",
        "5) 根拠URLが必要なら `enhanced/{doc_code}/{item_id}.html#pX` を使う",
        "",
        "収録ドキュメント",
    ]

    for s in specs:
        lines.append(f"- `{s.doc_code}`: {s.doc_title} ({len(items_by_doc.get(s.doc_code, []))}件)")

    (ROOT_DIR / "llms.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_index_html(specs: list[SourceSpec], items_by_doc: dict[str, list[Item]]) -> None:
    lis = []
    for s in specs:
        count = len(items_by_doc.get(s.doc_code, []))
        lis.append(
            "<li>"
            f"<a href=\"enhanced/{escape(s.doc_code)}/index.html\">{escape(s.doc_title)}</a> "
            f"(<code>{escape(s.doc_code)}</code>, {count}件)"
            "</li>"
        )

    html = f"""<!doctype html>
<html lang=\"ja\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>AI向け 会計基準DB</title>
  <meta name=\"description\" content=\"会計基準関連資料をAI向けに分割・構造化したDB\" />
  <link rel=\"alternate\" type=\"text/plain\" href=\"llms.txt\" title=\"LLM Site Map\" />
  <style>
    body {{ font-family: system-ui, -apple-system, 'Segoe UI', sans-serif; line-height: 1.7; max-width: 920px; margin: 0 auto; padding: 24px; }}
    .meta {{ color: #555; }}
    code {{ background: #f3f3f3; padding: 0 6px; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>AI向け 会計基準DB</h1>
  <p class=\"meta\">会計基準関連資料を、1URL=短い本文単位で再構成しています。</p>
  <ul>
    <li><a href=\"quickstart.txt\">quickstart.txt</a></li>
    <li><a href=\"llms.txt\">llms.txt</a></li>
    <li><a href=\"data/resolve_lite/index.json\">data/resolve_lite/index.json</a></li>
    <li><a href=\"data/shards_index.json\">data/shards_index.json</a></li>
  </ul>
  <h2>ドキュメント一覧</h2>
  <ul>
    {chr(10).join(lis)}
  </ul>
</body>
</html>
"""
    (ROOT_DIR / "index.html").write_text(html, encoding="utf-8")


def write_robots() -> None:
    txt = """User-agent: *
Allow: /

Sitemap: https://jplawdb.github.io/html-preview/ai-accounting-db/sitemap.xml
"""
    (ROOT_DIR / "robots.txt").write_text(txt, encoding="utf-8")


def write_sitemap() -> None:
    urls: list[str] = [
        f"{BASE_URL}/",
        f"{BASE_URL}/index.html",
        f"{BASE_URL}/llms.txt",
        f"{BASE_URL}/quickstart.txt",
        f"{BASE_URL}/robots.txt",
        f"{BASE_URL}/data/doc_aliases.json",
        f"{BASE_URL}/data/docs_index.tsv",
        f"{BASE_URL}/data/resolve_lite/index.json",
        f"{BASE_URL}/data/shards_index.json",
    ]

    for rel in [
        "enhanced",
        "text",
        "data/resolve_lite",
        "data/resolve_lite_parts",
        "data/chunks",
        "data/shards",
    ]:
        base = ROOT_DIR / rel
        for p in sorted(base.rglob("*")):
            if p.is_dir():
                continue
            sub = p.relative_to(ROOT_DIR).as_posix()
            urls.append(f"{BASE_URL}/{sub}")

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in sorted(set(urls)):
        lines.append("  <url><loc>" + escape(u) + "</loc></url>")
    lines.append("</urlset>")
    (ROOT_DIR / "sitemap.xml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    reset_generated_dirs()
    ensure_dir(RAW_DIR)

    items_by_doc: dict[str, list[Item]] = {}

    for spec in SOURCES:
        print(f"[fetch] {spec.doc_code}: {spec.index_url}")
        try:
            index_html = fetch_html(spec.index_url)
        except Exception as e:
            print(f"[error] failed to fetch index for {spec.doc_code}: {e}")
            items_by_doc[spec.doc_code] = []
            continue

        sections = extract_sections(spec, index_html)
        print(f"[sections] {spec.doc_code}: {len(sections)}")
        items = build_items(spec, sections)
        print(f"[items] {spec.doc_code}: {len(items)}")

        for item in items:
            write_text_item(item)
            write_html_item(item)

        write_doc_index(spec, items)
        items_by_doc[spec.doc_code] = items

    all_items = sorted(
        [it for items in items_by_doc.values() for it in items],
        key=lambda x: (x.doc_code, x.item_id),
    )

    write_doc_aliases(SOURCES)
    write_docs_index_tsv(SOURCES, items_by_doc)
    write_resolve_lite(SOURCES, items_by_doc)
    write_chunks(SOURCES, items_by_doc)
    write_shards(all_items)
    write_quickstart(SOURCES, items_by_doc)
    write_llms(SOURCES, items_by_doc)
    write_index_html(SOURCES, items_by_doc)
    write_robots()
    write_sitemap()

    print("[done] ai-accounting-db artifacts generated")


if __name__ == "__main__":
    main()
