from __future__ import annotations

import aiosqlite
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, cast
from uuid import uuid4

from bot.utils.resource_library_markdown import markdown_diff


class StaleDocumentError(ValueError):
    def __init__(self, current_version: int):
        self.current_version = current_version
        super().__init__(f"文件已更新至 version {current_version}，請重新載入後再提交。")


class ResourceRepository:
    """SQLite persistence for published resource documents and review drafts."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self.db_path, timeout=10) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            yield db

    async def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS resource_documents (
                    slug TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER,
                    content_md TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS resource_drafts (
                    id TEXT PRIMARY KEY,
                    document_slug TEXT NOT NULL REFERENCES resource_documents(slug),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    base_version INTEGER NOT NULL,
                    before_md TEXT NOT NULL,
                    after_md TEXT NOT NULL,
                    diff TEXT NOT NULL,
                    status TEXT NOT NULL,
                    review_thread_id INTEGER,
                    review_message_id INTEGER,
                    decided_by TEXT,
                    decided_at TEXT,
                    decision_note TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_resource_drafts_status
                    ON resource_drafts(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_resource_drafts_thread
                    ON resource_drafts(review_thread_id);

                CREATE TABLE IF NOT EXISTS resource_workflow_settings (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    review_channel_id INTEGER NOT NULL,
                    notification_role_id INTEGER NOT NULL
                );
                """
            )
            await db.commit()

    async def configure(
        self,
        documents: list[dict[str, Any]],
        review_channel_id: int,
        notification_role_id: int,
    ) -> None:
        now = _utc_now()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            for document in documents:
                existing = await (
                    await db.execute(
                        "SELECT channel_id, message_id FROM resource_documents WHERE slug = ?",
                        (document["slug"],),
                    )
                ).fetchone()
                if existing and int(existing["channel_id"]) != int(document["channel_id"]):
                    if existing["message_id"] is not None:
                        raise ValueError(
                            f"{document['title']} 已有正式訊息，不能直接更換頻道。"
                        )
                    await db.execute(
                        "UPDATE resource_documents SET channel_id = ?, title = ? WHERE slug = ?",
                        (int(document["channel_id"]), document["title"], document["slug"]),
                    )
                elif existing:
                    await db.execute(
                        "UPDATE resource_documents SET title = ? WHERE slug = ?",
                        (document["title"], document["slug"]),
                    )
                else:
                    await db.execute(
                        """
                        INSERT INTO resource_documents
                            (slug, title, channel_id, message_id, content_md, version, updated_by, updated_at)
                        VALUES (?, ?, ?, NULL, ?, 1, 'system', ?)
                        """,
                        (
                            document["slug"],
                            document["title"],
                            int(document["channel_id"]),
                            f"# {document['title']}\n\n",
                            now,
                        ),
                    )
            await db.execute(
                """
                INSERT INTO resource_workflow_settings(singleton, review_channel_id, notification_role_id)
                VALUES (1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    review_channel_id = excluded.review_channel_id,
                    notification_role_id = excluded.notification_role_id
                """,
                (int(review_channel_id), int(notification_role_id)),
            )
            await db.commit()

    async def get_workflow_settings(self) -> dict[str, int] | None:
        async with self._connect() as db:
            row = await (
                await db.execute(
                    "SELECT review_channel_id, notification_role_id "
                    "FROM resource_workflow_settings WHERE singleton = 1"
                )
            ).fetchone()
        if row is None:
            return None
        return {
            "review_channel_id": int(row["review_channel_id"]),
            "notification_role_id": int(row["notification_role_id"]),
        }

    async def list_documents(self) -> list[dict[str, Any]]:
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT slug, title, channel_id, message_id, content_md, version, updated_by, updated_at "
                    "FROM resource_documents ORDER BY rowid"
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def get_document(self, slug: str) -> dict[str, Any] | None:
        async with self._connect() as db:
            row = await (
                await db.execute(
                    "SELECT slug, title, channel_id, message_id, content_md, version, updated_by, updated_at "
                    "FROM resource_documents WHERE slug = ?",
                    (slug,),
                )
            ).fetchone()
        return dict(row) if row else None

    async def set_message_id(self, slug: str, message_id: int) -> None:
        async with self._connect() as db:
            await db.execute(
                "UPDATE resource_documents SET message_id = ? WHERE slug = ?",
                (int(message_id), slug),
            )
            await db.commit()

    async def migrate_document_messages(
        self, migrations: list[dict[str, Any]]
    ) -> None:
        if not migrations:
            raise ValueError("至少需要一份文件遷移設定。")
        target_channels = [int(item["channel_id"]) for item in migrations]
        target_messages = [int(item["message_id"]) for item in migrations]
        if len(set(target_channels)) != len(target_channels):
            raise ValueError("資源文件遷移目標頻道必須彼此不同。")
        if len(set(target_messages)) != len(target_messages):
            raise ValueError("資源文件遷移目標訊息 ID 必須彼此不同。")

        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                for migration in migrations:
                    row = await (
                        await db.execute(
                            "SELECT channel_id, message_id, version FROM resource_documents WHERE slug = ?",
                            (migration["slug"],),
                        )
                    ).fetchone()
                    if row is None:
                        raise KeyError(migration["slug"])
                    current_message_id = (
                        int(row["message_id"]) if row["message_id"] is not None else None
                    )
                    expected_message_id = migration["expected_message_id"]
                    if expected_message_id is not None:
                        expected_message_id = int(expected_message_id)
                    if (
                        int(row["channel_id"]) != int(migration["expected_channel_id"])
                        or current_message_id != expected_message_id
                        or int(row["version"]) != int(migration["expected_version"])
                    ):
                        raise ValueError(
                            f"{migration['slug']} 的頻道或訊息已變更，取消遷移以避免覆蓋新資料。"
                        )
                    await db.execute(
                        "UPDATE resource_documents SET channel_id = ?, message_id = ? WHERE slug = ?",
                        (
                            int(migration["channel_id"]),
                            int(migration["message_id"]),
                            migration["slug"],
                        ),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def create_draft(
        self,
        slug: str,
        created_by: int | str,
        base_version: int,
        after_md: str,
    ) -> dict[str, Any]:
        draft_id = str(uuid4())
        created_at = _utc_now()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            document = await (
                await db.execute(
                    "SELECT content_md, version FROM resource_documents WHERE slug = ?",
                    (slug,),
                )
            ).fetchone()
            if document is None:
                raise KeyError(slug)
            current_version = int(document["version"])
            if current_version != int(base_version):
                raise StaleDocumentError(current_version)
            before_md = str(document["content_md"])
            if before_md == after_md:
                raise ValueError("內容沒有變更，無需提交審核。")
            diff = markdown_diff(before_md, after_md)
            await db.execute(
                """
                INSERT INTO resource_drafts
                    (id, document_slug, created_by, created_at, base_version, before_md, after_md, diff, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'creating_review')
                """,
                (
                    draft_id,
                    slug,
                    str(created_by),
                    created_at,
                    current_version,
                    before_md,
                    after_md,
                    diff,
                ),
            )
            await db.commit()
        return cast(dict[str, Any], await self.get_draft(draft_id))

    async def set_review_thread(
        self,
        draft_id: str,
        thread_id: int,
        message_id: int,
    ) -> None:
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE resource_drafts
                SET review_thread_id = ?, review_message_id = ?, status = 'pending'
                WHERE id = ? AND status = 'creating_review'
                """,
                (int(thread_id), int(message_id), draft_id),
            )
            await db.commit()

    async def mark_review_failed(self, draft_id: str, note: str) -> None:
        async with self._connect() as db:
            await db.execute(
                "UPDATE resource_drafts SET status = 'review_failed', decision_note = ? "
                "WHERE id = ? AND status = 'creating_review'",
                (note[:1000], draft_id),
            )
            await db.commit()

    async def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        async with self._connect() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM resource_drafts WHERE id = ?",
                    (draft_id,),
                )
            ).fetchone()
        return dict(row) if row else None

    async def get_draft_by_thread(self, thread_id: int) -> dict[str, Any] | None:
        async with self._connect() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM resource_drafts WHERE review_thread_id = ?",
                    (int(thread_id),),
                )
            ).fetchone()
        return dict(row) if row else None

    async def decide_draft(
        self,
        draft_id: str,
        reviewer_id: int | str,
        approve: bool,
        decision_note: str = "",
    ) -> dict[str, Any]:
        decided_at = _utc_now()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    """
                    SELECT d.*, r.version AS current_version, r.content_md AS current_md,
                           r.title AS document_title, r.channel_id AS document_channel_id,
                           r.message_id AS document_message_id
                    FROM resource_drafts d
                    JOIN resource_documents r ON r.slug = d.document_slug
                    WHERE d.id = ?
                    """,
                    (draft_id,),
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                raise KeyError(draft_id)
            if row["status"] != "pending":
                await db.rollback()
                return {"status": row["status"], "draft": dict(row)}

            if approve and int(row["base_version"]) != int(row["current_version"]):
                await db.execute(
                    """
                    UPDATE resource_drafts
                    SET status = 'stale', decided_by = ?, decided_at = ?,
                        decision_note = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (
                        str(reviewer_id),
                        decided_at,
                        f"目前正式版本為 {row['current_version']}，草稿基於 {row['base_version']}。",
                        draft_id,
                    ),
                )
                await db.commit()
                return {
                    "status": "stale",
                    "current_version": int(row["current_version"]),
                    "draft": dict(row),
                }

            if approve:
                next_version = int(row["current_version"]) + 1
                await db.execute(
                    """
                    UPDATE resource_documents
                    SET content_md = ?, version = ?, updated_by = ?, updated_at = ?
                    WHERE slug = ? AND version = ?
                    """,
                    (
                        row["after_md"],
                        next_version,
                        str(reviewer_id),
                        decided_at,
                        row["document_slug"],
                        int(row["base_version"]),
                    ),
                )
                status = "approved"
            else:
                next_version = int(row["current_version"])
                status = "rejected"

            await db.execute(
                """
                UPDATE resource_drafts
                SET status = ?, decided_by = ?, decided_at = ?, decision_note = ?
                WHERE id = ? AND status = 'pending'
                """,
                (status, str(reviewer_id), decided_at, decision_note[:1000], draft_id),
            )
            await db.commit()

        document = await self.get_document(str(row["document_slug"]))
        return {"status": status, "draft": await self.get_draft(draft_id), "document": document}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
