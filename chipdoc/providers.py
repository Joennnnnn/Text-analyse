"""可插拔的模型后端。

四种 provider，任何阶段都能单独指定：
- openai     任意 OpenAI 兼容端点（DeepSeek / Qwen / Kimi / vLLM / Ollama / LM Studio …）
- anthropic  Claude Messages API
- command    任意本地命令行（prompt 走 stdin，回答读 stdout），例如 `ollama run qwen2.5`
- external   完全不调用模型：把 prompt 写成文件，等外部工具/人工把结果写回来

配置优先级：命令行参数 > 配置文件 stages.<阶段> > 配置文件 default > 环境变量默认值
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ProviderError(RuntimeError):
    pass


@dataclass
class ProviderConfig:
    kind: str = "openai"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    command: list[str] = field(default_factory=list)
    timeout: float = 180.0
    max_retries: int = 3
    max_tokens: int = 4096
    json_mode: bool = False
    temperature: float = 0.0

    @property
    def label(self) -> str:
        if self.kind == "external":
            return "external(handoff)"
        if self.kind == "command":
            return "command:" + " ".join(self.command[:2])
        return f"{self.kind}:{self.model or 'default'}"


class Provider:
    external = False
    label = "base"

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError


# --------------------------------------------------------------------------
# OpenAI 兼容
# --------------------------------------------------------------------------

class OpenAICompat(Provider):
    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.url = cfg.base_url.rstrip("/") + "/chat/completions"
        self.label = cfg.label

    def complete(self, system: str, user: str) -> str:
        body: dict[str, Any] = {
            "model": self.cfg.model,
            "temperature": self.cfg.temperature,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        if self.cfg.json_mode:
            body["response_format"] = {"type": "json_object"}
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", **self.cfg.headers}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        raw = _http_json(self.url, data, headers, self.cfg.timeout, self.cfg.max_retries)
        try:
            return raw["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"OpenAI 兼容响应结构异常: {str(raw)[:200]}") from e


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------

class Anthropic(Provider):
    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        base = cfg.base_url.rstrip("/") or "https://api.anthropic.com"
        if base.endswith("/v1"):
            self.url = base + "/messages"
        else:
            self.url = base + "/v1/messages"
        self.label = cfg.label

    def complete(self, system: str, user: str) -> str:
        body = {
            "model": self.cfg.model,
            "max_tokens": self.cfg.max_tokens,
            "temperature": self.cfg.temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {"Content-Type": "application/json",
                   "anthropic-version": "2023-06-01", **self.cfg.headers}
        if self.cfg.api_key:
            headers["x-api-key"] = self.cfg.api_key
        raw = _http_json(self.url, json.dumps(body).encode(), headers,
                         self.cfg.timeout, self.cfg.max_retries)
        try:
            parts = [b.get("text", "") for b in raw["content"] if b.get("type") == "text"]
            return "".join(parts)
        except (KeyError, TypeError) as e:
            raise ProviderError(f"Anthropic 响应结构异常: {str(raw)[:200]}") from e


# --------------------------------------------------------------------------
# 任意命令行
# --------------------------------------------------------------------------

class CommandProvider(Provider):
    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.label = cfg.label

    def complete(self, system: str, user: str) -> str:
        if not self.cfg.command:
            raise ProviderError("command provider 需要 command 数组，例如 [\"ollama\",\"run\",\"qwen2.5\"]")
        prompt = f"{system}\n\n{user}"
        try:
            proc = subprocess.run(
                self.cfg.command, input=prompt, capture_output=True, text=True,
                timeout=self.cfg.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ProviderError(f"命令执行失败: {e}") from e
        if proc.returncode != 0:
            raise ProviderError(f"命令退出码 {proc.returncode}: {proc.stderr[:300]}")
        return proc.stdout


# --------------------------------------------------------------------------
# 外部交接
# --------------------------------------------------------------------------

class ExternalProvider(Provider):
    external = True

    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.label = "external(handoff)"

    def complete(self, system: str, user: str) -> str:
        raise ProviderError("external provider 不直接调用模型，请把结果写入 response/ 目录后运行 ingest")


# --------------------------------------------------------------------------
# 工厂 / 配置
# --------------------------------------------------------------------------

def make_provider(cfg: ProviderConfig) -> Provider:
    if cfg.kind == "openai":
        return OpenAICompat(cfg)
    if cfg.kind == "anthropic":
        return Anthropic(cfg)
    if cfg.kind == "command":
        return CommandProvider(cfg)
    if cfg.kind == "external":
        return ExternalProvider(cfg)
    raise ProviderError(f"未知 provider: {cfg.kind}（可选 openai / anthropic / command / external）")


def load_config(path: str | None) -> dict:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise ProviderError(f"配置文件不存在: {path}")
    text = p.read_text(encoding="utf-8")
    if p.suffix in (".toml",):
        import tomllib
        return tomllib.loads(text)
    return json.loads(text)


def resolve_config(stage: str, config: dict, cli: dict) -> ProviderConfig:
    merged = {**(config.get("default") or {}), **((config.get("stages") or {}).get(stage) or {})}
    for k, v in (cli or {}).items():
        if v not in (None, "", []):
            merged[k] = v
    cfg = ProviderConfig(
        kind=str(merged.get("provider", "openai")).lower(),
        model=str(merged.get("model", "")),
        base_url=str(merged.get("base_url", "")),
        api_key=str(merged.get("api_key", "")),
        api_key_env=str(merged.get("api_key_env", "")),
        headers=dict(merged.get("headers") or {}),
        command=list(merged.get("command") or []),
        timeout=float(merged.get("timeout", 180)),
        max_retries=int(merged.get("max_retries", 3)),
        max_tokens=int(merged.get("max_tokens", 4096)),
        json_mode=bool(merged.get("json_mode", False)),
        temperature=float(merged.get("temperature", 0.0)),
    )
    if not cfg.api_key and cfg.api_key_env:
        cfg.api_key = os.environ.get(cfg.api_key_env, "")
    if not cfg.api_key and cfg.kind == "openai":
        cfg.api_key = (os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY") or "")
    if not cfg.api_key and cfg.kind == "anthropic":
        cfg.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not cfg.base_url:
        cfg.base_url = {
            "openai": os.environ.get("OPENAI_BASE_URL") or "https://api.deepseek.com/v1",
            "anthropic": "https://api.anthropic.com",
        }.get(cfg.kind, "")
    if cfg.kind == "openai" and not cfg.model:
        cfg.model = os.environ.get("CHIPDOC_MODEL", "") or "deepseek-chat"
    return cfg


def _http_json(url: str, data: bytes, headers: dict, timeout: float, retries: int) -> dict:
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:300]
            last = ProviderError(f"HTTP {e.code}: {detail}")
            if e.code in (400, 401, 403, 404):
                raise last
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = ProviderError(f"{type(e).__name__}: {e}")
        time.sleep(min(2 ** attempt * 0.5, 8))
    raise last or ProviderError("unknown provider failure")
