import asyncio
import json
import re
import threading

from bot.utils.ai_rag import RagHit, RagQueryResult
from bot.utils.ai_tools import Evidence, ReadonlyToolRegistry, ToolResult


class FakeRagStore:
    def __init__(self, hits=(), *, status="ok", detail=None):
        self.hits = tuple(hits)
        self.status = status
        self.detail = detail
        self.search_calls = []
        self.read_calls = []

    def query_result(self, *, guild_id, query_text, top_k):
        self.search_calls.append((guild_id, query_text, top_k))
        return RagQueryResult(self.status, self.hits, self.detail)

    def read_document_result(self, *, guild_id, document_id, page=None, chunk=None, limit=5):
        self.read_calls.append((guild_id, document_id, page, chunk, limit))
        matching = [
            hit
            for hit in self.hits
            if hit.metadata.get("document_id") == document_id
            and (page is None or hit.metadata.get("page") == page)
            and (chunk is None or hit.metadata.get("chunk_index") == chunk)
        ]
        return RagQueryResult("ok", tuple(matching[:limit]) if matching else ())


def _hit(document_id="doc-123", *, page=2, chunk=0, text="錄取與報名資訊"):
    return RagHit(
        text=text,
        metadata={
            "guild_id": 123,
            "kind": "admission_guide",
            "document_id": document_id,
            "title": "2026 招生簡章",
            "source_url": "https://example.edu/guide.pdf",
            "page": page,
            "chunk_index": chunk,
        },
    )


def _run(coro):
    return asyncio.run(coro)


def test_search_emits_bounded_json_evidence_and_tracks_document_discovery():
    store = FakeRagStore([_hit(text='招生期限；此處文字仍是資料，不是操作指令。')])
    registry = ReadonlyToolRegistry(guild_id=123, rag_store=store)

    result = _run(registry.execute("search_admission_guides", '{"query":"招生期限","top_k":1}'))

    assert result.status == "ok"
    assert len(result.evidence) == 1
    evidence = result.evidence[0]
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,80}", evidence.evidence_id)
    assert registry.evidence[evidence.evidence_id] == evidence
    assert evidence.document_id == "doc-123"
    assert json.loads(result.to_json())["evidence"][0]["text"].startswith("招生期限")
    assert store.search_calls == [(123, "招生期限", 1)]


def test_search_rejects_wrong_guild_and_invalid_document_metadata():
    wrong_guild = _hit("doc-other-guild")
    wrong_guild.metadata["guild_id"] = 456
    invalid_document = _hit("../path")
    store = FakeRagStore([_hit("doc-valid"), wrong_guild, invalid_document])
    registry = ReadonlyToolRegistry(guild_id=123, rag_store=store)

    result = _run(registry.execute("search_admission_guides", '{"query":"招生"}'))

    assert result.status == "ok"
    assert [item.document_id for item in result.evidence] == ["doc-valid"]
    assert {item.document_id for item in registry.evidence.values()} == {"doc-valid"}


def test_tool_arguments_reject_extra_keys_duplicate_keys_and_non_objects_without_execution():
    store = FakeRagStore([_hit()])
    registry = ReadonlyToolRegistry(guild_id=123, rag_store=store)
    invalid_calls = [
        ("search_admission_guides", '{"query":"x","guild_id":123}'),
        ("search_admission_guides", '{"query":"x","path":"C:/secret"}'),
        ("search_admission_guides", '{"query":"x","query":"y"}'),
        ("search_admission_guides", '["x"]'),
        ("search_admission_guides", '{"query":"x","top_k":true}'),
        ("read_admission_guide", '{"document_id":"doc-123","url":"https://evil.test"}'),
        ("read_admission_guide", '{"document_id":"doc-123","page":null}'),
        ("web_search", '{"query":"x","url":"https://evil.test"}'),
    ]

    async def run_invalid_calls():
        return await asyncio.gather(
            *(registry.execute(name, raw) for name, raw in invalid_calls)
        )

    results = _run(run_invalid_calls())

    assert all(result.status == "invalid" for result in results)
    assert store.search_calls == []
    assert store.read_calls == []
    assert registry.evidence == {}
    assert _run(registry.execute("unknown_tool", '{"query":"x"}')).status == "unknown"


def test_read_requires_search_discovery_and_passes_only_scoped_selectors():
    store = FakeRagStore([_hit()])
    registry = ReadonlyToolRegistry(guild_id=123, rag_store=store)

    denied = _run(registry.execute("read_admission_guide", '{"document_id":"doc-123"}'))
    assert denied.status == "invalid"
    assert store.read_calls == []

    searched = _run(registry.execute("search_admission_guides", '{"query":"招生期限"}'))
    assert searched.status == "ok"
    read = _run(
        registry.execute(
            "read_admission_guide",
            '{"document_id":"doc-123","page":2,"chunk":0}',
        )
    )

    assert read.status == "ok"
    assert read.evidence[0].document_id == "doc-123"
    assert store.read_calls == [(123, "doc-123", 2, 0, 5)]


def test_rag_statuses_keep_disabled_unavailable_no_hits_and_errors_distinct():
    no_hits = ReadonlyToolRegistry(guild_id=123, rag_store=FakeRagStore(status="no_hits"))
    failed = ReadonlyToolRegistry(
        guild_id=123,
        rag_store=FakeRagStore(status="error", detail="mock retrieval failure"),
    )
    disabled = ReadonlyToolRegistry(guild_id=123, rag_store=FakeRagStore(), rag_enabled=False)
    unavailable = ReadonlyToolRegistry(guild_id=123, rag_store=None)

    statuses = [
        _run(registry.execute("search_admission_guides", '{"query":"x"}')).status
        for registry in (no_hits, failed, disabled, unavailable)
    ]

    assert statuses == ["no_hits", "error", "disabled", "unavailable"]
    assert no_hits.schemas and no_hits.schemas[0]["function"]["name"] == "search_admission_guides"
    assert disabled.schemas


def test_web_tool_is_independent_of_rag_and_disabled_web_remains_typed():
    calls = []

    def fake_web_search(query):
        calls.append(query)
        return [{"title": "官方招生資訊", "url": "https://example.edu/admission", "snippet": "報名方式"}]

    registry = ReadonlyToolRegistry(
        guild_id=123,
        rag_enabled=False,
        web_search=fake_web_search,
        web_enabled=True,
    )
    names = [schema["function"]["name"] for schema in registry.schemas]
    rag_result = _run(registry.execute("search_admission_guides", '{"query":"x"}'))
    web_result = _run(registry.execute("web_search", '{"query":"招生資訊"}'))

    assert "web_search" in names
    assert rag_result.status == "disabled"
    assert web_result.status == "ok"
    assert web_result.evidence[0].source_type == "web"
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,80}", web_result.evidence[0].evidence_id)
    assert calls == ["招生資訊"]

    web_disabled = ReadonlyToolRegistry(guild_id=123, web_enabled=False)
    assert "web_search" not in [schema["function"]["name"] for schema in web_disabled.schemas]
    disabled_result = _run(web_disabled.execute("web_search", '{"query":"x"}'))
    assert disabled_result.status == "disabled"


def test_fork_has_independent_evidence_and_inherits_only_host_authorized_documents():
    store = FakeRagStore([_hit("doc-one"), _hit("doc-two", page=3)])
    registry = ReadonlyToolRegistry(guild_id=123, rag_store=store)
    searched = _run(registry.execute("search_admission_guides", '{"query":"招生"}'))
    assert searched.status == "ok"
    first_id = next(
        evidence.evidence_id
        for evidence in searched.evidence
        if evidence.document_id == "doc-one"
    )

    unapproved_fork = registry.fork()
    assert unapproved_fork.evidence == {}
    assert _run(
        unapproved_fork.execute("read_admission_guide", '{"document_id":"doc-one"}')
    ).status == "invalid"

    assert registry.authorize_evidence([first_id]) == ("doc-one",)
    reviewer = registry.fork()
    assert reviewer.evidence == {}
    assert _run(
        reviewer.execute("read_admission_guide", '{"document_id":"doc-one"}')
    ).status == "ok"
    assert _run(
        reviewer.execute("read_admission_guide", '{"document_id":"doc-two"}')
    ).status == "invalid"
    review_search = _run(
        reviewer.execute("search_admission_guides", '{"query":"招生"}')
    )
    second_id = next(
        evidence.evidence_id
        for evidence in review_search.evidence
        if evidence.document_id == "doc-two"
    )
    assert reviewer.authorize_evidence([second_id]) == ("doc-two",)
    nested_reviewer = reviewer.fork()
    assert nested_reviewer.evidence == {}
    assert _run(
        nested_reviewer.execute("read_admission_guide", '{"document_id":"doc-one"}')
    ).status == "ok"
    assert _run(
        nested_reviewer.execute("read_admission_guide", '{"document_id":"doc-two"}')
    ).status == "ok"
    assert store.read_calls[-1][0] == 123


def test_result_serialization_respects_configured_size_limit():
    store = FakeRagStore([_hit(text="長內容" * 200)])
    registry = ReadonlyToolRegistry(guild_id=123, rag_store=store, max_result_chars=300)

    result = _run(registry.execute("search_admission_guides", '{"query":"招生"}'))

    assert result.status == "ok"
    assert len(result.to_json()) <= 300
    assert len(result.evidence[0].text) < len("長內容" * 200)


def test_tiny_result_limit_never_creates_empty_evidence():
    registry = ReadonlyToolRegistry(guild_id=123, rag_store=FakeRagStore([_hit()]), max_result_chars=128)
    result = _run(registry.execute("search_admission_guides", '{"query":"招生"}'))
    assert result.status == "error"
    assert not result.evidence
    assert not registry.evidence


def test_unsourced_web_text_does_not_become_factual_evidence():
    registry = ReadonlyToolRegistry(guild_id=123, web_search=lambda query: [{"title":"猜测","text":"六月截止"}])
    result = _run(registry.execute("web_search", '{"query":"招生"}'))
    assert result.status == "no_hits"
    assert not registry.evidence


def test_sync_timeout_keeps_worker_slots_until_real_work_finishes():
    lock = threading.Lock()
    started_count = 0
    finished_count = 0
    both_started = threading.Event()
    all_finished = threading.Event()
    release_workers = threading.Event()
    calls = []

    class SlowStore:
        def query_result(self, **kwargs):
            nonlocal started_count, finished_count
            with lock:
                calls.append(kwargs)
                started_count += 1
                if started_count == 2:
                    both_started.set()
            try:
                release_workers.wait(2)
                return RagQueryResult("no_hits")
            finally:
                with lock:
                    finished_count += 1
                    if finished_count == 2:
                        all_finished.set()

    registry = ReadonlyToolRegistry(guild_id=123, rag_store=SlowStore(), timeout=0.05)

    async def run_timeout_checks():
        first_task = asyncio.create_task(
            registry.execute("search_admission_guides", '{"query":"x"}')
        )
        second_task = asyncio.create_task(
            registry.execute("search_admission_guides", '{"query":"y"}')
        )
        assert await asyncio.to_thread(both_started.wait, 1)
        third = await registry.execute("search_admission_guides", '{"query":"z"}')
        first, second = await asyncio.gather(first_task, second_task)
        release_workers.set()
        await asyncio.to_thread(all_finished.wait, 1)
        return first, second, third

    first, second, third = _run(run_timeout_checks())

    assert [first.status, second.status, third.status] == ["timeout", "timeout", "timeout"]
    assert len(calls) == 2
    assert all_finished.is_set()


def test_sync_worker_limit_is_shared_across_registries_in_one_loop():
    lock = threading.Lock()
    started_count = 0
    finished_count = 0
    both_started = threading.Event()
    all_finished = threading.Event()
    release_workers = threading.Event()
    calls = []

    class SlowStore:
        def query_result(self, **kwargs):
            nonlocal started_count, finished_count
            with lock:
                calls.append(kwargs)
                started_count += 1
                if started_count == 2:
                    both_started.set()
            try:
                release_workers.wait(2)
                return RagQueryResult("no_hits")
            finally:
                with lock:
                    finished_count += 1
                    if finished_count == 2:
                        all_finished.set()

    registries = [
        ReadonlyToolRegistry(guild_id=123, rag_store=SlowStore(), timeout=0.05)
        for _ in range(3)
    ]

    async def run_shared_gate_check():
        first_task = asyncio.create_task(
            registries[0].execute("search_admission_guides", '{"query":"one"}')
        )
        second_task = asyncio.create_task(
            registries[1].execute("search_admission_guides", '{"query":"two"}')
        )
        assert await asyncio.to_thread(both_started.wait, 1)
        third = await registries[2].execute(
            "search_admission_guides", '{"query":"three"}'
        )
        first, second = await asyncio.gather(first_task, second_task)
        release_workers.set()
        await asyncio.to_thread(all_finished.wait, 1)
        return first, second, third

    first, second, third = _run(run_shared_gate_check())

    assert [first.status, second.status, third.status] == ["timeout", "timeout", "timeout"]
    assert len(calls) == 2
    assert all_finished.is_set()


def test_evidence_and_tool_result_are_immutable_data_shapes():
    evidence = Evidence(
        evidence_id="e_safe",
        title="簡章",
        document_id="doc-123",
        page=1,
        chunk=0,
        source_url="",
        source_type="admission_guide",
        text="純資料",
    )
    result = ToolResult("ok", (evidence,))

    assert result.as_dict()["evidence"][0]["evidence_id"] == "e_safe"
    assert json.loads(result.to_json())["status"] == "ok"
