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
        self._prompt_config = settings.prompt_config
        self._model_name = os.getenv("GEMINI_MODEL", settings.llm_model)
        self._persona_prompt = self._prompt_config.system_prompt
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
            (
                "最近對話",
                "\n".join(safe_context) if safe_context else "(無歷史訊息)",
            ),
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
        style_rules = self._prompt_config.style_rules.strip()
        lines = [persona, "\n", style_rules, "\n\n"]
        if self._prompt_config.context_preamble.strip():
            lines.append(self._prompt_config.context_preamble.strip())
        for title, content in sections:
            if not content:
                continue
            lines.append(f"\n## {title}\n{content.strip()}")
        if self._prompt_config.response_rules.strip():
            lines.append(f"\n{self._prompt_config.response_rules.strip()}")
        return "".join(lines)

    def _post_process(self, text: str) -> str:
        sanitized = self.sanitize_text(text)
        sanitized = re.sub(r"\*[^^\n]*\*", "", sanitized)
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

    def _mask_prompt_leaks(self, text: str) -> str:
        persona = self._persona_prompt.strip()
        if not persona:
            return text
        pattern = re.compile(re.escape(persona), re.IGNORECASE)
        if not pattern.search(text):
            return text
        return pattern.sub("***", text)
