# Windows 拷贝即用（不需要 Python、pip、联网）

目标机装不了 pymupdf 时，用仓库里已经打好的 ZIP。

```
dist/chipdoc-offline.zip        29 MB
dist/chipdoc-offline.zip.sha256
```

## 三步

### 1. 复制并解压

把 `chipdoc-offline.zip` 拷到目标机（U 盘、共享盘都行），**右键 → 全部解压缩**。

Windows 10/11 资源管理器原生支持 ZIP，不需要 7-Zip、不需要 `tar` 命令。

> 建议解压到纯英文路径，例如 `D:\tools\chipdoc-offline`。
> 不要直接在 ZIP 里双击运行，必须先完整解压。

### 2. 双击 run.cmd 自检

双击 `chipdoc-offline\run.cmd`。不带参数时它会显示帮助并停住：

```
usage: chipdoc [-h] [-V] 命令 ...
...
用法示例：run.cmd doctor 手册.pdf
请按任意键继续. . .
```

看到这个就说明解释器和 PyMuPDF 都正常。

### 3. 命令行运行

打开 cmd，`cd` 到解压目录：

```bat
run.cmd where
run.cmd doctor 手册.pdf
run.cmd prepare 手册.pdf --work work --batch 4
run.cmd finish work\<slug> --out out
run.cmd run 手册.pdf --engine rules
```

`run.cmd where` 应显示：

```
解释器      ...\chipdoc-offline\runtime\python\python.exe
PyMuPDF     1.28.2
```

## 出问题时

**先运行 `diagnose.cmd`**（同目录下），它会打印 Python 版本、PyMuPDF 版本、解释器路径，并停住窗口。

| 现象 | 原因 | 处理 |
|---|---|---|
| 双击 run.cmd 一闪而过 | 出错但窗口关了 | 运行 `diagnose.cmd`，或在 cmd 里执行 run.cmd 看报错 |
| 解压后找不到 run.cmd | 只解压了内层目录 | 确认路径是 `...\chipdoc-offline\run.cmd` |
| SmartScreen / 杀软拦截 | `python.exe` 未签名 | 允许运行，或把目录加入白名单 |
| `无法启动此程序` / 缺 DLL | 32 位系统或解压不完整 | 本包只支持 **Windows x64**；重新完整解压 |
| 中文路径下异常 | 路径编码问题 | 移到 `D:\tools\chipdoc-offline` 这类纯英文路径 |
| `ModuleNotFoundError: pymupdf` | 包不完整 | 重新解压，确认 `site-packages\pymupdf\_mupdf.pyd` 存在 |

## 包内结构

```
chipdoc-offline/
├── run.cmd              入口
├── diagnose.cmd         自检
├── README-OFFLINE.txt
├── runtime/python/      CPython 3.12.7（Windows x64）
└── site-packages/       pymupdf 1.28.2 + chipdoc
```

## 自己重新构建（可选，需要联网一次）

只在需要更新版本时用：

```bat
py -3 tools\build_offline.py
```

脚本会下载 Windows 运行时和 PyMuPDF wheel，重新生成 `dist\chipdoc-offline.zip`。
构建机可以是 Windows，也可以是 Linux（交叉构建）。
