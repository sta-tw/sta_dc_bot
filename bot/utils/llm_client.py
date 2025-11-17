from __future__ import annotations

import asyncio
from typing import Iterable

import google.generativeai as genai

from .config import require_env


class GeminiClient:

    _client_lock = asyncio.Lock()
    _configured = False

    def __init__(self) -> None:
        self._model_name = require_env("GEMINI_MODEL")
        self._system_prompt = require_env("LLM_SYSTEM_PROMPT")

    async def generate(self, messages: Iterable[str]) -> str:
        async with self._client_lock:
            if not self._configured:
                genai.configure(api_key=require_env("GEMINI_API_KEY"))
                self.__class__._configured = True

        model = genai.GenerativeModel(self._model_name)
        prompt_segments = [self._system_prompt, "\n\n"]
        prompt_segments.extend(messages)
        prompt = "".join(prompt_segments)

        response = await asyncio.to_thread(model.generate_content, prompt)
        if not response.candidates:
            return "我目前無法回覆，請稍後再試。"

        candidate = response.candidates[0]
        if not candidate.content or not candidate.content.parts:
            return "我目前無法回覆，請稍後再試。"

        parts = []
        for part in candidate.content.parts:
            text = getattr(part, "text", None)
            if text:
                parts.append(text)

        return "".join(parts) if parts else "我目前無法回覆，請稍後再試。"
