"""把 chipdoc + 一个可搬迁的 venv 打成离线包。

    python tools/build_bundle.py --profile lean    # 几 MB，纯 Python 解析
    python tools/build_bundle.py --profile pdf     # 约 60MB，带轻量 PyMuPDF（构建时联网）
    python tools/build_bundle.py --profile full    # 约 600MB，完整复制当前 venv

产物：
    dist/chipdoc-bundle/        目录（可直接拷走）
    dist/chipdoc-bundle.tar.gz  压缩包

目标机器：解压后运行 ./run.sh doctor 手册.pdf（或 venv/bin/chipdoc）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from install_into_venv import install  # noqa: E402

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "pip", "setuptools", "pkg_resources")


def _run(cmd, **kw):
    print("+", " ".join(map(str, cmd)))
    subprocess.run(cmd, check=True, **kw)


def _clean_editable(site: Path) -> None:
    for p in site.glob("__editable__*chipdoc*"):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
    for p in site.glob("*.pth"):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "Text-analyse" in text or "chipdoc" in text:
            p.unlink(missing_ok=True)


def build(profile: str = "lean", out_dir: str = "dist",
          source_venv: str = ".venv") -> Path:
    out = (ROOT / out_dir).resolve()
    bundle = out / "chipdoc-bundle"
    shutil.rmtree(bundle, ignore_errors=True)
    bundle.mkdir(parents=True)

    venv = bundle / "venv"
    if profile == "full":
        src = (ROOT / source_venv).resolve()
        if not (src / "bin" / "python").exists():
            raise SystemExit(f"找不到源 venv: {src}（先 pip install -e .）")
        shutil.copytree(src, venv, symlinks=False, ignore=IGNORE)
        for pyc in venv.rglob("__pycache__"):
            shutil.rmtree(pyc, ignore_errors=True)
        site = Path(subprocess.check_output(
            [str(venv / "bin" / "python"), "-c",
             "import sysconfig;print(sysconfig.get_paths()['purelib'])"], text=True).strip())
        _clean_editable(site)
        shutil.rmtree(site / "chipdoc", ignore_errors=True)
    else:
        _run([sys.executable, "-m", "venv", str(venv)])
        if profile == "pdf":
            _run([str(venv / "bin" / "pip"), "install", "-q", "pymupdf<2"])
        site = Path(subprocess.check_output(
            [str(venv / "bin" / "python"), "-c",
             "import sysconfig;print(sysconfig.get_paths()['purelib'])"], text=True).strip())

    install(venv, ROOT)

    # 离线包不需要 pip；删掉能省十几 MB
    for pat in ("pip", "pip-*.dist-info", "setuptools", "setuptools-*.dist-info"):
        for item in site.glob(pat):
            shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)

    (bundle / "run.sh").write_text(
        '#!/bin/sh\n'
        'DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'exec "$DIR/venv/bin/python" -m chipdoc "$@"\n', encoding="utf-8")
    (bundle / "run.sh").chmod(0o755)

    (bundle / "README-OFFLINE.txt").write_text(
        "chipdoc 离线包\n"
        "==============\n\n"
        "运行：\n"
        "  ./run.sh doctor 手册.pdf\n"
        "  ./run.sh prepare 手册.pdf --work work --batch 4\n"
        "  ./run.sh finish work/<slug> --out out\n"
        "  ./run.sh run 手册.pdf --engine rules\n\n"
        "也可以直接用 venv 里的 python：\n"
        "  venv/bin/python -m chipdoc --help\n\n"
        f"profile: {profile}\n"
        "要求：目标机器与构建机器同架构、同 glibc；venv 依赖构建机的 Python 安装路径\n"
        "（pyvenv.cfg 里的 home）。跨大版本系统请改用源码 + 纯 Python 后端。\n",
        encoding="utf-8")

    size = 0
    for p in bundle.rglob("*"):
        if p.is_file() and not p.is_symlink():
            size += p.lstat().st_size
    print(f"目录：{bundle}  ({size/1e6:.1f} MB)")

    tar = out / "chipdoc-bundle.tar.gz"
    with tarfile.open(tar, "w:gz") as tf:
        tf.add(bundle, arcname="chipdoc-bundle")
    print(f"压缩：{tar}  ({tar.stat().st_size/1e6:.1f} MB)")
    return tar


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=["lean", "pdf", "full"], default="lean")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--source-venv", default=".venv")
    a = ap.parse_args()
    build(a.profile, a.out, a.source_venv)
