# 离线构建与部署

目标：在**没有网络、不能 pip、可能只有 Python** 的机器上把 chipdoc 跑起来。

分两个角色：

- **构建机**：有源码、有网（可选）、能装包
- **目标机**：离线，可能只有一个 Python 或一个现成 `.venv`

## 先选方案

| 方案 | 目标机需要什么 | 体积 | 适合 |
|---|---|---|---|
| A. 装进已有 venv | 已有 `.venv` + Python | 几百 KB | 目标机已经有环境，最推荐 |
| B. 打包整个 venv | 同架构/同 glibc 的机器 | 4 MB / 27 MB / 280 MB | 要连环境一起搬走 |
| C. 单文件 zipapp | 任意 Python 3.11+ | 48 KB | 什么包都不能装，但能跑 python |

> `.venv` 不能跨系统复制：Linux 的 venv 里是 `bin/`，Windows 是 `Scripts/`，二进制也不通用。跨系统请用方案 C，或在新系统上重新建 venv 走方案 A。

## 方案 A：装进已有的 venv（推荐）

**构建机**（有源码的目录）：

```bash
cd /path/to/Text-analyse
python3 tools/install_into_venv.py /目标机/.venv
```

只做三件事，不联网、不调 pip：

1. 把 `chipdoc/` 复制到该 venv 的 `site-packages/`
2. 复制 `plugins/`（如果存在）
3. 生成启动器：Linux/macOS 是 `bin/chipdoc`，Windows 是 `Scripts\chipdoc.cmd`

**目标机**：

```bash
# Linux / macOS
/目标机/.venv/bin/chipdoc where
/目标机/.venv/bin/chipdoc doctor 手册.pdf

# Windows
C:\目标机\.venv\Scripts\chipdoc.cmd where
```

如果目标机连 pip 都没有，venv 可以这样建：

```bash
python3 -m venv --without-pip .venv
python3 tools/install_into_venv.py .venv
.venv/bin/python -m chipdoc --version
```

**验证成功的标志**：

```bash
.venv/bin/chipdoc where
# chipdoc     0.2.0
# 包位置      .../.venv/lib/python3.x/site-packages/chipdoc
# 前缀        .../.venv  (venv)
# PDF 后端    pure-python 或 pymupdf
```

## 方案 B：打包整个 venv

### B1. 在构建机生成

```bash
python3 tools/build_bundle.py --profile lean    # 几 MB，纯 Python 解析
python3 tools/build_bundle.py --profile pdf     # 约 27 MB，带 PyMuPDF（构建时联网）
python3 tools/build_bundle.py --profile full    # 约 280 MB，复制当前 .venv
```

| profile | 内容 | 构建时联网 |
|---|---|---|
| `lean` | 仅 chipdoc + 内置解析器 | 不需要 |
| `pdf` | 加 PyMuPDF 1.x（更快、支持表格） | 需要，安装一次 |
| `full` | 完整复制源 venv（含所有依赖） | 不需要 |

产物：

```
dist/
├── chipdoc-bundle/                 # 目录，可直接拷
├── chipdoc-bundle.tar.gz           # 压缩包
└── chipdoc-bundle.tar.gz.sha256    # 校验值
```

### B2. 传到目标机并校验

```bash
sha256sum -c chipdoc-bundle.tar.gz.sha256
tar xzf chipdoc-bundle.tar.gz
```

Windows 没有 `sha256sum`，用 PowerShell：

```powershell
Get-FileHash .\chipdoc-bundle.tar.gz -Algorithm SHA256
```

### B3. 运行

```bash
cd chipdoc-bundle
./run.sh doctor 手册.pdf            # Linux / macOS
run.cmd doctor 手册.pdf             # Windows
```

`run.sh` / `run.cmd` 只是调用包内的 `venv`，不依赖系统的 python 命令名。

**能搬迁的前提**：

- 目标机与构建机**同架构**（都是 x86_64 或都是 arm64）
- glibc 版本不低于构建机（CentOS 7 这类老系统请用 `lean`）
- `venv/pyvenv.cfg` 里的 `home` 路径在目标机存在（否则用方案 A 重建 venv）

## 方案 C：单文件 zipapp

```bash
python3 tools/build_pyz.py          # dist/chipdoc.pyz（约 48 KB）
python3 dist/chipdoc.pyz doctor 手册.pdf
```

目标机只要有 `python3`（3.11+）即可，不需要 venv、不需要任何包。`pymupdf` 不存在时自动用内置纯 Python 解析器。

## 构建机一次性准备（可选）

想在构建机上正常开发/测试：

```bash
python3 -m venv .venv
.venv/bin/pip install -e .           # 有网
# 或离线：python3 tools/install_into_venv.py .venv
.venv/bin/chipdoc --version
```

## 验证清单

目标机上按顺序跑：

```bash
# 1. 能启动
./run.sh --version            # 或 .venv/bin/chipdoc --version
# 2. 看装在哪、用什么后端
./run.sh where
# 3. 能读 PDF
./run.sh doctor 手册.pdf       # 关注：页数、文本层、需 OCR 页
# 4. 能跑完整流程（不需要模型）
./run.sh run 手册.pdf --engine rules --work work --out out
# 5. 有产物
ls out/matrix.md out/docs/*/summary.md
```

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `.venv` 里没有 `bin` | Windows venv 用 `Scripts` | 用 `.venv\Scripts\python.exe` |
| `.venv` 里既没 `bin` 也没 `Scripts` | venv 创建失败/不完整 | 删掉重建：`python -m venv .venv` |
| `pyvenv.cfg` 的 `home` 路径不存在 | venv 来自别的机器 | 在目标机重建 venv，走方案 A |
| `ModuleNotFoundError: chipdoc` | 包没装进这个 venv | `install_into_venv.py .venv`，或设 `PYTHONPATH` |
| `ModuleNotFoundError: pymupdf` | 没装 PyMuPDF | 正常，会自动用纯解析器；要表格再加装 |
| `xref 流不受支持` | 纯解析器不支持该 PDF | 装 PyMuPDF，或用 `--backend pymupdf` |
| `Permission denied` | 启动器没有执行权限 | `chmod +x .venv/bin/chipdoc` |
| 打包后运行报 `No module named encodings` | venv 跨机器/跨版本搬坏了 | 改用 `lean`，或目标机重建 venv |

## 命令速查（目标机）

```bash
chipdoc where                       # 我在哪、用什么后端
chipdoc doctor 手册.pdf              # 能不能分析
chipdoc prepare 手册.pdf --work work --batch 4
chipdoc finish work/<slug> --out out
chipdoc run 手册.pdf --engine rules  # 不接模型跑一遍
```
