"""PDF → 页码锚定的文档索引。

只做确定性的、不花钱的事：
- 逐页抽文本（并清掉 TI 类文档的页眉页脚噪声）
- 读书签目录得到章节 → 页码
- 按"每块 ≤ N 字符、页数 ≤ 3"切块，每块自带页码标记
- 可选：对表格页做结构化抽取（慢，默认缓存）

LLM 永远只看到带 <<<p.N>>> 的块，永远不会看到整份 PDF。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

# 页眉页脚噪声（TI / 常见芯片手册）
_NOISE = [
    re.compile(r"^www\.ti\.com(\.cn)?$", re.I),
    re.compile(r"^submit document feedback$", re.I),
    re.compile(r"^copyright\s*©", re.I),
    re.compile(r"^product folder links:", re.I),
    re.compile(r"^(zhcs|spru|sprui|slau|slas|ds)[a-z0-9]{3,}[a-z]?\b", re.I),
    re.compile(r"^\d{1,4}$"),
    re.compile(r"^tms320[a-z0-9]+(?:[,\s]+tms320[a-z0-9]+)*$", re.I),
]

_HEADING = re.compile(r"^(\d+(?:\.\d+){0,3})\s+(\S.{0,80})$")


@dataclass
class Section:
    level: int
    title: str
    page: int


@dataclass
class Chunk:
    cid: str
    pages: list[int]
    sections: list[str]
    text: str

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass
class DocIndex:
    path: str
    sha: str
    title: str
    pages: list[str]
    toc: list[Section]
    chunks: list[Chunk]
    kind: str = "datasheet"
    meta: dict = field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def page_text(self, page: int) -> str:
        return self.pages[page - 1] if 1 <= page <= len(self.pages) else ""

    def section_blocks(self, min_level: int = 1, max_level: int = 2) -> list[Section]:
        """按目录展开小节，供"内容浓缩"使用。"""
        if not self.toc:
            return []
        out: list[Section] = []
        for i, s in enumerate(self.toc):
            if not (min_level <= s.level <= max_level):
                continue
            start = s.page
            end = self.page_count
            for nxt in self.toc[i + 1:]:
                if nxt.page > start:
                    end = nxt.page - 1
                    break
            out.append(Section(s.level, s.title, start))
        return out

    def section_range(self, sec: Section) -> tuple[int, int]:
        try:
            idx = self.toc.index(sec)
        except ValueError:      # 无目录时的合成小节
            return sec.page, min(self.page_count, sec.page + 2)
        end = self.page_count
        for nxt in self.toc[idx + 1:]:
            if nxt.page > sec.page:
                end = nxt.page - 1
                break
        return sec.page, end

    def block_text(self, sec: Section, limit_chars: int = 12000) -> str:
        """小节正文（含页码标记），按目录的下一条边界截断。"""
        start, end = self.section_range(sec)
        parts, used = [], 0
        for p in range(start, end + 1):
            t = self.page_text(p)
            if used + len(t) > limit_chars:
                break
            parts.append(f"<<<p.{p}>>>\n{t}")
            used += len(t)
        return "\n".join(parts)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _clean_page(raw: str) -> str:
    lines = []
    for ln in raw.splitlines():
        s = ln.strip()
        if not s:
            continue
        if any(p.match(s) for p in _NOISE):
            continue
        lines.append(s)
    text = "\n".join(lines)
    return re.sub(r"[ \t]+", " ", text).strip()


def _detect_kind(title: str, toc: list[Section], pages: list[str], meta: dict | None = None) -> str:
    meta = meta or {}
    head = " ".join([title, str(meta.get("subject", "")), str(meta.get("keywords", ""))]).lower()
    if any(k in head for k in ("reference manual", "technical reference", "trm", "参考手册")):
        return "trm"
    if any(k in head for k in ("data sheet", "datasheet", "数据表")):
        return "datasheet"
    reg = sum(1 for s in toc if "寄存器" in s.title or "register" in s.title.lower())
    return "trm" if toc and reg / len(toc) > 0.5 else "datasheet"


def _read_toc(doc: pymupdf.Document) -> list[Section]:
    toc = []
    for level, title, page in doc.get_toc(simple=True):
        t = re.sub(r"\s+", " ", title).strip()
        if not t:
            continue
        if re.search(r"table of contents|目录", t, re.I) and level == 1:
            continue
        toc.append(Section(level=max(1, level), title=t, page=page))
    return toc


def _fallback_toc(pages: list[str], max_level: int = 2) -> list[Section]:
    out = []
    for i, text in enumerate(pages, start=1):
        for ln in text.splitlines():
            if len(ln) > 90:
                continue
            m = _HEADING.match(ln)
            if m and len(m.group(1).split(".")) <= max_level:
                out.append(Section(len(m.group(1).split(".")), ln.strip(), i))
                break
    return out


def build_chunks(pages: list[str], toc: list[Section],
                 max_chars: int = 4500, max_pages: int = 3) -> list[Chunk]:
    chunks: list[Chunk] = []
    cur_pages: list[int] = []
    cur: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal cur_pages, cur, size
        if not cur:
            return
        secs = [s.title for s in toc if cur_pages and cur_pages[0] <= s.page <= cur_pages[-1]]
        chunks.append(Chunk(
            cid=f"c{len(chunks):03d}",
            pages=list(cur_pages),
            sections=secs[:6],
            text="\n".join(cur),
        ))
        cur_pages, cur, size = [], [], 0

    for i, text in enumerate(pages, start=1):
        if not text:
            continue
        if cur and (size + len(text) > max_chars or len(cur_pages) >= max_pages):
            flush()
        cur_pages.append(i)
        cur.append(f"<<<p.{i}>>>\n{text}")
        size += len(text)
    flush()
    return chunks


def page_tables(path: str, page_no: int) -> list[str]:
    """单页表格 → markdown（慢，调用方负责缓存）。"""
    with pymupdf.open(path) as doc:
        page = doc[page_no - 1]
        found = page.find_tables()
        out = []
        for t in found.tables:
            rows = t.extract()
            if not rows or len(rows) < 2:
                continue
            width = max(len(r) for r in rows)
            norm = [[(c or "").replace("\n", " ").strip() for c in r] + [""] * (width - len(r))
                    for r in rows]
            lines = ["| " + " | ".join(norm[0]) + " |",
                     "|" + "---|" * width]
            for r in norm[1:]:
                lines.append("| " + " | ".join(r) + " |")
            out.append("\n".join(lines))
        return out


def build_index(pdf_path: str | Path, kind: str | None = None,
                with_headers: bool = True) -> DocIndex:
    path = Path(pdf_path).resolve()
    with pymupdf.open(path) as doc:
        pages = [_clean_page(doc[i].get_text("text")) for i in range(doc.page_count)]
        meta = dict(doc.metadata or {})
        title = (meta.get("title") or path.stem).strip()
        toc = _read_toc(doc)
    if len(toc) < 3:
        toc = _fallback_toc(pages)
    kind = kind or _detect_kind(title, toc, pages, meta)
    return DocIndex(
        path=str(path),
        sha=_sha256(path),
        title=title,
        pages=pages,
        toc=toc,
        chunks=build_chunks(pages, toc),
        kind=kind,
        meta={"pages": len(pages), "toc": len(toc), "producer": meta.get("producer", "")},
    )
