from __future__ import annotations

import hashlib
import io
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class IngestResult:
    document_id: str
    title: str
    chunk_count: int
    page_count: int


@dataclass(frozen=True, slots=True)
class RagHit:
    text: str
    metadata: dict[str, Any]
    distance: float | None = None


@dataclass(frozen=True, slots=True)
class RagQueryResult:
    status: str
    hits: tuple[RagHit, ...] = ()
    detail: str | None = None


_ENTITY_ALIASES: dict[str, tuple[str, ...]] = {
    "交大": ("陽明交通大學", "交通大學"),
    "陽明交大": ("陽明交通大學",),
    "陽明交通大學": ("陽明交通大學",),
    "交通大學": ("交通大學",),
    "nycu": ("陽明交通大學",),
}


def _normalize_search_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return re.sub(r"\s+", "", normalized)


_CS_ALIASES = ("資工", "資訊工程", "計算機", "计算机")
_SECURITY_ALIASES = ("資安", "資訊安全", "信息安全")
_SECURITY_GROUP_ALIASES = ("資安組", "資訊安全組", "信息安全組")
_SECURITY_SECTION_ALIASES = (*_SECURITY_GROUP_ALIASES, "資訊安全學系", "資訊安全學程")
_TOPIC_ALIASES = (
    ("審查", "書審", "書面審查", "評分", "評量"),
    ("考試方式", "甄選方式", "甄試方式", "考試", "甄選", "甄試", "初試", "複試", "面試", "口試"),
    ("資格", "條件", "限制"),
    ("文件", "資料", "繳件", "繳交", "備審"),
    ("日期", "時間", "期限", "截止"),
)
# "大學程式設計" names an exam, not a degree-program heading.
_SECTION_HEADING = re.compile(r"學系|學程(?!式)|研究所|系所組|[（(][^（）()\n]{1,30}組[）)]")
_SECTION_TITLE = re.compile(
    r"(?:[\w&/-]{1,60}(?:學系|學程|研究所)(?:\([^()\n]{1,30}組\))?"
    r"|\([^()\n]{1,30}組\)|[^\W\d_]{1,30}組)"
)
_DOCUMENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _search_terms(query_text: str) -> list[str]:
    normalized = _normalize_search_text(query_text)
    words = re.findall(r"\w{2,}", unicodedata.normalize("NFKC", str(query_text or "")).casefold())
    entities = set(_explicit_entity_terms(query_text))
    return list(dict.fromkeys(term for term in (normalized, *words) if term and term not in entities))


def _explicit_entity_terms(query_text: str) -> list[str]:
    normalized = _normalize_search_text(query_text)
    terms: list[str] = []
    for alias, expansions in _ENTITY_ALIASES.items():
        if alias in normalized:
            terms.extend((alias, *expansions))
    return list(dict.fromkeys(term for term in terms if term))


def _explicit_field_groups(query_text: str) -> list[tuple[str, ...]]:
    normalized = _normalize_search_text(query_text)
    groups: list[tuple[str, ...]] = []
    if any(alias in normalized for alias in _CS_ALIASES):
        groups.append(_CS_ALIASES)
    if any(alias in normalized for alias in _SECURITY_GROUP_ALIASES):
        groups.append(_SECURITY_GROUP_ALIASES)
    elif any(alias in normalized for alias in _SECURITY_ALIASES):
        groups.append(_SECURITY_SECTION_ALIASES)
    return groups


def _matches_aliases(aliases: tuple[str, ...], normalized_text: str) -> bool:
    return any(alias in normalized_text for alias in aliases)


def _matches_heading_context(field_groups: list[tuple[str, ...]], document: str) -> bool:
    heading = ""
    for line in document.splitlines():
        normalized = _normalize_search_text(line).split(":", 1)[0]
        if normalized in {"班組", "系所組"} or not _SECTION_TITLE.fullmatch(normalized):
            continue
        if normalized.startswith("(") or normalized.endswith("組"):
            # A new group replaces, rather than retains, the prior group label.
            heading = f"{heading.split('(', 1)[0]}({normalized.strip('()')})"
        else:
            heading = normalized
    return bool(heading) and all(_matches_aliases(aliases, heading) for aliases in field_groups)


def _matches_entity(entity_terms: list[str], title: str, document: str, filename: str = "") -> bool:
    if not entity_terms:
        return True
    for label in (title, filename):
        normalized_label = _normalize_search_text(label)
        schools = re.findall(r"[㐀-鿿]+大學", normalized_label)
        if schools:
            # The short alias "交通大學" must not match Shanghai/Xi'an universities.
            return any(
                school == term
                or school.endswith(f"國立{term}")
                or (term != "交通大學" and school.endswith(term))
                for school in schools
                for term in entity_terms
                if term.endswith("大學")
            )
        if any(term in normalized_label for term in entity_terms):
            return True
    normalized_document = _normalize_search_text(document)
    return any(term in normalized_document for term in entity_terms)


def _lexical_score(query_terms: list[str], document: str) -> tuple[float, int]:
    normalized_document = _normalize_search_text(document)
    matches = [term for term in query_terms if len(term) > 1 and term in normalized_document]
    return sum(20.0 + min(len(term), 8) for term in matches), len(matches)


def _chunk_key(metadata: dict[str, Any], guild_id: int) -> tuple[str, int, int] | None:
    document_id, page, chunk = (metadata.get(name) for name in ("document_id", "page", "chunk_index"))
    if (
        type(metadata.get("guild_id")) is not int
        or metadata["guild_id"] != guild_id
        or metadata.get("kind") != "admission_guide"
        or not isinstance(document_id, str)
        or not _DOCUMENT_ID.fullmatch(document_id)
        or type(page) is not int
        or page < 1
        or type(chunk) is not int
        or chunk < 0
    ):
        return None
    return document_id, page, chunk


def chunk_text(text: str, *, max_chars: int = 1200, overlap: int = 160) -> list[str]:
    text = re.sub(r"\r\n?", "\n", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    max_chars = max(1, int(max_chars))
    overlap = max(0, min(int(overlap), max_chars // 2))
    if not text:
        return []

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            boundary_start = start + max_chars // 2
            candidates = [
                text.rfind("\n\n", boundary_start, end),
                text.rfind("\n", boundary_start, end),
                text.rfind(" ", boundary_start, end),
            ]
            boundary = max(candidates)
            if boundary > start:
                end = boundary

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        next_start = end - overlap
        start = max(start + 1, next_start)

    return chunks


def extract_document_pages(data: bytes, filename: str, *, max_pages: int = 120) -> list[str]:
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".pdf" or data[:5] == b"%PDF-":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("尚未安裝 pypdf，無法讀取 PDF。") from exc

        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > max_pages:
            raise ValueError(f"簡章頁數過多，最多支援 {max_pages} 頁。")
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        return pages

    try:
        return [data.decode("utf-8-sig", errors="replace").strip()]
    except Exception as exc:
        raise ValueError("無法讀取簡章文字內容。") from exc


class AdmissionGuideStore:
    """Chroma-backed, guild-scoped store for admission-guide chunks."""

    def __init__(
        self,
        *,
        directory: Path,
        collection_name: str = "ai_admission_guides",
        max_bytes: int = 10 * 1024 * 1024,
        max_pages: int = 120,
        chunk_chars: int = 1200,
        chunk_overlap: int = 160,
    ) -> None:
        import chromadb

        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max(1, int(max_bytes))
        self.max_pages = max(1, int(max_pages))
        self.chunk_chars = max(100, int(chunk_chars))
        self.chunk_overlap = max(0, int(chunk_overlap))
        client = chromadb.PersistentClient(path=str(self.directory))
        self.collection = client.get_or_create_collection(name=collection_name)

    def ingest_bytes(
        self,
        *,
        guild_id: int,
        data: bytes,
        filename: str,
        title: str = "",
        source_url: str = "",
    ) -> IngestResult:
        if len(data) > self.max_bytes:
            raise ValueError(f"簡章檔案過大，最多 {self.max_bytes // (1024 * 1024)} MB。")

        pages = extract_document_pages(data, filename, max_pages=self.max_pages)
        title = (title or Path(filename).stem or "未命名簡章").strip()
        document_id = hashlib.sha256(data).hexdigest()[:24]
        now = int(time.time())

        records: list[tuple[str, str, dict[str, Any]]] = []
        for page_index, page_text in enumerate(pages, start=1):
            for chunk_index, chunk in enumerate(
                chunk_text(page_text, max_chars=self.chunk_chars, overlap=self.chunk_overlap)
            ):
                record_id = f"{guild_id}:{document_id}:{page_index}:{chunk_index}"
                metadata = {
                    "guild_id": int(guild_id),
                    "kind": "admission_guide",
                    "document_id": document_id,
                    "title": title,
                    "filename": filename,
                    "source_url": source_url,
                    "page": page_index,
                    "chunk_index": chunk_index,
                    "ingested_at": now,
                }
                records.append((record_id, chunk, metadata))

        if not records:
            raise ValueError("簡章沒有可讀取的文字內容。")

        try:
            self.collection.delete(
                where={
                    "$and": [
                        {"guild_id": {"$eq": int(guild_id)}},
                        {"document_id": {"$eq": document_id}},
                    ]
                }
            )
        except Exception:
            pass

        self.collection.add(
            ids=[record[0] for record in records],
            documents=[record[1] for record in records],
            metadatas=[record[2] for record in records],
        )
        return IngestResult(
            document_id=document_id,
            title=title,
            chunk_count=len(records),
            page_count=len(pages),
        )

    def _lexical_query(self, *, guild_id: int, query_text: str, top_k: int) -> list[RagHit]:
        normalized_query = _normalize_search_text(query_text)
        if not normalized_query:
            return []
        query_terms = _search_terms(query_text)
        entity_terms = _explicit_entity_terms(query_text)
        field_groups = _explicit_field_groups(query_text)
        topic_groups = [aliases for aliases in _TOPIC_ALIASES if _matches_aliases(aliases, normalized_query)]
        result = self.collection.get(
            where={
                "$and": [
                    {"guild_id": {"$eq": int(guild_id)}},
                    {"kind": {"$eq": "admission_guide"}},
                ]
            },
            include=["documents", "metadatas"],
        )

        documents = result.get("documents", []) or []
        metadatas = result.get("metadatas", []) or []
        rows: list[tuple[RagHit, str, tuple[bool, ...], tuple[str, int, int] | None]] = []
        by_chunk: dict[tuple[str, int, int], list[int]] = {}
        for index, document in enumerate(documents):
            if not isinstance(document, str) or not document.strip():
                continue
            metadata = dict(metadatas[index] or {}) if index < len(metadatas) else {}
            if (
                (
                    "guild_id" in metadata
                    and (type(metadata["guild_id"]) is not int or metadata["guild_id"] != int(guild_id))
                )
                or ("kind" in metadata and metadata["kind"] != "admission_guide")
            ):
                continue
            title = str(metadata.get("title") or "")
            filename = str(metadata.get("filename") or "")
            if not _matches_entity(entity_terms, title, document, filename):
                continue
            normalized_document = _normalize_search_text(document)
            fields = tuple(_matches_aliases(aliases, normalized_document) for aliases in field_groups)
            key = _chunk_key(metadata, int(guild_id))
            if key is not None:
                by_chunk.setdefault(key, []).append(len(rows))
            rows.append((RagHit(document, metadata), normalized_document, fields, key))

        scored: list[tuple[tuple[Any, ...], RagHit]] = []
        for hit, normalized_document, fields, key in rows:
            if field_groups and not all(fields):
                # A headerless continuation can inherit only from the immediately
                # preceding, unambiguous heading in this guild/document/page.
                if key is None or len(by_chunk[key]) != 1 or _SECTION_HEADING.search(hit.text):
                    continue
                previous = by_chunk.get((key[0], key[1], key[2] - 1), [])
                if len(previous) != 1:
                    continue
                anchor, _, anchor_fields, _ = rows[previous[0]]
                if not all(anchor_fields) or not _matches_heading_context(field_groups, anchor.text):
                    continue
            matched_topics = [
                tuple(alias for alias in aliases if alias in normalized_document)
                for aliases in topic_groups
            ]
            topic_matches = sum(bool(matches) for matches in matched_topics)
            exact_topics = sum(
                any(alias in normalized_query for alias in matches)
                for matches in matched_topics
            )
            topic_score = sum(max(map(len, matches), default=0) for matches in matched_topics)
            score, matched_terms = _lexical_score(query_terms, hit.text)
            if (
                not field_groups
                and not topic_matches
                and score < 20
                and matched_terms < 2
                and normalized_query not in entity_terms
            ):
                continue
            numbers = []
            for name in ("page", "chunk_index"):
                try:
                    numbers.append(int(hit.metadata.get(name, 0)))
                except (TypeError, ValueError):
                    numbers.append(0)
            # Topics and field coverage outrank repetitions of a school name.
            rank = (
                -topic_matches,
                -exact_topics,
                -topic_score,
                -sum(fields),
                -score,
                *numbers,
                str(hit.metadata.get("document_id", "")),
                hit.text,
            )
            scored.append((rank, hit))

        scored.sort(key=lambda item: item[0])
        return [hit for _, hit in scored[: max(1, int(top_k))]]

    def query_result(
        self,
        *,
        guild_id: int,
        query_text: str,
        top_k: int = 3,
        enabled: bool = True,
    ) -> RagQueryResult:
        """Retrieve guide chunks while preserving errors separately from empty results."""
        if not enabled:
            return RagQueryResult("disabled", detail="RAG retrieval is disabled.")
        if getattr(self, "collection", None) is None:
            return RagQueryResult("unavailable", detail="RAG collection is unavailable.")
        if not isinstance(query_text, str) or not query_text.strip():
            return RagQueryResult("no_hits")

        limit = max(1, int(top_k))
        lexical_error: Exception | None = None
        try:
            lexical_hits = self._lexical_query(
                guild_id=guild_id,
                query_text=query_text,
                top_k=limit,
            )
        except Exception as exc:
            lexical_hits = []
            lexical_error = exc
        if lexical_hits:
            return RagQueryResult("ok", tuple(lexical_hits))

        if _explicit_entity_terms(query_text) or any(ord(char) > 127 for char in query_text):
            if lexical_error is not None:
                return RagQueryResult(
                    "error",
                    detail=f"Lexical retrieval failed ({type(lexical_error).__name__}).",
                )
            return RagQueryResult("no_hits")

        try:
            result = self.collection.query(
                query_texts=[query_text],
                n_results=limit,
                where={
                    "$and": [
                        {"guild_id": {"$eq": int(guild_id)}},
                        {"kind": {"$eq": "admission_guide"}},
                    ]
                },
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            return RagQueryResult(
                "error",
                detail=(
                    "Lexical and vector retrieval failed."
                    if lexical_error is not None
                    else f"Vector retrieval failed ({type(exc).__name__})."
                ),
            )

        try:
            documents = result.get("documents") or [[]]
            metadatas = result.get("metadatas") or [[]]
            distances = result.get("distances") or [[]]
            docs = documents[0] if documents else []
            metadata_rows = metadatas[0] if metadatas else []
            distance_rows = distances[0] if distances else []

            vector_hits: list[RagHit] = []
            for index, document in enumerate(docs):
                if not isinstance(document, str) or not document.strip():
                    continue
                metadata = metadata_rows[index] if index < len(metadata_rows) else {}
                distance = distance_rows[index] if index < len(distance_rows) else None
                vector_hits.append(
                    RagHit(
                        text=document,
                        metadata=dict(metadata or {}),
                        distance=float(distance) if isinstance(distance, (int, float)) else None,
                    )
                )
        except Exception as exc:
            return RagQueryResult(
                "error",
                detail=f"Vector retrieval failed ({type(exc).__name__}).",
            )
        if vector_hits:
            return RagQueryResult("ok", tuple(vector_hits))
        if lexical_error is not None:
            return RagQueryResult(
                "error",
                detail=f"Lexical retrieval failed ({type(lexical_error).__name__}).",
            )
        return RagQueryResult("no_hits")

    def query(self, *, guild_id: int, query_text: str, top_k: int = 3) -> list[RagHit]:
        """Legacy retrieval API; callers still receive a list of hits."""
        return list(
            self.query_result(
                guild_id=guild_id,
                query_text=query_text,
                top_k=top_k,
            ).hits
        )

    def read_document_result(
        self,
        *,
        guild_id: int,
        document_id: str,
        page: int | None = None,
        chunk: int | None = None,
        limit: int = 5,
    ) -> RagQueryResult:
        """Read bounded chunks from one guide, enforcing guild/kind/document scope."""
        if getattr(self, "collection", None) is None:
            return RagQueryResult("unavailable", detail="RAG collection is unavailable.")
        if not isinstance(document_id, str) or not document_id:
            return RagQueryResult("no_hits")
        try:
            page_number = int(page) if page is not None else None
            chunk_number = int(chunk) if chunk is not None else None
            result_limit = max(1, min(int(limit), 100))
            filters: list[dict[str, Any]] = [
                {"guild_id": {"$eq": int(guild_id)}},
                {"kind": {"$eq": "admission_guide"}},
                {"document_id": {"$eq": document_id}},
            ]
            if page_number is not None:
                filters.append({"page": {"$eq": page_number}})
            if chunk_number is not None:
                filters.append({"chunk_index": {"$eq": chunk_number}})
            result = self.collection.get(
                where={"$and": filters},
                include=["documents", "metadatas"],
                limit=result_limit,
            )
        except Exception as exc:
            return RagQueryResult(
                "error",
                detail=f"Document retrieval failed ({type(exc).__name__}).",
            )

        try:
            documents = result.get("documents", []) or []
            metadatas = result.get("metadatas", []) or []
            scoped_hits: list[RagHit] = []
            for index, document in enumerate(documents):
                if not isinstance(document, str) or not document.strip() or index >= len(metadatas):
                    continue
                metadata = dict(metadatas[index] or {})
                try:
                    metadata_guild_id = int(metadata.get("guild_id"))
                    metadata_page = int(metadata.get("page"))
                    metadata_chunk = int(metadata.get("chunk_index"))
                except (TypeError, ValueError):
                    continue
                if (
                    metadata_guild_id != int(guild_id)
                    or metadata.get("kind") != "admission_guide"
                    or metadata.get("document_id") != document_id
                    or (page_number is not None and metadata_page != page_number)
                    or (chunk_number is not None and metadata_chunk != chunk_number)
                ):
                    continue
                scoped_hits.append(RagHit(text=document, metadata=metadata))

            scoped_hits.sort(
                key=lambda hit: (
                    int(hit.metadata.get("page", 0)),
                    int(hit.metadata.get("chunk_index", 0)),
                )
            )
        except Exception as exc:
            return RagQueryResult(
                "error",
                detail=f"Document retrieval failed ({type(exc).__name__}).",
            )
        if not scoped_hits:
            return RagQueryResult("no_hits")
        return RagQueryResult("ok", tuple(scoped_hits[:result_limit]))

    def read_document(
        self,
        *,
        guild_id: int,
        document_id: str,
        page: int | None = None,
        chunk: int | None = None,
        limit: int = 5,
    ) -> list[RagHit]:
        """Legacy list-returning wrapper for scoped document reads."""
        return list(
            self.read_document_result(
                guild_id=guild_id,
                document_id=document_id,
                page=page,
                chunk=chunk,
                limit=limit,
            ).hits
        )
