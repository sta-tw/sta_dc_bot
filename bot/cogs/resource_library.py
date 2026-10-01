from __future__ import annotations

import io
import ipaddress
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

from bot.utils.config_paths import ConfigPaths
from bot.utils.resource_library_markdown import markdown_diff, validate_markdown_links
from database.resource_library import ResourceRepository, StaleDocumentError


logger = logging.getLogger(__name__)
MAX_MARKDOWN_CHARS = 2000
RESOURCE_DOCUMENTS = (
    ("communities", "資訊社群分享"),
    ("learning", "學習、比賽資源分享"),
    ("experiences", "特選心得彙整"),
    ("portfolios", "公開備審資料彙整"),
    ("tools", "做備審的好工具"),
)
RESOURCE_SLUGS = frozenset(slug for slug, _ in RESOURCE_DOCUMENTS)
_CUSTOM_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]{2,32}):\d{17,20}>")


class ResourceEditorEntryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="開啟編輯器",
        style=discord.ButtonStyle.primary,
        custom_id="resource-library:launch",
    )
    async def launch_editor(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "請在伺服器頻道中開啟資源編輯器。", ephemeral=True
            )
            return
        try:
            await interaction.response.launch_activity()
        except discord.HTTPException:
            logger.exception("Could not launch the resource editor Activity")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "無法開啟 Activity，請確認 Discord Developer Portal 已啟用 Activity。",
                    ephemeral=True,
                )


class ResourceReviewView(discord.ui.View):
    def __init__(self, cog: "ResourceLibraryCog", disabled: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        for item in self.children:
            item.disabled = disabled

    @discord.ui.button(
        label="同意",
        style=discord.ButtonStyle.success,
        custom_id="resource-library:approve",
    )
    async def approve(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.resolve_review(interaction, approve=True)

    @discord.ui.button(
        label="拒絕",
        style=discord.ButtonStyle.danger,
        custom_id="resource-library:reject",
    )
    async def reject(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog.resolve_review(interaction, approve=False)


class ResourceLibraryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._repositories: dict[int, ResourceRepository] = {}
        self._document_message_index: dict[tuple[int, int], str] = {}
        self._http_session: aiohttp.ClientSession | None = None
        self._web_runner: web.AppRunner | None = None
        self._startup_sync_done = False

    async def cog_load(self) -> None:
        self.bot.add_view(ResourceEditorEntryView())
        self.bot.add_view(ResourceReviewView(self))
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        )
        await self._start_web_server()

    async def cog_unload(self) -> None:
        if self._web_runner is not None:
            await self._web_runner.cleanup()
            self._web_runner = None
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

    def repository(self, guild_id: int) -> ResourceRepository:
        repository = self._repositories.get(guild_id)
        if repository is None:
            ConfigPaths.ensure_directories()
            repository = ResourceRepository(ConfigPaths.guild_database(guild_id))
            self._repositories[guild_id] = repository
        return repository

    @staticmethod
    async def _active_documents(repository: ResourceRepository) -> list[dict[str, Any]]:
        documents = {document["slug"]: document for document in await repository.list_documents()}
        return [documents[slug] for slug, _ in RESOURCE_DOCUMENTS if slug in documents]

    async def _start_web_server(self) -> None:
        activity_dir = Path(__file__).resolve().parents[2] / "activity"
        app = web.Application(client_max_size=64 * 1024)
        app.add_routes(
            [
                web.get("/", self._activity_index),
                web.get("/activity", self._activity_index),
                web.get("/activity/", self._activity_index),
                web.get("/activity/app.js", self._activity_script),
                web.get("/activity/discord-sdk.js", self._activity_sdk),
                web.get("/activity/styles.css", self._activity_styles),
                web.get("/api/config", self._api_config),
                web.post("/api/auth/token", self._api_exchange_code),
                web.get("/api/auth/standalone/login", self._api_standalone_login),
                web.get("/api/auth/standalone/callback", self._api_standalone_callback),
                web.get("/api/guilds/{guild_id:\\d+}/resources", self._api_list_resources),
                web.post(
                    "/api/guilds/{guild_id:\\d+}/resources/{slug}/preview",
                    self._api_preview,
                ),
                web.post(
                    "/api/guilds/{guild_id:\\d+}/resources/{slug}/drafts",
                    self._api_create_draft,
                ),
            ]
        )
        app["activity_dir"] = activity_dir
        self._web_runner = web.AppRunner(app, access_log=None)
        await self._web_runner.setup()
        host = os.getenv("RESOURCE_WEB_HOST", "0.0.0.0")
        port = int(os.getenv("RESOURCE_WEB_PORT", "8080"))
        if self._standalone_enabled():
            self._validate_standalone_configuration(host, port)
        site = web.TCPSite(self._web_runner, host=host, port=port)
        await site.start()
        self.bot.logger.info("Resource Activity API listening on %s:%s", host, port)

    async def _activity_index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(request.app["activity_dir"] / "index.html")

    async def _activity_script(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(request.app["activity_dir"] / "app.js")

    async def _activity_sdk(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(request.app["activity_dir"] / "discord-sdk.js")

    async def _activity_styles(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(request.app["activity_dir"] / "styles.css")

    def _client_id(self) -> str | None:
        configured = os.getenv("DISCORD_CLIENT_ID", "").strip()
        application_id = getattr(self.bot, "application_id", None)
        return configured or (str(application_id) if application_id else None)

    @staticmethod
    def _standalone_enabled() -> bool:
        return os.getenv("RESOURCE_STANDALONE_ENABLED", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @staticmethod
    def _standalone_guild_id() -> int:
        value = os.getenv("RESOURCE_STANDALONE_GUILD_ID", "").strip()
        try:
            guild_id = int(value)
        except ValueError:
            raise RuntimeError(
                "RESOURCE_STANDALONE_ENABLED requires a valid RESOURCE_STANDALONE_GUILD_ID."
            )
        if guild_id <= 0:
            raise RuntimeError(
                "RESOURCE_STANDALONE_GUILD_ID must be a positive Discord server ID."
            )
        return guild_id

    @staticmethod
    def _standalone_redirect_uri(port: int) -> str:
        value = os.getenv("RESOURCE_STANDALONE_REDIRECT_URI", "").strip()
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme == "http"
                and parsed.hostname == "127.0.0.1"
                and parsed.port == port
                and parsed.path == "/api/auth/standalone/callback"
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise RuntimeError(
                "RESOURCE_STANDALONE_REDIRECT_URI must be "
                f"http://127.0.0.1:{port}/api/auth/standalone/callback."
            )
        return value

    @staticmethod
    def _is_standalone_local_request(request: web.Request) -> bool:
        try:
            remote = ipaddress.ip_address(request.remote or "")
            host = urlsplit(f"//{request.host}").hostname
        except ValueError:
            return False
        return remote.is_loopback and host == "127.0.0.1"

    def _require_standalone_request(self, request: web.Request) -> None:
        if not self._standalone_enabled():
            raise web.HTTPNotFound(text="Standalone 測試模式未啟用。")
        if not self._is_standalone_local_request(request):
            raise web.HTTPForbidden(text="Standalone 測試模式僅允許從本機存取。")

    def _enforce_api_guild(self, request: web.Request, guild_id: int) -> None:
        if not self._standalone_enabled():
            return
        self._require_standalone_request(request)
        if guild_id != self._standalone_guild_id():
            raise web.HTTPForbidden(text="本機測試模式只允許使用設定的測試伺服器。")

    def _validate_standalone_configuration(self, host: str, port: int) -> None:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback or host != "127.0.0.1":
            raise RuntimeError(
                "Standalone mode requires RESOURCE_WEB_HOST=127.0.0.1; "
                "do not expose its local OAuth flow to the network."
            )
        self._standalone_guild_id()
        self._standalone_redirect_uri(port)

    async def _api_config(self, request: web.Request) -> web.Response:
        client_id = self._client_id()
        if not client_id:
            raise web.HTTPServiceUnavailable(text="Activity OAuth 尚未設定 DISCORD_CLIENT_ID。")
        standalone = self._standalone_enabled()
        guild_id = None
        if standalone:
            self._require_standalone_request(request)
            guild_id = str(self._standalone_guild_id())
        return web.json_response(
            {
                "client_id": client_id,
                "scope": ["identify"],
                "standalone": standalone,
                "guild_id": guild_id,
            }
        )

    async def _api_standalone_login(self, request: web.Request) -> web.Response:
        self._require_standalone_request(request)
        client_id = self._client_id()
        client_secret = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise web.HTTPServiceUnavailable(text="Standalone OAuth 設定不完整。")
        port = int(os.getenv("RESOURCE_WEB_PORT", "8080"))
        redirect_uri = self._standalone_redirect_uri(port)
        state = secrets.token_urlsafe(32)
        location = "https://discord.com/oauth2/authorize?" + urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": "identify",
                "state": state,
            }
        )
        response = web.HTTPFound(location)
        response.set_cookie(
            "resource_standalone_oauth_state",
            state,
            max_age=600,
            path="/api/auth/standalone/callback",
            httponly=True,
            secure=False,
            samesite="Lax",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _api_standalone_callback(self, request: web.Request) -> web.Response:
        self._require_standalone_request(request)
        state = request.query.get("state", "")
        state_cookie = request.cookies.get("resource_standalone_oauth_state", "")
        if not state or not state_cookie or not secrets.compare_digest(state, state_cookie):
            return self._standalone_redirect(error="invalid_state")
        if request.query.get("error") or "code" not in request.query:
            return self._standalone_redirect(error="authorization_failed")

        code = request.query.get("code", "")
        client_id = self._client_id()
        client_secret = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
        port = int(os.getenv("RESOURCE_WEB_PORT", "8080"))
        redirect_uri = self._standalone_redirect_uri(port)
        if not code or len(code) > 2048 or not client_id or not client_secret:
            return self._standalone_redirect(error="authorization_failed")
        if self._http_session is None:
            raise web.HTTPServiceUnavailable(text="Activity API 尚未啟動。")

        try:
            async with self._http_session.post(
                "https://discord.com/api/oauth2/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            ) as response:
                token_data = await response.json(content_type=None)
            access_token = token_data.get("access_token") if isinstance(token_data, dict) else None
            if response.status != 200 or not isinstance(access_token, str) or not access_token:
                return self._standalone_redirect(error="authorization_failed")
            async with self._http_session.get(
                "https://discord.com/api/users/@me",
                headers={"Authorization": f"Bearer {access_token}"},
            ) as response:
                user_data = await response.json(content_type=None)
            if response.status != 200 or not isinstance(user_data, dict):
                return self._standalone_redirect(error="authorization_failed")
            user_id = int(user_data["id"])
        except (aiohttp.ClientError, TimeoutError, KeyError, TypeError, ValueError):
            logger.exception("Standalone Discord OAuth validation failed")
            return self._standalone_redirect(error="authorization_failed")

        guild = self.bot.get_guild(self._standalone_guild_id())
        if guild is None:
            return self._standalone_redirect(error="test_guild_unavailable")
        try:
            await guild.fetch_member(user_id)
        except discord.NotFound:
            return self._standalone_redirect(error="not_a_test_guild_member")
        except discord.HTTPException:
            logger.exception("Could not verify standalone tester %s", user_id)
            return self._standalone_redirect(error="membership_check_failed")

        response = web.HTTPFound("/activity#" + urlencode({"access_token": access_token}))
        response.del_cookie("resource_standalone_oauth_state", path="/api/auth/standalone/callback")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @staticmethod
    def _standalone_redirect(*, error: str) -> web.Response:
        response = web.HTTPFound("/activity?standalone_error=" + error)
        response.del_cookie("resource_standalone_oauth_state", path="/api/auth/standalone/callback")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    async def _api_exchange_code(self, request: web.Request) -> web.Response:
        payload = await self._read_json(request)
        code = payload.get("code")
        client_id = self._client_id()
        client_secret = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
        if not isinstance(code, str) or not code or len(code) > 2048:
            raise web.HTTPBadRequest(text="缺少有效的 Discord OAuth 授權碼。")
        if not client_id or not client_secret:
            raise web.HTTPServiceUnavailable(text="Activity OAuth 設定不完整。")
        if self._http_session is None:
            raise web.HTTPServiceUnavailable(text="Activity API 尚未啟動。")

        try:
            async with self._http_session.post(
                "https://discord.com/api/oauth2/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                },
            ) as response:
                token_data = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError):
            logger.exception("Discord OAuth token exchange failed")
            raise web.HTTPBadGateway(text="無法連線至 Discord OAuth。")
        if response.status != 200 or not token_data.get("access_token"):
            raise web.HTTPUnauthorized(text="Discord OAuth 授權碼無效或已過期。")
        return web.json_response({"access_token": token_data["access_token"]})

    async def _read_json(self, request: web.Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="請提供有效的 JSON。")
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="JSON 內容格式錯誤。")
        return payload

    async def _authenticate_member(
        self,
        request: web.Request,
        guild_id: int,
    ) -> tuple[discord.Guild, discord.Member]:
        self._enforce_api_guild(request, guild_id)
        authorization = request.headers.get("Authorization", "")
        scheme, _, access_token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not access_token or len(access_token) > 4096:
            raise web.HTTPUnauthorized(text="Activity 尚未完成 Discord 登入。")
        if self._http_session is None:
            raise web.HTTPServiceUnavailable(text="Activity API 尚未啟動。")
        try:
            async with self._http_session.get(
                "https://discord.com/api/users/@me",
                headers={"Authorization": f"Bearer {access_token}"},
            ) as response:
                if response.status != 200:
                    raise web.HTTPUnauthorized(text="Discord 登入已失效，請重新開啟 Activity。")
                user_data = await response.json(content_type=None)
        except web.HTTPException:
            raise
        except (aiohttp.ClientError, TimeoutError):
            logger.exception("Discord identity verification failed")
            raise web.HTTPBadGateway(text="無法驗證 Discord 使用者。")

        try:
            user_id = int(user_data["id"])
        except (KeyError, TypeError, ValueError):
            raise web.HTTPUnauthorized(text="Discord 回傳的使用者資料無效。")

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise web.HTTPForbidden(text="此伺服器不支援資源編輯器。")
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            raise web.HTTPForbidden(text="只有此 Discord 伺服器的成員可以使用資源編輯器。")
        except discord.HTTPException:
            logger.exception("Could not verify guild membership for user %s", user_id)
            raise web.HTTPBadGateway(text="無法確認 Discord 伺服器成員資格。")
        return guild, member

    async def _api_list_resources(self, request: web.Request) -> web.Response:
        guild_id = int(request.match_info["guild_id"])
        await self._authenticate_member(request, guild_id)
        repository = self.repository(guild_id)
        await repository.initialize()
        documents = await self._active_documents(repository)
        if len(documents) != len(RESOURCE_DOCUMENTS):
            raise web.HTTPConflict(text="資源編輯系統尚未完成設定。")
        return web.json_response({"documents": documents})

    @staticmethod
    def _parse_base_version(payload: dict[str, Any]) -> int:
        value = payload.get("base_version")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise web.HTTPBadRequest(text="請提供有效的 base_version。")
        return value

    @staticmethod
    def _validate_slug(slug: str) -> None:
        if slug not in RESOURCE_SLUGS:
            raise web.HTTPNotFound(text="找不到指定的資源文件。")

    @staticmethod
    def _parse_content_markdown(payload: dict[str, Any]) -> str:
        content_md = payload.get("content_md")
        if not isinstance(content_md, str):
            raise web.HTTPBadRequest(text="Markdown 內容必須是文字。")
        if not content_md.strip():
            raise web.HTTPBadRequest(text="Markdown 內容不可為空。")
        try:
            validate_markdown_links(content_md)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        if len(content_md) > MAX_MARKDOWN_CHARS:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_MARKDOWN_CHARS,
                actual_size=len(content_md),
                text=f"Markdown 超過單則 Discord 訊息的 {MAX_MARKDOWN_CHARS} 字元限制。",
            )
        return content_md

    async def _api_preview(self, request: web.Request) -> web.Response:
        guild_id = int(request.match_info["guild_id"])
        slug = request.match_info["slug"]
        self._validate_slug(slug)
        await self._authenticate_member(request, guild_id)
        payload = await self._read_json(request)
        base_version = self._parse_base_version(payload)
        repository = self.repository(guild_id)
        await repository.initialize()
        document = await repository.get_document(slug)
        if document is None:
            raise web.HTTPNotFound(text="找不到指定的資源文件。")
        if base_version != int(document["version"]):
            raise web.HTTPConflict(text="文件已更新，請重新載入最新內容。")
        after_md = self._parse_content_markdown(payload)
        return web.json_response(
            {
                "content_md": after_md,
                "diff": markdown_diff(document["content_md"], after_md),
            }
        )

    async def _api_create_draft(self, request: web.Request) -> web.Response:
        guild_id = int(request.match_info["guild_id"])
        slug = request.match_info["slug"]
        self._validate_slug(slug)
        guild, member = await self._authenticate_member(request, guild_id)
        payload = await self._read_json(request)
        base_version = self._parse_base_version(payload)
        repository = self.repository(guild_id)
        await repository.initialize()
        document = await repository.get_document(slug)
        if document is None:
            raise web.HTTPNotFound(text="找不到指定的資源文件。")
        if base_version != int(document["version"]):
            raise web.HTTPConflict(text="文件已更新，請重新載入最新內容。")
        after_md = self._parse_content_markdown(payload)
        try:
            draft = await repository.create_draft(
                slug,
                created_by=member.id,
                base_version=base_version,
                after_md=after_md,
            )
        except StaleDocumentError as exc:
            raise web.HTTPConflict(text=str(exc))
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        except KeyError:
            raise web.HTTPNotFound(text="找不到指定的資源文件。")

        try:
            thread, review_message = await self._create_review_thread(
                guild, repository, document, draft, member.name
            )
            await repository.set_review_thread(draft["id"], thread.id, review_message.id)
        except discord.Forbidden as exc:
            logger.exception("Discord denied review thread creation for draft %s", draft["id"])
            await repository.mark_review_failed(draft["id"], str(exc))
            raise web.HTTPForbidden(
                text="草稿已保存，但 Bot 無權在審核頻道建立 Thread；請管理員檢查頻道權限。"
            ) from exc
        except Exception as exc:
            logger.exception("Failed to create the review thread for draft %s", draft["id"])
            await repository.mark_review_failed(draft["id"], str(exc))
            raise web.HTTPBadGateway(
                text="草稿已保存，但無法建立審核 Thread；請聯絡管理員處理。"
            ) from exc
        return web.json_response(
            {"draft_id": draft["id"], "status": "pending", "thread_id": thread.id},
            status=201,
        )

    async def _create_review_thread(
        self,
        guild: discord.Guild,
        repository: ResourceRepository,
        document: dict[str, Any],
        draft: dict[str, Any],
        submitter_name: str,
    ) -> tuple[discord.Thread, discord.Message]:
        settings = await repository.get_workflow_settings()
        if settings is None:
            raise ValueError("資源審核頻道尚未設定。")
        review_channel = guild.get_channel(settings["review_channel_id"])
        if review_channel is None:
            review_channel = await self.bot.fetch_channel(settings["review_channel_id"])
        if not isinstance(review_channel, discord.TextChannel) or review_channel.guild.id != guild.id:
            raise ValueError("設定的審核位置不是此伺服器的文字頻道。")

        role = guild.get_role(settings["notification_role_id"])
        allowed_mentions = discord.AllowedMentions(roles=[role] if role else [], users=False)
        ping = role.mention if role else ""
        starter = await review_channel.send(
            content=f"{ping} 資源文件「{document['title']}」有新的修改草稿待審核。".strip(),
            allowed_mentions=allowed_mentions,
        )
        thread = await starter.create_thread(
            name=f"資源審核｜{document['title']}｜v{draft['base_version']}"[:100],
            auto_archive_duration=1440,
        )
        embed = self._draft_embed(document, draft, submitter_name)
        files = []
        if len(draft["diff"]) > 3600:
            files.append(
                discord.File(
                    io.BytesIO(draft["diff"].encode("utf-8")),
                    filename="resource-diff.diff",
                )
            )
            embed.description = "修改 Diff 過長，請查看附加的 `resource-diff.diff`。"
        else:
            embed.description = f"```diff\n{draft['diff']}\n```"
        message = await thread.send(
            embed=embed,
            files=files,
            view=ResourceReviewView(self),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return thread, message

    @staticmethod
    def _draft_embed(
        document: dict[str, Any], draft: dict[str, Any], submitter_name: str
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"資源修改審核｜{document['title']}",
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="提交者", value=discord.utils.escape_markdown(submitter_name), inline=True
        )
        embed.add_field(name="基礎版本", value=str(draft["base_version"]), inline=True)
        embed.add_field(name="審核狀態", value="待審核", inline=True)
        return embed

    @staticmethod
    def _can_review(member: discord.Member) -> bool:
        permissions = member.guild_permissions
        return bool(permissions.administrator or permissions.manage_guild)

    async def resolve_review(
        self,
        interaction: discord.Interaction,
        *,
        approve: bool,
    ) -> None:
        if self._standalone_enabled() and interaction.guild_id != self._standalone_guild_id():
            await interaction.response.send_message(
                "本機測試模式只允許審核指定測試伺服器的草稿。",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if guild is None or interaction.channel_id is None or interaction.message is None:
            await interaction.followup.send("此審核操作只能在伺服器 Review Thread 中使用。", ephemeral=True)
            return
        try:
            member = await guild.fetch_member(interaction.user.id)
        except discord.HTTPException:
            logger.exception("Could not refresh reviewer permissions for user %s", interaction.user.id)
            await interaction.followup.send("無法重新驗證你的 Discord 權限，請稍後再試。", ephemeral=True)
            return
        if not self._can_review(member):
            await interaction.followup.send(
                "只有具有「管理伺服器」或「管理員」權限的成員可以審核。",
                ephemeral=True,
            )
            return

        repository = self.repository(guild.id)
        await repository.initialize()
        draft = await repository.get_draft_by_thread(interaction.channel_id)
        if (
            draft is None
            or draft.get("review_message_id") != interaction.message.id
            or draft.get("status") != "pending"
        ):
            await interaction.followup.send("此草稿已處理或審核資料不存在。", ephemeral=True)
            return
        if draft["document_slug"] not in RESOURCE_SLUGS:
            await interaction.followup.send(
                "此資源文件已停用；既有草稿保留為歷史紀錄，不再接受審核。",
                ephemeral=True,
            )
            return

        try:
            result = await repository.decide_draft(
                draft["id"], reviewer_id=member.id, approve=approve
            )
        except KeyError:
            await interaction.followup.send("找不到此草稿。", ephemeral=True)
            return

        status = result["status"]
        if status == "stale":
            current_version = result["current_version"]
            await interaction.followup.send(
                f"此草稿基於 v{draft['base_version']}，正式內容已更新至 v{current_version}；"
                "草稿已標記為過期，請重新載入最新內容後再提交。",
                ephemeral=True,
            )
            await self._finish_review(interaction, draft, "版本衝突，請重新載入後提交。", "過期")
            return
        if status != ("approved" if approve else "rejected") or result.get("document") is None:
            await interaction.followup.send("此草稿已由其他管理員處理。", ephemeral=True)
            return

        sync_error = None
        if approve:
            try:
                result["document"]["guild_id"] = guild.id
                await self.sync_document(result["document"])
            except (discord.HTTPException, ValueError):
                logger.exception("Database update succeeded but Discord sync failed for %s", draft["id"])
                sync_error = "資料庫已更新，但 Discord 正式訊息同步失敗；請執行 /resource_sync 重試。"

        status_text = "已同意並發布" if approve else "已拒絕"
        await self._finish_review(interaction, draft, status_text, status_text)
        if sync_error:
            await interaction.followup.send(sync_error, ephemeral=True)
        else:
            await interaction.followup.send(
                "已同意，正式文件已更新。" if approve else "已拒絕此草稿。",
                ephemeral=True,
            )

    async def _finish_review(
        self,
        interaction: discord.Interaction,
        draft: dict[str, Any],
        detail: str,
        status: str,
    ) -> None:
        if interaction.message is not None:
            embed = discord.Embed(
                title="資源修改審核已完成",
                description=f"草稿：`{draft['id']}`\n結果：{detail}",
                color=discord.Color.green() if status == "已同意並發布" else discord.Color.red(),
            )
            try:
                await interaction.message.edit(embed=embed, view=ResourceReviewView(self, disabled=True))
            except discord.HTTPException:
                logger.exception("Could not update completed review message %s", interaction.message.id)
        if isinstance(interaction.channel, discord.Thread):
            try:
                await interaction.channel.edit(archived=True, locked=True)
            except discord.HTTPException:
                logger.exception("Could not archive completed resource review thread")

    async def sync_document(self, document: dict[str, Any]) -> discord.Message:
        if document["slug"] not in RESOURCE_SLUGS:
            raise ValueError("此資源文件已停用，不再同步正式訊息。")
        guild_id = self._document_guild_id(document)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise ValueError("找不到資源文件所屬的伺服器。")
        channel = guild.get_channel(int(document["channel_id"]))
        if channel is None:
            channel = await self.bot.fetch_channel(int(document["channel_id"]))
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild_id:
            raise ValueError("資源文件指定的頻道無效。")

        content = document["content_md"] or f"# {document['title']}"
        repository = self.repository(guild_id)
        message_id = document.get("message_id")
        message = None
        if message_id:
            try:
                message = await channel.fetch_message(int(message_id))
            except discord.NotFound:
                message = None
        if message is not None:
            if self.bot.user is None or message.author.id != self.bot.user.id:
                raise ValueError("資料庫記錄的正式訊息不是 Bot 建立的，已停止同步以避免覆寫他人訊息。")
        if message is None:
            message = await channel.send(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if message_id:
                self._document_message_index.pop((guild_id, int(message_id)), None)
            await repository.set_message_id(document["slug"], message.id)
        else:
            content_matches = (
                _CUSTOM_EMOJI_RE.sub(r":\1:", message.content).rstrip("\r\n")
                == _CUSTOM_EMOJI_RE.sub(r":\1:", content).rstrip("\r\n")
            )
            has_custom_embed = any(embed.type == "rich" for embed in message.embeds)
            if not content_matches or has_custom_embed:
                await message.edit(
                    content=content,
                    embed=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        self._document_message_index[(guild_id, message.id)] = document["slug"]
        return message

    @staticmethod
    def _document_guild_id(document: dict[str, Any]) -> int:
        guild_id = document.get("guild_id")
        if guild_id is None:
            raise ValueError("資源文件缺少伺服器 ID。")
        return int(guild_id)

    async def _sync_guild(self, guild: discord.Guild) -> None:
        repository = self.repository(guild.id)
        await repository.initialize()
        for document in await self._active_documents(repository):
            document["guild_id"] = guild.id
            await self.sync_document(document)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._startup_sync_done:
            return
        self._startup_sync_done = True
        standalone_guild_id = (
            self._standalone_guild_id() if self._standalone_enabled() else None
        )
        for guild in self.bot.guilds:
            if standalone_guild_id is not None and guild.id != standalone_guild_id:
                continue
            db_path = ConfigPaths.guild_database(guild.id)
            if not db_path.exists():
                continue
            try:
                await self._sync_guild(guild)
            except (discord.HTTPException, OSError, ValueError):
                logger.exception("Resource message startup synchronization failed for guild %s", guild.id)

    async def _sync_registered_message(self, guild_id: int, message_id: int) -> None:
        if self._standalone_enabled() and guild_id != self._standalone_guild_id():
            return
        slug = self._document_message_index.get((guild_id, message_id))
        if slug not in RESOURCE_SLUGS:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        try:
            document = await self.repository(guild_id).get_document(slug)
            if document is None or int(document["message_id"] or 0) != message_id:
                return
            document["guild_id"] = guild_id
            await self.sync_document(document)
        except (discord.HTTPException, OSError, ValueError):
            logger.exception("Resource message reconciliation failed for %s in guild %s", message_id, guild_id)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if payload.guild_id is not None:
            await self._sync_registered_message(payload.guild_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is not None:
            await self._sync_registered_message(payload.guild_id, payload.message_id)

    async def _deny_non_standalone_guild(
        self, interaction: discord.Interaction
    ) -> bool:
        if not self._standalone_enabled() or interaction.guild_id == self._standalone_guild_id():
            return False
        await interaction.response.send_message(
            "本機測試模式只允許操作 RESOURCE_STANDALONE_GUILD_ID 指定的測試伺服器。",
            ephemeral=True,
        )
        return True

    @app_commands.command(name="resource_setup", description="設定五個資源頻道與審核位置")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        information_communities="資訊社群分享",
        learning_competitions="學習、比賽資源分享",
        selected_experiences="特選心得彙整",
        admission_portfolios="公開備審資料彙整",
        admission_tools="做備審的好工具",
        review_channel="建立資源審核 Thread 的頻道",
        notification_role="只用於審核通知的身分組；實際審核仍需管理伺服器權限",
    )
    async def resource_setup(
        self,
        interaction: discord.Interaction,
        information_communities: discord.TextChannel,
        learning_competitions: discord.TextChannel,
        selected_experiences: discord.TextChannel,
        admission_portfolios: discord.TextChannel,
        admission_tools: discord.TextChannel,
        review_channel: discord.TextChannel,
        notification_role: discord.Role,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("此指令只能在伺服器中使用。", ephemeral=True)
            return
        if await self._deny_non_standalone_guild(interaction):
            return
        resource_channels = {
            "communities": information_communities,
            "learning": learning_competitions,
            "experiences": selected_experiences,
            "portfolios": admission_portfolios,
            "tools": admission_tools,
        }
        channel_ids = [channel.id for channel in resource_channels.values()]
        if len(set(channel_ids)) != len(channel_ids):
            await interaction.response.send_message("五個資源文件必須使用不同的頻道。", ephemeral=True)
            return
        if review_channel.id in channel_ids:
            await interaction.response.send_message("審核頻道需與五個正式資源頻道分開。", ephemeral=True)
            return
        if notification_role.is_default():
            await interaction.response.send_message("通知身分組不可選擇 @everyone。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        repository = self.repository(interaction.guild.id)
        await repository.initialize()
        documents = [
            {"slug": slug, "title": title, "channel_id": resource_channels[slug].id}
            for slug, title in RESOURCE_DOCUMENTS
        ]
        try:
            await repository.configure(documents, review_channel.id, notification_role.id)
            await self._sync_guild(interaction.guild)
        except discord.Forbidden:
            logger.exception("Resource system setup failed for guild %s", interaction.guild.id)
            await interaction.followup.send(
                "設定失敗：Bot 無法存取其中一個資源頻道。請確認 Bot 有檢視頻道、傳送訊息和嵌入連結權限後，再重新執行 /resource_setup。",
                ephemeral=True,
            )
            return
        except (discord.HTTPException, OSError, ValueError) as exc:
            logger.exception("Resource system setup failed for guild %s", interaction.guild.id)
            await interaction.followup.send(f"設定失敗：{exc}", ephemeral=True)
            return
        await interaction.followup.send(
            "五份 Markdown 文件已在 Database 初始化，並已同步至各自的 Bot 管理訊息。\n"
            f"審核頻道：{review_channel.mention}；通知身分組：{notification_role.mention}。\n"
            "批准或拒絕仍須具有「管理伺服器」或「管理員」Discord 權限。",
            ephemeral=True,
        )

    @app_commands.command(name="resource_editor", description="發布資源彙整 Activity 編輯入口")
    @app_commands.guild_only()
    async def resource_editor(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("此指令只能在伺服器中使用。", ephemeral=True)
            return
        if await self._deny_non_standalone_guild(interaction):
            return
        repository = self.repository(interaction.guild.id)
        await repository.initialize()
        if len(await self._active_documents(repository)) != len(RESOURCE_DOCUMENTS):
            await interaction.response.send_message(
                "請先由管理員執行 /resource_setup。", ephemeral=True
            )
            return
        if self._standalone_enabled():
            port = int(os.getenv("RESOURCE_WEB_PORT", "8080"))
            await interaction.response.send_message(
                f"本機資源編輯器：<http://127.0.0.1:{port}/activity>",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "如果你也有超棒的東西想和大家分享，點我就對了",
            view=ResourceEditorEntryView(),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="resource_sync", description="從 Database 重新同步正式資源訊息")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def resource_sync(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("此指令只能在伺服器中使用。", ephemeral=True)
            return
        if await self._deny_non_standalone_guild(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            repository = self.repository(interaction.guild.id)
            await repository.initialize()
            documents = await self._active_documents(repository)
            if len(documents) != len(RESOURCE_DOCUMENTS):
                await interaction.followup.send("資源系統尚未完成設定。", ephemeral=True)
                return
            for document in documents:
                document["guild_id"] = interaction.guild.id
                await self.sync_document(document)
        except (discord.HTTPException, OSError, ValueError) as exc:
            logger.exception("Manual resource sync failed for guild %s", interaction.guild.id)
            await interaction.followup.send(f"同步失敗：{exc}", ephemeral=True)
            return
        await interaction.followup.send(
            "已從 Database 將五份正式 Markdown 文件同步至原有 Bot 訊息。",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ResourceLibraryCog(bot))
