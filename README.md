# chipdoc

把芯片 / 电子类 PDF（datasheet、TRM、应用笔记、勘误）变成：

- **带页码溯源的浓缩**：每条结论都能跳回原文该页
- **横向对比表**：多型号并排，单元格带 `p.N`
- **增量 diff**：新增型号时只处理新文档，输出字段级变化

核心设计：**阶段化 + 文件化 + 模型可选**。每一阶段读文件、写文件，可以只跑一段、换模型重跑、或完全人工完成。

## 工程结构

```
chipdoc/
├── cli.py         命令行入口（17 个命令）
├── parsing.py     PDF → 逐页文本 / 书签 / 切块
├── schema.py      字段集：datasheet / trm / generic / 自定义
├── prompts.py     抽取与浓缩的 prompt 模板
├── providers.py   模型后端：openai / anthropic / command / external
├── rules.py       规则引擎（无模型基线，可选）
├── pipeline.py    阶段实现：index → extract → verify → summarize → merge → render
├── verify.py      页码校验：引文是否真在该页
├── merge.py       归并：facts → 文档画像 + 要点
├── render.py      输出 summary.md / matrix.* / diff.*
├── plugins.py     插件加载（不装包也能扩展）
└── util.py        并发与 JSON 解析
tools/build_offline.py   构建 Windows 离线包（自带 Python + PyMuPDF）
tests/                   19 个用例
docs/OFFLINE.md          Windows 离线部署步骤
examples/plugin_example.py
samples/                 示例 PDF
```

## 最快用法（只需要 Python 3.11+ 和 pymupdf）

在仓库根目录执行：

```bat
py -3 -m pip install pymupdf
py -3 -m chipdoc doctor 手册.pdf
py -3 -m chipdoc run 手册.pdf --engine rules --out out
```

不需要安装 chipdoc 本身——从仓库根目录运行 `python -m chipdoc` 就会加载当前目录的包。
想在别的目录调用，再执行 `py -3 -m pip install -e .`。

也可以双击/运行仓库根目录的 `chipdoc.cmd`：它会自动挑解释器（`.venv` → `py -3` → `python`），
缺 pymupdf 时自动装一次。

## 三种工作方式

| 方式 | 谁做判断 | 联网 | 入口 |
|---|---|---|---|
| 脚本调模型 | 远程/本地模型 | 是 | `run` / `extract --provider ...` |
| agent 驱动 | 当前 agent（读 `show`、写 `submit`） | 否 | `prepare` → `show`/`submit` → `finish` |
| 纯手动 | 聊天窗口里的模型 | 否 | `prepare` → 复制 `outbox/*.md` → 存 `response/*.json` → `finish` |

## 5 分钟上手（不需要任何模型）

```bat
:: 1. 体检
chipdoc doctor 手册.pdf

:: 2. 本地准备：切块 + 任务包 + 清单
chipdoc prepare 手册.pdf --work work --batch 4

:: 3. 打开 work\<slug>\outbox\extract\b001.md，整份贴给任意模型
::    把返回的 JSON 存为 work\<slug>\response\extract\b001.json

:: 4. 本地收尾：收编 → 页码校验 → 归并 → 渲染
chipdoc finish work\<slug> --out out

:: 5. 看结果
type out\matrix.md
type out\docs\<slug>\summary.md
```

返回格式（每块一个 `cid`，`page` 与 `quote` 必填）：

```json
{"facts":[
  {"cid":"c000","field":"max_freq","value":"60 MHz","page":1,"quote":"60MHz（16.67ns 周期时间）"}
]}
```

## 命令参考

| 命令 | 作用 | 关键参数 |
|---|---|---|
| `doctor [pdf]` | 体检：环境、页数、文本层、目录、需 OCR 页 | `--kind` |
| `index <pdf...>` | 建索引与切块 | `--work` `--kind` `--fields` `--refresh` |
| `prepare <pdf...>` | index + 任务包 + `TASKS.md` | `--batch` `--rules` `--out` |
| `outbox <workdir...>` | 重新生成任务包（已完成的块不重复） | `--batch` |
| `extract <workdir...>` | 块 → facts（调模型或只出 prompt） | `--engine` `--provider` `--chunks` `--force` `--jobs` |
| `submit <workdir> <key>` | 把 JSON 答案写进 `response/` | `--stage` `--file` |
| `ingest <workdir...>` | 收编 `response/`（支持单块与批量 `cid`） | — |
| `verify <workdir...>` | 页码校验，丢弃编造引文 | — |
| `summarize <workdir...>` | 分节浓缩（可换模型） | `--provider` `--jobs` `--force` |
| `merge <workdir...>` | 归并成 `profile.json` | — |
| `render` | 生成 `summary.md` / `matrix.*` / `diff.*` | `--work` `--out` |
| `finish <workdir...>` | ingest + verify + merge + render | `--out` |
| `run <pdf...>` | 一条龙 | `--engine` `--provider` `--jobs` `--out` |
| `status` / `pending` / `show` | 进度、待办、打印某块请求 | `--stage` `--limit` |

通用参数：`--work`（默认 `work`）、`--out`（默认 `out`）、`--kind`（`datasheet`/`trm`/`generic`）、`--fields`（自定义字段集）、`--config`（多模型配置）。

模型相关：

| 参数 | 说明 |
|---|---|
| `--provider` | `openai`（任意兼容端点）/ `anthropic` / `command`（任意 CLI）/ `external`（只出 prompt） |
| `--model` `--base-url` `--api-key-env` | 端点与鉴权 |
| `--engine` | `llm` / `rules` / `both` |

每个命令的完整参数：`chipdoc <命令> --help`。

多模型配置（按阶段指定不同模型）示例见 [`chipdoc.example.json`](chipdoc.example.json)：
`chipdoc extract work/a --config chipdoc.example.json`。

## 产物

```
work/<slug>/
├── doc.json / pages.json / toc.json
├── chunks/c000.json            # 带 <<<p.N>>> 的块
├── outbox/extract/b001.md      # 可复制的任务包
├── response/extract/b001.json  # 答案
├── facts/raw/、facts/verified.json
├── summary/sections.json
└── profile.json

out/
├── docs/<slug>/summary.md      # 参数表 + 分节要点 + 其他发现
├── matrix.md / .csv / .json
└── diff.md / diff.json
```

## 页码为什么可信

1. 抽取要求每条 fact 带 `page` + 逐字 `quote`
2. `verify` 用 NFKC + 去空白后的字符串回查该页原文
3. 逐字命中 → `verified`；仅取值命中 → `page_only`；都不中 → 丢弃并记原因

不花 token，但能挡住“页码幻觉”。每次运行都会打印丢弃数量。

## 拷贝即用（不需要 Python / 不需要 pip）

目标机装不了 pymupdf 时，用仓库里打好的 ZIP：

```
dist/chipdoc-offline.zip        29 MB（内含 CPython 3.12 + PyMuPDF + chipdoc）
dist/chipdoc-offline.zip.sha256
```

三步：

1. 把 ZIP 复制到目标机，**右键 → 全部解压缩**（Windows 10/11 自带，不需要 7-Zip）
2. 进入 `chipdoc-offline\`，双击 `run.cmd`（不带参数会显示帮助并停住，确认能启动）
3. 在命令提示符里执行：

```bat
run.cmd where
run.cmd doctor 手册.pdf
run.cmd run 手册.pdf --engine rules --out out
```

包内自带解释器，`run.cmd` 用绝对路径调用，并加 `-E -s` 忽略机器上的 PYTHONHOME/PYTHONPATH，
所以不会和其他 Python 混用标准库（`SRE module mismatch` 就是混用导致的）。
详细步骤与排错见 [docs/OFFLINE.md](docs/OFFLINE.md)。

> 之前给的是 `.tar.gz`，Windows 资源管理器默认打不开，这通常就是“拷过去用不了”的原因；现在改成 ZIP。

## 插件

把 `xxx.py` 放进 `plugins/`（或 `CHIPDOC_PLUGINS` 指定目录）即自动加载，支持四个钩子：
`fields(kind)`、`on_facts(facts, index)`、`on_profile(profile)`、`markdown(profile)`。

示例见 `examples/plugin_example.py`。

## 测试

```bat
.venv\Scripts\python -m unittest discover -s tests -v
```

19 个用例：错页拒绝、页级降级、归并优先级、diff 等价、全页覆盖、规则基线、假模型端到端、
增量复用、分阶段换模型、纯外部交接、generic 文档 + agent 循环、本地准备→答题→收尾、离线包结构。

## 已知限制

- 扫描件需先 OCR（`doctor` 会列出无文本层页）
- 引用的是 PDF 物理页序；印刷页码与物理页不一致时未做偏移
- 寄存器级逐条对照尚未支持（当前是文档级字段）
- 表格依赖 PyMuPDF 的 `find_tables`，复杂跨页表格可能失真
