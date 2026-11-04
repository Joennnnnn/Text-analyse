"""渲染：单文档浓缩页、横向对比表、增量 diff。"""
from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path

from .reduce import Profile
from .schema import equivalent, fields_for


def _rel_link(pdf_path: str, out_dir: Path, page: int) -> str:
    try:
        rel = Path(pdf_path).resolve().relative_to(out_dir.resolve().parent)
    except ValueError:
        rel = Path(pdf_path)
    return f"{rel.as_posix()}#page={page}"


def field_table(profile: Profile, out_dir: Path) -> str:
    rows = ["| 字段 | 取值 | 出处 | 置信 |", "|---|---|---|---|"]
    for spec in fields_for(profile.kind):
        f = profile.fields.get(spec.id)
        if not f:
            continue
        link = f"[p.{f['page']}]({_rel_link(profile.path, out_dir, f['page'])})"
        conf = "✔ 逐字" if f["confidence"] == "verified" else "△ 仅页级"
        value = f["value"].replace("|", "\\|")
        if f.get("alternates"):
            value += f"<br><sub>其他：{'; '.join(a['value'] for a in f['alternates'][:2])}</sub>"
        rows.append(f"| {spec.label} | {value} | {link} | {conf} |")
    return "\n".join(rows)


def summary_markdown(profile: Profile, out_dir: Path) -> str:
    lines = [f"# {profile.title}", ""]
    loc = Path(profile.path)
    lines.append(f"> 文件：`{loc.name}` · 页数 {profile.pages} · 类型 {profile.kind} · "
                 f"引擎 {profile.model} · 生成 {dt.date.today().isoformat()}")
    lines.append("")

    stats = profile.stats
    lines.append(f"> 抽取字段 {stats.get('fields', 0)} 个，"
                 f"弃用未通过校验的候选 {stats.get('dropped_facts', 0)} 条；"
                 f"分节 {stats.get('sections', 0)} 个，要点 {stats.get('bullets', 0)} 条"
                 f"（因引用不落地丢弃 {stats.get('dropped_bullets', 0)} 条）。")
    lines.append("")
    lines.append("## 一、关键参数速览")
    lines.append("")
    lines.append(field_table(profile, out_dir))
    lines.append("")
    lines.append("## 二、分节浓缩（每条带页码）")
    lines.append("")
    for sec in profile.sections:
        if not sec.get("bullets"):
            continue
        lines.append(f"### {sec['title']}")
        lines.append("")
        for b in sec["bullets"]:
            lines.append(f"- {b}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_doc(profile: Profile, out_dir: Path) -> Path:
    d = out_dir / "docs" / profile.slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.md").write_text(summary_markdown(profile, out_dir), encoding="utf-8")
    (d / "profile.json").write_text(
        json.dumps(profile.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def build_matrix(profiles: list[Profile]) -> dict:
    """rows=字段，cols=文档。"""
    kind = profiles[0].kind if profiles else "datasheet"
    matrix = {"generated": dt.datetime.now().isoformat(timespec="seconds"),
              "kind": kind, "columns": [], "rows": []}
    for p in profiles:
        matrix["columns"].append({"slug": p.slug, "title": p.title, "path": p.path,
                                  "pages": p.pages, "sha": p.sha})
    for spec in fields_for(kind):
        row = {"field": spec.id, "label": spec.label, "cells": {}}
        for p in profiles:
            f = p.fields.get(spec.id)
            row["cells"][p.slug] = ({"value": f["value"], "page": f["page"],
                                     "confidence": f["confidence"]} if f else None)
        matrix["rows"].append(row)
    return matrix


def matrix_markdown(matrix: dict, out_dir: Path) -> str:
    cols = matrix["columns"]
    head = "| 字段 | " + " | ".join(c["title"].split("(")[0].strip()[:32] for c in cols) + " |"
    sep = "|---|" + "---|" * len(cols)
    lines = ["# 横向对比表", "", f"生成时间：{matrix['generated']}", "", head, sep]
    for row in matrix["rows"]:
        cells = []
        for c in cols:
            cell = row["cells"].get(c["slug"])
            if not cell:
                cells.append("—")
            else:
                cells.append(f"{cell['value']} `p.{cell['page']}`")
        lines.append(f"| {row['label']} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("> `p.N` 为该值在对应 PDF 中的页码，可直接跳转核对。")
    return "\n".join(lines) + "\n"


def write_matrix(matrix: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "matrix.json").write_text(
        json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "matrix.md").write_text(matrix_markdown(matrix, out_dir), encoding="utf-8")
    with open(out_dir / "matrix.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["field"] + [c["slug"] for c in matrix["columns"]])
        for row in matrix["rows"]:
            w.writerow([row["label"]] + [
                (f"{c['value']} (p.{c['page']})" if c else "") for c in
                (row["cells"].get(col["slug"]) for col in matrix["columns"])])


# --------------------------------------------------------------------------
# 增量 diff
# --------------------------------------------------------------------------

def make_snapshot(profiles: list[Profile]) -> dict:
    return {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "docs": {
            p.slug: {"title": p.title, "path": p.path, "sha": p.sha, "pages": p.pages,
                     "fields": {fid: {"value": f["value"], "page": f["page"]}
                                for fid, f in p.fields.items()}}
            for p in profiles
        },
    }


def diff_snapshots(old: dict | None, new: dict) -> dict:
    old_docs = (old or {}).get("docs", {})
    new_docs = new.get("docs", {})
    out = {"new_docs": [], "removed_docs": [], "changed": [], "added_fields": []}
    for slug, doc in new_docs.items():
        if slug not in old_docs:
            out["new_docs"].append({"slug": slug, "title": doc["title"]})
            continue
        old_fields = old_docs[slug].get("fields", {})
        for fid, cell in doc["fields"].items():
            prior = old_fields.get(fid)
            if not prior:
                out["added_fields"].append({"slug": slug, "field": fid, "value": cell["value"],
                                            "page": cell["page"]})
            elif not equivalent(prior["value"], cell["value"]):
                out["changed"].append({"slug": slug, "field": fid,
                                       "old": prior["value"], "old_page": prior["page"],
                                       "new": cell["value"], "new_page": cell["page"]})
    for slug in old_docs:
        if slug not in new_docs:
            out["removed_docs"].append({"slug": slug, "title": old_docs[slug]["title"]})
    return out


def diff_markdown(diff: dict, matrix: dict, out_dir: Path) -> str:
    labels = {r["field"]: r["label"] for r in matrix["rows"]}
    titles = {c["slug"]: c["title"] for c in matrix["columns"]}
    lines = ["# 增量对比（相对上一次快照）", ""]
    if not any(diff[k] for k in ("new_docs", "removed_docs", "changed", "added_fields")):
        lines.append("与上次快照一致，没有字段级变化。")
        return "\n".join(lines) + "\n"

    if diff["new_docs"]:
        lines += ["## 新增文档", ""]
        for d in diff["new_docs"]:
            lines.append(f"- `{d['slug']}` — {d['title']}")
        lines.append("")
    if diff["removed_docs"]:
        lines += ["## 移除文档", ""]
        for d in diff["removed_docs"]:
            lines.append(f"- `{d['slug']}` — {d['title']}")
        lines.append("")
    if diff["changed"]:
        lines += ["## 取值变化", "", "| 文档 | 字段 | 旧值 | 新值 |", "|---|---|---|---|"]
        for c in diff["changed"]:
            lines.append(f"| {titles.get(c['slug'], c['slug'])} | {labels.get(c['field'], c['field'])} "
                         f"| {c['old']} `p.{c['old_page']}` | {c['new']} `p.{c['new_page']}` |")
        lines.append("")
    if diff["added_fields"]:
        lines += ["## 新增字段（旧快照里没有）", "", "| 文档 | 字段 | 值 |", "|---|---|---|"]
        for c in diff["added_fields"]:
            lines.append(f"| {titles.get(c['slug'], c['slug'])} | {labels.get(c['field'], c['field'])} "
                         f"| {c['value']} `p.{c['page']}` |")
        lines.append("")
    return "\n".join(lines)


def write_diff(diff: dict, matrix: dict, out_dir: Path) -> None:
    (out_dir / "diff.json").write_text(json.dumps(diff, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    (out_dir / "diff.md").write_text(diff_markdown(diff, matrix, out_dir), encoding="utf-8")
