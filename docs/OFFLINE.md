# Windows 离线部署

> 仓库里已经带了一个构建好的成品：`portable/chipdoc-offline.tar.gz`（28 MB）。
> 不想构建的话，直接跳到「2. 传到 Windows 并校验」，或者用仓库根目录的 `chipdoc.cmd` 一键解压运行。

只有一个方案：**构建一个自带 Python 运行时和 PyMuPDF 的目录包**（Windows x64）。
目标机不需要装 Python、pip 或任何依赖：

- 仓库根目录的 `chipdoc.cmd`：首次自动解压，之后直接运行
- 或手动解压后运行 `chipdoc-offline\run.cmd`

```
dist/chipdoc-offline/            80 MB
├── runtime/python/              独立 CPython 3.12（Windows x64，可搬迁）
├── site-packages/               pymupdf 1.28.2 + chipdoc
├── plugins/                     插件（可选）
├── run.cmd                      run.cmd doctor 手册.pdf
└── README-OFFLINE.txt

dist/chipdoc-offline.tar.gz      29 MB
dist/chipdoc-offline.tar.gz.sha256
```

## 1. 构建（联网一次）

在任意能联网、且有 Python 3.11+ 的机器上（Windows 或 Linux 都行，这里是交叉构建）：

```bat
py -3 tools\build_offline.py
```

脚本做的四件事：

1. 下载 python-build-standalone 的 **CPython 3.12.7（Windows x64）**（约 38 MB，缓存在 `.cache\`）
2. 用 pip 交叉下载 **PyMuPDF 1.28.2 的 win_amd64 wheel** 并解包到 `site-packages`
3. 复制 `chipdoc\` 与 `plugins\`（如果有）
4. 裁掉测试套件、tkinter、pip、调试符号（.pdb），打 tar.gz 并生成 sha256

构建时需要的网络：**仅此一次**。之后重建可加 `--skip-download` 复用缓存。

## 2. 传到 Windows 并校验

```powershell
Get-FileHash .\chipdoc-offline.tar.gz -Algorithm SHA256
Get-Content .\chipdoc-offline.tar.gz.sha256
```

两个值一致即可（也可以用 `certutil -hashfile chipdoc-offline.tar.gz SHA256`）。

## 3. 解压

Windows 10 1803 及以上自带 tar：

```bat
tar -xzf chipdoc-offline.tar.gz
cd chipdoc-offline
```

## 4. 运行

```bat
run.cmd where                 :: 应显示：PDF 后端 pymupdf
run.cmd doctor 手册.pdf        :: 页数 / 文本层 / 需 OCR 页
run.cmd prepare 手册.pdf --work work --batch 4
run.cmd finish work\<slug> --out out
run.cmd run 手册.pdf --engine rules
```

`run.cmd` 里写死了包内解释器，并用 `-P` 禁止把当前目录当作模块路径：

```bat
set "PYTHONPATH=%DIR%site-packages"
"%DIR%runtime\python\python.exe" -P -m chipdoc %*
```

所以**既不会用到系统 Python，也不会误加载别的 chipdoc 版本**。

## 5. 验证清单

```bat
run.cmd --version          :: chipdoc 0.3.0
run.cmd where              :: 解释器在包内，后端 pymupdf
run.cmd doctor 手册.pdf     :: 能读出页数/文本层
run.cmd run 手册.pdf --engine rules --work work --out out
dir out\matrix.md out\docs
```

`where` 正常输出示例：

```
解释器      ...\chipdoc-offline\runtime\python\python.exe
包位置      ...\chipdoc-offline\site-packages\chipdoc
PDF 后端    pymupdf
```

## 6. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `run.cmd` 一闪而过 | 路径含空格/中文，或参数没加引号 | 把包放到纯英文路径（如 `D:\tools\chipdoc-offline`） |
| `拒绝访问` / 被杀软拦截 | 安全软件拦截未签名 exe | 加白名单，或改用「开发机 venv」方式 |
| `无法加载 python312.dll` | 解压不完整 | 重新解压，确认 `runtime\python\python312.dll` 存在 |
| `ModuleNotFoundError: pymupdf` | pymupdf 没进包 | 重新构建，确认日志里有 `下载并解包 PyMuPDF` |
| 中文乱码 | 终端代码页不是 UTF-8 | `chcp 65001` 后再运行 |
| `sha256` 不一致 | 传输损坏 | 重新传，别用文本方式拷二进制 |

## 7. 开发机怎么用（不是部署）

Windows 上日常开发：

```bat
py -3 -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\chipdoc doctor 手册.pdf
```

或直接双击仓库根目录的 `chipdoc.cmd`（自动找 `.venv\Scripts\python.exe`，找不到就用 `py -3`）。

这和离线包是两件事：`.venv` 依赖本机 Python，**不可搬迁**；离线包自带解释器，**可搬迁**。

## 8. 关于 Linux

已移除 Linux 部署方案（`chipdoc.sh`、linux 运行时、从 venv 复制等）。
构建脚本本身能在 Linux 上跑（用于交叉构建 Windows 包），但产物是 Windows x64。
