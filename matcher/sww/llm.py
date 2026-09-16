"""Server-only DeepSeek client plus one cached, validated, verified call path.

Two things used to be tangled together and duplicated. The HTTP client knew
about JSON repair retries; the ranker separately knew about schema validation,
evidence verification, retry-with-feedback and an on-disk cache — and
implemented that loop twice, once for assessments and once for pairwise
comparisons, with different cache layouts. `Extractor.call` is that loop, once:

    cache hit -> re-validate (never trust a cache blindly) -> return
    cache miss -> call -> validate -> verify -> on failure, one retry that
                  tells the model exactly what failed -> store -> return

Errors never carry a request or response body, so a posting's text and the
API key cannot leak into a message shown in the browser.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from . import config

Model = TypeVar("Model", bound=BaseModel)


class DeepSeekError(Exception):
    """Safe, user-facing error; contains no request or response body."""

    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


def load_api_key(root: Path = config.ROOT) -> str:
    value = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not value:
        path = Path(os.environ.get("DEEPSEEK_API_KEY_FILE", str(root / "deepseek_api.txt")))
        try:
            if path.stat().st_size > 16384:
                raise DeepSeekError("DeepSeek 密钥文件过大，请只保留 API key。")
            value = path.read_text(encoding="utf-8-sig").strip()
        except FileNotFoundError:
            raise DeepSeekError("未配置 DeepSeek key：请设置 DEEPSEEK_API_KEY 或项目根目录 deepseek_api.txt。") from None
        except (OSError, UnicodeError):
            raise DeepSeekError("无法读取 DeepSeek 密钥文件。") from None
    matches = re.findall(r"(?<![\w-])sk-[A-Za-z0-9_-]{16,}(?![\w-])", value)
    if len(set(matches)) != 1:
        raise DeepSeekError("DeepSeek 密钥格式无效；文件应包含一个 sk- 开头的 API key。")
    return matches[0]


def model_name() -> str:
    try:
        return config.model_name()
    except ValueError as exc:
        raise DeepSeekError("DEEPSEEK_MODEL 格式无效。") from exc


class DeepSeekClient:
    def __init__(self, key: str, model: str, transport=None, max_tokens: int = 6000):
        self.model = model
        self.max_tokens = max_tokens
        self._http = httpx.AsyncClient(
            base_url=config.API_ORIGIN, headers={"Authorization": "Bearer " + key},
            timeout=httpx.Timeout(150, connect=15), follow_redirects=False, transport=transport,
        )
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "requests": 0}

    async def close(self):
        await self._http.aclose()

    async def json(self, system: str, payload: dict) -> dict:
        request = {"model": self.model, "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ], "response_format": {"type": "json_object"}, "max_tokens": self.max_tokens,
            "thinking": {"type": "disabled"}, "temperature": 0}
        for attempt in range(3):
            self.usage["requests"] += 1
            try:
                response = await self._http.post("/chat/completions", json=request)
            except httpx.TransportError:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise DeepSeekError("DeepSeek 请求超时或连接失败，稍后重试。") from None
            status = response.status_code
            if status == 429 or status >= 500:
                if attempt < 2:
                    retry = response.headers.get("retry-after", "")
                    delay = min(float(retry), 30) if retry.isdigit() else 2 ** attempt
                    await asyncio.sleep(delay)
                    continue
                raise DeepSeekError("DeepSeek 限流或服务暂不可用，已停止本次评估；可稍后复用缓存重试。", fatal=True)
            if status != 200:
                message = {401: "DeepSeek key 无效。", 402: "DeepSeek 账户余额不足。",
                           403: "DeepSeek 拒绝访问。", 400: "DeepSeek 请求或模型配置不受支持。"}
                raise DeepSeekError(message.get(status, f"DeepSeek 请求失败（HTTP {status}）。"),
                                    fatal=status in {400, 401, 402, 403, 404})
            try:
                body = response.json()
                usage = body.get("usage", {})
                for name in ("prompt_tokens", "completion_tokens"):
                    self.usage[name] += max(0, int(usage.get(name, 0)))
                choice = body["choices"][0]
                if choice.get("finish_reason") != "stop":
                    raise ValueError("truncated")
                result = json.loads(choice["message"]["content"])
                if not isinstance(result, dict):
                    raise ValueError("not an object")
                return result
            except (KeyError, IndexError, ValueError, TypeError):
                if attempt < 2:
                    request["messages"][0]["content"] = system + "\nReturn one complete concise JSON object; no markdown."
                    continue
                raise DeepSeekError("DeepSeek 返回了空、截断或无效 JSON；本职位未完成评估。") from None
        raise DeepSeekError("DeepSeek 请求未完成。")


class Extractor:
    """Cached, schema-validated, evidence-verified structured extraction."""

    def __init__(self, client: DeepSeekClient, store, version: str):
        self.client = client
        self.store = store
        self.version = version
        self.hits = 0
        self.misses = 0

    def key(self, kind: str, system: str, payload: Any) -> str:
        material = json.dumps([self.version, self.client.model, kind, system, payload],
                              sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    async def call(self, *, kind: str, system: str, payload: dict, schema: type[Model],
                   verify: Optional[Callable[[Model], None]] = None,
                   retry_instruction: str = "",
                   cache_key: Optional[str] = None) -> tuple[Model, bool]:
        """Return (validated result, whether it came from cache).

        `verify` raises `DeepSeekError` when the model's own citations do not
        check out against the source text. A cached entry runs through the same
        verification, so a cache written by an older, laxer rule cannot smuggle
        an unverifiable claim into a score.

        `cache_key` overrides the default key for entries that should be shared
        more widely than "this prompt, this model" — a posting's requirements,
        for instance, which every stage and every model can reuse.
        """
        def accept(raw: Any) -> Model:
            result = schema.model_validate(raw)
            if verify is not None:
                verify(result)
            return result

        cache_key = cache_key or self.key(kind, system, payload)
        cached = await self.store.acached(cache_key)
        if cached is not None:
            try:
                result = accept(cached)
                self.hits += 1
                return result, True
            except (ValidationError, DeepSeekError):
                pass  # Stale or no longer verifiable: re-request it.

        request = payload
        for attempt in range(2):
            raw = await self.client.json(system, request)
            try:
                result = accept(raw)
            except (ValidationError, DeepSeekError) as exc:
                feedback = str(exc) if isinstance(exc, DeepSeekError) else "JSON structure does not match the required schema."
                if attempt:
                    # Keep the specific reason: "第 2 项引用无法核对" tells the user
                    # what to check, "校验未通过" tells them nothing.
                    raise DeepSeekError("模型的结构或证据校验未通过：" + feedback) from None
                request = {**payload, "validation_feedback": feedback,
                           "retry_instruction": retry_instruction or
                           "Return the complete JSON schema with exact source quotes; do not omit items to make the result look better."}
                continue
            await self.store.aput_cached(cache_key, kind, raw)
            self.misses += 1
            return result, False
        raise DeepSeekError("模型未返回可用结果。")
