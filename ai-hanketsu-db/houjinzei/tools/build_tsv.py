#!/usr/bin/env python3
"""
法人税判決のTSV shardsを生成（AI検索向け）
JSON比でトークン削減（TSV + 不要語削除 + 裁判所略称）
"""

import json
import re
from collections import Counter
from pathlib import Path

SOURCE_DIR = Path("/home/user/ai-law-db/simple/hanketsu")
LIST_FILE = Path("/tmp/houjinzei_list.txt")
OUTPUT_DIR = Path("/mnt/c/Users/PC/html-preview/ai-hanketsu-db/houjinzei/data/shards")
SHARD_SIZE = 100

# 裁判所略称マッピング
COURT_ABBREV = {
    '東京地方裁判所': '東京地裁',
    '東京高等裁判所': '東京高裁',
    '大阪地方裁判所': '大阪地裁',
    '大阪高等裁判所': '大阪高裁',
    '名古屋地方裁判所': '名古屋地裁',
    '名古屋高等裁判所': '名古屋高裁',
    '福岡地方裁判所': '福岡地裁',
    '福岡高等裁判所': '福岡高裁',
    '最高裁判所': '最高裁',
}

# title削除対象
TITLE_REMOVE = ['更正処分', '決定処分', '等', '取消請求', '控訴', '上告', '事件']

# keywords削除対象
KEYWORD_REMOVE = {'更正処分', '課税処分', '決定処分'}


def shorten_court(court: str) -> str:
    """裁判所名を略称化"""
    if court in COURT_ABBREV:
        return COURT_ABBREV[court]
    # その他: 正規表現で置換
    court = re.sub(r'地方裁判所', '地裁', court)
    court = re.sub(r'高等裁判所', '高裁', court)
    return court


def clean_title(title: str) -> str:
    """titleから不要語を削除"""
    for word in TITLE_REMOVE:
        title = title.replace(word, '')
    return title.strip()


def clean_keywords(keywords: list) -> list:
    """keywordsから不要語を削除"""
    return [kw for kw in keywords if kw not in KEYWORD_REMOVE]


def load_case_ids():
    with open(LIST_FILE, 'r') as f:
        return [line.strip() for line in f if line.strip()]


def extract_row(case_id):
    json_path = SOURCE_DIR / f"{case_id}.json"
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            d = json.load(f)

        # topics（カンマ区切り）
        topics = ','.join(d.get('topics', []))

        # keywords（不要語削除、最大5個）
        kw_list = clean_keywords(d.get('keywords', []))[:5]
        kw = ','.join(kw_list)

        # laws（最大5個に緩和）
        laws = ','.join(d.get('laws', [])[:5])

        # title（簡略化）
        title = clean_title(d.get('title', ''))[:50].replace('\t', ' ')

        # court（略称化）
        court = shorten_court(d.get('court', ''))

        tsv = f"{case_id}\t{d.get('date_iso', '')}\t{court}\t{title}\t{topics}\t{kw}\t{laws}"
        return tsv, kw_list
    except:
        return None


def build_shards(case_ids):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    header = "id\tdate\tcourt\ttitle\ttopics\tkeywords\tlaws"

    index_lines = ["# 法人税判例 shards index", f"# total: {len(case_ids)}", ""]
    shards_index_json = []

    for i in range(0, len(case_ids), SHARD_SIZE):
        shard_num = i // SHARD_SIZE
        shard_ids = case_ids[i:i + SHARD_SIZE]

        rows = [header]
        kw_counter = Counter()
        for cid in shard_ids:
            res = extract_row(cid)
            if not res:
                continue
            row, kw_list = res
            rows.append(row)
            kw_counter.update(kw_list)

        # TSV保存
        shard_file = OUTPUT_DIR / f"shard-{shard_num:02d}.txt"
        with open(shard_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(rows))

        print(f"shard-{shard_num:02d}.txt: {len(rows)-1} entries")
        index_lines.append(f"shard-{shard_num:02d}.txt\t{shard_ids[0]}-{shard_ids[-1]}\t{len(rows)-1}")

        shards_index_json.append({
            "file": f"data/shards/shard-{shard_num:02d}.txt",
            "range": f"{shard_ids[0]}-{shard_ids[-1]}",
            "count": len(rows) - 1,
            "sample_keywords": [kw for kw, _ in kw_counter.most_common(10)],
        })

    # index保存
    index_file = OUTPUT_DIR.parent / "shards_index.txt"
    with open(index_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(index_lines))

    # JSON index保存（AI向け）
    json_index_file = OUTPUT_DIR.parent / "shards_index.json"
    with open(json_index_file, 'w', encoding='utf-8') as f:
        json.dump({
            "total": len(case_ids),
            "shard_size": SHARD_SIZE,
            "format": "tsv",
            "fields": ["id", "date", "court", "title", "topics", "keywords", "laws"],
            "shards": shards_index_json,
        }, f, ensure_ascii=False, indent=2)

    print(f"\nshards_index.txt created")


if __name__ == "__main__":
    case_ids = load_case_ids()
    print(f"法人税判決: {len(case_ids)} 件")
    build_shards(case_ids)
    print("完了!")
