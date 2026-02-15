#!/usr/bin/env python3
"""Generate quickstart.txt (CHAPTER/SECTION/REF indexes) for a given tsutatsu doc_code."""

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from datetime import date

# ── Config per doc_code ──────────────────────────────────────────────

DOC_CONFIGS = {
    "shotokuzei_kihon_tsutatsu": {
        "doc_title": "所得税基本通達",
        "short": "所基通",
        "ref_legend": "法=所得税法 / 令=所得税法施行令 / 規=所得税法施行規則 / 措法=租税特別措置法",
        "url_pattern": r'shotoku/(\d+)/(\d+(?:-\d+)?)',
    },
    "shohizei_kihon_tsutatsu": {
        "doc_title": "消費税基本通達",
        "short": "消基通",
        "ref_legend": "法=消費税法 / 令=消費税法施行令 / 規=消費税法施行規則 / 措法=租税特別措置法",
        "url_pattern": r'shohi/(\d+)/(\d+)',
    },
    "sozokuzei_kihon_tsutatsu": {
        "doc_title": "相続税法基本通達",
        "short": "相基通",
        "ref_legend": "法=相続税法 / 令=相続税法施行令 / 規=相続税法施行規則",
        "url_pattern": r'sozoku2/(\d+)/(\d+)',
    },
    "sozei_tokubetsu_tsutatsu_hojinzei": {
        "doc_title": "租税特別措置法通達（法人税編）",
        "short": "措通(法)",
        "ref_legend": "措法=租税特別措置法 / 措令=措置法施行令 / 措規=措置法施行規則 / 法=法人税法 / 令=法人税法施行令",
        "url_pattern": r'750214/(\d+)/([^.#]+)',  # ch + full filename
        "sec_from_filename": True,  # extract article from filename
    },
    "hyoka_kihon_tsutatsu": {
        "doc_title": "財産評価基本通達",
        "short": "評基通",
        "ref_legend": "法=相続税法 / 令=相続税法施行令 / 規=相続税法施行規則",
        "url_pattern": r'hyoka_new/(\d+)/(\d+(?:_\d+)?)',
    },
    "kokuzei_tsusoku_kihon_tsutatsu": {
        "doc_title": "国税通則法基本通達",
        "short": "通法基通",
        "ref_legend": "法=国税通則法 / 令=国税通則法施行令 / 規=国税通則法施行規則",
        "url_pattern": None,  # all same URL, fallback to item_id
    },
}

# ── Helpers ───────────────────────────────────────────────────────────

def item_id_sort_key(item_id: str):
    """Sort item_ids numerically: '1-1-1' < '1-1-2' < '1-2-1' < '2-1-1'."""
    parts = []
    for p in re.split(r'[-_]', item_id):
        # Handle 9000-style prefixes and common/shared items
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(ord(p[0]) if p else 0)
    return tuple(parts)


def read_text_file(path: str):
    """Read a tsutatsu text file and return (title, body, source_page)."""
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    title = ""
    body_lines = []
    source_page = ""
    in_body = False

    for line in lines:
        line = line.rstrip("\n")
        if line.startswith("item:"):
            m = re.search(r'title:\s*(.+)$', line)
            if m:
                title = m.group(1).strip()
        elif line.startswith("source_page:"):
            source_page = line.split(":", 1)[1].strip()
        elif line.startswith("[p"):
            in_body = True
            body_lines.append(line)
        elif in_body:
            body_lines.append(line)

    return title, "\n".join(body_lines), source_page


def extract_refs(body: str):
    """Extract article references from body text. Returns list of ref strings like '法22', '令4-2'."""
    refs = []
    # Pattern: 法第XX条の... / 令第XX条の... / 規則第XX条 / 措置法第XX条
    for m in re.finditer(r'(法|令|規則|措置法|措令|措規)第(\d+)条(?:の(\d+))?', body):
        prefix = m.group(1)
        num = m.group(2)
        sub = m.group(3)
        # Normalize prefix
        if prefix == "規則":
            prefix = "規"
        elif prefix == "措置法":
            prefix = "措法"
        ref = f"{prefix}{num}"
        if sub:
            ref += f"-{sub}"
        refs.append(ref)
    return refs


def extract_keywords(titles: list[str], max_keywords: int = 5):
    """Extract top keywords from a list of titles."""
    # Remove common particles and stopwords
    stopwords = set("のがをにはでとももからまでなどによるについてにおけるおけるされるするしたないあるいうもの及び又場合とき定めるときにおいて".replace("", ""))
    stop_patterns = re.compile(
        r'^(の|が|を|に|は|で|と|も|から|まで|など|等|する|した|される|ない|ある|いう|'
        r'もの|及び|又は|場合|とき|こと|ため|ところ|おける|おいて|ついて|よる|より|'
        r'意義|定義|範囲|意味|規定|適用|計算|取扱い|取扱)$'
    )

    word_counts = defaultdict(int)
    for title in titles:
        if not title:
            continue
        # Extract runs of kanji/katakana (2+ chars, NO hiragana to split at particles)
        words = re.findall(r'[一-龥々ァ-ヺー]{2,}', title)
        seen = set()
        for w in words:
            if stop_patterns.match(w):
                continue
            if w not in seen:
                word_counts[w] += 1
                seen.add(w)

    # Sort by frequency, then by length (longer = more specific)
    sorted_words = sorted(word_counts.items(), key=lambda x: (-x[1], -len(x[0])))
    return [w for w, _ in sorted_words[:max_keywords]]


def _strip_zeros(s: str) -> str:
    """Strip leading zeros: '01'->'1', '00'->'0', '04a'->'4a'."""
    try:
        return str(int(s))
    except ValueError:
        return s.lstrip('0') or s


def determine_hierarchy(item_ids: list[str], text_dir: str, doc_code: str, config: dict):
    """Determine chapter and section for each item_id using NTA source_page URLs.
    Returns dict: item_id -> (chapter, section)
    """
    url_pattern = config.get("url_pattern")
    sec_from_filename = config.get("sec_from_filename", False)
    result = {}

    if url_pattern is None:
        # kokuzei_tsusoku: no URL structure, fallback to item_id
        for item_id in item_ids:
            parts = item_id.split("-")
            ch = parts[0]
            result[item_id] = (ch, ch)
        return result

    for item_id in item_ids:
        path = os.path.join(text_dir, f"{item_id}.txt")
        if not os.path.exists(path):
            result[item_id] = ("0", "0")
            continue

        _, _, source_page = read_text_file(path)
        m = re.search(url_pattern, source_page)

        if m:
            ch = _strip_zeros(m.group(1))

            if m.lastindex and m.lastindex >= 2:
                sec_raw = m.group(2)

                if sec_from_filename:
                    # sozei_tokubetsu: filename = "{ch}_{article_parts}"
                    # "01_42_03" → strip "01_" → "42_03" → "42-3"
                    fname_parts = sec_raw.split('_', 1)
                    if len(fname_parts) > 1:
                        article = fname_parts[1].replace('_', '-')
                        article = '-'.join(_strip_zeros(p) for p in article.split('-'))
                        sec = article
                    else:
                        sec = _strip_zeros(sec_raw)
                else:
                    # Standard: sec = "{ch}-{sec_raw}" with zeros stripped
                    sec_clean = '-'.join(
                        _strip_zeros(p) for p in sec_raw.replace('_', '-').split('-')
                    )
                    sec = f"{ch}-{sec_clean}"
            else:
                sec = ch

            result[item_id] = (ch, sec)
        else:
            # Fallback to item_id prefix
            parts = item_id.split("-")
            ch = parts[0]
            result[item_id] = (ch, ch)

    return result


# ── Main generation ──────────────────────────────────────────────────

def generate_quickstart(doc_code: str, base_dir: str):
    """Generate quickstart.txt content for the given doc_code."""
    config = DOC_CONFIGS[doc_code]

    # Load resolve_lite
    resolve_path = os.path.join(base_dir, "data", "resolve_lite", f"{doc_code}.json")
    with open(resolve_path, "r", encoding="utf-8") as f:
        resolve = json.load(f)

    all_items = resolve["items"]
    snapshot = resolve.get("snapshot", "unknown")
    text_dir = os.path.join(base_dir, "text", doc_code)

    # Determine hierarchy
    hierarchy = determine_hierarchy(all_items, text_dir, doc_code, config)

    # Collect data per section
    sections_data = defaultdict(lambda: {
        "items": [], "titles": [], "refs": defaultdict(int)
    })

    for item_id in all_items:
        if item_id not in hierarchy:
            continue
        ch, sec = hierarchy[item_id]

        txt_path = os.path.join(text_dir, f"{item_id}.txt")
        if not os.path.exists(txt_path):
            # Still count the item even if file missing
            sections_data[sec]["items"].append(item_id)
            continue

        title, body, _ = read_text_file(txt_path)
        sections_data[sec]["items"].append(item_id)
        sections_data[sec]["titles"].append(title)

        for ref in extract_refs(body):
            sections_data[sec]["refs"][ref] += 1

    # Sort sections
    sorted_sections = sorted(sections_data.keys(), key=item_id_sort_key)

    # Build chapter data
    chapters_data = defaultdict(lambda: {"sections": [], "items": 0})
    for sec in sorted_sections:
        sd = sections_data[sec]
        if not sd["items"]:
            continue
        # Determine chapter from first item's hierarchy
        ch = hierarchy[sd["items"][0]][0]
        if sec not in chapters_data[ch]["sections"]:
            chapters_data[ch]["sections"].append(sec)
        chapters_data[ch]["items"] += len(sd["items"])

    sorted_chapters = sorted(chapters_data.keys(), key=item_id_sort_key)

    # ── Build output ──
    lines = []
    today = date.today().strftime("%Y-%m-%d")

    # Header
    lines.append(f"# ai-tsutatsu-db quickstart（ハイブリッド索引）")
    lines.append("")
    lines.append(f"- 対象: {config['doc_title']}（doc_code: {doc_code}）")
    lines.append(f"- データsnapshot: {snapshot}")
    lines.append(f"- 生成: {today}（このファイルは機械生成。resolve_lite を太らせない設計）")
    lines.append("")

    # URL template
    lines.append("## 最短アクセス（URLテンプレ）")
    lines.append(f"- 本文（推奨）: text/{doc_code}/{{item_id}}.txt")
    lines.append(f"- 引用（段落指定）: enhanced/{doc_code}/{{item_id}}.html#p{{n}}")
    lines.append("")

    # Usage
    lines.append("## 使い方（LLM向け）")
    lines.append("1) 条文番号がある: 下の `REF_INDEX` を全文検索して、該当する `section` を得る")
    lines.append("2) キーワードがある: 下の `SECTION_INDEX` の `label` を全文検索して、該当する `section` を得る")
    lines.append("3) `SECTION_INDEX` の `range`（例: 9-2-1..9-2-19）にある item_id を resolve_lite で確認し、まず text を読む")
    lines.append("   - 迷ったら range の先頭 item_id の text を読む")
    lines.append(f"   - REF_INDEX の略記: {config['ref_legend']}")
    lines.append("")

    # CHAPTER_INDEX
    lines.append("## CHAPTER_INDEX（TSV）")
    lines.append("# ch\tlabel(上位キーワード)\tsections\titems\trange")

    for ch in sorted_chapters:
        cd = chapters_data[ch]
        # Collect all titles across sections in this chapter
        ch_titles = []
        ch_items_list = []
        for sec in cd["sections"]:
            ch_titles.extend(sections_data[sec]["titles"])
            ch_items_list.extend(sections_data[sec]["items"])

        keywords = extract_keywords(ch_titles)
        ch_items_sorted = sorted(ch_items_list, key=item_id_sort_key)
        range_str = f"{ch_items_sorted[0]}..{ch_items_sorted[-1]}" if ch_items_sorted else ""

        lines.append(f"{ch}\t{','.join(keywords)}\t{len(cd['sections'])}\t{cd['items']}\t{range_str}")

    lines.append("")

    # SECTION_INDEX
    lines.append("## SECTION_INDEX（TSV）")
    lines.append("# section\tlabel(上位キーワード)\trefs(頻出)\trange\titems")

    all_section_refs = set()  # For REF_INDEX generation

    for sec in sorted_sections:
        sd = sections_data[sec]
        if not sd["items"]:
            continue

        keywords = extract_keywords(sd["titles"], max_keywords=3)

        # Top refs (up to 4)
        sorted_refs = sorted(sd["refs"].items(), key=lambda x: -x[1])
        top_refs = [r for r, _ in sorted_refs[:4]]
        for r in top_refs:
            all_section_refs.add(r)
        refs_str = "/".join(top_refs)

        items_sorted = sorted(sd["items"], key=item_id_sort_key)
        range_str = f"{items_sorted[0]}..{items_sorted[-1]}" if items_sorted else ""

        lines.append(f"{sec}\t{','.join(keywords)}\t{refs_str}\t{range_str}\t{len(sd['items'])}")

    lines.append("")

    # REF_INDEX
    # Build reverse index: ref -> sections
    ref_to_sections = defaultdict(list)
    ref_items_count = defaultdict(int)

    for sec in sorted_sections:
        sd = sections_data[sec]
        for ref in sd["refs"]:
            if ref in all_section_refs:
                if sec not in ref_to_sections[ref]:
                    ref_to_sections[ref].append(sec)
                ref_items_count[ref] += sd["refs"][ref]

    # Sort refs: 法 → 令 → 規 → 措法 → 措令 → 措規
    def ref_sort_key(ref):
        prefix_order = {"法": 0, "令": 1, "規": 2, "措法": 3, "措令": 4, "措規": 5}
        for pfx in ["措規", "措令", "措法", "規", "令", "法"]:
            if ref.startswith(pfx):
                num_part = ref[len(pfx):]
                nums = []
                for n in num_part.split("-"):
                    try:
                        nums.append(int(n))
                    except ValueError:
                        nums.append(0)
                return (prefix_order.get(pfx, 99), tuple(nums))
        return (99, (0,))

    lines.append("## REF_INDEX（TSV）")
    lines.append("# ref\tsections(上位)\titems_count(合計)")

    for ref in sorted(ref_to_sections.keys(), key=ref_sort_key):
        secs = ref_to_sections[ref]
        count = ref_items_count[ref]
        lines.append(f"{ref}\t{','.join(secs)}\t{count}")

    lines.append("")

    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: generate_quickstart.py <doc_code> [<output_path>]")
        print(f"Available: {', '.join(DOC_CONFIGS.keys())}")
        sys.exit(1)

    doc_code = sys.argv[1]
    if doc_code not in DOC_CONFIGS:
        print(f"Unknown doc_code: {doc_code}")
        print(f"Available: {', '.join(DOC_CONFIGS.keys())}")
        sys.exit(1)

    base_dir = os.path.join(os.path.dirname(__file__), "..")
    base_dir = os.path.abspath(base_dir)

    output = generate_quickstart(doc_code, base_dir)

    # Output filename mapping (plan-defined names)
    output_names = {
        "shotokuzei_kihon_tsutatsu": "quickstart-shotokuzei.txt",
        "shohizei_kihon_tsutatsu": "quickstart-shohizei.txt",
        "sozokuzei_kihon_tsutatsu": "quickstart-sozokuzei.txt",
        "sozei_tokubetsu_tsutatsu_hojinzei": "quickstart-sozei-tokubetsu.txt",
        "hyoka_kihon_tsutatsu": "quickstart-hyoka.txt",
        "kokuzei_tsusoku_kihon_tsutatsu": "quickstart-tsusoku.txt",
    }

    if len(sys.argv) >= 3:
        out_path = sys.argv[2]
    else:
        fname = output_names.get(doc_code, f"quickstart-{doc_code.split('_')[0]}.txt")
        out_path = os.path.join(base_dir, fname)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(output)

    # Stats
    ja_chars = sum(1 for c in output if '\u3000' <= c <= '\u9fff' or '\uff00' <= c <= '\uffef')
    ascii_chars = sum(1 for c in output if c.isascii())
    est_tokens = int(ja_chars * 1.5 + ascii_chars * 0.25)

    print(f"Generated: {out_path}")
    print(f"  Size: {len(output.encode('utf-8')):,} bytes")
    print(f"  Lines: {len(output.splitlines())}")
    print(f"  Est. tokens: ~{est_tokens:,}")


if __name__ == "__main__":
    main()
