#!/usr/bin/env python3
"""
Build AI-optimized artifacts for NTA Q&A style datasets:
  - 質疑応答事例
  - 文書回答事例
  - タックスアンサー

Design goal (mobile-AI constraint):
  - one read can inspect only first ~10k tokens
  - keep retrieval deterministic and lightweight:
      llms.txt -> data/shards_index.json -> data/shards/shard-XXX.txt -> text/... or enhanced/...

Outputs (under ai-nta-qa-db):
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
  - sitemap.xml
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
import requests


SITE_BASE_URL = "https://jplawdb.github.io/html-preview/ai-nta-qa-db"
SITE_ENHANCED_BASE_URL = f"{SITE_BASE_URL}/enhanced"

ROOT_DIR = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT_DIR / "source" / "cache"

MAX_SHARD_ROWS = 120
SHARD_SNIPPET_CHARS = 100

COMMON_NOISE_LINES = {
    "このページの先頭へ",
    "ページの先頭へ",
    "このページの先頭へ戻る",
    "ページの先頭へ戻る",
    "ホーム",
    "国税庁ホームページ",
}

SOURCE_KIND_LABEL = {
    "shitsugi": "質疑応答事例",
    "bunshokaito": "文書回答事例",
    "taxanswer": "タックスアンサー",
}

BUNSHO_CATEGORY_LABELS = {
    "shotoku": "所得税",
    "gensen": "源泉所得税",
    "gensenshotoku": "源泉所得税",
    "joto-sanrin": "譲渡所得・山林所得",
    "sozoku": "相続税",
    "souzoku": "相続税",
    "zoyo": "贈与税",
    "zouyo": "贈与税",
    "hyoka": "財産評価",
    "hojin": "法人税",
    "shohi": "消費税",
    "shozei": "諸税",
    "inshi": "印紙税",
    "inshi-sonota": "印紙税・その他",
    "inshi_sonota": "印紙税・その他",
    "sonota": "その他の国税",
}

TAXANSWER_CATEGORY_LABELS = {
    "shotoku": "所得税",
    "gensen": "源泉所得税",
    "joto": "譲渡所得",
    "jouto": "譲渡所得",
    "souzoku": "相続税",
    "sozoku": "相続税",
    "zoyo": "贈与税",
    "shohi": "消費税",
    "hojin": "法人税",
    "inshi": "印紙税",
    "hotei": "法定調書",
    "shisan": "財産評価",
    "hyoka": "財産評価",
    "kakutei": "確定申告",
    "fufuku": "不服申立て",
    "osirase": "お知らせ",
    "saigai": "災害",
}


@dataclass(frozen=True)
class SourceSpec:
    key: str
    root_url: str
    path_token: str
    encoding: str
    max_pages: int
    classify_item: Callable[[str], tuple[str, str] | None]


@dataclass
class Item:
    source_kind: str
    doc_key: str
    doc_code: str
    doc_title: str
    item_id: str
    item_title: str
    source_url: str
    lines: list[str]
    snippet: str


SOURCE_SPECS = [
    SourceSpec(
        key="shitsugi",
        root_url="https://www.nta.go.jp/law/shitsugi/01.htm",
        path_token="/law/shitsugi/",
        encoding="cp932",
        max_pages=1600,
        classify_item=lambda u: classify_shitsugi_item(u),
    ),
    SourceSpec(
        key="bunshokaito",
        root_url="https://www.nta.go.jp/law/bunshokaito/01.htm",
        path_token="/bunshokaito/",
        encoding="cp932",
        max_pages=2200,
        classify_item=lambda u: classify_bunshokaito_item(u),
    ),
    SourceSpec(
        key="taxanswer",
        root_url="https://www.nta.go.jp/taxes/shiraberu/taxanswer/index2.htm",
        path_token="/taxes/shiraberu/taxanswer/",
        encoding="utf-8",
        max_pages=2200,
        classify_item=lambda u: classify_taxanswer_item(u),
    ),
]


def clean_ws(s: str) -> str:
    s = (s or "").replace("\u3000", " ")
    return re.sub(r"\s+", " ", s).strip()


def safe_slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "x"


def clean_for_tsv(s: str) -> str:
    return clean_ws(s).replace("\t", " ").replace("\n", " ").strip()


def normalize_url(raw: str) -> str:
    p = urlparse(raw)
    p = p._replace(params="", query="", fragment="")
    scheme = p.scheme or "https"
    netloc = p.netloc
    path = p.path or "/"
    return urlunparse((scheme, netloc, path, "", "", ""))


def natural_sort_key(s: str):
    parts = re.split(r"([0-9]+)", s or "")
    out: list[tuple[int, str | int]] = []
    for p in parts:
        if p.isdigit():
            out.append((0, int(p)))
        else:
            out.append((1, p))
    return out


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def reset_generated_dirs() -> None:
    for rel in ["enhanced", "text", "data/resolve_lite", "data/chunks", "data/shards"]:
        target = ROOT_DIR / rel
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)


def decode_html(data: bytes, preferred: str) -> str:
    tried: list[str] = []
    for enc in [preferred, "utf-8", "cp932", "shift_jis", "euc_jp"]:
        if enc in tried:
            continue
        tried.append(enc)
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def fetch_html(session: requests.Session, spec: SourceSpec, url: str) -> str | None:
    spec_cache_dir = CACHE_DIR / spec.key
    ensure_dir(spec_cache_dir)

    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    cache_path = spec_cache_dir / f"{key}.html"

    if cache_path.exists():
        data = cache_path.read_bytes()
        return decode_html(data, spec.encoding)

    try:
        r = session.get(
            url,
            timeout=60,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; jplawdb-ai-collector/1.0)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
    except Exception:
        return None

    if r.status_code != 200:
        return None

    content_type = (r.headers.get("content-type") or "").lower()
    if "text/html" not in content_type and not url.lower().endswith(".htm"):
        return None

    data = r.content
    cache_path.write_bytes(data)
    return decode_html(data, spec.encoding)


def extract_links(base_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        full = normalize_url(urljoin(base_url, href))
        p = urlparse(full)
        if p.scheme not in {"http", "https"}:
            continue
        if p.netloc != "www.nta.go.jp":
            continue
        out.append(full)
    return out


def is_dead_page(html: str) -> bool:
    hay = html
    return (
        "指定されたページを表示できませんでした" in hay
        or "404 Not Found" in hay
        or "Page Not Found" in hay
    )


def classify_shitsugi_item(url: str) -> tuple[str, str] | None:
    path = urlparse(url).path
    m = re.search(r"/law/shitsugi/([^/]+)/([0-9]{2,4})/([0-9]{2,4})\.htm$", path)
    if not m:
        return None
    cat, a, b = m.group(1), m.group(2), m.group(3)
    if cat == "shinki":
        return None
    return cat, f"{a}-{b}"


def classify_bunshokaito_item(url: str) -> tuple[str, str] | None:
    path = urlparse(url).path
    parts = [p for p in path.split("/") if p]
    if "bunshokaito" not in parts:
        return None

    i = parts.index("bunshokaito")
    if i + 2 >= len(parts):
        return None

    cat = parts[i + 1]
    rest = parts[i + 2 :]
    if not rest:
        return None

    filename = rest[-1]
    if not filename.endswith(".htm"):
        return None

    stem = filename[:-4]
    if len(rest) == 1:
        if re.fullmatch(r"[0-9]{1,3}(?:_1)?", stem):
            return None
        if re.fullmatch(r"[0-9]{4,8}", stem):
            return cat, stem
        return None

    parent = rest[-2]
    if not re.fullmatch(r"[0-9]{4,8}", parent):
        return None

    if stem == "index":
        return cat, f"{parent}-index"
    if re.fullmatch(r"[0-9]{1,3}", stem):
        return cat, f"{parent}-{stem.zfill(2)}"
    return None


def classify_taxanswer_item(url: str) -> tuple[str, str] | None:
    path = urlparse(url).path
    m = re.search(r"/taxes/shiraberu/taxanswer/([^/]+)/([0-9]{4,6})\.htm$", path)
    if not m:
        return None
    cat, item_id = m.group(1), m.group(2)
    if cat in {"code", "navi", "yogo"}:
        return None
    return cat, item_id


def should_follow(spec: SourceSpec, url: str) -> bool:
    p = urlparse(url)
    path = p.path
    if p.netloc != "www.nta.go.jp":
        return False
    if not path.endswith(".htm"):
        return False
    if spec.path_token not in path:
        return False
    return True


def extract_shitsugi_doc_labels(root_html: str, root_url: str) -> dict[str, str]:
    out: dict[str, str] = {}
    soup = BeautifulSoup(root_html, "html.parser")
    for a in soup.select("a[href]"):
        href = normalize_url(urljoin(root_url, a.get("href") or ""))
        text = clean_ws(a.get_text(" ", strip=True))
        m = re.search(r"/law/shitsugi/([^/]+)/01\.htm$", urlparse(href).path)
        if not m:
            continue
        cat = m.group(1)
        if cat == "shinki":
            continue
        if not text or text in COMMON_NOISE_LINES:
            continue
        out[cat] = text
    return out


def extract_bunshokaito_doc_labels() -> dict[str, str]:
    return dict(BUNSHO_CATEGORY_LABELS)


def extract_taxanswer_doc_labels(root_html: str, root_url: str) -> dict[str, str]:
    out = dict(TAXANSWER_CATEGORY_LABELS)
    soup = BeautifulSoup(root_html, "html.parser")
    for a in soup.select("a[href]"):
        href = normalize_url(urljoin(root_url, a.get("href") or ""))
        text = clean_ws(a.get_text(" ", strip=True))
        m = re.search(r"/taxes/shiraberu/taxanswer/code/bunya-([a-z0-9-]+)\.htm$", urlparse(href).path)
        if not m:
            continue
        key = m.group(1)
        if not text or text in COMMON_NOISE_LINES:
            continue
        out[key] = text
    return out


def discover_seed_urls(spec: SourceSpec, root_html: str, root_url: str) -> set[str]:
    seeds: set[str] = {root_url}
    for link in extract_links(root_url, root_html):
        path = urlparse(link).path
        if spec.key == "shitsugi":
            if re.search(r"/law/shitsugi/[^/]+/01\.htm$", path):
                seeds.add(link)
        elif spec.key == "bunshokaito":
            if re.search(r"/law/bunshokaito/[^/]+/[0-9]{1,3}(?:_1)?\.htm$", path):
                seeds.add(link)
            if re.search(r"/law/bunshokaito/sonota/01\.htm$", path):
                seeds.add(link)
        elif spec.key == "taxanswer":
            if re.search(r"/taxes/shiraberu/taxanswer/code/bunya-[a-z0-9-]+\.htm$", path):
                seeds.add(link)
            if re.search(r"/taxes/shiraberu/taxanswer/code/index\.htm$", path):
                seeds.add(link)
            if re.search(r"/taxes/shiraberu/taxanswer/navi/navi\.htm$", path):
                seeds.add(link)
    return seeds


def choose_doc_label(spec: SourceSpec, doc_key: str, labels: dict[str, str]) -> str:
    if doc_key in labels:
        return labels[doc_key]
    if spec.key == "taxanswer" and doc_key.startswith("bunya-"):
        k = doc_key[len("bunya-") :]
        if k in labels:
            return labels[k]
    return doc_key


def normalize_item_id(item_id: str) -> str:
    out = re.sub(r"[^0-9A-Za-z_-]+", "-", item_id or "")
    out = re.sub(r"-{2,}", "-", out)
    out = out.strip("-_")
    return out or "x"


def extract_item_lines(soup: BeautifulSoup) -> list[str]:
    body = soup.select_one("#bodyArea") or soup.find("main") or soup.body
    if body is None:
        return []

    for tag in body.select("script, style, noscript, template"):
        tag.decompose()

    lines: list[str] = []
    tags = ["h2", "h3", "h4", "h5", "p", "li", "dt", "dd", "th", "td"]
    for el in body.find_all(tags):
        t = clean_ws(el.get_text(" ", strip=True))
        if not t:
            continue
        if t in COMMON_NOISE_LINES:
            continue
        if t in {"▲", "▼"}:
            continue
        lines.append(t)

    # fallback when tag-based extraction is too sparse
    if len(lines) < 3:
        raw = body.get_text("\n", strip=True)
        for ln in raw.splitlines():
            t = clean_ws(ln)
            if not t or t in COMMON_NOISE_LINES:
                continue
            lines.append(t)

    # remove consecutive duplicates
    dedup: list[str] = []
    prev = ""
    for ln in lines:
        if ln == prev:
            continue
        dedup.append(ln)
        prev = ln
    return dedup


def build_item(
    session: requests.Session,
    spec: SourceSpec,
    url: str,
    doc_key: str,
    doc_code: str,
    doc_title: str,
) -> Item | None:
    html = fetch_html(session, spec, url)
    if not html or is_dead_page(html):
        return None

    soup = BeautifulSoup(html, "html.parser")
    title = clean_ws((soup.find("h1") or soup.find("title") or soup.new_tag("x")).get_text(" ", strip=True))
    if not title:
        title = f"{doc_title} {urlparse(url).path.rsplit('/', 1)[-1]}"

    lines = extract_item_lines(soup)
    if not lines:
        return None

    # drop duplicated title at head
    if lines and title and lines[0] == title:
        lines = lines[1:]
    if not lines:
        return None

    item_cls = spec.classify_item(url)
    if not item_cls:
        return None
    _, raw_item_id = item_cls

    snippet = clean_ws(lines[0])[:SHARD_SNIPPET_CHARS]

    return Item(
        source_kind=spec.key,
        doc_key=doc_key,
        doc_code=doc_code,
        doc_title=doc_title,
        item_id=normalize_item_id(raw_item_id),
        item_title=title,
        source_url=url,
        lines=lines,
        snippet=snippet,
    )


def crawl_source(
    session: requests.Session,
    spec: SourceSpec,
) -> tuple[dict[str, str], list[Item]]:
    print(f"[{spec.key}] fetch root: {spec.root_url}")
    root_html = fetch_html(session, spec, spec.root_url)
    if not root_html:
        raise RuntimeError(f"failed to fetch root: {spec.root_url}")

    if spec.key == "shitsugi":
        doc_labels = extract_shitsugi_doc_labels(root_html, spec.root_url)
    elif spec.key == "bunshokaito":
        doc_labels = extract_bunshokaito_doc_labels()
    else:
        doc_labels = extract_taxanswer_doc_labels(root_html, spec.root_url)

    seed_urls = discover_seed_urls(spec, root_html, spec.root_url)
    queue = deque(sorted(seed_urls))
    queued = set(seed_urls)
    visited: set[str] = set()
    item_urls: dict[tuple[str, str], str] = {}

    while queue and len(visited) < spec.max_pages:
        url = queue.popleft()
        visited.add(url)
        html = fetch_html(session, spec, url)
        if not html or is_dead_page(html):
            continue

        for link in extract_links(url, html):
            if not should_follow(spec, link):
                continue

            cls = spec.classify_item(link)
            if cls is not None:
                item_urls.setdefault(cls, link)
                continue

            if link not in visited and link not in queued:
                queue.append(link)
                queued.add(link)

    print(f"[{spec.key}] crawled pages={len(visited)}, item candidates={len(item_urls)}")

    # build item bodies
    out_items: list[Item] = []
    used_ids: dict[str, set[str]] = defaultdict(set)

    sorted_items = sorted(item_urls.items(), key=lambda kv: (kv[0][0], natural_sort_key(kv[0][1])))
    for (doc_key, _raw_item_id), url in sorted_items:
        label = choose_doc_label(spec, doc_key, doc_labels)
        doc_code = f"{spec.key}_{safe_slug(doc_key)}"
        doc_title = f"{SOURCE_KIND_LABEL[spec.key]}（{label}）"

        item = build_item(session, spec, url, doc_key, doc_code, doc_title)
        if item is None:
            continue

        # avoid collisions inside a doc
        iid = item.item_id
        if iid in used_ids[item.doc_code]:
            suffix = hashlib.sha1(url.encode("utf-8")).hexdigest()[:6]
            iid = f"{iid}-{suffix}"
        used_ids[item.doc_code].add(iid)
        item.item_id = iid

        out_items.append(item)

    print(f"[{spec.key}] valid items={len(out_items)}")
    return doc_labels, out_items


def write_html_item(item: Item) -> None:
    out_dir = ROOT_DIR / "enhanced" / item.doc_code
    ensure_dir(out_dir)
    out_path = out_dir / f"{item.item_id}.html"

    paras = []
    for i, ln in enumerate(item.lines, start=1):
        paras.append(f'<li id="p{i}">{escape(ln)}</li>')
    paras_html = "\n      ".join(paras)

    text_rel = f"../../text/{item.doc_code}/{item.item_id}.txt"
    body = f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(item.item_title)}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, 'Segoe UI', sans-serif; line-height: 1.7; max-width: 920px; margin: 0 auto; padding: 24px; }}
    .meta {{ color: #555; font-size: 14px; }}
    code {{ background: #f3f3f3; padding: 1px 5px; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>{escape(item.item_title)}</h1>
  <p class="meta">{escape(item.doc_title)} / item: <code>{escape(item.item_id)}</code></p>
  <p class="meta">source: <a href="{escape(item.source_url)}">{escape(item.source_url)}</a></p>
  <p class="meta">text: <a href="{escape(text_rel)}">{escape(text_rel)}</a></p>
  <ol>
      {paras_html}
  </ol>
</body>
</html>
"""
    out_path.write_text(body, encoding="utf-8")


def write_text_item(item: Item) -> None:
    out_dir = ROOT_DIR / "text" / item.doc_code
    ensure_dir(out_dir)
    out_path = out_dir / f"{item.item_id}.txt"

    lines = [
        f"title: {item.item_title}",
        f"doc_title: {item.doc_title}",
        f"doc_code: {item.doc_code}",
        f"item_id: {item.item_id}",
        f"source_kind: {item.source_kind}",
        f"source_url: {item.source_url}",
        "---",
        *item.lines,
        "",
    ]
    out_path.write_text("\n".join(lines), encoding="utf-8")


def write_doc_index(doc_code: str, doc_title: str, items: list[Item]) -> None:
    out_dir = ROOT_DIR / "enhanced" / doc_code
    ensure_dir(out_dir)
    out_path = out_dir / "index.html"

    links = []
    for it in sorted(items, key=lambda x: natural_sort_key(x.item_id)):
        links.append(f'<li><a href="{escape(it.item_id)}.html">{escape(it.item_id)}: {escape(it.item_title)}</a></li>')
    links_html = "\n    ".join(links)

    body = f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(doc_title)}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, 'Segoe UI', sans-serif; line-height: 1.7; max-width: 920px; margin: 0 auto; padding: 24px; }}
    .meta {{ color: #555; font-size: 14px; }}
  </style>
</head>
<body>
  <h1>{escape(doc_title)}</h1>
  <p class="meta">items: {len(items)}</p>
  <ul>
    {links_html}
  </ul>
</body>
</html>
"""
    out_path.write_text(body, encoding="utf-8")


def write_chunks_jsonl(snapshot: str, doc_code: str, doc_title: str, items: list[Item]) -> None:
    out_path = ROOT_DIR / "data" / "chunks" / f"{doc_code}.jsonl"
    ensure_dir(out_path.parent)

    with out_path.open("w", encoding="utf-8") as f:
        for it in sorted(items, key=lambda x: natural_sort_key(x.item_id)):
            for p_idx, p in enumerate(it.lines, start=1):
                rec = {
                    "id": f"{doc_code}:{it.item_id}:p{p_idx}",
                    "kind": "paragraph",
                    "source_kind": it.source_kind,
                    "snapshot": snapshot,
                    "doc_code": doc_code,
                    "doc_title": doc_title,
                    "item_id": it.item_id,
                    "item_title": it.item_title,
                    "paragraph": p_idx,
                    "url": f"{SITE_ENHANCED_BASE_URL}/{doc_code}/{it.item_id}.html#p{p_idx}",
                    "text": p,
                    "source_url": it.source_url,
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def write_resolve_lite(snapshot: str, doc_code: str, doc_title: str, items: list[Item]) -> None:
    out_path = ROOT_DIR / "data" / "resolve_lite" / f"{doc_code}.json"
    ensure_dir(out_path.parent)

    payload = {
        "snapshot": snapshot,
        "base_url": SITE_BASE_URL,
        "enhanced_base_url": SITE_ENHANCED_BASE_URL,
        "doc_code": doc_code,
        "doc_title": doc_title,
        "index_url": f"{SITE_ENHANCED_BASE_URL}/{doc_code}/index.html",
        "item_url_template": "enhanced/{doc_code}/{item_id}.html",
        "text_url_template": "text/{doc_code}/{item_id}.txt",
        "anchors": {"paragraph": "#p{paragraph}"},
        "items": [it.item_id for it in sorted(items, key=lambda x: natural_sort_key(x.item_id))],
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_docs_index_tsv(rows: list[dict]) -> None:
    out_path = ROOT_DIR / "data" / "docs_index.tsv"
    ensure_dir(out_path.parent)
    with out_path.open("w", encoding="utf-8") as f:
        f.write("doc_code\tdoc_title\tsource_kind\titems_count\tindex_url\tresolve_lite_url\n")
        for row in rows:
            f.write(
                "\t".join(
                    [
                        row["doc_code"],
                        clean_for_tsv(row["doc_title"]),
                        row["source_kind"],
                        str(row["items_count"]),
                        row["index_url"],
                        row["resolve_lite_url"],
                    ]
                )
                + "\n"
            )


def write_shards(item_rows: list[dict], docs_meta: list[dict]) -> None:
    shards_dir = ROOT_DIR / "data" / "shards"
    ensure_dir(shards_dir)

    item_rows = sorted(
        item_rows,
        key=lambda r: (
            r["source_kind"],
            r["doc_code"],
            natural_sort_key(r["item_id"]),
        ),
    )

    shard_entries = []
    for i in range(0, len(item_rows), MAX_SHARD_ROWS):
        shard_num = (i // MAX_SHARD_ROWS) + 1
        shard_id = f"shard-{shard_num:03d}"
        part = item_rows[i : i + MAX_SHARD_ROWS]
        rel_file = f"data/shards/{shard_id}.txt"
        out_path = ROOT_DIR / rel_file

        with out_path.open("w", encoding="utf-8") as f:
            f.write("id\tsource_kind\tdoc_code\tdoc_title\titem_id\titem_title\tsnippet\turl\ttext_url\tsource_url\n")
            for r in part:
                vals = [
                    r["id"],
                    r["source_kind"],
                    r["doc_code"],
                    clean_for_tsv(r["doc_title"]),
                    r["item_id"],
                    clean_for_tsv(r["item_title"]),
                    clean_for_tsv(r["snippet"]),
                    r["url"],
                    r["text_url"],
                    r["source_url"],
                ]
                f.write("\t".join(vals) + "\n")

        shard_entries.append(
            {
                "shard_id": shard_id,
                "file": rel_file,
                "rows": len(part),
                "source_kinds": sorted({r["source_kind"] for r in part}),
                "doc_codes": sorted({r["doc_code"] for r in part}),
            }
        )

    payload = {
        "base_url": SITE_BASE_URL,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_rows": len(item_rows),
        "shard_size": MAX_SHARD_ROWS,
        "fields": [
            "id",
            "source_kind",
            "doc_code",
            "doc_title",
            "item_id",
            "item_title",
            "snippet",
            "url",
            "text_url",
            "source_url",
        ],
        "docs": docs_meta,
        "shards": shard_entries,
    }
    (ROOT_DIR / "data" / "shards_index.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_resolve_lite_index(snapshot: str, docs_meta: list[dict], aliases: dict[str, str]) -> None:
    docs: dict[str, dict] = {}
    for d in docs_meta:
        docs[d["doc_code"]] = {
            "title": d["doc_title"],
            "source_kind": d["source_kind"],
            "snapshot": snapshot,
            "index_url": d["index_url"],
            "items_count": d["items_count"],
            "resolve_lite_url": d["resolve_lite_url"],
        }

    payload = {
        "base_url": SITE_BASE_URL,
        "enhanced_base_url": SITE_ENHANCED_BASE_URL,
        "item_url_template": "enhanced/{doc_code}/{item_id}.html",
        "text_url_template": "text/{doc_code}/{item_id}.txt",
        "anchors": {"paragraph": "#p{paragraph}"},
        "doc_aliases": aliases,
        "docs": docs,
    }
    out_path = ROOT_DIR / "data" / "resolve_lite" / "index.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_doc_aliases(aliases: dict[str, str]) -> None:
    out_path = ROOT_DIR / "data" / "doc_aliases.json"
    out_path.write_text(json.dumps(aliases, ensure_ascii=False, indent=2), encoding="utf-8")


def write_quickstart(docs_meta: list[dict]) -> None:
    lines = [
        "# AI向け NTA QA DB Quickstart",
        "",
        "前提: 1回のReadで先頭1万トークン程度しか読めない制約を想定。",
        "推奨フロー:",
        "1) `data/shards_index.json` で shard を選ぶ",
        "2) `data/shards/shard-XXX.txt` で候補を絞る",
        "3) `text/{doc_code}/{item_id}.txt` で本文確認",
        "4) 根拠URLが必要なら `enhanced/{doc_code}/{item_id}.html#pX` を使う",
        "",
        "主要入口:",
        "- `llms.txt`",
        "- `data/docs_index.tsv`",
        "- `data/resolve_lite/index.json`",
        "",
        "収録ドキュメント:",
    ]
    for d in sorted(docs_meta, key=lambda x: (x["source_kind"], x["doc_code"])):
        lines.append(f"- {d['doc_code']} : {d['doc_title']} ({d['items_count']} items)")
    lines.append("")
    (ROOT_DIR / "quickstart.txt").write_text("\n".join(lines), encoding="utf-8")


def write_llms(docs_meta: list[dict]) -> None:
    by_source: dict[str, list[dict]] = defaultdict(list)
    for d in docs_meta:
        by_source[d["source_kind"]].append(d)

    lines = [
        "# AI向け NTA QA DB（質疑応答事例・文書回答事例・タックスアンサー）",
        f"# Base URL: {SITE_BASE_URL}/",
        "",
        "入口（推奨）",
        "- `quickstart.txt`",
        "- `data/shards_index.json`",
        "- `data/docs_index.tsv`",
        "- `data/resolve_lite/index.json`",
        "",
        "高速フロー（10kトークン制約向け）",
        "1) `data/shards_index.json` で対象 shard を選ぶ",
        "2) `data/shards/shard-XXX.txt` を検索して `doc_code/item_id` を決める",
        "3) `text/{doc_code}/{item_id}.txt` を読む",
        "4) 引用URLは `enhanced/{doc_code}/{item_id}.html#pX`",
        "",
        "収録カテゴリ",
    ]
    for source_kind in ["shitsugi", "bunshokaito", "taxanswer"]:
        docs = sorted(by_source.get(source_kind, []), key=lambda x: x["doc_code"])
        lines.append(f"- {SOURCE_KIND_LABEL[source_kind]}:")
        for d in docs:
            lines.append(f"  - `{d['doc_code']}`: {d['doc_title']} ({d['items_count']})")

    lines.append("")
    (ROOT_DIR / "llms.txt").write_text("\n".join(lines), encoding="utf-8")


def write_index_html(docs_meta: list[dict]) -> None:
    lis = []
    for d in sorted(docs_meta, key=lambda x: (x["source_kind"], x["doc_code"])):
        rel = f"enhanced/{d['doc_code']}/index.html"
        lis.append(
            f'<li><a href="{escape(rel)}">{escape(d["doc_title"])}</a> '
            f'(<code>{escape(d["doc_code"])}</code>, {d["items_count"]}件)</li>'
        )
    lis_html = "\n    ".join(lis)

    html = f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI向け NTA QA DB</title>
  <meta name="description" content="国税庁の質疑応答事例・文書回答事例・タックスアンサーをAI向けに最適化したDB" />
  <link rel="alternate" type="text/plain" href="llms.txt" title="LLM Site Map" />
  <style>
    body {{ font-family: system-ui, -apple-system, 'Segoe UI', sans-serif; line-height: 1.7; max-width: 900px; margin: 0 auto; padding: 24px; }}
    .meta {{ color: #555; }}
    code {{ background: #f3f3f3; padding: 0 6px; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>AI向け NTA QA DB</h1>
  <p class="meta">質疑応答事例・文書回答事例・タックスアンサーを、1項目=1ページで提供します。</p>
  <ul>
    <li><a href="quickstart.txt">quickstart.txt</a></li>
    <li><a href="llms.txt">llms.txt</a></li>
    <li><a href="data/shards_index.json">data/shards_index.json</a></li>
    <li><a href="data/docs_index.tsv">data/docs_index.tsv</a></li>
    <li><a href="data/resolve_lite/index.json">data/resolve_lite/index.json</a></li>
  </ul>
  <h2>ドキュメント一覧</h2>
  <ul>
    {lis_html}
  </ul>
</body>
</html>
"""
    (ROOT_DIR / "index.html").write_text(html, encoding="utf-8")


def write_robots() -> None:
    (ROOT_DIR / "robots.txt").write_text("User-agent: *\nAllow: /\n", encoding="utf-8")


def write_sitemap(docs_meta: list[dict], items_by_doc: dict[str, list[Item]]) -> None:
    urls: list[str] = [
        f"{SITE_BASE_URL}/",
        f"{SITE_BASE_URL}/index.html",
        f"{SITE_BASE_URL}/llms.txt",
        f"{SITE_BASE_URL}/quickstart.txt",
        f"{SITE_BASE_URL}/data/doc_aliases.json",
        f"{SITE_BASE_URL}/data/docs_index.tsv",
        f"{SITE_BASE_URL}/data/resolve_lite/index.json",
        f"{SITE_BASE_URL}/data/shards_index.json",
    ]

    for d in docs_meta:
        urls.append(f"{SITE_ENHANCED_BASE_URL}/{d['doc_code']}/index.html")
        urls.append(f"{SITE_BASE_URL}/data/resolve_lite/{d['doc_code']}.json")
        urls.append(f"{SITE_BASE_URL}/data/chunks/{d['doc_code']}.jsonl")
        for it in items_by_doc.get(d["doc_code"], []):
            urls.append(f"{SITE_ENHANCED_BASE_URL}/{d['doc_code']}/{it.item_id}.html")

    # dedupe while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        deduped.append(u)

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in deduped:
        lines.append(f"  <url><loc>{escape(u)}</loc></url>")
    lines.append("</urlset>")
    (ROOT_DIR / "sitemap.xml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ensure_dir(CACHE_DIR)
    reset_generated_dirs()

    snapshot = datetime.now(timezone.utc).date().isoformat()
    session = requests.Session()

    all_items: list[Item] = []
    aliases: dict[str, str] = {}

    for spec in SOURCE_SPECS:
        doc_labels, items = crawl_source(session, spec)
        all_items.extend(items)

        # aliases from discovered category names
        for doc_key, label in doc_labels.items():
            doc_code = f"{spec.key}_{safe_slug(doc_key)}"
            aliases[label] = doc_code
            aliases[f"{SOURCE_KIND_LABEL[spec.key]}（{label}）"] = doc_code
            aliases[doc_key] = doc_code

    if not all_items:
        raise RuntimeError("No items built. Check source site reachability / parser rules.")

    items_by_doc: dict[str, list[Item]] = defaultdict(list)
    for it in all_items:
        items_by_doc[it.doc_code].append(it)

    docs_meta: list[dict] = []
    shard_rows: list[dict] = []

    for doc_code, items in sorted(items_by_doc.items(), key=lambda kv: kv[0]):
        # stable sort
        items.sort(key=lambda x: natural_sort_key(x.item_id))
        doc_title = items[0].doc_title
        source_kind = items[0].source_kind

        write_doc_index(doc_code, doc_title, items)
        write_chunks_jsonl(snapshot, doc_code, doc_title, items)
        write_resolve_lite(snapshot, doc_code, doc_title, items)

        for it in items:
            write_html_item(it)
            write_text_item(it)
            shard_rows.append(
                {
                    "id": f"{doc_code}:{it.item_id}",
                    "source_kind": source_kind,
                    "doc_code": doc_code,
                    "doc_title": doc_title,
                    "item_id": it.item_id,
                    "item_title": it.item_title,
                    "snippet": it.snippet,
                    "url": f"{SITE_ENHANCED_BASE_URL}/{doc_code}/{it.item_id}.html",
                    "text_url": f"{SITE_BASE_URL}/text/{doc_code}/{it.item_id}.txt",
                    "source_url": it.source_url,
                }
            )

        docs_meta.append(
            {
                "doc_code": doc_code,
                "doc_title": doc_title,
                "source_kind": source_kind,
                "items_count": len(items),
                "index_url": f"{SITE_ENHANCED_BASE_URL}/{doc_code}/index.html",
                "resolve_lite_url": f"{SITE_BASE_URL}/data/resolve_lite/{doc_code}.json",
            }
        )

        aliases[doc_title] = doc_code
        aliases[doc_code] = doc_code

    # stable aliases (sort by key for deterministic output)
    aliases = {k: aliases[k] for k in sorted(aliases.keys())}

    write_doc_aliases(aliases)
    write_docs_index_tsv(docs_meta)
    write_shards(shard_rows, docs_meta)
    write_resolve_lite_index(snapshot, docs_meta, aliases)
    write_quickstart(docs_meta)
    write_llms(docs_meta)
    write_index_html(docs_meta)
    write_robots()
    write_sitemap(docs_meta, items_by_doc)

    print(f"done: docs={len(docs_meta)}, items={len(all_items)}, snapshot={snapshot}")


if __name__ == "__main__":
    main()
