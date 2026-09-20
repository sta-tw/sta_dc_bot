from __future__ import annotations

import math


def estimate_tokens(text: str) -> int:
    """Estimate tokens conservatively without assuming the vLLM tokenizer."""
    if not text:
        return 0

    ascii_chars = sum(ord(char) < 128 for char in text)
    other_chars = len(text) - ascii_chars
    estimated = (ascii_chars / 4.0) + other_chars
    return max(1, math.ceil(estimated))


def trim_lines(
    lines: list[str],
    max_chars: int,
    *,
    newest_first: bool = True,
) -> list[str]:
    """Keep lines within a character budget, preserving the requested order."""
    if max_chars <= 0:
        return []

    selected: list[str] = []
    used = 0
    source_lines = reversed(lines) if newest_first else iter(lines)
    for raw_line in source_lines:
        line = str(raw_line).strip()
        if not line:
            continue

        separator = 1 if selected else 0
        remaining = max_chars - used - separator
        if remaining <= 0:
            break
        if len(line) > remaining:
            line = line[:remaining].rstrip()
        if not line:
            break

        selected.append(line)
        used += len(line) + separator
        if len(line) < len(str(raw_line).strip()):
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

    head_chars = (max_chars - 1) // 2
    tail_chars = max_chars - head_chars - 1
    return f"{text[:head_chars].rstrip()}…{text[-tail_chars:].lstrip()}"


def compact_prompt(prompt: str, max_chars: int, marker: str = "使用者最新訊息：") -> str:
    """Reduce a rendered prompt while keeping evidence and the latest request."""
    if max_chars <= 0:
        return ""
    if len(prompt) <= max_chars:
        return prompt

    if marker not in prompt:
        return trim_text(prompt, max_chars)

    prefix, latest = prompt.rsplit(marker, 1)
    latest = latest.strip()
    latest_budget = min(len(latest), max(1, max_chars // 3))
    compacted_latest = latest[:latest_budget].rstrip()
    marker_text = f"{marker}{compacted_latest}"
    separator = "\n…\n"
    prefix_budget = max_chars - len(marker_text) - len(separator)
    if prefix_budget <= 0:
        return trim_text(marker_text, max_chars)

    compacted_prefix = _trim_middle(prefix, prefix_budget).strip()
    compacted = f"{compacted_prefix}{separator}{marker_text}"
    return compacted[:max_chars]


def calculate_output_tokens(
    *,
    input_tokens: int,
    context_window_tokens: int,
    configured_max_tokens: int,
    safety_tokens: int,
) -> int:
    """Return an output cap that fits the remaining context window."""
    available = context_window_tokens - input_tokens - safety_tokens
    if available <= 0:
        return 1
    if configured_max_tokens <= 0:
        return available
    return max(1, min(configured_max_tokens, available))
