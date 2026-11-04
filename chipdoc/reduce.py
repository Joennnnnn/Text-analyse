"""归并：多块/多引擎的 Fact → 一份文档画像 + 分节浓缩。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .index import DocIndex, Section
from .schema import MULTI_VALUE_FIELDS, fields_for
from .verify import Fact, norm

CITE_RE = re.compile(r"\[p\.(\d+)\]")


@dataclass
class Profile:
    slug: str
    title: str
    path: str
    sha: str
    kind: str
    pages: int
    model: str
    fields: dict[str, dict] = field(default_factory=dict)
    sections: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def value(self, fid: str) -> str:
        return self.fields.get(fid, {}).get("value", "")

    def page(self, fid: str) -> int | None:
        return self.fields.get(fid, {}).get("page")

    def as_dict(self) -> dict:
        return {"slug": self.slug, "title": self.title, "path": self.path, "sha": self.sha,
                "kind": self.kind, "pages": self.pages, "model": self.model,
                "fields": self.fields, "sections": self.sections, "stats": self.stats}


def merge_facts(facts: list[Fact], kind: str) -> dict[str, dict]:
    ids = [f.id for f in fields_for(kind)]
    by: dict[str, list[Fact]] = {}
    for f in facts:
        by.setdefault(f.field, []).append(f)

    out: dict[str, dict] = {}
    for fid in ids:
        group = by.get(fid)
        if not group:
            continue
        if fid in MULTI_VALUE_FIELDS:
            seen_v: set[str] = set()
            items = []
            for f in sorted(group, key=lambda x: x.page):
                n = norm(f.value)
                if n and n not in seen_v:
                    seen_v.add(n)
                    items.append({"value": f.value, "page": f.page,
                                  "confidence": f.confidence})
            out[fid] = {"value": f"{len(items)} 条", "unit": "", "page": items[0]["page"],
                        "confidence": "page_only", "quote": "", "source": "llm",
                        "alternates": items[:20]}
            continue
        group.sort(key=lambda f: (f.confidence != "verified", f.source != "llm", f.page))
        best = group[0]
        others = []
        seen = {norm(best.value)}
        for f in group[1:]:
            n = norm(f.value)
            if n and n not in seen:
                seen.add(n)
                others.append({"value": f.value, "page": f.page, "confidence": f.confidence})
        out[fid] = {
            "value": best.value,
            "unit": best.unit,
            "page": best.page,
            "confidence": best.confidence,
            "quote": best.quote,
            "source": best.source,
            "alternates": others[:4],
        }
    return out


# --------------------------------------------------------------------------
# 分节浓缩
# --------------------------------------------------------------------------

def _bigrams(text: str) -> set[str]:
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", norm(text))
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _grounded(bullet: str, index: DocIndex, allowed: set[int]) -> tuple[bool, list[int]]:
    pages = [int(p) for p in CITE_RE.findall(bullet)]
    pages = [p for p in pages if p in allowed]
    if not pages:
        return False, []
    body = CITE_RE.sub("", bullet)
    grams = _bigrams(body)
    if not grams:
        return False, pages
    page_text = norm("\n".join(index.page_text(p) for p in pages))
    hit = sum(1 for g in grams if g in page_text) / len(grams)
    return hit >= 0.35, pages


def _extractive(index: DocIndex, blocks: list[Section]) -> list[dict]:
    """无 LLM 时的抽取式浓缩：每节取 1~2 行有成句信息的正文。"""
    out = []
    for sec in blocks:
        start, end = index.section_range(sec)
        bullets: list[str] = []
        for p in range(start, min(end, start + 3) + 1):
            for ln in index.page_text(p).splitlines():
                flat = ln.strip()
                if len(flat) < 24 or flat[0] in "•-–—" or flat.endswith(("：", ":")):
                    continue
                if re.match(r"^\d+(\.\d+)*\s+\S", flat):
                    continue
                if re.search(r"^(表|图|Table|Figure)\s*\d", flat):
                    continue
                bullets.append(f"{flat[:64]} [p.{p}]")
                if len(bullets) >= 2:
                    break
            if len(bullets) >= 2:
                break
        out.append({"title": sec.title, "page": sec.page, "bullets": bullets})
    return out
