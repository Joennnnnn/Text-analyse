"""构建 Windows 离线部署包：自带 Python 运行时 + PyMuPDF + chipdoc。

    python tools/build_offline.py

产物 dist/chipdoc-offline/（及同名 .tar.gz）：
    runtime/python/python.exe    独立 CPython 3.12（Windows x64，可搬迁）
    site-packages/               pymupdf + chipdoc
    plugins/                     插件（可选）
    run.cmd                      run.cmd doctor 手册.pdf

构建机需要联网（下载运行时与 wheel 各一次）；目标机（Windows）不需要任何依赖。
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
CACHE = ROOT / ".cache"

RUNTIME_URL = ("https://github.com/astral-sh/python-build-standalone/releases/download/"
               "20241016/cpython-3.12.7%2B20241016-x86_64-pc-windows-msvc-install_only.tar.gz")
TARGET_PLATFORM = "win_amd64"
TARGET_PY = "3.12"
PYMUPDF = "1.28.2"

TRIM = [
    "Lib/test", "Lib/idlelib", "Lib/turtledemo", "Lib/ensurepip", "Lib/lib2to3",
    "Lib/tkinter", "Lib/venv", "tcl", "Lib/site-packages/pip", "Lib/site-packages/setuptools",
    "Lib/site-packages/pkg_resources", "Lib/site-packages/pip-*.dist-info",
    "include", "libs", "Scripts", "python.pdb", "python3.pdb",
]


def _log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def _download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 1_000_000:
        _log(f"使用缓存 {dest}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    _log(f"下载 {url}")
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(url, timeout=180) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    tmp.replace(dest)
    return dest


def _trim_runtime(runtime: Path) -> None:
    for rel in TRIM:
        for p in runtime.glob(rel):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
    for dll in list((runtime / "DLLs").glob("tcl*")) + list((runtime / "DLLs").glob("tk*")):
        dll.unlink(missing_ok=True)
    for pdb in runtime.rglob("*.pdb"):          # 调试符号，占几十 MB
        pdb.unlink(missing_ok=True)
    for pyc in runtime.rglob("__pycache__"):
        shutil.rmtree(pyc, ignore_errors=True)


def _install_site(site: Path) -> None:
    site.mkdir(parents=True, exist_ok=True)
    _log(f"下载并解包 PyMuPDF {PYMUPDF}（{TARGET_PLATFORM}）")
    subprocess.run([
        sys.executable, "-m", "pip", "install", "-q", "--no-cache-dir",
        "--target", str(site),
        "--only-binary=:all:", "--platform", TARGET_PLATFORM,
        "--python-version", TARGET_PY, "--implementation", "cp", "--abi", f"cp{TARGET_PY.replace('.', '')}",
        f"pymupdf=={PYMUPDF}",
    ], check=True)
    for pyc in site.rglob("__pycache__"):
        shutil.rmtree(pyc, ignore_errors=True)
    shutil.rmtree(site / "bin", ignore_errors=True)
    shutil.copytree(ROOT / "chipdoc", site / "chipdoc",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _check_structure(out: Path) -> None:
    runtime = out / "runtime" / "python"
    checks = {
        "python.exe": runtime / "python.exe",
        "python312.dll": runtime / "python312.dll",
        "pymupdf/_mupdf.pyd": out / "site-packages" / "pymupdf" / "_mupdf.pyd",
        "run.cmd": out / "run.cmd",
    }
    missing = [name for name, p in checks.items() if not p.exists()]
    if missing:
        raise SystemExit("产物结构不完整：" + ", ".join(missing))
    wheel = next((out / "site-packages").glob("pymupdf-*.dist-info/WHEEL"), None)
    if wheel and TARGET_PLATFORM not in wheel.read_text(encoding="utf-8", errors="ignore"):
        raise SystemExit("PyMuPDF wheel 平台不是 " + TARGET_PLATFORM)


def build(out: Path = DIST / "chipdoc-offline", skip_download: bool = False) -> Path:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    tarball = CACHE / "windows-runtime.tar.gz"
    if not skip_download:
        _download(RUNTIME_URL, tarball)
    if not tarball.exists():
        raise SystemExit("没有运行时缓存，且指定了 --skip-download")

    _log("解压 Windows 运行时")
    (out / "runtime").mkdir()
    with tarfile.open(tarball) as tf:
        tf.extractall(out / "runtime")
    runtime = out / "runtime" / "python"
    if not (runtime / "python.exe").exists():
        raise SystemExit("运行时结构异常：找不到 python/python.exe")

    _trim_runtime(runtime)
    _install_site(out / "site-packages")

    plugins = ROOT / "plugins"
    if plugins.is_dir():
        shutil.copytree(plugins, out / "plugins", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    (out / "run.cmd").write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "set \"DIR=%~dp0\"\r\n"
        "set \"PYTHONPATH=%DIR%site-packages\"\r\n"
        "if exist \"%DIR%plugins\" set \"CHIPDOC_PLUGINS=%DIR%plugins\"\r\n"
        "\"%DIR%runtime\\python\\python.exe\" -P -m chipdoc %*\r\n",
        encoding="utf-8")

    (out / "README-OFFLINE.txt").write_text(
        "chipdoc 离线包（Windows x64，自带 Python 运行时，无需安装任何依赖）\r\n"
        "============================================================\r\n\r\n"
        "  run.cmd doctor 手册.pdf\r\n"
        "  run.cmd prepare 手册.pdf --work work --batch 4\r\n"
        "  run.cmd finish work\\<slug> --out out\r\n\r\n"
        f"运行时：CPython 3.12.7（Windows x64）\r\n"
        f"PyMuPDF：{PYMUPDF}\r\n\r\n"
        "验证：run.cmd where   应显示 PDF 后端 pymupdf\r\n",
        encoding="utf-8")

    _check_structure(out)

    size = sum(p.lstat().st_size for p in out.rglob("*") if p.is_file() and not p.is_symlink())
    _log(f"目录 {out}  ({size / 1e6:.0f} MB)")

    tar = out.with_suffix(".tar.gz")
    with tarfile.open(tar, "w:gz") as tf:
        tf.add(out, arcname=out.name)
    digest = hashlib.sha256(tar.read_bytes()).hexdigest()
    (tar.parent / f"{tar.name}.sha256").write_text(f"{digest}  {tar.name}\n", encoding="utf-8")
    _log(f"压缩 {tar}  ({tar.stat().st_size / 1e6:.0f} MB)  sha256={digest[:16]}…")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DIST / "chipdoc-offline"))
    ap.add_argument("--skip-download", action="store_true", help="复用 .cache 里的运行时")
    a = ap.parse_args()
    build(Path(a.out), a.skip_download)
