"""极简插件机制：离线也能扩展，不需要 pip。

把 `xxx.py` 放到以下任一位置即可自动加载（按顺序）：
1. 环境变量 `CHIPDOC_PLUGINS` 里的目录（os.pathsep 分隔）
2. 当前目录下的 `plugins/`
3. `~/.chipdoc/plugins/`

插件可选导出四个钩子（都是普通函数）：

    def fields(kind):            # kind = datasheet / trm / generic
        return [{"id": "pins", "label": "引脚数", "hint": "封装引脚数"}]

    def on_facts(facts, index):  # facts 已通过页码校验
        return facts

    def on_profile(profile):     # profile 是 dict，可直接改
        return profile

    def markdown(profile):       # 追加到 summary.md 末尾
        return "## 备注\\n..."

单个插件抛异常不会影响主流程，只会打印警告。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import traceback
from pathlib import Path

_loaded: list | None = None


def _dirs() -> list[Path]:
    out = []
    env = os.environ.get("CHIPDOC_PLUGINS", "")
    for part in env.split(os.pathsep):
        if part.strip():
            out.append(Path(part).expanduser())
    out.append(Path.cwd() / "plugins")
    out.append(Path.home() / ".chipdoc" / "plugins")
    return [p for p in out if p.is_dir()]


def load() -> list:
    global _loaded
    if _loaded is not None:
        return _loaded
    mods = []
    seen = set()
    for d in _dirs():
        for py in sorted(d.glob("*.py")):
            if py.name.startswith("_"):
                continue
            key = str(py.resolve())
            if key in seen:
                continue
            seen.add(key)
            try:
                spec = importlib.util.spec_from_file_location(f"chipdoc_plugin_{py.stem}", py)
                mod = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = mod
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
                mods.append(mod)
            except Exception as e:  # 插件坏了不影响主流程
                print(f"[plugin] 加载失败 {py}: {e}", file=sys.stderr)
    _loaded = mods
    return mods


def _call(name: str, *args):
    results = []
    for mod in load():
        fn = getattr(mod, name, None)
        if not callable(fn):
            continue
        try:
            results.append(fn(*args))
        except Exception:
            print(f"[plugin] {mod.__name__}.{name} 失败：", file=sys.stderr)
            traceback.print_exc()
    return results


def fields(kind: str):
    from .schema import FieldSpec
    extra = []
    for res in _call("fields", kind):
        for d in res or []:
            try:
                extra.append(FieldSpec(id=str(d["id"]), label=str(d.get("label", d["id"])),
                                       hint=str(d.get("hint", "")), unit=str(d.get("unit", ""))))
            except Exception:
                continue
    return extra


def apply_facts(facts, index):
    current = facts
    for res in _call("on_facts", current, index):
        if isinstance(res, list):
            current = res
    return current


def apply_profile(profile: dict) -> dict:
    for res in _call("on_profile", profile):
        if isinstance(res, dict):
            profile = res
    return profile


def extra_markdown(profile) -> str:
    parts = [m for m in _call("markdown", profile) if isinstance(m, str) and m.strip()]
    return "\n\n".join(parts)
