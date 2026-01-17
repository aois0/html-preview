#!/usr/bin/env python3
"""
Build AI-optimized artifacts for Japanese tax circulars (通達).

Inputs (default, sibling directory to repo root):
  ../gemini_share/*_tsutatsu_full.txt

Outputs (workspace-relative):
  - ai-tsutatsu-db/enhanced/{doc_code}/index.html
  - ai-tsutatsu-db/enhanced/{doc_code}/{item_id}.html
  - ai-tsutatsu-db/text/{doc_code}/{item_id}.txt
  - ai-tsutatsu-db/data/doc_aliases.json
  - ai-tsutatsu-db/data/resolve_lite/index.json
  - ai-tsutatsu-db/data/resolve_lite/{doc_code}.json
  - ai-tsutatsu-db/data/chunks/{doc_code}.jsonl
  - ai-tsutatsu-db/sitemap.xml
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
import json
import re
from pathlib import Path
from typing import Any, Iterable


SITE_BASE_URL = "https://jplawdb.github.io/html-preview/ai-tsutatsu-db"
SITE_ENHANCED_BASE_URL = f"{SITE_BASE_URL}/enhanced"

RESOLVE_LITE_DIR_NAME = "resolve_lite"


NOISE_LINES = {
    "すべての機能をご利用いただくにはJavascriptを有効にしてください。",
    "ホーム",
    "法令等",
    "法令解釈通達",
    "その他法令解釈に関する情報",
    "事務運営指針",
    "国税庁告示",
    "文書回答事例",
    "質疑応答事例",
    "このページの先頭へ",
    "このページの先頭へ戻る",
    "ページの先頭へ戻る",
}


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


_DASH_CHARS = {
    "-",
    "−",  # U+2212 minus
    "－",  # U+FF0D fullwidth hyphen-minus
    "‐",
    "‑",
    "‒",
    "–",
    "—",
    "―",
    "ー",  # long sound mark (often used as dash)
}


def normalize_digits(s: str) -> str:
    return (s or "").translate(_FULLWIDTH_DIGITS)


def clean_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def is_digits_only(line: str) -> bool:
    s = normalize_digits((line or "").strip())
    return bool(s) and s.isdigit()


def looks_like_id_suffix(line: str) -> bool:
    s = normalize_digits((line or "").strip())
    return bool(s) and s[0] in _DASH_CHARS and len(s) >= 2 and s[1].isdigit()


def is_title_line(line: str) -> bool:
    s = (line or "").strip()
    if len(s) < 2:
        return False
    return (s.startswith("(") and s.endswith(")")) or (s.startswith("（") and s.endswith("）"))


def split_item_id_prefix(line: str) -> tuple[str, str] | None:
    """
    Returns (id_raw, rest) if line begins with an item-id like:
      1−1−1
      2−4の2
      57の8−1
      1の3・1の4共−1
    The id part may be followed immediately by text (no whitespace).
    """
    s = (line or "").strip()
    if not s:
        return None

    first = normalize_digits(s[:1])
    if not first.isdigit():
        return None

    allowed = set("の・共") | _DASH_CHARS
    i = 0
    while i < len(s):
        ch = s[i]
        if normalize_digits(ch).isdigit():
            i += 1
            continue
        if ch in allowed:
            i += 1
            continue
        break

    prefix = s[:i]
    if not prefix:
        return None

    prefix_norm = normalize_digits(prefix)
    if not prefix_norm[-1].isdigit():
        return None

    # Require some separator to avoid matching plain numbers like "2025".
    if not any(c in prefix for c in ("の", "・", "共")) and not any(c in prefix for c in _DASH_CHARS):
        return None

    # Must contain at least one dash/"の" to be a realistic item id.
    if not (any(c in prefix for c in ("の", "・", "共")) or any(c in prefix for c in _DASH_CHARS)):
        return None

    rest = s[i:].lstrip()
    return prefix, rest


def normalize_item_id(raw: str) -> str:
    s = normalize_digits((raw or "").strip())
    if not s:
        return ""

    # Normalize dashes.
    for ch in _DASH_CHARS:
        s = s.replace(ch, "-")

    # Encode special markers.
    s = s.replace("の", "-")
    s = s.replace("・", "_")
    s = s.replace("共", "_common")

    # Keep only safe filename characters.
    s = re.sub(r"[^0-9a-zA-Z_-]+", "", s)
    s = re.sub(r"-{2,}", "-", s)
    s = re.sub(r"_{2,}", "_", s)
    s = s.strip("-_")
    return s


def sort_item_key(item_id: str):
    # Numeric-aware sort for ids like 57-8-1, 1-3_1-4_common-1.
    parts = re.split(r"[_-]", item_id)
    key: list[tuple[int, Any]] = []
    for p in parts:
        if p.isdigit():
            key.append((0, int(p)))
        else:
            key.append((1, p))
    return key


@dataclass(frozen=True)
class DocConfig:
    doc_code: str
    title: str
    source_file: str
    source_kind: str = "tsutatsu"


@dataclass
class Item:
    doc_code: str
    doc_title: str
    snapshot: str
    source_path: str
    source_page_url: str | None
    item_id_raw: str
    item_id: str
    item_title: str
    lines: list[str]


_PAGE_MARKER_RE = re.compile(r"^---\s*(?P<label>[^:]+):\s*(?P<url>https?://\S+)\s*---$")


def iter_items_from_source(doc: DocConfig, sources_dir: Path) -> list[Item]:
    src_path = (sources_dir / doc.source_file).resolve()
    if not src_path.exists():
        raise FileNotFoundError(f"missing source file: {src_path}")

    snapshot = datetime.fromtimestamp(src_path.stat().st_mtime, tz=timezone.utc).date().isoformat()

    current_page_url: str | None = None
    in_chapter = False
    pending_title: str | None = None
    pending_digits: str | None = None

    items: list[Item] = []
    current_item: Item | None = None

    def flush_current() -> None:
        nonlocal current_item
        if current_item is None:
            return
        # Trim empty lines.
        while current_item.lines and not current_item.lines[0].strip():
            current_item.lines.pop(0)
        while current_item.lines and not current_item.lines[-1].strip():
            current_item.lines.pop()
        items.append(current_item)
        current_item = None

    for raw_line in src_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = _PAGE_MARKER_RE.match(line)
        if m:
            if pending_digits is not None and current_item is not None:
                current_item.lines.append(pending_digits)
            pending_digits = None
            label = m.group("label").strip()
            url = m.group("url").strip()
            current_page_url = url
            in_chapter = label.lower().startswith("chapter page")
            if not in_chapter:
                # Ignore index and other non-chapter blocks.
                pending_title = None
                pending_digits = None
                flush_current()
            continue

        if not in_chapter:
            continue

        if line in NOISE_LINES:
            continue

        if is_title_line(line):
            pending_title = line[1:-1] if line.startswith("(") else line[1:-1]
            pending_title = clean_ws(pending_title)
            continue

        # Handle split item-ids like:
        #   1
        #   −1−1
        #
        # Note: split forms may appear while the previous item is still "open".
        # We delay deciding until we see the suffix line.
        if pending_digits is not None:
            if looks_like_id_suffix(line):
                line = pending_digits + normalize_digits(line)
            else:
                # Unused prefix; treat as content if we are inside an item.
                if current_item is not None:
                    current_item.lines.append(pending_digits)
            pending_digits = None

        if is_digits_only(line):
            pending_digits = normalize_digits(line)
            continue

        id_split = split_item_id_prefix(line)
        if id_split is not None:
            item_id_raw, rest = id_split
            item_id = normalize_item_id(item_id_raw)
            if not item_id:
                continue

            flush_current()
            current_item = Item(
                doc_code=doc.doc_code,
                doc_title=doc.title,
                snapshot=snapshot,
                source_path=str(src_path),
                source_page_url=current_page_url,
                item_id_raw=item_id_raw,
                item_id=item_id,
                item_title=pending_title or "",
                lines=[],
            )
            pending_title = None
            if rest:
                current_item.lines.append(rest)
            continue

        if current_item is not None:
            current_item.lines.append(line)

    if pending_digits is not None and current_item is not None:
        current_item.lines.append(pending_digits)
    flush_current()

    # Deduplicate by item_id (keep first).
    seen: set[str] = set()
    uniq: list[Item] = []
    for it in items:
        if it.item_id in seen:
            continue
        seen.add(it.item_id)
        uniq.append(it)
    return uniq


def split_paragraphs(lines: list[str]) -> list[dict[str, Any]]:
    """
    Converts raw lines into paragraph blocks:
      - non-enumeration line => new paragraph
      - (1)/(2)... => item lines attached to previous paragraph
    """
    paragraphs: list[dict[str, Any]] = []

    item_re = re.compile(r"^(?:\\(|（)(?P<num>\\d+)(?:\\)|）)\\s*(?P<text>.+)$")
    for raw in lines:
        line = clean_ws(raw)
        if not line:
            continue

        m = item_re.match(line)
        if m:
            if not paragraphs:
                paragraphs.append({"text": "", "items": []})
            paragraphs[-1]["items"].append(
                {
                    "index": int(m.group("num")),
                    "text": clean_ws(m.group("text")),
                }
            )
            continue

        paragraphs.append({"text": line, "items": []})

    return paragraphs


def write_item_files(item: Item, out_enhanced_dir: Path, out_text_dir: Path) -> None:
    doc_code = item.doc_code
    item_id = item.item_id

    item_url = f"{SITE_ENHANCED_BASE_URL}/{doc_code}/{item_id}.html"
    text_rel = f"../../text/{doc_code}/{item_id}.txt"

    paragraphs = split_paragraphs(item.lines)

    # Text mirror.
    txt_path = out_text_dir / doc_code / f"{item_id}.txt"
    txt_lines: list[str] = []
    txt_lines.append(f"doc: {item.doc_title} ({doc_code})")
    txt_lines.append(f"item: {item.item_id_raw} / id: {item_id} / title: {item.item_title}")
    txt_lines.append(f"snapshot: {item.snapshot}")
    if item.source_page_url:
        txt_lines.append(f"source_page: {item.source_page_url}")
    txt_lines.append(f"url: {item_url}")
    txt_lines.append("")

    for pi, p in enumerate(paragraphs, start=1):
        if p.get("text"):
            txt_lines.append(f"[p{pi}] {p['text']}")
        for it in p.get("items") or []:
            txt_lines.append(f"[p{pi}-i{it['index']}] {it['text']}")

    txt_path.write_text("\n".join(txt_lines) + "\n", encoding="utf-8")

    # HTML.
    title_bits = [item.doc_title, item.item_id_raw]
    if item.item_title:
        title_bits.append(f"（{item.item_title}）")
    title_bits.append(f"[{doc_code} {item_id}]")
    page_title = " ".join(title_bits)
    desc = f"{item.doc_title} {item.item_id_raw}（{item.item_title}） snapshot {item.snapshot}。AI向け。".replace("（）", "")

    html_lines: list[str] = []
    html_lines.append("<!DOCTYPE html>")
    html_lines.append('<html lang="ja">')
    html_lines.append("<head>")
    html_lines.append('<meta charset="UTF-8">')
    html_lines.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    html_lines.append(f"<title>{escape(page_title)}</title>")
    html_lines.append(f'<meta name="description" content="{escape(desc)}">')
    html_lines.append(f'<link rel="canonical" href="{escape(item_url)}">')
    html_lines.append(f'<link rel="alternate" type="text/plain" href="{escape(text_rel)}" title="Plain text">')
    html_lines.append("</head>")
    html_lines.append("<body>")
    html_lines.append(
        f'<article data-doc="{escape(item.doc_title)}" data-doc-code="{escape(doc_code)}" '
        f'data-item="{escape(item_id)}" data-item-raw="{escape(item.item_id_raw)}" '
        f'data-snapshot="{escape(item.snapshot)}">'
    )
    html_lines.append(f"<h1>{escape(item.doc_title)}</h1>")
    h2 = f"{item.item_id_raw}"
    if item.item_title:
        h2 += f"（{item.item_title}）"
    html_lines.append(f'<h2 id="item-{escape(item_id)}">{escape(h2)}</h2>')
    html_lines.append("<p>")
    html_lines.append(
        f'doc_code: <code>{escape(doc_code)}</code> / item: <code>{escape(item_id)}</code> '
        f'/ snapshot: <code>{escape(item.snapshot)}</code><br>'
        f'<a href="index.html">目次</a> / <a href="{escape(text_rel)}">text</a>'
        + (f' / <a href="{escape(item.source_page_url)}">source</a>' if item.source_page_url else "")
    )
    html_lines.append("</p>")

    cite = f"{item.doc_title}{item.item_id_raw}"
    cite_id = f"{doc_code}:{item_id}"

    for pi, p in enumerate(paragraphs, start=1):
        pid = f"p{pi}"
        html_lines.append(
            f'<div data-paragraph="{pi}" id="{pid}" data-cite="{escape(cite)}" data-cite-id="{escape(cite_id)}:{pid}">'
        )
        if p.get("text"):
            html_lines.append(f"<p>{escape(p['text'])}</p>")
        for it in p.get("items") or []:
            item_anchor = f"{pid}-i{it['index']}"
            html_lines.append(
                f'<p class="item" data-item-index="{it["index"]}" id="{item_anchor}" '
                f'data-cite="{escape(cite)}" data-cite-id="{escape(cite_id)}:{item_anchor}">{escape(it["text"])}</p>'
            )
        html_lines.append("</div>")

    html_lines.append("</article>")
    html_lines.append("</body>")
    html_lines.append("</html>")

    (out_enhanced_dir / doc_code / f"{item_id}.html").write_text("\n".join(html_lines) + "\n", encoding="utf-8")


def write_doc_index(doc: DocConfig, snapshot: str, items: list[Item], out_enhanced_dir: Path) -> None:
    doc_code = doc.doc_code
    index_url = f"{SITE_ENHANCED_BASE_URL}/{doc_code}/index.html"
    resolve_url = f"{SITE_BASE_URL}/data/{RESOLVE_LITE_DIR_NAME}/{doc_code}.json"

    html = f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(doc.title)} [{escape(doc_code)}]</title>
  <meta name="description" content="{escape(doc.title)}（{doc_code}）の項番一覧。AI向け。" />
  <link rel="canonical" href="{escape(index_url)}" />
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, 'Noto Sans JP', sans-serif; line-height: 1.7; max-width: 900px; margin: 0 auto; padding: 24px; }}
    code {{ background: #f3f3f3; padding: 0 6px; border-radius: 4px; }}
    ul {{ padding-left: 1.2rem; }}
  </style>
</head>
<body>
  <h1>{escape(doc.title)}</h1>
  <p>doc_code: <code>{escape(doc_code)}</code> / snapshot: <code>{escape(snapshot)}</code></p>
  <p>
    <a href="../../index.html">ai-tsutatsu-db</a>
    / <a href="{escape(resolve_url)}">resolve_lite</a>
  </p>
  <p>項番一覧は <code>resolve_lite</code> を推奨（HTMLに全件リンクを埋め込まない設計）。</p>
</body>
</html>
"""
    (out_enhanced_dir / doc_code / "index.html").write_text(html, encoding="utf-8")


def build_doc_aliases() -> dict[str, str]:
    # 手動の略称（必要に応じて増やす）
    return {
        "法人税基本通達": "hojinzei_kihon_tsutatsu",
        "法基通": "hojinzei_kihon_tsutatsu",
        "所得税基本通達": "shotokuzei_kihon_tsutatsu",
        "所基通": "shotokuzei_kihon_tsutatsu",
        "消費税基本通達": "shohizei_kihon_tsutatsu",
        "消基通": "shohizei_kihon_tsutatsu",
        "相続税法基本通達": "sozokuzei_kihon_tsutatsu",
        "相続税基本通達": "sozokuzei_kihon_tsutatsu",
        "租税特別措置法関係通達（法人税編）": "sozei_tokubetsu_tsutatsu_hojinzei",
        "措置法通達（法人税編）": "sozei_tokubetsu_tsutatsu_hojinzei",
        "措置法通達": "sozei_tokubetsu_tsutatsu_hojinzei",
        "国税通則法基本通達": "kokuzei_tsusoku_kihon_tsutatsu",
        "通則法基本通達": "kokuzei_tsusoku_kihon_tsutatsu",
    }


def build_sitemap(urls: Iterable[str]) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        lines.append(f"  <url><loc>{escape(u)}</loc></url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def main() -> None:
    base_dir = Path(__file__).resolve().parents[1]  # ai-tsutatsu-db/
    repo_root = base_dir.parent  # html-preview/
    sources_dir = repo_root.parent / "gemini_share"

    out_enhanced_dir = base_dir / "enhanced"
    out_text_dir = base_dir / "text"
    out_data_dir = base_dir / "data"
    out_chunks_dir = out_data_dir / "chunks"
    out_resolve_lite_dir = out_data_dir / RESOLVE_LITE_DIR_NAME

    out_enhanced_dir.mkdir(parents=True, exist_ok=True)
    out_text_dir.mkdir(parents=True, exist_ok=True)
    out_chunks_dir.mkdir(parents=True, exist_ok=True)
    out_resolve_lite_dir.mkdir(parents=True, exist_ok=True)

    docs = [
        DocConfig("hojinzei_kihon_tsutatsu", "法人税基本通達", "hojinzei_kihon_tsutatsu_full.txt"),
        DocConfig("shotokuzei_kihon_tsutatsu", "所得税基本通達", "shotoku_kihon_tsutatsu_full.txt"),
        DocConfig("shohizei_kihon_tsutatsu", "消費税基本通達", "shohi_kihon_tsutatsu_full.txt"),
        DocConfig("sozokuzei_kihon_tsutatsu", "相続税法基本通達", "sisan_sozoku2_kihon_tsutatsu_full.txt"),
        DocConfig("sozei_tokubetsu_tsutatsu_hojinzei", "租税特別措置法関係通達（法人税編）", "sochiho_hojinzei_tsutatsu_full.txt"),
        DocConfig("kokuzei_tsusoku_kihon_tsutatsu", "国税通則法基本通達", "tsusoku_kihon_tsutatsu_full.txt"),
    ]

    aliases = build_doc_aliases()
    (out_data_dir / "doc_aliases.json").write_text(
        json.dumps({"aliases": aliases}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    resolve_index_docs: dict[str, Any] = {}
    sitemap_urls: list[str] = [
        f"{SITE_BASE_URL}/",
        f"{SITE_BASE_URL}/index.html",
        f"{SITE_BASE_URL}/llms.txt",
        f"{SITE_BASE_URL}/data/doc_aliases.json",
        f"{SITE_BASE_URL}/data/{RESOLVE_LITE_DIR_NAME}/index.json",
    ]

    for doc in docs:
        items = iter_items_from_source(doc, sources_dir)
        if not items:
            continue

        snapshot = items[0].snapshot

        # Output folders.
        (out_enhanced_dir / doc.doc_code).mkdir(parents=True, exist_ok=True)
        (out_text_dir / doc.doc_code).mkdir(parents=True, exist_ok=True)

        # chunks
        chunks_path = out_chunks_dir / f"{doc.doc_code}.jsonl"
        with chunks_path.open("w", encoding="utf-8", newline="\n") as f:
            for item in sorted(items, key=lambda it: sort_item_key(it.item_id)):
                write_item_files(item, out_enhanced_dir, out_text_dir)

                item_url = f"{SITE_ENHANCED_BASE_URL}/{doc.doc_code}/{item.item_id}.html"
                paragraphs = split_paragraphs(item.lines)
                for pi, p in enumerate(paragraphs, start=1):
                    if p.get("text"):
                        rec = {
                            "id": f"{doc.doc_code}:{item.item_id}:p{pi}",
                            "kind": "paragraph",
                            "doc_code": doc.doc_code,
                            "doc_title": doc.title,
                            "snapshot": item.snapshot,
                            "item_id": item.item_id,
                            "item_id_raw": item.item_id_raw,
                            "item_title": item.item_title or None,
                            "paragraph": pi,
                            "item": None,
                            "source_page_url": item.source_page_url,
                            "url": f"{item_url}#p{pi}",
                            "text": p["text"],
                            "source_path": item.source_path,
                        }
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    for it in p.get("items") or []:
                        rec = {
                            "id": f"{doc.doc_code}:{item.item_id}:p{pi}:i{it['index']}",
                            "kind": "item",
                            "doc_code": doc.doc_code,
                            "doc_title": doc.title,
                            "snapshot": item.snapshot,
                            "item_id": item.item_id,
                            "item_id_raw": item.item_id_raw,
                            "item_title": item.item_title or None,
                            "paragraph": pi,
                            "item": {"index": it["index"]},
                            "source_page_url": item.source_page_url,
                            "url": f"{item_url}#p{pi}-i{it['index']}",
                            "text": it["text"],
                            "source_path": item.source_path,
                        }
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        write_doc_index(doc, snapshot, items, out_enhanced_dir)

        per_doc_items = sorted({it.item_id for it in items}, key=sort_item_key)
        per_doc_resolve = {
            "snapshot": snapshot,
            "base_url": SITE_BASE_URL,
            "enhanced_base_url": SITE_ENHANCED_BASE_URL,
            "doc_code": doc.doc_code,
            "doc_title": doc.title,
            "index_url": f"{SITE_ENHANCED_BASE_URL}/{doc.doc_code}/index.html",
            "item_url_template": "enhanced/{doc_code}/{item_id}.html",
            "text_url_template": "text/{doc_code}/{item_id}.txt",
            "anchors": {"paragraph": "#p{paragraph}", "item": "#p{paragraph}-i{item_index}"},
            "items": per_doc_items,
        }
        (out_resolve_lite_dir / f"{doc.doc_code}.json").write_text(
            json.dumps(per_doc_resolve, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        resolve_index_docs[doc.doc_code] = {
            "title": doc.title,
            "snapshot": snapshot,
            "index_url": per_doc_resolve["index_url"],
            "items_count": len(per_doc_items),
            "resolve_lite_url": f"{SITE_BASE_URL}/data/{RESOLVE_LITE_DIR_NAME}/{doc.doc_code}.json",
        }

        sitemap_urls.append(per_doc_resolve["index_url"])
        sitemap_urls.append(resolve_index_docs[doc.doc_code]["resolve_lite_url"])
        # Add item pages to sitemap (HTML only, text is linked via rel=alternate).
        for iid in per_doc_items:
            sitemap_urls.append(f"{SITE_ENHANCED_BASE_URL}/{doc.doc_code}/{iid}.html")

    resolve_index = {
        "base_url": SITE_BASE_URL,
        "enhanced_base_url": SITE_ENHANCED_BASE_URL,
        "item_url_template": "enhanced/{doc_code}/{item_id}.html",
        "text_url_template": "text/{doc_code}/{item_id}.txt",
        "anchors": {"paragraph": "#p{paragraph}", "item": "#p{paragraph}-i{item_index}"},
        "doc_aliases": aliases,
        "docs": resolve_index_docs,
    }
    (out_resolve_lite_dir / "index.json").write_text(
        json.dumps(resolve_index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # Sitemap
    (base_dir / "sitemap.xml").write_text(build_sitemap(sitemap_urls), encoding="utf-8")


if __name__ == "__main__":
    main()
