import pytest

from bot.utils.ai_rag import AdmissionGuideStore, chunk_text


class FakeCollection:
    def __init__(self):
        self.added = []
        self.deleted = []

    def delete(self, *, where):
        self.deleted.append(where)

    def add(self, *, ids, documents, metadatas):
        self.added.append((ids, documents, metadatas))


class QueryCollection:
    def query(self, **kwargs):
        return {
            "documents": [["其他學校的交通資訊"]],
            "metadatas": [[{"title": "國立高雄大學", "page": 1}]],
            "distances": [[0.1]],
        }

    def get(self, **kwargs):
        return {
            "documents": [
                "其他學校的交通資訊與組別",
                "國立陽明交通大學招生簡章封面與目錄",
                "國立陽明交通大學 資訊工程學系 資安組與其他組別說明",
            ],
            "metadatas": [
                {"title": "國立高雄大學", "filename": "kaohsiung.pdf", "page": 1},
                {"title": "國立陽明交通大學", "filename": "nycu.pdf", "page": 1},
                {"title": "國立陽明交通大學", "filename": "nycu.pdf", "page": 19},
            ],
        }


def test_chunk_text_uses_overlap_and_limits_size():
    chunks = chunk_text("甲" * 100, max_chars=40, overlap=10)

    assert len(chunks) >= 3
    assert all(len(chunk) <= 40 for chunk in chunks)
    assert chunks[0][-10:] == chunks[1][:10]


def test_query_uses_alias_and_lexical_ranking_for_chiao_tung():
    store = AdmissionGuideStore.__new__(AdmissionGuideStore)
    store.collection = QueryCollection()

    hits = store.query(
        guild_id=123,
        query_text="交大資工有哪些組？",
        top_k=1,
    )

    assert len(hits) == 1
    assert hits[0].metadata["title"] == "國立陽明交通大學"
    assert "資訊工程" in hits[0].text


def test_ingest_text_stores_guild_scoped_metadata():
    store = AdmissionGuideStore.__new__(AdmissionGuideStore)
    store.collection = FakeCollection()
    store.max_bytes = 10000
    store.chunk_chars = 20
    store.chunk_overlap = 4
    store.max_pages = 20

    result = store.ingest_bytes(
        guild_id=123,
        data="招生資格\n報名期限\n".encode("utf-8"),
        filename="guide.txt",
        title="2026 招生簡章",
    )

    assert result.chunk_count >= 1
    assert store.collection.added
    metadata = store.collection.added[0][2][0]
    assert metadata["guild_id"] == 123
    assert metadata["kind"] == "admission_guide"
    assert metadata["title"] == "2026 招生簡章"


class NoLexicalMatchCollection:
    def query(self, **kwargs):
        return {
            "documents": [["國立陽明交通大學完全不相關的內容"]],
            "metadatas": [[{"title": "國立陽明交通大學", "page": 2}]],
            "distances": [[0.01]],
        }

    def get(self, **kwargs):
        return {
            "documents": ["國立陽明交通大學完全不相關的內容"],
            "metadatas": [{"title": "國立陽明交通大學", "page": 2}],
        }


def test_query_does_not_return_unrelated_vector_hit_for_unmatched_cjk():
    store = AdmissionGuideStore.__new__(AdmissionGuideStore)
    store.collection = NoLexicalMatchCollection()

    hits = store.query(
        guild_id=123,
        query_text="完全不存在的中文問題",
        top_k=1,
    )

    assert hits == []


class LexicalOnlyCollection:
    def query(self, **kwargs):
        raise RuntimeError("embedding model unavailable")

    def get(self, **kwargs):
        return {
            "documents": ["國立陽明交通大學資訊工程學系的組別說明"],
            "metadatas": [{"title": "國立陽明交通大學", "page": 19}],
        }


def test_query_can_use_lexical_retrieval_when_vector_query_fails():
    store = AdmissionGuideStore.__new__(AdmissionGuideStore)
    store.collection = LexicalOnlyCollection()

    hits = store.query(
        guild_id=123,
        query_text="交大資工有哪些組？",
        top_k=1,
    )

    assert len(hits) == 1
    assert hits[0].metadata["page"] == 19


def test_lexical_query_does_not_boost_nycu_for_unrelated_text():
    store = AdmissionGuideStore.__new__(AdmissionGuideStore)
    store.collection = LexicalOnlyCollection()

    hits = store._lexical_query(
        guild_id=123,
        query_text="校園交通規定",
        top_k=1,
    )

    assert hits == []


class EmptyCollection:
    def get(self, **kwargs):
        return {"documents": [], "metadatas": []}


class FailingCollection:
    def get(self, **kwargs):
        raise RuntimeError("storage unavailable")


class ScopedReadCollection:
    def __init__(self):
        self.kwargs = None

    def get(self, **kwargs):
        self.kwargs = kwargs
        return {
            "documents": ["valid chunk", "foreign guild", "foreign document", "wrong kind"],
            "metadatas": [
                {
                    "guild_id": 123,
                    "kind": "admission_guide",
                    "document_id": "doc-123",
                    "page": 1,
                    "chunk_index": 0,
                },
                {
                    "guild_id": 456,
                    "kind": "admission_guide",
                    "document_id": "doc-123",
                    "page": 1,
                    "chunk_index": 0,
                },
                {
                    "guild_id": 123,
                    "kind": "admission_guide",
                    "document_id": "other-doc",
                    "page": 1,
                    "chunk_index": 0,
                },
                {
                    "guild_id": 123,
                    "kind": "other",
                    "document_id": "doc-123",
                    "page": 1,
                    "chunk_index": 0,
                },
            ],
        }


def test_query_result_distinguishes_no_hits_disabled_unavailable_and_errors():
    store = AdmissionGuideStore.__new__(AdmissionGuideStore)
    store.collection = EmptyCollection()
    assert store.query_result(guild_id=123, query_text="招生期限").status == "no_hits"
    assert store.query_result(guild_id=123, query_text="招生期限", enabled=False).status == "disabled"

    missing_collection = AdmissionGuideStore.__new__(AdmissionGuideStore)
    assert missing_collection.query_result(guild_id=123, query_text="招生期限").status == "unavailable"

    failing = AdmissionGuideStore.__new__(AdmissionGuideStore)
    failing.collection = FailingCollection()
    result = failing.query_result(guild_id=123, query_text="招生期限")
    assert result.status == "error"
    assert result.hits == ()
    assert failing.query(guild_id=123, query_text="招生期限") == []


def test_read_document_is_restricted_to_same_guild_kind_and_document():
    store = AdmissionGuideStore.__new__(AdmissionGuideStore)
    store.collection = ScopedReadCollection()

    result = store.read_document_result(
        guild_id=123,
        document_id="doc-123",
        page=1,
        chunk=0,
        limit=3,
    )

    assert result.status == "ok"
    assert [hit.text for hit in result.hits] == ["valid chunk"]
    where = store.collection.kwargs["where"]["$and"]
    assert {"guild_id": {"$eq": 123}} in where
    assert {"kind": {"$eq": "admission_guide"}} in where
    assert {"document_id": {"$eq": "doc-123"}} in where
    assert {"page": {"$eq": 1}} in where
    assert {"chunk_index": {"$eq": 0}} in where
    assert store.collection.kwargs["limit"] == 3


class RankedGuideCollection:
    def __init__(self, rows):
        self.rows = rows
        self.get_calls = []
        self.query_calls = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return {
            "documents": [text for text, _ in self.rows],
            "metadatas": [metadata for _, metadata in self.rows],
        }

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        raise AssertionError("A specific Chinese guide query must not use unrelated vector hits")


def guide_row(text, *, page=21, chunk=0, **metadata):
    return text, {
        "guild_id": 123,
        "kind": "admission_guide",
        "document_id": "nycu-116",
        "title": "國立陽明交通大學",
        "filename": "nycu-116.pdf",
        "page": page,
        "chunk_index": chunk,
        **metadata,
    }


def ranked_store(rows):
    store = AdmissionGuideStore.__new__(AdmissionGuideStore)
    store.collection = RankedGuideCollection(rows)
    return store


def security_guide_rows():
    return [
        guide_row("國立陽明交通大學 116 學年度特殊選才招生簡章\n報名日期及招生網址", page=1),
        guide_row("國立陽明交通大學招生重要日程表", page=2),
        guide_row("國立陽明交通大學校園介紹，又稱陽明交大及交大。", page=18),
        guide_row("資訊工程學系\n可提出 MyFirstCTF 資安競賽、APCS 等證明。\n書面審查。", page=19),
        guide_row("資訊工程學系（資安組）\n報考資格：資安技能金盾獎、AIS3 或其他特殊天賦證明。\n通過初試參加複試者另收報名費。"),
        guide_row("大學程式設計先修檢測(APCS)成績單等。\n考試方式\n初試[0.3] 書面審查[1]\n複試[0.7] 面試[1]", chunk=1),
    ]


@pytest.mark.parametrize("query", [
    "交大資安組審查標準",
    "陽明交大資安組的書審標準是什麼？",
    "NYCU 資安組考試方式",
    "陽明交通大學資工系資安組審查標準",
    "交大資訊安全組書面審查",
    "請問交大資訊安全的甄選方式？",
    "ＮＹＣＵ 資 安 組 審 查 標 準",
])
def test_security_group_query_returns_review_and_heading_not_cover(query):
    rows = security_guide_rows()
    store = ranked_store(rows)

    result = store.query_result(guild_id=123, query_text=query, top_k=3)

    assert result.status == "ok"
    assert [(hit.metadata["page"], hit.metadata["chunk_index"]) for hit in result.hits] == [(21, 1), (21, 0)]
    assert "書面審查" in result.hits[0].text
    assert result.hits[0].text == rows[-1][0]
    assert result.hits[1].text == rows[-2][0]
    assert result.hits[0].metadata == rows[-1][1]
    assert result.hits[1].metadata == rows[-2][1]
    assert not store.collection.query_calls
    assert len(store.collection.get_calls) == 1
    assert store.collection.get_calls[0]["where"] == {
        "$and": [{"guild_id": {"$eq": 123}}, {"kind": {"$eq": "admission_guide"}}],
    }


@pytest.mark.parametrize("group", ["資安組", "資訊安全組"])
def test_specific_security_group_does_not_match_general_cs_competition_mentions(group):
    store = ranked_store(security_guide_rows()[:4])

    result = store.query_result(guild_id=123, query_text=f"交大{group}審查標準")

    assert result.status == "no_hits"
    assert result.hits == ()
    assert not store.collection.query_calls


def test_all_requested_fields_must_match_instead_of_any_alias():
    rows = [
        guide_row("資訊工程學系\n書面審查與面試", page=19),
        guide_row("資訊安全管理學系（資安組）\n書面審查與面試", page=23),
        *security_guide_rows()[-2:],
    ]
    store = ranked_store(rows)

    hits = store.query(guild_id=123, query_text="交大資工系資安組審查標準", top_k=3)

    assert len(hits) == 2
    assert {hit.metadata["page"] for hit in hits} == {21}


@pytest.mark.parametrize("metadata", [
    {"document_id": "other-document"},
    {"page": 22},
    {"chunk_index": 2},
    {"guild_id": 456},
    {"kind": "private_memory"},
    {"title": "國立高雄大學", "filename": "other.pdf"},
])
def test_headerless_review_cannot_inherit_from_other_scope_or_nonadjacent_chunk(metadata):
    anchor = security_guide_rows()[-2]
    other = guide_row("書面審查 初試[0.3] 複試[0.7]", chunk=1, **metadata)
    store = ranked_store([anchor, other])

    hits = store.query(guild_id=123, query_text="交大資安組審查標準", top_k=3)

    assert [hit.text for hit in hits] == [anchor[0]]


@pytest.mark.parametrize("missing", ["guild_id", "kind", "document_id", "page", "chunk_index"])
def test_incomplete_metadata_cannot_join_a_continuation(missing):
    anchor = security_guide_rows()[-2]
    continuation, metadata = guide_row("書面審查 初試[0.3] 複試[0.7]", chunk=1)
    metadata.pop(missing)
    store = ranked_store([anchor, (continuation, metadata)])

    hits = store.query(guild_id=123, query_text="交大資安組審查標準", top_k=3)

    assert [hit.text for hit in hits] == [anchor[0]]


@pytest.mark.parametrize("header", [
    "資訊工程學系（一般組）",
    "電機工程學系",
    "管理學院財務金融學系",
    "電資不分系學程",
    "資訊安全研究所（一般組）",
])
def test_new_department_heading_stops_same_page_continuation(header):
    anchor = security_guide_rows()[-2]
    other = guide_row(f"{header}\n書面審查 初試[0.3] 複試[0.7]", chunk=1)
    store = ranked_store([anchor, other])

    hits = store.query(guild_id=123, query_text="交大資安組審查標準", top_k=3)

    assert [hit.text for hit in hits] == [anchor[0]]


@pytest.mark.parametrize("metadata", [
    {"guild_id": 456},
    {"kind": "private_memory"},
    {"title": "國立高雄大學", "filename": "other.pdf"},
])
def test_explicit_school_and_collection_scope_exclude_foreign_direct_hits(metadata):
    store = ranked_store([guide_row("資訊工程學系（資安組）\n書面審查與面試", **metadata)])

    assert store.query_result(guild_id=123, query_text="交大資安組審查標準").status == "no_hits"


@pytest.mark.parametrize("top_k", [1, 2, 3])
def test_review_topic_ranking_is_bounded_and_stable_after_collection_order_changes(top_k):
    rows = security_guide_rows()
    store = ranked_store(rows)
    reversed_store = ranked_store(list(reversed(rows)))

    hits = store.query(guild_id=123, query_text="交大資安組審查標準", top_k=top_k)
    reversed_hits = reversed_store.query(guild_id=123, query_text="交大資安組審查標準", top_k=top_k)

    assert len(hits) <= top_k
    assert [(hit.text, hit.metadata) for hit in hits] == [(hit.text, hit.metadata) for hit in reversed_hits]
    assert hits[0].metadata["chunk_index"] == 1


def test_qualification_topic_prioritizes_security_group_heading():
    store = ranked_store(security_guide_rows())

    hits = store.query(guild_id=123, query_text="交大資安組報考資格", top_k=2)

    assert [(hit.metadata["page"], hit.metadata["chunk_index"]) for hit in hits] == [(21, 0), (21, 1)]


@pytest.mark.parametrize("school", ["上海交通大學", "西安交通大學", "國立高雄大學"])
def test_similarly_named_university_is_not_the_requested_chiao_tung(school):
    store = ranked_store([
        guide_row("資訊工程學系（資安組）\n書面審查與面試", title=school, filename="國立陽明交通大學.pdf"),
    ])

    result = store.query_result(guild_id=123, query_text="交大資安組審查標準")

    assert result.status == "no_hits"
    assert result.hits == ()
    assert not store.collection.query_calls


@pytest.mark.parametrize("school", ["國立陽明交通大學", "國立交通大學", "交通大學"])
def test_legacy_chiao_tung_school_name_remains_searchable(school):
    store = ranked_store([
        guide_row("資訊工程學系（資安組）\n書面審查與面試", title=school),
    ])

    result = store.query_result(guild_id=123, query_text="交大資安組審查標準")

    assert result.status == "ok"
    assert result.hits[0].metadata["title"] == school


@pytest.mark.parametrize("metadata", [
    {"document_id": ""},
    {"document_id": "../other"},
    {"document_id": 123},
    {"page": True},
    {"page": "21"},
    {"page": 21.0},
    {"page": 0},
    {"chunk_index": True},
    {"chunk_index": "1"},
    {"chunk_index": 1.0},
    {"chunk_index": -1},
    {"guild_id": "123"},
    {"guild_id": 123.0},
])
def test_invalid_relation_metadata_cannot_supply_a_continuation(metadata):
    anchor = security_guide_rows()[-2]
    other = guide_row("書面審查 初試[0.3] 複試[0.7]", chunk=1, **metadata)
    store = ranked_store([anchor, other])

    hits = store.query(guild_id=123, query_text="交大資安組審查標準")

    assert [hit.text for hit in hits] == [anchor[0]]


@pytest.mark.parametrize("duplicate_chunk", [0, 1])
def test_ambiguous_duplicate_chunk_keys_cannot_join_a_continuation(duplicate_chunk):
    anchor, continuation = security_guide_rows()[-2:]
    duplicate = guide_row("電機工程學系\n書面審查", chunk=duplicate_chunk)
    store = ranked_store([anchor, continuation, duplicate])

    hits = store.query(guild_id=123, query_text="交大資安組審查標準")

    assert [hit.text for hit in hits] == [anchor[0]]


def test_header_context_does_not_propagate_to_nonadjacent_third_chunk():
    anchor, continuation = security_guide_rows()[-2:]
    third = guide_row("書面審查評分與評量方式", chunk=2)
    store = ranked_store([anchor, continuation, third])

    hits = store.query(guild_id=123, query_text="交大資安組審查標準")

    assert [hit.text for hit in hits] == [continuation[0], anchor[0]]


@pytest.mark.parametrize("title", ["116 特選招生簡章", "116學年度特殊選才招生簡章", ""])
def test_school_in_filename_remains_searchable_with_a_generic_title(title):
    row = guide_row("資訊工程學系（資安組）\n書面審查與面試", title=title, filename="國立陽明交通大學.pdf")
    store = ranked_store([row])

    result = store.query_result(guild_id=123, query_text="交大資安組審查標準")

    assert result.status == "ok"
    assert [(hit.text, hit.metadata) for hit in result.hits] == [row]


@pytest.mark.parametrize("last_heading", [
    "資訊工程學系（一般組）",
    "電機工程學系",
    "資訊安全管理學系（資安組）",
    "電資不分系學程",
    "（一般組）",
    "一般組",
])
def test_heading_at_end_of_anchor_prevents_inheriting_the_previous_group(last_heading):
    anchor = guide_row(f"資訊工程學系（資安組）\n報考資格與證明文件\n{last_heading}")
    continuation = guide_row("考試方式\n書面審查與面試", chunk=1)
    store = ranked_store([anchor, continuation])

    hits = store.query(guild_id=123, query_text="交大資工系資安組審查標準")

    assert [hit.text for hit in hits] == [anchor[0]]


@pytest.mark.parametrize("heading", [
    "資訊工程學系\n（資安組）",
    "資訊工程學系\n資安組",
    "資訊工程學系（資安組）：報考資格",
    "資訊工程學系（資安組）\n班組\n代碼 一般生：3 名",
])
def test_matching_heading_context_supports_split_group_or_colon_layouts(heading):
    anchor = guide_row(f"{heading}\n符合學系規定之報考資格證明文件。\nAPCS大學程式設計先修檢測。")
    continuation = guide_row("考試方式\n書面審查與面試", chunk=1)
    store = ranked_store([anchor, continuation])

    hits = store.query(guild_id=123, query_text="交大資工系資安組審查標準")

    assert [hit.text for hit in hits] == [continuation[0], anchor[0]]
