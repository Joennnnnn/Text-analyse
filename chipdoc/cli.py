"""命令行入口：每个阶段一条命令，所有中间产物都是文件。

    chipdoc index      PDF → work/<slug>/{doc,pages,toc,chunks}
    chipdoc extract    chunks → facts/raw/*.json      （可换模型 / 可外部交接）
    chipdoc ingest     response/ → facts/raw/*.json   （外部结果收编）
    chipdoc verify     facts/raw → facts/verified.json（纯字符串校验，不用模型）
    chipdoc summarize  章节 → summary/sections.json   （可换另一个模型）
    chipdoc merge      verified + summary → profile.json
    chipdoc render     profile → out/{matrix,diff}.{md,csv,json}
    chipdoc run        一条龙
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from .index import build_index
from .providers import ExternalProvider, ProviderConfig, ProviderError, load_config, make_provider, resolve_config
from .render import (build_matrix, diff_snapshots, make_snapshot, write_diff, write_doc,
                     write_matrix)
from .prompts import EXTRACT_SYSTEM, SUMMARY_SYSTEM, extract_prompt, summary_prompt
from .stages import (Workdoc, _read, pending_chunks, pending_sections, stage_extract,
                     stage_index, stage_ingest, stage_merge, stage_outbox, stage_summarize,
                     stage_verify)


def _provider_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", default=None, help="多模型配置 JSON/TOML：default + stages.<阶段>")
    p.add_argument("--provider", choices=["openai", "anthropic", "command", "external"],
                   default=None,
                   help="openai=任意兼容端点；anthropic=Claude；command=任意命令行；external=只出任务包")
    p.add_argument("--model", default=None, help="模型名，如 deepseek-chat、qwen-max")
    p.add_argument("--base-url", default=None, help="OpenAI 兼容端点，如 http://localhost:11434/v1")
    p.add_argument("--api-key-env", default=None, help="从该环境变量读取 API key")
    p.add_argument("--json-mode", action="store_true", help="请求 JSON 模式（部分端点不支持）")


def _cfg(args, stage: str):
    cli = {"provider": args.provider, "model": args.model, "base_url": args.base_url,
           "api_key_env": args.api_key_env}
    if getattr(args, "json_mode", False):
        cli["json_mode"] = True
    return resolve_config(stage, load_config(getattr(args, "config", None)), cli)


def _wd(paths: list[str]) -> list[Workdoc]:
    return [Workdoc(p) for p in paths]


# --------------------------------------------------------------------------
# 阶段命令
# --------------------------------------------------------------------------

def cmd_index(args) -> int:
    for pdf in args.pdfs:
        wd = stage_index(pdf, args.work, force=args.refresh,
                         kind=args.kind, fields_path=args.fields, backend=args.backend)
        print(f"+ {wd.slug}: {wd.index.page_count} 页 / {len(wd.index.chunks)} 块 / "
              f"目录 {len(wd.index.toc)} 条 → {wd.dir}")


def cmd_extract(args) -> int:
    cfg = _cfg(args, "extract")
    provider = make_provider(cfg)
    if provider.external:
        print("· external 模式：只生成 prompt，不调用模型")
    for wd in _wd(args.workdirs):
        stats = stage_extract(wd, provider, engine=args.engine, jobs=args.jobs,
                              force=args.force, chunks=args.chunks.split(",") if args.chunks else None)
        print(f"+ {wd.slug}: 完成 {stats['done']}，复用 {stats['reused']}，"
              f"待外部处理 {stats['pending']}，失败 {stats['failed']}")
        if stats["pending"]:
            print(f"  → 把回答写到 {wd.dir}/response/extract/<cid>.json，再运行 "
                  f"`chipdoc ingest {wd.dir}`")


def cmd_ingest(args) -> int:
    for wd in _wd(args.workdirs):
        stats = stage_ingest(wd)
        print(f"+ {wd.slug}: 收编 {stats['ingested']}，未完成 {stats['missing']} 项，解析失败 {stats['failed']}")
        if stats["missing"]:
            print(f"  → 还有 {stats['missing']} 个块/小节没有答案，可运行 chipdoc outbox 重新生成任务包")


def cmd_verify(args) -> int:
    for wd in _wd(args.workdirs):
        s = stage_verify(wd)
        print(f"+ {wd.slug}: 输入 {s['input']} 条，保留 {s['kept']}（逐字 {s['verified']}，"
              f"页级 {s['page_only']}），丢弃 {s['dropped']}")


def cmd_summarize(args) -> int:
    provider = make_provider(_cfg(args, "summarize"))
    for wd in _wd(args.workdirs):
        s = stage_summarize(wd, provider, jobs=args.jobs, force=args.force)
        print(f"+ {wd.slug}: 小节 {s['sections']}，要点 {s['bullets']}，"
              f"待外部 {s['pending']}，失败 {s['failed']}（{s['provider']}）")


def cmd_merge(args) -> int:
    for wd in _wd(args.workdirs):
        profile = stage_merge(wd)
        print(f"+ {wd.slug}: 字段 {len(profile.fields)}，小节 {len(profile.sections)} → {wd.dir}/profile.json")


def cmd_render(args) -> int:
    from .reduce import Profile
    profiles = [Profile(**json.loads(p.read_text(encoding="utf-8")))
                for p in sorted(Path(args.work).glob("*/profile.json"))]
    if not profiles:
        print("work/ 下还没有 profile.json，先跑 extract/verify/merge", file=sys.stderr)
        return 2
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    for profile in profiles:
        write_doc(profile, out)
    matrix = build_matrix(profiles)
    write_matrix(matrix, out)
    diff = diff_snapshots(state.get("snapshot"), make_snapshot(profiles))
    write_diff(diff, matrix, out)
    state["snapshot"] = make_snapshot(profiles)
    state["version"] = 1
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    changed = len(diff["changed"]) + len(diff["new_docs"]) + len(diff["added_fields"])
    print(f"完成：{len(profiles)} 份文档 → {out}/matrix.md、diff.md（本次变化 {changed} 项）")


def cmd_run(args) -> int:
    started = time.time()
    provider = make_provider(_cfg(args, "extract"))
    summary_provider = make_provider(_cfg(args, "summarize"))
    engine = args.engine
    if engine in ("llm", "both") and not provider.external and not provider.cfg.api_key \
            and provider.cfg.kind in ("openai", "anthropic"):
        print("! 没有可用的 API key，回退到 --engine rules（或配置 --api-key-env / --provider command）",
              file=sys.stderr)
        engine = "rules"

    for pdf in args.pdfs:
        t0 = time.time()
        wd = stage_index(pdf, args.work, force=args.refresh,
                         kind=getattr(args, "kind", None), fields_path=getattr(args, "fields", None),
                         backend=getattr(args, "backend", "auto"))
        print(f"+ {wd.slug}: {wd.index.page_count} 页 / {len(wd.index.chunks)} 块")
        ex = stage_extract(wd, provider, engine=engine, jobs=args.jobs, force=args.refresh)
        if engine == "rules":
            print(f"  extract(rules): {ex.get('rules_facts', 0)} 条候选（规则引擎，非模型）")
        else:
            print(f"  extract: 完成 {ex['done']}，复用 {ex['reused']}，待外部 {ex['pending']}，失败 {ex['failed']}")
        if ex["pending"]:
            print(f"  → 外部模式：prompt 已写到 {wd.dir}/prompt/extract/，"
                  f"写回 response/extract/<cid>.json 后运行 "
                  f"`chipdoc ingest {wd.dir} && chipdoc verify {wd.dir} && chipdoc merge {wd.dir}`")
            continue
        if engine != "rules":
            sm = stage_summarize(wd, summary_provider, jobs=args.jobs)
            print(f"  summarize: 要点 {sm['bullets']}，待外部 {sm['pending']}，失败 {sm['failed']}")
            if sm["pending"]:
                print(f"  → 外部模式：prompt 已写到 {wd.dir}/prompt/summary/")
                continue
        v = stage_verify(wd)
        stage_merge(wd, model_label=("rules" if engine == "rules" else provider.label))
        print(f"  verify: 保留 {v['kept']}（逐字 {v['verified']}），丢弃 {v['dropped']}；"
              f"{time.time() - t0:.1f}s")

    if not list(Path(args.work).glob("*/profile.json")):
        print("\n没有生成 profile（可能是外部模式仍在等回答），完成后运行 "
              "`chipdoc ingest … && chipdoc verify … && chipdoc merge … && chipdoc render`",
              file=sys.stderr)
        return 0
    return cmd_render(argparse.Namespace(work=args.work, out=args.out))


def _chunk_pending(wd: Workdoc) -> list[tuple[str, list[int]]]:
    out = []
    for f in wd.chunk_files():
        if not (wd.dir / "facts" / "raw" / f"{f.stem}.json").exists():
            data = _read(f, {})
            out.append((f.stem, data.get("pages", [])))
    return out


def _summary_pending(wd: Workdoc) -> list[tuple[str, str, int]]:
    from .index import Section
    blocks = wd.index.section_blocks(1, 2)[:20]
    if not blocks:
        blocks = [Section(1, s.sections[0] if s.sections else f"块 {s.cid}", s.pages[0])
                  for s in wd.index.chunks][:20]
    out = []
    for i, sec in enumerate(blocks):
        sid = f"s{i:03d}"
        if not (wd.dir / "summary" / "partial" / f"{sid}.json").exists():
            out.append((sid, sec.title, sec.page))
    return out


def _write_tasks(wd, n_extract: int, n_summary: int, out: str = "out",
                 batches: dict | None = None) -> Path:
    batches = batches or {}
    lines = [f"# 待处理任务：{wd.doc['title']}", "",
             f"- 工作目录：`{wd.dir}`",
             f"- {wd.index.page_count} 页 · {len(wd.chunk_files())} 块 · 类型 `{wd.index.kind}`",
             f"- 任务包目录：`{batches.get('dir', wd.dir / 'outbox')}`", ""]
    if n_extract:
        lines += ["## 1. 抽取（只依据块内原文，必须给 page + 逐字 quote）", "",
                  f"共 {batches.get('extract_batches', 0)} 个任务包，每个包 {n_extract} 块待处理：", "",
                  "1. 打开 `outbox/extract/b001.md`，整份复制给任意模型/聊天窗口",
                  "2. 把模型返回的 JSON 原样保存为 `response/extract/b001.json`",
                  "3. 本地收编：`chipdoc ingest <workdir>`", ""]
    if n_summary:
        lines += ["## 2. 分节浓缩（要点 ≤40 字，末尾 [p.N]）", "",
                  f"共 {batches.get('summary_batches', 0)} 个任务包：", "",
                  "1. 打开 `outbox/summary/b001.md`，整份复制给模型",
                  "2. 返回的 JSON 存为 `response/summary/b001.json`",
                  "3. `chipdoc ingest <workdir>`", ""]
    if not n_extract and not n_summary:
        lines += ["所有块与小节都已处理，直接收尾即可。", ""]
    lines += ["## 3. 收尾（本地执行，不调用模型）", "",
              "```bash",
              f"chipdoc finish {wd.dir} --out {out}",
              "```", "",
              "`finish` 会做：收编 response → 页码校验（丢弃编造引文）→ 归并 → 生成 "
              "`summary.md` / `matrix.md` / `diff.md`。", ""]
    path = wd.dir / "TASKS.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def cmd_outbox(args) -> int:
    for wd in _wd(args.workdirs):
        info = stage_outbox(wd, batch_size=args.batch)
        print(f"+ {wd.slug}: 抽取 {info['chunks']} 块 → {info['extract_batches']} 个包，"
              f"浓缩 {info['sections']} 节 → {info['summary_batches']} 个包")
        print(f"  目录：{info['dir']}")


def cmd_prepare(args) -> int:
    """本地准备：索引 + 生成 prompt + 写任务清单。全程不调用模型。"""
    external = ExternalProvider(ProviderConfig(kind="external"))
    for pdf in args.pdfs:
        wd = stage_index(pdf, args.work, force=args.refresh,
                         kind=args.kind, fields_path=args.fields, backend=args.backend)
        print(f"+ {wd.slug}: {wd.index.page_count} 页 / {len(wd.chunk_files())} 块 / 类型 {wd.index.kind}")
        if args.rules:
            r = stage_extract(wd, external, engine="rules")
            print(f"  rules 基线: {r.get('rules_facts', 0)} 条")
        ex = stage_extract(wd, external, engine="llm")
        sm = stage_summarize(wd, external, jobs=1)
        batches = stage_outbox(wd, batch_size=args.batch)
        tasks = _write_tasks(wd, ex["pending"], sm["pending"], args.out, batches)
        print(f"  待处理：抽取 {ex['pending']} 块（{batches['extract_batches']} 个包），"
              f"浓缩 {sm['pending']} 节（{batches['summary_batches']} 个包）")
        print(f"  任务清单：{tasks}")
        print(f"  完成后本地收尾：chipdoc finish {wd.dir} --out {args.out}")


def cmd_finish(args) -> int:
    """本地收尾：收编 → 校验 → 归并 → 渲染。全程不调用模型。"""
    roots: list[Path] = []
    for wd in _wd(args.workdirs):
        ing = stage_ingest(wd)
        v = stage_verify(wd)
        stage_merge(wd)
        flag = f"，仍有 {ing['missing']} 个 response 缺失" if ing["missing"] else ""
        print(f"+ {wd.slug}: 收编 {ing['ingested']}，保留 {v['kept']}（逐字 {v['verified']}），"
              f"丢弃 {v['dropped']}{flag}")
        if wd.dir.parent not in roots:
            roots.append(wd.dir.parent)
    code = 0
    for root in roots:
        code = cmd_render(argparse.Namespace(work=str(root), out=args.out)) or code
    return code


def cmd_status(args) -> int:
    for wd in _wd(args.workdirs):
        chunks = wd.chunk_files()
        done = len(chunks) - len(_chunk_pending(wd))
        v = _read(wd.dir / "facts" / "verified.json", {})
        si = _read(wd.dir / "summary" / "sections.json", {})
        print(f"{wd.slug}: {wd.index.page_count} 页 · {len(chunks)} 块 · 类型 {wd.index.kind}")
        rules = _read(wd.dir / "facts" / "raw" / "rules.json", {})
        print(f"  extract : {done}/{len(chunks)} 完成"
              + (f" · 规则引擎 {len(rules.get('facts', []))} 条" if rules else ""))
        if v:
            st = v.get("stats", {})
            print(f"  verify  : 保留 {st.get('kept', 0)}（逐字 {st.get('verified', 0)}）"
                  f"，丢弃 {st.get('dropped', 0)}")
        pend = _summary_pending(wd)
        if si:
            print(f"  summary : {len(si.get('sections', []))} 节，"
                  f"要点 {si.get('stats', {}).get('bullets', 0)}")
        elif pend:
            print(f"  summary : 待处理 {len(pend)} 节")
        if (wd.dir / "profile.json").exists():
            print("  profile : 已生成")


def cmd_pending(args) -> int:
    for wd in _wd(args.workdirs):
        if args.stage == "extract":
            items = _chunk_pending(wd)
            for cid, pages in items[:args.limit]:
                print(f"{cid}\tpages={','.join(map(str, pages))}")
            if len(items) > args.limit:
                print(f"… 还有 {len(items) - args.limit} 个")
        else:
            items = _summary_pending(wd)
            for sid, title, page in items[:args.limit]:
                print(f"{sid}\tpage={page}\t{title}")
            if len(items) > args.limit:
                print(f"… 还有 {len(items) - args.limit} 节")


def cmd_show(args) -> int:
    wd = Workdoc(args.workdir)
    if args.stage == "extract":
        chunk = _read(wd.dir / "chunks" / f"{args.key}.json")
        if not chunk:
            print(f"没有块 {args.key}", file=sys.stderr)
            return 2
        print(f"# {wd.slug} 块 {args.key} 页码 {chunk['pages']}", file=sys.stderr)
        print("--- SYSTEM ---")
        print(EXTRACT_SYSTEM)
        print("--- USER ---")
        print(extract_prompt(wd.fields, chunk["text"]))
    else:
        from .index import Section
        blocks = wd.index.section_blocks(1, 2)[:20]
        if not blocks:
            blocks = [Section(1, s.sections[0] if s.sections else f"块 {s.cid}", s.pages[0])
                      for s in wd.index.chunks][:20]
        sec = None
        for i, b in enumerate(blocks):
            if f"s{i:03d}" == args.key:
                sec = b
                break
        if not sec:
            print(f"没有小节 {args.key}", file=sys.stderr)
            return 2
        print("--- SYSTEM ---")
        print(SUMMARY_SYSTEM)
        print("--- USER ---")
        print(summary_prompt(sec.title, wd.index.block_text(sec, limit_chars=12000)))
    return 0


def cmd_submit(args) -> int:
    """agent 用：把一段 JSON 答案写进 response/，之后 ingest 收编。"""
    raw = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    try:
        json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"不是合法 JSON：{e}", file=sys.stderr)
        return 2
    wd = Workdoc(args.workdir)
    target = wd.dir / "response" / args.stage / f"{args.key}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(raw, encoding="utf-8")
    print(f"已写入 {target.relative_to(wd.dir)}")
    return 0


def cmd_where(args) -> int:
    """我在哪、装了哪些东西、插件从哪来。"""
    import platform

    from . import plugins as plugin_mod

    print(f"chipdoc     {__version__}")
    print(f"python      {sys.version.split()[0]} ({platform.platform()})")
    print(f"解释器      {sys.executable}")
    print(f"包位置      {Path(__file__).resolve().parent}")
    print(f"当前目录    {Path.cwd()}")
    print(f"前缀        {sys.prefix}" + ("  (venv)" if sys.prefix != sys.base_prefix else "  (系统 Python)"))
    for d in (Path("work"), Path("out"), Path("dist")):
        print(f"  {d.name:<8} {'存在 ' + str(d.resolve()) if d.exists() else '不存在'}")
    mods = plugin_mod.load()
    print(f"插件        {len(mods)} 个" + ("" if not mods else "：" + ", ".join(m.__name__ for m in mods)))
    print(f"PDF 后端    {'pymupdf' if __import__('chipdoc.backend', fromlist=['_has_pymupdf'])._has_pymupdf() else 'pure-python'}")
    return 0


def cmd_doctor(args) -> int:
    """环境与文档体检：哪些能力可用、这份 PDF 能不能直接跑。"""
    import shutil

    from .backend import Doc, _has_pymupdf

    print(f"python      {sys.version.split()[0]}")
    print(f"pymupdf     {'已安装（自动优先使用）' if _has_pymupdf() else '未安装（使用内置纯 Python 解析）'}")
    print("可选工具：")
    for tool, why in (("tesseract", "OCR 引擎"), ("ocrmypdf", "整本 OCR"),
                      ("pdftotext", "外部文本抽取"), ("gs", "Ghostscript")):
        path = shutil.which(tool)
        print(f"  {tool:<10} {'✓ ' + path if path else '✗ 未安装'}   （{why}）")
    if not args.pdf:
        print("\n提示：加一个 PDF 路径可以体检具体文档。")
        return 0

    with Doc(args.pdf, backend=args.backend) as doc:
        n = doc.page_count
        texts = [doc.page_text(i) for i in range(n)]
        empty = [i + 1 for i, t in enumerate(texts) if len(t.strip()) < 40]
        chars = sum(len(t) for t in texts)
        toc = doc.toc()
        meta = doc.metadata()
    print(f"\n文档：{args.pdf}")
    print(f"  解析后端      {doc.name}")
    print(f"  页数          {n}")
    print(f"  标题          {meta.get('title') or '(无)'}")
    print(f"  文本层        {n - len(empty)}/{n} 页有文本，共 {chars:,} 字符")
    print(f"  需 OCR 的页   {len(empty)} 页" + (f"，例如 {empty[:10]}" if empty else ""))
    print(f"  书签目录      {len(toc)} 条" + ("" if toc else "（无，将退化为按页切分）"))
    print(f"  估算 token    ~{int(chars / 2.2):,}（切块后每块约 3.3k 字符）")
    if not empty:
        verdict = "可直接分析"
    elif len(empty) == n:
        verdict = "纯扫描件：必须先 OCR"
    else:
        verdict = f"部分可分析：{len(empty)} 页无文本层，涉及这些页的结论会缺失"
    print(f"  结论          {verdict}")
    return 0


def cmd_inspect(args) -> int:
    idx = build_index(args.pdf, kind=args.kind)
    print(f"{idx.title}\n{idx.page_count} 页 / 类型 {idx.kind} / sha {idx.sha[:12]}")
    print(f"目录 {len(idx.toc)} 条，前 10 条：")
    for s in idx.toc[:10]:
        print(f"  {'  ' * (s.level - 1)}{s.title}  p.{s.page}")
    empty = [i for i, t in enumerate(idx.pages, 1) if not t.strip()]
    print(f"无文本层页面：{empty[:20]}{' …' if len(empty) > 20 else ''}（共 {len(empty)} 页）")
    print(f"切块：{len(idx.chunks)} 块，平均 {sum(c.chars for c in idx.chunks) // max(1, len(idx.chunks))} 字符")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="chipdoc",
        description="芯片/电子类 PDF 分析流水线：索引 → 抽取 → 校验 → 浓缩 → 归并 → 渲染",
        epilog="第一次用先跑：chipdoc where；体检文档：chipdoc doctor 文件.pdf")
    ap.add_argument("-V", "--version", action="version", version=f"chipdoc {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="命令")

    p = sub.add_parser("where", help="显示安装位置/解释器/插件/后端，排查“东西在哪”")
    p.set_defaults(func=cmd_where)

    p = sub.add_parser("doctor", help="体检：环境能力 + PDF 是否可分析")
    p.add_argument("pdf", nargs="?", default=None, help="可选：体检指定 PDF")
    p.add_argument("--backend", choices=["auto", "pure", "pymupdf"], default="auto",
                   help="强制使用某个解析后端")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("inspect", help="预览页数/目录/切块/无文本层页")
    p.add_argument("pdf", help="PDF 路径")
    p.add_argument("--kind", default=None, help="强制指定文档类型")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("index", help="① 建索引与切块（不调用模型）")
    p.add_argument("pdfs", nargs="+", help="一个或多个 PDF")
    p.add_argument("--work", default="work", help="工作目录根，默认 work")
    p.add_argument("--kind", default=None, help="datasheet / trm / generic（默认自动判断）")
    p.add_argument("--fields", default=None, help="自定义字段集 JSON")
    p.add_argument("--refresh", action="store_true", help="忽略已有索引，重新建立")
    p.add_argument("--backend", choices=["auto", "pure", "pymupdf"], default="auto",
                   help="PDF 解析后端：auto 优先 PyMuPDF，没有则用内置纯 Python")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("prepare", help="①+ 本地准备：索引 + 任务包 + 清单（不调用模型）")
    p.add_argument("pdfs", nargs="+", help="一个或多个 PDF")
    p.add_argument("--work", default="work", help="工作目录根，默认 work")
    p.add_argument("--out", default="out", help="输出目录，默认 out")
    p.add_argument("--kind", default=None, help="datasheet / trm / generic")
    p.add_argument("--fields", default=None, help="自定义字段集 JSON")
    p.add_argument("--refresh", action="store_true", help="忽略缓存，重新建立")
    p.add_argument("--rules", action="store_true", help="同时跑一遍规则引擎基线")
    p.add_argument("--batch", type=int, default=4, help="每个任务包放几个块，默认 4")
    p.add_argument("--backend", choices=["auto", "pure", "pymupdf"], default="auto",
                   help="PDF 解析后端")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("outbox", help="重新生成可复制粘贴的任务包（不调用模型）")
    p.add_argument("workdirs", nargs="+", help="工作目录")
    p.add_argument("--batch", type=int, default=4, help="每个任务包放几个块，默认 4")
    p.set_defaults(func=cmd_outbox)

    p = sub.add_parser("extract", help="② 抽取：块 → facts（可调模型 / 可只出任务包）")
    p.add_argument("workdirs", nargs="+", help="工作目录，如 work/tms320f28054f")
    p.add_argument("--engine", choices=["llm", "rules", "both"], default="llm",
                   help="llm=调模型；rules=规则引擎；both=都跑后合并")
    p.add_argument("--jobs", type=int, default=6, help="并发数，默认 6")
    p.add_argument("--force", action="store_true", help="忽略已有结果，强制重跑")
    p.add_argument("--chunks", default=None, help="只跑指定块，逗号分隔，如 c010,c011")
    _provider_args(p)
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("submit", help="②' 把 agent/手动的 JSON 答案写进 response/")
    p.add_argument("workdir", help="工作目录")
    p.add_argument("key", help="块 id（c000）或小节 id（s000），也可是任务包名 b001")
    p.add_argument("--stage", choices=["extract", "summary"], default="extract", help="提交哪一阶段")
    p.add_argument("--file", default=None, help="JSON 文件；省略则从 stdin 读")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("ingest", help="②' 收编 response/ 里的答案")
    p.add_argument("workdirs", nargs="+", help="工作目录")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("verify", help="③ 页码校验：引文是否真在该页（不调用模型）")
    p.add_argument("workdirs", nargs="+", help="工作目录")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("summarize", help="④ 分节浓缩（可单独换模型）")
    p.add_argument("workdirs", nargs="+", help="工作目录")
    p.add_argument("--jobs", type=int, default=4, help="并发数，默认 4")
    p.add_argument("--force", action="store_true", help="忽略已有要点，重新生成")
    _provider_args(p)
    p.set_defaults(func=cmd_summarize)

    p = sub.add_parser("merge", help="⑤ 归并成 profile.json（不调用模型）")
    p.add_argument("workdirs", nargs="+", help="工作目录")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("render", help="⑥ 生成 summary/matrix/diff（不调用模型）")
    p.add_argument("--work", default="work", help="工作目录根，默认 work")
    p.add_argument("--out", default="out", help="输出目录，默认 out")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("finish", help="⑥+ 本地收尾：ingest+verify+merge+render（不调用模型）")
    p.add_argument("workdirs", nargs="+", help="工作目录")
    p.add_argument("--work", default="work", help="（保留参数，收尾时按工作目录自动定位）")
    p.add_argument("--out", default="out", help="输出目录，默认 out")
    p.set_defaults(func=cmd_finish)

    for name in ("run", "analyze"):
        p = sub.add_parser(name, help="一条龙：index → extract → verify → merge → render")
        p.add_argument("pdfs", nargs="+", help="一个或多个 PDF")
        p.add_argument("--work", default="work", help="工作目录根，默认 work")
        p.add_argument("--out", default="out", help="输出目录，默认 out")
        p.add_argument("--engine", choices=["llm", "rules", "both"], default="llm",
                       help="llm=调模型；rules=规则引擎；both=两者合并")
        p.add_argument("--jobs", type=int, default=6, help="并发数，默认 6")
        p.add_argument("--refresh", action="store_true", help="忽略缓存，全部重跑")
        p.add_argument("--kind", default=None, help="datasheet / trm / generic")
        p.add_argument("--fields", default=None, help="自定义字段集 JSON")
        p.add_argument("--backend", choices=["auto", "pure", "pymupdf"], default="auto",
                       help="PDF 解析后端")
        _provider_args(p)
        p.set_defaults(func=cmd_run)

    p = sub.add_parser("compare", help="只重建矩阵与 diff（不调用模型）")
    p.add_argument("--work", default="work", help="工作目录根，默认 work")
    p.add_argument("--out", default="out", help="输出目录，默认 out")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("status", help="查看各阶段进度（不调用模型）")
    p.add_argument("workdirs", nargs="+", help="工作目录")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("pending", help="列出还没处理的块/小节")
    p.add_argument("workdirs", nargs="+", help="工作目录")
    p.add_argument("--stage", choices=["extract", "summary"], default="extract",
                   help="extract=抽取块；summary=分节浓缩")
    p.add_argument("--limit", type=int, default=20, help="最多列出几条，默认 20")
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("show", help="打印某块/某节的完整请求（system+user），给 agent 用")
    p.add_argument("workdir", help="工作目录")
    p.add_argument("key", help="块 id（c000）或小节 id（s000）")
    p.add_argument("--stage", choices=["extract", "summary"], default="extract",
                   help="看哪一阶段的请求")
    p.set_defaults(func=cmd_show)

    args = ap.parse_args(argv)
    try:
        return args.func(args) or 0
    except (ProviderError, FileNotFoundError) as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
