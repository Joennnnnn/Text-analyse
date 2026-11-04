"""把 chipdoc 塞进已有的 venv —— 不需要 pip、不需要联网。

用法：
    python tools/install_into_venv.py /path/to/.venv
    /path/to/.venv/bin/chipdoc doctor 手册.pdf

原理：把 chipdoc/ 整包复制到 venv 的 site-packages，并生成 bin/chipdoc 启动器。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")


def _venv_python(venv: Path) -> Path:
    for name in ("bin/python", "bin/python3", "Scripts/python.exe", "Scripts/python3.exe"):
        p = venv / name
        if p.exists():
            return p
    raise SystemExit(
        f"{venv} 里既没有 bin/python 也没有 Scripts/python.exe。\n"
        f"目录内容：{sorted(x.name for x in venv.iterdir()) if venv.exists() else '路径不存在'}\n"
        "请先运行：python -m venv " + str(venv))


def _scripts_dir(venv: Path) -> Path:
    """POSIX 是 bin/，Windows 是 Scripts/。"""
    scripts = venv / "Scripts"
    return scripts if scripts.exists() else venv / "bin"


def _site_packages(python: Path) -> Path:
    out = subprocess.check_output(
        [str(python), "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
        text=True).strip()
    return Path(out)


def install(venv: str | Path, source: str | Path | None = None,
            with_plugins: bool = True) -> Path:
    venv = Path(venv).resolve()
    python = _venv_python(venv)
    site = _site_packages(python)
    src = Path(source or ROOT) / "chipdoc"
    dest = site / "chipdoc"
    shutil.copytree(src, dest, ignore=IGNORE, dirs_exist_ok=True)

    if with_plugins:
        plugins = Path(source or ROOT) / "plugins"
        if plugins.is_dir():
            shutil.copytree(plugins, venv / "plugins", ignore=IGNORE, dirs_exist_ok=True)

    scripts = _scripts_dir(venv)
    if scripts.name == "Scripts":          # Windows：生成 chipdoc.cmd
        launcher = scripts / "chipdoc.cmd"
        launcher.write_text(
            "@echo off\r\n"
            '"%~dp0python.exe" -m chipdoc %*\r\n', encoding="utf-8")
    else:                                   # POSIX：生成 bin/chipdoc
        launcher = scripts / "chipdoc"
        launcher.write_text(
            f"#!{python}\n"
            "import sys\n"
            "from chipdoc.cli import main\n"
            "sys.exit(main())\n",
            encoding="utf-8")
        launcher.chmod(0o755)
    return dest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把 chipdoc 安装进已有 venv（离线可用）")
    ap.add_argument("venv", help="目标 venv 目录，例如 .venv")
    ap.add_argument("--source", default=None, help="源码根目录（默认本仓库）")
    ap.add_argument("--no-plugins", action="store_true", help="不复制 plugins/ 目录")
    args = ap.parse_args(argv)
    dest = install(args.venv, args.source, with_plugins=not args.no_plugins)
    print(f"已安装到 {dest}")
    venv = Path(args.venv).resolve()
    print(f"启动器：{_scripts_dir(venv)}/{'chipdoc.cmd' if _scripts_dir(venv).name == 'Scripts' else 'chipdoc'}")
    print(f"验证：{_scripts_dir(venv)}/chipdoc doctor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
