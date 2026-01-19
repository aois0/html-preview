#!/usr/bin/env python3
"""
Generate ai-law-db/enhanced/{law_code} pages from e-LAWS (e-Gov) Law XML.

This script is intentionally minimal and only targets what build_ai_artifacts.py expects:
- 1 article = 1 HTML page
- <article> with data-* meta
- <div id="p{n}" data-paragraph="{n}"> for paragraphs
- <p class="item" id="p{n}-i{k}"> for items (号)
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import html
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


SITE_BASE_URL = "https://jplawdb.github.io/html-preview/ai-law-db"
ENHANCED_BASE_URL = f"{SITE_BASE_URL}/enhanced"
ELAWS_LAWDATA_BASE_URL = "https://elaws.e-gov.go.jp/api/1/lawdata"


def clean_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def text_of(elem: ET.Element | None) -> str:
    if elem is None:
        return ""
    return clean_ws("".join(elem.itertext()))


def sort_article_key(num: str):
    parts = re.split(r"[-_]", num)
    key = []
    for p in parts:
        if p.isdigit():
            key.append((0, int(p)))
        else:
            key.append((1, p))
    return key


def norm_article_num(num: str) -> str:
    return (num or "").replace("_", "-")


def article_label(article: str) -> str:
    """
    "57-5" -> "第57条の5"
    "2-4-2" -> "第2条の4の2"
    """
    parts = (article or "").split("-")
    if not parts or not parts[0].isdigit():
        return f"第{article}条"
    label = f"第{int(parts[0])}条"
    for p in parts[1:]:
        label += f"の{int(p)}" if p.isdigit() else f"の{p}"
    return label


def infer_law_type(law_code: str) -> str:
    if law_code.endswith("_seirei"):
        return "order"
    if law_code.endswith("_kisoku"):
        return "rule"
    return "act"


def related_laws(law_code: str) -> list[tuple[str, str]]:
    """
    Returns [(relative_index_path, label), ...]
    """
    law_type = infer_law_type(law_code)
    if law_type == "act":
        return [
            (f"../{law_code}_seirei/index.html", "施行令"),
            (f"../{law_code}_kisoku/index.html", "施行規則"),
        ]
    if law_type == "order":
        base = law_code[: -len("_seirei")]
        return [
            (f"../{base}/index.html", "本則"),
            (f"../{base}_kisoku/index.html", "施行規則"),
        ]
    if law_type == "rule":
        base = law_code[: -len("_kisoku")]
        return [
            (f"../{base}/index.html", "本則"),
            (f"../{base}_seirei/index.html", "施行令"),
        ]
    return []


def fetch_elaws_xml(law_id: str) -> str:
    url = f"{ELAWS_LAWDATA_BASE_URL}/{law_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "jplawdb-html-preview/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def find_law_root(doc: ET.Element) -> ET.Element:
    law = doc.find(".//ApplData/LawFullText/Law")
    if law is None:
        law = doc.find(".//Law")
    return law if law is not None else doc


def find_main_provision(law: ET.Element) -> ET.Element | None:
    return law.find(".//LawBody/MainProvision")


@dataclass(frozen=True)
class Item:
    title: str
    text: str


@dataclass(frozen=True)
class Paragraph:
    num: str
    text: str
    items: list[Item]


@dataclass(frozen=True)
class Article:
    num: str
    heading: str
    paragraphs: list[Paragraph]


@dataclass(frozen=True)
class Law:
    law_id: str
    title: str
    law_num: str
    articles: list[Article]


def parse_law(xml_text: str) -> Law:
    doc = ET.fromstring(xml_text)
    code = clean_ws(doc.findtext(".//Result/Code") or "")
    if code and code != "0":
        msg = clean_ws(doc.findtext(".//Result/Message") or "")
        raise RuntimeError(f"e-LAWS returned code={code}: {msg}")

    law_id = clean_ws(doc.findtext(".//ApplData/LawId") or "")
    law = find_law_root(doc)
    law_num = text_of(law.find("LawNum"))
    title = text_of(law.find(".//LawBody/LawTitle"))

    main = find_main_provision(law)
    if main is None:
        raise RuntimeError("MainProvision not found")

    articles: list[Article] = []
    for art in main.iter("Article"):
        num = norm_article_num(art.get("Num", ""))
        if not num:
            continue
        heading = text_of(art.find("ArticleTitle")) + text_of(art.find("ArticleCaption"))

        paragraphs: list[Paragraph] = []
        for para in art.findall("Paragraph"):
            para_num = clean_ws(para.get("Num", ""))
            # Join all ParagraphSentence texts (usually 0 or 1 in this corpus)
            sent_texts = [text_of(s) for s in para.findall("ParagraphSentence")]
            para_text = clean_ws(" ".join([t for t in sent_texts if t]))

            items: list[Item] = []
            for item_el in para.findall("Item"):
                it_title = text_of(item_el.find("ItemTitle"))
                it_text = text_of(item_el.find("ItemSentence"))
                if it_title or it_text:
                    items.append(Item(title=it_title, text=it_text))

            paragraphs.append(Paragraph(num=para_num, text=para_text, items=items))

        articles.append(Article(num=num, heading=heading, paragraphs=paragraphs))

    articles.sort(key=lambda a: sort_article_key(a.num))
    return Law(law_id=law_id, title=title, law_num=law_num, articles=articles)

def write_index(out_dir: Path, law_code: str, law: Law, law_type: str, egov_id: str, as_of: str) -> None:
    h = html.escape
    canonical = f"{ENHANCED_BASE_URL}/{law_code}/index.html"

    lines: list[str] = []
    lines.append("<!DOCTYPE html>")
    lines.append('<html lang="ja">')
    lines.append("<head>")
    lines.append('<meta charset="UTF-8">')
    lines.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    lines.append(f"<title>{h(law.title)} [index] [{h(law_code)}]</title>")
    lines.append(
        f'<meta name="description" content="{h(law.title)}（{h(law_code)}）条文一覧（as_of {h(as_of)}）。AI向け。">'
    )
    lines.append(f'<link rel="canonical" href="{h(canonical)}">')
    lines.append("</head>")
    lines.append("<body>")
    lines.append(f"<h1>{h(law.title)}（{h(law_code)}）</h1>")
    lines.append(
        f"<p>法令番号: <code>{h(law.law_num)}</code> / e-Gov ID: <code>{h(egov_id)}</code> / as_of: <code>{h(as_of)}</code></p>"
    )
    lines.append(f"<p>全{len(law.articles)}条</p>")
    lines.append(
        f'<p><a href="../../data/chunks/{h(law_code)}.jsonl">chunks.jsonl</a>（RAG投入用） / '
        f'<a href="{h(SITE_BASE_URL)}/llms.txt">llms.txt</a></p>'
    )

    related = related_laws(law_code)
    if related:
        rel_links = " / ".join([f'<a href="{h(p)}">{h(label)}</a>' for p, label in related])
        lines.append(f"<p>関連法令: {rel_links}</p>")

    lines.append("<ul>")
    for a in law.articles:
        lines.append(f'<li><a href="{h(a.num)}.html">{h(a.heading)}</a></li>')
    lines.append("</ul>")
    lines.append("</body>")
    lines.append("</html>")
    (out_dir / "index.html").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_article_pages(out_dir: Path, law_code: str, law: Law, law_type: str, egov_id: str, as_of: str) -> None:
    h = html.escape

    nums = [a.num for a in law.articles]
    by_num = {a.num: a for a in law.articles}

    for idx, num in enumerate(nums):
        art = by_num[num]
        label = article_label(num)
        cite = f"{law.title}{label}"
        cite_id = f"{law_code}:{num}"

        prev_num = nums[idx - 1] if idx > 0 else None
        next_num = nums[idx + 1] if idx < len(nums) - 1 else None

        canonical = f"{ENHANCED_BASE_URL}/{law_code}/{num}.html"
        index_url = f"{ENHANCED_BASE_URL}/{law_code}/index.html"

        # JSON-LD identifier: acts use e-Gov LawId; others use law_code (matching existing corpus).
        jsonld_identifier = egov_id if law_type == "act" else law_code
        jsonld_payload = {
            "@context": "https://schema.org",
            "@type": "Legislation",
            "name": law.title,
            "legislationIdentifier": jsonld_identifier,
            "legislationType": law_type,
            "url": canonical,
            "text": art.heading,
            "isPartOf": {"@type": "Legislation", "name": law.title, "url": index_url},
        }

        nav_parts: list[str] = []
        nav_parts.append('<a href="index.html">目次</a>')
        if prev_num:
            nav_parts.append(f'<a href="{h(prev_num)}.html">前条</a>')
        if next_num:
            nav_parts.append(f'<a href="{h(next_num)}.html">次条</a>')
        nav_parts.append(f'<a href="../../text/{h(law_code)}/{h(num)}.txt">text</a>')

        related = related_laws(law_code)
        related_html = ""
        if related:
            rel_links = " , ".join([f'<a href="{h(p)}">{h(label)}</a>' for p, label in related])
            related_html = f" / related: {rel_links}"

        lines: list[str] = []
        lines.append("<!DOCTYPE html>")
        lines.append('<html lang="ja">')
        lines.append("<head>")
        lines.append('<meta charset="UTF-8">')
        lines.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
        lines.append(f"<title>{h(law.title)} {h(art.heading)} [{h(law_code)} {h(num)}]</title>")
        lines.append(
            f'<meta name="description" content="{h(law.title)} {h(label)}（{h(art.heading)}） 全文（as_of {h(as_of)}）。AI向け。">'
        )
        lines.append(f'<link rel="canonical" href="{h(canonical)}">')
        lines.append(
            f'<link rel="alternate" type="text/plain" href="../../text/{h(law_code)}/{h(num)}.txt" title="Plain text">'
        )
        lines.append('<script type="application/ld+json">')
        lines.append(json.dumps(jsonld_payload, ensure_ascii=False, separators=(",", ":")))
        lines.append("</script>")
        lines.append("</head>")
        lines.append("<body>")
        lines.append(
            f'<article data-law="{h(law.title)}" data-law-code="{h(law_code)}" data-law-type="{h(law_type)}" '
            f'data-law-num="{h(law.law_num)}" data-egov-id="{h(egov_id)}" data-as-of="{h(as_of)}" '
            f'data-article="{h(num)}" data-cite="{h(cite)}" data-cite-id="{h(cite_id)}">'
        )
        lines.append(f"<h1>{h(law.title)}</h1>")
        lines.append(f'<h2 id="article-{h(num)}">{h(art.heading)}</h2>')
        lines.append("<p>")
        lines.append(
            f'law_code: <code>{h(law_code)}</code> / article: <code>{h(num)}</code> / '
            f'cite: <code>{h(cite)}</code> / as_of: <code>{h(as_of)}</code><br>'
        )
        lines.append(" / ".join(nav_parts) + related_html)
        lines.append("</p>")

        for para in art.paragraphs:
            if not para.num:
                continue
            para_cite = f"{cite}第{para.num}項"
            para_cite_id = f"{law_code}:{num}:p{para.num}"
            lines.append(
                f'<div data-paragraph="{h(para.num)}" id="p{h(para.num)}" data-cite="{h(para_cite)}" '
                f'data-cite-id="{h(para_cite_id)}">'
            )
            if para.text:
                lines.append(f"<p>{h(para.num)} {h(para.text)}</p>")
            item_index = 0
            for item in para.items:
                item_index += 1
                item_cite = f"{para_cite}第{item_index}号"
                item_cite_id = f"{para_cite_id}:i{item_index}"
                item_text = clean_ws(" ".join([t for t in [item.title, item.text] if t]))
                if not item_text:
                    continue
                lines.append(
                    f'<p class="item" data-item="{h(item.title)}" data-item-index="{item_index}" '
                    f'id="p{h(para.num)}-i{item_index}" data-cite="{h(item_cite)}" data-cite-id="{h(item_cite_id)}">'
                    f"{h(item_text)}</p>"
                )
            lines.append("</div>")

        lines.append("</article>")
        lines.append("</body>")
        lines.append("</html>")
        (out_dir / f"{num}.html").write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate_one(law_code: str, law_id: str, out_enhanced_dir: Path, as_of: str) -> None:
    xml_text = fetch_elaws_xml(law_id)
    law = parse_law(xml_text)
    if not law.title:
        raise RuntimeError("failed to parse law title")

    law_type = infer_law_type(law_code)
    egov_id = law_id if law_type == "act" else ""

    out_dir = out_enhanced_dir / law_code
    out_dir.mkdir(parents=True, exist_ok=True)
    write_index(out_dir=out_dir, law_code=law_code, law=law, law_type=law_type, egov_id=egov_id, as_of=as_of)
    write_article_pages(out_dir=out_dir, law_code=law_code, law=law, law_type=law_type, egov_id=egov_id, as_of=as_of)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate enhanced HTML for ai-law-db from e-LAWS")
    parser.add_argument("--law-code", required=True, help="e.g. shohizei_seirei")
    parser.add_argument("--law-id", required=True, help="e.g. 363CO0000000360")
    parser.add_argument("--as-of", default="2025-12-27", help="as_of date string embedded in HTML")
    parser.add_argument(
        "--enhanced-dir",
        default=str(Path(__file__).resolve().parents[1] / "enhanced"),
        help="output enhanced/ directory",
    )
    args = parser.parse_args()

    enhanced_dir = Path(args.enhanced_dir)
    generate_one(law_code=args.law_code, law_id=args.law_id, out_enhanced_dir=enhanced_dir, as_of=args.as_of)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
