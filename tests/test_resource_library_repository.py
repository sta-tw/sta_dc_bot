import asyncio
import sqlite3

import pytest

from database.resource_library import ResourceRepository, StaleDocumentError


DOCUMENTS = [
    {"slug": "events", "title": "活動資訊分享", "channel_id": 1001},
    {"slug": "communities", "title": "資訊社群分享", "channel_id": 1002},
    {"slug": "learning", "title": "學習、比賽資源分享", "channel_id": 1003},
    {"slug": "experiences", "title": "特選心得彙整", "channel_id": 1004},
    {"slug": "portfolios", "title": "公開備審資料彙整", "channel_id": 1005},
    {"slug": "tools", "title": "做備審的好工具", "channel_id": 1006},
]


@pytest.mark.asyncio
async def test_configure_initializes_six_documents_and_is_idempotent(tmp_path):
    repo = ResourceRepository(tmp_path / "guild.db")
    await repo.initialize()
    await repo.configure(DOCUMENTS, review_channel_id=2001, notification_role_id=3001)
    await repo.configure(DOCUMENTS, review_channel_id=2001, notification_role_id=3001)

    documents = await repo.list_documents()
    assert [row["slug"] for row in documents] == [doc["slug"] for doc in DOCUMENTS]
    assert documents[0]["content_md"] == "# 活動資訊分享\n\n"
    assert documents[0]["version"] == 1
    assert set(documents[0]) >= {
        "channel_id",
        "message_id",
        "content_md",
        "version",
        "updated_by",
        "updated_at",
    }
    assert await repo.get_workflow_settings() == {
        "review_channel_id": 2001,
        "notification_role_id": 3001,
    }


@pytest.mark.asyncio
async def test_migrate_document_messages_updates_all_pointers_without_changing_content(tmp_path):
    repo = ResourceRepository(tmp_path / "guild.db")
    await repo.initialize()
    await repo.configure(DOCUMENTS, review_channel_id=2001, notification_role_id=3001)
    await repo.set_message_id("events", 8001)
    await repo.set_message_id("communities", 8002)
    original = await repo.list_documents()

    await repo.migrate_document_messages(
        [
            {
                "slug": document["slug"],
                "expected_channel_id": document["channel_id"],
                "expected_message_id": document["message_id"],
                "expected_version": document["version"],
                "channel_id": 9001 + index,
                "message_id": 9101 + index,
            }
            for index, document in enumerate(original)
        ]
    )

    migrated = await repo.list_documents()
    assert [document["channel_id"] for document in migrated] == [
        9001 + index for index in range(len(DOCUMENTS))
    ]
    assert [document["message_id"] for document in migrated] == [
        9101 + index for index in range(len(DOCUMENTS))
    ]
    for before, after in zip(original, migrated):
        assert after["content_md"] == before["content_md"]
        assert after["version"] == before["version"]
        assert after["updated_by"] == before["updated_by"]
        assert after["updated_at"] == before["updated_at"]


@pytest.mark.asyncio
async def test_migrate_document_messages_rolls_back_on_stale_source_pointer(tmp_path):
    repo = ResourceRepository(tmp_path / "guild.db")
    await repo.initialize()
    await repo.configure(DOCUMENTS, review_channel_id=2001, notification_role_id=3001)
    await repo.set_message_id("events", 8001)

    with pytest.raises(ValueError, match="頻道或訊息已變更"):
        await repo.migrate_document_messages(
            [
                {
                    "slug": "events",
                    "expected_channel_id": 1001,
                    "expected_message_id": 8001,
                    "expected_version": 1,
                    "channel_id": 9001,
                    "message_id": 9101,
                },
                {
                    "slug": "communities",
                    "expected_channel_id": 1003,
                    "expected_message_id": None,
                    "expected_version": 1,
                    "channel_id": 9002,
                    "message_id": 9102,
                },
            ]
        )

    documents = await repo.list_documents()
    assert documents[0]["channel_id"] == 1001
    assert documents[0]["message_id"] == 8001
    assert documents[1]["channel_id"] == 1002
    assert documents[1]["message_id"] is None


@pytest.mark.asyncio
async def test_draft_is_not_published_until_approved_and_keeps_snapshot(tmp_path):
    repo = ResourceRepository(tmp_path / "guild.db")
    await repo.initialize()
    await repo.configure(DOCUMENTS, 2001, 3001)
    document = await repo.get_document("events")
    new_markdown = "# 活動資訊分享\n\n- [活動](https://example.org/)\n"

    draft = await repo.create_draft("events", 77, document["version"], new_markdown)
    assert draft["status"] == "creating_review"
    assert draft["base_version"] == 1
    assert draft["before_md"] == document["content_md"]
    assert draft["after_md"] == new_markdown
    assert (await repo.get_document("events"))["content_md"] == document["content_md"]

    await repo.set_review_thread(draft["id"], 4001, 4002)
    result = await repo.decide_draft(draft["id"], reviewer_id=88, approve=True)

    assert result["status"] == "approved"
    assert result["document"]["content_md"] == new_markdown
    assert result["document"]["version"] == 2
    assert result["document"]["updated_by"] == "88"
    assert result["draft"]["review_thread_id"] == 4001
    assert result["draft"]["review_message_id"] == 4002


@pytest.mark.asyncio
async def test_stale_draft_is_rejected_without_overwriting_newer_version(tmp_path):
    repo = ResourceRepository(tmp_path / "guild.db")
    await repo.initialize()
    await repo.configure(DOCUMENTS, 2001, 3001)
    first = await repo.create_draft("events", 1, 1, "# 活動資訊分享\n\n- [A](https://a.example/)\n")
    second = await repo.create_draft("events", 2, 1, "# 活動資訊分享\n\n- [B](https://b.example/)\n")
    await repo.set_review_thread(first["id"], 4001, 4002)
    await repo.set_review_thread(second["id"], 4003, 4004)

    approved = await repo.decide_draft(first["id"], reviewer_id=10, approve=True)
    stale = await repo.decide_draft(second["id"], reviewer_id=11, approve=True)

    assert approved["document"]["version"] == 2
    assert stale["status"] == "stale"
    assert stale["current_version"] == 2
    current = await repo.get_document("events")
    assert "[A]" in current["content_md"]
    assert "[B]" not in current["content_md"]
    assert current["version"] == 2


@pytest.mark.asyncio
async def test_stale_base_version_and_rejection_do_not_change_published_data(tmp_path):
    repo = ResourceRepository(tmp_path / "guild.db")
    await repo.initialize()
    await repo.configure(DOCUMENTS, 2001, 3001)

    with pytest.raises(StaleDocumentError) as error:
        await repo.create_draft("events", 1, 2, "# old\n")
    assert error.value.current_version == 1

    draft = await repo.create_draft("events", 1, 1, "# 新標題\n\n")
    await repo.set_review_thread(draft["id"], 4001, 4002)
    result = await repo.decide_draft(draft["id"], reviewer_id=10, approve=False)

    assert result["status"] == "rejected"
    document = await repo.get_document("events")
    assert document["version"] == 1
    assert document["content_md"] == "# 活動資訊分享\n\n"


@pytest.mark.asyncio
async def test_duplicate_approvals_only_increment_version_once(tmp_path):
    repo = ResourceRepository(tmp_path / "guild.db")
    await repo.initialize()
    await repo.configure(DOCUMENTS, 2001, 3001)
    draft = await repo.create_draft("events", 1, 1, "# 活動資訊分享\n\n- [A](https://a.example/)\n")
    await repo.set_review_thread(draft["id"], 4001, 4002)

    results = await asyncio.gather(
        repo.decide_draft(draft["id"], reviewer_id=10, approve=True),
        repo.decide_draft(draft["id"], reviewer_id=11, approve=True),
    )

    assert all(result["status"] == "approved" for result in results)
    assert (await repo.get_document("events"))["version"] == 2


@pytest.mark.asyncio
async def test_documents_are_isolated_between_guild_databases(tmp_path):
    first = ResourceRepository(tmp_path / "guild-1.db")
    second = ResourceRepository(tmp_path / "guild-2.db")
    await first.initialize()
    await second.initialize()
    await first.configure(DOCUMENTS, 2001, 3001)

    assert await first.get_document("events") is not None
    assert await second.get_document("events") is None


@pytest.mark.asyncio
async def test_legacy_schema_upgrade_backfills_idempotently_and_survives_reopen(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            CREATE TABLE resource_documents (
                slug TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                content_md TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                updated_by TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.executemany(
            """
            INSERT INTO resource_documents
                (slug, title, channel_id, message_id, content_md, version, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("events", "Events", 1001, 8101, "# Events\n", 4, "editor", "2026-01-01"),
                ("communities", "Communities", 1002, 8201, "# Communities\n", 2, "editor", "2026-01-02"),
            ],
        )

    repo = ResourceRepository(db_path)
    await repo.initialize()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO resource_document_messages (slug, position, message_id) VALUES (?, ?, ?)",
            ("events", 1, 8102),
        )

    await repo.initialize()
    reopened = ResourceRepository(db_path)
    await reopened.initialize()

    events = await reopened.get_document("events")
    communities = await reopened.get_document("communities")
    assert events["message_ids"] == [8101, 8102]
    assert events["message_id"] == 8101
    assert communities["message_ids"] == [8201]
    assert communities["message_id"] == 8201


@pytest.mark.asyncio
async def test_ordered_message_ids_grow_shrink_and_keep_first_compatibility(tmp_path):
    repo = ResourceRepository(tmp_path / "messages.db")
    await repo.initialize()
    await repo.configure(DOCUMENTS, 2001, 3001)

    steps = [
        ([], [8301]),
        ([8301], [8301, 8302]),
        ([8301, 8302], [8301, 8302, 8303]),
        ([8301, 8302, 8303], [8301, 8303]),
        ([8301, 8303], [8301]),
        ([8301], []),
    ]
    for expected_ids, new_ids in steps:
        await repo.set_message_ids(
            "events",
            new_ids,
            expected_channel_id=1001,
            expected_version=1,
            expected_message_ids=expected_ids,
        )
        document = await repo.get_document("events")
        assert document["message_ids"] == new_ids
        assert document["message_id"] == (new_ids[0] if new_ids else None)

    listed = next(document for document in await repo.list_documents() if document["slug"] == "events")
    assert listed["message_ids"] == []
    assert listed["message_id"] is None


@pytest.mark.asyncio
async def test_set_message_ids_rejects_stale_channel_version_and_ids_atomically(tmp_path):
    repo = ResourceRepository(tmp_path / "stale.db")
    await repo.initialize()
    await repo.configure(DOCUMENTS, 2001, 3001)
    await repo.set_message_ids(
        "events",
        [8401, 8402],
        expected_channel_id=1001,
        expected_version=1,
        expected_message_ids=[],
    )

    stale_calls = [
        {"expected_channel_id": 9999, "expected_version": 1, "expected_message_ids": [8401, 8402]},
        {"expected_channel_id": 1001, "expected_version": 2, "expected_message_ids": [8401, 8402]},
        {"expected_channel_id": 1001, "expected_version": 1, "expected_message_ids": [8402, 8401]},
    ]
    for expected in stale_calls:
        with pytest.raises(ValueError):
            await repo.set_message_ids("events", [8501], **expected)
        assert (await repo.get_document("events"))["message_ids"] == [8401, 8402]
        assert (await repo.get_document("events"))["message_id"] == 8401


@pytest.mark.parametrize(
    "message_ids",
    [None, (8501,), [0], [-1], [True], [8501, 8501]],
)
@pytest.mark.asyncio
async def test_set_message_ids_rejects_invalid_values_without_writing(tmp_path, message_ids):
    repo = ResourceRepository(tmp_path / "invalid.db")
    await repo.initialize()
    await repo.configure(DOCUMENTS, 2001, 3001)

    with pytest.raises(ValueError):
        await repo.set_message_ids(
            "events",
            message_ids,
            expected_channel_id=1001,
            expected_version=1,
            expected_message_ids=[],
        )

    document = await repo.get_document("events")
    assert document["message_ids"] == []
    assert document["message_id"] is None


@pytest.mark.asyncio
async def test_set_message_ids_collision_rolls_back_all_message_pointers(tmp_path):
    repo = ResourceRepository(tmp_path / "collision.db")
    await repo.initialize()
    await repo.configure(DOCUMENTS, 2001, 3001)
    await repo.set_message_ids(
        "events",
        [8601],
        expected_channel_id=1001,
        expected_version=1,
        expected_message_ids=[],
    )
    await repo.set_message_ids(
        "communities",
        [8602],
        expected_channel_id=1002,
        expected_version=1,
        expected_message_ids=[],
    )

    with pytest.raises(sqlite3.IntegrityError):
        await repo.set_message_ids(
            "events",
            [8603, 8602],
            expected_channel_id=1001,
            expected_version=1,
            expected_message_ids=[8601],
        )

    events = await repo.get_document("events")
    communities = await repo.get_document("communities")
    assert events["message_ids"] == [8601]
    assert events["message_id"] == 8601
    assert communities["message_ids"] == [8602]


@pytest.mark.asyncio
async def test_set_message_id_preserves_multipart_suffix_and_rejects_other_first_id(tmp_path):
    repo = ResourceRepository(tmp_path / "legacy-set.db")
    await repo.initialize()
    await repo.configure(DOCUMENTS, 2001, 3001)
    await repo.set_message_ids(
        "events",
        [8701, 8702],
        expected_channel_id=1001,
        expected_version=1,
        expected_message_ids=[],
    )

    await repo.set_message_id("events", 8701)
    with pytest.raises(ValueError):
        await repo.set_message_id("events", 8703)
    assert (await repo.get_document("events"))["message_ids"] == [8701, 8702]

    await repo.set_message_id("communities", 8711)
    await repo.set_message_id("communities", 8712)
    communities = await repo.get_document("communities")
    assert communities["message_ids"] == [8712]
    assert communities["message_id"] == 8712


@pytest.mark.asyncio
async def test_migration_refuses_implicit_multipart_loss_and_accepts_full_lists(tmp_path):
    repo = ResourceRepository(tmp_path / "multi-migration.db")
    await repo.initialize()
    await repo.configure(DOCUMENTS, 2001, 3001)
    await repo.set_message_ids(
        "events",
        [8801, 8802],
        expected_channel_id=1001,
        expected_version=1,
        expected_message_ids=[],
    )
    before = await repo.get_document("events")

    with pytest.raises(ValueError, match="完整訊息 ID 清單"):
        await repo.migrate_document_messages(
            [
                {
                    "slug": "events",
                    "expected_channel_id": 1001,
                    "expected_message_id": 8801,
                    "expected_version": 1,
                    "channel_id": 9001,
                    "message_id": 8901,
                }
            ]
        )
    assert await repo.get_document("events") == before

    await repo.migrate_document_messages(
        [
            {
                "slug": "events",
                "expected_channel_id": 1001,
                "expected_message_ids": [8801, 8802],
                "expected_version": 1,
                "channel_id": 9001,
                "message_ids": [8901, 8902],
            }
        ]
    )
    migrated = await repo.get_document("events")
    assert migrated["channel_id"] == 9001
    assert migrated["message_ids"] == [8901, 8902]
    assert migrated["message_id"] == 8901
    assert migrated["content_md"] == before["content_md"]
    assert migrated["version"] == before["version"]
    assert migrated["updated_by"] == before["updated_by"]
    assert migrated["updated_at"] == before["updated_at"]


@pytest.mark.asyncio
async def test_configure_blocks_channel_change_for_any_published_message_and_rolls_back(tmp_path):
    repo = ResourceRepository(tmp_path / "configure-guard.db")
    await repo.initialize()
    await repo.configure(DOCUMENTS, 2001, 3001)
    await repo.set_message_ids(
        "events",
        [9001, 9002],
        expected_channel_id=1001,
        expected_version=1,
        expected_message_ids=[],
    )

    with pytest.raises(ValueError, match="不能直接更換頻道"):
        await repo.configure(
            [
                {"slug": "communities", "title": "Updated Communities", "channel_id": 9102},
                {"slug": "events", "title": "Updated Events", "channel_id": 9101},
            ],
            review_channel_id=2101,
            notification_role_id=3101,
        )

    communities = await repo.get_document("communities")
    events = await repo.get_document("events")
    assert communities["channel_id"] == 1002
    assert communities["title"] == "資訊社群分享"
    assert events["channel_id"] == 1001
    assert events["message_ids"] == [9001, 9002]
    assert await repo.get_workflow_settings() == {
        "review_channel_id": 2001,
        "notification_role_id": 3001,
    }


@pytest.mark.asyncio
async def test_setting_message_ids_preserves_document_metadata_and_drafts(tmp_path):
    repo = ResourceRepository(tmp_path / "preservation.db")
    await repo.initialize()
    await repo.configure(DOCUMENTS, 2001, 3001)
    before = await repo.get_document("events")
    draft = await repo.create_draft(
        "events", 77, before["version"], "# 活動資訊分享\n\n- [活動](https://example.org/)\n"
    )

    await repo.set_message_ids(
        "events",
        [9201, 9202],
        expected_channel_id=before["channel_id"],
        expected_version=before["version"],
        expected_message_ids=[],
    )

    after = await repo.get_document("events")
    assert after["message_ids"] == [9201, 9202]
    for key in ("channel_id", "content_md", "version", "updated_by", "updated_at"):
        assert after[key] == before[key]
    assert await repo.get_draft(draft["id"]) == draft


@pytest.mark.asyncio
async def test_message_ids_are_isolated_between_guild_databases(tmp_path):
    first = ResourceRepository(tmp_path / "messages-guild-1.db")
    second = ResourceRepository(tmp_path / "messages-guild-2.db")
    await first.initialize()
    await second.initialize()
    await first.configure(DOCUMENTS, 2001, 3001)
    await second.configure(DOCUMENTS, 2002, 3002)
    await first.set_message_ids(
        "events",
        [9301, 9302],
        expected_channel_id=1001,
        expected_version=1,
        expected_message_ids=[],
    )

    assert (await first.get_document("events"))["message_ids"] == [9301, 9302]
    assert (await second.get_document("events"))["message_ids"] == []
