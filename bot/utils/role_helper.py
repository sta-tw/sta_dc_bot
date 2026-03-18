import discord
from typing import Optional
import logging

logger = logging.getLogger("bot")

async def get_or_create_role(
    guild: discord.Guild,
    role_id: Optional[int],
    role_name: str,
    color: discord.Color = discord.Color.default(),
    reason: str = "自動創建身分組"
) -> Optional[discord.Role]:
    if role_id:
        role = guild.get_role(role_id)
        if role:
            return role
        logger.warning(f"找不到身分組 ID {role_id}，嘗試按名稱查找...")

    role = discord.utils.get(guild.roles, name=role_name)
    if role:
        logger.info(f"找到現有身分組: {role_name} (ID: {role.id})")
        return role

    try:
        role = await guild.create_role(
            name=role_name,
            color=color,
            reason=reason
        )
        logger.info(f"已自動創建身分組: {role_name} (ID: {role.id})")
        return role
    except discord.Forbidden:
        logger.error(f"沒有權限創建身分組: {role_name}")
        return None
    except discord.HTTPException as e:
        logger.error(f"創建身分組失敗: {role_name}, 錯誤: {e}")
        return None

async def update_role_id_in_config(
    db_manager,
    role_name: str,
    role_id: int
) -> bool:
    try:
        import json

        with open(db_manager.verification_json, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if "roles" not in data:
            data["roles"] = {}

        data["roles"][role_name] = role_id

        with open(db_manager.verification_json, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

        logger.info(f"已更新配置檔: {role_name} = {role_id}")
        return True

    except Exception as e:
        logger.error(f"更新配置檔失敗: {e}")
        return False

ROLE_COLORS = {
    "exchange": discord.Color.blue(),
    "gay": discord.Color.from_rgb(255, 0, 255),
    "crown": discord.Color.gold(),
    "cat": discord.Color.orange(),
    "115特選生": discord.Color.green(),
    "歷屆特選生": discord.Color.dark_green(),
    "資工系": discord.Color.blue(),
    "不分系": discord.Color.purple(),
}

def get_role_color(role_name: str) -> discord.Color:
    return ROLE_COLORS.get(role_name, discord.Color.from_rgb(153, 170, 181))
