from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
import threading
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from bot.utils.timeouts import wait_for
from bot.utils.ai_rag import RagHit, RagQueryResult


_EVIDENCE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_DOCUMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_QUERY_MAX_CHARS = 500
_ARGUMENTS_MAX_CHARS = 4096
_MAX_CONFIG_RESULT_CHARS = 20_000
_MAX_CONFIG_TIMEOUT = 30.0
_MAX_CONFIG_TOP_K = 10
_SYNC_GATE_LIMIT = 2
_SYNC_GATES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)
_SYNC_GATES_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    title: str
    document_id: str
    page: int | None
    chunk: int | None
    source_url: str
    source_type: str
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "title": self.title,
            "document_id": self.document_id,
            "page": self.page,
            "chunk": self.chunk,
            "source_url": self.source_url,
            "source_type": self.source_type,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class ToolResult:
    status: str
    evidence: tuple[Evidence, ...] = ()
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "evidence": [item.as_dict() for item in self.evidence],
            "detail": self.detail,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))


class _InvalidArguments(ValueError):
    pass


class ReadonlyToolRegistry:
    """Allowlisted, read-only RAG and optional web-search tools for one guild."""

    def __init__(
        self,
        guild_id: int,
        rag_store: Any = None,
        rag_enabled: bool = True,
        web_search: Callable[..., Any] | None = None,
        web_enabled: bool = True,
        max_result_chars: int = 5000,
        timeout: float = 8.0,
        max_top_k: int = 5,
    ) -> None:
        if isinstance(guild_id, bool):
            raise ValueError("guild_id must be an integer")
        self.guild_id = int(guild_id)
        self.rag_store = rag_store
        self.rag_enabled = bool(rag_enabled)
        self.web_search = web_search
        self.web_enabled = bool(web_enabled)
        try:
            configured_result_chars = int(max_result_chars)
        except (TypeError, ValueError):
            configured_result_chars = 5000
        self.max_result_chars = max(128, min(configured_result_chars, _MAX_CONFIG_RESULT_CHARS))
        try:
            configured_timeout = float(timeout)
        except (TypeError, ValueError):
            configured_timeout = 8.0
        if not math.isfinite(configured_timeout):
            configured_timeout = 8.0
        self.timeout = max(0.001, min(configured_timeout, _MAX_CONFIG_TIMEOUT))
        try:
            configured_top_k = int(max_top_k)
        except (TypeError, ValueError):
            configured_top_k = 5
        self.max_top_k = max(1, min(configured_top_k, _MAX_CONFIG_TOP_K))

        self._evidence: dict[str, Evidence] = {}
        self._request_discovered_documents: set[str] = set()
        self._host_authorized_documents: set[str] = set()
        self._inherited_document_authorizations: set[str] = set()

    @property
    def evidence(self) -> dict[str, Evidence]:
        return self._evidence

    @property
    def schemas(self) -> list[dict[str, Any]]:
        search_schema = {
            "type": "function",
            "function": {
                "name": "search_admission_guides",
                "description": "Search this guild's admission-guide chunks and return evidence.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "maxLength": _QUERY_MAX_CHARS,
                            "description": "A bounded search query (maximum 500 characters).",
                        },
                        "top_k": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": self.max_top_k,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        }
        read_schema = {
            "type": "function",
            "function": {
                "name": "read_admission_guide",
                "description": (
                    "Read chunks from a document discovered by search_admission_guides "
                    "in this request; page is one-based and chunk is a zero-based chunk index."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "document_id": {
                            "type": "string",
                            "maxLength": 128,
                            "pattern": "^[A-Za-z0-9_-]{1,128}$",
                        },
                        "page": {"type": "integer", "minimum": 1, "maximum": 10000},
                        "chunk": {"type": "integer", "minimum": 0, "maximum": 100000},
                    },
                    "required": ["document_id"],
                    "additionalProperties": False,
                },
            },
        }
        schemas = [search_schema, read_schema]
        if self.web_enabled:
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "description": "Search the configured web-search provider for read-only evidence.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "maxLength": _QUERY_MAX_CHARS,
                                    "description": "A bounded search query (maximum 500 characters).",
                                }
                            },
                            "required": ["query"],
                            "additionalProperties": False,
                        },
                    },
                }
            )
        return schemas

    def authorize_evidence(self, evidence_ids: Iterable[str]) -> tuple[str, ...]:
        """Mark host-validated RAG evidence as safe for a reviewer fork to re-read."""
        if isinstance(evidence_ids, (str, bytes)):
            return ()
        authorized: set[str] = set()
        for evidence_id in evidence_ids:
            if not isinstance(evidence_id, str) or not _EVIDENCE_ID_RE.fullmatch(evidence_id):
                continue
            evidence = self._evidence.get(evidence_id)
            if (
                evidence is None
                or evidence.source_type != "admission_guide"
                or not _DOCUMENT_ID_RE.fullmatch(evidence.document_id)
            ):
                continue
            authorized.add(evidence.document_id)
        self._host_authorized_documents.update(authorized)
        return tuple(sorted(authorized))

    def fork(self) -> ReadonlyToolRegistry:
        """Create a reviewer registry with fresh evidence and host-authorized docs only."""
        forked = ReadonlyToolRegistry(
            guild_id=self.guild_id,
            rag_store=self.rag_store,
            rag_enabled=self.rag_enabled,
            web_search=self.web_search,
            web_enabled=self.web_enabled,
            max_result_chars=self.max_result_chars,
            timeout=self.timeout,
            max_top_k=self.max_top_k,
        )
        forked._inherited_document_authorizations = set(
            self._host_authorized_documents | self._inherited_document_authorizations
        )
        return forked

    async def execute(self, name: str, raw_arguments: str) -> ToolResult:
        if not isinstance(name, str) or name not in {
            "search_admission_guides", "read_admission_guide", "web_search"
        }:
            return ToolResult("unknown", detail="Unknown tool.")
        try:
            arguments = self._parse_arguments(raw_arguments)
            if name == "search_admission_guides":
                query = self._query_argument(arguments, {"query", "top_k"})
                top_k = self._top_k_argument(arguments)
                return await self._search_admission_guides(query, top_k)
            if name == "read_admission_guide":
                document_id, page, chunk = self._read_arguments(arguments)
                return await self._read_admission_guide(document_id, page, chunk)
            query = self._query_argument(arguments, {"query"})
            if not self.web_enabled:
                return ToolResult("disabled", detail="Web search is disabled.")
            return await self._web_search(query)
        except _InvalidArguments as exc:
            return ToolResult("invalid", detail=str(exc))
        except TimeoutError:
            return ToolResult("timeout", detail="Tool execution exceeded its time limit.")
        except Exception:
            return ToolResult("error", detail="Tool execution failed.")

    @staticmethod
    def _parse_arguments(raw_arguments: str) -> dict[str, Any]:
        if not isinstance(raw_arguments, str) or len(raw_arguments) > _ARGUMENTS_MAX_CHARS:
            raise _InvalidArguments("Arguments must be a bounded JSON object.")

        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            parsed: dict[str, Any] = {}
            for key, value in pairs:
                if key in parsed:
                    raise _InvalidArguments("Duplicate argument keys are not allowed.")
                parsed[key] = value
            return parsed

        def reject_nonstandard_constant(_value: str) -> None:
            raise _InvalidArguments("Arguments must use standard JSON values.")

        try:
            parsed = json.loads(
                raw_arguments,
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=reject_nonstandard_constant,
            )
        except _InvalidArguments:
            raise
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise _InvalidArguments("Arguments must be valid JSON.") from exc
        if not isinstance(parsed, dict):
            raise _InvalidArguments("Arguments must be a JSON object.")
        return parsed

    @staticmethod
    def _check_keys(arguments: dict[str, Any], allowed: set[str], required: set[str]) -> None:
        if set(arguments) - allowed:
            raise _InvalidArguments("Unexpected argument key.")
        if required - set(arguments):
            raise _InvalidArguments("Required argument is missing.")

    @classmethod
    def _query_argument(cls, arguments: dict[str, Any], allowed: set[str]) -> str:
        cls._check_keys(arguments, allowed, {"query"})
        query = arguments.get("query")
        if not isinstance(query, str):
            raise _InvalidArguments("query must be a string.")
        query = query.strip()
        if not query or len(query) > _QUERY_MAX_CHARS:
            raise _InvalidArguments("query must contain 1 to 500 characters.")
        return query

    def _top_k_argument(self, arguments: dict[str, Any]) -> int:
        top_k = arguments.get("top_k", min(3, self.max_top_k))
        if isinstance(top_k, bool) or not isinstance(top_k, int):
            raise _InvalidArguments("top_k must be an integer.")
        if not 1 <= top_k <= self.max_top_k:
            raise _InvalidArguments(f"top_k must be between 1 and {self.max_top_k}.")
        return top_k

    @classmethod
    def _read_arguments(cls, arguments: dict[str, Any]) -> tuple[str, int | None, int | None]:
        cls._check_keys(arguments, {"document_id", "page", "chunk"}, {"document_id"})
        document_id = arguments.get("document_id")
        if (
            not isinstance(document_id, str)
            or not _DOCUMENT_ID_RE.fullmatch(document_id)
        ):
            raise _InvalidArguments("document_id must be a bounded identifier.")
        page = arguments.get("page")
        if "page" in arguments and (
            isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 10000
        ):
            raise _InvalidArguments("page must be an integer from 1 to 10000.")
        chunk = arguments.get("chunk")
        if "chunk" in arguments and (
            isinstance(chunk, bool) or not isinstance(chunk, int) or not 0 <= chunk <= 100000
        ):
            raise _InvalidArguments("chunk must be a nonnegative integer.")
        return document_id, page, chunk

    async def _run_sync(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        with _SYNC_GATES_LOCK:
            gate = _SYNC_GATES.get(loop)
            if gate is None:
                gate = asyncio.Semaphore(_SYNC_GATE_LIMIT)
                _SYNC_GATES[loop] = gate
        remaining = deadline - loop.time()
        await wait_for(gate.acquire(), timeout=remaining)
        try:
            task = loop.create_task(asyncio.to_thread(function, *args, **kwargs))
        except BaseException:
            gate.release()
            raise

        def release_when_worker_finishes(worker: asyncio.Task[Any]) -> None:
            try:
                worker.exception()
            except asyncio.CancelledError:
                pass
            finally:
                gate.release()

        task.add_done_callback(release_when_worker_finishes)
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError
        return await wait_for(asyncio.shield(task), timeout=remaining)

    async def _call(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if inspect.iscoroutinefunction(function):
            return await wait_for(function(*args, **kwargs), timeout=self.timeout)
        deadline = asyncio.get_running_loop().time() + self.timeout
        value = await self._run_sync(function, *args, **kwargs)
        if inspect.isawaitable(value):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                if inspect.iscoroutine(value):
                    value.close()
                raise TimeoutError
            return await wait_for(value, timeout=remaining)
        return value

    async def _search_admission_guides(self, query: str, top_k: int) -> ToolResult:
        if not self.rag_enabled:
            return ToolResult("disabled", detail="Admission-guide search is disabled.")
        if self.rag_store is None:
            return ToolResult("unavailable", detail="Admission-guide store is unavailable.")
        query_result = getattr(self.rag_store, "query_result", None)
        query_method = query_result if callable(query_result) else getattr(self.rag_store, "query", None)
        if not callable(query_method):
            return ToolResult("unavailable", detail="Admission-guide search is unavailable.")
        try:
            if query_result is not None and callable(query_result):
                raw_result = await self._call(
                    query_method,
                    guild_id=self.guild_id,
                    query_text=query,
                    top_k=top_k,
                )
            else:
                raw_result = await self._call(
                    query_method,
                    guild_id=self.guild_id,
                    query_text=query,
                    top_k=top_k,
                )
        except TimeoutError:
            raise
        except Exception:
            return ToolResult("error", detail="Admission-guide search failed.")

        status, hits, detail = self._unpack_rag_result(raw_result)
        if status != "ok":
            return self._status_result(status, detail)
        hits = [hit for hit in hits if self._is_scoped_guide_hit(hit)]
        if not hits:
            return ToolResult("no_hits")
        result, evidence = self._evidence_result(hits, source_type="admission_guide")
        self._remember_evidence(evidence)
        self._request_discovered_documents.update(
            item.document_id for item in evidence if item.document_id
        )
        return result

    async def _read_admission_guide(
        self,
        document_id: str,
        page: int | None,
        chunk: int | None,
    ) -> ToolResult:
        if not self.rag_enabled:
            return ToolResult("disabled", detail="Admission-guide reading is disabled.")
        if self.rag_store is None:
            return ToolResult("unavailable", detail="Admission-guide store is unavailable.")
        if (
            document_id not in self._request_discovered_documents
            and document_id not in self._inherited_document_authorizations
        ):
            return ToolResult(
                "invalid",
                detail="document_id must be discovered or host-authorized for this request.",
            )

        read_method = getattr(self.rag_store, "read_document_result", None)
        if not callable(read_method):
            read_method = getattr(self.rag_store, "read_document", None)
        if not callable(read_method):
            return ToolResult("unavailable", detail="Scoped document reading is unavailable.")
        try:
            raw_result = await self._call(
                read_method,
                guild_id=self.guild_id,
                document_id=document_id,
                page=page,
                chunk=chunk,
                limit=self.max_top_k,
            )
        except TimeoutError:
            raise
        except Exception:
            return ToolResult("error", detail="Admission-guide reading failed.")

        status, hits, detail = self._unpack_rag_result(raw_result)
        if status != "ok":
            return self._status_result(status, detail)
        scoped_hits = [
            hit
            for hit in hits
            if self._hit_metadata(hit).get("document_id") == document_id
            and self._hit_metadata(hit).get("kind") == "admission_guide"
            and self._metadata_guild(self._hit_metadata(hit)) == self.guild_id
        ]
        result, evidence = self._evidence_result(scoped_hits, source_type="admission_guide")
        self._remember_evidence(evidence)
        return result

    async def _web_search(self, query: str) -> ToolResult:
        if self.web_search is None:
            return ToolResult("unavailable", detail="Web-search provider is unavailable.")
        try:
            raw_result = await self._call(self.web_search, query)
        except TimeoutError:
            raise
        except Exception:
            return ToolResult("error", detail="Web search failed.")

        status, raw_items, detail = self._unpack_web_result(raw_result)
        if status != "ok":
            return self._status_result(status, detail)
        result, evidence = self._evidence_result(raw_items, source_type="web")
        self._remember_evidence(evidence)
        return result

    @staticmethod
    def _unpack_rag_result(raw_result: Any) -> tuple[str, list[Any], str | None]:
        if isinstance(raw_result, RagQueryResult):
            return raw_result.status, list(raw_result.hits), raw_result.detail
        if isinstance(raw_result, Mapping):
            status = raw_result.get("status")
            hits = raw_result.get("hits", raw_result.get("evidence", []))
            detail = raw_result.get("detail")
        else:
            status = getattr(raw_result, "status", None)
            hits = getattr(raw_result, "hits", None)
            detail = getattr(raw_result, "detail", None)
        if status is None and isinstance(raw_result, (list, tuple)):
            status, hits = ("ok" if raw_result else "no_hits"), raw_result
        if not isinstance(status, str) or status not in {
            "ok", "no_hits", "disabled", "unavailable", "error", "timeout"
        }:
            return "error", [], "Admission-guide store returned an invalid result."
        if status != "ok":
            return status, [], ReadonlyToolRegistry._safe_detail(detail)
        if not isinstance(hits, (list, tuple)):
            return "error", [], "Admission-guide store returned invalid hits."
        if not hits:
            return "no_hits", [], None
        return "ok", list(hits), ReadonlyToolRegistry._safe_detail(detail)

    @staticmethod
    def _unpack_web_result(raw_result: Any) -> tuple[str, list[Any], str | None]:
        status: Any = None
        detail: Any = None
        raw_items: Any = raw_result
        if isinstance(raw_result, Mapping):
            status = raw_result.get("status")
            detail = raw_result.get("detail")
            raw_items = raw_result.get(
                "results",
                raw_result.get("items", raw_result.get("evidence", raw_result.get("hits"))),
            )
            if raw_items is None and any(
                key in raw_result for key in ("title", "name", "text", "snippet", "url", "link")
            ):
                raw_items = [raw_result]
        elif not isinstance(raw_result, (str, bytes, list, tuple)):
            status = getattr(raw_result, "status", None)
            detail = getattr(raw_result, "detail", None)
            raw_items = getattr(
                raw_result,
                "results",
                getattr(raw_result, "items", getattr(raw_result, "evidence", None)),
            )
        if status is not None:
            if not isinstance(status, str) or status not in {
                "ok", "no_hits", "disabled", "unavailable", "error", "timeout"
            }:
                return "error", [], "Web-search provider returned an invalid status."
            if status != "ok":
                return status, [], ReadonlyToolRegistry._safe_detail(detail)
        if raw_items is None:
            return "no_hits", [], None
        if isinstance(raw_items, (str, bytes)):
            text = raw_items.decode("utf-8", errors="replace") if isinstance(raw_items, bytes) else raw_items
            raw_items = [{"title": "Web search result", "text": text}]
        elif not isinstance(raw_items, (list, tuple)):
            raw_items = [raw_items]
        return ("ok" if raw_items else "no_hits"), list(raw_items), ReadonlyToolRegistry._safe_detail(detail)

    @staticmethod
    def _safe_detail(detail: Any) -> str | None:
        if detail is None:
            return None
        if not isinstance(detail, str):
            return "Tool returned a status detail."
        return detail[:60]

    @staticmethod
    def _hit_metadata(hit: Any) -> dict[str, Any]:
        if isinstance(hit, RagHit):
            return dict(hit.metadata or {})
        if isinstance(hit, Mapping):
            metadata = hit.get("metadata")
            return dict(metadata) if isinstance(metadata, Mapping) else dict(hit)
        metadata = getattr(hit, "metadata", {})
        return dict(metadata) if isinstance(metadata, Mapping) else {}

    @staticmethod
    def _metadata_guild(metadata: Mapping[str, Any]) -> int | None:
        try:
            return int(metadata.get("guild_id"))
        except (TypeError, ValueError):
            return None

    def _is_scoped_guide_hit(self, hit: Any) -> bool:
        metadata = self._hit_metadata(hit)
        document_id = metadata.get("document_id")
        return (
            metadata.get("kind") == "admission_guide"
            and self._metadata_guild(metadata) == self.guild_id
            and isinstance(document_id, str)
            and _DOCUMENT_ID_RE.fullmatch(document_id) is not None
        )

    @staticmethod
    def _field(value: Any, *names: str, default: Any = None) -> Any:
        if isinstance(value, Mapping):
            for name in names:
                if name in value:
                    return value[name]
            return default
        for name in names:
            if hasattr(value, name):
                return getattr(value, name)
        return default

    @staticmethod
    def _bounded_text(value: Any, limit: int, *, default: str = "") -> str:
        if value is None:
            return default
        if not isinstance(value, str):
            if isinstance(value, (int, float, bool)):
                value = str(value)
            else:
                return default
        return value[:limit]

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _make_evidence(self, raw: Any, *, source_type: str) -> Evidence | None:
        if source_type == "admission_guide":
            metadata = self._hit_metadata(raw)
            text = self._field(raw, "text", "document", default="")
            document_id = self._bounded_text(metadata.get("document_id"), 128)
            if not _DOCUMENT_ID_RE.fullmatch(document_id):
                document_id = ""
            page = self._optional_int(metadata.get("page"))
            chunk = self._optional_int(metadata.get("chunk_index", metadata.get("chunk")))
            title = self._bounded_text(
                metadata.get("title") or metadata.get("filename"),
                200,
                default="Admission guide",
            ) or "Admission guide"
            source_url = self._bounded_text(metadata.get("source_url"), 500)
            if not source_url.startswith(("http://", "https://")):
                source_url = ""
        else:
            text = self._field(raw, "text", "snippet", "content", "description", default="")
            title = self._bounded_text(self._field(raw, "title", "name", "label"), 200, default="Web result")
            source_url = self._bounded_text(
                self._field(raw, "source_url", "url", "link"), 500
            )
            if not source_url.startswith(("http://", "https://")):
                return None
            document_id = ""
            page = self._optional_int(self._field(raw, "page"))
            chunk = self._optional_int(self._field(raw, "chunk"))

        text = self._bounded_text(text, self.max_result_chars, default="")
        if not text.strip():
            return None
        digest_input = "\0".join(
            (
                str(self.guild_id),
                source_type,
                document_id,
                str(page),
                str(chunk),
                source_url,
                title,
                text,
            )
        )
        prefix = "e_" if source_type == "admission_guide" else "w_"
        evidence_id = prefix + hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:32]
        return Evidence(
            evidence_id=evidence_id,
            title=title,
            document_id=document_id,
            page=page,
            chunk=chunk,
            source_url=source_url,
            source_type=source_type,
            text=text,
        )

    def _evidence_result(
        self,
        raw_items: Iterable[Any],
        *,
        source_type: str,
    ) -> tuple[ToolResult, tuple[Evidence, ...]]:
        candidates: list[Evidence] = []
        seen: set[str] = set()
        for raw in raw_items:
            item = self._make_evidence(raw, source_type=source_type)
            if item is None or item.evidence_id in seen:
                continue
            seen.add(item.evidence_id)
            candidates.append(item)
            if len(candidates) >= self.max_top_k:
                break
        if not candidates:
            return ToolResult("no_hits"), ()

        included: list[Evidence] = []
        truncated = False
        for candidate in candidates:
            full_trial = ToolResult("ok", tuple((*included, candidate)))
            if len(full_trial.to_json()) <= self.max_result_chars:
                included.append(candidate)
                continue

            truncated = True
            low, high = 0, len(candidate.text)
            best: Evidence | None = None
            while low <= high:
                middle = (low + high) // 2
                partial = Evidence(
                    evidence_id=candidate.evidence_id,
                    title=candidate.title,
                    document_id=candidate.document_id,
                    page=candidate.page,
                    chunk=candidate.chunk,
                    source_url=candidate.source_url,
                    source_type=candidate.source_type,
                    text=candidate.text[:middle],
                )
                trial = ToolResult("ok", tuple((*included, partial)))
                if len(trial.to_json()) <= self.max_result_chars:
                    best = partial
                    low = middle + 1
                else:
                    high = middle - 1
            if best is not None and best.text.strip():
                included.append(best)
            break

        if not included:
            return ToolResult("error", detail="Result size limit is too small for evidence."), ()
        detail = "Evidence text was truncated to the result-size limit." if truncated else None
        result = ToolResult("ok", tuple(included), detail)
        if len(result.to_json()) > self.max_result_chars:
            result = ToolResult("ok", tuple(included))
        return result, tuple(included)

    def _remember_evidence(self, evidence: Iterable[Evidence]) -> None:
        for item in evidence:
            self._evidence[item.evidence_id] = item

    @staticmethod
    def _status_result(status: str, detail: str | None) -> ToolResult:
        if status not in {"no_hits", "disabled", "unavailable", "error", "timeout"}:
            return ToolResult("error", detail="Tool returned an invalid status.")
        return ToolResult(status, detail=ReadonlyToolRegistry._safe_detail(detail))
