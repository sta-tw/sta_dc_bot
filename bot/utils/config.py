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
class PromptConfig:

    system_prompt: str
    style_rules: str
    context_preamble: str
    response_rules: str

    @classmethod
    def from_env(cls) -> "PromptConfig":
        return cls(
            system_prompt=get_env_or_default("LLM_SYSTEM_PROMPT", ""),
            style_rules=get_env_or_default("LLM_STYLE_RULES", ""),
            context_preamble=get_env_or_default("LLM_CONTEXT_PREAMBLE", ""),
            response_rules=get_env_or_default("LLM_RESPONSE_RULES", ""),
        )

@dataclass(slots=True)
class Settings:

    guild_id: int
    welcome_channel_id: int
    ticket_category_id: int
    ticket_panel_channel_id: int
    support_role_ids: list[int]
    transcript_dir: Path
    llm_model: str
    prompt_config: PromptConfig
    extensions: list[str] = field(default_factory=list)
    ticket_categories: list[TicketCategory] = field(default_factory=list)
    faq_content: str = ""
    blocked_keywords: list[str] = field(default_factory=list)
    llm_max_sentences: int = 3
    llm_api_keys: list[str] = field(default_factory=list)
    config_channel_id: int | None = None
    starboard_channel_id: int | None = None
    starboard_min_reactions: int = 3
    starboard_emoji: str = "⭐"
    repeater_filtered_category_ids: list[int] = field(default_factory=list)
    repeater_filtered_response: str = "#拒絕電神崇拜，從你我做起"
    quote_api_base_url: str = ""
    quote_api_timeout: int = 15
    quote_api_user_agent: str = ""
    config_path: Path | None = None

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
        llm_model = str(llm_settings.get("model", "gemini-1.5-flash"))
        llm_max_sentences = int(llm_settings.get("max_sentences", 3))
        llm_api_keys = [
            key.strip()
            for key in llm_settings.get("api_keys", [])
            if isinstance(key, str) and key.strip()
        ]
        config_channel_id = int(data.get("config_channel_id", 0)) or None
        starboard_channel_id = int(data.get("starboard_channel_id", 0)) or None
        starboard_min_reactions = max(1, int(data.get("starboard_min_reactions", 3)))
        starboard_emoji = str(data.get("starboard_emoji", "⭐")).strip() or "⭐"
        repeater_filtered_category_ids = [
            int(category_id)
            for category_id in data.get("repeater_filtered_category_ids", [])
            if str(category_id).strip()
        ]
        repeater_filtered_response = str(
            data.get("repeater_filtered_response", "#拒絕電神崇拜，從你我做起")
        ).strip() or "#拒絕電神崇拜，從你我做起"
        quote_api_base_url = get_env_or_default("QUOTE_API_BASE_URL", "").strip().split("#", 1)[0].rstrip("/")
        quote_api_timeout = max(5, int(get_env_or_default("QUOTE_API_TIMEOUT", "15")))
        quote_api_user_agent = get_env_or_default("QUOTE_API_USER_AGENT", "").strip()
        prompt_config = PromptConfig.from_env()

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
            prompt_config=prompt_config,
            llm_max_sentences=max(1, llm_max_sentences),
            llm_api_keys=llm_api_keys,
            config_channel_id=config_channel_id,
            starboard_channel_id=starboard_channel_id,
            starboard_min_reactions=starboard_min_reactions,
            starboard_emoji=starboard_emoji,
            repeater_filtered_category_ids=repeater_filtered_category_ids,
            repeater_filtered_response=repeater_filtered_response,
            quote_api_base_url=quote_api_base_url,
            quote_api_timeout=quote_api_timeout,
            quote_api_user_agent=quote_api_user_agent,
            config_path=path.resolve(),
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

def get_env_or_default(name: str, default: str = "") -> str:
    from os import getenv
    result = getenv(name)
    return result if result and result.strip() else default
