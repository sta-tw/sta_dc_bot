import asyncio

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
