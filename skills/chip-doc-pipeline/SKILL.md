---
name: chip-doc-pipeline
description: 分析芯片/电子类技术文档（datasheet、TRM、应用笔记、勘误、用户指南、寄存器手册）的通用流程。分阶段文件化：索引→抽取→校验→浓缩→归并→渲染。中间层由本 skill 驱动，不需要任何模型 API——agent 自己读块、写结论，脚本负责切块、页码校验、汇总与对比。
---

# chip-doc-pipeline

**这个 skill 就是"中间层"。** 它不绑定模型：脚本只做确定性工作（切块、校验、汇总），判断与总结由当前 agent 完成，结论以文件交接。

## 适用

- 任意有文本层的 PDF 技术文档：datasheet / TRM / 应用笔记 / 勘误 / 用户指南 / 编程手册
- 需要"每句话可回查页码"的总结、跨型号对比、增量 diff
- 无法或不希望调用外部模型 API 的场景

不适用的：扫描件（先 OCR）、纯图片、加密 PDF。

## 前置

```bash
python3 -m venv .venv && .venv/bin/pip install pymupdf
```

文档类型三选一（默认自动判断）：

- `datasheet`：芯片数据手册（主频/存储/外设/电气/封装）
- `trm`：技术参考手册（存储器映射/时钟/中断/寄存器）
- `generic`：应用笔记、勘误、用户指南等（文档类型/适用范围/关键参数/时序/寄存器/勘误）
- 都不合适：`--fields my_fields.json` 自定义字段集（`[{"id","label","hint"}]`）

## 三种推进方式

| 方式 | 谁来做判断 | 是否调 API | 命令 |
|---|---|---|---|
| 脚本调模型 | 远程模型 | 是 | `run` / `extract --provider openai…` |
| agent 驱动 | 当前 agent | 否 | `prepare` → `show`/`submit` → `finish` |
| 纯手动 | 聊天窗口里的模型 | 否 | `prepare --batch 4` → 复制 `outbox/*.md` → 存 `response/*.json` → `finish` |

## 工作流（agent 驱动，不调用模型）

### 0. 建索引

```bash
chipdoc prepare 文档.pdf --work work --out out --kind generic   # 索引+prompt+任务清单
```

（等价于 `index` + 生成 prompt；`prepare` 之后直接读 `work/<slug>/TASKS.md` 即可。）

产物：`work/<slug>/{pages,toc,doc}.json`、`chunks/cNNN.json`。

### 1. 逐块抽取（循环，直到 pending 为空）

```bash
chipdoc pending work/<slug> --limit 5          # 列出待处理块
chipdoc show    work/<slug> c000               # 打印该块完整请求（system+user）
```

读 `show` 的输出，**只依据该块原文**产出：

```json
{"facts":[{"field":"max_freq","value":"60 MHz","page":1,"quote":"60MHz（16.67ns 周期时间）"}]}
```

写回：

```bash
chipdoc submit work/<slug> c000 --file facts.json    # 或 echo '…' | chipdoc submit …
```

每批处理完就 `chipdoc ingest work/<slug>`，再 `pending` 看下一批。**一次不要读超过 3 页**（一个块就是 ≤3 页）。

### 2. 校验（必做，脚本自动）

```bash
chipdoc ingest work/<slug>
chipdoc verify work/<slug>
```

`quote` 在该页原文里找不到 → 该条被丢弃，记入 `facts/verified.json` 的 `dropped`。若丢弃率很高，说明引文不逐字，回第 1 步重做该块。

### 3. 分节浓缩（同样循环）

```bash
chipdoc pending work/<slug> --stage summary --limit 5
chipdoc show    work/<slug> s000 --stage summary
```

产出 `{"bullets":["要点 [p.12]","…"]}`，`submit --stage summary`。要点 ≤40 字、页码必须来自该节原文；脚本会用 bigram 落地检查，编造页码的要点会被丢弃。

### 4. 归并 + 渲染

```bash
chipdoc finish work/<slug> --out out     # = ingest + verify + merge + render
```

产物：

- `out/docs/<slug>/summary.md`：参数表 + 分节要点，每条 `[p.N]`
- `out/matrix.md|csv`：多文档横向对比
- `out/diff.md`：相对上次快照的新增/变化

## 回答用户时

- 数值结论保留 `[p.N]`，页码只能来自产物，不要自己换算
- `—` = 没抽到，不等于"器件不支持"
- 置信度 `△ 仅页级` 的值要提示复核
- 新增型号：只把新 PDF 传给 `index`，其余复用，读 `diff.md`

## 铁律

- 不把整份 PDF 读进上下文；>3 页的原文一律走 `show`
- 不编造页号、不把 `dropped` 里的候选当结论
- 每写完一批就 `ingest`，避免最后一次性失败

## 常见问题

| 现象 | 处理 |
|---|---|
| 大量无文本层页面 | 扫描件，先 OCR |
| 目录为空/章节错乱 | 用 `--kind generic`；摘要会退化为按页抽取 |
| 字段不够/不适用 | `--fields` 自定义字段集后重新 `index --refresh` |
| 文档特别大（>1000 页） | 分批 `pending --limit`，先跑关键章节（`--chunks`） |
| 想接外部模型 | `chipdoc extract … --provider openai/anthropic/command/external`，与 agent 驱动可混用 |
