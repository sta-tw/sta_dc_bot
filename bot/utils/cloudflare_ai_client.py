from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Sequence

try:
    from cloudflare_ai_python import CloudflareAI
except ImportError:
    CloudflareAI = None

from .config import Settings


class CloudflareAIClient:

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._prompt_config = settings.prompt_config
        self._model_name = os.getenv("CLOUDFLARE_MODEL")
        self._persona_prompt = self._prompt_config.system_prompt
        self._max_sentences = settings.llm_max_sentences
        self._lock = asyncio.Lock()
        self._logger = logging.getLogger("bot.llm")
        account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
        api_token = os.getenv("CLOUDFLARE_API_TOKEN", "")
        
        if not account_id or not api_token:
            self._logger.error("CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN must be set")
            self._client = None
        elif CloudflareAI is None:
            self._logger.error("cloudflare-ai-python package not installed")
            self._client = None
        else:
            try:
                self._client = CloudflareAI(account_id, api_token)
                self._logger.info("Cloudflare AI client initialized successfully")
            except Exception as exc:
                self._logger.exception("Failed to initialize Cloudflare AI client", exc_info=exc)
                self._client = None

    async def generate_sections(self, sections: Sequence[tuple[str, str]]) -> str:
        prompt = self._build_prompt(sections)
        return await self._run_model(prompt)

    async def generate_chat_reply(
        self,
        *,
        context: list[str],
        previous_reply: str | None,
        user_display: str,
        message: str,
        reference_info: str | None = None,
        input_time: str | None = None,
    ) -> str:
        safe_message = self.sanitize_text(message)
        safe_context = [self.sanitize_text(item) for item in context]
        safe_previous = self.sanitize_text(previous_reply) if previous_reply else None
        sections: list[tuple[str, str]] = []
        
        if reference_info:
            sections.append(("參考資訊 (知識庫)", self.sanitize_text(reference_info)))

        if input_time:
            sections.append(("使用者訊息時間", str(input_time)))

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

    async def _run_model(self, prompt: str) -> str:
        if self._client is None:
            return "目前尚未正確配置 Cloudflare AI，請檢查環境變數設定。"
        
        default_message = "我目前無法回覆，請稍後再試。"
        
        async with self._lock:
            try:
                response = await asyncio.to_thread(
                    self._client.run_model,
                    self._model_name,
                    prompt,
                    max_tokens=256,
                    temperature=0.7
                )
                
                content = self._extract_text(response)
                if content:
                    return self._post_process(content)
                
                self._logger.warning("Cloudflare AI response empty")
                return default_message
                
            except Exception as exc:
                self._logger.exception("Cloudflare AI request failed", exc_info=exc)
                return default_message

    def _extract_text(self, response: dict) -> str:
        try:
            if isinstance(response, dict):
                result = response.get('result', {})
                if isinstance(result, dict):
                    return result.get('response', '')
                return response.get('response', '')
            return str(response)
        except Exception as exc:
            self._logger.warning("Failed to extract text from response: %s", exc)
            return ""

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
        sanitized = re.sub(r"\*[^*\n]*\*", "", sanitized)
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
