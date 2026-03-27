from __future__ import annotations

import asyncio
import importlib
import os
import re
import time
import uuid
from pathlib import Path

import discord
from openai import OpenAI, RateLimitError
from discord.ext import commands


class AiChat(commands.Cog):
    MASS_MENTION_TOKENS: tuple[str, str] = ("@everyone", "@here")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.api_key = (os.getenv("VLLM_API_KEY") or "").strip()
        self.base_url = os.getenv("VLLM_BASE_URL", "").strip()
        self.model = os.getenv("VLLM_MODEL", "").strip()
        self.system_prompt = (os.getenv("VLLM_SYSTEM_PROMPT") or "").strip()
        self.user_prompt_template = (os.getenv("VLLM_USER_PROMPT_TEMPLATE") or "").strip()
        self.no_context_text = (os.getenv("VLLM_NO_CONTEXT_TEXT") or "").strip()
        self.no_memory_text = (os.getenv("VLLM_NO_MEMORY_TEXT") or "").strip()
        self.empty_user_text = (os.getenv("VLLM_EMPTY_USER_TEXT") or "").strip()
        self.empty_reply_text = (os.getenv("VLLM_EMPTY_REPLY_TEXT") or "").strip()
        self.rate_limit_message_template = (os.getenv("VLLM_RATE_LIMIT_MESSAGE") or "").strip()
        self.context_limit = max(1, int(os.getenv("VLLM_CONTEXT_MESSAGES") or "8"))
        self.max_reply_chars = max(200, int(os.getenv("VLLM_MAX_REPLY_CHARS") or "1800"))
        self.temperature = max(0.0, min(2.0, float(os.getenv("VLLM_TEMPERATURE") or "0.7")))
        self.max_tokens = max(50, int(os.getenv("VLLM_MAX_TOKENS") or "512"))
        self.rate_limit_cooldown = max(5, int(os.getenv("VLLM_RATE_LIMIT_COOLDOWN") or "60"))
        self.s2t_enabled = (os.getenv("VLLM_S2T_ENABLED") or "1").strip() == "1"
        self.memory_enabled = (os.getenv("VLLM_MEMORY_ENABLED") or "1").strip() == "1"
        self.memory_top_k = max(1, int(os.getenv("VLLM_MEMORY_TOP_K") or "3"))
        self.memory_collection_name = (os.getenv("VLLM_MEMORY_COLLECTION") or "ai_chat_memory").strip()
        self.memory_dir = Path((os.getenv("VLLM_MEMORY_DIR") or "data/chroma").strip())
        self._rate_limited_until = 0.0
        self.memory_collection = None
        self.s2t_converter = self._init_s2t_converter() if self.s2t_enabled else None

        self.client: OpenAI | None = None
        has_required_config = all(
            [
                self.api_key,
                self.base_url,
                self.model,
                self.system_prompt,
                self.user_prompt_template,
                self.no_context_text,
                self.no_memory_text,
                self.empty_user_text,
                self.empty_reply_text,
                self.rate_limit_message_template,
            ]
        )
        if has_required_config:
            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            self.bot.logger.info("AiChat initialized: base_url=%s, model=%s", self.base_url, self.model)
        else:
            self.bot.logger.warning("AiChat disabled: missing required VLLM_* environment variables")

        if self.s2t_enabled and self.s2t_converter is None:
            self.bot.logger.warning("AiChat s2t disabled: opencc is not installed")

        if self.memory_enabled:
            self._init_memory_store()

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

    def _init_s2t_converter(self):
        try:
            opencc_module = importlib.import_module("opencc")
            return opencc_module.OpenCC("s2t")
        except Exception:
            return None

    def _build_user_prompt(
        self,
        message: discord.Message,
        cleaned_user_text: str,
        context_lines: list[str],
        memory_lines: list[str],
        user_name: str,
    ) -> str:
        context_text = "\n".join(context_lines) if context_lines else self.no_context_text
        memory_text = "\n".join(memory_lines) if memory_lines else self.no_memory_text
        return self.user_prompt_template.format(
            context=context_text,
            memory=memory_text,
            user_input=cleaned_user_text,
            user_name=user_name,
        )

    def _sanitize_mass_mentions(self, text: str) -> str:
        sanitized = text
        sanitized = re.sub(r"@everyone", "@ everyone", sanitized, flags=re.IGNORECASE)
        sanitized = re.sub(r"@here", "@ here", sanitized, flags=re.IGNORECASE)
        return sanitized

    def _is_identity_question(self, text: str) -> bool:
        normalized = text.strip().lower().replace("？", "?")
        patterns = (
            r"^我是誰\??$",
            r"^我是谁\??$",
            r"^who am i\??$",
            r"^whoami\??$",
        )
        return any(re.fullmatch(pattern, normalized) for pattern in patterns)

    async def _collect_context(self, message: discord.Message) -> list[str]:
        context_messages: list[discord.Message] = []
        async for item in message.channel.history(limit=self.context_limit + 1, before=message, oldest_first=False):
            if item.author.bot:
                continue
            text = (item.content or "").strip()
            if not text:
                continue
            context_messages.append(item)

        context_messages.reverse()
        lines: list[str] = []
        for item in context_messages:
            lines.append(f"[{item.author.display_name}] {item.content}")
        return lines

    def _call_vllm(self, messages: list[dict]) -> str:

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            stream=False,
        )
        return response.choices[0].message.content if response.choices else ""

    def _query_memory(self, query_text: str, guild_id: int, user_id: int) -> list[str]:
        if self.memory_collection is None:
            return []

        try:
            result = self.memory_collection.query(
                query_texts=[query_text],
                n_results=self.memory_top_k,
                where={
                    "$and": [
                        {"guild_id": {"$eq": guild_id}},
                        {"user_id": {"$eq": user_id}},
                    ]
                },
            )
            docs = result.get("documents", [[]])
            return [doc for doc in (docs[0] if docs else []) if isinstance(doc, str) and doc.strip()]
        except Exception as exc:
            self.bot.logger.warning("AiChat memory query failed: %s", exc)
            return []

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

        memory_doc = f"User({user_name}): {prompt_text}\nAssistant: {reply_text}"
        record_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex}"
        try:
            self.memory_collection.add(
                ids=[record_id],
                documents=[memory_doc],
                metadatas=[
                    {
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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if self.client is None:
            return
        if message.guild is None or message.author.bot:
            return

        me = message.guild.me
        if me is None:
            return

        
        if me not in message.mentions:
            return
        if message.role_mentions or message.mention_everyone:
            return

        user_text = (message.content or "").strip()
        if not user_text:
            return

        mention_pattern = rf"<@!?{self.bot.user.id}>"
        cleaned_user_text = re.sub(mention_pattern, "", user_text).strip()
        if not cleaned_user_text:
            cleaned_user_text = self.empty_user_text

        user_name = message.author.display_name
        query_text = f"[{user_name}] {cleaned_user_text}"

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
            context_lines = await self._collect_context(message)
            memory_lines = await asyncio.to_thread(
                self._query_memory,
                query_text,
                message.guild.id,
                message.author.id,
            )
            prompt = self._build_user_prompt(
                message,
                cleaned_user_text,
                context_lines,
                memory_lines,
                user_name,
            )

            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt},
            ]

            async with message.channel.typing():
                content = await asyncio.to_thread(self._call_vllm, messages)

            content = (content or "").strip()
            if not content:
                content = self.empty_reply_text

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
        except Exception as exc:
            self.bot.logger.exception("AiChat failed", exc_info=exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AiChat(bot))
