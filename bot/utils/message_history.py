from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Awaitable, Callable

import discord


SCOPE = "readable-text-active-threads-public-archives-v1"
MAX_INTEGER = (1 << 63) - 1


class HistoryCollectionError(ValueError):
    pass


def boundary_id(moment: datetime) -> int:
    aligned = moment.replace(microsecond=moment.microsecond // 1000 * 1000)
    return discord.utils.time_snowflake(aligned, high=False)


def next_boundary_id() -> int:
    return boundary_id(discord.utils.utcnow() + timedelta(milliseconds=2))


def is_countable_message(message) -> bool:
    return message.guild is not None and not message.author.bot and message.webhook_id is None


def _integer(value, *, positive: bool = True) -> int:
    if type(value) is not int or value > MAX_INTEGER or value < (1 if positive else 0):
        raise ValueError("Invalid history integer")
    return value


def _identifier(value) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise ValueError("Invalid history identifier")
    number = _integer(int(value))
    if str(number) != value:
        raise ValueError("Invalid history identifier")
    return number


def validate_counts(value) -> Counter[int]:
    if not isinstance(value, dict):
        raise ValueError("Invalid history counts")
    return Counter({_identifier(user_id): _integer(count) for user_id, count in value.items()})


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate history JSON key")
        result[key] = value
    return result


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("Invalid history JSON")
    return value


def _canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class HistoryProgress:
    phase: str = "準備頻道"
    channel_id: int | None = None
    channels_done: int = 0
    channels_total: int = 0
    messages_read: int = 0
    messages_counted: int = 0
    error: str | None = None

    def text(self) -> str:
        status = f"{self.phase}；頻道 {self.channels_done}/{self.channels_total}，已統計 {self.messages_counted:,} 則訊息。"
        if self.error:
            return f"排行榜初始化失敗：{self.error}。再次執行指令可重試。"
        return status + "統計在背景進行，完成後請重新執行此指令。"


@dataclass
class HistoryScope:
    guild_id: int
    channels: list = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class HistorySnapshot:
    guild_id: int
    cutoff_id: int
    counts: Counter[int]
    channel_ids: frozenset[int]
    checksum: str
    excluded: tuple[dict, ...]


def load_snapshot(path: Path, guild_id: int) -> HistorySnapshot:
    payload = read_json(path)
    checksum = payload.pop("sha256", None)
    if not isinstance(checksum, str) or hashlib.sha256(_canonical(payload)).hexdigest() != checksum:
        raise ValueError("History snapshot checksum mismatch")
    if type(payload.get("version")) is not int or payload.get("version") != 1 or payload.get("scope") != SCOPE:
        raise ValueError("Unsupported history snapshot")
    if _identifier(payload.get("guild_id")) != guild_id:
        raise ValueError("History snapshot belongs to another guild")
    if payload.get("complete") is not True or payload.get("failures") != []:
        raise ValueError("History snapshot is incomplete")
    cutoff_id = _identifier(payload.get("cutoff_id"))
    if cutoff_id & ((1 << 22) - 1) or cutoff_id > boundary_id(discord.utils.utcnow()):
        raise ValueError("Invalid or future history cutoff")
    ids = payload.get("channel_ids")
    if not isinstance(ids, list) or not ids:
        raise ValueError("Invalid history inventory")
    channel_ids = frozenset(_identifier(value) for value in ids)
    if len(channel_ids) != len(ids):
        raise ValueError("Invalid history inventory")
    if payload.get("completed_channel_ids") != ids:
        raise ValueError("History inventory is incomplete")
    counts = validate_counts(payload.get("counts"))
    if _integer(payload.get("total_messages"), positive=False) != sum(counts.values()):
        raise ValueError("History snapshot total mismatch")
    excluded = payload.get("excluded")
    if not isinstance(excluded, list):
        raise ValueError("Invalid history exclusions")
    for item in excluded:
        if not isinstance(item, dict) or item.get("reason") != "missing_read_permission":
            raise ValueError("Invalid history exclusion")
        _identifier(item.get("channel_id"))
    return HistorySnapshot(guild_id, cutoff_id, counts, channel_ids, checksum, tuple(excluded))


def write_snapshot(path: Path, scope: HistoryScope, cutoff_id: int, counts: Counter[int]) -> HistorySnapshot:
    ids = [str(channel.id) for channel in sorted(scope.channels, key=lambda channel: channel.id)]
    payload = {
        "version": 1,
        "scope": SCOPE,
        "guild_id": str(scope.guild_id),
        "cutoff_id": str(cutoff_id),
        "cutoff_utc": discord.utils.snowflake_time(cutoff_id).isoformat(),
        "complete": True,
        "failures": [],
        "channel_ids": ids,
        "completed_channel_ids": ids,
        "excluded": scope.excluded,
        "counts": {str(user_id): count for user_id, count in counts.items() if count > 0},
        "total_messages": sum(counts.values()),
    }
    payload["sha256"] = hashlib.sha256(_canonical(payload)).hexdigest()
    atomic_json(path, payload)
    return load_snapshot(path, scope.guild_id)


async def discover_scope(
    guild, bot_user_id: int, progress: HistoryProgress,
    *, report: Callable[[HistoryProgress], None] | None = None,
) -> HistoryScope:
    scope = HistoryScope(guild.id)
    seen: set[int] = set()
    try:
        channels = await guild.fetch_channels()
        threads = await guild.active_threads()
        member = await guild.fetch_member(bot_user_id)
        readable_parents = []
        for channel in channels:
            if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
                continue
            permissions = channel.permissions_for(member)
            if not permissions.view_channel or not permissions.read_message_history:
                scope.excluded.append({"channel_id": str(channel.id), "reason": "missing_read_permission"})
                continue
            readable_parents.append(channel)
            if isinstance(channel, discord.TextChannel):
                scope.channels.append(channel)
                seen.add(channel.id)
        readable_ids = {channel.id for channel in readable_parents}
        for thread in threads:
            if thread.parent_id in readable_ids and thread.id not in seen:
                scope.channels.append(thread)
                seen.add(thread.id)
        progress.phase = "列出封存討論串"
        for parent in readable_parents:
            progress.channel_id = parent.id
            progress.channels_total = len(scope.channels)
            if report is not None:
                report(progress)
            async for thread in parent.archived_threads(limit=None):
                if thread.id not in seen:
                    scope.channels.append(thread)
                    seen.add(thread.id)
                    progress.channels_total = len(scope.channels)
                if report is not None:
                    report(progress)
    except (discord.HTTPException, OSError) as exc:
        status = getattr(exc, "status", type(exc).__name__)
        raise HistoryCollectionError(f"頻道／討論串清單讀取失敗 ({status})") from exc
    if not scope.channels:
        raise HistoryCollectionError("沒有可讀取歷史的頻道")
    progress.channels_total = len(scope.channels)
    return scope


async def collect_history(
    scope: HistoryScope,
    *,
    upper_id: int,
    progress: HistoryProgress,
    snapshot: HistorySnapshot | None = None,
    completed: dict[str, dict[str, int]] | None = None,
    cursors: dict[str, int] | None = None,
    concurrency: int = 3,
    checkpoint: Callable[[int, Counter[int], int | None], Awaitable[None]] | None = None,
    report: Callable[[HistoryProgress], None] | None = None,
) -> Counter[int]:
    if snapshot is not None and (snapshot.guild_id != scope.guild_id or snapshot.cutoff_id >= upper_id):
        raise ValueError("History snapshot boundary does not precede live boundary")
    if not 1 <= concurrency <= 8:
        raise ValueError("Invalid history concurrency")
    counts = snapshot.counts.copy() if snapshot is not None else Counter()
    completed = completed or {}
    cursors = cursors or {}
    progress.channels_total = len(scope.channels)
    progress.phase = "補掃快照後的訊息" if snapshot is not None else "掃描歷史訊息"
    semaphore = asyncio.Semaphore(concurrency)
    first_error: HistoryCollectionError | None = None

    async def scan_one(channel) -> None:
        nonlocal first_error
        cid = str(channel.id)
        saved_counts = completed.get(cid)
        cursor = cursors.get(cid)
        async with semaphore:
            if first_error is not None:
                return
            if saved_counts is not None and cursor is None:
                channel_counts = validate_counts(saved_counts)
                counts.update(channel_counts)
                progress.messages_counted += sum(channel_counts.values())
                progress.channels_done += 1
                if report is not None:
                    report(progress)
                return
            lower_id = None
            if snapshot is not None and channel.id in snapshot.channel_ids:
                lower_id = snapshot.cutoff_id
            channel_counts = validate_counts(saved_counts) if saved_counts is not None else Counter()
            kwargs: dict = {
                "limit": None,
                "oldest_first": False,
                "before": discord.Object(id=cursor if cursor is not None else upper_id),
            }
            if lower_id is not None:
                kwargs["after"] = discord.Object(id=lower_id - 1)
            last_cursor = None
            pending = 0
            iterator = channel.history(**kwargs).__aiter__()
            try:
                while True:
                    try:
                        message = await asyncio.wait_for(anext(iterator), timeout=300)
                    except StopAsyncIteration:
                        break
                    progress.messages_read += 1
                    if message.id >= upper_id or (lower_id is not None and message.id < lower_id):
                        continue
                    last_cursor = message.id
                    if is_countable_message(message):
                        channel_counts[message.author.id] += 1
                        progress.messages_counted += 1
                    pending += 1
                    if checkpoint is not None and pending >= 1000:
                        pending = 0
                        await checkpoint(channel.id, channel_counts, last_cursor)
                    if report is not None and progress.messages_read % 100 == 0:
                        report(progress)
            except (discord.HTTPException, OSError, TimeoutError) as exc:
                status = getattr(exc, "status", type(exc).__name__)
                error = HistoryCollectionError(f"頻道 {channel.id} 歷史讀取失敗 ({status})")
                if first_error is None:
                    first_error = error
                raise error from exc
            if checkpoint is not None:
                await checkpoint(channel.id, channel_counts, None)
            counts.update(channel_counts)
            progress.channels_done += 1
            if report is not None:
                report(progress)

    await asyncio.gather(*(scan_one(channel) for channel in scope.channels), return_exceptions=True)
    if first_error is not None:
        raise first_error
    progress.phase = "歷史掃描完成"
    return counts
