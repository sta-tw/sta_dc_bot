from __future__ import annotations

from pathlib import Path

from bot import build_bot
from bot.utils.config import require_env


def main() -> None:
    bot = build_bot(Path("config/settings.json"))
    token = require_env("DISCORD_TOKEN")
    bot.run(token)


if __name__ == "__main__":
    main()
