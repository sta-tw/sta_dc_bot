import asyncio
from contextlib import asynccontextmanager

import pytest

from database.resource_library import ResourceRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("listing", [False, True], ids=["get", "list"])
async def test_document_and_ordered_message_ids_share_one_read_snapshot(listing, tmp_path, monkeypatch):
    reader = ResourceRepository(tmp_path / "resources.db")
    await reader.initialize()
    await reader.configure(
        [{"slug": "tools", "title": "工具", "channel_id": 7001}], 8100, 8200,
    )
    await reader.set_message_id("tools", 8001)
    writer = ResourceRepository(reader.db_path)
    between_queries = asyncio.Event()
    changed = asyncio.Event()
    original_connect = reader._connect

    @asynccontextmanager
    async def gated_connect():
        async with original_connect() as db:
            original_execute = db.execute

            async def execute(sql, *args, **kwargs):
                if "FROM resource_document_messages" in sql:
                    between_queries.set()
                    await changed.wait()
                return await original_execute(sql, *args, **kwargs)

            db.execute = execute
            yield db

    monkeypatch.setattr(reader, "_connect", gated_connect)
    read = asyncio.create_task(reader.list_documents() if listing else reader.get_document("tools"))
    await between_queries.wait()
    async with writer._connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "UPDATE resource_documents SET version = 2, content_md = ?, message_id = 8002 WHERE slug = 'tools'",
            ("# 新版工具\n",),
        )
        await db.execute("DELETE FROM resource_document_messages WHERE slug = 'tools'")
        await db.executemany(
            "INSERT INTO resource_document_messages(slug, position, message_id) VALUES ('tools', ?, ?)",
            [(0, 8002), (1, 8003)],
        )
        await db.commit()
    changed.set()
    result = await read
    document = result[0] if listing else result

    assert document["version"] == 1
    assert document["content_md"] == "# 工具\n\n"
    assert document["message_ids"] == [8001]
    assert document["message_id"] == 8001
    latest = await writer.get_document("tools")
    assert latest["version"] == 2
    assert latest["message_ids"] == [8002, 8003]
