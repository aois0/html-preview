#!/usr/bin/env python3
"""
Build AI-optimized artifacts for NTA guide pamphlets under 10k-token read constraints.

Current corpus (latest editions as of 2026-02-11):
  - 法人税のあらましと申告の手引（令和7年版）
  - 源泉徴収のあらまし（令和8年版）

Output layout (ai-nta-guide-db/):
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

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
import json
import re
import shutil
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from pypdf import PdfReader
import requests


BASE_URL = "https://jplawdb.github.io/html-preview/ai-nta-guide-db"
ROOT_DIR = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT_DIR / "source" / "raw"

MAX_PART_CHARS = 6500
MAX_SHARD_ROWS = 12
SHARD_SNIPPET_CHARS = 120
MAX_RESOLVE_ITEMS_PER_PART = 15

COMMON_NOISE_LINES = {
    "このページの先頭へ",
    "ページの先頭へ",
    "ホーム",
    "国税庁ホームページ",
    "戻る",
}


@dataclass(frozen=True)
class SourceSpec:
    doc_code: str
    doc_title: str
    year: str
    index_url: str
    link_prefix: str
    aliases: list[str]
    manual_sections: tuple[tuple[str, str], ...] = ()


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
        doc_code="hojin_aramashi_2025",
        doc_title="法人税のあらましと申告の手引（令和7年版）",
        year="2025",
        index_url="https://www.nta.go.jp/publication/pamph/hojin/aramashi2025/01.htm",
        link_prefix="/publication/pamph/hojin/aramashi2025/",
        aliases=[
            "法人税のあらましと申告の手引",
            "法人税 手引",
            "法人税 あらまし",
            "hojin aramashi",
        ],
    ),
    SourceSpec(
        doc_code="gensen_aramashi_2026",
        doc_title="源泉徴収のあらまし（令和8年版）",
        year="2026",
        index_url="https://www.nta.go.jp/publication/pamph/gensen/aramashi2026/index.htm",
        link_prefix="/publication/pamph/gensen/aramashi2026/",
        aliases=[
            "源泉徴収のあらまし",
            "源泉 あらまし",
            "源泉徴収 手引",
            "gensen aramashi",
        ],
    ),
    SourceSpec(
        doc_code="gensen_shikata_2026",
        doc_title="源泉徴収のしかた（令和8年版）",
        year="2026",
        index_url="https://www.nta.go.jp/publication/pamph/gensen/shikata_r08/01.htm",
        link_prefix="/publication/pamph/gensen/shikata_r08/",
        aliases=[
            "源泉徴収のしかた",
            "源泉 しかた",
            "gensen shikata",
        ],
    ),
    SourceSpec(
        doc_code="nencho_shikata_2025",
        doc_title="年末調整のしかた（令和7年分）",
        year="2025",
        index_url="https://www.nta.go.jp/publication/pamph/gensen/nencho2025/01.htm",
        link_prefix="/publication/pamph/gensen/nencho2025/",
        aliases=[
            "年末調整のしかた",
            "年調のしかた",
            "nencho shikata",
        ],
    ),
    SourceSpec(
        doc_code="hotei_tebiki_2025",
        doc_title="法定調書の作成と提出の手引（令和7年分）",
        year="2025",
        index_url="https://www.nta.go.jp/publication/pamph/hotei/tebiki2025/index.htm",
        link_prefix="/publication/pamph/hotei/tebiki2025/",
        aliases=[
            "法定調書の作成と提出の手引",
            "法定調書 手引",
            "hotei tebiki",
        ],
    ),
    SourceSpec(
        doc_code="inshi_tebiki_2025",
        doc_title="印紙税の手引（令和7年5月）",
        year="2025",
        index_url="https://www.nta.go.jp/publication/pamph/inshi/tebiki/01.htm",
        link_prefix="/publication/pamph/inshi/tebiki/",
        aliases=[
            "印紙税の手引",
            "印紙税 手引",
            "inshi tebiki",
        ],
    ),
    SourceSpec(
        doc_code="hojin_kaisei_gaiyo_2025",
        doc_title="法人税関係法令の改正の概要（令和7年度）",
        year="2025",
        index_url="https://www.nta.go.jp/publication/pamph/hojin/kaisei_gaiyo2025/01.htm",
        link_prefix="/publication/pamph/hojin/kaisei_gaiyo2025/",
        aliases=[
            "法人税関係法令の改正の概要",
            "法人税 改正の概要",
            "hojin kaisei gaiyo",
        ],
    ),
    SourceSpec(
        doc_code="hojin_shinkoku_besshyo",
        doc_title="法人税及び地方法人税の申告（別表等）",
        year="2025",
        index_url="https://www.nta.go.jp/taxes/tetsuzuki/shinsei/annai/hojin/shinkoku/01.htm",
        link_prefix="/taxes/tetsuzuki/shinsei/annai/hojin/shinkoku/",
        aliases=[
            "法人税及び地方法人税の申告（別表等）",
            "法人税申告書別表",
            "hojin shinkoku besshyo",
        ],
        manual_sections=(
            ("法人税及び地方法人税の申告（法人税申告書別表等）", "https://www.nta.go.jp/taxes/tetsuzuki/shinsei/annai/hojin/shinkoku/01.htm"),
            ("令和7年4月以降に提供した法人税等各種別表関係", "https://www.nta.go.jp/taxes/tetsuzuki/shinsei/annai/hojin/shinkoku/itiran2025/01.htm"),
        ),
    ),
    SourceSpec(
        doc_code="etax_tetsuzuki6",
        doc_title="e-Tax 利用可能手続一覧（法人税確定申告等）",
        year="2026",
        index_url="https://www.e-tax.nta.go.jp/tetsuzuki/tetsuzuki6.htm",
        link_prefix="/tetsuzuki/",
        aliases=[
            "e-Tax 利用可能手続一覧",
            "e-Tax 法人税確定申告",
            "etax tetsuzuki6",
        ],
        manual_sections=(
            ("利用可能手続一覧", "https://www.e-tax.nta.go.jp/tetsuzuki/tetsuzuki6.htm"),
            ("ファイル形式を定める国税庁告示（令和7年国税庁告示第12号）の概要", "https://www.e-tax.nta.go.jp/hojin/gimuka/r7_12go_outline_2025.pdf"),
            ("法人税申告書別表等（明細記載を要する部分）のCSV形式データの作成方法", "https://www.e-tax.nta.go.jp/hojin/gimuka/csv_jyoho5.htm"),
        ),
    ),
    SourceSpec(
        doc_code="hojin_shohi_kakikata_ippan",
        doc_title="法人用 消費税及び地方消費税の申告書（一般用）の書き方",
        year="2020",
        index_url="https://www.nta.go.jp/publication/pamph/shohi/kaisei/yoshiki/pdf/202008_01.pdf",
        link_prefix="/publication/pamph/shohi/kaisei/yoshiki/pdf/",
        aliases=[
            "法人用 消費税 申告書 一般用 書き方",
            "消費税申告書 一般用",
        ],
        manual_sections=(
            ("法人用 消費税及び地方消費税の申告書（一般用）の書き方", "https://www.nta.go.jp/publication/pamph/shohi/kaisei/yoshiki/pdf/202008_01.pdf"),
        ),
    ),
    SourceSpec(
        doc_code="hojin_shohi_kakikata_kani",
        doc_title="法人用 消費税及び地方消費税の申告書（簡易課税用）の書き方",
        year="2020",
        index_url="https://www.nta.go.jp/publication/pamph/shohi/kaisei/yoshiki/pdf/202008_02.pdf",
        link_prefix="/publication/pamph/shohi/kaisei/yoshiki/pdf/",
        aliases=[
            "法人用 消費税 申告書 簡易課税用 書き方",
            "消費税申告書 簡易課税用",
        ],
        manual_sections=(
            ("法人用 消費税及び地方消費税の申告書（簡易課税用）の書き方", "https://www.nta.go.jp/publication/pamph/shohi/kaisei/yoshiki/pdf/202008_02.pdf"),
        ),
    ),
    SourceSpec(
        doc_code="invoice_oshirase_2025",
        doc_title="インボイス制度に関するお知らせ（令和7年4月）",
        year="2025",
        index_url="https://www.nta.go.jp/taxes/shiraberu/zeimokubetsu/shohi/keigenzeiritsu/pdf/0024004-035.pdf",
        link_prefix="/taxes/shiraberu/zeimokubetsu/shohi/keigenzeiritsu/pdf/",
        aliases=[
            "インボイス制度に関するお知らせ",
            "インボイス お知らせ",
            "invoice oshirase",
        ],
        manual_sections=(
            ("インボイス制度に関するお知らせ（令和7年4月）", "https://www.nta.go.jp/taxes/shiraberu/zeimokubetsu/shohi/keigenzeiritsu/pdf/0024004-035.pdf"),
        ),
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


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def decode_html_bytes(data: bytes, hints: list[str]) -> str:
    tried: list[str] = []
    for enc in hints + ["cp932", "shift_jis", "utf-8", "euc_jp"]:
        if not enc or enc in tried:
            continue
        tried.append(enc)
        try:
            return data.decode(enc)
        except Exception:
            pass
    return data.decode("utf-8", errors="replace")


def fetch_html(url: str) -> str:
    r = requests.get(url, timeout=40)
    r.raise_for_status()
    hints: list[str] = []
    if r.apparent_encoding:
        hints.append(r.apparent_encoding)
    if r.encoding and "iso-8859" not in r.encoding.lower():
        hints.append(r.encoding)
    return decode_html_bytes(r.content, hints)


def download_file(url: str, out_path: Path) -> None:
    if out_path.exists() and out_path.stat().st_size > 0:
        return
    ensure_dir(out_path.parent)
    r = requests.get(url, timeout=80)
    r.raise_for_status()
    out_path.write_bytes(r.content)


def derive_row_title(cells: list[str], anchor_text: str) -> str:
    filtered: list[str] = []
    for c in cells:
        t = clean_ws(c)
        if not t:
            continue
        if t in {"◎", "○", "●", "-", "－", "―", "ー", "&nbsp;"}:
            continue
        if re.search(r"PDF|KB", t, flags=re.IGNORECASE):
            continue
        filtered.append(t)

    if filtered:
        if len(filtered) >= 2 and re.fullmatch(r"第\s*[0-9０-９]+", filtered[0]):
            return clean_ws(f"{filtered[0]} {filtered[1]}")
        return clean_ws(filtered[0])

    t = clean_ws(anchor_text)
    if not t or re.search(r"PDF|KB", t, flags=re.IGNORECASE):
        return ""
    return t


def derive_fallback_title(source_url: str) -> str:
    name = Path(urlparse(source_url).path).name
    stem = Path(name).stem
    stem = clean_ws(stem.replace("_", " ").replace("-", " "))
    if not stem:
        return name or source_url
    if re.fullmatch(r"[0-9]{1,4}", stem):
        return f"資料 {stem}"
    return stem


def build_manual_sections(spec: SourceSpec) -> list[Section]:
    out: list[Section] = []
    for i, (title, source_url) in enumerate(spec.manual_sections, start=1):
        parsed = urlparse(source_url)
        stype = "pdf" if parsed.path.lower().endswith(".pdf") else "html"
        out.append(
            Section(
                section_no=i,
                section_id=f"s{i:03d}",
                section_title=clean_ws(title) or derive_fallback_title(source_url),
                source_url=source_url,
                source_type=stype,
            )
        )
    return out


def extract_sections(spec: SourceSpec, html: str) -> list[Section]:
    if spec.manual_sections:
        return build_manual_sections(spec)

    soup = BeautifulSoup(html, "html.parser")
    sections: list[Section] = []
    seen_urls: set[str] = set()
    index_parsed = urlparse(spec.index_url)

    for tr in soup.select("table tr"):
        cells = [clean_ws(td.get_text(" ", strip=True)) for td in tr.find_all(["th", "td"])]
        for a in tr.find_all("a", href=True):
            source_url = urljoin(spec.index_url, a.get("href", ""))
            parsed = urlparse(source_url)
            path_lc = parsed.path.lower()
            if spec.link_prefix not in parsed.path:
                continue
            if not (path_lc.endswith(".pdf") or path_lc.endswith(".htm") or path_lc.endswith(".html")):
                continue
            if source_url in seen_urls:
                continue

            title = derive_row_title(cells, a.get_text(" ", strip=True))
            if not title:
                title = derive_fallback_title(source_url)

            seen_urls.add(source_url)
            n = len(sections) + 1
            sections.append(
                Section(
                    section_no=n,
                    section_id=f"s{n:03d}",
                    section_title=title,
                    source_url=source_url,
                    source_type="pdf" if path_lc.endswith(".pdf") else "html",
                )
            )

    # Also collect non-table anchors under link_prefix to avoid missing 00.pdf / A.pdf / all.pdf etc.
    for a in soup.find_all("a", href=True):
        source_url = urljoin(spec.index_url, a.get("href", ""))
        parsed = urlparse(source_url)
        path_lc = parsed.path.lower()
        if spec.link_prefix not in parsed.path:
            continue
        if parsed.fragment:
            continue
        if not (path_lc.endswith(".pdf") or path_lc.endswith(".htm") or path_lc.endswith(".html")):
            continue
        if parsed.path == index_parsed.path:
            continue
        if source_url in seen_urls:
            continue

        title = clean_ws(a.get_text(" ", strip=True))
        if not title or re.search(r"PDF|KB", title, flags=re.IGNORECASE):
            title = derive_fallback_title(source_url)

        seen_urls.add(source_url)
        n = len(sections) + 1
        sections.append(
            Section(
                section_no=n,
                section_id=f"s{n:03d}",
                section_title=title,
                source_url=source_url,
                source_type="pdf" if path_lc.endswith(".pdf") else "html",
            )
        )

    return sections


def normalize_line(line: str) -> str:
    t = clean_ws(line)
    t = t.replace("’", "'").replace("“", '"').replace("”", '"')
    return t


def extract_pdf_lines(pdf_path: Path) -> list[str]:
    out: list[str] = []
    reader = PdfReader(str(pdf_path))
    for page_no, page in enumerate(reader.pages, start=1):
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


def extract_html_lines(url: str) -> list[str]:
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("#bodyArea") or soup.body or soup

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

    # Light de-dup for immediate repeats often found in menu blocks.
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
        local = raw_dir / filename

        if s.source_type == "pdf":
            download_file(s.source_url, local)
            lines = extract_pdf_lines(local)
        else:
            # Keep source HTML snapshot as cache for reproducibility.
            html = fetch_html(s.source_url)
            local.write_text(html, encoding="utf-8", errors="replace")
            lines = extract_html_lines(s.source_url)

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
    out_path.write_text(html, encoding="utf-8", errors="replace")


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
    shard_files: list[str] = []

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
        shard_files.append(fname)

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
        "shard_count": len(shard_files),
        "row_count": len(rows),
        "shards": shards_meta,
    }
    (ROOT_DIR / "data" / "shards_index.json").write_text(
        json.dumps(idx, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def write_quickstart(specs: list[SourceSpec], items_by_doc: dict[str, list[Item]]) -> None:
    lines = [
        "# AI向け NTA手引DB クイックスタート（1URL≒1万トークン制約向け）",
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

    # Add one sample for each doc when available.
    for s in specs:
        items = items_by_doc.get(s.doc_code, [])
        if items:
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
        "# AI向け NTA Guide DB",
        "# Base URL: https://jplawdb.github.io/html-preview/ai-nta-guide-db/",
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
  <title>AI向け NTA Guide DB</title>
  <meta name=\"description\" content=\"国税庁の手引き系資料をAI向けに分割・構造化したDB\" />
  <link rel=\"alternate\" type=\"text/plain\" href=\"llms.txt\" title=\"LLM Site Map\" />
  <style>
    body {{ font-family: system-ui, -apple-system, 'Segoe UI', sans-serif; line-height: 1.7; max-width: 920px; margin: 0 auto; padding: 24px; }}
    .meta {{ color: #555; }}
    code {{ background: #f3f3f3; padding: 0 6px; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>AI向け NTA Guide DB</h1>
  <p class=\"meta\">国税庁の「手引き・あらまし」資料を、1URL=短い本文単位で再構成しています。</p>
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

Sitemap: https://jplawdb.github.io/html-preview/ai-nta-guide-db/sitemap.xml
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
        index_html = fetch_html(spec.index_url)
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

    print("[done] ai-nta-guide-db artifacts generated")


if __name__ == "__main__":
    main()
