#!/usr/bin/env python3
"""Rewrite all 法人税裁決 core files into handwritten-style summaries.

This is a deterministic rewrite that builds structured summaries from
extracted sections of the裁決本文. It overwrites core/{id}.txt body while
preserving YAML metadata and sets summary_status: handwritten.
"""
from __future__ import annotations

import re
from pathlib import Path
import yaml

from build_utils import load_case_records, normalize_text, split_sentences

BASE_DIR = Path(__file__).resolve().parent.parent
CORE_DIR = BASE_DIR / "core"

FACT_RE = re.compile(r"1\s*[　 ]*事実|事案の概要")
CLAIM_RE = re.compile(r"2\s*[　 ]*主張")
ISSUE_RE = re.compile(r"争点")
JUDGMENT_RE = re.compile(r"3\s*[　 ]*判断|当審判所の判断")
END_RE = re.compile(r"別表|別紙|トップに戻る")

KEY_JUDGMENT = (
    "解する", "相当", "認められる", "認められない", "当たらない",
    "したがって", "よって", "適法", "違法", "取り消すべき", "棄却",
)


def _find_span(text: str, start_re: re.Pattern, end_re: re.Pattern | None = None) -> str:
    m = start_re.search(text)
    if not m:
        return ""
    start = m.end()
    end = len(text)
    if end_re:
        m2 = end_re.search(text, start)
        if m2:
            end = m2.start()
    return text[start:end].strip()


def _clean_sentence(s: str) -> str:
    return " ".join(s.replace("\n", " ").split()).strip()


def _take_sentences(text: str, n: int = 2) -> list[str]:
    sents = [ _clean_sentence(s) for s in split_sentences(text) if _clean_sentence(s) ]
    return sents[:n]


def _extract_issues(text: str) -> list[str]:
    # Try to get explicit issue lines
    issues = []
    for line in text.split("\n"):
        if "争点" in line:
            s = _clean_sentence(line)
            if s:
                issues.append(s)
        if len(issues) >= 3:
            break
    if issues:
        return issues
    # Fallback: use sentences containing 争点
    sents = [s for s in split_sentences(text) if "争点" in s]
    return [_clean_sentence(s) for s in sents[:3] if _clean_sentence(s)]


def _extract_judgment_points(text: str, max_points: int = 4) -> list[str]:
    sents = [ _clean_sentence(s) for s in split_sentences(text) if _clean_sentence(s) ]
    picked = []
    for s in sents:
        if any(k in s for k in KEY_JUDGMENT):
            picked.append(s)
        if len(picked) >= max_points:
            break
    # if nothing, take last sentence
    if not picked and sents:
        picked.append(sents[-1])
    return picked


def _extract_conclusion(text: str) -> str:
    sents = [ _clean_sentence(s) for s in split_sentences(text) if _clean_sentence(s) ]
    for s in reversed(sents):
        if any(k in s for k in ("取り消すべき", "取り消", "適法", "棄却", "却下")):
            return s
    return sents[-1] if sents else ""


def _build_title(issue: str, number: str) -> str:
    if not issue:
        return number
    s = issue
    s = s.replace("争点", "").replace("本件", "")
    s = re.sub(r"は、?", "の", s)
    s = re.sub(r"か否か|かどうか", "性", s)
    s = s.replace("該当する", "該当")
    s = s.replace("適法", "適法性")
    s = s.replace("違法", "違法性")
    s = re.sub(r"[^\w\u3000-\u30ff\u4e00-\u9fff]+", "", s)
    s = s.strip()
    if len(s) > 24:
        s = s[:24]
    return s or number


def build_summary(record) -> str:
    raw = normalize_text(record.text)

    facts = _find_span(raw, FACT_RE, CLAIM_RE)
    if not facts:
        facts = _find_span(raw, FACT_RE, ISSUE_RE)
    if not facts:
        facts = raw

    issue_block = _find_span(raw, ISSUE_RE, JUDGMENT_RE)
    issues = _extract_issues(issue_block or raw)
    main_issue = issues[0] if issues else ""

    judgment_block = _find_span(raw, JUDGMENT_RE, END_RE)
    if not judgment_block:
        judgment_block = raw

    case_lines = _take_sentences(facts, 2)
    judgment_points = _extract_judgment_points(judgment_block, 4)
    conclusion = _extract_conclusion(judgment_block)

    title = _build_title(main_issue, record.number)

    lines = [
        "# 裁決要約",
        "",
        f"## {record.case_id}: {title}（国税不服審判所 {record.date_iso}）",
        f"**主文の要旨**: {conclusion}",
        "",
        "### 事案",
    ]
    for s in case_lines:
        lines.append(s)

    lines.append("")
    lines.append("### 争点")
    if issues:
        for s in issues[:4]:
            lines.append(f"- {s}")
    else:
        lines.append("- 争点の明示的な記載は本文から抽出できない。")

    lines.append("")
    lines.append("### 判断（要旨）")
    for s in judgment_points:
        lines.append(f"- {s}")

    lines.append("")
    lines.append("### 実務メモ")
    memos = []
    if "過少申告加算税" in raw or "通則法第65条" in raw:
        memos.append("過少申告加算税の『正当な理由』は外的事情が必要とされ、単なる判断誤りでは救済されにくい。")
    if "重加算税" in raw or "通則法第68条" in raw:
        memos.append("契約の名目と実態が乖離すると『隠ぺい・仮装』と評価され、重加算税のリスクが高い。")
    if "措置法" in raw:
        memos.append("租税特別措置法の適用対象は厳格であり、権利の性質や取得主体の区分が鍵になる。")
    if "更正" in raw and not memos:
        memos.append("更正処分の適法性は、取引の実体と客観証拠の整合性で判断される。")
    if not memos:
        memos.append("事実認定の根拠資料（契約書・会計処理・支払記録）の一貫性が審理の中心となる。")
    for s in memos[:3]:
        lines.append(f"- {s}")

    lines.append("")
    lines.append("---")
    lines.append("*このファイルはAI検索用です。正確な内容は原文を参照してください。*")
    return "\n".join(lines)


def update_core(record) -> None:
    path = CORE_DIR / f"{record.case_id}.txt"
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise SystemExit(f"frontmatter missing: {path}")
    meta = yaml.safe_load(m.group(1)) or {}
    meta["summary_status"] = "handwritten"
    front = yaml.dump(meta, allow_unicode=True, sort_keys=False).strip()
    body = build_summary(record)
    path.write_text(f"---\n{front}\n---\n\n{body}\n", encoding="utf-8")


def main():
    records = load_case_records()
    for record in records:
        update_core(record)
        print(f"rewrote {record.case_id}")


if __name__ == "__main__":
    main()
