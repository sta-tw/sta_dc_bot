import aiosqlite
import json
import os
from pathlib import Path
from typing import Dict, List, Any, Optional
from bot.utils.config_paths import ConfigPaths


DEFAULT_VERIFICATION_DATA = {"roles": {}, "users": {}}

class DatabaseManager:
    def __init__(self, guild_id: int, guild_name: str = None):
        self.guild_id = guild_id
        self.guild_name = guild_name

        ConfigPaths.ensure_directories()
        ConfigPaths.ensure_guild_dir(guild_id)

        self.db_name = str(ConfigPaths.guild_database(guild_id))
        self.verification_json = str(ConfigPaths.guild_verification(guild_id))
        self.config_json = str(ConfigPaths.guild_settings(guild_id))
        self.emoji_json = str(ConfigPaths.EMOJI_CONFIG)

        self._init_verification_config()
        self._init_guild_config()
        self._init_emoji_config()

    def _init_verification_config(self):
        if not os.path.exists(self.verification_json):
            with open(self.verification_json, 'w', encoding='utf-8') as f:
                json.dump(DEFAULT_VERIFICATION_DATA, f, ensure_ascii=False, indent=4)

    def _load_verification_data(self) -> Dict[str, Any]:
        needs_repair = False

        try:
            with open(self.verification_json, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
            needs_repair = True

        if not isinstance(data, dict):
            data = {}
            needs_repair = True

        roles = data.get("roles", {})
        users = data.get("users", {})

        if not isinstance(roles, dict):
            roles = {}
            needs_repair = True

        if not isinstance(users, dict):
            users = {}
            needs_repair = True

        normalized = {"roles": roles, "users": users}
        if needs_repair:
            self._save_verification_data(normalized)

        return normalized

    def _save_verification_data(self, data: Dict[str, Any]):
        with open(self.verification_json, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def _init_guild_config(self):
        if not os.path.exists(self.config_json):
            with open(self.config_json, 'w', encoding='utf-8') as f:
                json.dump({
                    "name": self.guild_name,
                    "prefix": "!",
                    "roles": {"admin": None},
                    "settings": {"application_category": None},
                    "bot_created_channels": []
                }, f, ensure_ascii=False, indent=4)

    def _init_emoji_config(self):
        if not os.path.exists(self.emoji_json):
            with open(self.emoji_json, 'w', encoding='utf-8') as f:
                json.dump({"emojis": {}}, f, ensure_ascii=False, indent=4)

    async def init_db(self):
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS application_channels (
                    user_id INTEGER PRIMARY KEY,
                    channel_id INTEGER,
                    status TEXT
                )
            ''')
            await db.commit()

    async def save_application_channel(self, user_id: int, channel_id: int):
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute('''
                INSERT OR REPLACE INTO application_channels (user_id, channel_id, status)
                VALUES (?, ?, ?)
            ''', (user_id, channel_id, "pending"))
            await db.commit()

    async def get_application_channel(self, user_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute('''
                SELECT channel_id, status FROM application_channels WHERE user_id = ?
            ''', (user_id,))
            row = await cursor.fetchone()
            if row:
                return {"channel_id": row[0], "status": row[1]}
            return None

    async def get_application_user_by_channel(self, channel_id: int) -> Optional[int]:
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                '''SELECT user_id FROM application_channels WHERE channel_id = ?''',
                (channel_id,)
            )
            row = await cursor.fetchone()
            if row:
                return int(row[0])
            return None

    async def update_application_status(self, user_id: int, status: str):
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute('''
                UPDATE application_channels SET status = ? WHERE user_id = ?
            ''', (status, user_id))
            await db.commit()

    async def get_applications_by_status(self, status: str) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute('''
                SELECT user_id, channel_id, status FROM application_channels WHERE status = ?
            ''', (status,))
            rows = await cursor.fetchall()
            return [{"user_id": row[0], "channel_id": row[1], "status": row[2]} for row in rows]

    async def get_all_applications(self) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute('SELECT user_id, channel_id, status FROM application_channels')
            rows = await cursor.fetchall()
            return [{"user_id": row[0], "channel_id": row[1], "status": row[2]} for row in rows]

    async def save_verification_role(self, user_id: str, role_id: int, role_name: str):
        data = self._load_verification_data()

        if "users" not in data:
            data["users"] = {}

        if user_id not in data["users"]:
            data["users"][user_id] = []

        role_data = {"role_id": role_id, "role_name": role_name}
        if role_data not in data["users"][user_id]:
            data["users"][user_id].append(role_data)

        self._save_verification_data(data)

    async def get_verification_roles(self, user_id: str = None) -> Dict[str, Any]:
        data = self._load_verification_data()

        if user_id:
            return data.get("users", {}).get(user_id, [])
        return data

    async def get_verification_role(self, user_id: str) -> Optional[int]:
        data = self._load_verification_data()

        user_roles = data.get("users", {}).get(user_id, [])
        if user_roles:
            role_name = user_roles[0] if isinstance(user_roles, list) else user_roles
            return data.get("roles", {}).get(role_name)
        return None

    async def get_all_user_roles(self, user_id: str) -> List[Dict[str, Any]]:
        """獲取用戶的所有驗證角色"""
        return await self.get_verification_roles(user_id)

    async def get_available_roles(self) -> List[Dict[str, Any]]:
        data = self._load_verification_data()

        roles = data.get("roles", {})
        role_list = []
        for role_name, role_id in roles.items():
            normalized_role_id = role_id
            if isinstance(role_id, str):
                stripped = role_id.strip()
                if stripped.isdigit():
                    normalized_role_id = int(stripped)
                elif stripped == "":
                    normalized_role_id = None

            if normalized_role_id == 0:
                normalized_role_id = None

            if normalized_role_id is None or isinstance(normalized_role_id, int):
                role_list.append({"name": role_name, "id": normalized_role_id})

        return role_list

    async def get_role_id(self, role_name: str) -> Optional[int]:
        data = self._load_verification_data()

        return data.get("roles", {}).get(role_name)

    async def update_role_id(self, role_name: str, role_id: int):
        data = self._load_verification_data()

        if "roles" not in data:
            data["roles"] = {}

        data["roles"][role_name] = role_id

        self._save_verification_data(data)

    def load_guild_settings(self) -> Dict[str, Any]:
        with open(self.config_json, 'r', encoding='utf-8') as f:
            return json.load(f)

    def save_guild_settings(self, settings: Dict[str, Any]):
        with open(self.config_json, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=4)

    async def save_admin_role(self, role_id: int):
        settings = self.load_guild_settings()
        if "roles" not in settings:
            settings["roles"] = {}
        settings["roles"]["admin"] = role_id
        self.save_guild_settings(settings)

    async def get_application_category(self) -> Optional[int]:
        config = self.load_guild_settings()
        return config.get("settings", {}).get("application_category")

    async def save_application_category(self, category_id: int):
        config = self.load_guild_settings()
        if "settings" not in config:
            config["settings"] = {}
        config["settings"]["application_category"] = category_id
        self.save_guild_settings(config)

    async def get_channel_id(self, channel_name: str) -> Optional[int]:
        config = self.load_guild_settings()

        candidates = [
            config.get("channels", {}).get(channel_name),
            config.get("channel_ids", {}).get(channel_name),
            config.get("settings", {}).get(f"{channel_name}_channel"),
            config.get("settings", {}).get(f"{channel_name}_channel_id"),
            config.get(f"{channel_name}_channel"),
            config.get(f"{channel_name}_channel_id"),
        ]

        for value in candidates:
            if value in (None, ""):
                continue
            try:
                channel_id = int(value)
            except (TypeError, ValueError):
                continue
            if channel_id > 0:
                return channel_id

        return None

    async def save_channel_id(self, channel_name: str, channel_id: int):
        config = self.load_guild_settings()
        if "channels" not in config or not isinstance(config["channels"], dict):
            config["channels"] = {}
        config["channels"][channel_name] = int(channel_id)
        self.save_guild_settings(config)

    async def register_bot_created_channel(self, channel_id: int):
        config = self.load_guild_settings()
        if "bot_created_channels" not in config:
            config["bot_created_channels"] = []
        if channel_id not in config["bot_created_channels"]:
            config["bot_created_channels"].append(channel_id)
        self.save_guild_settings(config)

    async def is_bot_created_channel(self, channel_id: int) -> bool:
        config = self.load_guild_settings()
        return channel_id in config.get("bot_created_channels", [])

    async def remove_bot_created_channel(self, channel_id: int):
        """從機器人創建的頻道列表中移除頻道"""
        config = self.load_guild_settings()
        if "bot_created_channels" in config and channel_id in config["bot_created_channels"]:
            config["bot_created_channels"].remove(channel_id)
            self.save_guild_settings(config)

    async def save_emoji(self, name: str, emoji_id: int, emoji: str):
        with open(self.emoji_json, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if "emojis" not in data:
            data["emojis"] = {}

        data["emojis"][name] = {
            "id": emoji_id,
            "format": emoji
        }

        with open(self.emoji_json, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def load_emoji(self) -> Dict[str, Any]:
        with open(self.emoji_json, 'r', encoding='utf-8') as f:
            return json.load(f)
