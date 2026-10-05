from __future__ import annotations

import asyncio
import inspect
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable

from bot.utils.ai_context import estimate_tokens


@dataclass(frozen=True, slots=True)
class ConversationScope:
    """A single guild channel or thread; thread history never falls back to its parent."""

    guild_id: int
    channel_id: int
    parent_channel_id: int | None = None

    def as_dict(self) -> dict[str, int | None]:
        return {
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "parent_channel_id": self.parent_channel_id,
        }


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    message_id: int
    author_id: int | None
    author_name: str
    timestamp: float | None
    reply_to_id: int | None
    content: str
    is_bot: bool
    source: str

    def as_dict(self) -> dict[str, int | float | str | bool | None]:
        return {
            "message_id": self.message_id,
            "author_id": self.author_id,
            "author_name": self.author_name,
            "timestamp": self.timestamp,
            "reply_to_id": self.reply_to_id,
            "content": self.content,
            "is_bot": self.is_bot,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ScopedConversation:
    scope: ConversationScope
    records: tuple[ConversationRecord, ...]
    missing_ids: tuple[int, ...]
    revision: int
    latest_id: int | None
    reply_branch_ids: tuple[int, ...] = ()
    _generation: int = field(default=0, repr=False, compare=False)
    _valid_at_collect: bool = field(default=True, repr=False, compare=False)

    def as_data(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot without internal validity state."""
        return {
            "scope": self.scope.as_dict(),
            "records": [record.as_dict() for record in self.records],
            "missing_ids": list(self.missing_ids),
            "revision": self.revision,
            "latest_id": self.latest_id,
            "reply_branch_ids": list(self.reply_branch_ids),
        }


AllowedCallback = Callable[[ConversationScope], Any]


@dataclass
class _Session:
    scope: ConversationScope
    generation: int
    revision: int
    last_access: float
    records: OrderedDict[int, ConversationRecord] = field(default_factory=OrderedDict)
    refresh_ids: OrderedDict[int, None] = field(default_factory=OrderedDict)
    deleted_ids: OrderedDict[int, None] = field(default_factory=OrderedDict)
    block_resolved_refs: bool = False
    block_all_records: bool = False
    allowed_callback: AllowedCallback | None = None


class ConversationManager:
    """Bounded, in-memory conversation snapshots isolated to one guild channel/thread."""

    def __init__(
        self,
        ttl_seconds: float = 1800,
        max_sessions: int = 100,
        max_records: int = 300,
        history_page_size: int = 50,
        max_api_calls: int = 6,
        fetch_timeout: float = 5.0,
    ) -> None:
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.max_sessions = max(1, int(max_sessions))
        self.max_records = max(1, int(max_records))
        self.history_page_size = max(1, min(100, int(history_page_size)))
        self.max_api_calls = max(0, int(max_api_calls))
        self.fetch_timeout = max(0.001, float(fetch_timeout))
        self._sessions: OrderedDict[ConversationScope, _Session] = OrderedDict()
        self._revision_counter = 0
        self._generation_counter = 0

    def _next_revision(self) -> int:
        self._revision_counter += 1
        return self._revision_counter

    def _next_generation(self) -> int:
        self._generation_counter += 1
        return self._generation_counter

    @staticmethod
    def _as_id(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(getattr(value, "id", value))
        except (TypeError, ValueError, OverflowError):
            return None

    @classmethod
    def _value_id(cls, obj: Any, name: str) -> int | None:
        return cls._as_id(getattr(obj, name, None)) if obj is not None else None

    @classmethod
    def _message_scope(cls, message: Any) -> ConversationScope | None:
        channel = getattr(message, "channel", None)
        guild = getattr(message, "guild", None) or getattr(channel, "guild", None)
        guild_id = cls._value_id(guild, "id")
        channel_id = cls._value_id(channel, "id")
        if channel_id is None:
            channel_id = cls._value_id(message, "channel_id")
        if guild_id is None:
            guild_id = cls._value_id(message, "guild_id")
        if guild_id is None or channel_id is None:
            return None

        channel_guild_id = cls._value_id(getattr(channel, "guild", None), "id")
        if channel_guild_id is None:
            channel_guild_id = cls._value_id(channel, "guild_id")
        if channel_guild_id is not None and channel_guild_id != guild_id:
            return None

        parent_id = cls._value_id(channel, "parent_id")
        if parent_id is None:
            parent_id = cls._value_id(getattr(channel, "parent", None), "id")
        return ConversationScope(guild_id, channel_id, parent_id)

    @classmethod
    def _matches_scope(cls, message: Any, scope: ConversationScope) -> bool:
        channel = getattr(message, "channel", None)
        channel_id = cls._value_id(channel, "id")
        if channel_id is None:
            channel_id = cls._value_id(message, "channel_id")
        if channel_id != scope.channel_id:
            return False

        guild = getattr(message, "guild", None) or getattr(channel, "guild", None)
        guild_id = cls._value_id(guild, "id")
        if guild_id is None:
            guild_id = cls._value_id(message, "guild_id")
        channel_guild_id = cls._value_id(getattr(channel, "guild", None), "id")
        if channel_guild_id is None:
            channel_guild_id = cls._value_id(channel, "guild_id")
        return all(
            known_id is None or known_id == scope.guild_id
            for known_id in (guild_id, channel_guild_id)
        )

    @classmethod
    def _reference_target(
        cls,
        reference: Any,
        scope: ConversationScope,
    ) -> tuple[int | None, Any | None, bool]:
        if reference is None:
            return None, None, True
        resolved = getattr(reference, "resolved", None)
        target_id = cls._value_id(reference, "message_id")
        if target_id is None:
            target_id = cls._value_id(resolved, "id")
        ref_channel_id = cls._value_id(reference, "channel_id")
        ref_guild_id = cls._value_id(reference, "guild_id")
        if ref_channel_id is not None and ref_channel_id != scope.channel_id:
            return target_id, None, False
        if ref_guild_id is not None and ref_guild_id != scope.guild_id:
            return target_id, None, False
        if resolved is not None:
            resolved_id = cls._value_id(resolved, "id")
            if target_id is not None and resolved_id != target_id:
                return target_id, None, False
            if not cls._matches_scope(resolved, scope):
                return target_id, None, False
        return target_id, resolved, True

    @classmethod
    def _message_reference(cls, message: Any) -> Any | None:
        return getattr(message, "reference", None) or getattr(message, "message_reference", None)

    @classmethod
    def _timestamp(cls, message: Any) -> float | None:
        raw = getattr(message, "created_at", None)
        if raw is None:
            raw = getattr(message, "timestamp", None)
        if raw is not None:
            try:
                if hasattr(raw, "timestamp"):
                    return float(raw.timestamp())
                return float(raw)
            except (TypeError, ValueError, OverflowError, OSError):
                pass
        message_id = cls._value_id(message, "id")
        if message_id is not None and message_id > (1 << 22):
            # Discord snowflakes encode creation time in their upper bits.
            return ((message_id >> 22) + 1420070400000) / 1000.0
        return None

    @staticmethod
    def _safe_name(author: Any) -> str:
        name = str(
            getattr(author, "display_name", None)
            or getattr(author, "name", None)
            or "未命名成員"
        )
        normalized = " ".join(name.replace("\r", " ").replace("\n", " ").split())
        return (normalized[:100] or "未命名成員")

    @classmethod
    def _record_from_message(
        cls,
        message: Any,
        scope: ConversationScope,
        *,
        bot_user_id: int | None,
        source: str,
        assume_own_bot: bool = False,
    ) -> ConversationRecord | None:
        if not cls._matches_scope(message, scope):
            return None
        message_id = cls._value_id(message, "id")
        if message_id is None:
            return None
        author = getattr(message, "author", None)
        author_id = cls._value_id(author, "id")
        is_bot = bool(getattr(author, "bot", False)) or (
            bot_user_id is not None and author_id == bot_user_id
        )
        is_own_bot = (
            (bot_user_id is not None and author_id == bot_user_id)
            or (assume_own_bot and is_bot)
        )
        if is_bot and not is_own_bot:
            return None
        if getattr(message, "webhook_id", None) is not None and not is_own_bot:
            return None
        content = str(getattr(message, "content", None) or "").strip()
        if not content:
            return None

        reference = cls._message_reference(message)
        reply_to_id, _, ref_matches = cls._reference_target(reference, scope)
        if not ref_matches:
            reply_to_id = None
        return ConversationRecord(
            message_id=message_id,
            author_id=author_id,
            author_name=cls._safe_name(author),
            timestamp=cls._timestamp(message),
            reply_to_id=reply_to_id,
            content=content,
            is_bot=is_bot,
            source="own_bot_reply" if is_own_bot else source,
        )

    def _prune_expired(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        expired = [
            scope
            for scope, session in self._sessions.items()
            if current - session.last_access >= self.ttl_seconds
        ]
        for scope in expired:
            self._sessions.pop(scope, None)

    def _get_session(self, scope: ConversationScope) -> _Session:
        now = time.monotonic()
        self._prune_expired(now)
        existing = self._sessions.get(scope)
        if existing is not None:
            existing.last_access = now
            self._sessions.move_to_end(scope)
            return existing

        # Channel-level invalidations are kept under the parentless scope. A
        # thread can inherit only the guard metadata, never its messages.
        parentless = ConversationScope(scope.guild_id, scope.channel_id, None)
        guard = self._sessions.get(parentless) if scope.parent_channel_id is not None else None
        session = _Session(
            scope=scope,
            generation=self._next_generation(),
            revision=guard.revision if guard is not None else self._next_revision(),
            last_access=now,
        )
        if guard is not None:
            session.refresh_ids.update(guard.refresh_ids)
            session.deleted_ids.update(guard.deleted_ids)
            session.block_resolved_refs = guard.block_resolved_refs
            session.block_all_records = guard.block_all_records
        self._sessions[scope] = session
        self._sessions.move_to_end(scope)
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)
        return session

    def _is_active(self, session: _Session, revision: int) -> bool:
        now = time.monotonic()
        self._prune_expired(now)
        return (
            self._sessions.get(session.scope) is session
            and session.revision == revision
            and now - session.last_access < self.ttl_seconds
        )

    async def _allowed(self, callback: AllowedCallback | None, scope: ConversationScope) -> bool:
        if callback is None:
            return True
        try:
            result = callback(scope)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=self.fetch_timeout)
            return bool(result)
        except Exception:
            return False

    def _scope_context(
        self,
        session: _Session,
        records: Iterable[ConversationRecord] = (),
        missing_ids: Iterable[int] = (),
        latest_id: int | None = None,
        *,
        valid: bool = True,
        reply_branch_ids: Iterable[int] = (),
    ) -> ScopedConversation:
        return ScopedConversation(
            scope=session.scope,
            records=tuple(records),
            missing_ids=tuple(dict.fromkeys(missing_ids)),
            revision=session.revision,
            latest_id=latest_id,
            reply_branch_ids=tuple(dict.fromkeys(reply_branch_ids)),
            _generation=session.generation,
            _valid_at_collect=valid,
        )

    def capture_guard(self, scope: ConversationScope) -> ScopedConversation:
        """Capture bounded validity metadata without exposing cached records."""
        return self._scope_context(self._get_session(scope))

    def _remember_record(self, session: _Session, record: ConversationRecord) -> None:
        if session.block_all_records or record.message_id in session.deleted_ids:
            return
        session.refresh_ids.pop(record.message_id, None)
        session.records[record.message_id] = record
        session.records.move_to_end(record.message_id)
        while (
            len(session.records) + len(session.refresh_ids) + len(session.deleted_ids)
            > self.max_records
        ):
            if session.records:
                session.records.popitem(last=False)
                continue
            if session.refresh_ids:
                session.refresh_ids.clear()
                session.block_resolved_refs = True
                break
            session.block_all_records = True
            session.deleted_ids.clear()
            break

    def _add_refresh_id(self, session: _Session, message_id: int) -> None:
        if message_id in session.deleted_ids or session.block_all_records:
            return
        session.records.pop(message_id, None)
        session.refresh_ids[message_id] = None
        session.refresh_ids.move_to_end(message_id)
        while (
            len(session.records) + len(session.refresh_ids) + len(session.deleted_ids)
            > self.max_records
        ):
            if session.records:
                session.records.popitem(last=False)
                continue
            # A bounded overflow drops per-ID refresh metadata and blocks all
            # embedded resolved objects until this idle session expires.
            session.refresh_ids.clear()
            session.records.clear()
            session.block_resolved_refs = True
            break

    def _add_deleted_id(self, session: _Session, message_id: int) -> None:
        session.records.pop(message_id, None)
        session.refresh_ids.pop(message_id, None)
        session.deleted_ids[message_id] = None
        session.deleted_ids.move_to_end(message_id)
        if len(session.deleted_ids) > self.max_records:
            # A boolean fail-closed guard bounds tombstone metadata while
            # ensuring no deleted ID can re-enter from cache, history, or refs.
            session.records.clear()
            session.refresh_ids.clear()
            session.deleted_ids.clear()
            session.block_all_records = True
            return
        while (
            len(session.records) + len(session.refresh_ids) + len(session.deleted_ids)
            > self.max_records
        ):
            if session.records:
                session.records.popitem(last=False)
            elif session.refresh_ids:
                session.refresh_ids.clear()
                session.block_resolved_refs = True
            else:
                session.block_all_records = True
                session.deleted_ids.clear()
                break

    def remember(self, message: Any, *, bot_user_id: int | None = None) -> ConversationRecord | None:
        """Cache an actually observed Discord message (normally the sent bot reply)."""
        scope = self._message_scope(message)
        if scope is None:
            return None
        author = getattr(message, "author", None)
        assume_own_bot = bot_user_id is None and bool(getattr(author, "bot", False))
        record = self._record_from_message(
            message,
            scope,
            bot_user_id=bot_user_id,
            source="remembered",
            assume_own_bot=assume_own_bot,
        )
        if record is None:
            return None
        session = self._get_session(scope)
        if session.block_all_records or record.message_id in session.deleted_ids:
            return None
        previous = session.records.get(record.message_id)
        if previous is not None and (
            previous.content != record.content
            or previous.author_id != record.author_id
            or previous.reply_to_id != record.reply_to_id
        ):
            session.revision = self._next_revision()
        self._remember_record(session, record)
        session.last_access = time.monotonic()
        return record

    def invalidate_message(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        deleted: bool = False,
    ) -> None:
        """Invalidate edits for refresh, or retain a bounded delete tombstone."""
        guild = self._as_id(guild_id)
        channel = self._as_id(channel_id)
        target = self._as_id(message_id)
        if guild is None or channel is None or target is None:
            return
        self._prune_expired()
        matches = [
            session
            for scope, session in self._sessions.items()
            if scope.guild_id == guild and scope.channel_id == channel
        ]
        if not matches:
            matches = [self._get_session(ConversationScope(guild, channel, None))]
        revision = self._next_revision()
        now = time.monotonic()
        for session in matches:
            session.revision = revision
            session.last_access = now
            if deleted:
                self._add_deleted_id(session, target)
            else:
                self._add_refresh_id(session, target)

    def invalidate_channel(
        self,
        guild_id: int,
        channel_id: int,
        include_children: bool = True,
    ) -> None:
        """Invalidate a channel's context epoch, optionally including its threads."""
        guild = self._as_id(guild_id)
        channel = self._as_id(channel_id)
        if guild is None or channel is None:
            return
        self._prune_expired()
        matches = [
            session
            for scope, session in self._sessions.items()
            if scope.guild_id == guild
            and (
                scope.channel_id == channel
                or (include_children and scope.parent_channel_id == channel)
            )
        ]
        if not matches:
            matches = [self._get_session(ConversationScope(guild, channel, None))]
        revision = self._next_revision()
        now = time.monotonic()
        for session in matches:
            session.revision = revision
            session.last_access = now
            session.records.clear()
            session.refresh_ids.clear()
            session.block_resolved_refs = True

    def is_current(self, context: ScopedConversation) -> bool:
        """Return false after invalidation, expiry, eviction, or permission change."""
        if not isinstance(context, ScopedConversation) or not context._valid_at_collect:
            return False
        self._prune_expired()
        session = self._sessions.get(context.scope)
        if (
            session is None
            or session.generation != context._generation
            or session.revision != context.revision
        ):
            return False
        callback = session.allowed_callback
        if callback is not None:
            try:
                result = callback(context.scope)
                if inspect.isawaitable(result):
                    close = getattr(result, "close", None)
                    if callable(close):
                        close()
                    return False
                if not bool(result):
                    return False
            except Exception:
                return False
        return True

    def validate_source_ids(
        self,
        context: ScopedConversation,
        source_ids: Iterable[int],
    ) -> bool:
        """Check that every summary provenance ID belongs to this current snapshot."""
        if not self.is_current(context):
            return False
        try:
            requested = {int(source_id) for source_id in source_ids}
        except (TypeError, ValueError, OverflowError):
            return False
        record_ids = {record.message_id for record in context.records}
        return requested.issubset(record_ids)

    def _is_tombstoned(self, session: _Session, message_id: int) -> bool:
        return message_id in session.deleted_ids

    @staticmethod
    def _sort_key(record: ConversationRecord) -> tuple[float, int]:
        return (record.timestamp if record.timestamp is not None else float(record.message_id), record.message_id)

    @staticmethod
    def _before_trigger(record: ConversationRecord, trigger_id: int | None, trigger_time: float | None) -> bool:
        if trigger_id is not None and record.message_id >= trigger_id:
            return False
        if trigger_id is None and trigger_time is not None and record.timestamp is not None:
            return record.timestamp < trigger_time
        return True

    async def _fetch_message(self, channel: Any, message_id: int) -> Any | None:
        fetcher = getattr(channel, "fetch_message", None)
        if not callable(fetcher):
            return None
        result = fetcher(message_id)
        if inspect.isawaitable(result):
            return await asyncio.wait_for(result, timeout=self.fetch_timeout)
        return result

    async def _history_page(self, channel: Any, before: Any, limit: int) -> list[Any]:
        history = getattr(channel, "history", None)
        if not callable(history):
            return []
        iterator = history(limit=limit, before=before, oldest_first=False)
        if inspect.isawaitable(iterator):
            iterator = await iterator
        page: list[Any] = []
        if hasattr(iterator, "__aiter__"):
            async for item in iterator:
                page.append(item)
                if len(page) >= limit:
                    break
        else:
            for item in iterator:
                page.append(item)
                if len(page) >= limit:
                    break
        return page

    def _record_tokens(self, record: ConversationRecord) -> int:
        identity = record.author_name
        if record.author_id is not None:
            identity += f" ({record.author_id})"
        return estimate_tokens(f"{identity}: {record.content}") + 6

    def _fit_record(self, record: ConversationRecord, budget: int) -> ConversationRecord | None:
        if budget <= 0:
            return None
        if self._record_tokens(record) <= budget:
            return record
        if not record.content:
            return None

        low, high = 1, len(record.content) - 1
        best: ConversationRecord | None = None
        while low <= high:
            retained = (low + high) // 2
            head_size = (retained + 1) // 2
            tail_size = retained // 2
            tail = record.content[-tail_size:] if tail_size else ""
            content = f"{record.content[:head_size]}…{tail}"
            source = record.source
            if not source.endswith("_excerpt"):
                source = f"{source}_excerpt"
            candidate = replace(record, content=content, source=source)
            if self._record_tokens(candidate) <= budget:
                best = candidate
                low = retained + 1
            else:
                high = retained - 1
        return best

    def _approximate_tokens(self, records: Iterable[ConversationRecord]) -> int:
        unique = {record.message_id: record for record in records}
        return sum(self._record_tokens(record) for record in unique.values())

    def _select_records(
        self,
        records_by_id: dict[int, ConversationRecord],
        ancestor_direct_first: list[ConversationRecord],
        token_budget: int,
    ) -> tuple[ConversationRecord, ...]:
        remaining = max(0, int(token_budget))
        if remaining <= 0:
            return ()

        ancestor_ids = {record.message_id for record in ancestor_direct_first}
        descendant_ids: set[int] = set()
        related = set(ancestor_ids)
        # Include complete observed reply branches below the reply ancestors.
        changed = True
        while changed:
            changed = False
            for record in records_by_id.values():
                if (
                    record.message_id not in related
                    and record.reply_to_id is not None
                    and record.reply_to_id in related
                ):
                    related.add(record.message_id)
                    descendant_ids.add(record.message_id)
                    changed = True

        ancestors = list(ancestor_direct_first)
        branches = [records_by_id[mid] for mid in descendant_ids if mid in records_by_id]
        branches.sort(key=self._sort_key, reverse=True)
        nearby = [
            record
            for message_id, record in records_by_id.items()
            if message_id not in ancestor_ids and message_id not in descendant_ids
        ]
        nearby.sort(key=self._sort_key, reverse=True)

        selected: dict[int, ConversationRecord] = {}
        groups = (ancestors, branches, nearby)
        for group in groups:
            for record in group:
                if record.message_id in selected or len(selected) >= self.max_records:
                    continue
                candidate = self._fit_record(record, remaining)
                if candidate is None:
                    continue
                cost = self._record_tokens(candidate)
                selected[record.message_id] = candidate
                remaining -= cost
            # If ancestors themselves exhaust the token budget, lower priority
            # branches and nearby messages correctly remain absent.
            if remaining <= 0:
                break
        return tuple(sorted(selected.values(), key=self._sort_key))

    async def collect(
        self,
        message: Any,
        *,
        bot_user_id: int | None,
        token_budget: int,
        is_allowed: AllowedCallback | None,
    ) -> ScopedConversation:
        """Collect reply ancestors, related branches, then nearby channel history."""
        scope = self._message_scope(message)
        if scope is None:
            # DMs and malformed partial messages are deliberately not collected.
            empty_scope = ConversationScope(0, 0, None)
            return ScopedConversation(
                empty_scope,
                (),
                (),
                0,
                self._value_id(message, "id"),
                _valid_at_collect=False,
            )

        session = self._get_session(scope)
        session.allowed_callback = is_allowed
        start_revision = session.revision
        trigger_id = self._value_id(message, "id")
        trigger_time = self._timestamp(message)
        budget = max(0, int(token_budget))
        missing: list[int] = []
        observed: dict[int, ConversationRecord] = {}
        ancestor_direct_first: list[ConversationRecord] = []
        calls = 0
        channel = getattr(message, "channel", None)

        async def still_allowed() -> bool:
            if not self._is_active(session, start_revision):
                return False
            allowed = await self._allowed(is_allowed, scope)
            return allowed and self._is_active(session, start_revision)

        async def obtain_ancestor(ref: Any, fallback_id: int | None = None) -> tuple[int | None, Any | None, ConversationRecord | None, bool]:
            nonlocal calls
            target_id, resolved, matches = self._reference_target(ref, scope)
            if target_id is None:
                target_id = fallback_id
            if target_id is None:
                return None, None, None, True
            if not matches:
                missing.append(target_id)
                return target_id, None, None, False
            if trigger_id is not None and target_id == trigger_id:
                return target_id, None, None, False
            if self._is_tombstoned(session, target_id):
                missing.append(target_id)
                return target_id, None, None, False
            refresh_required = target_id in session.refresh_ids
            if refresh_required or session.block_resolved_refs:
                resolved = None

            record: ConversationRecord | None = None
            source_message: Any | None = None
            if resolved is not None:
                source_message = resolved
                record = self._record_from_message(
                    resolved,
                    scope,
                    bot_user_id=bot_user_id,
                    source="reply_reference",
                )
            cached = session.records.get(target_id)
            if record is None and cached is not None and not refresh_required:
                record = cached
            if record is None:
                if calls >= self.max_api_calls or not callable(getattr(channel, "fetch_message", None)):
                    missing.append(target_id)
                    return target_id, None, None, False
                if not await still_allowed():
                    return target_id, None, None, False
                calls += 1
                try:
                    source_message = await asyncio.wait_for(
                        self._fetch_message(channel, target_id),
                        timeout=self.fetch_timeout,
                    )
                except Exception:
                    source_message = None
                if not await still_allowed():
                    return target_id, None, None, False
                if (
                    source_message is None
                    or self._value_id(source_message, "id") != target_id
                    or not self._matches_scope(source_message, scope)
                ):
                    missing.append(target_id)
                    return target_id, None, None, False
                record = self._record_from_message(
                    source_message,
                    scope,
                    bot_user_id=bot_user_id,
                    source="reply_reference",
                )
            if record is None or record.message_id != target_id:
                missing.append(target_id)
                return target_id, None, None, False
            if refresh_required and source_message is not None:
                session.refresh_ids.pop(target_id, None)
            observed[target_id] = record
            return target_id, source_message, record, True

        valid = await self._allowed(is_allowed, scope)
        if not valid:
            return self._scope_context(
                session,
                missing_ids=(),
                latest_id=trigger_id,
                valid=False,
            )
        if session.block_all_records:
            return self._scope_context(session, latest_id=trigger_id)

        # Walk the explicit reply chain before spending calls on general history.
        reference = self._message_reference(message)
        next_id, _, reference_matches = self._reference_target(reference, scope)
        if not reference_matches and next_id is not None:
            missing.append(next_id)
            reference = None
            next_id = None
        visited: set[int] = {trigger_id} if trigger_id is not None else set()
        while reference is not None or next_id is not None:
            if not await still_allowed():
                return self._scope_context(session, missing_ids=missing, latest_id=trigger_id, valid=False)
            target_id, source_message, record, found = await obtain_ancestor(reference, next_id)
            reference = None
            next_id = None
            if target_id is None or target_id in visited:
                break
            visited.add(target_id)
            if record is None:
                break
            ancestor_direct_first.append(record)
            if source_message is not None:
                parent_ref = self._message_reference(source_message)
                parent_id, _, parent_matches = self._reference_target(parent_ref, scope)
                if parent_id is not None and not parent_matches:
                    missing.append(parent_id)
                    break
                reference = parent_ref
                next_id = parent_id
            else:
                parent_id = record.reply_to_id
                if parent_id is not None:
                    reference = SimpleReference(parent_id, scope.channel_id, scope.guild_id)
                    next_id = parent_id
            if not found or (next_id is not None and next_id in visited):
                break

        # Page backwards through this exact channel/thread. Every page is a
        # separate bounded API call; duplicate/non-advancing mocks stop safely.
        cached_records = list(session.records.values())
        before: Any = message
        scanned_history_ids: set[int] = set()
        ancestor_tokens = self._approximate_tokens(ancestor_direct_first)
        while (
            budget > 0
            and ancestor_tokens < budget
            and calls < self.max_api_calls
            and len(observed) < self.max_records
        ):
            if not await still_allowed():
                return self._scope_context(session, missing_ids=missing, latest_id=trigger_id, valid=False)
            page_limit = min(self.history_page_size, self.max_records - len(observed))
            if page_limit <= 0 or not callable(getattr(channel, "history", None)):
                break
            calls += 1
            try:
                page = await asyncio.wait_for(
                    self._history_page(channel, before, page_limit),
                    timeout=self.fetch_timeout,
                )
            except Exception:
                break
            if not await still_allowed():
                return self._scope_context(session, missing_ids=missing, latest_id=trigger_id, valid=False)
            if not page:
                break

            page_items: list[Any] = []
            page_progress = False
            useful_ids: set[int] = set()
            for item in page:
                item_id = self._value_id(item, "id")
                if (
                    item_id is None
                    or item_id == trigger_id
                    or (trigger_id is not None and item_id >= trigger_id)
                    or not self._matches_scope(item, scope)
                ):
                    continue
                page_items.append(item)
                if item_id not in scanned_history_ids:
                    scanned_history_ids.add(item_id)
                    page_progress = True
                if item_id in session.deleted_ids:
                    continue
                record = self._record_from_message(
                    item,
                    scope,
                    bot_user_id=bot_user_id,
                    source="history",
                )
                if record is None or not self._before_trigger(record, trigger_id, trigger_time):
                    continue
                session.refresh_ids.pop(item_id, None)
                observed[item_id] = record
                useful_ids.add(item_id)
            if not page_progress or not page_items:
                break

            before = min(page_items, key=lambda item: self._sort_key_for_message(item))
            if useful_ids:
                cached_context = [
                    record
                    for record in cached_records
                    if record.message_id not in session.deleted_ids
                    and record.message_id not in session.refresh_ids
                    and self._before_trigger(record, trigger_id, trigger_time)
                ]
                coverage_tokens = self._approximate_tokens(
                    [*ancestor_direct_first, *cached_context, *observed.values()]
                )
                # Always inspect one useful nearby page unless reply ancestors
                # already consume the budget; later pages stop when coverage is full.
                if coverage_tokens >= budget:
                    break
            if len(page) < page_limit:
                break

        if not await still_allowed():
            return self._scope_context(session, missing_ids=missing, latest_id=trigger_id, valid=False)

        # Merge only same-scope, pre-trigger cache records. Network results win
        # over cached copies, which may have been invalidated or edited.
        records_by_id: dict[int, ConversationRecord] = {}
        for record in cached_records:
            if (
                record.message_id != trigger_id
                and not self._is_tombstoned(session, record.message_id)
                and record.message_id not in session.refresh_ids
                and self._before_trigger(record, trigger_id, trigger_time)
            ):
                records_by_id[record.message_id] = record
        records_by_id.update(observed)

        # Ancestors can be old and absent from recent history; retain them even
        # when not in the page scan, but only after the same-scope checks above.
        for record in ancestor_direct_first:
            records_by_id[record.message_id] = record

        selected = self._select_records(records_by_id, ancestor_direct_first, budget)
        if not await still_allowed():
            return self._scope_context(session, missing_ids=missing, latest_id=trigger_id, valid=False)

        # Commit this snapshot to the bounded cache only after the epoch check;
        # a concurrent delete/edit therefore cannot be republished by this task.
        if not session.block_all_records:
            for record in observed.values():
                if record.message_id not in session.deleted_ids:
                    self._remember_record(session, record)
            for record in ancestor_direct_first:
                if record.message_id not in session.deleted_ids:
                    self._remember_record(session, record)
        session.last_access = time.monotonic()
        final_records = (
            self._select_records(
                {
                    record.message_id: record
                    for record in selected
                    if record.message_id not in session.deleted_ids
                },
                [
                    record
                    for record in ancestor_direct_first
                    if record.message_id not in session.deleted_ids
                ],
                budget,
            )
            if not session.block_all_records
            else ()
        )
        return self._scope_context(
            session,
            records=final_records,
            missing_ids=missing,
            latest_id=trigger_id,
            valid=True,
            reply_branch_ids=(record.message_id for record in ancestor_direct_first),
        )

    @staticmethod
    def _sort_key_for_message(message: Any) -> tuple[float, int]:
        message_id = ConversationManager._value_id(message, "id") or 0
        timestamp = ConversationManager._timestamp(message)
        return (timestamp if timestamp is not None else float(message_id), message_id)


@dataclass(frozen=True, slots=True)
class SimpleReference:
    message_id: int
    channel_id: int
    guild_id: int
    resolved: Any | None = None
