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


_QUERY_ALIASES: dict[str, tuple[str, ...]] = {
    "交大": ("陽明交通大學", "交通大學"),
    "陽明交大": ("陽明交通大學",),
    "陽明交通大學": ("陽明交通大學",),
    "交通大學": ("交通大學",),
    "nycu": ("陽明交通大學",),
    "資工": ("資訊工程", "資訊工程學系"),
    "計算機": ("資訊工程", "資訊工程學系"),
    "计算机": ("資訊工程", "資訊工程學系"),
}

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


def _search_terms(query_text: str) -> list[str]:
    normalized = _normalize_search_text(query_text)
    if not normalized:
        return []

    terms: list[str] = [normalized]
    for alias, expansions in _QUERY_ALIASES.items():
        if alias in normalized:
            terms.extend((alias, *expansions))
    if "組" in normalized:
        terms.append("組")

    unique_terms: list[str] = []
    for term in terms:
        if term and term not in unique_terms:
            unique_terms.append(term)
    return unique_terms


def _explicit_entity_terms(query_text: str) -> list[str]:
    normalized = _normalize_search_text(query_text)
    terms: list[str] = []
    for alias, expansions in _ENTITY_ALIASES.items():
        if alias in normalized:
            terms.extend((alias, *expansions))
    return list(dict.fromkeys(term for term in terms if term))


def _explicit_field_terms(query_text: str) -> list[str]:
    normalized = _normalize_search_text(query_text)
    terms: list[str] = []
    for alias, expansions in _QUERY_ALIASES.items():
        if alias in _ENTITY_ALIASES:
            continue
        if alias in normalized:
            terms.extend((alias, *expansions))
    return list(dict.fromkeys(term for term in terms if term))


def _matches_entity(entity_terms: list[str], title: str, document: str) -> bool:
    if not entity_terms:
        return True
    normalized_title = _normalize_search_text(title)
    normalized_document = _normalize_search_text(document)
    return any(
        term in normalized_title or term in normalized_document
        for term in entity_terms
    )


def _lexical_score(query_terms: list[str], title: str, document: str) -> tuple[float, int]:
    normalized_title = _normalize_search_text(title)
    normalized_document = _normalize_search_text(document)
    score = 0.0
    matched_terms = 0
    for term in query_terms:
        if len(term) <= 1:
            if term in normalized_document:
                score += 0.5
            continue
        if term in normalized_title:
            score += 20.0 + min(len(term), 8)
            matched_terms += 1
        elif term in normalized_document:
            score += 5.0 + min(len(term), 8)
            matched_terms += 1

    return score, matched_terms


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
        query_terms = _search_terms(query_text)
        if not query_terms:
            return []
        entity_terms = _explicit_entity_terms(query_text)
        field_terms = _explicit_field_terms(query_text)
        try:
            result = self.collection.get(
                where={
                    "$and": [
                        {"guild_id": {"$eq": int(guild_id)}},
                        {"kind": {"$eq": "admission_guide"}},
                    ]
                },
                include=["documents", "metadatas"],
            )
        except Exception:
            return []

        documents = result.get("documents", []) or []
        metadatas = result.get("metadatas", []) or []
        scored: list[tuple[float, int, str, dict[str, Any]]] = []
        for index, document in enumerate(documents):
            if not isinstance(document, str) or not document.strip():
                continue
            metadata = dict(metadatas[index] or {}) if index < len(metadatas) else {}
            title = f"{metadata.get('title', '')} {metadata.get('filename', '')}"
            if entity_terms and not _matches_entity(entity_terms, title, document):
                continue
            if field_terms and not _matches_entity(field_terms, "", document):
                continue
            score, matched_terms = _lexical_score(query_terms, title, document)
            if entity_terms:
                score += 60.0
                matched_terms += 1
            if score < 20 and matched_terms < 2:
                continue
            page = metadata.get("page")
            try:
                page_number = int(page)
            except (TypeError, ValueError):
                page_number = 0
            scored.append((score, page_number, document, metadata))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            RagHit(text=document, metadata=metadata)
            for _, _, document, metadata in scored[: max(1, int(top_k))]
        ]

    def query(self, *, guild_id: int, query_text: str, top_k: int = 3) -> list[RagHit]:
        if not query_text.strip():
            return []
        limit = max(1, int(top_k))

        lexical_hits = self._lexical_query(
            guild_id=guild_id,
            query_text=query_text,
            top_k=limit,
        )
        if lexical_hits:
            return lexical_hits
        if _explicit_entity_terms(query_text):
            return []
        if any(ord(char) > 127 for char in query_text):
            return []

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
        except Exception:
            return []
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
        return vector_hits
