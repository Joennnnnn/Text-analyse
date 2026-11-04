# chipdoc — 芯片 / 电子类 PDF 分析流水线

把几十万字符的技术文档（datasheet、TRM、应用笔记、勘误、用户指南）压成：

- **带页码溯源的浓缩**（每条结论都能跳回原文该页）
- **多型号横向对比表**（CSV/Markdown）
- **增量 diff**（新增型号、字段变化）

特点：阶段化、文件化、**不绑定模型**。全程可以零 API 调用——本地生成任务包，你手动贴给任意模型，再把 JSON 放回来。

## 目录

1. [它能做什么](#它能做什么)
2. [安装与快速开始](#安装与快速开始)
3. [三种工作方式](#三种工作方式)
4. [离线运行与扩展](#离线运行与扩展)
5. [命令参考](#命令参考)
6. [参数速查表](#参数速查表)
7. [环境变量与配置文件](#环境变量与配置文件)
8. [目录与产物](#目录与产物)
9. [页码溯源原理](#页码溯源原理)
10. [成本与性能](#成本与性能)
11. [已知限制](#已知限制)
12. [测试](#测试)

## 它能做什么

| 能力 | 说明 |
|---|---|
| 文档类型 | datasheet / TRM / 应用笔记 / 勘误 / 通用技术文档；也可自定义字段集 |
| 输入 | 任意有文本层的 PDF（1 份或多份） |
| 输出 | `summary.md`（带 `[p.N]`）、`matrix.md/csv`、`diff.md` |
| 判断层 | 任意 OpenAI 兼容模型、Claude、本地命令行，或 **agent / 人手动完成** |
| 溯源 | 每条结论强制 `page` + 逐字 `quote`，脚本回查原文，查不到即丢弃 |
| 增量 | 按文件指纹复用，新增文档只跑新增部分 |

## 安装与快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -e .             # 安装 chipdoc 命令 + 唯一依赖 PyMuPDF

# 有模型 key 时，一条龙
export DEEPSEEK_API_KEY=sk-...
chipdoc run 手册.pdf --out out

# 没有 key / 不想联网：本地准备 → 手动贴模型 → 本地收尾
chipdoc prepare 手册.pdf --work work --batch 4
# 复制 work/<slug>/outbox/extract/b001.md 给任意模型，JSON 存到 response/extract/b001.json
chipdoc finish work/<slug> --out out
```

> 下文所有 `chipdoc` 指 `.venv/bin/chipdoc`（激活 venv 后直接可用）。不想安装时，把 `chipdoc` 换成 `.venv/bin/python -m chipdoc`，行为完全一致。

## 三种工作方式

| 方式 | 谁做判断 | 联网 | 典型命令 |
|---|---|---|---|
| A. 脚本调模型 | 远程/本地模型 | 是 | `run` / `extract --provider openai` |
| B. agent 驱动 | 当前 agent | 否 | `prepare` → `show` → `submit` → `finish` |
| C. 纯手动 | 聊天窗口里的模型 | 否 | `prepare --batch` → 复制 `outbox/*.md` → 存 `response/*.json` → `finish` |

## 离线运行与扩展

### 方式一：塞进已有的 venv（推荐，不需要 pip）

离线机器上已经有一个 `.venv` 时，把源码目录里的包复制进去即可：

```bash
# 在能访问源码的地方执行（复制一个目录，不联网、不装包）
python3 tools/install_into_venv.py /目标机/.venv

# 目标机上直接用这个 venv
/目标机/.venv/bin/chipdoc doctor 手册.pdf
/目标机/.venv/bin/python -m chipdoc run 手册.pdf --engine rules
```

它会做三件事：把 `chipdoc/` 复制到 venv 的 site-packages、复制 `plugins/`（如果有）、在 `bin/` 下生成 `chipdoc` 启动器。venv 里没有 PyMuPDF 也能跑（自动用内置纯 Python 解析）。

### 方式二：把环境一起打包带走

```bash
python tools/build_bundle.py --profile pdf     # 约 27 MB 压缩包，带轻量 PyMuPDF
python tools/build_bundle.py --profile lean    # 几 MB，无第三方依赖
python tools/build_bundle.py --profile full    # 完整复制当前 .venv（很大）

# 目标机解压后
./run.sh doctor 手册.pdf
./run.sh prepare 手册.pdf --work work --batch 4
```

| profile | 体积 | 内容 | 构建时需要联网 |
|---|---|---|---|
| `lean` | ~4 MB | 仅 chipdoc + 内嵌解析器 | 否 |
| `pdf` | ~27 MB | 加 PyMuPDF 1.x（表格/更快） | 是（装一次） |
| `full` | ~280 MB | 完整复制源 venv | 否 |

产物：`dist/chipdoc-bundle/` 和 `dist/chipdoc-bundle.tar.gz`，带 `run.sh` 和 `README-OFFLINE.txt`。

> 可搬迁的前提：目标机与构建机同架构、同 glibc，且 venv 的 `pyvenv.cfg` 里 `home` 指向的 Python 路径存在。跨发行版/大版本时，最稳的是「目标机已有 Python → 用方式一塞包」，或改用 `lean` + 纯解析器。

### 方式三：单文件 zipapp

```bash
python3 tools/build_pyz.py                 # dist/chipdoc.pyz（约 50 KB）
python3 dist/chipdoc.pyz doctor 手册.pdf
```

只依赖 Python 3.11+ 标准库，适合“机器上有 python3、但什么包都不能装”的场景。

### 解析后端

- 装了 PyMuPDF → 自动优先使用（快、支持表格检测）
- 没装 → 自动退回内置纯 Python 解析器（`chipdoc/pdfread.py`）
- 强制指定：`--backend pure|pymupdf|auto`，或 `CHIPDOC_BACKEND=pure`

内置解析器覆盖：经典 xref（含增量更新）、FlateDecode/ASCIIHex/ASCII85/RunLength、页面树、书签目录、Type0(Identity-H)+ToUnicode、简单字体 Differences、Tj/TJ/Tm/Td 等文本算子。
不支持并会明确报错：加密 PDF、xref 流、对象流、OCR。

### 插件：不装包也能扩展

把 `xxx.py` 放进下面任一位置，启动时自动加载：

1. `CHIPDOC_PLUGINS` 环境变量里的目录（`:` 分隔）
2. 当前目录的 `plugins/`
3. `~/.chipdoc/plugins/`

可选钩子（普通函数，异常不影响主流程）：

```python
def fields(kind):            # 追加字段，进入抽取 prompt 和对比表
    return [{"id": "pin_count", "label": "引脚数", "hint": "封装引脚数"}]

def on_facts(facts, index):  # 页码校验之后、归并之前
    return facts

def on_profile(profile):     # 归并后的字典
    return profile

def markdown(profile):       # 追加到 summary.md 末尾
    return "## 备注\n- ..."
```

完整例子见 `examples/plugin_example.py`（复制到 `plugins/` 即生效）。

## 命令参考

通用约定：

- `<workdir>`：某份文档的工作目录，形如 `work/tms320f28054f`，由 `index` / `prepare` 生成
- `<slug>`：PDF 文件名转成的小写短名（`TMS320F28054F.pdf` → `tms320f28054f`）
- 🔒 标记的命令**完全不联网**；🌐 表示可能调用模型
- 每个命令都支持 `-h/--help`
- 退出码：`0` 成功，`1` 运行错误（provider/文件），`2` 参数或输入错误

### 1. 体检与预览

#### `chipdoc doctor [pdf]` 🔒

检查环境（PyMuPDF、OCR 工具）和某份 PDF 能否直接分析。**任何操作前建议先跑一次。**

| 参数 | 说明 |
|---|---|
| `pdf` | 可选。不传只报环境 |
| `--backend` | 强制解析后端：`auto` / `pure` / `pymupdf` |

```bash
chipdoc doctor                  # 只看环境
chipdoc doctor 手册.pdf          # 看这份文档的文本层/目录/加密/图片
```

输出包含：页数、文本层覆盖页数、需 OCR 的页、书签数、估算 token、结论（可直接分析 / 部分可分析 / 必须先 OCR）。

#### `chipdoc inspect <pdf>` 🔒

预览文档结构：页数、目录前 10 条、无文本层页、切块数量与平均大小。

| 参数 | 说明 |
|---|---|
| `pdf` | 必填 |
| `--kind` | 强制文档类型：`datasheet` / `trm` / `generic` |

```bash
chipdoc inspect 手册.pdf --kind trm
```

### 2. 准备（全部 🔒）

#### `chipdoc index <pdf...>` 🔒

建立索引：逐页抽文本（清页眉页脚噪声）、读 PDF 书签、按 ≤3 页且 ≤4500 字符切块。

| 参数 | 默认 | 说明 |
|---|---|---|
| `pdfs` | — | 一个或多个 PDF |
| `--work` | `work` | 工作目录根 |
| `--kind` | 自动 | `datasheet` / `trm` / `generic` |
| `--fields` | 无 | 自定义字段集 JSON（`[{"id","label","hint"}]`） |
| `--refresh` | 关 | 忽略已有索引，强制重建 |
| `--backend` | `auto` | `auto` / `pure` / `pymupdf` |

```bash
chipdoc index 手册.pdf 另一份.pdf --work work --kind datasheet
```

产物：`work/<slug>/{doc,pages,toc}.json`、`work/<slug>/chunks/cNNN.json`。

#### `chipdoc prepare <pdf...>` 🔒

`index` 的超集：索引 + 生成任务包（outbox）+ 写 `TASKS.md`。**手动/agent 流程从这里开始。**

| 参数 | 默认 | 说明 |
|---|---|---|
| `pdfs` | — | 一个或多个 PDF |
| `--work` | `work` | 工作目录根 |
| `--out` | `out` | 输出目录（写进 TASKS.md 的收尾命令里） |
| `--kind` | 自动 | `datasheet` / `trm` / `generic` |
| `--fields` | 无 | 自定义字段集 |
| `--batch` | `4` | 每个任务包包含几个块（3–5 适合手动粘贴） |
| `--rules` | 关 | 同时跑规则引擎，先得到一份粗基线 |
| `--refresh` | 关 | 忽略缓存重建 |
| `--backend` | `auto` | PDF 解析后端 |

```bash
chipdoc prepare 手册.pdf --work work --out out --batch 4 --rules
```

产物：`work/<slug>/outbox/extract/b001.md`（可粘贴的任务包）、`outbox/summary/b001.md`、`TASKS.md`。

#### `chipdoc outbox <workdir...>` 🔒

重新生成任务包。用于“做了一部分、想继续”或改了 `--batch`。

| 参数 | 默认 | 说明 |
|---|---|---|
| `workdirs` | — | 工作目录 |
| `--batch` | `4` | 每包块数 |

```bash
chipdoc outbox work/tms320f28054f --batch 2   # 已完成的块不会重复出现
```

### 3. 抽取（判断层）

#### `chipdoc extract <workdir...>` 🌐

把每个块交给模型，产出 `facts/raw/cNNN.json`。同一块已有结果会跳过（增量）。

| 参数 | 默认 | 说明 |
|---|---|---|
| `workdirs` | — | 工作目录 |
| `--engine` | `llm` | `llm` 调模型 / `rules` 只用规则 / `both` 两者都跑后合并 |
| `--jobs` | `6` | 并发数 |
| `--chunks` | 全部 | 只跑指定块，如 `c010,c011` |
| `--force` | 关 | 忽略已有结果重跑 |
| `--provider` | `openai` | `openai` / `anthropic` / `command` / `external` |
| `--model` | 端点默认 | 模型名 |
| `--base-url` | 端点默认 | OpenAI 兼容地址 |
| `--api-key-env` | 见环境变量 | 从哪个环境变量读 key |
| `--json-mode` | 关 | 请求 JSON 模式 |
| `--config` | 无 | 多模型配置，见下文 |

```bash
# 任何 OpenAI 兼容端点
chipdoc extract work/a --model qwen-max \
  --base-url https://dashscope.aliyuncs.com/compatible-mode/v1 --api-key-env DASHSCOPE_API_KEY

# 只重跑两块，换个模型对比效果
chipdoc extract work/a --chunks c010,c011 --force --model 另一个模型

# 只生成任务包，不调模型
chipdoc extract work/a --provider external
```

| provider | 需要什么 | 说明 |
|---|---|---|
| `openai` | `base_url` + `model` + key | DeepSeek / Qwen / Kimi / vLLM / Ollama / LM Studio 等 |
| `anthropic` | key | Claude Messages API |
| `command` | `command: [...]` | prompt 走 stdin，答案读 stdout，如 `["ollama","run","qwen2.5"]` |
| `external` | 无 | 只写任务包/请求文件，答案由外部写回 |

#### `chipdoc show <workdir> <key>` 🔒

打印某块（或某小节）的完整请求（system + user）。给 agent 读，或给你复制。

| 参数 | 说明 |
|---|---|
| `workdir` | 工作目录 |
| `key` | 块 id `c000`，或小节 id `s000` |
| `--stage` | `extract`（默认）/ `summary` |

```bash
chipdoc show work/a c000
chipdoc show work/a s000 --stage summary
```

#### `chipdoc submit <workdir> <key>` 🔒

把一份 JSON 答案写进 `response/`。`key` 可以是块 id、小节 id，或任务包名 `b001`。

| 参数 | 说明 |
|---|---|
| `workdir` | 工作目录 |
| `key` | `c000` / `s000` / `b001` |
| `--stage` | `extract`（默认）/ `summary` |
| `--file` | JSON 文件；省略则读 stdin |

```bash
chipdoc submit work/a c000 --file facts.json
echo '{"facts":[...]}' | chipdoc submit work/a c000
```

#### `chipdoc ingest <workdir...>` 🔒

收编 `response/` 里的答案到 `facts/raw/`。支持三种格式：

- 单块：`{"facts":[{field,value,page,quote}]}`，文件名即块 id
- 批量：`{"facts":[{cid,field,value,page,quote}]}`，按 `cid` 自动拆分
- 摘要批量：`{"sections":[{"sid":"s000","bullets":[...]}]}`

```bash
chipdoc ingest work/a
```

#### `chipdoc status <workdir...>` 🔒

看各阶段进度：多少块完成、校验保留/丢弃、摘要几节、profile 是否已生成。

```bash
chipdoc status work/a
# extract : 40/76 完成 · 规则引擎 13 条
# verify  : 保留 22（逐字 20），丢弃 2
# summary : 待处理 12 节
```

#### `chipdoc pending <workdir...>` 🔒

列出还没处理的块或小节。

| 参数 | 默认 | 说明 |
|---|---|---|
| `workdirs` | — | 工作目录 |
| `--stage` | `extract` | `extract` / `summary` |
| `--limit` | `20` | 最多列几条 |

```bash
chipdoc pending work/a --limit 5
chipdoc pending work/a --stage summary
```

### 4. 校验与浓缩

#### `chipdoc verify <workdir...>` 🔒

页码校验：把每条 fact 的 `quote` 拿到对应页原文里做 NFKC + 去空白比对。

- 逐字命中 → `verified`
- 只有取值命中 → `page_only`（降级保留）
- 都不命中 → `rejected`，写入 `verified.json` 的 `dropped`

```bash
chipdoc verify work/a
# 输入 40 条，保留 37（逐字 35，页级 2），丢弃 3
```

#### `chipdoc summarize <workdir...>` 🌐

按目录 level 1/2 分节生成要点，每条要求带 `[p.N]`；脚本还会做 bigram 落地检查，编造页码的要点会被丢弃。

| 参数 | 默认 | 说明 |
|---|---|---|
| `workdirs` | — | 工作目录 |
| `--jobs` | `4` | 并发 |
| `--force` | 关 | 重新生成 |
| provider 参数 | 同上 | 可单独换模型 |

```bash
chipdoc summarize work/a --provider anthropic --model claude-sonnet-4-5
chipdoc summarize work/a --provider external   # 只出浓缩任务包
```

### 5. 汇总与输出（全部 🔒）

#### `chipdoc merge <workdir...>` 🔒

合并 `facts/verified.json` + `summary/sections.json` → `profile.json`（字段去重、选优、保留备选值）。

```bash
chipdoc merge work/a
```

#### `chipdoc render` / `chipdoc compare` 🔒

从所有 `profile.json` 生成：

- `out/docs/<slug>/summary.md`（参数表 + 分节要点 + 其他发现）
- `out/matrix.md` / `matrix.csv` / `matrix.json`
- `out/diff.md` / `diff.json`（相对上次快照）

| 参数 | 默认 | 说明 |
|---|---|---|
| `--work` | `work` | 工作目录根 |
| `--out` | `out` | 输出目录 |

```bash
chipdoc render --work work --out out
chipdoc compare                 # 只是 render 的别名
```

#### `chipdoc finish <workdir...>` 🔒

本地收尾一条龙：`ingest → verify → merge → render`。

| 参数 | 默认 | 说明 |
|---|---|---|
| `workdirs` | — | 工作目录（可多个） |
| `--out` | `out` | 输出目录 |
| `--work` | `work` | 保留参数，收尾按工作目录自动定位根 |

```bash
chipdoc finish work/a work/b --out out
```

### 6. 一条龙

#### `chipdoc run <pdf...>` / `chipdoc analyze <pdf...>` 🌐

`index → extract → verify → summarize → merge → render`。

| 参数 | 默认 | 说明 |
|---|---|---|
| `pdfs` | — | 一个或多个 PDF |
| `--work` | `work` | 工作目录根 |
| `--out` | `out` | 输出目录 |
| `--engine` | `llm` | `llm` / `rules` / `both` |
| `--jobs` | `6` | 并发 |
| `--kind` | 自动 | 文档类型 |
| `--fields` | 无 | 自定义字段集 |
| `--refresh` | 关 | 全部重跑 |
| `--backend` | `auto` | PDF 解析后端 |
| provider 参数 | 同上 | 模型配置 |

```bash
chipdoc run 手册.pdf docs/另一份.pdf --jobs 8
chipdoc analyze 手册.pdf --engine rules          # 无 key 的粗基线
```

## 参数速查表

| 参数 | 适用命令 | 作用 |
|---|---|---|
| `--work` | index/prepare/render/finish/run/compare | 工作目录根，默认 `work` |
| `--out` | prepare/render/finish/run/compare | 输出目录，默认 `out` |
| `--kind` | index/prepare/run/inspect | `datasheet` / `trm` / `generic` |
| `--fields` | index/prepare/run | 自定义字段集 JSON |
| `--refresh` | index/prepare/run | 忽略缓存重建 |
| `--rules` | prepare | 同时跑规则引擎基线 |
| `--batch` | prepare/outbox | 每包块数，默认 4 |
| `--engine` | extract/run | `llm` / `rules` / `both` |
| `--jobs` | extract/summarize/run | 并发数 |
| `--chunks` | extract | 只跑指定块 |
| `--force` | extract/summarize | 忽略已有结果重跑 |
| `--stage` | show/submit/pending | `extract` / `summary` |
| `--limit` | pending | 列几条 |
| `--file` | submit | 从文件读 JSON |
| `--config` | extract/summarize/run | 多模型配置 |
| `--provider` | extract/summarize/run | `openai` / `anthropic` / `command` / `external` |
| `--model` | extract/summarize/run | 模型名 |
| `--base-url` | extract/summarize/run | OpenAI 兼容端点 |
| `--api-key-env` | extract/summarize/run | key 的环境变量名 |
| `--json-mode` | extract/summarize/run | 请求 JSON 模式 |
| `--backend` | doctor/index/prepare/run | `auto` / `pure` / `pymupdf` |

## 环境变量与配置文件

| 变量 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` | `openai` provider 默认 key（也认 `OPENAI_API_KEY`） |
| `ANTHROPIC_API_KEY` | `anthropic` provider 默认 key |
| `OPENAI_BASE_URL` | 默认端点，缺省 `https://api.deepseek.com/v1` |
| `CHIPDOC_MODEL` | 默认模型名 |

多模型配置（`chipdoc.example.json`），按阶段指定：

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
chipdoc extract   work/a --config chipdoc.json
chipdoc summarize work/a --config chipdoc.json
```

## 目录与产物

```
work/<slug>/
├── doc.json                  # 路径、sha、标题、类型、字段集
├── pages.json                # 逐页文本
├── toc.json                  # [level, title, page]
├── chunks/c000.json          # {"cid","pages","sections","text"}，text 带 <<<p.N>>>
├── outbox/extract/b001.md    # 手动任务包（含字段清单 + 多个块）
├── outbox/summary/b001.md    # 浓缩任务包
├── prompt/extract/c000.json  # 程序化交接用的单块请求
├── response/extract/b001.json# 你/模型写回的答案
├── facts/raw/c000.json       # 每块抽取出的事实（可审计）
├── facts/verified.json       # 保留 + 丢弃 + 统计
├── summary/sections.json     # 分节要点
├── TASKS.md                  # 任务清单
└── profile.json              # 归并后的文档画像

out/
├── docs/<slug>/summary.md    # 参数表 + 分节要点 + 其他发现
├── matrix.md / .csv / .json  # 横向对比
├── diff.md / diff.json       # 相对上次快照的增量
└── state.json                # 快照（用于 diff）
```

## 页码溯源原理

1. 抽取 prompt 强制每条 fact 给 `page` + `quote`
2. `verify` 用 NFKC + 去空白后的字符串比对回查该页原文
3. 命中 → `verified`；仅取值命中 → `page_only`；都不中 → 丢弃并记原因

不花 token，却能挡住“页码幻觉”。被丢弃的数量每次都会打印，丢弃率高说明引文不逐字。

## 成本与性能

| 文档 | 页数 | 字符 | 块数 | rules | LLM 估算 |
|---|---|---|---|---|---|
| TMS320F28054F（中） | 159 | 252k | 76 | ~1 s | ~11.5 万 input token，8 并发 2–3 分钟 |
| TMS320F28004x（英） | 275 | 521k | 156 | ~2 s | ~24 万 input token，8 并发 4–6 分钟 |

第二次分析同一文档：0 次模型调用（结果复用）。

## 已知限制

- 扫描件（无文本层）需先 OCR；`doctor` 会列出受影响的页
- 表格在文本层里常被竖排展开；需要精确表格可用 `page_tables()`（`chipdoc/index.py`，约 0.5 s/页）
- 规则引擎是粗基线，连接器数量这类说法不统一的字段会漏
- 引用的是 **PDF 物理页序**；印刷页码与物理页不一致时尚未做偏移校正
- 寄存器级对照（每个寄存器一条记录）尚未支持，当前对比粒度是文档级
- 内置纯 Python 解析器不处理加密 PDF、xref 流、对象流；这类文件会明确报错，请改用 PyMuPDF 后端
- 纯后端不提供表格结构抽取（`page_tables` 直接返回空），速度约为 PyMuPDF 的 1/5

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

17 个用例：错页拒绝、页级降级、归并优先级、diff 等价、全页覆盖、规则基线、假模型端到端、增量复用、分阶段换模型（command）、纯外部交接、generic 文档 + agent 循环、本地准备→手动答题→本地收尾。
