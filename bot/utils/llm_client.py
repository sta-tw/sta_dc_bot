from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Sequence

try:
    import google.generativeai as genai
except ImportError:
    genai = None

from .config import Settings


class GeminiClient:

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model_name = os.getenv("GEMINI_MODEL", settings.llm_model)
        self._persona_prompt = settings.llm_persona_prompt
        self._max_sentences = settings.llm_max_sentences
        self._api_keys = self._load_api_keys()
        self._lock = asyncio.Lock()
        self._key_index = 0
        self._logger = logging.getLogger("bot.llm")

    async def generate_sections(self, sections: Sequence[tuple[str, str]]) -> str:
        prompt = self._build_prompt(sections)
        return await self._run_with_fallback(prompt)

    async def generate_chat_reply(
        self,
        *,
        context: list[str],
        previous_reply: str | None,
        user_display: str,
        message: str,
        reference_info: str | None = None,
    ) -> str:
        safe_message = self.sanitize_text(message)
        safe_context = [self.sanitize_text(item) for item in context]
        safe_previous = self.sanitize_text(previous_reply) if previous_reply else None
        sections: list[tuple[str, str]] = []
        
        if reference_info:
            sections.append(("參考資訊 (知識庫)", self.sanitize_text(reference_info)))

        sections.extend([
            ("最近對話", "\n".join(safe_context) if safe_context else "(無歷史訊息)"),
            ("上一輪回覆", safe_previous or "(尚未有回覆)"),
            ("使用者訊息", f"{user_display}: {safe_message}"),
        ])
        return await self.generate_sections(sections)

    async def generate_ticket_reply(
        self,
        *,
        requester: str,
        category_label: str,
        summary: str,
        description: str,
        ai_hint: str | None,
        reference_info: str | None,
    ) -> str:
        sections: list[tuple[str, str]] = [
            ("客戶", requester),
            ("分類", category_label),
            ("主旨", self.sanitize_text(summary)),
            ("描述", self.sanitize_text(description)),
        ]
        if ai_hint:
            sections.append(("附加指引", self.sanitize_text(ai_hint)))
        if reference_info:
            sections.append(("參考資訊 (知識庫)", self.sanitize_text(reference_info)))
        return await self.generate_sections(sections)

    async def _run_with_fallback(self, prompt: str) -> str:
        if genai is None:
            return "目前尚未安裝所需的 LLM 套件 (google-generativeai)，請安裝後再試。"
        default_message = "我目前無法回覆，請稍後再試。"
        async with self._lock:
            start_index = self._key_index
            for attempt in range(len(self._api_keys)):
                key_index = (start_index + attempt) % len(self._api_keys)
                api_key = self._api_keys[key_index]
                try:
                    genai.configure(api_key=api_key)
                    model = genai.GenerativeModel(self._model_name)
                    response = await asyncio.to_thread(model.generate_content, prompt)
                except Exception as exc:
                    self._logger.warning("Gemini key index %s failed (switching to next): %s", key_index, exc)
                    self._key_index = (key_index + 1) % len(self._api_keys)
                    continue

                content = self._extract_text(response)
                if content:
                    self._key_index = key_index
                    return self._post_process(content)
                self._logger.info("Gemini response empty with key index %s", key_index)
        return default_message

    def _extract_text(self, response: object) -> str:
        candidates = getattr(response, "candidates", None)
        if not candidates:
            return ""
        candidate = candidates[0]
        content = getattr(candidate, "content", None)
        if not content:
            return ""
        parts = getattr(content, "parts", None) or []
        texts: list[str] = []
        for part in parts:
            text = getattr(part, "text", None)
            if text:
                texts.append(text)
        return "".join(texts)

    def _build_prompt(self, sections: Sequence[tuple[str, str]]) -> str:
        persona = self._persona_prompt.strip()
        style_rules = os.getenv("LLM_STYLE_RULES", "").strip()
        lines = [persona, "\n", style_rules, "\n\n"]
        lines.append("以下是與使用者互動所需的資訊：")
        for title, content in sections:
            if not content:
                continue
            lines.append(f"\n## {title}\n{content.strip()}")
        lines.append(
            "\n請用最多三個句子回答，避免舞台指示、程式碼區塊或任何提及系統提示、模型。"
        )
        return "".join(lines)

    def _post_process(self, text: str) -> str:
        sanitized = self.sanitize_text(text)
        sanitized = re.sub(r"\*[^^\n]*\*", "", sanitized)
        sanitized = re.sub(r"\[[^\]]*\]", "", sanitized)
        sanitized = re.sub(r"\([^\)]*\)", "", sanitized)
        sanitized = self._mask_prompt_leaks(sanitized)
        sentences = re.split(r"(?<=[。.!?])\s+", sanitized.strip())
        if self._max_sentences and len(sentences) > self._max_sentences:
            sentences = sentences[: self._max_sentences]
        result = " ".join(sentence for sentence in sentences if sentence)
        if not result:
            return "喵？目前找不到可以分享的資訊，請再提供一些細節喔。"
        persona_signature = re.compile(re.escape(self._persona_prompt), re.IGNORECASE)
        result = persona_signature.sub("", result)
        return result.strip()

    def _load_api_keys(self) -> list[str]:
        keys = [key for key in self._settings.llm_api_keys if key]
        keys_env = os.getenv("GEMINI_API_KEYS", "")
        keys += [key.strip() for key in keys_env.split(",") if key.strip()]
        fallback = os.getenv("GEMINI_API_KEY", "").strip()
        if fallback and fallback not in keys:
            keys.append(fallback)
        seen: set[str] = set()
        ordered: list[str] = []
        for k in keys:
            if k not in seen:
                seen.add(k)
                ordered.append(k)
        if not ordered:
            raise RuntimeError("No Gemini API keys configured")
        return ordered

    @staticmethod
    def sanitize_text(text: str | None) -> str:
        if not text:
            return ""
        sanitized = text.replace("@everyone", "@ everyone").replace("@here", "@ here")
        sanitized = re.sub(r"<@&?\d+>", "@成員", sanitized)
        return sanitized

    @staticmethod
    def _mask_prompt_leaks(text: str) -> str:
        leak_terms = [
            r"提示詞", r"system prompt", r"prompt", r"系統提示", r"模型", r"model", r"我是AI", r"我是一個AI",
        ]
        result = text
        for term in leak_terms:
            result = re.sub(term, "***", result, flags=re.IGNORECASE)
        return result
