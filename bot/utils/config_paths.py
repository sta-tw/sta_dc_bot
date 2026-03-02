from pathlib import Path

class ConfigPaths:

    ROOT_DIR = Path(__file__).parent.parent.parent
    CONFIG_DIR = ROOT_DIR / "config"
    DATA_DIR = ROOT_DIR / "data"

    BOT_CONFIG = CONFIG_DIR / "bot.json"

    EMOJI_CONFIG = CONFIG_DIR / "emoji.json"

    GUILDS_DIR = CONFIG_DIR / "guilds"

    DATABASE_DIR = DATA_DIR / "database"
    TRANSCRIPTS_DIR = DATA_DIR / "transcripts"

    @classmethod
    def guild_dir(cls, guild_id: int) -> Path:
        return cls.GUILDS_DIR / str(guild_id)

    @classmethod
    def guild_settings(cls, guild_id: int) -> Path:
        return cls.guild_dir(guild_id) / "settings.json"

    @classmethod
    def guild_verification(cls, guild_id: int) -> Path:
        return cls.guild_dir(guild_id) / "verification.json"

    @classmethod
    def guild_database(cls, guild_id: int) -> Path:
        return cls.DATABASE_DIR / f"{guild_id}.db"

    @classmethod
    def ensure_directories(cls):
        cls.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cls.GUILDS_DIR.mkdir(parents=True, exist_ok=True)
        cls.DATABASE_DIR.mkdir(parents=True, exist_ok=True)
        cls.TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    @classmethod
    def ensure_guild_dir(cls, guild_id: int):
        guild_dir = cls.guild_dir(guild_id)
        guild_dir.mkdir(parents=True, exist_ok=True)
        return guild_dir
