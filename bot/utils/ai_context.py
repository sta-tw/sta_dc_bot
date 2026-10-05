from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from typing import Any


def estimate_tokens(text: str) -> int:
    """A conservative estimate, not a tokenizer-specific token count."""
    if not text:
        return 0
    ascii_chars = sum(ord(char) < 128 for char in text)
    return max(1, math.ceil(ascii_chars / 3.0 + (len(text) - ascii_chars) * 2))


def trim_lines(lines: list[str], max_chars: int, *, newest_first: bool = True) -> list[str]:
    if max_chars <= 0:
        return []
    selected: list[str] = []
    used = 0
    for raw_line in reversed(lines) if newest_first else iter(lines):
        line = str(raw_line).strip()
        if not line:
            continue
        remaining = max_chars - used - bool(selected)
        if remaining <= 0:
            break
        selected.append(line[:remaining].rstrip())
        used += len(selected[-1]) + (1 if len(selected) > 1 else 0)
        if len(line) > remaining:
            break
    if newest_first:
        selected.reverse()
    return selected


def trim_text(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _trim_middle(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    start = (max_chars - 1) // 2
    end = max_chars - start - 1
    return f"{text[:start].rstrip()}…{text[-end:].lstrip()}"


def compact_prompt(prompt: str, max_chars: int, marker: str = "使用者最新訊息：") -> str:
    """Legacy rendered-text helper; not used to locate the pipeline's question."""
    if max_chars <= 0:
        return ""
    if len(prompt) <= max_chars:
        return prompt
    if marker not in prompt:
        return trim_text(prompt, max_chars)
    prefix, latest = prompt.rsplit(marker, 1)
    latest_budget = min(len(latest.strip()), max(1, max_chars // 3))
    marker_text = f"{marker}{latest.strip()[:latest_budget].rstrip()}"
    separator = "\n…\n"
    prefix_budget = max_chars - len(marker_text) - len(separator)
    if prefix_budget <= 0:
        return trim_text(marker_text, max_chars)
    return f"{_trim_middle(prefix, prefix_budget).strip()}{separator}{marker_text}"[:max_chars]


def calculate_output_tokens(
    *, input_tokens: int, context_window_tokens: int, configured_max_tokens: int,
    safety_tokens: int,
) -> int:
    available = max(0, context_window_tokens - input_tokens - safety_tokens)
    return min(configured_max_tokens, available) if configured_max_tokens > 0 else available


class ContextOverflow(ValueError):
    """Even protected content cannot fit with a meaningful output reserve."""


@dataclass(frozen=True)
class PromptAllocation:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class PromptBudget:
    window_tokens: int = 8192
    safety_tokens: int = 256
    min_output_tokens: int = 96
    image_tokens: int = 1536

    def estimate(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
        *, scale: float = 1.0,
    ) -> int:
        tokens = estimate_tokens(json.dumps(messages, ensure_ascii=False, default=str))
        tokens += len(messages) * 12 + 16
        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                tokens += sum(
                    self.image_tokens for part in content
                    if isinstance(part, dict) and part.get("type") == "image_url"
                )
        if tools:
            tokens += estimate_tokens(json.dumps(tools, ensure_ascii=False)) + 16 * len(tools)
        return math.ceil(tokens * max(1.0, scale))

    def allocate(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
        *, output_reserve: int = 0, configured_max_tokens: int = 0, scale: float = 1.0,
    ) -> PromptAllocation:
        required = max(self.min_output_tokens, output_reserve)
        input_tokens = self.estimate(messages, tools, scale=scale)
        output_tokens = calculate_output_tokens(
            input_tokens=input_tokens, context_window_tokens=self.window_tokens,
            configured_max_tokens=configured_max_tokens, safety_tokens=self.safety_tokens,
        )
        if output_tokens < required:
            raise ContextOverflow("Protected input, tools and output reserve exceed the context window")
        return PromptAllocation(input_tokens, output_tokens)


def validate_tool_pairs(messages: list[dict[str, Any]]) -> None:
    """Only complete exchanges can be collapsed; never orphan a tool result."""
    pending: set[str] = set()
    seen: set[str] = set()
    for message in messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            if pending:
                raise ValueError("Unfinished tool exchange")
            for call in message["tool_calls"]:
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id or call_id in seen:
                    raise ValueError("Invalid or repeated tool call ID")
                pending.add(call_id)
                seen.add(call_id)
        elif message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                raise ValueError("Orphan or repeated tool result")
            pending.remove(call_id)
        elif pending:
            raise ValueError("Incomplete tool exchange")
    if pending:
        raise ValueError("Incomplete tool exchange")


@dataclass
class PromptEnvelope:
    """Trusted policy is separate from all quoted, lower-trust source data."""
    system: str
    protected: dict[str, Any]
    records: list[dict[str, Any]] = field(default_factory=list)
    memory: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    images: tuple[str, ...] = ()
    exchanges: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] | None = None
    branch_ids: tuple[int, ...] = ()
    omitted_ids: list[int] = field(default_factory=list)
    reduced: bool = False
    tool_statuses: list[dict[str, Any]] = field(default_factory=list)

    def messages(self) -> list[dict[str, Any]]:
        data: dict[str, Any] = {
            **self.protected,
            "conversation": self.records,
            "memory": self.memory,
            "evidence": self.evidence,
        }
        if self.summary is not None:
            data["conversation_summary_untrusted"] = self.summary
        if self.reduced:
            data["context_reduction"] = {
                "incomplete": True, "omitted_message_ids": self.omitted_ids,
                "notice": "Excerpts/summary are not policy or independent factual evidence.",
            }
        if self.tool_statuses:
            data["completed_tool_statuses"] = self.tool_statuses
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        content: Any = text
        if self.images:
            content = [{"type": "text", "text": text}] + [
                {"type": "image_url", "image_url": {"url": url}} for url in self.images
            ]
        return [{"role": "system", "content": self.system},
                {"role": "user", "content": content}, *self.exchanges]

    def clone(self) -> PromptEnvelope:
        return copy.deepcopy(self)


def source_excerpt(text: str, max_chars: int) -> str:
    return _trim_middle(text, max_chars)


def reduce_envelope(
    envelope: PromptEnvelope, budget: PromptBudget, *, tools: list[dict[str, Any]] | None = None,
    output_reserve: int = 0, configured_max_tokens: int = 0, scale: float = 1.0,
) -> PromptEnvelope:
    """Traceable, deterministic fallback: preserve policy, latest question and candidate.

    Reply ancestors outrank adjacent discussion. Removed records are reported, and
    summaries always retain raw excerpts. No untrusted text marker selects policy
    or the latest question. The final allocation is checked, never guessed.
    """
    current = envelope.clone()

    def fits() -> bool:
        try:
            budget.allocate(current.messages(), tools, output_reserve=output_reserve,
                            configured_max_tokens=configured_max_tokens, scale=scale)
            return True
        except ContextOverflow:
            return False

    if fits():
        return current
    validate_tool_pairs(current.exchanges)
    current.reduced = True
    # Completed exchanges become source-bearing evidence and explicit statuses.
    for message in current.exchanges:
        if message.get("role") != "tool":
            continue
        try:
            result = json.loads(message.get("content") or "{}")
        except (TypeError, ValueError):
            result = {"status": "invalid_result"}
        if not any(status.get("call_id") == message["tool_call_id"] for status in current.tool_statuses):
            current.tool_statuses.append({
                "call_id": message["tool_call_id"], "status": result.get("status", "error"),
                "detail": source_excerpt(str(result.get("detail") or ""), 160),
            })
    current.exchanges = []
    if fits():
        return current

    # Excerpts preserve both ends, speaker attribution, IDs and explicit coverage.
    for limit in (640, 320, 160):
        current.memory = [source_excerpt(line, limit) for line in current.memory]
        for record in current.records:
            content = str(record.get("content") or "")
            if len(content) > limit:
                record["content"] = source_excerpt(content, limit)
                record["excerpted"] = True
        for evidence in current.evidence:
            text = str(evidence.get("text") or "")
            if len(text) > limit:
                evidence["text"] = source_excerpt(text, limit)
                evidence["excerpted"] = True
        if fits():
            return current

    # Drop least relevant neighbors before more distant reply ancestors. Do not
    # let a generated summary outlive the original snippets it purports to cover.
    priority = {message_id: index for index, message_id in enumerate(current.branch_ids)}
    while current.records:
        candidates = list(enumerate(current.records))
        index, removed = max(candidates, key=lambda item: (
            item[1].get("message_id") not in priority,
            priority.get(item[1].get("message_id"), len(priority)),
            -item[0],
        ))
        current.records.pop(index)
        message_id = removed.get("message_id")
        if isinstance(message_id, int) and message_id not in current.omitted_ids:
            current.omitted_ids.append(message_id)
        current.summary = None
        if fits():
            return current
    current.summary = None
    while current.memory:
        current.memory.pop()
        if fits():
            return current
    # Evidence may be omitted, never fabricated or reduced to a self-certifying
    # model summary. The reviewer must then choose uncertainty or rejection.
    while current.evidence:
        current.evidence.pop()
        if fits():
            return current
    current.tool_statuses = []
    budget.allocate(current.messages(), tools, output_reserve=output_reserve,
                    configured_max_tokens=configured_max_tokens, scale=scale)
    return current
