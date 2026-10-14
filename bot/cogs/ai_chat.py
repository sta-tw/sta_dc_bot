from __future__ import annotations

import asyncio
import importlib
import mimetypes
import math
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from openai import OpenAI

from bot.utils.config import save_llm_disabled_channel_ids
from bot.utils.ai_context import ContextOverflow, PromptBudget, trim_lines
from bot.utils.ai_conversation import ConversationManager, ConversationScope
from bot.utils.ai_pipeline import (
    AnswerPipeline, ChannelScheduler, LLMWorkerPool,
    NoticeKind, PipelineConfig, PipelineRequest, PipelineResult, ServiceNotice,
    SyncLLMTransport, TurnBudget, extract_tool_calls, response_message, response_text, shared_llm_pool,
)
from bot.utils.ai_rag import AdmissionGuideStore
from bot.utils.ai_tools import ReadonlyToolRegistry
from bot.utils.web_search import SearxngSearch


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name) or str(default)))
    except (TypeError, ValueError):
        return max(minimum, default)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name) or str(default))
        return max(minimum, value if math.isfinite(value) else default)
    except (TypeError, ValueError):
        return max(minimum, default)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def _decode_env_text(value: str) -> str:
    return value.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\r")


def _quote_text(text: str) -> str:
    return "\n".join(f"> {line}" for line in (str(text or "").splitlines() or [""]))


@dataclass
class _PublicationAttempt:
    attempted: bool = False

    def claim(self) -> bool:
        if self.attempted:
            return False
        self.attempted = True
        return True


class AiChat(commands.Cog):
    IMAGE_URL_PATTERN = re.compile(r"https?://[^\s<>\]]+")
    THINKING_EMOJI = "🤔"
    REACTION_TIMEOUT = 3.0

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.api_key = (os.getenv("VLLM_API_KEY") or "").strip()
        self.base_url = os.getenv("VLLM_BASE_URL", "").strip()
        self.model = os.getenv("VLLM_MODEL", "").strip()
        configured_system_prompt = _decode_env_text((os.getenv("VLLM_SYSTEM_PROMPT") or "").strip())
        agent_instructions = _decode_env_text((os.getenv("VLLM_AGENT_INSTRUCTIONS") or "").strip())
        self.system_prompt = "\n\n".join(part for part in (configured_system_prompt, agent_instructions) if part)
        self.user_prompt_template = _decode_env_text((os.getenv("VLLM_USER_PROMPT_TEMPLATE") or "").strip())
        self.no_context_text = (os.getenv("VLLM_NO_CONTEXT_TEXT") or "").strip()
        self.no_memory_text = (os.getenv("VLLM_NO_MEMORY_TEXT") or "").strip()
        self.no_rag_text = (os.getenv("VLLM_NO_RAG_TEXT") or "(使用按需簡章工具)").strip()
        self.no_web_search_text = (os.getenv("VLLM_NO_WEB_SEARCH_TEXT") or "(尚未搜尋網路)").strip()
        self.empty_user_text = (os.getenv("VLLM_EMPTY_USER_TEXT") or "").strip()
        self.empty_reply_text = (os.getenv("VLLM_EMPTY_REPLY_TEXT") or "").strip()
        self.rate_limit_message_template = (os.getenv("VLLM_RATE_LIMIT_MESSAGE") or "").strip()
        self.unavailable_message_template = (os.getenv("VLLM_UNAVAILABLE_MESSAGE") or "").strip()

        self.context_limit = _env_int("VLLM_CONTEXT_MESSAGES", 8, 1)
        self.context_max_chars = _env_int("VLLM_CONTEXT_MAX_CHARS", 5000, 500)
        self.memory_max_chars = _env_int("VLLM_MEMORY_MAX_CHARS", 3000, 500)
        self.rag_max_chars = _env_int("VLLM_RAG_MAX_CHARS", 5000, 500)
        self.max_reply_chars = min(2000, _env_int("VLLM_MAX_REPLY_CHARS", 1800, 200))
        self.temperature = max(0.0, min(2.0, _env_float("VLLM_TEMPERATURE", 0.7)))
        self.disable_thinking = _env_bool("VLLM_DISABLE_THINKING", True)
        self.max_tokens = _env_int("VLLM_MAX_TOKENS", 0)
        self.agent_final_max_tokens = _env_int("VLLM_AGENT_FINAL_MAX_TOKENS", 0)
        self.min_output_tokens = _env_int("VLLM_MIN_OUTPUT_TOKENS", 96, 16)
        self.context_window_tokens = _env_int("VLLM_CONTEXT_WINDOW_TOKENS", 8192, 1024)
        self.input_safety_tokens = _env_int("VLLM_INPUT_SAFETY_TOKENS", 256, 32)
        self.rate_limit_cooldown = _env_int("VLLM_RATE_LIMIT_COOLDOWN", 60, 5)
        self.vllm_timeout = _env_float("VLLM_TIMEOUT", 1800.0, 1.0)
        self.request_timeout = min(self.vllm_timeout, _env_float("VLLM_REQUEST_TIMEOUT", 600.0, 1.0))
        self.request_retries = _env_int("VLLM_REQUEST_RETRIES", 2)
        self.retry_backoff = _env_float("VLLM_RETRY_BACKOFF", 0.8, 0.1)
        self.mention_only = _env_bool("VLLM_MENTION_ONLY", False)
        self.s2t_enabled = _env_bool("VLLM_S2T_ENABLED", True)
        self.memory_enabled = _env_bool("VLLM_MEMORY_ENABLED", True)
        self.memory_top_k = _env_int("VLLM_MEMORY_TOP_K", 3, 1)
        self.memory_scope = (os.getenv("VLLM_MEMORY_SCOPE") or "channel").strip().lower()
        self.memory_collection_name = (os.getenv("VLLM_MEMORY_COLLECTION") or "ai_chat_memory").strip()
        self.memory_dir = Path((os.getenv("VLLM_MEMORY_DIR") or "data/chroma").strip())
        self.vision_enabled = _env_bool("VLLM_VISION_ENABLED", True)
        self.vision_max_images = min(4, _env_int("VLLM_VISION_MAX_IMAGES", 3, 1))
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
        has_required_config = all([
            self.api_key, self.base_url, self.model, configured_system_prompt,
            self.user_prompt_template, self.no_context_text, self.no_memory_text,
            self.empty_user_text, self.empty_reply_text, self.rate_limit_message_template,
        ])
        if has_required_config:
            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url,
                                 timeout=self.request_timeout, max_retries=0)
            self.bot.logger.info("AiChat stage=initialization outcome=ready")
        else:
            self.bot.logger.warning("AiChat disabled: missing required VLLM_* environment variables")
        if self.s2t_enabled and self.s2t_converter is None:
            self.bot.logger.warning("AiChat s2t disabled: opencc is not installed")
        if self.memory_enabled:
            self._init_memory_store()
        if self.rag_enabled:
            self._init_rag_store()

        self.output_reviewer_thinking = _env_bool("VLLM_OUTPUT_REVIEWER_THINKING", False)
        self.prompt_budget = PromptBudget(
            self.context_window_tokens, self.input_safety_tokens, self.min_output_tokens,
            _env_int("VLLM_IMAGE_RESERVE_TOKENS", 1536, 256),
        )
        self.llm_pool = shared_llm_pool()
        self.io_pool = LLMWorkerPool(2)
        self.transport = SyncLLMTransport(
            self.client, model=self.model, temperature=self.temperature,
            disable_thinking=self.disable_thinking, timeout=self.request_timeout,
            retries=self.request_retries, retry_backoff=self.retry_backoff, pool=self.llm_pool,
            output_review_thinking=self.output_reviewer_thinking,
        )
        self.pipeline_config = PipelineConfig(
            system_rules=self.system_prompt,
            reviewer_model=(os.getenv("VLLM_REVIEWER_MODEL") or "").strip() or None,
            output_reviewer_model=(os.getenv("VLLM_OUTPUT_REVIEWER_MODEL") or "").strip() or None,
            max_tokens=self.max_tokens, final_max_tokens=self.agent_final_max_tokens,
            tool_rounds=min(6, _env_int("VLLM_TOOL_ROUNDS", 3)),
            tool_calls=min(12, _env_int("VLLM_TOOL_CALLS", 6)),
            review_tool_rounds=min(3, _env_int("VLLM_REVIEW_TOOL_ROUNDS", 1)),
            review_tool_calls=min(6, _env_int("VLLM_REVIEW_TOOL_CALLS", 2)),
            max_revisions=min(1, _env_int("VLLM_MAX_REVISIONS", 1)),
            turn_timeout=_env_float("VLLM_TURN_TIMEOUT", 600.0, 1.0),
            turn_calls=min(64, _env_int("VLLM_TURN_MAX_CALLS", 16, 1)),
            empty_reply=self.empty_reply_text,
        )
        self.pipeline = AnswerPipeline(self.transport, config=self.pipeline_config,
                                       prompt_budget=self.prompt_budget, formatter=self._format_outbound,
                                       logger=self.bot.logger)
        self.conversations = ConversationManager(
            ttl_seconds=_env_float("VLLM_SESSION_IDLE_TTL", 1800.0, 1.0),
            max_sessions=min(1000, _env_int("VLLM_MAX_SESSIONS", 100, 1)),
            max_records=min(1000, _env_int("VLLM_SESSION_MAX_RECORDS", 300, 10)),
            max_api_calls=min(12, _env_int("VLLM_CONTEXT_API_CALLS", 6, 1)),
            fetch_timeout=_env_float("VLLM_CONTEXT_FETCH_TIMEOUT", 5.0, 0.1),
        )
        self.scheduler = ChannelScheduler(
            per_channel=min(64, _env_int("VLLM_CHANNEL_QUEUE_SIZE", 8, 1)),
            total_pending=min(256, _env_int("VLLM_GLOBAL_QUEUE_SIZE", 64, 1)),
            turn_timeout=self.pipeline_config.turn_timeout,
        )
        self.tool_timeout = _env_float("VLLM_TOOL_TIMEOUT", 8.0, 0.1)
        self._known_scopes: dict[tuple[int, int], int | None] = {}
        self._reaction_cleanups: set[asyncio.Task[None]] = set()

    def _init_memory_store(self) -> None:
        try:
            chromadb = importlib.import_module("chromadb")
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(path=str(self.memory_dir))
            self.memory_collection = client.get_or_create_collection(name=self.memory_collection_name)
            self.bot.logger.info("AiChat stage=memory_initialization outcome=ready")
        except Exception as exc:
            self.memory_collection = None
            self.bot.logger.warning("AiChat stage=memory_initialization outcome=failed error=%s", type(exc).__name__)

    def _init_rag_store(self) -> None:
        try:
            self.rag_store = AdmissionGuideStore(
                directory=self.memory_dir,
                collection_name=(os.getenv("VLLM_RAG_COLLECTION") or "ai_admission_guides").strip(),
                max_bytes=_env_int("VLLM_RAG_MAX_BYTES", 10 * 1024 * 1024, 1),
                max_pages=_env_int("VLLM_RAG_MAX_PAGES", 120, 1),
                chunk_chars=_env_int("VLLM_RAG_CHUNK_CHARS", 1200, 100),
                chunk_overlap=_env_int("VLLM_RAG_CHUNK_OVERLAP", 160),
            )
            self.bot.logger.info("AiChat RAG ready")
        except Exception as exc:
            self.rag_store = None
            self.bot.logger.warning("AiChat stage=rag_initialization outcome=failed error=%s", type(exc).__name__)

    def _init_s2t_converter(self):
        try:
            return importlib.import_module("opencc").OpenCC("s2t")
        except Exception:
            return None

    def _build_user_prompt(
        self, message: discord.Message | None, cleaned_user_text: str,
        context_lines: list[str], memory_lines: list[str], user_name: str,
        rag_lines: list[str] | None = None, web_search_text: str | None = None,
    ) -> str:
        values = {
            "context": "\n".join(trim_lines(context_lines, self.context_max_chars)) or self.no_context_text,
            "memory": "\n".join(trim_lines(memory_lines, self.memory_max_chars, newest_first=False)) or self.no_memory_text,
            "rag": "\n".join(trim_lines(rag_lines or [], self.rag_max_chars, newest_first=False)) or self.no_rag_text,
            "web_search": web_search_text or self.no_web_search_text,
            "user_input": cleaned_user_text, "user_name": user_name,
        }
        try:
            rendered = self.user_prompt_template.format(**values)
        except (KeyError, ValueError) as exc:
            self.bot.logger.warning("AiChat stage=prompt_template outcome=fallback error=%s", type(exc).__name__)
            return (f"頻道上下文：\n{values['context']}\n\n長期記憶：\n{values['memory']}\n\n"
                    f"官方簡章：\n{values['rag']}\n\n網路搜尋：\n{values['web_search']}\n\n"
                    f"使用者：{user_name}\n使用者最新訊息：{cleaned_user_text}")
        missing = []
        if "{rag}" not in self.user_prompt_template:
            missing.append(f"官方簡章資料：\n{values['rag']}")
        if "{web_search}" not in self.user_prompt_template:
            missing.append(f"網路搜尋結果：\n{values['web_search']}")
        if missing:
            rendered += "\n\n" + "\n\n".join(missing) + f"\n\n使用者最新訊息：{cleaned_user_text}"
        return rendered

    def _sanitize_mass_mentions(self, text: str) -> str:
        text = re.sub(r"@everyone", "@ everyone", text, flags=re.IGNORECASE)
        text = re.sub(r"@here", "@ here", text, flags=re.IGNORECASE)
        return re.sub(r"<@([!&]?)(\d+)>", r"<＠\1\2>", text)

    def _format_outbound(self, text: str) -> str:
        text = (text or "").strip()
        if self.s2t_converter is not None:
            text = self.s2t_converter.convert(text)
        text = self._sanitize_mass_mentions(text)
        if len(text) > self.max_reply_chars:
            text = text[: self.max_reply_chars - 1] + "…"
        return text

    def _is_image_attachment(self, attachment: discord.Attachment) -> bool:
        content_type = (attachment.content_type or "").lower()
        guessed_type, _ = mimetypes.guess_type(attachment.filename or "")
        return content_type.startswith("image/") or bool(guessed_type and guessed_type.startswith("image/"))

    def _extract_image_urls(self, message: discord.Message) -> list[str]:
        if not self.vision_enabled:
            return []
        urls = []
        for attachment in message.attachments:
            if len(urls) >= self.vision_max_images:
                break
            if self._is_image_attachment(attachment) and not (attachment.size and attachment.size > self.vision_max_image_bytes):
                urls.append(attachment.url)
        for url in self.IMAGE_URL_PATTERN.findall(message.content or ""):
            if len(urls) >= self.vision_max_images:
                break
            if url.lower().split("?", 1)[0].endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                urls.append(url.rstrip(")].,>"))
        return list(dict.fromkeys(urls))

    def _bot_user_id(self) -> int | None:
        bot_id = getattr(getattr(self.bot, "user", None), "id", None)
        return bot_id if isinstance(bot_id, int) else None

    @staticmethod
    def _author_name(author: Any) -> str:
        name = str(getattr(author, "display_name", None) or getattr(author, "name", None) or "未命名成員")
        return name.replace("\r", " ").replace("\n", " ").strip()[:100] or "未命名成員"

    def _query_memory(self, query_text: str, guild_id: int, user_id: int, channel_id: int | None = None) -> list[str]:
        if self.memory_collection is None:
            return []
        try:
            filters: list[dict[str, Any]] = [{"guild_id": {"$eq": guild_id}}, {"user_id": {"$eq": user_id}}]
            if self.memory_scope != "guild" and channel_id is not None:
                filters.append({"channel_id": {"$eq": channel_id}})
            result = self.memory_collection.query(query_texts=[query_text], n_results=self.memory_top_k,
                                                  where={"$and": filters}, include=["documents", "metadatas"])
            docs = (result.get("documents") or [[]])[0]
            metadatas = (result.get("metadatas") or [[]])[0]
            lines = []
            for index, doc in enumerate(docs, start=1):
                if not isinstance(doc, str) or not doc.strip():
                    continue
                metadata = dict(metadatas[index - 1] or {}) if index - 1 < len(metadatas) else {}
                scope = "同一頻道" if self.memory_scope != "guild" and channel_id is not None else "同一伺服器"
                name = str(metadata.get("user_name") or "目前使用者").replace("\r", " ").replace("\n", " ").strip()
                lines.append(f"[歷史記憶 #{index}｜來源：{scope}｜使用者：{name}（ID：{metadata.get('user_id', user_id)}）]\n"
                             f"{_quote_text(doc)}\n[/歷史記憶 #{index}]")
            return lines
        except Exception as exc:
            self.bot.logger.warning("AiChat stage=memory_query outcome=failed error=%s", type(exc).__name__)
            return []

    def _save_memory(
        self, guild_id: int, channel_id: int, user_id: int, user_name: str,
        prompt_text: str, reply_text: str, *, request_message_id: int | None = None,
        reply_message_id: int | None = None, content_digest: str = "",
    ) -> None:
        if self.memory_collection is None:
            return
        document = (f"[記憶記錄]\n使用者顯示名稱：\n{_quote_text(user_name)}\n"
                    f"使用者訊息：\n{_quote_text(prompt_text)}\n"
                    f"管理機器人／助理回覆：\n{_quote_text(reply_text)}\n[/記憶記錄]")
        metadata: dict[str, Any] = {"schema_version": 3, "source": "ai_chat", "approved": True,
                                    "guild_id": guild_id, "channel_id": channel_id, "user_id": user_id,
                                    "user_name": user_name, "ts": int(time.time()), "content_digest": content_digest}
        if request_message_id is not None:
            metadata["request_message_id"] = request_message_id
        if reply_message_id is not None:
            metadata["reply_message_id"] = reply_message_id
        try:
            self.memory_collection.add(ids=[f"{int(time.time() * 1000)}-{uuid.uuid4().hex}"],
                                       documents=[document], metadatas=[metadata])
        except Exception as exc:
            self.bot.logger.warning("AiChat stage=memory_save outcome=failed error=%s", type(exc).__name__)

    _response_message = staticmethod(response_message)
    _response_text = staticmethod(response_text)
    _extract_tool_calls = staticmethod(extract_tool_calls)
    _is_context_error = staticmethod(SyncLLMTransport.is_context_error)
    _is_tool_error = staticmethod(SyncLLMTransport.is_tool_error)
    _is_thinking_option_error = staticmethod(SyncLLMTransport.is_thinking_error)

    def _compat_budget(self) -> PromptBudget:
        return PromptBudget(self.context_window_tokens, self.input_safety_tokens,
                            getattr(self, "min_output_tokens", 96))

    def _estimate_message_tokens(self, messages: list[dict[str, Any]]) -> int:
        return self._compat_budget().estimate(messages)

    def _prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self._compat_budget().allocate(messages)
        return messages

    def _max_tokens_for_messages(self, messages: list[dict[str, Any]], configured_max_tokens: int | None = None) -> int:
        return self._compat_budget().allocate(
            messages, configured_max_tokens=self.max_tokens if configured_max_tokens is None else configured_max_tokens,
        ).output_tokens

    def _call_vllm(self, messages: list[dict[str, Any]], *, use_tools: bool = False,
                   max_tokens_override: int | None = None) -> Any:
        """Legacy transport entry point, never an outbound publication path."""
        transport = getattr(self, "transport", None) or SyncLLMTransport(
            self.client, model=self.model, temperature=self.temperature,
            disable_thinking=getattr(self, "disable_thinking", True),
            timeout=getattr(self, "request_timeout", 60.0), retries=self.request_retries,
            retry_backoff=getattr(self, "retry_backoff", 0.8),
        )
        tools = None
        if use_tools:
            raise ValueError("Tools require a guild-bound pipeline request")
        prepared = self._prepare_messages(messages)
        return transport.call_sync(prepared, budget=TurnBudget(), tools=tools,
                                   max_tokens=self._max_tokens_for_messages(prepared, max_tokens_override))

    def _can_manage_rag(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if getattr(getattr(member, "guild_permissions", None), "manage_guild", False):
            return True
        support_ids = set(getattr(getattr(self.bot, "settings", None), "support_role_ids", []) or [])
        return any(getattr(role, "id", None) in support_ids for role in getattr(member, "roles", []))

    def _is_llm_disabled_channel(self, channel_id: int, parent_channel_id: int | None = None) -> bool:
        disabled = getattr(getattr(self.bot, "settings", None), "llm_disabled_channel_ids", []) or []
        return channel_id in disabled or parent_channel_id in disabled

    def _scope_allowed(self, scope: ConversationScope, message: discord.Message) -> bool:
        channel = message.channel
        guild = message.guild
        if (guild is None or scope.guild_id != guild.id or scope.channel_id != channel.id
                or scope.parent_channel_id != getattr(channel, "parent_id", None)
                or self._is_llm_disabled_channel(channel.id, getattr(channel, "parent_id", None))):
            return False
        if getattr(getattr(channel, "guild", guild), "id", None) != guild.id or guild.me is None:
            return False
        current_author = message.author
        get_member = getattr(guild, "get_member", None)
        if callable(get_member):
            current_author = get_member(message.author.id)
            if current_author is None:
                return False
        permissions_for = getattr(channel, "permissions_for", None)
        if callable(permissions_for):
            for member in (guild.me, current_author):
                permissions = permissions_for(member)
                if not permissions.view_channel or not permissions.read_message_history:
                    return False
            permissions = permissions_for(guild.me)
            if isinstance(channel, discord.Thread):
                if not permissions.send_messages_in_threads:
                    return False
            elif not permissions.send_messages:
                return False
        return True

    def _log_turn(self, message: discord.Message, stage: str, outcome: str,
                  error: BaseException | None = None) -> None:
        ids = [getattr(message, "id", None), getattr(getattr(message, "guild", None), "id", None),
               getattr(getattr(message, "channel", None), "id", None)]
        message_id, guild_id, channel_id = (value if type(value) is int else 0 for value in ids)
        log = self.bot.logger.warning if error is not None else self.bot.logger.info
        log("AiChat message_id=%s guild_id=%s channel_id=%s stage=%s outcome=%s exception_class=%s",
            message_id, guild_id, channel_id, stage, outcome,
            type(error).__name__ if error is not None else "none")

    async def _send_notice(self, message: discord.Message, notice: ServiceNotice,
                           publication: _PublicationAttempt) -> None:
        if not publication.claim():
            self._log_turn(message, "notice", "already_attempted")
            return
        self._log_turn(message, "notice", "started")
        try:
            await message.reply(notice.render(), mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except asyncio.CancelledError:
            self._log_turn(message, "notice", "cancelled")
            raise
        except Exception as exc:
            self._log_turn(message, "notice", "failed", exc)
            raise
        self._log_turn(message, "notice", "sent")

    async def _clear_thinking_reaction(self, message: discord.Message) -> None:
        try:
            await asyncio.wait_for(
                message.remove_reaction(self.THINKING_EMOJI, self.bot.user), timeout=self.REACTION_TIMEOUT,
            )
        except Exception as exc:
            self._log_turn(message, "reaction_cleanup", "failed", exc)

    async def _process_turn_with_reaction(
        self, message: discord.Message, scope: ConversationScope, question: str, mentioned: bool,
        publication: _PublicationAttempt,
    ) -> PipelineResult:
        if not self._scope_allowed(scope, message):
            return PipelineResult("stale")
        if mentioned:
            self._log_turn(message, "turn", "started")
        cleanup = mentioned
        try:
            if mentioned:
                try:
                    await asyncio.wait_for(message.add_reaction(self.THINKING_EMOJI), timeout=self.REACTION_TIMEOUT)
                except (discord.Forbidden, discord.NotFound):
                    cleanup = False
                except Exception as exc:
                    self._log_turn(message, "reaction_add", "failed", exc)
            return await self._process_turn(message, scope, question, mentioned, publication)
        finally:
            if cleanup:
                task = asyncio.create_task(self._clear_thinking_reaction(message))
                self._reaction_cleanups.add(task)
                task.add_done_callback(self._reaction_cleanups.discard)
                await asyncio.shield(task)

    async def _process_turn(self, message: discord.Message, scope: ConversationScope, question: str,
                            mentioned: bool, publication: _PublicationAttempt) -> PipelineResult:
        budget = TurnBudget(self.pipeline_config.turn_timeout, self.pipeline_config.turn_calls)
        allowed = lambda target: self._scope_allowed(target, message)
        if not allowed(scope):
            return PipelineResult("stale")
        if time.time() < self._rate_limited_until:
            if mentioned:
                await self._send_notice(message, ServiceNotice(NoticeKind.RATE_LIMITED,
                                        int(self._rate_limited_until - time.time())), publication)
            return PipelineResult("cooldown")
        context = await self.conversations.collect(
            message, bot_user_id=self._bot_user_id(),
            token_budget=max(256, self.context_window_tokens // 2), is_allowed=allowed,
        )
        if not allowed(scope) or not self.conversations.is_current(context):
            return PipelineResult("stale")
        self.conversations.remember(message, bot_user_id=self._bot_user_id())
        user_name = self._author_name(message.author)

        async def load_memory() -> list[str]:
            if not self.memory_enabled or self.memory_collection is None:
                return []
            return await self.io_pool.run(
                lambda: self._query_memory(question, scope.guild_id, message.author.id, scope.channel_id), budget,
            )

        prompt = self._build_user_prompt(message, question, ["授權回覆鏈另以 JSON 提供。"],
                                         ["若有授權長期記憶，另以 JSON memory 提供。"], user_name)
        request = PipelineRequest(message.id, scope.guild_id, scope.channel_id, message.author.id,
                                  user_name, question, context.as_data(), tuple(self._extract_image_urls(message)),
                                  (), prompt)
        registry = ReadonlyToolRegistry(
            guild_id=scope.guild_id, rag_store=self.rag_store, rag_enabled=self.rag_enabled,
            web_search=self.web_search.search_result, web_enabled=self.web_search_enabled,
            max_result_chars=min(12000, max(self.rag_max_chars, self.web_search_max_chars)),
            timeout=min(self.tool_timeout, budget.remaining()), max_top_k=min(10, self.rag_top_k),
        )
        async with message.channel.typing():
            result = await self.pipeline.run(request, registry, budget=budget, memory_loader=load_memory)
        if not allowed(scope) or not self.conversations.is_current(context):
            return PipelineResult("stale")
        if result.approved is not None and not result.approved.is_intact():
            self._log_turn(message, "integrity", "rejected")
            result = PipelineResult("rejected")
        if result.approved is not None:
            approved = result.approved
            budget.remaining()
            if not publication.claim():
                return result
            self._log_turn(message, "answer", "started")
            try:
                sent = await message.reply(approved.candidate.body, mention_author=False,
                                           allowed_mentions=discord.AllowedMentions.none())
            except asyncio.CancelledError:
                self._log_turn(message, "answer", "cancelled")
                raise
            except Exception as exc:
                self._log_turn(message, "answer", "failed", exc)
                raise
            self._log_turn(message, "answer", "sent")
            if not allowed(scope) or not self.conversations.is_current(context):
                return PipelineResult("stale")
            self.conversations.remember(sent, bot_user_id=self._bot_user_id())
            if self.memory_enabled and approved.review.memory_safe:
                try:
                    await self.io_pool.run(lambda: self._save_memory(
                        scope.guild_id, scope.channel_id, message.author.id, user_name, question, approved.candidate.body,
                        request_message_id=message.id, reply_message_id=getattr(sent, "id", None),
                        content_digest=approved.candidate.content_digest,
                    ), budget)
                except (TimeoutError, RuntimeError) as exc:
                    self._log_turn(message, "memory_save", "skipped", exc)
        elif result.outcome == "rejected":
            if mentioned:
                await self._send_notice(message, ServiceNotice(NoticeKind.NOT_APPROVED), publication)
        elif result.notice is not None:
            if result.notice.kind in {NoticeKind.RATE_LIMITED, NoticeKind.UNAVAILABLE}:
                self._rate_limited_until = time.time() + self.rate_limit_cooldown
            await self._send_notice(message, result.notice, publication)
        return result

    def _invalidate_channel(self, guild_id: int, channel_id: int) -> None:
        conversations = getattr(self, "conversations", None)
        if conversations is not None:
            conversations.invalidate_channel(guild_id, channel_id, include_children=True)
        scheduler = getattr(self, "scheduler", None)
        scopes = getattr(self, "_known_scopes", {})
        if scheduler is not None:
            scheduler.invalidate((guild_id, channel_id))
            for key, parent_id in list(scopes.items()):
                if key[0] == guild_id and (key[1] == channel_id or parent_id == channel_id):
                    if key != (guild_id, channel_id):
                        scheduler.invalidate(key)
                    scopes.pop(key, None)

    @app_commands.command(name="llm_channel", description="設定指定頻道是否允許使用 LLM")
    @app_commands.guild_only()
    @app_commands.describe(channel="要設定的文字頻道", enabled="是否允許在此頻道使用 LLM")
    async def llm_channel(self, interaction: discord.Interaction, channel: discord.TextChannel, enabled: bool) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("請在伺服器內使用此指令。", ephemeral=True)
            return
        if not self._can_manage_rag(interaction):
            await interaction.response.send_message("需要伺服器管理權限或客服身分組。", ephemeral=True)
            return
        if getattr(getattr(channel, "guild", None), "id", None) != guild.id:
            await interaction.response.send_message("頻道必須屬於目前的伺服器。", ephemeral=True)
            return
        settings = getattr(self.bot, "settings", None)
        if settings is None:
            await interaction.response.send_message("找不到 Bot 設定，無法變更 LLM 頻道。", ephemeral=True)
            return
        disabled_ids = {int(channel_id) for channel_id in getattr(settings, "llm_disabled_channel_ids", []) or []}
        if (channel.id in disabled_ids) == (not enabled):
            state = "停用" if not enabled else "啟用"
            await interaction.response.send_message(f"{channel.mention} 已經是 LLM {state}狀態。", ephemeral=True)
            return
        settings_path = getattr(self.bot, "settings_path", None) or getattr(settings, "config_path", None)
        if settings_path is None:
            await interaction.response.send_message("找不到 Bot 設定檔路徑，無法儲存變更。", ephemeral=True)
            return
        if enabled:
            disabled_ids.discard(channel.id)
        else:
            disabled_ids.add(channel.id)
        try:
            saved_ids = save_llm_disabled_channel_ids(settings_path, disabled_ids)
        except (OSError, TypeError, ValueError) as exc:
            self.bot.logger.warning("AiChat stage=channel_settings outcome=failed error=%s", type(exc).__name__)
            await interaction.response.send_message(f"儲存 LLM 頻道設定失敗：{exc}", ephemeral=True)
            return
        settings.llm_disabled_channel_ids = saved_ids
        self._invalidate_channel(guild.id, channel.id)
        state = "已啟用" if enabled else "已停用"
        details = "" if enabled else "此頻道訊息也不會寫入長期記憶。"
        await interaction.response.send_message(f"{channel.mention} 的 LLM {state}。{details}", ephemeral=True)

    @app_commands.command(name="rag_add", description="加入一份供 AI 查詢的招生簡章（管理員）")
    @app_commands.describe(attachment="PDF 或 UTF-8 文字檔", title="簡章名稱，可省略")
    async def rag_add(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None,
                      title: str | None = None) -> None:
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
        if attachment.size and attachment.size > _env_int("VLLM_RAG_MAX_BYTES", 10 * 1024 * 1024, 1):
            await interaction.response.send_message("簡章檔案過大，請先壓縮或分割檔案。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = await attachment.read()
            result = await asyncio.to_thread(self.rag_store.ingest_bytes, guild_id=interaction.guild.id, data=data,
                                             filename=attachment.filename, title=title or "",
                                             source_url=str(getattr(attachment, "url", "") or ""))
        except Exception as exc:
            self.bot.logger.warning("AiChat stage=rag_ingest outcome=failed error=%s", type(exc).__name__)
            await interaction.followup.send(f"簡章匯入失敗：{exc}", ephemeral=True)
            return
        await interaction.followup.send(f"已加入「{result.title}」，共 {result.chunk_count} 個段落（文件 ID：`{result.document_id}`）。",
                                        ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        bot_id = self._bot_user_id()
        mentioned = bot_id is not None and any(
            getattr(member, "id", None) == bot_id for member in getattr(message, "mentions", []))
        publication = _PublicationAttempt()

        def skipped(outcome: str) -> None:
            if mentioned:
                self._log_turn(message, "gate", outcome)

        try:
            if self.client is None:
                skipped("client_unavailable")
                return
            if message.guild is None:
                skipped("dm")
                return
            if message.author.bot:
                skipped("bot")
                return
            if getattr(message, "webhook_id", None) is not None:
                skipped("webhook")
                return
            channel = message.channel
            scope = ConversationScope(message.guild.id, channel.id, getattr(channel, "parent_id", None))
            if not self._scope_allowed(scope, message):
                skipped("scope_denied")
                return
            if getattr(message, "role_mentions", []) or getattr(message, "mention_everyone", False):
                skipped("mass_mention")
                return
            if self.mention_only and not mentioned:
                return
            raw_text = (message.content or "").strip()
            if not raw_text and not getattr(message, "attachments", []):
                skipped("empty")
                return
            get_context = getattr(self.bot, "get_context", None)
            if callable(get_context) and (await get_context(message)).valid:
                skipped("command")
                return
            if type(getattr(message, "id", None)) is not int:
                skipped("invalid_message_id")
                return
            question = re.sub(rf"<@!?{bot_id}>", "", raw_text).strip() or self.empty_user_text
            key = (scope.guild_id, scope.channel_id)
            self._known_scopes[key] = scope.parent_channel_id
            if len(self._known_scopes) > self.conversations.max_sessions:
                oldest = next(iter(self._known_scopes))
                self._known_scopes.pop(oldest, None)
            guard = self.conversations.capture_guard(scope) if mentioned else None
            if mentioned:
                self._log_turn(message, "scheduler", "submitted")
            result = await self.scheduler.submit(key, message.id, lambda: self._process_turn_with_reaction(
                message, scope, question, mentioned, publication))
            if mentioned:
                stage = "scheduler" if result.outcome in {"scheduler_timeout", "cancelled", "stale", "duplicate", "busy"} else "turn"
                self._log_turn(message, stage, result.outcome)
            if mentioned and result.outcome in {"busy", "scheduler_timeout"} and result.notice is not None:
                if publication.attempted:
                    self._log_turn(message, "notice", "already_attempted")
                elif not self._scope_allowed(scope, message) or not self.conversations.is_current(guard):
                    self._log_turn(message, "notice", "stale")
                else:
                    await self._send_notice(message, result.notice, publication)
        except asyncio.CancelledError:
            if mentioned:
                self._log_turn(message, "scheduler", "cancelled")
            raise
        except Exception as exc:
            self._log_turn(message, "listener", "failed", exc)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if after.guild is not None:
            self.conversations.invalidate_message(after.guild.id, after.channel.id, after.id)
            self.scheduler.invalidate((after.guild.id, after.channel.id))
            scope = ConversationScope(after.guild.id, after.channel.id, getattr(after.channel, "parent_id", None))
            if self._scope_allowed(scope, after):
                self.conversations.remember(after, bot_user_id=self._bot_user_id())

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if payload.guild_id is not None:
            self.conversations.invalidate_message(payload.guild_id, payload.channel_id, payload.message_id)
            self.scheduler.invalidate((payload.guild_id, payload.channel_id))

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is not None:
            self.conversations.invalidate_message(payload.guild_id, payload.channel_id, payload.message_id, deleted=True)
            self.scheduler.invalidate((payload.guild_id, payload.channel_id))

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.guild_id is not None:
            for message_id in payload.message_ids:
                self.conversations.invalidate_message(payload.guild_id, payload.channel_id, message_id, deleted=True)
            self.scheduler.invalidate((payload.guild_id, payload.channel_id))

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: Any, after: Any) -> None:
        self._invalidate_channel(after.guild.id, after.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: Any) -> None:
        self._invalidate_channel(channel.guild.id, channel.id)

    @commands.Cog.listener()
    async def on_thread_update(self, before: Any, after: Any) -> None:
        self._invalidate_channel(after.guild.id, after.id)

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: Any) -> None:
        self._invalidate_channel(thread.guild.id, thread.id)

    @commands.Cog.listener()
    async def on_raw_thread_update(self, payload: discord.RawThreadUpdateEvent) -> None:
        self._invalidate_channel(payload.guild_id, payload.thread_id)

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        self._invalidate_channel(payload.guild_id, payload.thread_id)

    def _invalidate_guild(self, guild_id: int) -> None:
        for key in list(self._known_scopes):
            if key[0] == guild_id:
                self._invalidate_channel(*key)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: Any, after: Any) -> None:
        self._invalidate_guild(after.guild.id)

    @commands.Cog.listener()
    async def on_member_update(self, before: Any, after: Any) -> None:
        if before.roles != after.roles:
            self._invalidate_guild(after.guild.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: Any) -> None:
        self._invalidate_guild(member.guild.id)

    @commands.Cog.listener()
    async def on_thread_member_remove(self, member: Any) -> None:
        self._invalidate_channel(member.parent.guild.id, member.parent.id)

    @commands.Cog.listener()
    async def on_raw_thread_member_remove(self, payload: discord.RawThreadMembersUpdate) -> None:
        self._invalidate_channel(payload.guild_id, payload.thread_id)

    @commands.Cog.listener()
    async def on_thread_remove(self, thread: Any) -> None:
        self._invalidate_channel(thread.guild.id, thread.id)

    def cog_unload(self) -> None:
        self.scheduler.close()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AiChat(bot))
