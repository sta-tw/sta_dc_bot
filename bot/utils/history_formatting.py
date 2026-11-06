"""Keep source text readable and within Discord embed limits."""

import random

import discord
from discord.utils import escape_markdown, escape_mentions

from .history_today import CATEGORY_LABELS, HistoryResult


def clean_text(text: str) -> str:
    return escape_markdown(escape_mentions(" ".join(text.split())))


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_embed(
    result: HistoryResult, count: int = 5, *, random_count: bool = False
) -> discord.Embed:
    if not 1 <= count <= 10:
        raise ValueError("筆數必須介於 1 與 10。")
    selected = (
        random.sample(result.items, min(5, len(result.items)))
        if random_count
        else result.items[:count]
    )
    entries = []
    for item in selected:
        year = f"**{shorten(clean_text(item.year), 40)}**｜" if item.year else ""
        entries.append(f"• {year}{shorten(clean_text(item.text), 310)}")
    description = "\n\n".join(entries) or "維基百科此日期沒有列出這個分類的資料。"
    embed = discord.Embed(
        title=f"歷史上的今天｜{result.month} 月 {result.day} 日",
        description=description,
        color=0xD5A448,
        url=result.source_url,
    )
    embed.set_author(name=CATEGORY_LABELS[result.category])
    embed.add_field(
        name="閱讀來源",
        value=f"[維基百科：{result.month}月{result.day}日]({result.source_url})",
        inline=False,
    )
    embed.set_footer(text="資料：中文維基百科 · CC BY-SA · 點擊來源查看完整內容與版本")
    return embed
