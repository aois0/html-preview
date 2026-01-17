#!/usr/bin/env python3
"""
Build AI-optimized artifacts from ai-law-db/enhanced HTML.

Outputs (workspace-relative):
- ai-law-db/data/resolve.json
- ai-law-db/data/resolve.min.json
- ai-law-db/data/resolve_lite.json
- ai-law-db/data/resolve_lite/index.json
- ai-law-db/data/resolve_lite/{law_code}.json
- ai-law-db/data/law_aliases.json
- ai-law-db/data/chunks/{law_code}.jsonl
- ai-law-db/text/{law_code}/{article}.txt

Design goals:
- Deterministic resolution: law_code/article/anchor => stable URL
- Low-noise ingestion: JSONL chunks with rich metadata
- Fast extraction: plain text mirrors anchors ([p2-i1] etc.)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import re
from typing import Any, Iterable

from bs4 import BeautifulSoup


SITE_BASE_URL = "https://jplawdb.github.io/html-preview/ai-law-db"
SITE_ENHANCED_BASE_URL = f"{SITE_BASE_URL}/enhanced"
RESOLVE_LITE_DIR_NAME = "resolve_lite"


def sort_article_key(num: str):
    parts = re.split(r"[-_]", num)
    key = []
    for p in parts:
        if p.isdigit():
            key.append((0, int(p)))
        else:
            key.append((1, p))
    return key


def clean_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def iter_article_files(law_dir: Path) -> list[Path]:
    files = [p for p in law_dir.glob("*.html") if p.name != "index.html"]
    return sorted(files, key=lambda p: sort_article_key(p.stem))


def build_law_aliases() -> dict[str, str]:
    # 手動で強い略称をカバー（必要に応じて増やす）
    return {
        # 所得税法
        "所得税法": "shotokuzei",
        "所法": "shotokuzei",
        # 法人税法
        "法人税法": "hojinzei",
        "法法": "hojinzei",
        # 法人税法施行令/規則
        "法人税法施行令": "hojinzei_seirei",
        "法法令": "hojinzei_seirei",
        "法人税法施行規則": "hojinzei_kisoku",
        "法法規則": "hojinzei_kisoku",
        # 消費税法
        "消費税法": "shohizei",
        "消法": "shohizei",
        # 相続税法
        "相続税法": "sozokuzei",
        "相法": "sozokuzei",
        # 国税通則法
        "国税通則法": "kokuzei_tsusoku",
        "国通法": "kokuzei_tsusoku",
        "通則法": "kokuzei_tsusoku",
        # 租税特別措置法
        "租税特別措置法": "sozei_tokubetsu",
        "租特法": "sozei_tokubetsu",
        "租特": "sozei_tokubetsu",
        "措置法": "sozei_tokubetsu",
        "措法": "sozei_tokubetsu",
        # 租税特別措置法施行令/規則
        "租税特別措置法施行令": "sozei_tokubetsu_seirei",
        "租特令": "sozei_tokubetsu_seirei",
        "措置令": "sozei_tokubetsu_seirei",
        "租税特別措置法施行規則": "sozei_tokubetsu_kisoku",
        "租特規則": "sozei_tokubetsu_kisoku",
        "措置規則": "sozei_tokubetsu_kisoku",
    }


@dataclass(frozen=True)
class ArticleMeta:
    law_title: str
    law_code: str
    law_type: str
    law_num: str
    egov_id: str
    as_of: str
    article: str
    article_title: str
    cite: str
    cite_id: str


def parse_article_meta(html_text: str, law_code: str) -> ArticleMeta:
    soup = BeautifulSoup(html_text, "html.parser")
    article_el = soup.find("article")
    if article_el is None:
        raise ValueError("missing <article>")

    h2 = soup.find("h2")
    article_title = clean_ws(h2.get_text(" ", strip=True) if h2 else "")

    return ArticleMeta(
        law_title=article_el.get("data-law", ""),
        law_code=law_code,
        law_type=article_el.get("data-law-type", ""),
        law_num=article_el.get("data-law-num", ""),
        egov_id=article_el.get("data-egov-id", ""),
        as_of=article_el.get("data-as-of", ""),
        article=article_el.get("data-article", ""),
        article_title=article_title,
        cite=article_el.get("data-cite", ""),
        cite_id=article_el.get("data-cite-id", ""),
    )


def iter_paragraph_blocks(soup: BeautifulSoup) -> Iterable[tuple[Any, list[Any]]]:
    """
    Yields (paragraph_div, children_p_list) where children_p_list are direct <p> children.
    """
    for div in soup.find_all("div"):
        if not div.get("id"):
            continue
        # Paragraph blocks have either data-paragraph or data-cite-id
        if not (div.has_attr("data-paragraph") or div.has_attr("data-cite-id")):
            continue
        ps = [c for c in div.find_all("p", recursive=False)]
        yield div, ps


def build_chunks_and_text(enhanced_dir: Path, out_data_dir: Path, out_text_dir: Path) -> dict[str, Any]:
    out_chunks_dir = out_data_dir / "chunks"
    out_chunks_dir.mkdir(parents=True, exist_ok=True)
    out_text_dir.mkdir(parents=True, exist_ok=True)

    resolve_laws: dict[str, Any] = {}

    for law_dir in sorted([p for p in enhanced_dir.iterdir() if p.is_dir()]):
        law_code = law_dir.name
        article_files = iter_article_files(law_dir)
        if not article_files:
            continue

        first_html = article_files[0].read_text(encoding="utf-8", errors="replace")
        first_meta = parse_article_meta(first_html, law_code)

        resolve_laws[law_code] = {
            "title": first_meta.law_title,
            "type": first_meta.law_type,
            "law_num": first_meta.law_num,
            "egov_id": first_meta.egov_id or None,
            "as_of": first_meta.as_of,
            "index_url": f"{SITE_ENHANCED_BASE_URL}/{law_code}/index.html",
            "articles": {},
        }

        chunks_path = out_chunks_dir / f"{law_code}.jsonl"
        chunks_f = chunks_path.open("w", encoding="utf-8", newline="\n")

        (out_text_dir / law_code).mkdir(parents=True, exist_ok=True)

        try:
            for article_path in article_files:
                html_text = article_path.read_text(encoding="utf-8", errors="replace")
                soup = BeautifulSoup(html_text, "html.parser")

                meta = parse_article_meta(html_text, law_code)
                if not meta.article:
                    continue

                article_url = f"{SITE_ENHANCED_BASE_URL}/{law_code}/{meta.article}.html"
                resolve_laws[law_code]["articles"][meta.article] = {
                    "title": meta.article_title,
                    "url": article_url,
                    "cite": meta.cite,
                    "cite_id": meta.cite_id,
                }

                # Plain text
                txt_path = out_text_dir / law_code / f"{meta.article}.txt"
                txt_lines: list[str] = []
                txt_lines.append(f"law: {meta.law_title} ({meta.law_code})")
                txt_lines.append(f"law_type: {meta.law_type} / law_num: {meta.law_num} / egov_id: {meta.egov_id} / as_of: {meta.as_of}")
                txt_lines.append(f"article: {meta.article} / title: {meta.article_title}")
                txt_lines.append(f"url: {article_url}")
                txt_lines.append("")

                for div, ps in iter_paragraph_blocks(soup):
                    block_id = div.get("id", "")
                    para_num = div.get("data-paragraph", "")
                    cite = div.get("data-cite", "")
                    cite_id = div.get("data-cite-id", "")

                    # paragraph sentence(s): <p> without class="item"
                    sentence_ps = [p for p in ps if "item" not in (p.get("class") or [])]
                    if sentence_ps:
                        sentence_text = clean_ws(sentence_ps[0].get_text(" ", strip=True))
                        if sentence_text:
                            txt_lines.append(f"[{block_id}] {sentence_text}")
                            record = {
                                "id": cite_id or f"{law_code}:{meta.article}:{block_id}",
                                "kind": "paragraph",
                                "law_code": meta.law_code,
                                "law_title": meta.law_title,
                                "law_type": meta.law_type,
                                "law_num": meta.law_num,
                                "egov_id": meta.egov_id or None,
                                "as_of": meta.as_of,
                                "article": meta.article,
                                "article_title": meta.article_title,
                                "paragraph": para_num or None,
                                "item": None,
                                "cite": cite or None,
                                "cite_id": cite_id or None,
                                "url": f"{article_url}#{block_id}",
                                "text": sentence_text,
                                "source_path": str(article_path.relative_to(enhanced_dir.parent)),
                            }
                            chunks_f.write(json.dumps(record, ensure_ascii=False) + "\n")

                    # items
                    item_ps = [p for p in ps if "item" in (p.get("class") or [])]
                    for p in item_ps:
                        item_id = p.get("id", "")
                        item_num = p.get("data-item", "")
                        item_index = p.get("data-item-index", "")
                        item_cite = p.get("data-cite", "")
                        item_cite_id = p.get("data-cite-id", "")
                        item_text = clean_ws(p.get_text(" ", strip=True))
                        if not item_text:
                            continue

                        txt_lines.append(f"[{item_id}] {item_text}")
                        record = {
                            "id": item_cite_id or f"{law_code}:{meta.article}:{item_id}",
                            "kind": "item",
                            "law_code": meta.law_code,
                            "law_title": meta.law_title,
                            "law_type": meta.law_type,
                            "law_num": meta.law_num,
                            "egov_id": meta.egov_id or None,
                            "as_of": meta.as_of,
                            "article": meta.article,
                            "article_title": meta.article_title,
                            "paragraph": para_num or None,
                            "item": {
                                "num": item_num or None,
                                "index": int(item_index) if item_index.isdigit() else None,
                            },
                            "cite": item_cite or None,
                            "cite_id": item_cite_id or None,
                            "url": f"{article_url}#{item_id}",
                            "text": item_text,
                            "source_path": str(article_path.relative_to(enhanced_dir.parent)),
                        }
                        chunks_f.write(json.dumps(record, ensure_ascii=False) + "\n")

                txt_path.write_text("\n".join(txt_lines) + "\n", encoding="utf-8")

        finally:
            chunks_f.close()

    return resolve_laws


def main() -> None:
    base_dir = Path(__file__).resolve().parents[1]  # ai-law-db/
    enhanced_dir = base_dir / "enhanced"
    data_dir = base_dir / "data"
    text_dir = base_dir / "text"

    data_dir.mkdir(parents=True, exist_ok=True)

    aliases = build_law_aliases()
    (data_dir / "law_aliases.json").write_text(
        json.dumps({"aliases": aliases}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    laws = build_chunks_and_text(enhanced_dir, data_dir, text_dir)

    resolve = {
        "as_of": next(iter(laws.values()), {}).get("as_of"),
        "base_url": SITE_BASE_URL,
        "enhanced_base_url": SITE_ENHANCED_BASE_URL,
        "article_url_template": "enhanced/{law_code}/{article}.html",
        "anchors": {
            "paragraph": "#p{paragraph}",
            "item": "#p{paragraph}-i{item_index}",
        },
        "law_aliases": aliases,
        "laws": laws,
        "examples": [
            {
                "query": "租特法57条の8第2項第1号",
                "example_url": f"{SITE_ENHANCED_BASE_URL}/sozei_tokubetsu/57-8.html#p2-i1",
            }
        ],
    }

    (data_dir / "resolve.json").write_text(
        json.dumps(resolve, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    (data_dir / "resolve.min.json").write_text(
        json.dumps(resolve, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    resolve_lite_laws: dict[str, Any] = {}
    for law_code, law in laws.items():
        articles = list((law.get("articles") or {}).keys())
        resolve_lite_laws[law_code] = {
            "title": law.get("title"),
            "type": law.get("type"),
            "law_num": law.get("law_num"),
            "egov_id": law.get("egov_id"),
            "as_of": law.get("as_of"),
            "index_url": law.get("index_url"),
            "articles": sorted(articles, key=sort_article_key),
        }

    resolve_lite = {
        "as_of": resolve.get("as_of"),
        "base_url": SITE_BASE_URL,
        "enhanced_base_url": SITE_ENHANCED_BASE_URL,
        "article_url_template": resolve.get("article_url_template"),
        "text_url_template": "text/{law_code}/{article}.txt",
        "anchors": resolve.get("anchors"),
        "law_aliases": aliases,
        "laws": resolve_lite_laws,
    }

    (data_dir / "resolve_lite.json").write_text(
        json.dumps(resolve_lite, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    resolve_lite_dir = data_dir / RESOLVE_LITE_DIR_NAME
    resolve_lite_dir.mkdir(parents=True, exist_ok=True)

    # Per-law resolver-lite: much smaller download when the law_code is known.
    # Typical flow:
    #   1) data/law_aliases.json => law_code
    #   2) data/resolve_lite/{law_code}.json => available articles
    #   3) text/{law_code}/{article}.txt => content
    for law_code, law in laws.items():
        articles = list((law.get("articles") or {}).keys())
        per_law = {
            "as_of": law.get("as_of"),
            "base_url": SITE_BASE_URL,
            "enhanced_base_url": SITE_ENHANCED_BASE_URL,
            "law_code": law_code,
            "law_title": law.get("title"),
            "law_type": law.get("type"),
            "law_num": law.get("law_num"),
            "egov_id": law.get("egov_id"),
            "index_url": law.get("index_url"),
            "article_url_template": resolve.get("article_url_template"),
            "text_url_template": "text/{law_code}/{article}.txt",
            "anchors": resolve.get("anchors"),
            "articles": sorted(articles, key=sort_article_key),
        }
        (resolve_lite_dir / f"{law_code}.json").write_text(
            json.dumps(per_law, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    # Directory index (lightweight) for discovery.
    index_laws: dict[str, Any] = {}
    for law_code, law in laws.items():
        articles = list((law.get("articles") or {}).keys())
        index_laws[law_code] = {
            "title": law.get("title"),
            "type": law.get("type"),
            "law_num": law.get("law_num"),
            "egov_id": law.get("egov_id"),
            "as_of": law.get("as_of"),
            "index_url": law.get("index_url"),
            "articles_count": len(articles),
            "resolve_lite_url": f"{SITE_BASE_URL}/data/{RESOLVE_LITE_DIR_NAME}/{law_code}.json",
        }

    resolve_lite_index = {
        "as_of": resolve.get("as_of"),
        "base_url": SITE_BASE_URL,
        "enhanced_base_url": SITE_ENHANCED_BASE_URL,
        "article_url_template": resolve.get("article_url_template"),
        "text_url_template": "text/{law_code}/{article}.txt",
        "anchors": resolve.get("anchors"),
        "law_aliases": aliases,
        "laws": index_laws,
    }

    (resolve_lite_dir / "index.json").write_text(
        json.dumps(resolve_lite_index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
