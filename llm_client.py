# -*- coding: utf-8 -*-
"""OpenAI 兼容接口的流式客户端，只依赖 requests。

支持：DeepSeek / 任意 OpenAI 兼容网关 / 本地 Magpie 网关。
未配置 Key 时上层会退化为"把完整提示词复制给豆包"。
"""
from __future__ import annotations

import json
import os
import time
import uuid


class LLMError(RuntimeError):
    pass


MOCK_TEXT = """【一句判断】她现在在试探你的态度，不是真想分手。

【可直接发送】
这几天是我状态不好，冷着你是我处理方式的问题，不是你做错了。给我半天把手上事收个尾，今晚我给你打个电话，把话说清楚。

【为什么】
先认"冷处理"这个行为，不认"你错了"这个帽子，对方最在意的是你有没有把她当回事。给具体时间点比说"我会改"更可信，也留了被拒绝的余地。

【观察点】
她回"行/可以/我等你"算接住了；只回"随便"或继续追问旧账，就不要在电话里翻三件事，只谈眼前这一件。
"""


class LLMClient:
    def __init__(self, api: dict):
        self.base_url = (api.get("base_url") or "").rstrip("/")
        self.api_key = api.get("api_key") or os.environ.get("GOUTOU_API_KEY", "")
        self.model = api.get("model") or "deepseek-chat"
        self.temperature = float(api.get("temperature", 0.7))
        self.max_tokens = int(api.get("max_tokens", 900))
        self.timeout = int(api.get("timeout", 60))

    @property
    def configured(self) -> bool:
        if self.mock:
            return True
        return bool(self.base_url and self.api_key)

    @property
    def mock(self) -> bool:
        return os.environ.get("GOUTOU_MOCK") == "1" or self.api_key.startswith("mock")

    def stream_chat(self, messages, temperature: float | None = None,
                    max_tokens: int | None = None, on_delta=None) -> str:
        """逐块回调，返回完整文本。messages 可以是消息列表，也可以直接给一段文本。"""
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        if self.mock:
            return self._mock_stream(on_delta)
        if not self.configured:
            raise LLMError("未配置 API Key：请在设置里填写，或点『复制给豆包』用提示词模式。")

        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        try:
            import requests
        except ImportError as e:
            raise LLMError(f"缺少 requests 依赖：{e}") from e

        full: list[str] = []
        try:
            with requests.post(url, headers=headers, json=payload,
                               stream=True, timeout=(10, self.timeout)) as r:
                if r.status_code != 200:
                    body = r.text[:300]
                    raise LLMError(self._friendly(r.status_code, body))
                for raw in r.iter_lines(decode_unicode=True):
                    if not raw:
                        continue
                    line = raw.strip()
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if line == "[DONE]":
                        break
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content") or ""
                    if piece:
                        full.append(piece)
                        if on_delta:
                            on_delta(piece)
        except LLMError:
            raise
        except Exception as e:
            raise LLMError(f"请求失败：{type(e).__name__} {e}") from e

        text = "".join(full).strip()
        if not text:
            raise LLMError("模型返回为空，可能是余额不足或模型名写错。")
        return text

    @staticmethod
    def _friendly(code: int, body: str) -> str:
        if code == 401:
            return "401 未授权：API Key 无效或与 base_url 不匹配。"
        if code == 402:
            return "402 余额不足：请到服务商后台充值。"
        if code == 404:
            return f"404 接口或模型不存在：检查 base_url/model。返回：{body}"
        if code == 429:
            return "429 触发限流：稍等几秒再点一次。"
        return f"HTTP {code}：{body}"

    def _mock_stream(self, on_delta=None) -> str:
        buf = ""
        for i in range(0, len(MOCK_TEXT), 12):
            chunk = MOCK_TEXT[i:i + 12]
            buf += chunk
            if on_delta:
                on_delta(chunk)
            time.sleep(0.02)
        return buf

    # 供设置页"测试连接"使用
    def ping(self) -> str:
        msgs = [{"role": "user", "content": "只回复两个字：可用"}]
        return self.stream_chat(msgs, temperature=0.0)
