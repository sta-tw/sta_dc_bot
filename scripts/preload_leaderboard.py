from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import sys
from time import monotonic
from types import SimpleNamespace

import discord
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.utils.message_history import (
    SCOPE,
    HistoryProgress,
    atomic_json,
    boundary_id,
    collect_history,
    discover_scope,
    load_snapshot,
    read_json,
    validate_counts,
    write_snapshot,
)


def emit(event: str, **values) -> None:
    print(json.dumps({"event": event, **values}, ensure_ascii=True), flush=True)


async def preload(guild_id: int, output: Path, state_path: Path, concurrency: int = 3) -> None:
    if output.exists():
        snapshot = load_snapshot(output, guild_id)
        emit("complete", reused=True, guild_id=str(guild_id), users=len(snapshot.counts),
             messages=sum(snapshot.counts.values()), cutoff_id=str(snapshot.cutoff_id),
             sha256=snapshot.checksum, excluded=len(snapshot.excluded), output=str(output))
        return
    load_dotenv(ROOT / ".env", override=False)
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN is not configured")
    if state_path.exists():
        state = read_json(state_path)
        if state.get("version") != 1 or state.get("scope") != SCOPE or state.get("guild_id") != str(guild_id):
            raise ValueError("Checkpoint belongs to another guild or scope")
        cutoff_id = int(state["cutoff_id"])
        if cutoff_id <= 0 or cutoff_id & ((1 << 22) - 1) or cutoff_id > boundary_id(discord.utils.utcnow()):
            raise ValueError("Checkpoint has an invalid cutoff")
        if not isinstance(state.get("completed"), dict) or not isinstance(state.get("channel_ids"), list):
            raise ValueError("Invalid checkpoint inventory")
        for counts in state["completed"].values():
            validate_counts(counts)
        state.setdefault("cursors", {})
    else:
        cutoff_id = boundary_id(discord.utils.utcnow())
        state = {"version": 1, "scope": SCOPE, "guild_id": str(guild_id),
                 "cutoff_id": str(cutoff_id), "channel_ids": [], "completed": {}, "cursors": {}}
    progress = HistoryProgress()
    last_report = 0.0

    def report(value: HistoryProgress) -> None:
        nonlocal last_report
        now = monotonic()
        if now - last_report < 20:
            return
        last_report = now
        emit("progress", phase=value.phase, channel_id=str(value.channel_id),
             channels_done=value.channels_done, channels_total=value.channels_total,
             messages_read=value.messages_read, messages_counted=value.messages_counted)

    async def checkpoint(channel_id: int, counts: Counter[int], cursor: int | None) -> None:
        cid = str(channel_id)
        if cursor is None:
            state["completed"][cid] = {str(user_id): count for user_id, count in counts.items()}
            state["cursors"].pop(cid, None)
        else:
            state["cursors"][cid] = str(cursor)
            if cid in state["completed"]:
                state["completed"][cid] = {str(user_id): count for user_id, count in counts.items()}
        atomic_json(state_path, state)

    emit("starting", guild_id=str(guild_id), gateway=False, concurrency=concurrency,
         cutoff_id=str(cutoff_id), completed_channels=len(state["completed"]),
         in_progress_channels=len(state["cursors"]))
    async with discord.Client(intents=discord.Intents.none()) as client:
        await client.login(token)
        if not client.user.bot:
            raise ValueError("DISCORD_TOKEN does not belong to a bot account")
        emit("authenticated", bot_id=str(client.user.id), gateway=False)
        try:
            guild = await client.fetch_guild(guild_id)
        except discord.NotFound as exc:
            raise ValueError(f"Local bot {client.user.id} cannot access target guild {guild_id} (404)") from exc
        async with asyncio.timeout(6 * 60 * 60):
            scope = await discover_scope(guild, client.user.id, progress, report=report)
            visible_ids = {str(channel.id) for channel in scope.channels}
            missing = set(state["channel_ids"]) - visible_ids
            if missing & set(state["cursors"]):
                raise ValueError("An in-progress channel is no longer readable")
            for channel_id in missing - set(state["completed"]):
                scope.channels.append(SimpleNamespace(id=int(channel_id)))
            state["channel_ids"] = [str(channel.id) for channel in scope.channels]
            atomic_json(state_path, state)
            report(progress)
            counts = await collect_history(
                scope, upper_id=cutoff_id, progress=progress, completed=state["completed"],
                cursors={key: int(value) for key, value in state["cursors"].items()},
                concurrency=concurrency, checkpoint=checkpoint, report=report,
            )
    snapshot = write_snapshot(output, scope, cutoff_id, counts)
    emit("complete", reused=False, guild_id=str(guild_id), users=len(snapshot.counts),
         messages=sum(snapshot.counts.values()), channels=len(snapshot.channel_ids),
         cutoff_id=str(snapshot.cutoff_id), sha256=snapshot.checksum,
         excluded=len(snapshot.excluded), output=str(output))


def main() -> int:
    parser = argparse.ArgumentParser(description="Preload leaderboard history using REST only; no gateway or database writes.")
    parser.add_argument("--guild-id", type=int, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()
    output = args.output or ROOT / "data" / "leaderboard_preloads" / f"{args.guild_id}.json"
    state = args.state or output.with_suffix(".checkpoint.json")
    if output.resolve() == state.resolve() or args.guild_id <= 0 or not 1 <= args.concurrency <= 8:
        parser.error("Output and checkpoint must differ, and guild ID and concurrency must be valid")
    try:
        asyncio.run(preload(args.guild_id, output, state, concurrency=args.concurrency))
    except KeyboardInterrupt:
        emit("cancelled", checkpoint=str(state))
        return 130
    except Exception as exc:
        error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        emit("failed", error=error, checkpoint=str(state))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
