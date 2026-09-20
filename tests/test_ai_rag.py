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
