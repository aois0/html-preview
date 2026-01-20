#!/usr/bin/env python3
"""
法人税判決のTSV shardsを生成
JSON比で約50%のトークン削減
"""

import json
from pathlib import Path

SOURCE_DIR = Path("/home/user/ai-law-db/simple/hanketsu")
LIST_FILE = Path("/tmp/houjinzei_list.txt")
OUTPUT_DIR = Path("/mnt/c/Users/PC/html-preview/ai-hanketsu-db/houjinzei/data/shards")
SHARD_SIZE = 100

def load_case_ids():
    with open(LIST_FILE, 'r') as f:
        return [line.strip() for line in f if line.strip()]

def extract_row(case_id):
    json_path = SOURCE_DIR / f"{case_id}.json"
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            d = json.load(f)

        kw = ','.join(d.get('keywords', [])[:5])
        laws = ','.join(d.get('laws', [])[:3])
        title = d.get('title', '')[:50].replace('\t', ' ')

        return f"{case_id}\t{d.get('date_iso', '')}\t{d.get('court', '')}\t{title}\t{kw}\t{laws}"
    except:
        return None

def build_shards(case_ids):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    header = "id\tdate\tcourt\ttitle\tkeywords\tlaws"

    index_lines = ["# 法人税判例 shards index", f"# total: {len(case_ids)}", ""]

    for i in range(0, len(case_ids), SHARD_SIZE):
        shard_num = i // SHARD_SIZE
        shard_ids = case_ids[i:i + SHARD_SIZE]

        rows = [header]
        for cid in shard_ids:
            row = extract_row(cid)
            if row:
                rows.append(row)

        # TSV保存
        shard_file = OUTPUT_DIR / f"shard-{shard_num:02d}.txt"
        with open(shard_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(rows))

        print(f"shard-{shard_num:02d}.txt: {len(rows)-1} entries")
        index_lines.append(f"shard-{shard_num:02d}.txt\t{shard_ids[0]}-{shard_ids[-1]}\t{len(rows)-1}")

    # index保存
    index_file = OUTPUT_DIR.parent / "shards_index.txt"
    with open(index_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(index_lines))

    print(f"\nshards_index.txt created")

if __name__ == "__main__":
    case_ids = load_case_ids()
    print(f"法人税判決: {len(case_ids)} 件")
    build_shards(case_ids)
    print("完了!")
