from __future__ import annotations

import io
import re
import textwrap

import discord
from discord.ext import commands

try:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
except Exception:
    Image = None
    ImageDraw = None
    ImageEnhance = None
    ImageFilter = None
    ImageFont = None


class Starboard(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._posted_map: dict[int, int] = {}

    def _is_target_emoji(self, emoji: str) -> bool:
        target = (self.bot.settings.starboard_emoji or "⭐").strip()
        return emoji == target

    async def _count_human_reactors(self, reaction: discord.Reaction) -> int:
        count = 0
        async for user in reaction.users(limit=None):
            if not user.bot:
                count += 1
        return count

    async def _build_reply_preview(self, message: discord.Message) -> str:
        if message.reference is None or message.reference.message_id is None:
            return ""

        referenced: discord.Message | None = message.reference.resolved if isinstance(message.reference.resolved, discord.Message) else None
        if referenced is None and isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            try:
                referenced = await message.channel.fetch_message(message.reference.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return ""

        if referenced is None:
            return ""

        preview_text = (referenced.content or "").strip()
        if len(preview_text) > 90:
            preview_text = f"{preview_text[:90].rstrip()}…"
        return f"> Replying to {referenced.author.display_name}:\n> {preview_text}\n"

    def _get_font(self, size: int):
        font_paths = [
            "C:/Windows/Fonts/msjh.ttc",
            "C:/Windows/Fonts/microsoftjhengheiui.ttf",
            "C:/Windows/Fonts/simsun.ttc",
            "C:/Windows/Fonts/segoeui.ttf",
        ]
        for path in font_paths:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def _wrap_lines(self, text: str, width: int) -> str:
        lines: list[str] = []
        for block in (text or "").splitlines() or [""]:
            wrapped = textwrap.wrap(block, width=width, break_long_words=True, break_on_hyphens=False)
            if not wrapped:
                lines.append("")
            else:
                lines.extend(wrapped)
        return "\n".join(lines).strip()

    def _wrap_text_by_pixels(self, draw, text: str, font, max_width: int, max_lines: int) -> str:
        raw = (text or "").strip()
        if not raw:
            return ""

        lines: list[str] = []
        for paragraph in raw.split("\n"):
            p = paragraph.strip()
            if not p:
                if len(lines) < max_lines:
                    lines.append("")
                continue

            current = ""
            for ch in p:
                test = f"{current}{ch}"
                test_box = draw.textbbox((0, 0), test, font=font)
                test_w = test_box[2] - test_box[0]
                if test_w <= max_width or not current:
                    current = test
                    continue

                lines.append(current)
                if len(lines) >= max_lines:
                    break
                current = ch

            if len(lines) >= max_lines:
                break
            if current:
                lines.append(current)
            if len(lines) >= max_lines:
                break

        if len(lines) > max_lines:
            lines = lines[:max_lines]

        if lines:
            last = lines[-1]
            ellipsis = "…"
            if "".join(lines) != raw.replace("\n", ""):
                while last:
                    box = draw.textbbox((0, 0), f"{last}{ellipsis}", font=font)
                    if (box[2] - box[0]) <= max_width:
                        last = f"{last}{ellipsis}"
                        break
                    last = last[:-1]
                lines[-1] = last or ellipsis

        return "\n".join(lines).strip()

    def _normalize_quote_text(self, message: discord.Message) -> str:
        text = (getattr(message, "clean_content", "") or message.content or "").strip()

        def _replace_user_mention(match: re.Match[str]) -> str:
            user_id = int(match.group(1))
            member = message.guild.get_member(user_id) if message.guild else None
            if member is not None:
                return f"@{member.display_name}"
            return "@使用者"

        text = re.sub(r"<@!?(\d+)>", _replace_user_mention, text)
        text = re.sub(r"<@&(\d+)>", "@身分組", text)
        text = re.sub(r"<#(\d+)>", "#頻道", text)
        text = re.sub(r"<a?:([A-Za-z0-9_]+):\d+>", r":\1:", text)

        text = re.sub(r"^\s*[-*#>]+\s*", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    async def _build_quote_image(
        self,
        *,
        message: discord.Message,
        star_count: int,
        reply_preview: str,
        content_text: str,
    ) -> discord.File | None:
        if Image is None or ImageDraw is None or ImageFont is None:
            return None

        width = 1200
        height = 630
        base = Image.new("RGB", (width, height), (0, 0, 0))
        draw = ImageDraw.Draw(base)

        left_w = 540
        source_image_bytes: bytes | None = None

        image_attachment = next(
            (att for att in message.attachments if (att.content_type or "").startswith("image/")),
            None,
        )
        try:
            if image_attachment is not None:
                source_image_bytes = await image_attachment.read()
            else:
                source_image_bytes = await message.author.display_avatar.read()
        except Exception:
            source_image_bytes = None

        if source_image_bytes is not None:
            try:
                visual = Image.open(io.BytesIO(source_image_bytes)).convert("RGB")
                scale = max((left_w + 140) / max(1, visual.width), height / max(1, visual.height))
                resized_w = max(1, int(visual.width * scale))
                resized_h = max(1, int(visual.height * scale))
                visual = visual.resize((resized_w, resized_h))

                crop_left = max(0, (resized_w - (left_w + 140)) // 2)
                crop_top = max(0, (resized_h - height) // 2)
                visual = visual.crop((crop_left, crop_top, crop_left + left_w + 140, crop_top + height))

                if ImageEnhance is not None:
                    visual = ImageEnhance.Color(visual).enhance(0.82)
                    visual = ImageEnhance.Brightness(visual).enhance(0.88)
                if ImageFilter is not None:
                    visual = visual.filter(ImageFilter.GaussianBlur(radius=0.6))

                base.paste(visual, (0, 0))
            except Exception:
                draw.rectangle([(0, 0), (left_w + 140, height)], fill=(16, 16, 16))
        else:
            draw.rectangle([(0, 0), (left_w + 140, height)], fill=(16, 16, 16))

        dark_overlay = Image.new("RGBA", (left_w + 140, height), (0, 0, 0, 70))
        base.paste(dark_overlay, (0, 0), dark_overlay)

        fade_start_x = 280
        fade_w = 620
        fade = Image.new("RGBA", (fade_w, height), (0, 0, 0, 0))
        fade_draw = ImageDraw.Draw(fade)
        for x in range(fade_w):
            t = x / max(1, fade_w - 1)
            alpha = int(255 * (t**0.95))
            fade_draw.line([(x, 0), (x, height)], fill=(0, 0, 0, alpha))
        base.paste(fade, (fade_start_x, 0), fade)

        mid_overlay_w = 360
        mid_overlay = Image.new("RGBA", (mid_overlay_w, height), (0, 0, 0, 0))
        mid_draw = ImageDraw.Draw(mid_overlay)
        for x in range(mid_overlay_w):
            t = x / max(1, mid_overlay_w - 1)
            alpha = int(220 * t)
            mid_draw.line([(x, 0), (x, height)], fill=(0, 0, 0, alpha))
        base.paste(mid_overlay, (460, 0), mid_overlay)

        draw.rectangle([(640, 0), (width, height)], fill=(0, 0, 0))

        right_x = 705
        user_font = self._get_font(22)

        quote = content_text.strip() or "（無文字內容）"
        text_right_margin = 38
        max_text_width = width - right_x - text_right_margin
        quote_y = 160
        max_quote_bottom = height - 140

        quote_font = None
        rendered_quote = ""
        line_spacing = 10
        for size in (72, 68, 64, 60, 56, 52, 48, 44):
            candidate_font = self._get_font(size)
            wrapped = self._wrap_text_by_pixels(
                draw,
                f"：{quote}",
                candidate_font,
                max_width=max_text_width,
                max_lines=4,
            )
            if not wrapped:
                continue
            box = draw.multiline_textbbox((right_x, quote_y), wrapped, font=candidate_font, spacing=line_spacing)
            if box[3] <= max_quote_bottom:
                quote_font = candidate_font
                rendered_quote = wrapped
                break

        if quote_font is None:
            quote_font = self._get_font(44)
            rendered_quote = self._wrap_text_by_pixels(
                draw,
                f"：{quote}",
                quote_font,
                max_width=max_text_width,
                max_lines=4,
            )

        draw.text((right_x, quote_y), rendered_quote, font=quote_font, fill=(245, 245, 245), spacing=line_spacing)

        quote_box = draw.multiline_textbbox((right_x, quote_y), rendered_quote, font=quote_font, spacing=line_spacing)
        user_y = min(height - 110, quote_box[3] + 22)

        author_text = f"- {message.author.display_name}"
        author_box = draw.textbbox((0, 0), author_text, font=user_font)
        author_w = author_box[2] - author_box[0]
        text_area_center_x = right_x + ((width - 38 - right_x) // 2)
        author_x = max(right_x, text_area_center_x - (author_w // 2))
        draw.text((author_x, user_y), author_text, font=user_font, fill=(235, 235, 235))

        out = io.BytesIO()
        base.save(out, format="PNG", optimize=True)
        out.seek(0)
        return discord.File(fp=out, filename=f"starboard-{message.id}.png")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return

        starboard_channel_id = self.bot.settings.starboard_channel_id
        if not starboard_channel_id:
            return

        if payload.channel_id == starboard_channel_id:
            return

        if not self._is_target_emoji(str(payload.emoji)):
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        channel = guild.get_channel(payload.channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(payload.channel_id)

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        target_reaction: discord.Reaction | None = None
        for reaction in message.reactions:
            if self._is_target_emoji(str(reaction.emoji)):
                target_reaction = reaction
                break

        if target_reaction is None:
            return

        star_count = await self._count_human_reactors(target_reaction)
        if star_count < self.bot.settings.starboard_min_reactions:
            return

        target_channel = guild.get_channel(starboard_channel_id)
        if target_channel is None:
            try:
                target_channel = await self.bot.fetch_channel(starboard_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                self.bot.logger.warning("找不到爆言頻道 %s", starboard_channel_id)
                return

        if not isinstance(target_channel, discord.TextChannel):
            self.bot.logger.warning("爆言頻道 %s 不是文字頻道", starboard_channel_id)
            return

        content_text = self._normalize_quote_text(message)
        reply_preview = await self._build_reply_preview(message)
        description_parts: list[str] = []
        if reply_preview:
            description_parts.append(reply_preview)
        if content_text:
            description_parts.append(content_text)

        image_file = await self._build_quote_image(
            message=message,
            star_count=star_count,
            reply_preview=reply_preview,
            content_text=content_text,
        )
        post_content = f"{self.bot.settings.starboard_emoji} **{star_count}** in {message.channel.mention}"

        posted_message_id = self._posted_map.get(message.id)
        if posted_message_id:
            try:
                posted_message = await target_channel.fetch_message(posted_message_id)
                await posted_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            finally:
                self._posted_map.pop(message.id, None)

        try:
            if image_file is not None:
                sent = await target_channel.send(content=post_content, file=image_file)
            else:
                fallback = discord.Embed(
                    description=("\n\n".join(description_parts) if description_parts else None),
                    color=discord.Color.gold(),
                    timestamp=message.created_at,
                )
                fallback.set_author(
                    name=message.author.display_name,
                    icon_url=message.author.display_avatar.url,
                    url=message.jump_url,
                )
                sent = await target_channel.send(content=post_content, embed=fallback)
            self._posted_map[message.id] = sent.id
        except discord.HTTPException as exc:
            self.bot.logger.warning("發送爆言失敗(message=%s): %s", message.id, exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Starboard(bot))
