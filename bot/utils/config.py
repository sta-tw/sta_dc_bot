from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv

load_dotenv()


@dataclass(slots=True)
class TicketCategory:

    label: str
    value: str
    description: str
    channel_prefix: str
    ai_hint: str | None = None


@dataclass(slots=True)
class Settings:

    guild_id: int
    welcome_channel_id: int
    ticket_category_id: int
    ticket_panel_channel_id: int
    support_role_ids: list[int]
    transcript_dir: Path
    extensions: list[str] = field(default_factory=list)
    ticket_categories: list[TicketCategory] = field(default_factory=list)
    faq_content: str = ""
    blocked_keywords: list[str] = field(default_factory=list)
    llm_model: str = "gemini-2.0-flash"
    llm_persona_prompt: str = "你是WinterCamp 2026的客服助理，請以親切專業的語氣提供協助。"
    llm_max_sentences: int = 3
    llm_api_keys: list[str] = field(default_factory=list)
    config_channel_id: int | None = None
    config_path: Path | None = None
    moderation_spam_interval_seconds: int = 10
    moderation_spam_max_messages: int = 5
    moderation_delete_invite_links: bool = True
    moderation_enable_rate_limit: bool = True
    moderation_duplicate_window_seconds: int = 30
    moderation_duplicate_max_repeats: int = 3
    moderation_duplicate_min_length: int = 6
    moderation_gibberish_enabled: bool = True
    moderation_gibberish_min_length: int = 40
    moderation_gibberish_long_run_no_space: int = 50
    moderation_gibberish_ascii_min_length: int = 30

    @classmethod
    def from_file(cls, path: Path) -> "Settings":
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        transcript_dir = Path(data["transcript_dir"]).expanduser().resolve()
        transcript_dir.mkdir(parents=True, exist_ok=True)

        ticket_categories = [
            TicketCategory(
                label=str(item.get("label", "未命名分類")),
                value=str(item.get("value", f"cat-{index}")),
                description=str(item.get("description", "")),
                channel_prefix=str(item.get("channel_prefix", "ticket")),
                ai_hint=(str(item["ai_hint"]) if item.get("ai_hint") else None),
            )
            for index, item in enumerate(data.get("ticket_categories", []))
        ]

        faq_content = str(data.get("faq_content", ""))

        blocked_keywords = [term.strip().lower() for term in data.get("blocked_keywords", []) if term.strip()]

        llm_settings = data.get("llm", {})
        default_persona = (
            require_env("LLM_SYSTEM_PROMPT"))
        persona_prompt = str(llm_settings.get("persona_prompt", default_persona))
        llm_model = str(llm_settings.get("model", "gemini-1.5-flash"))
        llm_max_sentences = int(llm_settings.get("max_sentences", 3))
        llm_api_keys = [
            key.strip()
            for key in llm_settings.get("api_keys", [])
            if isinstance(key, str) and key.strip()
        ]
        config_channel_id = int(data.get("config_channel_id", 0)) or None

        moderation = data.get("moderation", {})
        spam_interval = int(moderation.get("spam_interval_seconds", 10))
        spam_max = int(moderation.get("spam_max_messages", 5))
        delete_inv = bool(moderation.get("delete_invite_links", True))
        enable_rate = bool(moderation.get("enable_rate_limit", True))
        dup_window = int(moderation.get("duplicate_window_seconds", 30))
        dup_repeats = int(moderation.get("duplicate_max_repeats", 3))
        dup_min_len = int(moderation.get("duplicate_min_length", 6))
        gib_enable = bool(moderation.get("gibberish_enabled", True))
        gib_min_len = int(moderation.get("gibberish_min_length", 40))
        gib_long_run = int(moderation.get("gibberish_long_run_no_space", 50))
        gib_ascii_min = int(moderation.get("gibberish_ascii_min_length", 30))

        return cls(
            guild_id=int(data["guild_id"]),
            welcome_channel_id=int(data["welcome_channel_id"]),
            ticket_category_id=int(data["ticket_category_id"]),
            ticket_panel_channel_id=int(data.get("ticket_panel_channel_id", 0)),
            support_role_ids=[int(role_id) for role_id in data.get("support_role_ids", [])],
            transcript_dir=transcript_dir,
            extensions=list(data.get("extensions", [])),
            ticket_categories=ticket_categories,
            faq_content=faq_content,
            blocked_keywords=blocked_keywords,
            llm_model=llm_model,
            llm_persona_prompt=persona_prompt,
            llm_max_sentences=max(1, llm_max_sentences),
            llm_api_keys=llm_api_keys,
            config_channel_id=config_channel_id,
            config_path=path.resolve(),
            moderation_spam_interval_seconds=max(3, spam_interval),
            moderation_spam_max_messages=max(3, spam_max),
            moderation_delete_invite_links=delete_inv,
            moderation_enable_rate_limit=enable_rate,
            moderation_duplicate_window_seconds=max(5, dup_window),
            moderation_duplicate_max_repeats=max(2, dup_repeats),
            moderation_duplicate_min_length=max(1, dup_min_len),
            moderation_gibberish_enabled=gib_enable,
            moderation_gibberish_min_length=max(10, gib_min_len),
            moderation_gibberish_long_run_no_space=max(20, gib_long_run),
            moderation_gibberish_ascii_min_length=max(10, gib_ascii_min),
        )

    def find_blocked_keyword(self, text: str) -> str | None:
        lowered = text.lower()
        for keyword in self.blocked_keywords:
            if keyword and keyword in lowered:
                return keyword
        return None

    def iter_support_roles(self) -> Iterable[int]:
        return tuple(self.support_role_ids)

    def find_category(self, value: str) -> TicketCategory | None:
        for category in self.ticket_categories:
            if category.value == value:
                return category
        return None


def require_env(name: str) -> str:
    from os import getenv

    result = getenv(name)
    if result is None or not result.strip():
        raise RuntimeError(f"Environment variable {name} is not set")
    return result
