# chipdoc — 芯片 PDF 分析流水线（datasheet / TRM）

把几十万字符的芯片手册，压成**带页码可溯源**的结论、**可横向对比**的表格，并在新增型号时只跑新文档、输出**增量 diff**。

拆成 7 个阶段，**每阶段读文件、写文件**，所以：

- 可以只跑其中一段（比如只重跑某几块的抽取）
- 可以给每一段配不同的模型/工具（抽取用便宜模型、浓缩用强模型）
- 可以完全不调用模型：把 prompt 写成文件，交给任意工具或人，再把结果写回来
- 断点续跑、局部返工都不会重花前面的钱

## 阶段与产物

| 阶段 | 命令 | 输入 | 输出 | 用模型？ |
|---|---|---|---|---|
| ① 索引 | `chipdoc index` | PDF | `work/<slug>/{doc,pages,toc}.json`、`chunks/cNNN.json` | 否 |
| ② 抽取 | `chipdoc extract` | chunks | `facts/raw/cNNN.json`（每块一个文件） | 可换模型 / 可外部 |
| ②' 收编 | `chipdoc ingest` | `response/` | `facts/raw/*.json` | 否 |
| ③ 校验 | `chipdoc verify` | facts/raw | `facts/verified.json`（保留 + 丢弃 + 原因） | 否（纯字符串比对） |
| ④ 浓缩 | `chipdoc summarize` | 章节 | `summary/sections.json` | 可换另一个模型 / 可外部 |
| ⑤ 归并 | `chipdoc merge` | verified + sections | `profile.json` | 否 |
| ⑥ 渲染 | `chipdoc render` | 所有 profile | `out/docs/*/summary.md`、`out/matrix.{md,csv}`、`out/diff.md` | 否 |

一条龙：`chipdoc run`（= `analyze`），内部就是按顺序调用上面这些阶段。

辅助命令（给 agent / 人工用）：

| 命令 | 用途 |
|---|---|
| `chipdoc status <work>` | 看各阶段进度：多少块完成、校验保留/丢弃多少、摘要几节 |
| `chipdoc pending <work> [--stage summary]` | 列出还没处理的块/小节 |
| `chipdoc show <work> c000 [--stage summary]` | 打印该块完整请求（system+user），agent 读完直接作答 |
| `chipdoc submit <work> c000 --file ans.json` | 把答案写进 `response/`，随后 `ingest` 收编 |
| `chipdoc doctor [pdf]` | 体检：OCR 工具是否可用、PDF 有没有文本层/目录/加密，能不能直接跑 |

## 支持哪些文档

| 类型 | 字段集 | 说明 |
|---|---|---|
| datasheet | `DATASHEET_FIELDS` | 主频/存储/外设/电气/温度/封装 |
| TRM | `TRM_FIELDS` | 存储器映射/时钟/中断/寄存器/低功耗/调试 |
| 应用笔记、勘误、用户指南等 | `GENERIC_FIELDS` | 文档类型/适用范围/关键参数/接口/时序/寄存器/勘误 |
| 其它 | `--fields my.json` | 自定义 `[{id,label,hint}]`，任意文档都能套 |

默认按标题、元数据和目录自动判断；判断不准时用 `--kind generic` 或 `--fields` 覆盖。**任何有文本层的 PDF 都能跑**；扫描件需要先 OCR（`chipdoc inspect` 会列出无文本层的页）。

## 两种用法

**A. 脚本调模型**：`run` 或分阶段 `extract/summarize --provider ...`，适合批处理。

**B. agent 自己当模型（不调任何 API）**：skill 负责中间层，脚本只做切块和校验。

```bash
chipdoc index 文档.pdf --work work --kind generic
chipdoc pending work/<slug> --limit 5      # 看还有哪些块
chipdoc show    work/<slug> c000           # 拿到该块的 system+user
# agent 读原文、产出 {"facts":[...]} 并 submit
chipdoc submit work/<slug> c000 --file facts.json
chipdoc ingest work/<slug> && chipdoc verify work/<slug>
# 摘要同理：pending --stage summary → show → submit --stage summary → ingest
chipdoc merge work/<slug> && chipdoc render --work work --out out
```

完整协议见 [`skills/chip-doc-pipeline/SKILL.md`](skills/chip-doc-pipeline/SKILL.md)。

## 支持的后端（任选，可混用）

| provider | 说明 | 配置 |
|---|---|---|
| `openai` | 任意 OpenAI 兼容端点：DeepSeek、通义千问、Kimi、vLLM、Ollama、LM Studio、one-api… | `base_url` + `model` + `api_key_env` |
| `anthropic` | Claude Messages API | `model` + `ANTHROPIC_API_KEY` |
| `command` | 任意本地命令行：prompt 走 stdin，答案读 stdout | `command: ["ollama","run","qwen2.5"]` |
| `external` | 不调用任何模型：写 prompt 文件，等外部写回 | 无 |

```json
{
  "default":  {"provider": "openai", "model": "deepseek-chat",
               "base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY"},
  "stages": {
    "extract":   {"model": "qwen-max",
                  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "api_key_env": "DASHSCOPE_API_KEY"},
    "summarize": {"provider": "anthropic", "model": "claude-sonnet-4-5",
                  "api_key_env": "ANTHROPIC_API_KEY"}
  }
}
```

```bash
chipdoc extract work/a --config chipdoc.json
chipdoc summarize work/a --config chipdoc.json
```

单个阶段的临时覆盖：`--provider/--model/--base-url/--api-key-env`（优先级高于配置文件）。
想只重跑某些块、顺便换个模型试：

```bash
chipdoc extract work/a --chunks c010,c011 --force --model qwen-max --base-url ...
```

## 完全不用模型（外部交接）

```bash
chipdoc index manual.pdf --work work                 # 切块
chipdoc extract work/manual --provider external      # 只生成 prompt/extract/*.json
#  —— 这里换成任何工具：本地脚本、别的 AI、人工、批处理队列 ——
#  把每个 cNNN.json 的答案写到 response/extract/cNNN.json
chipdoc ingest  work/manual                          # 收编
chipdoc verify  work/manual                          # 页码校验，编造的直接丢
chipdoc summarize work/manual --provider external    # 浓缩同样可以外部做
chipdoc ingest work/manual && chipdoc merge work/manual
chipdoc render --work work --out out
```

请求文件长这样（自带 system/user，任何能发 prompt 的工具都能接）：

```json
{"cid":"c000","stage":"extract","system":"……","user":"……","expect":"json",
 "output":"facts/raw/c000.json"}
```

答案文件就是模型原样输出的 JSON：`{"facts":[{"field":"max_freq","value":"60 MHz","page":1,"quote":"…"}]}`。
解析失败的文件会被跳过并在 `ingest` 里计数，不会污染结果。

## 快速开始（有模型）

```bash
python3 -m venv .venv && .venv/bin/pip install pymupdf     # 只依赖 PyMuPDF

export DEEPSEEK_API_KEY=sk-...
.venv/bin/python -m chipdoc run tms320f28054f.pdf docs/tms320f280049c.pdf --jobs 8

# 或者分阶段来，便于换模型/返工
.venv/bin/python -m chipdoc index   *.pdf --work work
.venv/bin/python -m chipdoc extract work/tms320f28054f --model ... --base-url ...
.venv/bin/python -m chipdoc verify  work/tms320f28054f
.venv/bin/python -m chipdoc summarize work/tms320f28054f --model ...
.venv/bin/python -m chipdoc merge   work/tms320f28054f
.venv/bin/python -m chipdoc render  --work work --out out
```

没 key 也能跑粗基线：

```bash
.venv/bin/python -m chipdoc run *.pdf --engine rules
```

## 为什么页码能溯源

抽取 prompt 强制每条结论给 `page` + 逐字 `quote`，`verify` 阶段用 NFKC + 去空白后的字符串比对回查原文：

- 页码越界 / 该页无文本层 → 丢弃
- 引文片段命中 ≥60% 或整段逐字命中 → `verified`
- 只有取值命中 → `page_only`（保留但降级）
- 都不命中 → `rejected`，写入 `facts/verified.json` 的 `dropped`，带原因

这一步不花 token，却能把"页码幻觉"挡在结果外；每个块用哪个模型抽的、原始输出是什么，都留在 `facts/raw/`。

## 产物

```
work/<slug>/
├── pages.json / toc.json / doc.json
├── chunks/c000.json            # 带 <<<p.N>>> 的块文本
├── facts/raw/c000.json         # 该块模型原始输出（可审计）
├── facts/verified.json         # 保留/丢弃 + 统计
├── summary/sections.json       # 分节要点
└── profile.json                # 归并后的文档画像
out/
├── docs/<slug>/summary.md      # 参数表 + 分节要点，每条 [p.N] 链接
├── matrix.md / matrix.csv      # 横向对比（行=字段，列=文档）
├── diff.md                     # 相对上次快照：新增文档 / 取值变化 / 新增字段
└── state.json                  # 快照，用于增量
```

## 增量

`chipdoc extract` 遇到已有 `facts/raw/<cid>.json` 会跳过；新增一份 PDF 时只跑那一份的块，`render` 自动生成 diff（新增文档、字段变化、新增字段）。改了 prompt 版本或想全部重来用 `--force`。

## 成本 / 耗时（实测参考）

| 文档 | 页数 | 字符 | 块数 | rules | LLM 估算 |
|---|---|---|---|---|---|
| TMS320F28054F（中） | 159 | 252k | 76 | 0.7 s | ~11.5 万 input token，8 并发约 2–3 分钟 |
| TMS320F28004x（英） | 275 | 521k | 156 | 1.0 s | ~24 万 input token，8 并发约 4–6 分钟 |

## 已知限制

- 扫描版 PDF（无文本层）需要先 OCR；`chipdoc inspect` 会列出这些页。
- 表格在文本层里常被"竖着"展开；规则引擎用 label-walk 兜底，LLM 路径可用 `page_tables()`（`chipdoc/index.py`，按页缓存，约 0.5 s/页）拿到 markdown 表格。
- 规则引擎是粗基线（频率/存储/温度/供电/封装/常见外设计数），连接器数量这类说法不统一的信息会漏，以模型结果为准。
- TRM 走同一流程，字段集切换为 `TRM_FIELDS`。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

14 个用例：页码拒绝/降级、归并优先级、diff 等价、全页覆盖、规则基线、本地假模型端到端、二次复用、**分阶段换模型（command provider）**、**纯外部交接全流程**。
