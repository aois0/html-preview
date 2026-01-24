from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

SOURCE_DIR = Path("/home/user/ai-law-db/simple/saiketsu")
BASE_DIR = Path(__file__).resolve().parent.parent
CORE_DIR = BASE_DIR / "core"
SHARDS_DIR = BASE_DIR / "data" / "shards"
TARGET_TAX = "法人税"

SENTENCE_SPLIT_RE = re.compile(r'(?<=[。！？])\s*')
CONCLUSION_KEYWORDS = [
    "適法",
    "違法",
    "棄却",
    "取消",
    "認められ",
    "認める",
    "却下",
    "認定",
    "申告",
]
MARKER_CUTS = [
    "トップに戻る",
    "Copyright",
    "審判所の概要",
    "審査請求の状況",
    "制度の概要図",
    "調査の結果",
    "パンフレット等",
    "お知らせ",
]


@dataclass
class CaseRecord:
    case_id: str
    collection: int
    ruling_num: int
    number: str
    date_iso: str
    url: str
    tax_types: List[str]
    title: str
    text: str
    source_path: Path


def load_case_records() -> List[CaseRecord]:
    records: List[CaseRecord] = []
    for path in sorted(SOURCE_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue

        tax_types = [str(t).strip() for t in (data.get("tax_types") or []) if str(t).strip()]
        if TARGET_TAX not in tax_types:
            continue

        try:
            collection = int(str(data.get("collection", "")).strip())
        except ValueError:
            continue

        try:
            ruling_num = int(str(data.get("ruling_num", "")).strip())
        except ValueError:
            continue

        case_id = f"{collection}_{ruling_num:02d}"
        number = data.get("number") or f"裁決事例集No.{collection}-{ruling_num:02d}"
        date_iso = data.get("date_iso") or data.get("date") or ""

        title = data.get("title") or ""
        text = str(data.get("text") or "")

        records.append(
            CaseRecord(
                case_id=case_id,
                collection=collection,
                ruling_num=ruling_num,
                number=number.strip(),
                date_iso=str(date_iso).strip(),
                url=str(data.get("url") or ""),
                tax_types=tax_types,
                title=title.strip(),
                text=text.strip(),
                source_path=path,
            )
        )

    records.sort(key=lambda r: (r.collection, r.ruling_num))
    return records


def split_sentences(text: str) -> List[str]:
    clean = text.replace("\r", " ")
    segments = [seg.strip() for seg in SENTENCE_SPLIT_RE.split(clean) if seg.strip()]
    return segments


def extract_issue_hint(text: str) -> str:
    sentences = split_sentences(text)
    for s in sentences:
        if "争点" in s:
            return s
    if "事案" in text:
        for idx, s in enumerate(sentences):
            if "事案" in s and idx + 1 < len(sentences):
                return sentences[idx + 1]
    if sentences:
        return sentences[0]
    return ""


def extract_case_summary(text: str, max_sentences: int = 2) -> str:
    sentences = split_sentences(text)
    if not sentences:
        return ""
    for idx, s in enumerate(sentences):
        if "事案" in s and idx + 1 < len(sentences):
            snippet = sentences[idx : min(len(sentences), idx + max_sentences)]
            return " ".join(snippet)
    snippet = sentences[:max_sentences]
    return " ".join(snippet)


def extract_conclusion(text: str) -> str:
    sentences = split_sentences(text)
    for s in reversed(sentences):
        for kw in CONCLUSION_KEYWORDS:
            if kw in s:
                return s
    return sentences[-1] if sentences else ""


def normalize_text(text: str) -> str:
    working = text.replace("\r", " ")
    for marker in MARKER_CUTS:
        idx = working.find(marker)
        if idx >= 0:
            working = working[:idx]
    working = re.sub(r"\n{3,}", "\n\n", working)
    working = re.sub(r"[ \t]+", " ", working)
    return working.strip()


def chunk_records(records: Iterable[CaseRecord], size: int) -> Iterable[List[CaseRecord]]:
    batch: List[CaseRecord] = []
    for record in records:
        batch.append(record)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
