"""Small server-only DeepSeek client. Never expose upstream bodies or secrets."""
import asyncio
import json
import os
import re
from pathlib import Path

import httpx

API_ORIGIN = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"


class DeepSeekError(Exception):
    """Safe, user-facing error; contains no request or response body."""

    def __init__(self, message, fatal=False):
        super().__init__(message)
        self.fatal = fatal


def load_api_key(root: Path) -> str:
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
    model = os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", model):
        raise DeepSeekError("DEEPSEEK_MODEL 格式无效。")
    return model


class DeepSeekClient:
    def __init__(self, key: str, model: str, transport=None):
        self.model = model
        self._http = httpx.AsyncClient(
            base_url=API_ORIGIN, headers={"Authorization": "Bearer " + key},
            timeout=httpx.Timeout(150, connect=15), follow_redirects=False,
            transport=transport,
        )
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "requests": 0}

    async def close(self):
        await self._http.aclose()

    async def json(self, system: str, payload: dict) -> dict:
        request = {"model": self.model, "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ], "response_format": {"type": "json_object"}, "max_tokens": 6000,
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
                raise DeepSeekError(message.get(status, f"DeepSeek 请求失败（HTTP {status}）。"), fatal=status in {400, 401, 402, 403, 404})
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
