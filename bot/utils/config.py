from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(slots=True)
class Settings:

    guild_id: int
    welcome_channel_id: int
    ticket_category_id: int
    ticket_panel_channel_id: int
    support_role_ids: list[int]
    transcript_dir: Path
    extensions: list[str] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path) -> "Settings":
        data = json.loads(path.read_text(encoding="utf-8"))
        transcript_dir = Path(data["transcript_dir"]).expanduser().resolve()
        transcript_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            guild_id=int(data["guild_id"]),
            welcome_channel_id=int(data["welcome_channel_id"]),
            ticket_category_id=int(data["ticket_category_id"]),
            ticket_panel_channel_id=int(data.get("ticket_panel_channel_id", 0)),
            support_role_ids=[int(role_id) for role_id in data.get("support_role_ids", [])],
            transcript_dir=transcript_dir,
            extensions=list(data.get("extensions", [])),
        )


def require_env(name: str) -> str:
    from os import getenv

    result = getenv(name)
    if result is None or not result.strip():
        raise RuntimeError(f"Environment variable {name} is not set")
    return result
