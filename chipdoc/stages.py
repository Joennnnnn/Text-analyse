"""分阶段流水线：每个阶段只读上一阶段的文件、只写自己的文件。

work/<slug>/
  doc.json              文档元信息（路径、sha、标题、类型）
  pages.json            逐页文本
  toc.json              书签目录
  chunks/c000.json      切块（含 <<<p.N>>> 页码标记）
  prompt/extract/*.json 给外部工具的请求（仅 external 模式）
  response/extract/*.json 外部工具写回的结果
  facts/raw/*.json      每个块的模型原始输出
  facts/verified.json   页码校验后的结论 + 被丢弃的候选
  prompt/summary/*.json / response/summary/*.json / summary/sections.json
  profile.json          归并后的文档画像
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .extract import extract_rules
from .index import DocIndex, Section, build_chunks, build_index
from .prompts import (EXTRACT_SYSTEM, PROMPT_VERSION, SUMMARY_SYSTEM, batch_extract_markdown,
                      batch_summary_markdown, extract_prompt, summary_prompt)
from .providers import Provider, ProviderError
from .reduce import Profile, _grounded, merge_facts
from .schema import fields_for, load_fields
from .util import ParseError, parse_json, run_parallel
from .verify import Fact, verify_facts


def slugify(pdf: str | Path) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(pdf).stem).strip("-").lower()
    return s or "doc"


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 文档工作目录
# --------------------------------------------------------------------------

class Workdoc:
    def __init__(self, path: str | Path):
        self.dir = Path(path).resolve()
        self.doc = _read(self.dir / "doc.json")
        if not self.doc:
            raise FileNotFoundError(f"{self.dir} 不是有效的工作目录（缺 doc.json），先运行 chipdoc index")
        self.pages: list[str] = _read(self.dir / "pages.json", [])
        self.toc = [Section(*t) for t in _read(self.dir / "toc.json", [])]
        self.index = DocIndex(
            path=self.doc["path"], sha=self.doc["sha"], title=self.doc["title"],
            pages=self.pages, toc=self.toc,
            chunks=build_chunks(self.pages, self.toc), kind=self.doc.get("kind", "generic"),
        )
        fields_path = self.doc.get("fields_path")
        self.fields = load_fields(fields_path) if fields_path else fields_for(self.index.kind)

    @property
    def slug(self) -> str:
        return self.dir.name

    def chunk_files(self) -> list[Path]:
        return sorted((self.dir / "chunks").glob("c*.json"))

    def load_facts(self) -> list[Fact]:
        out: list[Fact] = []
        for p in sorted((self.dir / "facts" / "raw").glob("*.json")):
            data = _read(p, {})
            for d in data.get("facts", []):
                try:
                    out.append(Fact(
                        field=d["field"], value=d["value"], page=int(d["page"]),
                        quote=d.get("quote", ""), unit=d.get("unit", ""),
                        source=d.get("source", "llm"),
                        confidence=d.get("confidence", "page_only"),
                        extra=d.get("extra", {}),
                    ))
                except (KeyError, TypeError, ValueError):
                    continue
        return out


def stage_index(pdf: str | Path, work_root: str | Path, force: bool = False,
                kind: str | None = None, fields_path: str | None = None,
                backend: str = "auto") -> Workdoc:
    pdf = Path(pdf).resolve()
    root = Path(work_root).resolve()
    slug = slugify(pdf)
    wd = root / slug
    if (wd / "doc.json").exists() and not force:
        wd_obj = Workdoc(wd)
        if wd_obj.doc["sha"] == build_index(pdf, kind=None, backend=backend).sha:
            return wd_obj
    idx = build_index(pdf, kind=kind, backend=backend)
    _write(root / slug / "doc.json", {
        "path": str(pdf), "sha": idx.sha, "title": idx.title, "kind": idx.kind,
        "pages": idx.page_count, "meta": idx.meta, "prompt_version": PROMPT_VERSION,
        "fields_path": str(Path(fields_path).resolve()) if fields_path else None,
    })
    _write(root / slug / "pages.json", idx.pages)
    _write(root / slug / "toc.json", [[s.level, s.title, s.page] for s in idx.toc])
    for c in idx.chunks:
        _write(root / slug / "chunks" / f"{c.cid}.json",
               {"cid": c.cid, "pages": c.pages, "sections": c.sections, "text": c.text})
    _write(root / slug / "facts" / "raw" / "_meta.json",
           {"created": time.time(), "prompt_version": PROMPT_VERSION})
    return Workdoc(root / slug)


def pending_chunks(wd: Workdoc) -> list[dict]:
    out = []
    for f in wd.chunk_files():
        if (wd.dir / "facts" / "raw" / f"{f.stem}.json").exists():
            continue
        data = _read(f, {})
        out.append({"cid": f.stem, "pages": data.get("pages", []), "text": data.get("text", "")})
    return out


def _blocks(wd: Workdoc) -> list[Section]:
    blocks = wd.index.section_blocks(1, 2)[:20]
    if not blocks:
        blocks = [Section(1, s.sections[0] if s.sections else f"块 {s.cid}", s.pages[0])
                  for s in wd.index.chunks][:20]
    return blocks


def pending_sections(wd: Workdoc) -> list[dict]:
    out = []
    for i, sec in enumerate(_blocks(wd)):
        sid = f"s{i:03d}"
        if (wd.dir / "summary" / "partial" / f"{sid}.json").exists():
            continue
        text = wd.index.block_text(sec, limit_chars=12000)
        if len(text) < 120:
            continue
        out.append({"sid": sid, "title": sec.title, "page": sec.page, "text": text})
    return out


def stage_outbox(wd: Workdoc, batch_size: int = 1) -> dict:
    """生成可复制粘贴的任务包（markdown），完全不需要 API。"""
    ex = pending_chunks(wd)
    sm = pending_sections(wd)
    n = max(1, batch_size)
    ex_batches = [ex[i:i + n] for i in range(0, len(ex), n)]
    sm_batches = [sm[i:i + n] for i in range(0, len(sm), n)]
    for i, group in enumerate(ex_batches, 1):
        _write_text(wd.dir / "outbox" / "extract" / f"b{i:03d}.md",
                    batch_extract_markdown(wd.fields, group, f"b{i:03d}"))
    for i, group in enumerate(sm_batches, 1):
        _write_text(wd.dir / "outbox" / "summary" / f"b{i:03d}.md",
                    batch_summary_markdown(group, f"b{i:03d}"))
    return {"extract_batches": len(ex_batches), "summary_batches": len(sm_batches),
            "chunks": len(ex), "sections": len(sm),
            "dir": str(wd.dir / "outbox")}


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# 抽取
# --------------------------------------------------------------------------

def _save_result(wd: Path, cid: str, facts: list[Fact]) -> None:
    _write(wd / "facts" / "raw" / f"{cid}.json",
           {"cid": cid, "facts": [f.as_dict() for f in facts], "created": time.time()})


def stage_extract(wd: Workdoc, provider: Provider, engine: str = "llm", jobs: int = 6,
                  force: bool = False, chunks: list[str] | None = None) -> dict:
    stats = {"chunks": 0, "done": 0, "reused": 0, "pending": 0, "failed": 0, "requests": []}
    stats["provider"] = provider.label

    if engine in ("rules", "both"):
        facts, dropped = extract_rules(wd.index)
        _write(wd.dir / "facts" / "raw" / "rules.json",
               {"cid": "rules", "facts": [f.as_dict() for f in facts], "created": time.time()})
        stats["rules_facts"] = len(facts)
        stats["rules_dropped"] = len(dropped)

    if engine == "rules":
        return stats

    files = wd.chunk_files()
    if chunks:
        want = set(chunks)
        files = [f for f in files if f.stem in want]
    stats["chunks"] = len(files)
    specs = wd.fields

    def one(path: Path) -> tuple[str, str]:
        cid = path.stem
        data = _read(path)
        out_file = wd.dir / "facts" / "raw" / f"{cid}.json"
        if out_file.exists() and not force:
            return cid, "reused"
        prompt = extract_prompt(specs, data["text"])
        if provider.external:
            _write(wd.dir / "prompt" / "extract" / f"{cid}.json",
                   {"cid": cid, "stage": "extract", "system": EXTRACT_SYSTEM,
                    "user": prompt, "expect": "json",
                    "output": str(out_file.relative_to(wd.dir))})
            return cid, "pending"
        raw = provider.complete(EXTRACT_SYSTEM, prompt)
        try:
            payload = parse_json(raw)
        except ParseError as e:
            _write(wd.dir / "facts" / "raw" / f"{cid}.failed.json",
                   {"cid": cid, "error": str(e), "raw": raw[:4000]})
            return cid, "failed"
        facts = []
        valid = {s.id for s in specs}
        for d in payload.get("facts", []):
            f = _coerce(d, valid)
            if f:
                facts.append(f)
        _save_result(wd.dir, cid, facts)
        return cid, "done"

    results = run_parallel(files, one, jobs=jobs)
    for cid, status in results:
        if status == "pending":
            stats["pending"] += 1
            stats["requests"].append(f"prompt/extract/{cid}.json")
        elif status == "reused":
            stats["reused"] += 1
        elif status == "failed":
            stats["failed"] += 1
        else:
            stats["done"] += 1
    return stats


def _coerce(raw: dict, valid: set[str]) -> Fact | None:
    if not isinstance(raw, dict):
        return None
    fid = str(raw.get("field", "")).strip()
    if fid not in valid:
        return None
    value = str(raw.get("value", "")).strip()
    if not value:
        return None
    try:
        page = int(raw.get("page"))
    except (TypeError, ValueError):
        return None
    return Fact(field=fid, value=value[:160], unit=str(raw.get("unit", ""))[:20],
                page=page, quote=str(raw.get("quote", ""))[:200], source="llm")


def _section_by_id(wd: Workdoc, sid: str):
    blocks = wd.index.section_blocks(1, 2)[:20]
    if not blocks:
        blocks = [Section(1, s.sections[0] if s.sections else f"块 {s.cid}", s.pages[0])
                  for s in wd.index.chunks][:20]
    for i, sec in enumerate(blocks):
        if f"s{i:03d}" == sid:
            return sec
    return None


def stage_ingest(wd: Workdoc) -> dict:
    """把 response/ 里的外部/agent 结果收编成 facts/raw 或 summary/partial。"""
    stats = {"ingested": 0, "missing": 0, "failed": 0}
    for stage in ("extract", "summary"):
        keys = {p.stem for p in (wd.dir / "prompt" / stage).glob("*.json")}
        keys |= {p.stem for p in (wd.dir / "response" / stage).glob("*.json")}
        for key in sorted(keys):
            resp = wd.dir / "response" / stage / f"{key}.json"
            if not resp.exists():
                continue
            try:
                payload = json.loads(resp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                try:
                    payload = parse_json(resp.read_text(encoding="utf-8"))
                except ParseError:
                    stats["failed"] += 1
                    continue
            if stage == "extract":
                valid = {s.id for s in wd.fields}
                grouped: dict[str, list[Fact]] = {}
                for d in payload.get("facts", []):
                    cid = str(d.get("cid", key)) if isinstance(d, dict) else key
                    f = _coerce(d, valid)
                    if f:
                        grouped.setdefault(cid, []).append(f)
                for cid, fs in grouped.items():
                    _save_result(wd.dir, cid, fs)
                if not grouped:
                    _save_result(wd.dir, key, [])
            elif "sections" in payload:
                for item in payload.get("sections", []):
                    sid = str(item.get("sid", ""))
                    if not sid:
                        continue
                    sec = _section_by_id(wd, sid)
                    _write(wd.dir / "summary" / "partial" / f"{sid}.json", {
                        "sid": sid,
                        "title": sec.title if sec else sid,
                        "page": sec.page if sec else 1,
                        "bullets": [str(b).strip() for b in item.get("bullets", []) if str(b).strip()],
                    })
            else:
                meta = _read(wd.dir / "prompt" / stage / f"{key}.json", {})
                if not meta:
                    sec = _section_by_id(wd, key)
                    meta = {"title": sec.title, "page": sec.page} if sec else {}
                payload.setdefault("sid", key)
                payload.setdefault("title", meta.get("title", key))
                payload.setdefault("page", meta.get("page", 1))
                _write(wd.dir / "summary" / "partial" / f"{key}.json", payload)
            stats["ingested"] += 1
    stats["missing"] = len(pending_chunks(wd)) + len(pending_sections(wd))
    return stats


# --------------------------------------------------------------------------
# 校验 / 浓缩 / 归并
# --------------------------------------------------------------------------

def stage_verify(wd: Workdoc) -> dict:
    facts = wd.load_facts()
    kept, dropped = verify_facts(facts, wd.index)
    try:
        from .plugins import apply_facts
        kept = apply_facts(kept, wd.index)
    except Exception:
        pass
    stats = {
        "input": len(facts), "kept": len(kept), "dropped": len(dropped),
        "verified": sum(1 for f in kept if f.confidence == "verified"),
        "page_only": sum(1 for f in kept if f.confidence == "page_only"),
    }
    _write(wd.dir / "facts" / "verified.json", {
        "stats": stats,
        "kept": [f.as_dict() for f in kept],
        "dropped": [f.as_dict() for f in dropped],
    })
    return stats


def stage_summarize(wd: Workdoc, provider: Provider, jobs: int = 4,
                    max_sections: int = 20, force: bool = False) -> dict:
    blocks = wd.index.section_blocks(1, 2)[:max_sections]
    if not blocks:
        blocks = [Section(1, s.sections[0] if s.sections else f"块 {s.cid}", s.pages[0])
                  for s in wd.index.chunks][:max_sections]
    stats = {"sections": len(blocks), "bullets": 0, "dropped": 0,
             "pending": 0, "failed": 0, "provider": provider.label}

    def one(item: tuple[int, Section]) -> tuple[str, list[str], str]:
        i, sec = item
        sid = f"s{i:03d}"
        out_file = wd.dir / "summary" / "partial" / f"{sid}.json"
        if out_file.exists() and not force:
            data = _read(out_file, {})
            return sid, data.get("bullets", []), "reused"
        text = wd.index.block_text(sec, limit_chars=12000)
        if len(text) < 120:
            _write(out_file, {"sid": sid, "title": sec.title, "page": sec.page,
                              "bullets": [], "skipped": True})
            return sid, [], "skipped"
        prompt = summary_prompt(sec.title, text)
        if provider.external:
            _write(wd.dir / "prompt" / "summary" / f"{sid}.json",
                   {"sid": sid, "stage": "summary", "title": sec.title, "page": sec.page,
                    "system": SUMMARY_SYSTEM, "user": prompt, "expect": "json"})
            return sid, [], "pending"
        try:
            payload = parse_json(provider.complete(SUMMARY_SYSTEM, prompt))
            raw_bullets = [str(b).strip() for b in payload.get("bullets", []) if str(b).strip()]
        except (ParseError, ProviderError):
            return sid, [], "failed"
        allowed = {int(p) for p in re.findall(r"<<<p\.(\d+)>>>", text)}
        good = [b for b in raw_bullets if _grounded(b, wd.index, allowed)[0]]
        _write(out_file, {"sid": sid, "title": sec.title, "page": sec.page,
                          "bullets": good, "dropped": len(raw_bullets) - len(good)})
        return sid, good, "done"

    results = run_parallel(list(enumerate(blocks)), one, jobs=jobs)
    out = []
    for (sid, bullets, status), (i, sec) in zip(results, enumerate(blocks)):
        if status == "pending":
            stats["pending"] += 1
        elif status == "failed":
            stats["failed"] += 1
        stats["bullets"] += len(bullets)
        out.append({"id": sid, "title": sec.title, "page": sec.page, "bullets": bullets})
    _write(wd.dir / "summary" / "sections.json", {"sections": out, "stats": stats})
    return stats


def stage_merge(wd: Workdoc, model_label: str = "") -> Profile:
    verified = _read(wd.dir / "facts" / "verified.json", {})
    facts: list[Fact] = []
    for d in verified.get("kept", []):
        facts.append(Fact(field=d["field"], value=d["value"], page=int(d["page"]),
                          quote=d.get("quote", ""), unit=d.get("unit", ""),
                          source=d.get("source", "llm"),
                          confidence=d.get("confidence", "page_only")))
    sections = _read(wd.dir / "summary" / "sections.json", {}).get("sections", [])
    if not sections:
        partial = [_read(p, {}) for p in sorted((wd.dir / "summary" / "partial").glob("s*.json"))]
        sections = [{"title": x.get("title", x.get("sid", "")), "page": x.get("page", 1),
                     "bullets": x.get("bullets", [])} for x in partial if x.get("bullets")]
    if not sections:
        sections = _extractive_sections(wd)
    profile = Profile(
        slug=wd.slug, title=wd.doc["title"], path=wd.doc["path"], sha=wd.doc["sha"],
        kind=wd.doc.get("kind", "datasheet"), pages=wd.doc["pages"],
        model=model_label or wd.doc.get("model", "") or "external",
        fields=merge_facts(facts, wd.doc.get("kind", "datasheet")),
        sections=[{"title": s["title"], "page": s["page"], "bullets": s["bullets"]}
                  for s in sections],
        stats={
            "fields": len(merge_facts(facts, wd.doc.get("kind", "datasheet"))),
            "dropped_facts": verified.get("stats", {}).get("dropped", 0),
            "sections": len(sections),
            "bullets": sum(len(s["bullets"]) for s in sections),
            "engine": model_label or "external",
        },
    )
    _write(wd.dir / "profile.json", profile.as_dict())
    return profile


def _extractive_sections(wd: Workdoc) -> list[dict]:
    from .reduce import _extractive
    blocks = wd.index.section_blocks(1, 2)[:14]
    if not blocks:
        blocks = [Section(1, s.sections[0] if s.sections else f"块 {s.cid}", s.pages[0])
                  for s in wd.index.chunks][:14]
    return _extractive(wd.index, blocks)
