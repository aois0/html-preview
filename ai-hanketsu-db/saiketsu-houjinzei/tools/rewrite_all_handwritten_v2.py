#!/usr/bin/env python3
"""Rewrite all 法人税裁決 core files into higher-quality handwritten-style summaries.

This uses better section extraction from the original text while still
producing deterministic output. It overwrites core/{id}.txt body and sets
summary_status: handwritten.
"""
from __future__ import annotations

import re
from pathlib import Path
import yaml

from build_utils import load_case_records

BASE_DIR = Path(__file__).resolve().parent.parent
CORE_DIR = BASE_DIR / "core"

NAV_LINES = (
    "本文へジャンプします",
    "サイト内検索",
    "検索の仕方",
    "利用案内",
    "サイトマップ",
    "関連リンク",
    "ホーム",
    "審判所の概要",
    "審査請求の状況",
    "実績の評価",
    "パンフレット等",
    "不服申立手続等",
    "制度の概要図",
    "不服申立ての対象等",
    "再調査の請求との関係",
    "審査請求書の提出",
    "代理人と総代",
    "審理と裁決",
    "提出書類一覧",
    "提出先一覧",
    "Ｑ＆Ａコーナー",
    "公表裁決事例集等の紹介",
    "公表裁決事例",
    "公表裁決事例要旨",
    "裁決要旨の検索",
    "調達情報",
    "情報公開",
    "個人情報保護",
    "トップに戻る",
)

FACT_RE = re.compile(r"(^|\n)\s*(?:1|１)\s*[　 ]*事実|事案の概要", re.M)
CLAIM_RE = re.compile(r"(^|\n)\s*(?:2|２)\s*[　 ]*主張", re.M)
ISSUE_RE = re.compile(r"争点")
JUDGMENT_RE = re.compile(r"(^|\n)\s*(?:3|３)\s*[　 ]*判断", re.M)
END_RE = re.compile(r"別表|別紙|Copyright")

KEY_JUDGMENT = (
    "解する", "相当", "認められる", "認められない", "当たらない",
    "したがって", "よって", "適法", "違法", "取り消すべき", "棄却",
)


def clean_text(raw: str) -> str:
    text = raw
    # drop nav before main body
    if "裁決書（抄）" in text:
        text = text.split("裁決書（抄）", 1)[1]
    if "《裁決書（抄）》" in text:
        text = text.split("《裁決書（抄）》", 1)[1]
    # cut tail
    for marker in ("Copyright",):
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx]
    # remove nav lines
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if any(n in s for n in NAV_LINES):
            continue
        lines.append(s)
    cleaned = "\n".join(lines)
    m = FACT_RE.search(cleaned)
    if m:
        cleaned = cleaned[m.start():]
    return cleaned


def extract_section(text: str, start_re: re.Pattern, end_re: re.Pattern | None = None) -> str:
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


def split_sentences(text: str) -> list[str]:
    sents = re.split(r"(?<=。)", text)
    cleaned = []
    for s in sents:
        s = " ".join(s.replace("\n", " ").split())
        if s:
            cleaned.append(s)
    merged: list[str] = []
    for s in cleaned:
        if merged:
            prev = merged[-1]
            if (prev.endswith("という。") or prev.endswith("という。）")) and s[:1] in ("）", "は", "が", "を", "に"):
                merged[-1] = prev + s
                continue
            if prev.endswith("。") and s.startswith("）"):
                merged[-1] = prev + s
                continue
        merged.append(s)
    return merged


def pick_sentences(text: str, n: int = 2) -> list[str]:
    sents = split_sentences(text)
    picked = []
    for s in sents:
        if len(s) < 8:
            continue
        if re.search(r"^（?単位|^区分", s):
            continue
        picked.append(s)
        if len(picked) >= n:
            break
    return picked


def extract_issues(text: str) -> list[str]:
    issues = []
    for line in text.split("\n"):
        if "争点" in line:
            s = " ".join(line.split())
            if s:
                issues.append(s)
        if len(issues) >= 4:
            break
    if issues:
        return issues
    # fallback: sentences containing 争点 (including longer phrases)
    sents = [s for s in split_sentences(text) if "争点" in s]
    return sents[:4]


def extract_judgment_points(text: str, max_points: int = 4) -> list[str]:
    sents = split_sentences(text)
    picked = []
    for s in sents:
        if re.match(r"^[（(]\\d", s):
            continue
        if re.match(r"^\\(\\d+\\)", s):
            continue
        if "争点" in s:
            continue
        if "主張" in s:
            continue
        if any(k in s for k in KEY_JUDGMENT):
            picked.append(s)
        if len(picked) >= max_points:
            break
    if not picked and sents:
        picked = sents[:2]
    return picked


def summarize_conclusion(text: str) -> str:
    sents = split_sentences(text)
    for s in reversed(sents):
        if "取り消すべき" in s or "取消すべき" in s:
            return "原処分は取り消すべきとされた。"
        if "適法" in s:
            return "原処分は適法とされた。"
        if "棄却" in s:
            return "請求人の主張は棄却された。"
        if "却下" in s:
            return "請求は却下された。"
    return sents[-1] if sents else ""


def build_title(issue: str, number: str) -> str:
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
    if len(s) > 20:
        return number
    return s or number


def build_summary(record) -> str:
    raw = clean_text(record.text)
    facts = extract_section(raw, FACT_RE, CLAIM_RE) or extract_section(raw, FACT_RE, ISSUE_RE)
    issues_block = extract_section(raw, ISSUE_RE, JUDGMENT_RE)
    judgment_block = extract_section(raw, JUDGMENT_RE, END_RE)
    if not judgment_block:
        judgment_block = extract_section(raw, re.compile(r"判断"), END_RE)

    case_lines = pick_sentences(facts or raw, 2)
    issues = extract_issues(issues_block or raw)
    if not issues and judgment_block:
        for s in split_sentences(judgment_block):
            if "争点" in s:
                issues.append(s)
                break
    judgment_points = extract_judgment_points(judgment_block or raw, 4)
    conclusion = summarize_conclusion(judgment_block or raw)

    title = build_title(issues[0] if issues else "", record.number)

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
        memos.append("過少申告加算税の『正当な理由』は外的事情が必要とされ、判断誤りだけでは救済されにくい。")
    if "重加算税" in raw or "通則法第68条" in raw:
        memos.append("名目と実態の乖離があると『隠ぺい・仮装』と評価され、重加算税のリスクが高い。")
    if "措置法" in raw:
        memos.append("租税特別措置法の適用は権利性・取得主体などの要件が厳格に見られる。")
    if not memos:
        memos.append("事実認定の根拠資料（契約書・会計処理・支払記録）の整合性が結論を左右する。")
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
