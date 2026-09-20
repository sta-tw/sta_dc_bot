from __future__ import annotations

import asyncio
import importlib
import json
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from openai import APIConnectionError, APITimeoutError, BadRequestError, OpenAI, RateLimitError

from bot.utils.ai_context import calculate_output_tokens, compact_prompt, estimate_tokens, trim_lines, trim_text
from bot.utils.ai_rag import AdmissionGuideStore, RagHit
from bot.utils.web_search import SearxngSearch



WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "當目前上下文、記憶與簡章不足以可靠回答，或使用者需要最新公開資訊時，"
            "搜尋網路資料。只傳入簡短、具體的搜尋字串。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要搜尋的公開資訊，最多一個查詢。",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name) or str(default)))
    except (TypeError, ValueError):
        return max(minimum, default)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name) or str(default)))
    except (TypeError, ValueError):
        return max(minimum, default)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _decode_env_text(value: str) -> str:
    """Decode escaped newlines commonly used in dotenv prompt values."""
    return value.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\r")


def _quote_text(text: str) -> str:
    """Make untrusted multiline text visibly quoted in the prompt."""
    lines = str(text or "").splitlines() or [""]
    return "\n".join(f"> {line}" for line in lines)


class AiChat(commands.Cog):
    MASS_MENTION_TOKENS: tuple[str, str] = ("@everyone", "@here")
    IMAGE_URL_PATTERN = re.compile(r"https?://[^\s<>\]]+")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.api_key = (os.getenv("VLLM_API_KEY") or "").strip()
        self.base_url = os.getenv("VLLM_BASE_URL", "").strip()
        self.model = os.getenv("VLLM_MODEL", "").strip()
        configured_system_prompt = _decode_env_text(
            (os.getenv("VLLM_SYSTEM_PROMPT") or "").strip()
        )
        agent_instructions = _decode_env_text(
            (os.getenv("VLLM_AGENT_INSTRUCTIONS") or "").strip()
        )
        self.system_prompt = "\n\n".join(
            part for part in (configured_system_prompt, agent_instructions) if part
        )
        self.user_prompt_template = _decode_env_text(
            (os.getenv("VLLM_USER_PROMPT_TEMPLATE") or "").strip()
        )
        self.no_context_text = (os.getenv("VLLM_NO_CONTEXT_TEXT") or "").strip()
        self.no_memory_text = (os.getenv("VLLM_NO_MEMORY_TEXT") or "").strip()
        self.no_rag_text = (os.getenv("VLLM_NO_RAG_TEXT") or "(無可用簡章資料)").strip()
        self.no_web_search_text = (os.getenv("VLLM_NO_WEB_SEARCH_TEXT") or "(尚未搜尋網路)").strip()
        self.empty_user_text = (os.getenv("VLLM_EMPTY_USER_TEXT") or "").strip()
        self.empty_reply_text = (os.getenv("VLLM_EMPTY_REPLY_TEXT") or "").strip()
        self.rate_limit_message_template = (os.getenv("VLLM_RATE_LIMIT_MESSAGE") or "").strip()
        self.unavailable_message_template = (
            os.getenv("VLLM_UNAVAILABLE_MESSAGE") or "LLM 服務暫時無法連線，請稍後再試（約 {seconds} 秒）。"
        ).strip()

        self.context_limit = _env_int("VLLM_CONTEXT_MESSAGES", 8, 1)
        self.context_max_chars = _env_int("VLLM_CONTEXT_MAX_CHARS", 5000, 500)
        self.memory_max_chars = _env_int("VLLM_MEMORY_MAX_CHARS", 3000, 500)
        self.rag_max_chars = _env_int("VLLM_RAG_MAX_CHARS", 5000, 500)
        self.max_reply_chars = _env_int("VLLM_MAX_REPLY_CHARS", 1800, 200)
        self.temperature = max(0.0, min(2.0, _env_float("VLLM_TEMPERATURE", 0.7)))
        self.disable_thinking = _env_bool("VLLM_DISABLE_THINKING", True)
        self.max_tokens = _env_int("VLLM_MAX_TOKENS", 0, 0)
        self.agent_final_max_tokens = _env_int(
            "VLLM_AGENT_FINAL_MAX_TOKENS", 0, 0
        )
        self.min_output_tokens = _env_int("VLLM_MIN_OUTPUT_TOKENS", 96, 16)
        self.context_window_tokens = _env_int("VLLM_CONTEXT_WINDOW_TOKENS", 8192, 1024)
        self.input_safety_tokens = _env_int("VLLM_INPUT_SAFETY_TOKENS", 256, 32)
        self.rate_limit_cooldown = _env_int("VLLM_RATE_LIMIT_COOLDOWN", 60, 5)
        self.vllm_timeout = _env_float("VLLM_TIMEOUT", 1800.0, 30.0)
        self.request_retries = _env_int("VLLM_REQUEST_RETRIES", 2, 0)
        self.retry_backoff = _env_float("VLLM_RETRY_BACKOFF", 0.8, 0.1)
        self.s2t_enabled = _env_bool("VLLM_S2T_ENABLED", True)
        self.memory_enabled = _env_bool("VLLM_MEMORY_ENABLED", True)
        self.memory_top_k = _env_int("VLLM_MEMORY_TOP_K", 3, 1)
        self.memory_scope = (os.getenv("VLLM_MEMORY_SCOPE") or "channel").strip().lower()
        self.memory_collection_name = (os.getenv("VLLM_MEMORY_COLLECTION") or "ai_chat_memory").strip()
        self.memory_dir = Path((os.getenv("VLLM_MEMORY_DIR") or "data/chroma").strip())
        self.vision_enabled = _env_bool("VLLM_VISION_ENABLED", True)
        self.vision_max_images = _env_int("VLLM_VISION_MAX_IMAGES", 3, 1)
        self.vision_max_image_bytes = _env_int("VLLM_VISION_MAX_IMAGE_BYTES", 1024 * 1024, 1)

        self.web_search_enabled = _env_bool("VLLM_WEB_SEARCH_ENABLED", True)
        self.web_search_max_chars = _env_int("VLLM_WEB_SEARCH_MAX_CHARS", 5000, 500)
        self.web_search = SearxngSearch(
            os.getenv("VLLM_WEB_SEARCH_URL", "http://100.127.35.14:8162"),
            timeout=_env_float("VLLM_WEB_SEARCH_TIMEOUT", 8.0, 1.0),
            max_results=_env_int("VLLM_WEB_SEARCH_MAX_RESULTS", 5, 1),
            max_snippet_chars=_env_int("VLLM_WEB_SEARCH_SNIPPET_CHARS", 700, 80),
            language=os.getenv("VLLM_WEB_SEARCH_LANGUAGE", "auto"),
            user_agent=os.getenv("VLLM_WEB_SEARCH_USER_AGENT", "sta-dc-bot/1.0"),
            logger=bot.logger,
        )

        self.rag_enabled = _env_bool("VLLM_RAG_ENABLED", True)
        self.rag_top_k = _env_int("VLLM_RAG_TOP_K", 3, 1)
        self.rag_store: AdmissionGuideStore | None = None
        self._rate_limited_until = 0.0
        self.memory_collection = None
        self.s2t_converter = self._init_s2t_converter() if self.s2t_enabled else None

        self.client: OpenAI | None = None
        has_required_config = all(
            [
                self.api_key,
                self.base_url,
                self.model,
                configured_system_prompt,
                self.user_prompt_template,
                self.no_context_text,
                self.no_memory_text,
                self.empty_user_text,
                self.empty_reply_text,
                self.rate_limit_message_template,
            ]
        )
        if has_required_config:
            self.client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.vllm_timeout,
            )
            self.bot.logger.info(
                "AiChat initialized: base_url=%s, model=%s, timeout=%ss",
                self.base_url,
                self.model,
                self.vllm_timeout,
            )
        else:
            self.bot.logger.warning("AiChat disabled: missing required VLLM_* environment variables")

        if self.s2t_enabled and self.s2t_converter is None:
            self.bot.logger.warning("AiChat s2t disabled: opencc is not installed")

        if self.memory_enabled:
            self._init_memory_store()
        if self.rag_enabled:
            self._init_rag_store()

    def _init_memory_store(self) -> None:
        try:
            chromadb = importlib.import_module("chromadb")
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(path=str(self.memory_dir))
            self.memory_collection = client.get_or_create_collection(name=self.memory_collection_name)
            self.bot.logger.info("AiChat memory ready: %s", self.memory_collection_name)
        except Exception as exc:
            self.memory_collection = None
            self.bot.logger.warning("AiChat memory init failed: %s", exc)

    def _init_rag_store(self) -> None:
        try:
            self.rag_store = AdmissionGuideStore(
                directory=self.memory_dir,
                collection_name=(os.getenv("VLLM_RAG_COLLECTION") or "ai_admission_guides").strip(),
                max_bytes=_env_int("VLLM_RAG_MAX_BYTES", 10 * 1024 * 1024, 1),
                max_pages=_env_int("VLLM_RAG_MAX_PAGES", 120, 1),
                chunk_chars=_env_int("VLLM_RAG_CHUNK_CHARS", 1200, 100),
                chunk_overlap=_env_int("VLLM_RAG_CHUNK_OVERLAP", 160, 0),
            )
            self.bot.logger.info("AiChat RAG ready")
        except Exception as exc:
            self.rag_store = None
            self.bot.logger.warning("AiChat RAG init failed: %s", exc)

    def _init_s2t_converter(self):
        try:
            opencc_module = importlib.import_module("opencc")
            return opencc_module.OpenCC("s2t")
        except Exception:
            return None

    def _build_user_prompt(
        self,
        message: discord.Message | None,
        cleaned_user_text: str,
        context_lines: list[str],
        memory_lines: list[str],
        user_name: str,
        rag_lines: list[str] | None = None,
        web_search_text: str | None = None,
    ) -> str:
        context_lines = trim_lines(context_lines, self.context_max_chars)
        memory_lines = trim_lines(
            memory_lines,
            self.memory_max_chars,
            newest_first=False,
        )
        rag_lines = trim_lines(
            rag_lines or [],
            self.rag_max_chars,
            newest_first=False,
        )
        context_text = "\n".join(context_lines) if context_lines else self.no_context_text
        memory_text = "\n".join(memory_lines) if memory_lines else self.no_memory_text
        rag_text = "\n".join(rag_lines) if rag_lines else self.no_rag_text
        web_text = web_search_text or self.no_web_search_text
        values = {
            "context": context_text,
            "memory": memory_text,
            "rag": rag_text,
            "web_search": web_text,
            "user_input": cleaned_user_text,
            "user_name": user_name,
        }
        try:
            rendered = self.user_prompt_template.format(**values)
        except (KeyError, ValueError) as exc:
            self.bot.logger.warning("Invalid VLLM_USER_PROMPT_TEMPLATE, using safe fallback: %s", exc)
            return (
                f"頻道上下文：\n{context_text}\n\n"
                f"長期記憶：\n{memory_text}\n\n"
                f"官方簡章：\n{rag_text}\n\n"
                f"網路搜尋：\n{web_text}\n\n"
                f"使用者：{user_name}\n使用者最新訊息：{cleaned_user_text}"
            )

        missing_sections: list[str] = []
        if "{rag}" not in self.user_prompt_template:
            missing_sections.append(f"官方簡章資料：\n{rag_text}")
        if "{web_search}" not in self.user_prompt_template:
            missing_sections.append(f"網路搜尋結果：\n{web_text}")
        if missing_sections:
            evidence_text = "\n\n".join(missing_sections)
            marker = "使用者最新訊息："
            if marker in rendered:
                prefix, latest = rendered.rsplit(marker, 1)
                rendered = (
                    f"{prefix.rstrip()}\n\n{evidence_text}\n\n"
                    f"{marker}{latest.lstrip()}"
                )
            else:
                user_position = rendered.rfind(cleaned_user_text)
                if user_position >= 0 and cleaned_user_text:
                    prefix = rendered[:user_position]
                    latest = rendered[user_position:]
                    rendered = (
                        f"{prefix.rstrip()}\n\n{evidence_text}\n\n"
                        f"{marker}{latest.lstrip()}"
                    )
                else:
                    rendered = f"{rendered.rstrip()}\n\n{evidence_text}"
        return rendered

    def _sanitize_mass_mentions(self, text: str) -> str:
        sanitized = re.sub(r"@everyone", "@ everyone", text, flags=re.IGNORECASE)
        return re.sub(r"@here", "@ here", sanitized, flags=re.IGNORECASE)

    def _is_identity_question(self, text: str) -> bool:
        normalized = text.strip().lower().replace("？", "?")
        patterns = (r"^我是誰\??$", r"^我是谁\??$", r"^who am i\??$", r"^whoami\??$")
        return any(re.fullmatch(pattern, normalized) for pattern in patterns)

    def _is_image_attachment(self, attachment: discord.Attachment) -> bool:
        content_type = (attachment.content_type or "").lower()
        if content_type.startswith("image/"):
            return True
        suffix = Path(attachment.filename or "").suffix.lower()
        if not suffix:
            return False
        guessed_type, _ = mimetypes.guess_type(attachment.filename)
        return bool(guessed_type and guessed_type.startswith("image/"))

    def _extract_image_urls(self, message: discord.Message) -> list[str]:
        if not self.vision_enabled:
            return []
        image_urls: list[str] = []
        for attachment in message.attachments:
            if len(image_urls) >= self.vision_max_images:
                break
            if not self._is_image_attachment(attachment):
                continue
            if attachment.size and attachment.size > self.vision_max_image_bytes:
                continue
            image_urls.append(attachment.url)
        if len(image_urls) < self.vision_max_images:
            for url in self.IMAGE_URL_PATTERN.findall(message.content or ""):
                if len(image_urls) >= self.vision_max_images:
                    break
                lowered = url.lower().split("?", 1)[0]
                if any(lowered.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif")):
                    image_urls.append(url.rstrip(")].,>"))
        return image_urls

    def _bot_user_id(self) -> int | None:
        bot_user = getattr(self.bot, "user", None)
        bot_id = getattr(bot_user, "id", None)
        return int(bot_id) if isinstance(bot_id, int) else None

    @staticmethod
    def _author_name(author: Any) -> str:
        name = str(
            getattr(author, "display_name", None)
            or getattr(author, "name", None)
            or "未命名成員"
        )
        return name.replace("\r", " ").replace("\n", " ").strip() or "未命名成員"

    def _format_context_record(self, record: dict[str, Any], index: int) -> str:
        author_id = record.get("author_id")
        identity = f"{record['display_name']}（ID：{author_id}）" if author_id is not None else record["display_name"]
        return (
            f"[頻道歷史訊息 #{index}]\n"
            f"說話者類型：{record['speaker_type']}\n"
            f"說話者：{identity}\n"
            f"訊息內容：\n{_quote_text(record['content'])}\n"
            f"[/頻道歷史訊息 #{index}]"
        )

    def _context_records_to_messages(
        self,
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not records:
            return []
        per_message_budget = max(180, self.context_max_chars // len(records))
        messages: list[dict[str, Any]] = []
        for index, record in enumerate(records, start=1):
            content = self._format_context_record(record, index)
            content = compact_prompt(content, per_message_budget, marker="訊息內容：")
            messages.append({"role": record["role"], "content": content})
        return messages

    async def _collect_context_records(self, message: discord.Message) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        current_user_id = getattr(getattr(message, "author", None), "id", None)
        bot_user_id = self._bot_user_id()
        history_limit = max(1, self.context_limit + 1)
        message_id = getattr(message, "id", None)
        async for item in message.channel.history(
            limit=history_limit,
            before=message,
            oldest_first=False,
        ):
            if message_id is not None and getattr(item, "id", None) == message_id:
                continue
            author = getattr(item, "author", None)
            author_id = getattr(author, "id", None)
            is_own_bot = bot_user_id is not None and author_id == bot_user_id
            if getattr(item, "webhook_id", None) is not None and not is_own_bot:
                continue
            if getattr(author, "bot", False) and not is_own_bot:
                continue
            text = (getattr(item, "content", None) or "").strip()
            if not text:
                continue

            if is_own_bot:
                role = "assistant"
                speaker_type = "管理機器人／助理"
            elif author_id == current_user_id:
                role = "user"
                speaker_type = "目前使用者"
            else:
                role = "user"
                speaker_type = "其他成員"
            records.append(
                {
                    "role": role,
                    "speaker_type": speaker_type,
                    "author_id": author_id,
                    "display_name": self._author_name(author),
                    "content": text,
                }
            )

        records.reverse()
        return records[-max(1, self.context_limit) :]

    async def _collect_context(self, message: discord.Message) -> list[str]:
        records = await self._collect_context_records(message)
        return [
            self._format_context_record(record, index)
            for index, record in enumerate(records, start=1)
        ]

    def _build_user_message_content(self, prompt: str, image_urls: list[str]) -> str | list[dict[str, object]]:
        if not image_urls:
            return prompt
        content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
        for url in image_urls:
            content.append({"type": "image_url", "image_url": {"url": url}})
        return content

    def _query_memory(self, query_text: str, guild_id: int, user_id: int, channel_id: int | None = None) -> list[str]:
        if self.memory_collection is None:
            return []
        try:
            filters: list[dict[str, Any]] = [
                {"guild_id": {"$eq": guild_id}},
                {"user_id": {"$eq": user_id}},
            ]
            if self.memory_scope != "guild" and channel_id is not None:
                filters.append({"channel_id": {"$eq": channel_id}})
            result = self.memory_collection.query(
                query_texts=[query_text],
                n_results=self.memory_top_k,
                where={"$and": filters},
                include=["documents", "metadatas"],
            )
            docs = result.get("documents") or [[]]
            metadata_rows = result.get("metadatas") or [[]]
            documents = docs[0] if docs else []
            metadatas = metadata_rows[0] if metadata_rows else []
            lines: list[str] = []
            for index, doc in enumerate(documents, start=1):
                if not isinstance(doc, str) or not doc.strip():
                    continue
                metadata = dict(metadatas[index - 1] or {}) if index - 1 < len(metadatas) else {}
                source = (
                    "同一頻道"
                    if self.memory_scope != "guild" and channel_id is not None
                    else "同一伺服器"
                )
                stored_name = str(metadata.get("user_name") or "目前使用者")
                stored_name = stored_name.replace("\r", " ").replace("\n", " ").strip()
                stored_id = metadata.get("user_id", user_id)
                lines.append(
                    f"[歷史記憶 #{index}｜來源：{source}｜使用者：{stored_name}（ID：{stored_id}）]\n"
                    f"{_quote_text(doc)}\n"
                    f"[/歷史記憶 #{index}]"
                )
            return lines
        except Exception as exc:
            self.bot.logger.warning("AiChat memory query failed: %s", exc)
            return []

    def _query_rag(self, query_text: str, guild_id: int) -> list[str]:
        if self.rag_store is None:
            return []
        try:
            hits = self.rag_store.query(guild_id=guild_id, query_text=query_text, top_k=self.rag_top_k)
        except Exception as exc:
            self.bot.logger.warning("AiChat RAG query failed: %s", exc)
            return []
        lines: list[str] = []
        for index, hit in enumerate(hits, start=1):
            title = str(hit.metadata.get("title") or "未命名簡章")
            page = hit.metadata.get("page")
            page_text = f"，第 {page} 頁" if page else ""
            source = hit.metadata.get("source_url") or hit.metadata.get("filename")
            source_text = f"（來源：{source}）" if source else ""
            lines.append(
                f"[簡章 {index}] {title}{page_text}{source_text}\n"
                f"<簡章引用>\n{_quote_text(hit.text)}\n</簡章引用>"
            )
        return trim_lines(lines, self.rag_max_chars, newest_first=False)

    def _save_memory(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        user_name: str,
        prompt_text: str,
        reply_text: str,
    ) -> None:
        if self.memory_collection is None:
            return
        memory_doc = (
            "[記憶記錄]\n"
            f"使用者顯示名稱：\n{_quote_text(user_name)}\n"
            f"使用者訊息：\n{_quote_text(prompt_text)}\n"
            f"管理機器人／助理回覆：\n{_quote_text(reply_text)}\n"
            "[/記憶記錄]"
        )
        record_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex}"
        try:
            self.memory_collection.add(
                ids=[record_id],
                documents=[memory_doc],
                metadatas=[
                    {
                        "schema_version": 2,
                        "source": "ai_chat",
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                        "user_id": user_id,
                        "user_name": user_name,
                        "ts": int(time.time()),
                    }
                ],
            )
        except Exception as exc:
            self.bot.logger.warning("AiChat memory save failed: %s", exc)

    def _save_message_memory(
        self,
        guild_id: int,
        channel_id: int,
        channel_name: str,
        user_id: int,
        user_name: str,
        text: str,
    ) -> None:
        """Keep non-mentioned human messages for the configured memory scope."""
        if self.memory_collection is None:
            return
        text = text.strip()
        if len(text) < 3 or text.startswith(("!", "/", "$", "-")):
            return
        memory_doc = (
            "[頻道記憶]\n"
            f"頻道：{_quote_text(channel_name)}\n"
            f"使用者：\n{_quote_text(user_name)}\n"
            f"訊息內容：\n{_quote_text(text)}\n"
            "[/頻道記憶]"
        )
        record_id = f"msg-{int(time.time() * 1000)}-{uuid.uuid4().hex}"
        try:
            self.memory_collection.add(
                ids=[record_id],
                documents=[memory_doc],
                metadatas=[
                    {
                        "schema_version": 2,
                        "source": "channel_message",
                        "type": "channel_msg",
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                        "user_id": user_id,
                        "user_name": user_name,
                        "channel_name": channel_name,
                        "ts": int(time.time()),
                    }
                ],
            )
        except Exception as exc:
            self.bot.logger.warning("AiChat general message memory save failed: %s", exc)

    @staticmethod
    def _response_message(response: Any) -> Any | None:
        choices = getattr(response, "choices", None) or []
        return getattr(choices[0], "message", None) if choices else None

    @staticmethod
    def _response_text(response: Any) -> str:
        message = AiChat._response_message(response)
        content = getattr(message, "content", "") if message is not None else ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(item.get("text", "")) for item in content if isinstance(item, dict)
            )
        return str(content or "")

    @staticmethod
    def _extract_tool_calls(response: Any) -> list[tuple[str, str, str]]:
        message = AiChat._response_message(response)
        tool_calls = getattr(message, "tool_calls", None) if message is not None else None
        extracted: list[tuple[str, str, str]] = []
        for call in tool_calls or []:
            function = getattr(call, "function", None)
            call_id = getattr(call, "id", "")
            name = getattr(function, "name", "") if function is not None else ""
            arguments = getattr(function, "arguments", "{}") if function is not None else "{}"
            if call_id and name:
                extracted.append((str(call_id), str(name), str(arguments or "{}")))
        return extracted

    @staticmethod
    def _is_context_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            marker in text
            for marker in ("context length", "maximum context", "too many tokens", "prompt is too long", "max_tokens")
        )

    @staticmethod
    def _is_tool_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(marker in text for marker in ("tool", "function calling", "function call", "unsupported"))

    @staticmethod
    def _is_thinking_option_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "enable_thinking",
                "chat_template_kwargs",
                "thinking is not supported",
            )
        )

    def _estimate_message_tokens(self, messages: list[dict[str, Any]]) -> int:
        serialized = json.dumps(messages, ensure_ascii=False, default=str)
        message_overhead = len(messages) * 4
        return estimate_tokens(serialized) + message_overhead

    def _compact_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        compacted: list[dict[str, Any]] = []
        for message in messages:
            copied = dict(message)
            role = copied.get("role")
            text_limit = 2400 if role == "user" else 1200
            content = copied.get("content")
            if isinstance(content, str):
                copied["content"] = compact_prompt(content, text_limit)
            elif isinstance(content, list):
                blocks = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        block = dict(block)
                        block["text"] = compact_prompt(
                            str(block.get("text") or ""), text_limit
                        )
                    blocks.append(block)
                copied["content"] = blocks
            compacted.append(copied)
        return compacted

    def _prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        input_budget = max(
            1,
            self.context_window_tokens
            - self.input_safety_tokens
            - max(1, self.min_output_tokens),
        )
        current_messages = messages
        for _ in range(3):
            if self._estimate_message_tokens(current_messages) <= input_budget:
                break
            compacted = self._compact_messages(current_messages)
            if compacted == current_messages:
                break
            current_messages = compacted
        return current_messages

    def _max_tokens_for_messages(
        self,
        messages: list[dict[str, Any]],
        configured_max_tokens: int | None = None,
    ) -> int:
        input_tokens = self._estimate_message_tokens(messages)
        return calculate_output_tokens(
            input_tokens=input_tokens,
            context_window_tokens=self.context_window_tokens,
            configured_max_tokens=(
                self.max_tokens if configured_max_tokens is None else configured_max_tokens
            ),
            safety_tokens=self.input_safety_tokens,
        )

    def _call_vllm(
        self,
        messages: list[dict[str, Any]],
        *,
        use_tools: bool = False,
        max_tokens_override: int | None = None,
    ) -> Any:
        if self.client is None:
            return ""
        attempts = self.request_retries + 1
        compacted_once = False
        thinking_option_fallback_used = False
        disable_thinking = bool(getattr(self, "disable_thinking", True))
        current_messages = self._prepare_messages(messages)
        attempt = 1
        while attempt <= attempts:
            try:
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "messages": current_messages,
                    "temperature": self.temperature,
                    "max_tokens": self._max_tokens_for_messages(
                        current_messages,
                        configured_max_tokens=max_tokens_override,
                    ),
                    "stream": False,
                }
                if disable_thinking:
                    kwargs["extra_body"] = {
                        "chat_template_kwargs": {"enable_thinking": False}
                    }
                if use_tools and self.web_search_enabled:
                    kwargs["tools"] = [WEB_SEARCH_TOOL]
                    kwargs["tool_choice"] = "auto"
                response = self.client.chat.completions.create(**kwargs)
                choices = getattr(response, "choices", None) or []
                if choices:
                    self.bot.logger.debug("AiChat finish_reason=%s", getattr(choices[0], "finish_reason", None))
                return response
            except BadRequestError as exc:
                if (
                    disable_thinking
                    and not thinking_option_fallback_used
                    and self._is_thinking_option_error(exc)
                ):
                    thinking_option_fallback_used = True
                    disable_thinking = False
                    attempts += 1
                    self.bot.logger.warning(
                        "Local model rejected the thinking option; retrying without it: %s",
                        exc,
                    )
                    continue
                if self._is_context_error(exc) and not compacted_once:
                    compacted_once = True
                    current_messages = self._prepare_messages(
                        self._compact_messages(current_messages)
                    )
                    attempt += 1
                    continue
                raise
            except (APIConnectionError, APITimeoutError) as exc:
                if attempt >= attempts:
                    raise
                delay_seconds = self.retry_backoff * (2 ** (attempt - 1))
                self.bot.logger.warning(
                    "AiChat request failed (%s/%s), retry in %.2fs: %s",
                    attempt,
                    attempts,
                    delay_seconds,
                    exc,
                )
                time.sleep(delay_seconds)
                attempt += 1
        return ""

    async def _retry_empty_response(
        self,
        messages: list[dict[str, Any]],
        response: Any,
    ) -> str:
        text = self._response_text(response)
        if text.strip():
            return text

        choice = (getattr(response, "choices", None) or [None])[0]
        self.bot.logger.warning(
            "AiChat returned an empty response (finish_reason=%s); retrying without tools",
            getattr(choice, "finish_reason", None),
        )
        retry_max_tokens = getattr(self, "agent_final_max_tokens", None)
        if retry_max_tokens is None:
            retry_max_tokens = 0
        direct_response = await asyncio.to_thread(
            self._call_vllm,
            messages,
            use_tools=False,
            max_tokens_override=retry_max_tokens,
        )
        return self._response_text(direct_response) or text

    async def _run_agent(self, messages: list[dict[str, Any]]) -> str:
        try:
            response = await asyncio.to_thread(self._call_vllm, messages, use_tools=True)
        except BadRequestError as exc:
            if not self._is_tool_error(exc):
                raise
            self.bot.logger.warning("Local model does not support web tools; using direct reply: %s", exc)
            response = await asyncio.to_thread(self._call_vllm, messages, use_tools=False)

        tool_calls = self._extract_tool_calls(response)
        if not tool_calls or not self.web_search_enabled:
            return await self._retry_empty_response(messages, response)

        call_id, name, raw_arguments = tool_calls[0]
        if name != "web_search":
            return await self._retry_empty_response(messages, response)
        try:
            arguments = json.loads(raw_arguments)
            query = str(arguments.get("query") or "").strip()[:300]
        except (TypeError, ValueError, AttributeError):
            query = ""
        if not query:
            return await self._retry_empty_response(messages, response)

        results = await asyncio.to_thread(self.web_search.search, query)
        search_text = self.web_search.format_results(results, max_chars=self.web_search_max_chars)
        tool_call_payload = {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": raw_arguments},
        }
        assistant_message = self._response_message(response)
        follow_up_messages = list(messages)
        follow_up_messages.append(
            {
                "role": "assistant",
                "content": getattr(assistant_message, "content", "") if assistant_message else "",
                "tool_calls": [tool_call_payload],
            }
        )
        follow_up_messages.append(
            {"role": "tool", "tool_call_id": call_id, "content": search_text}
        )
        final_response = await asyncio.to_thread(
            self._call_vllm,
            follow_up_messages,
            use_tools=False,
            max_tokens_override=getattr(self, "agent_final_max_tokens", 0),
        )
        return await self._retry_empty_response(follow_up_messages, final_response)

    def _can_manage_rag(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        permissions = getattr(member, "guild_permissions", None)
        if getattr(permissions, "manage_guild", False):
            return True
        support_role_ids = set(getattr(getattr(self.bot, "settings", None), "support_role_ids", []) or [])
        return any(getattr(role, "id", None) in support_role_ids for role in getattr(member, "roles", []))

    @app_commands.command(name="rag_add", description="加入一份供 AI 查詢的招生簡章（管理員）")
    @app_commands.describe(attachment="PDF 或 UTF-8 文字檔", title="簡章名稱，可省略")
    async def rag_add(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        title: str | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("請在伺服器內使用此指令。", ephemeral=True)
            return
        if not self._can_manage_rag(interaction):
            await interaction.response.send_message("需要伺服器管理權限或客服身分組。", ephemeral=True)
            return
        if self.rag_store is None:
            await interaction.response.send_message("RAG 尚未啟用或 Chroma 無法使用。", ephemeral=True)
            return
        if attachment is None:
            await interaction.response.send_message("請附上一份 PDF 或 UTF-8 文字格式的簡章。", ephemeral=True)
            return
        max_bytes = _env_int("VLLM_RAG_MAX_BYTES", 10 * 1024 * 1024, 1)
        if attachment.size and attachment.size > max_bytes:
            await interaction.response.send_message("簡章檔案過大，請先壓縮或分割檔案。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = await attachment.read()
            result = await asyncio.to_thread(
                self.rag_store.ingest_bytes,
                guild_id=interaction.guild.id,
                data=data,
                filename=attachment.filename,
                title=title or "",
                source_url=str(getattr(attachment, "url", "") or ""),
            )
        except Exception as exc:
            self.bot.logger.warning("RAG ingest failed: %s", exc)
            await interaction.followup.send(f"簡章匯入失敗：{exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"已加入「{result.title}」，共 {result.chunk_count} 個段落（文件 ID：`{result.document_id}`）。",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if self.client is None:
            return
        if message.guild is None or message.author.bot:
            return
        me = message.guild.me
        if me is None:
            return
        user_text = (message.content or "").strip()
        if me not in message.mentions or message.role_mentions or message.mention_everyone:
            if user_text:
                await asyncio.to_thread(
                    self._save_message_memory,
                    message.guild.id,
                    message.channel.id,
                    str(getattr(message.channel, "name", "")),
                    message.author.id,
                    self._author_name(message.author),
                    user_text,
                )
            return

        if not user_text:
            return
        mention_pattern = rf"<@!?{self.bot.user.id}>"
        cleaned_user_text = re.sub(mention_pattern, "", user_text).strip()
        if not cleaned_user_text:
            cleaned_user_text = self.empty_user_text

        user_name = self._author_name(message.author)
        query_text = f"[目前使用者｜{user_name}｜ID：{message.author.id}] {cleaned_user_text}"
        if self._is_identity_question(cleaned_user_text):
            await message.reply(
                f"你是 {user_name}",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        now = time.time()
        if now < self._rate_limited_until:
            retry_after = int(self._rate_limited_until - now)
            await message.reply(
                self.rate_limit_message_template.format(seconds=retry_after),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        try:
            context_records = await self._collect_context_records(message)
            history_messages = self._context_records_to_messages(context_records)
            context_lines = (
                [
                    f"[頻道歷史已以 {len(history_messages)} 則結構化 API 訊息提供；請依 API role 和說話者欄位判斷。]"
                ]
                if history_messages
                else []
            )
            image_urls = self._extract_image_urls(message)
            memory_lines = await asyncio.to_thread(
                self._query_memory,
                query_text,
                message.guild.id,
                message.author.id,
                message.channel.id,
            )
            rag_lines = await asyncio.to_thread(self._query_rag, cleaned_user_text, message.guild.id)
            prompt = self._build_user_prompt(
                message,
                cleaned_user_text,
                context_lines,
                memory_lines,
                user_name,
                rag_lines=rag_lines,
            )
            user_message_content = self._build_user_message_content(prompt, image_urls)
            messages = [
                {"role": "system", "content": self.system_prompt},
                *history_messages,
                {"role": "user", "content": user_message_content},
            ]

            async with message.channel.typing():
                content = await self._run_agent(messages)

            content = (content or "").strip() or self.empty_reply_text
            if self.s2t_converter is not None:
                content = self.s2t_converter.convert(content)
            content = self._sanitize_mass_mentions(content)
            if len(content) > self.max_reply_chars:
                content = content[: self.max_reply_chars - 1] + "…"

            await message.reply(
                content,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await asyncio.to_thread(
                self._save_memory,
                message.guild.id,
                message.channel.id,
                message.author.id,
                user_name,
                cleaned_user_text,
                content,
            )
        except RateLimitError as exc:
            self._rate_limited_until = time.time() + self.rate_limit_cooldown
            self.bot.logger.warning("AiChat rate limited, cooldown=%s sec: %s", self.rate_limit_cooldown, exc)
            await message.reply(
                self.rate_limit_message_template.format(seconds=self.rate_limit_cooldown),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (APIConnectionError, APITimeoutError) as exc:
            self._rate_limited_until = time.time() + self.rate_limit_cooldown
            self.bot.logger.warning("AiChat unavailable, cooldown=%s sec: %s", self.rate_limit_cooldown, exc)
            await message.reply(
                self.unavailable_message_template.format(seconds=self.rate_limit_cooldown),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            self.bot.logger.exception("AiChat failed", exc_info=exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AiChat(bot))
