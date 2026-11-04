"""小工具：并发 map、容错 JSON 解析。"""
from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")
R = TypeVar("R")


class ParseError(ValueError):
    pass


def parse_json(raw: str) -> dict:
    """容忍 ```json 围栏、前后解释文字。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start < 0:
        raise ParseError(f"未返回 JSON: {raw[:200]}")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as e:
        raise ParseError(f"JSON 解析失败: {e}: {raw[:200]}") from e
    if not isinstance(obj, dict):
        raise ParseError(f"JSON 顶层不是对象: {raw[:200]}")
    return obj


def run_parallel(items: Iterable[T], fn: Callable[[T], R], jobs: int = 6,
                 on_done: Callable[[int, int], None] | None = None) -> list[R]:
    items = list(items)
    results: list[R] = [None] * len(items)  # type: ignore[list-item]
    done = 0
    lock = threading.Lock()

    def work(i: int, item: T) -> None:
        nonlocal done
        results[i] = fn(item)
        with lock:
            done += 1
            if on_done:
                on_done(done, len(items))

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = [pool.submit(work, i, it) for i, it in enumerate(items)]
        for f in futures:
            f.result()
    return results
