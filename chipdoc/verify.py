"""引用校验：把模型给的 (页码, 引文) 落到原文上。

这是消除"页码瞎编"的关键一环，纯字符串比对，不花 token：
- verified  引文能在该页原文中逐字找到
- page_only 引文找不到，但取值能在该页找到（弱证据，保留但降级）
- rejected  该页根本没有这个内容（丢弃）
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    for a, b in (("–", "-"), ("—", "-"), ("−", "-"), ("，", ","), ("、", ","),
                 ("：", ":"), ("；", ";"), ("（", "("), ("）", ")"),
                 ("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'"), ("　", " ")):
        t = t.replace(a, b)
    t = re.sub(r"<<<p\.\d+>>>", " ", t)
    t = t.replace("\u00ad", "").replace("-\n", "")
    t = re.sub(r"\s+", "", t)
    return t.lower()


@dataclass
class Checked:
    ok: bool
    status: str          # verified | page_only | rejected
    reason: str = ""


@dataclass
class Fact:
    field: str
    value: str
    page: int
    quote: str = ""
    unit: str = ""
    source: str = "llm"          # llm | rules
    confidence: str = "page_only"
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"field": self.field, "value": self.value, "unit": self.unit,
                "page": self.page, "quote": self.quote, "source": self.source,
                "confidence": self.confidence, **({"extra": self.extra} if self.extra else {})}


def _quote_fragments(quote: str) -> list[str]:
    parts = re.split(r"\.{3,}|…{1,}", quote)
    return [p for p in (norm(x) for x in parts) if len(p) >= 4]


def check(fact: Fact, page_text: str, page_count: int) -> Checked:
    if not isinstance(fact.page, int) or not (1 <= fact.page <= page_count):
        return Checked(False, "rejected", "页码越界")
    page = norm(page_text)
    if not page.strip():
        return Checked(False, "rejected", "该页无文本层")
    frags = _quote_fragments(fact.quote)
    if frags and all(f in page for f in frags):
        return Checked(True, "verified", "引文逐字匹配")
    if frags:
        hit = sum(1 for f in frags if f in page)
        if hit / len(frags) >= 0.6:
            return Checked(True, "verified", f"引文 {hit}/{len(frags)} 片段匹配")
    val = norm(fact.value)
    if val and val in page:
        return Checked(True, "page_only", "取值为该页子串，引文未匹配")
    return Checked(False, "rejected", "该页找不到取值或引文")


def verify_facts(facts: list[Fact], index) -> tuple[list[Fact], list[Fact]]:
    """返回 (保留, 丢弃)。修改 confidence / 丢弃原因。"""
    kept, dropped = [], []
    for f in facts:
        res = check(f, index.page_text(f.page), index.page_count)
        if res.ok:
            f.confidence = res.status
            if res.status == "verified":
                f.extra.setdefault("verify", res.reason)
            kept.append(f)
        else:
            f.extra["drop_reason"] = res.reason
            dropped.append(f)
    return kept, dropped
