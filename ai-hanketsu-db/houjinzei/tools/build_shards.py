#!/usr/bin/env python3
"""
法人税判決のshardsを生成するスクリプト
- 990件を10 shardsに分割（100件/shard）
- 各shard: 約8,000トークン以内
"""

import json
import os
from pathlib import Path

# パス設定
SOURCE_DIR = Path("/home/user/ai-law-db/simple/hanketsu")
LIST_FILE = Path("/tmp/houjinzei_list.txt")
OUTPUT_DIR = Path("/mnt/c/Users/PC/html-preview/ai-hanketsu-db/houjinzei/data/shards")
SHARD_SIZE = 100

def load_case_ids():
    """法人税判決のcase_idリストを読み込み"""
    with open(LIST_FILE, 'r') as f:
        return [line.strip() for line in f if line.strip()]

def extract_metadata(case_id):
    """JSONから軽量メタデータを抽出"""
    json_path = SOURCE_DIR / f"{case_id}.json"
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # 争点を抽出（textの「争点」セクションから簡易抽出、なければtopics使用）
        text = data.get('text', '')
        issues = []

        # 簡易的に争点を推測（キーワードから）
        keywords = data.get('keywords', [])

        return {
            "id": case_id,
            "date": data.get('date_iso', ''),
            "court": data.get('court', ''),
            "title": data.get('title', '')[:50],  # 長すぎる場合は切り詰め
            "result": data.get('result', ''),
            "keywords": keywords[:5],  # 最大5個
            "laws": data.get('laws', [])[:3],  # 最大3個
        }
    except Exception as e:
        print(f"Error processing {case_id}: {e}")
        return None

def build_shards(case_ids):
    """shardsを生成"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    shards_index = []

    for i in range(0, len(case_ids), SHARD_SIZE):
        shard_num = i // SHARD_SIZE
        shard_ids = case_ids[i:i + SHARD_SIZE]

        # メタデータを抽出
        entries = []
        for cid in shard_ids:
            meta = extract_metadata(cid)
            if meta:
                entries.append(meta)

        # shard保存
        shard_file = OUTPUT_DIR / f"shard-{shard_num:02d}.json"
        with open(shard_file, 'w', encoding='utf-8') as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)

        # インデックス用情報
        if entries:
            # このshardの主なキーワードを集約
            all_keywords = []
            for e in entries:
                all_keywords.extend(e.get('keywords', []))
            top_keywords = list(set(all_keywords))[:10]

            shards_index.append({
                "file": f"shard-{shard_num:02d}.json",
                "range": f"{shard_ids[0]}-{shard_ids[-1]}",
                "count": len(entries),
                "sample_keywords": top_keywords
            })

        print(f"shard-{shard_num:02d}.json: {len(entries)} entries")

    # shards_index.json を保存
    index_file = OUTPUT_DIR.parent / "shards_index.json"
    with open(index_file, 'w', encoding='utf-8') as f:
        json.dump({
            "total": len(case_ids),
            "shard_size": SHARD_SIZE,
            "shards": shards_index
        }, f, ensure_ascii=False, indent=2)

    print(f"\nshards_index.json: {len(shards_index)} shards")

if __name__ == "__main__":
    case_ids = load_case_ids()
    print(f"法人税判決: {len(case_ids)} 件")
    build_shards(case_ids)
    print("完了!")
